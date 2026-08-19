# Third-party components

## mc-keygen

The `mc-keygen/` directory is derived from [samschlegel/mc-keygen](https://github.com/samschlegel/mc-keygen), upstream commit `62ed67f`.

It is distributed under the MIT License or Apache License 2.0. The original `LICENSE-MIT`, `LICENSE-APACHE`, source notices, and CUDA third-party notices are retained in that directory.

Local integration currently selects the `cudarc` CUDA 12.4 feature. Other CUDA toolkit versions may require changing the corresponding `cudarc` feature in `mc-keygen/Cargo.toml` before building.
