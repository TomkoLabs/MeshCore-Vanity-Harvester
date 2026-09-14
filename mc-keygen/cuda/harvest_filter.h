// Screening ABI v2: visible six-character ID, then anchored patterns only.
// Also compiled as C/C++ for host screening and differential tests.
#if defined(__CUDACC__)
#define MC_FILTER __device__ __forceinline__
#else
#define MC_FILTER static inline
#endif

MC_FILTER unsigned int harvest_nibble(const unsigned char *key, int index) {
    return (key[index / 2] >> ((index & 1) ? 0 : 4)) & 15;
}

// Reads only bytes 0..2: safe before computing the compressed point's sign.
// Low run thresholds also exercise this same path in the device self-test.
MC_FILTER int harvest_visible_id(const unsigned char *key,
                                const unsigned int *p,
                                const unsigned int *trie) {
    if (key[0] == 0 || key[0] == 255) return 0;
    unsigned int n[6];
    for (int i = 0; i < 6; ++i) n[i] = harvest_nibble(key, i);
    unsigned int run = 1;
    while (run < 4 && n[run] == n[0]) ++run;
    if (run >= 4 || run >= p[1]) return 1;
    int up = 1, down = 1;
    for (int i = 1; i < 5; ++i) {
        up &= n[i] == n[i-1] + 1;
        down &= n[i] + 1 == n[i-1];
    }
    if (up || down) return 1;
    if ((n[0] == n[2] && n[2] == n[4] && n[1] == n[3] && n[3] == n[5]) ||
        (n[0] == n[3] && n[1] == n[4] && n[2] == n[5]) ||
        (n[0] == n[1] && n[1] == n[2] && n[3] == n[4] && n[4] == n[5]) ||
        (n[0] == n[1] && n[2] == n[3] && n[4] == n[5]) ||
        (n[0] == n[5] && n[1] == n[4] && n[2] == n[3])) return 1;
    unsigned int node = 0;
    for (int i = 0; i < 6; ++i) {
        node = trie[node*17 + n[i]];
        if (!node) return 0;
        if (trie[node*17+16]) return 1;
    }
    return 1; // first six characters of a longer catalog word/preference
}

MC_FILTER int harvest_candidate(const unsigned char *key,
                                const unsigned int *p,
                                const unsigned int *trie) {
    if (!harvest_visible_id(key, p, trie)) return 0;
    unsigned char n[64];
    for (int i = 0; i < 64; ++i) n[i] = (unsigned char)harvest_nibble(key, i);

    unsigned int run = 1, up = 1, down = 1;
    while (run < 64 && n[run] == n[0]) ++run;
    if (run >= p[1]) return 1;
    while (up < 16 && n[up] == n[up-1] + 1) ++up;
    while (down < 16 && n[down] + 1 == n[down-1]) ++down;
    if (up >= p[3] || down >= p[3]) return 1;
    if (p[7] && ((n[0] == n[1] && n[1] == n[2] && n[3] == n[4] && n[4] == n[5]) ||
                 (n[0] == n[1] && n[2] == n[3] && n[4] == n[5]))) return 1;

    for (int unit = 2; unit <= 12; ++unit) {
        if (p[8+unit] > 64) continue;
        unsigned int span = (unsigned int)unit;
        while (span < 64 && n[span] == n[span-unit]) ++span;
        if (span >= p[8+unit]) return 1;
    }
    for (unsigned int length = p[5]; length <= 64; ++length) {
        unsigned int left = 0;
        while (left < length / 2 && n[left] == n[length-1-left]) ++left;
        if (left == length / 2) return 1;
    }

    int prefix_word = 0;
    unsigned int node = 0;
    for (int end = 0; end < 64; ++end) {
        node = trie[node*17 + n[end]];
        if (!node) break;
        unsigned int flags = trie[node*17+16];
        int length = end+1;
        if (flags & 2) return 1;
        if (!(flags & 1)) continue;
        if (p[34+length]) return 1;
        prefix_word = 1;
        unsigned int tail = (unsigned int)length;
        while (tail < 64 && n[tail] == n[length-1]) ++tail;
        if (tail >= p[164+length]) return 1;
        unsigned int repeated = (unsigned int)length;
        while (repeated < 64 && n[repeated] == n[repeated % length]) ++repeated;
        if (repeated >= p[229+length]) return 1;
    }
    if (!prefix_word) return 0;

    // Dynamic programming recognizes arbitrary catalog-word chains and tails.
    // Retain every endpoint: a shorter chain can earn a longer final run.
    unsigned char depth[65];
    for (int i = 0; i <= 64; ++i) depth[i] = 255;
    depth[0] = 0;
    for (int start = 0; start < 64; ++start) {
        if (depth[start] == 255) continue;
        node = 0;
        for (int end = start; end < 64; ++end) {
            node = trie[node*17 + n[end]];
            if (!node) break;
            if (!(trie[node*17+16] & 1)) continue;
            int length = end+1;
            if (depth[length] > depth[start]+1) depth[length] = depth[start]+1;
            if (depth[length] < 2) continue;
            if (p[294+length]) return 1;
            unsigned int tail = (unsigned int)length;
            while (tail < 64 && n[tail] == n[length-1]) ++tail;
            if (tail >= p[359+length]) return 1;
        }
    }
    return 0;
}
