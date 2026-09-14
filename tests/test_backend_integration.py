"""The mc-keygen backend, end to end, without needing a GPU.

`tests/fixtures/fake_mc_keygen.py` speaks the real command line and prints the
real JSON, so these tests exercise capability detection, subprocess handling,
result parsing and independent re-verification the way they actually run.
"""

from __future__ import annotations

import os
import subprocess
import queue
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from meshcore_vanity.config import Config, GpuConfig
from meshcore_vanity.engine_gpu import (
    KeygenEngine,
    build_targets,
    describe_backend,
    detect_capabilities,
)
from meshcore_vanity.keys import public_key_from_seed
from meshcore_vanity.leaderboards import make_record_from_material
from meshcore_vanity.report import Style, match_line

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FIXTURE = PROJECT_ROOT / "tests" / "fixtures" / "fake_mc_keygen.py"
REAL_BINARY = PROJECT_ROOT / "mc-keygen" / "target" / "release" / "mc-keygen"
PLAIN = Style(enabled=False)


def real_binary_state() -> str:
    """"missing", "stale" or "current".

    A binary built before a source change cannot be judged for features it was
    never compiled with. Telling those two situations apart is the difference
    between "rebuild" and "you broke something".
    """
    if not REAL_BINARY.is_file():
        return "missing"
    sources = list((PROJECT_ROOT / "mc-keygen" / "src").glob("*.rs"))
    sources.append(PROJECT_ROOT / "mc-keygen" / "Cargo.toml")
    newest = max((p.stat().st_mtime for p in sources if p.is_file()), default=0.0)
    return "stale" if REAL_BINARY.stat().st_mtime < newest else "current"

# Short prefixes so the stub finds one on a CPU in a fraction of a second.
QUICK_GPU = GpuConfig(min_target_length=3, max_target_length=4,
                      max_prefixes_per_campaign=64,
                      campaign_time_slice_seconds=60.0,
                      run_startup_self_test=True)


class StubEnvironment:
    """Runs the stub under a chosen mode, with cryptography importable."""

    def __init__(self, mode: str = "gpu"):
        self.mode = mode
        self.previous = {}

    def __enter__(self):
        self.previous = {
            "FAKE_MC_KEYGEN_MODE": os.environ.get("FAKE_MC_KEYGEN_MODE"),
            "FAKE_MC_KEYGEN_IMPORT_PATH": os.environ.get("FAKE_MC_KEYGEN_IMPORT_PATH"),
        }
        os.environ["FAKE_MC_KEYGEN_MODE"] = self.mode
        os.environ["FAKE_MC_KEYGEN_IMPORT_PATH"] = os.pathsep.join(sys.path)
        return self

    def __exit__(self, *exc):
        for name, value in self.previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def stub_binary() -> Path:
    """The stub, guaranteed runnable.

    Git preserves the executable bit, but files that arrive by other means may
    not. The whole suite would then fail with every capability reported False,
    which reads like a broken backend rather than a lost permission.
    """
    if not os.access(FIXTURE, os.X_OK):
        FIXTURE.chmod(FIXTURE.stat().st_mode | 0o755)
    return FIXTURE


class CapabilityDetectionTests(unittest.TestCase):
    def test_only_current_policy_harvesting_is_enabled(self):
        from unittest import mock
        for help_text, expected in (("harvest", False), ("harvest policy-v2", True)):
            result = subprocess.CompletedProcess([], 0, stdout=help_text, stderr="")
            with mock.patch("meshcore_vanity.engine_gpu.subprocess.run", return_value=result):
                self.assertEqual(detect_capabilities(stub_binary())["harvest"], expected)

    def test_a_gpu_build_is_recognised(self):
        with StubEnvironment("gpu"):
            capabilities = detect_capabilities(stub_binary())
        self.assertTrue(capabilities["gpu"])
        self.assertTrue(capabilities["verify"])

    def test_a_cpu_build_is_recognised_and_not_sent_gpu_flags(self):
        """A CPU-only build rejects --gpu-only outright; adapt rather than fail."""
        with StubEnvironment("cpu"):
            capabilities = detect_capabilities(stub_binary())
        self.assertFalse(capabilities["gpu"])
        status = describe_backend(GpuConfig(), stub_binary(), capabilities)
        self.assertTrue(status.active)
        self.assertEqual(status.mode, "cpu")


class BackendRunTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        base = Config.create(data_directory=Path(self.directory.name))
        self.config = replace(base, gpu=QUICK_GPU)
        self.queue: "queue.Queue" = queue.Queue()

    def tearDown(self):
        self.directory.cleanup()

    def _drain(self, engine, timeout=90.0):
        import time as _time

        deadline = _time.monotonic() + timeout
        messages = []
        while _time.monotonic() < deadline:
            try:
                messages.append(self.queue.get(timeout=1.0))
            except queue.Empty:
                continue
            if any(m.get("type") == "keygen_result" for m in messages):
                return messages
            if any(m.get("type") == "keygen_disabled" for m in messages):
                return messages
        return messages

    def test_a_match_is_produced_verified_and_announced(self):
        with StubEnvironment("gpu"):
            engine = KeygenEngine(stub_binary(), self.config, self.queue)
            # Mode depends on whether a GPU is actually present, which this
            # machine may not have; GpuPresenceTests covers that distinction.
            self.assertTrue(engine.capabilities["gpu"])
            engine.start()
            try:
                messages = self._drain(engine)
            finally:
                engine.stop()

        results = [m for m in messages if m.get("type") == "keygen_result"]
        self.assertTrue(results, f"no match produced; saw {[m.get('type') for m in messages]}")
        result = results[0]

        # The key must survive independent re-derivation, not just be believed.
        seed = bytes.fromhex(result["seed_hex"])
        self.assertEqual(public_key_from_seed(seed).hex().upper(), result["public_key_hex"])
        self.assertTrue(result["public_key_hex"].startswith(result["matched_prefix"]))

        record = make_record_from_material(
            result["private_key_hex"], result["seed_hex"], result["public_key_hex"],
            result["analysis"], 0.0, 0, "mc_keygen_gpu", result["matched_prefix"],
        )
        line = match_line(PLAIN, record, result.get("gpu_measured_keys_per_second"), rank=1)
        self.assertIn("FOUND GPU", line)
        self.assertIn(result["public_key_hex"][:12], line)

    def test_progress_is_persisted_so_a_restart_resumes(self):
        with StubEnvironment("gpu"):
            engine = KeygenEngine(stub_binary(), self.config, self.queue)
            engine.start()
            try:
                self._drain(engine)
            finally:
                engine.stop()
        self.assertTrue(self.config.paths.gpu_progress.is_file())

        with StubEnvironment("gpu"):
            resumed = KeygenEngine(stub_binary(), self.config, self.queue)
        self.assertGreater(len(resumed.progress["found_prefixes"]), 0)
        self.assertGreater(resumed.progress["matches_total"], 0)
        self.assertTrue(resumed.progress["target_runs"])
        self.assertEqual(resumed.progress["target_runs"], engine.progress["target_runs"])

    def test_repeated_failures_disable_the_backend_rather_than_spinning(self):
        config = replace(self.config, gpu=replace(QUICK_GPU, max_consecutive_failures=2,
                                                  retry_delay_seconds=0.05))
        with StubEnvironment("fail"):
            engine = KeygenEngine(stub_binary(), config, self.queue)
            engine.start()
            try:
                messages = self._drain(engine, timeout=30.0)
            finally:
                engine.stop()
        kinds = [m.get("type") for m in messages]
        self.assertIn("keygen_disabled", kinds)

    def test_unparseable_output_is_refused_not_trusted(self):
        config = replace(self.config, gpu=replace(QUICK_GPU, max_consecutive_failures=2,
                                                  retry_delay_seconds=0.05))
        with StubEnvironment("garbage"):
            engine = KeygenEngine(stub_binary(), config, self.queue)
            engine.start()
            try:
                messages = self._drain(engine, timeout=30.0)
            finally:
                engine.stop()
        self.assertFalse([m for m in messages if m.get("type") == "keygen_result"])
        self.assertTrue([m for m in messages if m.get("type") in ("keygen_error", "keygen_disabled")])

    def test_stopping_is_prompt_and_leaves_no_child_behind(self):
        with StubEnvironment("gpu"):
            engine = KeygenEngine(stub_binary(), self.config, self.queue)
            engine.start()
            import time as _time
            _time.sleep(1.0)
            started = _time.monotonic()
            engine.stop()
            elapsed = _time.monotonic() - started
        self.assertLess(elapsed, 15.0)
        self.assertFalse(engine.thread.is_alive())
        self.assertIsNone(engine.current_process)


class TargetsTests(unittest.TestCase):
    def test_short_target_configuration_produces_reachable_work(self):
        targets = build_targets(QUICK_GPU)
        self.assertTrue(targets)
        for prefix, _priority, _description in targets:
            self.assertLessEqual(len(prefix), QUICK_GPU.max_target_length)


