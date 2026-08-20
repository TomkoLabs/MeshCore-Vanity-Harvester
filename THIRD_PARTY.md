# Third-party components

## mc-keygen

`mc-keygen/` began as
[samschlegel/mc-keygen](https://github.com/samschlegel/mc-keygen), upstream
commit `62ed67f`, dual-licensed MIT or Apache-2.0. It was a vendored subtree;
it is now maintained as part of this project, reduced to what the harvester
actually needs.

Full provenance, including exactly what was kept, what was removed and why, is
in [mc-keygen/ATTRIBUTION.md](mc-keygen/ATTRIBUTION.md). The original
`LICENSE-MIT`, `LICENSE-APACHE` and `cuda/THIRD-PARTY-NOTICES` are retained
unmodified in that directory.

The one file that matters most is `mc-keygen/cuda/vanity_kernel.cu` — upstream's
hand-written Ed25519 field arithmetic, kept unchanged. It is the reason the GPU
path is worth having, and the reason absorbing was preferred to rewriting.

## Python dependencies

`cryptography` (Apache-2.0 / BSD-3-Clause) provides the Ed25519 implementation
used to derive and check keys on the CPU side.
