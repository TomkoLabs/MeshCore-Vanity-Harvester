//! MeshCore vanity Ed25519 key generator.
//!
//! Absorbed from https://github.com/samschlegel/mc-keygen (upstream commit
//! 62ed67f), dual-licensed MIT or Apache-2.0. See ATTRIBUTION.md.

// The `gpu` feature is an umbrella implied by a backend; never enable it alone.
#[cfg(all(feature = "gpu", not(feature = "cuda")))]
compile_error!("feature `gpu` requires a backend; enable `cuda`");

pub mod keygen;
pub mod harvest;
pub mod search;
pub mod types;

#[cfg(feature = "cuda")]
pub mod gpu;

#[cfg(feature = "cuda")]
pub mod gpu_harvest;
