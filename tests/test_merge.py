"""Offline aggregation of independently harvested private snapshots."""

from __future__ import annotations

import hashlib
import os
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from dataclasses import replace
from unittest import mock

from meshcore_vanity import SCORE_VERSION
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
    add_integrity_hash,
    advance_compute_ledger,
    checkpoint,
    compute_source_stats,
    merge_state_files,
    restore_state,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SHIM = PROJECT_ROOT / "meshcore_vanity_harvester.py"


def genuine_record(tag: str):
    """Find a genuine pair with a prefix pattern for persistence/board tests."""
    for suffix in range(100_000):
        seed = hashlib.sha256(f"{tag}-{suffix}".encode("ascii")).digest()
        public_key_hex = public_key_from_seed(seed).hex().upper()
        if public_key_hex.startswith(("00", "FF")):
            continue
        analysis = analyze_public_key(public_key_hex)
        if analysis["score"] > 0:
            return make_cpu_record(seed, public_key_hex, analysis, 0.0, 0)
    raise AssertionError("could not create a prefix-pattern test identity")


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


class LegacyImportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.config = Config.create(data_directory=self.root / "destination")
        self.record = genuine_record("legacy-import")

    def tearDown(self):
        self.temporary.cleanup()

    def write(self, payload, name="legacy.json"):
        path = self.root / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def merge(self, payload):
        return merge_state_files([self.write(payload)], self.config.boards)

    def test_original_hashed_board_names_and_ed25519_seed(self):
        old = dict(self.record)
        old["ed25519_seed"] = old.pop("seed")
        old["score_version"] = 7
        old["score"] = 999_999_999
        payload = add_integrity_hash({"repeater_id_top50": [old],
                                      "vanity_hall_of_fame_top50": [old],
                                      "attempts_total": 123, "elapsed_seconds_total": 12.5})
        with mock.patch("meshcore_vanity.leaderboards.public_key_from_meshcore_private") as slow:
            result = self.merge(payload)
        slow.assert_not_called()
        self.assertEqual(result.input_keys, 1)
        self.assertEqual(result.attempts_total, 123)
        restored = result.hall[self.record["public_key"]]
        self.assertEqual(restored["seed"], self.record["seed"])
        self.assertEqual(restored["score"], self.record["score"])
        self.assertEqual(restored["score_version"], SCORE_VERSION)

    def test_arbitrary_nesting_does_not_need_public_fields_or_board_names(self):
        payload = self.record["private_key"]
        for index in range(100):
            payload = {f"unknown-{index}": [payload]}
        result = self.merge(payload)
        self.assertEqual(result.distinct_keys, 1)
        self.assertIn(self.record["public_key"], result.hall)

    def test_public_key_maps_and_private_lookup_files(self):
        for payload in ({self.record["public_key"]: self.record["private_key"]},
                        {"keys": {self.record["public_key"]: {"private_key": self.record["private_key"]}}}):
            with self.subTest(payload_type=type(payload).__name__):
                self.assertEqual(set(self.merge(payload).hall), {self.record["public_key"]})

    def test_seed_fields_and_seed_private_keys(self):
        for field in ("seed", "ed25519_seed", "private_seed", "privateKey", "secret_key_hex"):
            result = self.merge({"somewhere": [{field: self.record["seed"]}]})
            self.assertEqual(result.hall[self.record["public_key"]]["private_key"], self.record["private_key"])

    def test_nested_seed_wrapper_and_hex_byte_arrays(self):
        for value in ("0x" + " ".join(self.record["seed"][i:i+2] for i in range(0, 64, 2)),
                      list(bytes.fromhex(self.record["seed"]))):
            self.assertIn(self.record["public_key"], self.merge({"privateKey": {"value": value}}).hall)
        self.assertIn(self.record["public_key"], self.merge([list(bytes.fromhex(self.record["private_key"]))]).hall)

    def test_seed_public_encoding_is_converted_to_meshcore_material(self):
        result = self.merge([self.record["seed"] + self.record["public_key"]])
        self.assertEqual(result.hall[self.record["public_key"]]["private_key"], self.record["private_key"])
        self.assertEqual(result.hall[self.record["public_key"]]["seed"], self.record["seed"])

    def test_unlabelled_public_keys_and_hashes_are_never_treated_as_seeds(self):
        with self.assertRaisesRegex(SnapshotMergeError, "no private key material"):
            self.merge([self.record["public_key"], {"checksum": self.record["seed"]}])

    def test_a_supplied_wrong_public_key_is_not_silently_replaced(self):
        for material in (self.record["private_key"], self.record["seed"],
                         self.record["seed"] + self.record["public_key"]):
            with self.assertRaisesRegex(SnapshotMergeError, "no key pair passed verification"):
                self.merge({"private_key": material, "public_key": "A7" * 32})

    def test_bad_duplicate_cannot_hide_a_good_pair(self):
        bad = {**self.record, "private_key": "AB" * 64}
        result = self.merge({"old_board": [bad, self.record, self.record]})
        self.assertIn(self.record["public_key"], result.hall)
        self.assertEqual(result.discarded_keys, 1)
        self.assertEqual(result.distinct_keys, 1)

    def test_malformed_metadata_cannot_leak_secrets_into_public_fields(self):
        record = {**self.record, "source": self.record["private_key"],
                  "found_at": self.record["seed"], "found_after_attempts": "invalid"}
        result = self.merge(record)
        restored = result.hall[self.record["public_key"]]
        self.assertEqual(restored["source"], "imported_private")
        self.assertNotEqual(restored["found_at"], self.record["seed"])
        self.assertIn(self.record["public_key"], self.merge({**record, "source": []}).hall)

    def test_nested_integrity_hash_failure_is_not_bypassed_by_generic_scan(self):
        tampered = add_integrity_hash({"private_key": self.record["private_key"]})
        tampered["extra"] = 1
        with self.assertRaisesRegex(SnapshotMergeError, "checksum mismatch"):
            self.merge([tampered])

    def test_duplicate_unhashed_imports_do_not_double_count_work(self):
        path = self.write({"record": self.record, "attempts_total": 20})
        copy = self.root / "copied.json"
        copy.write_bytes(path.read_bytes())
        result = merge_state_files([path, copy], self.config.boards)
        self.assertEqual(result.attempts_total, 20)
        self.assertEqual(result.distinct_keys, 1)
        self.assertTrue(result.statistics_approximate)

    def test_json_lines_and_plain_meshcore_commands(self):
        for content in (json.dumps({"record": self.record}) + "\n" + json.dumps({"record": self.record}),
                        self.record["private_key"] + "\nset prv.key " + self.record["private_key"]):
            path = self.root / "old.private.jsonl"
            path.write_text(content, encoding="utf-8")
            self.assertEqual(merge_state_files([path], self.config.boards).distinct_keys, 1)

    def test_invalid_lines_abort_the_import_without_echoing_material(self):
        path = self.root / "bad.private.jsonl"
        path.write_text(json.dumps(self.record) + "\nBROKEN " + self.record["private_key"])
        with self.assertRaises(SnapshotMergeError) as caught:
            merge_state_files([path], self.config.boards)
        self.assertIn("line 2", str(caught.exception))
        self.assertNotIn(self.record["private_key"], str(caught.exception))

    def test_signed_empty_destination_can_be_merged(self):
        checkpoint({}, {}, empty_category_boards(self.config.boards), 42, 5.0, 0,
                   "private_state", False, self.config)
        result = merge_state_files([self.config.paths.private_state, self.write([self.record])], self.config.boards)
        self.assertIn(self.record["public_key"], result.hall)
        self.assertEqual(result.attempts_total, 42)

    def test_verified_but_ineligible_keys_are_reported_separately(self):
        seed = hashlib.sha256(b"ordinary-legacy-key").digest()
        public = public_key_from_seed(seed).hex().upper()
        self.assertEqual(analyze_public_key(public)["score"], 0)
        result = self.merge({"ed25519_seed": seed.hex()})
        self.assertEqual(result.distinct_keys, 1)
        self.assertEqual(result.ineligible_keys, 1)
        self.assertEqual(result.discarded_keys, 0)
        self.assertFalse(result.hall)

    def test_unclamped_or_boolean_byte_arrays_are_not_accepted(self):
        for material in ("AB" * 64, [True] * 64, "not a key"):
            with self.assertRaisesRegex(SnapshotMergeError, "no key pair passed verification"):
                self.merge({"private_key": material})

    def test_valid_backup_can_recover_a_corrupt_snapshot(self):
        path = self.write({"private_key": self.record["private_key"]})
        self.config.paths.backup(path).write_bytes(path.read_bytes())
        path.write_text("{broken json")
        self.assertIn(self.record["public_key"], merge_state_files([path], self.config.boards).hall)

    def test_board_limits_do_not_change_verification_or_discard_counts(self):
        other = genuine_record("legacy-import-other")
        limits = replace(self.config.boards, top_repeater_ids=1, top_vanity_keys=1, top_category_keys=1)
        result = merge_state_files([self.write([self.record, other])], limits)
        self.assertEqual(result.distinct_keys, 2)
        self.assertEqual(result.discarded_keys, 0)
        self.assertEqual(len(result.hall), 1)

    def test_malformed_checksums_and_utf8_have_safe_errors(self):
        with self.assertRaisesRegex(SnapshotMergeError, "checksum mismatch"):
            self.merge({"private_key": self.record["private_key"], "integrity_sha256": "é" * 64})
        path = self.root / "non-utf8.json"
        path.write_bytes(b"\xff\xff")
        with self.assertRaisesRegex(SnapshotMergeError, "invalid UTF-8"):
            merge_state_files([path], self.config.boards)



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
        self.assertIn("Merged 2 private files successfully", completed.stdout)
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
        self.assertIn("Merged 2 private files successfully", completed.stdout)
        merged = Config.create(data_directory=destination, node_id="kraken")
        _unique, hall, *_ = restore_state(merged)
        self.assertEqual(
            set(hall),
            {kraken_record["public_key"], gx10_record["public_key"]},
        )

    def test_command_imports_legacy_and_current_files_without_public_secret_leaks(self):
        current, current_record = write_snapshot(self.root / "current", "kraken", "mixed-current", 50)
        legacy_record = genuine_record("mixed-legacy")
        legacy = self.root / "legacy.json"
        legacy.write_text(json.dumps({"old": {"unknown_board": [
            {"ed25519_seed": legacy_record["seed"]}]}}))
        before = legacy.read_bytes()
        destination = self.root / "merged"
        completed = self.run_merge(destination, legacy, current.paths.private_state)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        config = Config.create(data_directory=destination)
        _, hall, *_ = restore_state(config)
        self.assertEqual(set(hall), {legacy_record["public_key"], current_record["public_key"]})
        self.assertEqual(legacy.read_bytes(), before)
        for record in (legacy_record, current_record):
            for secret in (record["private_key"], record["seed"]):
                for output in (completed.stdout, completed.stderr, config.paths.public_state.read_text(),
                               config.paths.public_id_list.read_text(), config.paths.public_text.read_text()):
                    self.assertNotIn(secret, output)
        self.assertEqual(os.stat(config.paths.private_state).st_mode & 0o777, 0o600)

    def test_a_failed_import_leaves_the_destination_snapshot_unchanged(self):
        config, record = write_snapshot(self.root / "destination", "kraken", "failed-import", 50)
        before = config.paths.private_state.read_bytes()
        broken = self.root / "broken.json"
        broken.write_text(json.dumps(record) + "\nBROKEN " + record["private_key"])
        completed = self.run_merge(config.paths.data_directory, broken)
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(config.paths.private_state.read_bytes(), before)
        self.assertNotIn(record["private_key"], completed.stdout + completed.stderr)

    def test_in_place_unhashed_import_archives_even_retired_material(self):
        config = Config.create(data_directory=self.root / "legacy-destination")
        config.paths.data_directory.mkdir()
        good = genuine_record("in-place-legacy")
        retired_seed = hashlib.sha256(b"ordinary-legacy-key").hexdigest().upper()
        original = {"unknown_layout": [good, {"ed25519_seed": retired_seed}]}
        config.paths.private_state.write_text(json.dumps(original))
        completed = self.run_merge(config.paths.data_directory, config.paths.private_state)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("Excluded 1 verified identities", completed.stdout)
        archives = list(config.paths.data_directory.glob("before_merge_*.private.json"))
        self.assertEqual(len(archives), 1)
        self.assertEqual(os.stat(archives[0]).st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(archives[0].read_text())["imported_payload"], original)
        recovered = merge_state_files(archives, config.boards)
        self.assertEqual(recovered.distinct_keys, 2)
        self.assertEqual(recovered.ineligible_keys, 1)
        self.assertEqual(set(restore_state(config)[1]), {good["public_key"]})

    def test_a_backup_only_destination_is_included_automatically(self):
        config, first = write_snapshot(self.root / "backup-destination", "kraken", "backup-first", 20)
        backup = config.paths.backup(config.paths.private_state)
        backup.write_bytes(config.paths.private_state.read_bytes())
        config.paths.private_state.unlink()
        source, second = write_snapshot(self.root / "incoming", "gx10", "backup-second", 30)
        completed = self.run_merge(config.paths.data_directory, source.paths.private_state)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        _, hall, _, attempts, *_ = restore_state(config)
        self.assertEqual(set(hall), {first["public_key"], second["public_key"]})
        self.assertEqual(attempts, 50)


if __name__ == "__main__":
    unittest.main()
