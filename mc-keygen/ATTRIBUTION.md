# Where this came from

`mc-keygen` originated as
[samschlegel/mc-keygen](https://github.com/samschlegel/mc-keygen), upstream
commit `62ed67f`, dual-licensed MIT or Apache-2.0. `LICENSE-MIT` and
`LICENSE-APACHE` are the original files, retained unmodified, and
`cuda/THIRD-PARTY-NOTICES` records the kernel's own third-party attributions.

It was a vendored subtree of that repository. It is now maintained here, so
that it can be reduced to what this project actually needs and improved
alongside it. Upstream remains the reference for anything not kept below, and
is the right place to look for a Metal backend, an interactive progress
display, or the benchmarking harness.

## What was kept, unchanged in substance

- `cuda/vanity_kernel.cu` — the Ed25519 field arithmetic that makes GPU search
  worthwhile. It generates successive candidates by repeated point addition
  rather than a full scalar multiplication per key, which is where the
  hundreds-of-millions-per-second figure comes from. This file is upstream's
  work and the reason absorbing rather than rewriting was the right call.
- `src/search.rs` — CPU worker pool, prefix matching, GPU dispatch loop.
- `src/gpu.rs` — CUDA device setup and kernel driving.
- `src/keygen.rs`, `src/types.rs` — key derivation and the result types.

## What was removed, and why

| Removed | Reason |
|---|---|
| `build.rs` and the `vergen-gix` build dependency | Stamped a Git version string nothing ever read, and pulled roughly 94 crates — the entire gitoxide stack — into every build. |
| `src/bench.rs`, `benches/`, the `bench` subcommand, `criterion`, `chrono` | A benchmarking harness for tuning the kernel. Useful upstream; dead weight here. |
| The `ratatui` progress display, `crossterm`, `sysinfo` | An interactive terminal UI. This binary is driven by the harvester and never seen by a person, so progress is now a plain line on stderr. About 17 crates. |
| The Metal backend and `metal/` kernels | macOS-only. This project targets Debian. Upstream still has it. |

Together that removed about 111 of 348 crates from the dependency graph.

## What was added

- A plain-text progress line on stderr, so the tool is still usable by hand
  without a terminal UI.
- `verify-pairs`, a batch verification mode. The harvester re-derives every
  saved key on startup to make sure nothing was tampered with, and keys found
  on the GPU carry no seed to re-derive from, so it must do the elliptic curve
  arithmetic itself — about 95 ms per key in Python. This mode does the same
  work in Rust in microseconds.
- One error path. Upstream called `std::process::exit` from several places;
  everything now returns a `Result` to a single exit point.

## Keeping in step with upstream

Upstream fixes can still be taken by hand. Compare against
`https://github.com/samschlegel/mc-keygen` and apply what is relevant to the
files listed as kept — `cuda/vanity_kernel.cu` above all.
