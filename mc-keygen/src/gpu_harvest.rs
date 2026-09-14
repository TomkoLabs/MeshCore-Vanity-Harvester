//! CUDA harvesting with persistent allocations, independent chains, lossless
//! overflow replay, exact attempt counts and a mandatory device/host self-test.

use std::sync::{atomic::{AtomicBool, Ordering}, Arc};
use std::time::{Duration, Instant};

use cudarc::driver::{CudaFunction, CudaSlice, CudaStream, LaunchConfig, PushKernelArg};
use curve25519_dalek::EdwardsPoint;

use crate::gpu::compile_kernel;
use crate::harvest::{emit, emit_pair, fill_chain_starts, verified_pair, HarvestEvent, HarvestPolicy};
use crate::search::advance_scalar;
use crate::types::MeshCoreKeypair;

const BLOCK: u32 = 128;
const ITERS: u64 = 256;
const CAPACITY: u32 = 4096;

struct HarvestBatch {
    keys: Vec<MeshCoreKeypair>,
    attempts: u64,
    replayed: u64,
}

pub struct CudaHarvester {
    stream: Arc<CudaStream>,
    function: CudaFunction,
    starts: CudaSlice<u8>,
    host_starts: Vec<u8>,
    policy_device: CudaSlice<u32>,
    trie: CudaSlice<u32>,
    output: CudaSlice<u8>,
    thread_count: u32,
    policy: HarvestPolicy,
}

impl CudaHarvester {
    pub fn new(policy: HarvestPolicy) -> Result<Self, String> {
        policy.validate()?;
        let (module, stream) = compile_kernel().map_err(|e| e.to_string())?;
        let function = module.load_function("vanity_harvest").map_err(|e| e.to_string())?;
        let sm_count = stream.context().attribute(
            cudarc::driver::sys::CUdevice_attribute::CU_DEVICE_ATTRIBUTE_MULTIPROCESSOR_COUNT
        ).map_err(|e| e.to_string())?;
        // Bounded independent chains. The original kernel uses 512 threads/SM;
        // retain that concurrency while allowing smaller, less register-heavy blocks.
        let thread_count = (sm_count as u32).saturating_mul(512).clamp(512, 131072);
        let starts = stream.alloc_zeros::<u8>(thread_count as usize * 32).map_err(|e| e.to_string())?;
        let policy_device = stream.clone_htod(&policy.thresholds).map_err(|e| e.to_string())?;
        let trie = stream.clone_htod(&policy.trie).map_err(|e| e.to_string())?;
        let output = stream.alloc_zeros::<u8>(16 + CAPACITY as usize * 64).map_err(|e| e.to_string())?;
        let host_starts = vec![0; thread_count as usize * 32];
        let mut harvester = Self { stream, function, starts, host_starts, policy_device, trie, output, thread_count, policy };
        harvester.verify()?;
        Ok(harvester)
    }

    fn collect(&mut self, offset: u32, threads: u32, capacity: u32) -> Result<HarvestBatch, String> {
        self.stream.memset_zeros(&mut self.output).map_err(|e| e.to_string())?;
        let config = LaunchConfig {
            grid_dim: (threads.div_ceil(BLOCK), 1, 1), block_dim: (BLOCK, 1, 1), shared_mem_bytes: 0,
        };
        unsafe {
            self.stream.launch_builder(&self.function)
                .arg(&mut self.output).arg(&self.starts).arg(&offset).arg(&threads)
                .arg(&self.policy_device).arg(&self.trie).arg(&capacity).arg(&ITERS).launch(config)
        }.map_err(|e| e.to_string())?;
        self.stream.synchronize().map_err(|e| e.to_string())?;
        let header = self.stream.clone_dtoh(&self.output.slice(..16)).map_err(|e| e.to_string())?;
        let count = u32::from_le_bytes(header[..4].try_into().unwrap());
        let attempts = u64::from_le_bytes(header[8..16].try_into().unwrap());
        if count > threads || attempts > threads as u64 * ITERS || attempts < threads as u64 {
            return Err("invalid CUDA harvest counters".into());
        }
        if count > capacity {
            // Discard this incomplete output and replay the SAME independent
            // starts in disjoint smaller groups. Never reseed before replay.
            if threads <= 1 { return Err("CUDA harvest overflow with one thread".into()); }
            let left_count = threads / 2;
            let mut left = self.collect(offset, left_count, capacity)?;
            let right = self.collect(offset + left_count, threads - left_count, capacity)?;
            left.keys.extend(right.keys);
            left.attempts += right.attempts;
            left.replayed += right.replayed + attempts;
            if left.attempts != attempts || left.keys.len() != count as usize {
                return Err("CUDA harvest overflow replay was not deterministic".into());
            }
            return Ok(left);
        }
        if count == 0 { return Ok(HarvestBatch { keys: Vec::new(), attempts, replayed: 0 }); }
        let records = self.stream.clone_dtoh(&self.output.slice(16..16 + count as usize * 64))
            .map_err(|e| e.to_string())?;
        let mut keys = Vec::with_capacity(count as usize);
        for record in records.chunks_exact(64) {
            let public: [u8; 32] = record[..32].try_into().unwrap();
            let scalar: [u8; 32] = record[32..].try_into().unwrap();
            keys.push(verified_pair(public, scalar)?);
        }
        Ok(HarvestBatch { keys, attempts, replayed: 0 })
    }