if __name__ == "__main__":
    unittest.main()


class PairVerificationTests(unittest.TestCase):
    """Batch verification must be an optimisation, never a way in.

    Restoring a full set of boards means re-deriving every saved key. Keys found
    on the GPU have no seed, so Python must do the scalar multiplication itself
    at ~95 ms each. Handing the batch to mc-keygen turns a 52 second startup
    into 0.6 — but only pairs it actually proves may skip the slow path.
    """

    def setUp(self):
        state = real_binary_state()
        if state == "missing":
            self.skipTest("mc-keygen has not been built yet: cargo build --release "
                          "--manifest-path mc-keygen/Cargo.toml")
        if state == "stale":
            self.skipTest("mc-keygen is older than its sources; rebuild it: cargo build "
                          "--release --manifest-path mc-keygen/Cargo.toml")
        self.binary = REAL_BINARY
        # Restore tests need an eligible ID, as patternless keys are retired.
        import hashlib
        from meshcore_vanity.scoring import analyze_public_key
        for index in range(100_000):
            self.seed = hashlib.sha256(f"pair-verification-{index}".encode()).digest()
            self.public = public_key_from_seed(self.seed).hex().upper()
            if not self.public.startswith(("00", "FF")) and analyze_public_key(self.public)["score"] > 0:
                break
        else:
            self.fail("could not create an eligible verification fixture")
        from meshcore_vanity.keys import meshcore_private_key_from_seed
        self.private = meshcore_private_key_from_seed(self.seed).hex().upper()

    def test_the_backend_advertises_the_capability(self):
        """A current build that cannot verify pairs is a regression, not a skip."""
        from meshcore_vanity.engine_gpu import detect_capabilities, supports_pair_verification
        self.assertTrue(detect_capabilities(self.binary)["verify_pairs"],
                        "this build of mc-keygen has no verify-pairs command")
        self.assertTrue(supports_pair_verification(self.binary))

    def test_a_genuine_pair_verifies(self):
        from meshcore_vanity.engine_gpu import verify_pairs
        self.assertEqual(verify_pairs(self.binary, [(self.public, self.private)]),
                         {(self.public, self.private)})

    def test_a_tampered_private_key_does_not_verify(self):
        from meshcore_vanity.engine_gpu import verify_pairs
        wrong = "AB" * 64
        self.assertEqual(verify_pairs(self.binary, [(self.public, wrong)]), set())

    def test_a_mismatched_pair_does_not_verify(self):
        from meshcore_vanity.engine_gpu import verify_pairs
        from meshcore_vanity.keys import meshcore_private_key_from_seed
        other = meshcore_private_key_from_seed(bytes([7] * 32)).hex().upper()
        self.assertEqual(verify_pairs(self.binary, [(self.public, other)]), set())

    def test_good_and_bad_pairs_are_separated_in_one_batch(self):
        from meshcore_vanity.engine_gpu import verify_pairs
        batch = [(self.public, self.private), (self.public, "CD" * 64)]
        self.assertEqual(verify_pairs(self.binary, batch), {(self.public, self.private)})

    def test_an_empty_batch_costs_nothing(self):
        from meshcore_vanity.engine_gpu import verify_pairs
        self.assertEqual(verify_pairs(self.binary, []), set())

    def test_a_broken_backend_proves_nothing_rather_than_everything(self):
        from meshcore_vanity.engine_gpu import verify_pairs
        self.assertEqual(verify_pairs(Path("/nonexistent/mc-keygen"),
                                      [(self.public, self.private)]), set())

    def test_validate_record_still_rejects_a_pair_that_was_not_proven(self):
        """A proof for one pair must not admit a record with a different key."""
        from meshcore_vanity.leaderboards import validate_record
        record = {
            "public_key": self.public,
            "private_key": "AB" * 64,
            "seed": None,
            "score_version": 0,
        }
        # The set names a *different* private key for this public key.
        self.assertIsNone(validate_record(record, {(self.public, self.private)}))

    def test_validate_record_accepts_a_proven_pair_without_the_slow_path(self):
        from unittest import mock
        from meshcore_vanity.leaderboards import validate_record
        record = {
            "public_key": self.public,
            "private_key": self.private,
            "seed": None,
            "score_version": 0,
        }
        with mock.patch("meshcore_vanity.leaderboards.public_key_from_meshcore_private") as slow:
            validated = validate_record(record, {(self.public, self.private)})
        self.assertIsNotNone(validated)
        slow.assert_not_called()

    def test_a_verifier_that_raises_is_ignored_not_fatal(self):
        from meshcore_vanity.storage import _restore_records
        from meshcore_vanity.config import BoardConfig

        def exploding(_pairs):
            raise RuntimeError("backend went away")

        record = {
            "public_key": self.public,
            "private_key": self.private,
            "seed": None,
            "score_version": 0,
        }
        unique, hall, _c, _r = _restore_records([record], BoardConfig(), None, exploding)
        self.assertEqual(len(unique), 1, "restore should fall back to verifying in Python")

    def test_two_records_sharing_a_public_key_are_judged_separately(self):
        """The case that makes key-based matching unsafe.

        A tampered copy of a record has the same public key and a different
        private key. Pairing verdicts by public key would let the good one's
        verdict vouch for the bad one.
        """
        from meshcore_vanity.engine_gpu import verify_pairs
        tampered = "CD" * 64
        for batch in ([(self.public, self.private), (self.public, tampered)],
                      [(self.public, tampered), (self.public, self.private)]):
            self.assertEqual(verify_pairs(self.binary, batch),
                             {(self.public, self.private)},
                             f"tampered pair leaked through for batch order {batch[0][1][:4]}")

    def test_a_short_reply_proves_nothing(self):
        """Fail closed if the backend answers fewer requests than it was given."""
        from unittest import mock
        from meshcore_vanity import engine_gpu
        reply = mock.Mock(returncode=0, stdout='{"public_key":"%s","valid":true}\n' % self.public)
        with mock.patch.object(engine_gpu.subprocess, "run", return_value=reply):
            result = engine_gpu.verify_pairs(
                self.binary, [(self.public, self.private), (self.public, "EF" * 64)])
        self.assertEqual(result, set())


