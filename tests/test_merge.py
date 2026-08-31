"""Offline aggregation of independently harvested private snapshots."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from meshcore_vanity.config import Config
from meshcore_vanity.keys import public_key_from_seed
from meshcore_vanity.leaderboards import (
    empty_category_boards,
    insert_by_public_key,
    insert_category,
    insert_unique,
    make_cpu_record,
)
from meshcore_vanity.scoring import analyze_public_key
from meshcore_vanity.storage import (
    SnapshotMergeError,
    advance_compute_ledger,
    checkpoint,
    compute_source_stats,
    merge_state_files,
    restore_state,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SHIM = PROJECT_ROOT / "meshcore_vanity_harvester.py"


def genuine_record(tag: str):
    for suffix in range(100):
        seed = hashlib.sha256(f"{tag}-{suffix}".encode("ascii")).digest()
        public_key = public_key_from_seed(seed).hex().upper()
        if public_key.startswith(("00", "FF")):
            continue
        return make_cpu_record(seed, public_key, analyze_public_key(public_key), 0.0, 0)
    raise AssertionError("could not create an unreserved test identity")


def write_snapshot(directory: Path, node_id: str, tag: str, attempts: int,
                   ledger=None):
    config = Config.create(data_directory=directory, node_id=node_id)
    record = genuine_record(tag)
    unique, hall = {}, {}
    categories = empty_category_boards(config.boards)
    insert_unique(unique, record, config.boards.top_repeater_ids)
    insert_by_public_key(hall, record, config.boards.top_vanity_keys,
                         config.boards.max_per_signature)
    insert_category(categories, record, config.boards)
    checkpoint(
        unique, hall, categories, attempts, 10.0, 1, "new", False, config,
        compute_sources=ledger,
    )
    return config, record


class MergeEngineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_independent_snapshots_are_verified_and_combined(self):
        kraken, kraken_record = write_snapshot(
            self.root / "kraken", "kraken", "kraken-key", 100)
        gx10, gx10_record = write_snapshot(
            self.root / "gx10", "gx10", "gx10-key", 200)

        result = merge_state_files(
            [kraken.paths.private_state, gx10.paths.private_state], kraken.boards)

        self.assertEqual(result.source_count, 2)
        self.assertEqual(result.distinct_keys, 2)
        self.assertEqual(result.attempts_total, 300)
        self.assertEqual(set(result.compute_sources), {"gx10", "kraken"})
        self.assertEqual(
            set(result.hall),
            {kraken_record["public_key"], gx10_record["public_key"]},
        )

    def test_a_shared_baseline_is_not_counted_twice(self):
        baseline = {
            "gx10": compute_source_stats(200, 20.0, 2),
            "kraken": compute_source_stats(100, 10.0, 1),
        }
        gx10_later = advance_compute_ledger(baseline, "gx10", 50, 5.0, 1)
        first, _ = write_snapshot(
            self.root / "first", "kraken", "first-key", 300, baseline)
        second, _ = write_snapshot(
            self.root / "second", "gx10", "second-key", 350, gx10_later)

        result = merge_state_files(
            [first.paths.private_state, second.paths.private_state], first.boards)

        self.assertEqual(result.attempts_total, 350)
        self.assertEqual(result.elapsed_total, 35.0)
        self.assertEqual(result.events_total, 4)

    def test_a_public_snapshot_is_refused(self):
        source, _record = write_snapshot(
            self.root / "source", "kraken", "public-refusal", 100)
        with self.assertRaisesRegex(SnapshotMergeError, "public snapshots cannot be merged"):
            merge_state_files([source.paths.public_state], source.boards)

    def test_a_tampered_snapshot_is_refused(self):
        source, _record = write_snapshot(
            self.root / "source", "kraken", "tampered", 100)
        payload = json.loads(source.paths.private_state.read_text(encoding="utf-8"))
        payload["cpu_attempts_total"] = 999999
        source.paths.private_state.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaisesRegex(SnapshotMergeError, "no valid integrity-hashed snapshot"):
            merge_state_files([source.paths.private_state], source.boards)


class MergeCommandTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def run_merge(self, destination: Path, *sources: Path):
        return subprocess.run(
            [
                sys.executable,
                str(SHIM),
                "--no-gpu",
                "--node-id",
                "merge-hub",
                "--data-dir",
                str(destination),
                "--merge",
                *(str(source) for source in sources),
            ],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )

    def test_command_accepts_directories_and_writes_every_output(self):
        kraken, kraken_record = write_snapshot(
            self.root / "kraken", "kraken", "command-kraken", 100)
        gx10, gx10_record = write_snapshot(
            self.root / "gx10", "gx10", "command-gx10", 200)
        destination = self.root / "merged"

        completed = self.run_merge(
            destination, kraken.paths.data_directory, gx10.paths.private_state)

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("Merged 2 private snapshots successfully", completed.stdout)
        merged = Config.create(data_directory=destination, node_id="merge-hub")
        _unique, hall, _categories, attempts, *_ = restore_state(merged)
        self.assertEqual(attempts, 300)
        self.assertEqual(
            set(hall),
            {kraken_record["public_key"], gx10_record["public_key"]},
        )
        for path in (
            merged.paths.private_state,
            merged.paths.public_state,
            merged.paths.public_text,
            merged.paths.private_key_map,
            merged.paths.public_id_list,
        ):
            self.assertTrue(path.is_file(), f"merge did not write {path.name}")

    def test_destination_snapshot_is_included_automatically(self):
        destination = self.root / "kraken"
        _kraken, kraken_record = write_snapshot(
            destination, "kraken", "in-place-kraken", 100)
        gx10, gx10_record = write_snapshot(
            self.root / "gx10", "gx10", "in-place-gx10", 200)

        completed = self.run_merge(destination, gx10.paths.data_directory)

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("Merged 2 private snapshots successfully", completed.stdout)
        merged = Config.create(data_directory=destination, node_id="kraken")
        _unique, hall, *_ = restore_state(merged)
        self.assertEqual(
            set(hall),
            {kraken_record["public_key"], gx10_record["public_key"]},
        )


if __name__ == "__main__":
    unittest.main()
