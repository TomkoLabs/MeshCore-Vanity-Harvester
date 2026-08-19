# Benchmarks

Run with `cargo bench`:

## Prefix matching overhead

Isolated prefix check cost per key:

| Prefixes | Time per check |
|----------|---------------|
| 1        | ~0.5 ns       |
| 5        | ~2.4 ns       |
| 10       | ~4.7 ns       |

## Full keygen + match pipeline

End-to-end per-key cost (single thread):

| Prefixes | Time per key | Throughput |
|----------|-------------|------------|
| 0 (keygen only) | 12.5 µs | 80K keys/s |
| 1        | 12.5 µs     | 80K keys/s |
| 5        | 12.2 µs     | 82K keys/s |
| 10       | 12.2 µs     | 82K keys/s |
| 20       | 12.2 µs     | 82K keys/s |

Multi-prefix matching adds **no measurable overhead** — the prefix check (~5 ns for 10 prefixes) is dwarfed by the Ed25519 scalar multiplication (~12.5 µs).

Measured on AMD Ryzen 9 7950X3D.