    fn verify(&mut self) -> Result<(), String> {
        // Force many matches and a one-record buffer to exercise multi-thread
        // indexing, sign bits, chain advancement, counters and overflow replay.
        let mut test_policy = self.policy.clone();
        test_policy.thresholds[1] = 2;
        self.stream.memcpy_htod(&test_policy.thresholds, &mut self.policy_device).map_err(|e| e.to_string())?;
        let mut starts = Vec::new();
        let mut expected = Vec::new();
        let mut expected_attempts = 0;
        for index in 0..8 {
            let mut scalar = [0u8; 32];
            scalar[0] = 8 * index;
            scalar[31] = 64;
            starts.extend_from_slice(&scalar);
            for _ in 0..ITERS {
                let public = EdwardsPoint::mul_base_clamped(scalar).compress().to_bytes();
                expected_attempts += 1;
                if test_policy.matches(&public) { expected.push(public); break; }
                advance_scalar(&mut scalar, 8);
            }
        }
        self.stream.memcpy_htod(&starts, &mut self.starts.slice_mut(..starts.len())).map_err(|e| e.to_string())?;
        let batch = self.collect(0, 8, 1)?;
        let mut actual: Vec<_> = batch.keys.iter().map(|key| key.public_key).collect();
        expected.sort(); actual.sort();
        if expected != actual || expected_attempts != batch.attempts || batch.replayed == 0 {
            return Err("CUDA harvesting self-test failed (results, counters or overflow replay)".into());
        }
        self.stream.memcpy_htod(&self.policy.thresholds, &mut self.policy_device).map_err(|e| e.to_string())?;
        Ok(())
    }

    fn batch(&mut self) -> Result<HarvestBatch, String> {
        fill_chain_starts(&mut self.host_starts);
        self.stream.memcpy_htod(&self.host_starts, &mut self.starts).map_err(|e| e.to_string())?;
        let batch = self.collect(0, self.thread_count, CAPACITY)?;
        for key in &batch.keys {
            if !self.policy.matches(&key.public_key) { return Err("CUDA harvest filter disagrees with host".into()); }
        }
        Ok(batch)
    }
}

pub fn run_gpu(policy: HarvestPolicy, seconds: f64, stop: Arc<AtomicBool>, benchmark: bool) -> Result<(), String> {
    let mut harvester = CudaHarvester::new(policy)?;
    let start = Instant::now();
    let mut last_stats = start;
    let mut attempts = 0;
    let mut replayed_attempts = 0;
    while !stop.load(Ordering::Relaxed) && start.elapsed().as_secs_f64() < seconds {
        let batch = harvester.batch()?;
        attempts += batch.attempts;
        replayed_attempts += batch.replayed;
        if !benchmark {
            for pair in batch.keys { emit_pair(pair).map_err(|e| e.to_string())?; }
        }
        if last_stats.elapsed() >= Duration::from_secs(1) {
            emit(&HarvestEvent::Stats { attempts, elapsed_secs: start.elapsed().as_secs_f64(), replayed_attempts })
                .map_err(|e| e.to_string())?;
            last_stats = Instant::now();
        }
    }
    emit(&HarvestEvent::Done { attempts, elapsed_secs: start.elapsed().as_secs_f64(), replayed_attempts })
        .map_err(|e| e.to_string())
}