class GpuPresenceTests(unittest.TestCase):
    """A CUDA-capable build is not a GPU.

    Conflating the two made the harvester announce "CPU + GPU" on a machine
    with no card, and would have had it pass --gpu-only to a binary that exits
    rather than run without one — three campaign failures and a disabled
    backend, on a machine that should simply have used the processor.
    """

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        base = Config.create(data_directory=Path(self.directory.name))
        self.config = replace(base, gpu=QUICK_GPU)
        self.queue: "queue.Queue" = queue.Queue()

    def tearDown(self):
        self.directory.cleanup()

    def _engine(self, device):
        from unittest import mock
        with StubEnvironment("gpu"), \
             mock.patch("meshcore_vanity.engine_gpu.gpu_name", return_value=device):
            return KeygenEngine(stub_binary(), self.config, self.queue)

    def test_mode_is_cpu_when_no_device_is_present(self):
        engine = self._engine(None)
        self.assertTrue(engine.capabilities["gpu"], "the stub does advertise GPU support")
        self.assertEqual(engine.mode, "cpu")

    def test_mode_is_gpu_when_a_device_is_present(self):
        self.assertEqual(self._engine("NVIDIA GeForce RTX 3080").mode, "gpu")

    def test_gpu_only_is_not_passed_without_a_device(self):
        engine = self._engine(None)
        self.assertNotIn("--gpu-only", engine._mode_arguments())

    def test_gpu_only_is_passed_with_a_device(self):
        self.assertIn("--gpu-only", self._engine("NVIDIA GeForce RTX 3080")._mode_arguments())

    def test_startup_verification_is_skipped_without_a_device(self):
        """--verify cross-checks the GPU kernel; with no GPU there is nothing to check."""
        engine = self._engine(None)
        self.assertTrue(engine._verify_backend(), "verification should be a no-op, not a failure")

    def test_the_engine_status_matches_its_mode(self):
        for device, mode in ((None, "cpu"), ("NVIDIA GeForce RTX 3080", "gpu")):
            engine = self._engine(device)
            self.assertEqual(engine.status.mode, mode)
            self.assertEqual(engine.status.mode, engine.mode)

    def test_a_deviceless_run_still_produces_matches(self):
        """The point of the fix: this machine should search, not give up."""
        engine = self._engine(None)
        engine.start()
        try:
            import time as _time
            deadline = _time.monotonic() + 60.0
            messages = []
            while _time.monotonic() < deadline:
                try:
                    messages.append(self.queue.get(timeout=1.0))
                except queue.Empty:
                    continue
                if any(m.get("type") == "keygen_result" for m in messages):
                    break
        finally:
            engine.stop()
        self.assertTrue([m for m in messages if m.get("type") == "keygen_result"],
                        f"no match without a GPU; saw {[m.get('type') for m in messages]}")
