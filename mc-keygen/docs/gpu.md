# GPU acceleration

Build with `cargo build --release --features cuda --locked`. The locked CUDA
dependencies require Rust 1.88+. Runtime use needs NVIDIA's driver and NVRTC
libraries. The `cuda-12040` cudarc feature selects driver API bindings; NVRTC
must also understand the GPU's actual architecture. The program selects the
detected compute capability at runtime.

## Prefix harvesting

The supervisor generates a screening policy and invokes `mc-keygen harvest`.
The first six characters must show a desirable ID. Leading runs, sequences,
periodic units, palindromes, words, word extensions and arbitrary catalog-word
chains are then screened from character zero. Buried and suffix-only patterns
are excluded. There is no 16-character span ceiling: qualifying continuations
can reach the end of the 64-character key. Policy v2 is advertised in backend
help; older binaries use the optional exact-prefix path until rebuilt.

Each GPU thread starts with an independent OS-random clamped scalar, uses
`+8B` additions and Montgomery batch inversion for up to 256 candidates, and
retires at its first hit. Only one private key is retained per chain, avoiding
related-scalar outputs. A three-byte visible-ID gate runs before computing the
x-coordinate sign bit; most candidates avoid that extra field multiplication.
Survivors receive their complete sign encoding before long-prefix screening.
Device buffers persist between launches.

The output buffer holds 4,096 matches. Overflow discards the incomplete output
and replays the same starts in smaller disjoint thread groups. All first hits
are recovered without double-counting useful attempts. Replay work is reported
separately. Every emitted pair is independently verified in Rust; Python
re-derives accepted pairs and decides their ranking.

A mandatory GPU startup self-test checks multiple threads against CPU results,
including chain advancement, sign bits, exact counters and forced overflow.
The original field arithmetic in `vanity_kernel.cu` is unchanged.

## Measurement

From the project root:

```bash
.venv/bin/python scripts/benchmark_harvest.py --seconds 60
```

This invokes `harvest --benchmark`: statistics only, no private keys printed.
Divide the final `attempts` by `elapsed_secs` for useful keys/second. Native
initialization is excluded. The measurement includes prefix filtering, random
starts, transfers, Rust verification and overflow recovery. It excludes Python
ranking and persistence.

Historical exact-prefix rates do not measure this workload. The anchored
filter stops at the first mismatch and catches generic families
outside a fixed target list. It avoids the former all-position scans; the
end-to-end gain still depends on how much time the device spends on curve math.
Benchmark the same policy on each machine. See
[search strategy](../../docs/search-strategy.md) for probabilities and hardware.

## Legacy search and verification

Exact prefixes remain available with `mc-keygen PREFIX... --gpu-only` or the
supervisor's `--prefix-campaigns`. Its first-match kernel can stop partway
through a launch while reporting the full batch size. Short-prefix statistics
are therefore not exact throughput measurements.

`mc-keygen A --gpu-only --verify` checks the original chain arithmetic. Prefix
harvesting always performs its additional device self-test automatically.

Validation here covers Rust CPU/CUDA builds, differential filter tests, host
execution of the harvest kernel, and NVRTC compilation for compute 8.6 and
12.1. No GPU was available for actual device execution or performance
measurement. The runtime self-test must pass on the user's GPU.
