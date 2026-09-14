//! Continuous broad-pattern harvesting and its versioned screening policy.
//! CPU and GPU use the identical C-compatible integer filter.

use std::io::{self, Write};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{mpsc, Arc};
use std::time::{Duration, Instant};

use curve25519_dalek::{constants::ED25519_BASEPOINT_TABLE, scalar::Scalar, EdwardsPoint};
use rand::{rngs::OsRng, RngCore};
use serde::{Deserialize, Serialize};

use crate::search::{advance_scalar, clamp_scalar};
use crate::types::MeshCoreKeypair;

#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct HarvestPolicy {
    pub version: u32,
    pub cutoff_score: u64,
    pub(crate) thresholds: Vec<u32>,
    pub(crate) trie: Vec<u32>,
}

unsafe extern "C" {
    fn mc_harvest_candidate(key: *const u8, policy: *const u32, trie: *const u32) -> i32;
}

impl HarvestPolicy {
    pub fn validate(&self) -> Result<(), String> {
        if self.version != 2 || self.thresholds.len() != 424 || self.thresholds[0] != 2 {
            return Err("unsupported harvest policy layout".into());
        }
        if self.thresholds.iter().any(|&value| value > 65) {
            return Err("harvest thresholds must be in 0..65".into());
        }
        if self.trie.is_empty() || self.trie.len() % 17 != 0 || self.trie.len() > 17 * 65536 {
            return Err("invalid harvest trie size".into());
        }
        let nodes = self.trie.len() / 17;
        for (index, node) in self.trie.chunks_exact(17).enumerate() {
            // Forward edges guarantee a finite rooted trie and exclude cycles.
            if node[..16].iter().any(|&child| child != 0 && (child as usize <= index || child as usize >= nodes))
                || node[16] > 7 {
                return Err("invalid harvest trie edge or terminal flags".into());
            }
        }
        Ok(())
    }

    pub(crate) fn matches(&self, key: &[u8; 32]) -> bool {
        // Policies are validated at the process boundary before any worker or
        // kernel can see them. Arrays have fixed sizes; trie indices are bounded.
        unsafe { mc_harvest_candidate(key.as_ptr(), self.thresholds.as_ptr(), self.trie.as_ptr()) != 0 }
    }
}

/// Draw independent starts, leaving room for a short +8B chain before the
/// clamp boundary. Never use a returned private scalar to seed another chain.
pub fn random_chain_start() -> [u8; 32] {
    loop {
        let mut scalar = [0u8; 32];
        OsRng.fill_bytes(&mut scalar);
        clamp_scalar(&mut scalar);
        let mut end = scalar;
        advance_scalar(&mut end, 8 * 255);
        if end[31] & 0xc0 == 0x40 {
            return scalar;
        }
    }
}

#[cfg(feature = "cuda")]
pub(crate) fn fill_chain_starts(starts: &mut [u8]) {
    assert_eq!(starts.len() % 32, 0);
    OsRng.fill_bytes(starts);
    for chunk in starts.chunks_exact_mut(32) {
        let scalar: &mut [u8; 32] = chunk.try_into().unwrap();
        clamp_scalar(scalar);
        let mut end = *scalar;
        advance_scalar(&mut end, 8 * 255);
        if end[31] & 0xc0 != 0x40 { *scalar = random_chain_start(); }
    }
}

pub fn verified_pair(public_key: [u8; 32], scalar: [u8; 32]) -> Result<MeshCoreKeypair, String> {
    let mut clamped = scalar;
    clamp_scalar(&mut clamped);
    if clamped != scalar || EdwardsPoint::mul_base_clamped(scalar).compress().to_bytes() != public_key {
        // Never put private scalars in an error message or a public log.
        return Err("harvested key failed independent scalar/public-key verification".into());
    }
    let mut private_key = [0u8; 64];
    private_key[..32].copy_from_slice(&scalar);
    OsRng.fill_bytes(&mut private_key[32..]);
    Ok(MeshCoreKeypair { public_key, private_key })
}

#[derive(Serialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum HarvestEvent {
    Match { public_key: String, private_key: String },
    Stats { attempts: u64, elapsed_secs: f64, replayed_attempts: u64 },
    Done { attempts: u64, elapsed_secs: f64, replayed_attempts: u64 },
}

pub fn emit(event: &HarvestEvent) -> io::Result<()> {
    let stdout = io::stdout();
    let mut out = stdout.lock();
    serde_json::to_writer(&mut out, event)?;
    out.write_all(b"\n")?;
    out.flush()
}

pub fn emit_pair(pair: MeshCoreKeypair) -> io::Result<()> {
    emit(&HarvestEvent::Match {
        public_key: hex::encode_upper(pair.public_key),
        private_key: hex::encode_upper(pair.private_key),
    })
}

