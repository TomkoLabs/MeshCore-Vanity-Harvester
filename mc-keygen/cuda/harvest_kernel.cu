// Prefix-pattern harvest kernel. Included after vanity_kernel.cu so the
// established field arithmetic and batch-inversion routines are reused.
// Output: u32 matches, u32 padding, u64 actual attempts, then capacity
// records of [public key:32][scalar:32]. Overflow is replayed by the host.

extern "C" __global__ void vanity_harvest(
    u8 *result, const u8 *start_scalars,
    unsigned int thread_offset, unsigned int thread_count,
    const unsigned int *policy, const unsigned int *trie,
    unsigned int capacity, unsigned long long iters_per_thread)
{
    unsigned int tid = blockIdx.x * blockDim.x + threadIdx.x;
    if (tid >= thread_count) return;
    u8 my_scalar[32];
    for (int i = 0; i < 32; ++i)
        my_scalar[i] = start_scalars[32ULL * (thread_offset + tid) + i];
    unsigned long long checked = 0;

    ge_p3 A;
    ge_scalarmult_base(&A, my_scalar);

    // Per-thread batch storage. fe is int32[10] = 40 bytes; total ~2.5 KB/thread
    // for B=16. Expect compiler to spill to local memory but it's cached.
    fe batch_X[VANITY_BATCH];
    fe batch_Y[VANITY_BATCH];
    fe batch_Z[VANITY_BATCH];
    fe products[VANITY_BATCH];

    unsigned long long batches = iters_per_thread / VANITY_BATCH;
    for (unsigned long long b = 0; b < batches; b++) {

        // Phase 1: snapshot (X,Y,Z) at each chain step and accumulate the
        // running product of Zs. After this loop A has advanced past the batch.
        for (int i = 0; i < VANITY_BATCH; i++) {
            for (int k = 0; k < 10; k++) {
                batch_X[i][k] = A.X[k];
                batch_Y[i][k] = A.Y[k];
                batch_Z[i][k] = A.Z[k];
            }
            if (i == 0) {
                for (int k = 0; k < 10; k++) products[i][k] = batch_Z[i][k];
            } else {
                fe_mul(products[i], products[i-1], batch_Z[i]);
            }
            ge_p1p1 r;
            ge_madd(&r, &A, &base[0][7]);
            ge_p1p1_to_p3(&A, &r);
        }

        // Phase 2: one inversion of the full product.
        fe all_inv;
        fe_invert(all_inv, products[VANITY_BATCH - 1]);

        // Phase 3: backward pass to extract individual Z inverses. Overwrites
        // batch_Z in place (its original values get rolled into `running`
        // before being clobbered).
        fe running;
        for (int k = 0; k < 10; k++) running[k] = all_inv[k];
        for (int i = VANITY_BATCH - 1; i > 0; i--) {
            fe temp_inv;
            fe_mul(temp_inv, running, products[i-1]);  // = Z_i^{-1}
            fe new_running;
            fe_mul(new_running, running, batch_Z[i]);  // = (Z_0 * ... * Z_{i-1})^{-1}
            for (int k = 0; k < 10; k++) {
                running[k] = new_running[k];
                batch_Z[i][k] = temp_inv[k];
            }
        }
        for (int k = 0; k < 10; k++) batch_Z[0][k] = running[k];

        // Phase 4: compute y = Y/Z, encode, prefix-check each point.
        for (int i = 0; i < VANITY_BATCH; i++) {
            fe y;
            fe_mul(y, batch_Y[i], batch_Z[i]);
            u8 pubkey[32];
            fe_tobytes(pubkey, y);
            ++checked;
            // Most IDs fail after three bytes. Defer the x-coordinate/sign
            // multiplication until a visible ID survives this cheap gate.
            if (!harvest_visible_id(pubkey, policy, trie)) continue;
            // Complete the encoding before testing prefixes reaching byte 31.
            fe x;
            fe_mul(x, batch_X[i], batch_Z[i]);
            pubkey[31] ^= fe_isnegative(x) << 7;
            if (harvest_candidate(pubkey, policy, trie)) {
                u8 match_scalar[32];
                for (int k = 0; k < 32; k++) match_scalar[k] = my_scalar[k];
                unsigned long long matched_offset = 8ULL * (b * VANITY_BATCH + (unsigned long long)i);
                unsigned long long carry = 0;
                for (int k = 0; k < 32; k++) {
                    unsigned long long byte_add = (k < 8) ? ((matched_offset >> (k * 8)) & 0xFFULL) : 0ULL;
                    unsigned long long s = (unsigned long long)match_scalar[k] + byte_add + carry;
                    match_scalar[k] = (u8)(s & 0xFFULL);
                    carry = s >> 8;
                }

                unsigned int slot = atomicAdd((unsigned int *)result, 1);
                if (slot < capacity) {
                    u8 *entry = result + 16 + 64ULL * slot;
                    for (int k = 0; k < 32; k++) entry[k] = pubkey[k];
                    for (int k = 0; k < 32; k++) entry[32 + k] = match_scalar[k];
                }
                atomicAdd((unsigned long long *)(result + 8), checked);
                // At most one retained key per independently random chain.
                // A match retires this thread; the next launch reseeds it.
                return;
            }
        }
    }
    atomicAdd((unsigned long long *)(result + 8), checked);
}
