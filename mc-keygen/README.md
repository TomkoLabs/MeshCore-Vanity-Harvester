# mc-keygen

Vanity Ed25519 key generator for [MeshCore](https://github.com/ripplebiz/MeshCore).
Harvests patterns beginning at character zero, or searches chosen exact prefixes.

Originally [samschlegel/mc-keygen](https://github.com/samschlegel/mc-keygen);
now maintained as part of this project. See [ATTRIBUTION.md](ATTRIBUTION.md)
for what was kept, what was removed and why.

It is normally driven by the harvester rather than run by hand, so the result
goes to stdout and progress goes to stderr.

## Usage

```
mc-keygen <PREFIX>... [OPTIONS]
mc-keygen verify-pairs
mc-keygen harvest --policy POLICY.json --seconds 900 [--gpu-only]
```

**Options**

- `-t, --threads <N>` — worker threads (default: all cores)
- `--json` — print the result as JSON, and stay quiet on stderr
- `--cpu-only` — skip the GPU even if one is available *(needs `cuda`)*
- `--gpu-only` — no CPU threads *(needs `cuda`)*
- `--verify` — cross-check GPU key generation against the host *(needs `cuda`)*

**`verify-pairs`** reads JSON Lines on stdin, each `{"public_key":…,
"private_key":…}`, and writes one verdict per line. The harvester uses it to
re-derive saved keys on startup: keys found on the GPU have no seed, so they
must be checked by scalar multiplication, which costs about 95 ms each in
Python and microseconds here.

**`harvest`** streams multiple independently generated matches and exact attempt
statistics as JSON Lines. Python generates its policy; normally use `./run.sh`
from the project root. `--stdin-stop` enables graceful supervisor shutdown and
`--benchmark` emits only statistics, never private keys. CUDA harvesting runs a
mandatory host/device verification before accepting work. See
[GPU details](docs/gpu.md) and [search strategy](../docs/search-strategy.md).

**Examples**

```bash
mc-keygen C0FFEE                    # hybrid if a GPU is available
mc-keygen C0FFEE DEAD BEEF          # match any of these prefixes
mc-keygen C0FFEE --json             # machine-readable
mc-keygen C0FFEEC0F --gpu-only      # GPU only, for longer prefixes
echo '{"public_key":"AB…","private_key":"CD…"}' | mc-keygen verify-pairs
```

Multiple distinct equal-length prefixes increase the hit probability in
proportion to their count, but matching overhead also grows. The legacy JSON
output names the one that matched. Prefix harvesting uses a shared word trie
and pattern predicates rather than this exact-prefix list.

## Search difficulty

Each hex character multiplies the expected attempts by 16.

| Prefix | Expected attempts | CPU¹ | GPU¹ |
|--------|-------------------|------|------|
| 6 char | ~16M | ~12s | <1s |
| 7 char | ~268M | ~3.5m | ~4s |
| 8 char | ~4.3B | ~55m | ~63s |
| 9 char | ~69B | ~15h | ~17m |

¹ CPU: Ryzen 9 7950X3D, 32 threads (~1.3M keys/s). GPU: RTX 3080 (~460M keys/s),
measured against this project. See [docs/gpu.md](docs/gpu.md).

## Building

```bash
cargo build --release                    # CPU only
cargo build --release --features cuda    # with NVIDIA GPU support
```

The CUDA kernel is compiled at runtime by NVRTC, so building needs no CUDA
toolkit; GPU execution needs the driver and NVRTC libraries. Rust 1.88+
is required for the locked CUDA dependencies. A C compiler builds the shared
CPU/GPU filter. `install.sh` in
the project root handles this for you.

## How it works

1. Draw a random scalar from the OS CSPRNG and clamp it.
2. Multiply the Ed25519 base point by it to get a public key.
3. If the hex starts with a target prefix, return; otherwise advance the scalar
   by 8 and add the corresponding point — far cheaper than a fresh
   multiplication, and the reason GPU search is worth doing.

Keys starting with `00` or `FF` are skipped; MeshCore reserves them.

Because candidates come from advancing a scalar rather than hashing a seed,
found keys have no 32-byte seed — only the 64-byte expanded private key, which
is what MeshCore stores anyway.

## Sources

- [MeshCore](https://github.com/ripplebiz/MeshCore) — the firmware these keys are for
- [Ed25519 / RFC 8032](https://datatracker.ietf.org/doc/html/rfc8032)
- [curve25519-dalek](https://github.com/dalek-cryptography/curve25519-dalek)