pub fn run_cpu(policy: HarvestPolicy, threads: usize, seconds: f64, stop: Arc<AtomicBool>, benchmark: bool) -> Result<(), String> {
    policy.validate()?;
    let policy = Arc::new(policy);
    let attempts = Arc::new(AtomicU64::new(0));
    let (sender, receiver) = mpsc::sync_channel(128);
    let mut workers = Vec::new();
    for _ in 0..threads {
        let policy = Arc::clone(&policy);
        let stop = Arc::clone(&stop);
        let attempts = Arc::clone(&attempts);
        let sender = sender.clone();
        workers.push(std::thread::spawn(move || {
            let eight_b = ED25519_BASEPOINT_TABLE * &Scalar::from(8u64);
            while !stop.load(Ordering::Relaxed) {
                let mut scalar = random_chain_start();
                let mut point = EdwardsPoint::mul_base_clamped(scalar);
                let mut checked = 0;
                let mut found = None;
                'chain: for _ in 0..16 {
                    if stop.load(Ordering::Relaxed) { break; }
                    let batch: [EdwardsPoint; 16] = core::array::from_fn(|_| {
                        let current = point;
                        point += eight_b;
                        current
                    });
                    for compressed in EdwardsPoint::compress_batch::<16>(&batch) {
                        let public_key = compressed.to_bytes();
                        checked += 1;
                        if policy.matches(&public_key) {
                            found = Some(verified_pair(public_key, scalar));
                            break 'chain; // Only one retained key per independent chain.
                        }
                        advance_scalar(&mut scalar, 8);
                    }
                }
                attempts.fetch_add(checked, Ordering::Relaxed);
                if let Some(mut value) = found {
                    loop {
                        match sender.try_send(value) {
                            Ok(()) => break,
                            Err(mpsc::TrySendError::Disconnected(_)) => return,
                            Err(mpsc::TrySendError::Full(v)) => {
                                value = v;
                                // Receiver drains while stopping, so complete
                                // this already-found record before exiting.
                                std::thread::sleep(Duration::from_millis(1));
                            }
                        }
                    }
                }
            }
        }));
    }
    drop(sender);
    let start = Instant::now();
    let mut last_stats = start;
    let mut error = None;
    loop {
        if start.elapsed().as_secs_f64() >= seconds { stop.store(true, Ordering::Relaxed); }
        match receiver.recv_timeout(Duration::from_millis(50)) {
            Ok(Ok(pair)) => {
                if !benchmark {
                    if let Err(e) = emit_pair(pair) { error = Some(e.to_string()); break; }
                }
            }
            Ok(Err(e)) => { error = Some(e); break; }
            Err(mpsc::RecvTimeoutError::Disconnected) => break,
            Err(mpsc::RecvTimeoutError::Timeout) => (),
        }
        if last_stats.elapsed() >= Duration::from_secs(1) {
            if let Err(e) = emit(&HarvestEvent::Stats {
                attempts: attempts.load(Ordering::Relaxed), elapsed_secs: start.elapsed().as_secs_f64(), replayed_attempts: 0,
            }) { error = Some(e.to_string()); break; }
            last_stats = Instant::now();
        }
    }
    stop.store(true, Ordering::Relaxed);
    drop(receiver);
    for worker in workers {
        if worker.join().is_err() { error = Some("CPU harvest worker panicked".into()); }
    }
    if let Some(error) = error { return Err(error); }
    emit(&HarvestEvent::Done { attempts: attempts.load(Ordering::Relaxed),
        elapsed_secs: start.elapsed().as_secs_f64(), replayed_attempts: 0 }).map_err(|e| e.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn independent_starts_and_pairs() {
        let first = random_chain_start();
        let second = random_chain_start();
        assert_ne!(first, second);
        let public = EdwardsPoint::mul_base_clamped(first).compress().to_bytes();
        assert!(verified_pair(public, first).is_ok());
        assert!(verified_pair(public, second).is_err());
    }

    #[test]
    fn malformed_policy_is_rejected() {
        let mut policy = HarvestPolicy { version: 2, cutoff_score: 0, thresholds: vec![65; 424], trie: vec![0; 17] };
        policy.thresholds[0] = 2;
        assert!(policy.validate().is_ok());
        policy.version = 1;
        assert!(policy.validate().is_err());
        policy.version = 2;
        policy.trie[16] = 4; // visible preference, independent of score cutoff
        assert!(policy.validate().is_ok());
        policy.trie[16] = 8;
        assert!(policy.validate().is_err());
        policy.trie[16] = 0;
        policy.trie[0] = 1;
        assert!(policy.validate().is_err());
        policy.trie[0] = 0;
        policy.thresholds.pop();
        assert!(policy.validate().is_err());
    }
}
