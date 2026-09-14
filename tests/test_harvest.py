"""Differential native-filter tests and host execution of the CUDA kernel.

The kernel harness checks logic/arithmetic without claiming to emulate GPU
scheduling. Production also runs its mandatory self-test on the actual device.
"""

import ctypes
import hashlib
import json
from pathlib import Path
import queue
import random
import shutil
import struct
import subprocess
import tempfile
import threading
import unittest
from dataclasses import replace
from unittest import mock

from meshcore_vanity.catalog import HEX_WORDS
from meshcore_vanity.config import Config
from meshcore_vanity.engine_gpu import KeygenEngine, default_progress
from meshcore_vanity.harvest import build_policy
from meshcore_vanity.keys import meshcore_private_key_from_seed, public_key_from_seed, public_key_from_meshcore_private
from meshcore_vanity.scoring import analyze_public_key

ROOT = Path(__file__).resolve().parents[1]
HEX = "0123456789ABCDEF"
U32 = ctypes.c_uint32
U8 = ctypes.c_ubyte


def arrays(policy):
    return ((U32 * len(policy["thresholds"]))(*policy["thresholds"]),
            (U32 * len(policy["trie"]))(*policy["trie"]))


class NativeFilterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        compiler = shutil.which("cc")
        if not compiler:
            raise unittest.SkipTest("C compiler required for native screening tests")
        cls.directory = tempfile.TemporaryDirectory()
        library = Path(cls.directory.name) / "filter.so"
        subprocess.run([compiler, "-shared", "-fPIC", "-O2", str(ROOT / "mc-keygen/src/harvest_filter.c"),
                        "-o", str(library)], check=True, capture_output=True)
        cls.library = ctypes.CDLL(str(library))
        cls.check = cls.library.mc_harvest_candidate
        cls.check.argtypes = [ctypes.POINTER(U8), ctypes.POINTER(U32), ctypes.POINTER(U32)]
        cls.check.restype = ctypes.c_int

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def admits(self, key, policy):
        thresholds, trie = arrays(policy)
        return bool(self.check((U8 * 32).from_buffer_copy(bytes.fromhex(key)), thresholds, trie))

    def test_long_prefix_patterns_are_not_capped_at_sixteen(self):
        rng = random.Random(671)
        patterns = ["2" * 24, "DEADBEEF" * 3, "C0FFEEBEEF" + "F" * 12,
                    "123456789ABCDEF", "AB" * 12, "FEEDDEADCAFEFACE"]
        policy = build_policy(28_000_000)
        for pattern in patterns:
            for offset in (0, 3, 64-len(pattern)):
                key = list("".join(rng.choices(HEX, k=64)))
                key[offset:offset+len(pattern)] = pattern
                key = "".join(key)
                if key.startswith(("00", "FF")):
                    continue
                analysis = analyze_public_key(key)
                if analysis["score"] >= 28_000_000:
                    self.assertTrue(self.admits(key, policy), (pattern, offset))

    def test_no_false_negatives_against_python_ranking(self):
        rng = random.Random(184091)
        policies = [(cutoff, build_policy(cutoff)) for cutoff in (1_000_000, 16_000_000, 28_000_000, 40_000_000, 64_000_000)]
        words = list(HEX_WORDS)
        for index in range(4000):
            key = list("".join(rng.choices(HEX, k=64)))
            word = rng.choice(words)
            mode = index % 8
            if mode == 0:
                pattern = rng.choice(HEX) * rng.randint(4, 40)
            elif mode == 1:
                pattern = word * rng.randint(1, 7)
            elif mode == 2:
                pattern = word + rng.choice(words) + rng.choice(words)
                pattern += pattern[-1] * rng.randint(1, 15)
            elif mode == 3:
                unit = "".join(rng.choices(HEX, k=rng.randint(2, 12)))
                pattern = unit * rng.randint(2, 4)
            elif mode == 4:
                half = "".join(rng.choices(HEX, k=rng.randint(3, 20)))
                pattern = half + half[::-1]
            elif mode == 5:
                pattern = rng.choice((HEX, HEX[::-1]))[rng.randint(0, 7):]
            elif mode == 6:
                pattern = word + word[-1] * rng.randint(1, 30)
            else:
                pattern = ""
            pattern = pattern[:64]
            start = 0 if mode in (1, 2, 6) else rng.randint(0, 64-len(pattern))
            key[start:start+len(pattern)] = pattern
            key = "".join(key)
            if key.startswith(("00", "FF")):
                continue
            score = analyze_public_key(key)["score"]
            for cutoff, policy in policies:
                if score >= cutoff:
                    self.assertTrue(self.admits(key, policy), (key, score, cutoff))

    def test_filter_is_selective_at_production_floor(self):
        rng = random.Random(681)
        policy = build_policy(28_000_000)
        hits = sum(self.admits("".join(rng.choices(HEX, k=64)), policy) for _ in range(2000))
        self.assertLess(hits, 5)

    def test_tail_only_patterns_are_rejected_even_at_low_cutoffs(self):
        filler = hashlib.sha256(b"prefix-filter").hexdigest().upper()
        for key in (("A73C91" + filler)[:40] + "2" * 24,
                    ("A73C91" * 11)[:64],
                    "A73C91" + "2" * 52 + "19C37A"):
            self.assertEqual(analyze_public_key(key)["score"], 0)
            for cutoff in (1_000_000, 16_000_000, 28_000_000):
                self.assertFalse(self.admits(key, build_policy(cutoff)), key)

    def test_full_length_and_partial_unit_prefixes_survive_their_own_score(self):
        for prefix in ("2" * 64, "DEADBEEF" * 8, "C0FFEEBEEF" + "F" * 54,
                       "AB" * 31 + "A", "514" * 21, "ABCABC" * 10,
                       "123321" + "789ABCDEF" * 5 + "123321"):
            key = (prefix + "91C7E2" * 11)[:64]
            score = analyze_public_key(key)["score"]
            self.assertGreater(score, 0)
            self.assertTrue(self.admits(key, build_policy(score)), prefix)


class KernelHostTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        compiler = shutil.which("g++")
        if not compiler:
            raise unittest.SkipTest("C++ compiler required for kernel logic tests")
        cls.directory = tempfile.TemporaryDirectory()
        directory = Path(cls.directory.name)
        source = directory / "kernel.cpp"
        source.write_text('''
#define __device__
#define __global__
struct Dim { unsigned int x; } blockIdx, blockDim, threadIdx;
unsigned int atomicAdd(unsigned int *p, unsigned int v) { unsigned int old=*p; *p+=v; return old; }
unsigned long long atomicAdd(unsigned long long *p, unsigned long long v) { auto old=*p; *p+=v; return old; }
unsigned int atomicCAS(unsigned int *p, unsigned int cmp, unsigned int v) { unsigned int old=*p; if(old==cmp)*p=v; return old; }
''' + (ROOT / "mc-keygen/cuda/harvest_filter.h").read_text()
            + (ROOT / "mc-keygen/cuda/vanity_kernel.cu").read_text()
            + (ROOT / "mc-keygen/cuda/harvest_kernel.cu").read_text() + '''
extern "C" void host_harvest(unsigned char *out, const unsigned char *starts,
 unsigned int offset, unsigned int count, const unsigned int *policy,
 const unsigned int *trie, unsigned int capacity) {
    blockDim.x=128;
    for (unsigned int i=0; i<((count+127)/128)*128; ++i) {
        blockIdx.x=i/128; threadIdx.x=i%128;
        vanity_harvest(out, starts, offset, count, policy, trie, capacity, 256);
    }
}
''')
        library = directory / "kernel.so"
        subprocess.run([compiler, "-shared", "-fPIC", "-O2", str(source), "-o", str(library)],
                       check=True, capture_output=True)
        cls.library = ctypes.CDLL(str(library))
        cls.run_kernel = cls.library.host_harvest
        cls.run_kernel.argtypes = [ctypes.POINTER(U8), ctypes.POINTER(U8), U32, U32,
                                  ctypes.POINTER(U32), ctypes.POINTER(U32), U32]

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def launch(self, scalars, policy, offset=0, count=None, capacity=8):
        if count is None:
            count = len(scalars)
        size = 16 + capacity*64
        output = (U8 * (size+64))()
        output[size:] = [165] * 64
        starts = (U8 * (32*len(scalars))).from_buffer_copy(b"".join(scalars))
        thresholds, trie = arrays(policy)
        self.run_kernel(output, starts, offset, count, thresholds, trie, capacity)
        self.assertEqual(list(output[size:]), [165]*64, "kernel wrote beyond result capacity")
        raw = bytes(output[:size])
        found = struct.unpack_from("<I", raw)[0]
        attempts = struct.unpack_from("<Q", raw, 8)[0]
        records = [(raw[16+i*64:48+i*64], raw[48+i*64:80+i*64]) for i in range(min(found, capacity))]
        return found, attempts, records

    def test_full_key_sign_bit_multiple_threads_and_overflow_replay(self):
        seeds = [hashlib.sha256(f"kernel-test-{i}".encode()).digest() for i in range(8)]
        seeds = [seed for seed in seeds if public_key_from_seed(seed)[0] not in (0, 255)]
        scalars = [meshcore_private_key_from_seed(seed)[:32] for seed in seeds]
        expected = [public_key_from_seed(seed) for seed in seeds]
        self.assertTrue(any(key[-1] & 128 for key in expected))
        policy = build_policy(28_000_000)
        policy["thresholds"][1] = 1  # first eligible candidate from each chain
        policy["thresholds"][2] = 1
        found, attempts, records = self.launch(scalars, policy)
        self.assertEqual([key for key, _ in records], expected)
        self.assertEqual(found, len(seeds))
        self.assertEqual(attempts, len(seeds))
        self.assertEqual([scalar for _, scalar in records], scalars)
        found, _, _ = self.launch(scalars, policy, capacity=1)
        self.assertEqual(found, len(seeds))
        replayed = []
        for offset in range(len(scalars)):
            _, checked, result = self.launch(scalars, policy, offset=offset, count=1, capacity=1)
            self.assertEqual(checked, 1)
            replayed.extend(result)
        self.assertEqual(replayed, records)

    def test_later_chain_matches_have_exact_counts_and_valid_scalars(self):
        scalars = [meshcore_private_key_from_seed(hashlib.sha256(f"chain-{i}".encode()).digest())[:32] for i in range(4)]
        policy = build_policy(28_000_000)
        policy["thresholds"][1] = 2
        found, attempts, records = self.launch(scalars, policy)
        self.assertEqual(found, len(scalars))
        expected_attempts = 0
        for start, (public, scalar) in zip(scalars, records):
            delta = int.from_bytes(scalar, "little") - int.from_bytes(start, "little")
            self.assertEqual(delta % 8, 0)
            self.assertTrue(0 <= delta < 256*8)
            expected_attempts += delta//8 + 1
            self.assertEqual(public_key_from_meshcore_private(scalar + bytes(32)), public)
        self.assertEqual(attempts, expected_attempts)
        self.assertGreater(attempts, len(scalars))


class HarvestProtocolTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        config = Config.create(data_directory=Path(self.directory.name))
        self.engine = object.__new__(KeygenEngine)
        self.engine.config = config
        self.engine.gpu = config.gpu
        self.engine.lock = threading.Lock()
        self.engine.progress = default_progress(config.gpu)
        self.engine.cutoff_score = 0

    def tearDown(self):
        self.directory.cleanup()

    def test_stats_update_even_when_there_are_no_matches(self):
        previous = self.engine._harvest_event({"type": "stats", "attempts": 10000,
            "elapsed_secs": 2.0, "replayed_attempts": 100}, (0, 0.0, 0), 28_000_000)
        self.assertEqual(self.engine.progress["keys_per_second"], 5000.0)
        self.engine._harvest_event({"type": "done", "attempts": 20000,
            "elapsed_secs": 4.0, "replayed_attempts": 200}, previous, 28_000_000)
        self.assertEqual(self.engine.progress["harvest_attempts_total"], 20000)
        self.assertEqual(self.engine.progress["harvest_replayed_attempts_total"], 200)

    def test_bad_stats_and_event_shapes_are_rejected(self):
        for value in ([], {"type": "bogus"}, {"type": "stats", "attempts": -1,
                      "elapsed_secs": 2, "replayed_attempts": 0},
                      {"type": "stats", "attempts": 1, "elapsed_secs": float("nan"), "replayed_attempts": 0}):
            with self.assertRaises(ValueError):
                self.engine._harvest_event(value, (0, 0.0, 0), 28_000_000)

    def test_qualifying_material_is_verified_before_recording(self):
        key = ("2"*20 + hashlib.sha256(b"protocol").hexdigest().upper())[:64]
        with mock.patch("meshcore_vanity.engine_gpu.normalize_external_private_key", side_effect=ValueError("bad pair")), \
             mock.patch.object(self.engine, "_record_match") as record:
            with self.assertRaises(ValueError):
                self.engine._harvest_event({"type": "match", "public_key": key, "private_key": "0"*128},
                                           (0, 0.0, 0), 28_000_000)
            record.assert_not_called()


class NativeHarvestIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.binary = ROOT / "mc-keygen/target/release/mc-keygen"
        if not self.binary.is_file():
            self.skipTest("build mc-keygen to run native integration tests")
        result = subprocess.run([str(self.binary), "--help"], capture_output=True, text=True, check=True)
        if "policy-v2" not in result.stdout:
            self.skipTest("rebuild mc-keygen to enable harvesting")
        self.directory = tempfile.TemporaryDirectory()

    def tearDown(self):
        if hasattr(self, "directory"):
            self.directory.cleanup()

    def test_native_cpu_stream_matches_verify_and_benchmark_contains_no_secrets(self):
        policy = Path(self.directory.name) / "policy.json"
        # Low test-only cutoff produces a useful sample in a short native run.
        policy.write_text(json.dumps(build_policy(8_000_000)))
        base = [str(self.binary), "harvest", "--policy", str(policy), "--seconds", "0.15"]
        result = subprocess.run(base, capture_output=True, text=True, timeout=20, check=True)
        events = [json.loads(line) for line in result.stdout.splitlines()]
        matches = [event for event in events if event["type"] == "match"]
        self.assertTrue(matches)
        self.assertEqual(events[-1]["type"], "done")
        self.assertGreater(events[-1]["attempts"], len(matches))
        scalars = [int.from_bytes(bytes.fromhex(event["private_key"])[:32], "little") for event in matches]
        self.assertTrue(all(abs(left-right) > 8*255 for left, right in zip(scalars, scalars[1:])),
                        "native harvesting retained related short-chain scalars")
        for event in matches[:3]:
            self.assertEqual(public_key_from_meshcore_private(bytes.fromhex(event["private_key"])).hex().upper(), event["public_key"])
        result = subprocess.run(base + ["--benchmark"], capture_output=True, text=True, timeout=20, check=True)
        self.assertNotIn("private_key", result.stdout)
        self.assertTrue(all(json.loads(line)["type"] in ("stats", "done") for line in result.stdout.splitlines()))

    def test_engine_consumes_native_statistics_without_waiting_for_a_match(self):
        base = Config.create(data_directory=Path(self.directory.name))
        config = replace(base, gpu=replace(base.gpu, campaign_time_slice_seconds=0.2))
        with mock.patch("meshcore_vanity.engine_gpu.gpu_name", return_value=None):
            engine = KeygenEngine(self.binary, config, queue.Queue())
        self.assertTrue(engine.harvesting)
        self.assertEqual(engine._run_harvest(), "complete")
        self.assertGreater(engine.progress["harvest_attempts_total"], 0)
        self.assertEqual(engine.progress["failures_total"], 0)
        self.assertIsNone(engine.current_process)

    def test_stdin_stop_flushes_a_done_event(self):
        policy = Path(self.directory.name) / "policy.json"
        policy.write_text(json.dumps(build_policy(28_000_000)))
        result = subprocess.run([str(self.binary), "harvest", "--policy", str(policy),
                                 "--seconds", "30", "--stdin-stop"], input="stop\n",
                                capture_output=True, text=True, timeout=5, check=True)
        events = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(events[-1]["type"], "done")
