//! MeshCore Ed25519 key derivation.
//!
//! Absorbed from https://github.com/samschlegel/mc-keygen (upstream commit
//! 62ed67f). `generate_keypair` is upstream's; `public_key_from_private_hex`
//! was added here to back the `verify-pairs` command.

use curve25519_dalek::EdwardsPoint;
use sha2::{Digest, Sha512};

use crate::types::MeshCoreKeypair;

/// Generate a MeshCore Ed25519 keypair from a 32-byte seed.
///
/// The search path does not use this — it advances a scalar directly, which is
/// the whole reason it is fast. This remains the canonical statement of the
/// derivation, and the oracle the verification tests check against.
///
/// Algorithm (matching the mc-keygen web tool):
/// 1. SHA-512(seed) -> 64 bytes
/// 2. Clamp first 32 bytes: [0] &= 248, [31] &= 63, [31] |= 64
/// 3. Multiply Ed25519 base point by clamped scalar -> 32-byte public key
/// 4. Private key = clamped_scalar[0..32] || sha512_digest[32..64]
pub fn generate_keypair(seed: &[u8; 32]) -> MeshCoreKeypair {
    let hash = Sha512::digest(seed);

    let mut scalar_bytes = [0u8; 32];
    scalar_bytes.copy_from_slice(&hash[..32]);

    // Clamp the scalar
    scalar_bytes[0] &= 248;
    scalar_bytes[31] &= 63;
    scalar_bytes[31] |= 64;

    // Multiply base point by clamped scalar
    // mul_base_clamped applies clamping internally, but clamping is idempotent
    let public_point: EdwardsPoint = EdwardsPoint::mul_base_clamped(scalar_bytes);
    let public_key = public_point.compress().to_bytes();

    // Private key = clamped scalar || second half of SHA-512 digest
    let mut private_key = [0u8; 64];
    private_key[..32].copy_from_slice(&scalar_bytes);
    private_key[32..].copy_from_slice(&hash[32..]);

    MeshCoreKeypair {
        public_key,
        private_key,
    }
}

/// Re-derive the public key from a stored 64-byte MeshCore private key.
///
/// The first 32 bytes are the clamped Ed25519 scalar; multiplying the base
/// point by it reproduces the public key. Keys found on the GPU are generated
/// by advancing a scalar rather than hashing a seed, so this is the only way
/// to check them, and it is what `verify-pairs` calls.
pub fn public_key_from_private_hex(private_hex: &str) -> Result<String, String> {
    let bytes = hex::decode(private_hex.trim()).map_err(|e| format!("private key is not hex: {}", e))?;
    if bytes.len() != 64 {
        return Err(format!("private key must be 64 bytes, got {}", bytes.len()));
    }

    let mut scalar_bytes = [0u8; 32];
    scalar_bytes.copy_from_slice(&bytes[..32]);

    // Reject anything that was never clamped: an unclamped scalar cannot have
    // come from this program, and silently "fixing" it would turn a corrupt
    // record into a plausible-looking one.
    if scalar_bytes[0] & 7 != 0 || scalar_bytes[31] & 128 != 0 || scalar_bytes[31] & 64 == 0 {
        return Err("private key scalar is not clamped".to_string());
    }

    let point = EdwardsPoint::mul_base_clamped(scalar_bytes);
    Ok(hex::encode_upper(point.compress().to_bytes()))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn deterministic_keygen() {
        let seed = [42u8; 32];
        let kp1 = generate_keypair(&seed);
        let kp2 = generate_keypair(&seed);
        assert_eq!(kp1.public_key, kp2.public_key);
        assert_eq!(kp1.private_key, kp2.private_key);
    }

    #[test]
    fn different_seeds_different_keys() {
        let kp1 = generate_keypair(&[1u8; 32]);
        let kp2 = generate_keypair(&[2u8; 32]);
        assert_ne!(kp1.public_key, kp2.public_key);
    }

    #[test]
    fn clamping_applied() {
        let seed = [0xFFu8; 32];
        let hash = Sha512::digest(&seed);
        let kp = generate_keypair(&seed);

        // Verify clamping was applied to the private key scalar
        assert_eq!(kp.private_key[0] & 7, 0, "low 3 bits should be cleared");
        assert_eq!(kp.private_key[31] & 128, 0, "high bit should be cleared");
        assert_eq!(kp.private_key[31] & 64, 64, "second-highest bit should be set");

        // Second half should match SHA-512 digest
        assert_eq!(&kp.private_key[32..], &hash[32..]);
    }

    #[test]
    fn public_key_is_32_bytes() {
        let kp = generate_keypair(&[7u8; 32]);
        assert_eq!(kp.public_key.len(), 32);
    }

    #[test]
    fn private_key_is_64_bytes() {
        let kp = generate_keypair(&[7u8; 32]);
        assert_eq!(kp.private_key.len(), 64);
    }

    #[test]
    fn private_key_round_trips_to_its_public_key() {
        let kp = generate_keypair(&[9u8; 32]);
        let derived = public_key_from_private_hex(&hex::encode_upper(kp.private_key)).unwrap();
        assert_eq!(derived, hex::encode_upper(kp.public_key));
    }

    #[test]
    fn lower_case_hex_is_accepted() {
        let kp = generate_keypair(&[11u8; 32]);
        let derived = public_key_from_private_hex(&hex::encode(kp.private_key)).unwrap();
        assert_eq!(derived, hex::encode_upper(kp.public_key));
    }

    #[test]
    fn a_wrong_length_key_is_rejected() {
        assert!(public_key_from_private_hex(&"AB".repeat(32)).is_err());
    }

    #[test]
    fn an_unclamped_scalar_is_rejected() {
        let mut bytes = generate_keypair(&[3u8; 32]).private_key;
        bytes[0] |= 1; // break the low-bit clamp
        assert!(public_key_from_private_hex(&hex::encode_upper(bytes)).is_err());
    }

    #[test]
    fn non_hex_is_rejected() {
        assert!(public_key_from_private_hex("not hex at all").is_err());
    }
}
