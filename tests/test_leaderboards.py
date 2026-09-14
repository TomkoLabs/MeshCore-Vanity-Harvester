"""Board insertion, diversity, persistence and recovery."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from meshcore_vanity import SCORE_VERSION
from meshcore_vanity.config import BoardConfig, Config
from meshcore_vanity.keys import public_key_from_seed
from meshcore_vanity.leaderboards import (
    board_diversity,
    current_cutoff,
    empty_category_boards,
    insert_by_public_key,
    insert_category,
    insert_unique,
    make_cpu_record,
    sorted_records,
    validate_record,
)
from meshcore_vanity.scoring import analyze_public_key
from meshcore_vanity.storage import (
    advance_compute_ledger,
    checkpoint,
    compute_ledger_totals,
    compute_source_stats,
    merge_compute_ledgers,
    restore_state,
    verify_integrity_hash,
)


def real_record(tag: str):
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



def synthetic_record(public_key_hex: str, score: int, signature: str, length: int = 8):
    return {
        "public_key": public_key_hex,
        "repeater_id": public_key_hex[:6],
        "private_key": "AB" * 64,
        "seed": None,
        "score": score,
        "score_version": SCORE_VERSION,
        "rarity_bits": score / 1_000_000,
        "pattern_length": length,
        "pattern_start": 0,
        "pattern_kind": "single_run",
        "pattern_family": "single_run",
        "pattern_signature": signature,
        "reasons": ["synthetic"],
        "source": "test",
        "found_at": "2026-01-01T00:00:00Z",
        "found_after_attempts": 0,
    }


class BoardTests(unittest.TestCase):
    def test_sequence_has_its_own_board(self):
        config = BoardConfig()
        categories = empty_category_boards(config)
        key = ("123456789ABC0" + hashlib.sha256(b"sequence-board").hexdigest().upper())[:64]
        analysis = analyze_public_key(key)
        record = synthetic_record(key, analysis["score"], analysis["pattern_signature"])
        record.update(analysis)
        self.assertEqual(insert_category(categories, record, config), "sequence")
        self.assertIn(key, categories["sequence"])

    def test_higher_score_replaces_a_weaker_repeater_id_entry(self):
        board = {}
        low = synthetic_record("AAAAAA" + "1" * 58, 10_000_000, "run:A")
        high = synthetic_record("AAAAAA" + "2" * 58, 20_000_000, "run:A")
        self.assertTrue(insert_unique(board, low, 500))
        self.assertTrue(insert_unique(board, high, 500))
        self.assertEqual(len(board), 1)
        self.assertEqual(board["AAAAAA"]["score"], 20_000_000)

    def test_weaker_entry_is_rejected(self):
        board = {}
        insert_unique(board, synthetic_record("AAAAAA" + "1" * 58, 20_000_000, "run:A"), 500)
        self.assertFalse(
            insert_unique(board, synthetic_record("AAAAAA" + "2" * 58, 10_000_000, "run:A"), 500)
        )

    def test_limit_evicts_the_worst_entry(self):
        board = {}
        for index in range(5):
            key = f"{index:06X}" + "A" * 58
            insert_by_public_key(board, synthetic_record(key, 10_000_000 + index, f"sig{index}"), 3)
        self.assertEqual(len(board), 3)
        self.assertEqual(min(r["score"] for r in board.values()), 10_000_002)

    def test_ties_break_on_length_then_public_key(self):
        board = {}
        short = synthetic_record("11" + "A" * 62, 15_000_000, "s", length=6)
        long_ = synthetic_record("22" + "A" * 62, 15_000_000, "s", length=9)
        insert_by_public_key(board, short, 500)
        insert_by_public_key(board, long_, 500)
        self.assertEqual(sorted_records(board.values())[0]["pattern_length"], 9)


class DiversityTests(unittest.TestCase):
    """Rarity alone would fill a board with one shape; the signature cap stops it."""

    def test_board_keeps_only_the_best_few_of_one_shape(self):
        board = {}
        for index in range(10):
            key = f"{index:06X}" + "B" * 58
            insert_by_public_key(board, synthetic_record(key, 10_000_000 + index, "run:1"), 500, 3)
        self.assertEqual(len(board), 3)
        self.assertEqual(
            sorted(r["score"] for r in board.values()),
            [10_000_007, 10_000_008, 10_000_009],
        )

    def test_a_better_key_still_displaces_the_worst_of_its_own_shape(self):
        board = {}
        for index in range(3):
            insert_by_public_key(board, synthetic_record(f"{index:06X}" + "B" * 58, 10_000_000 + index, "run:1"), 500, 3)
        newcomer = synthetic_record("ABCDEF" + "B" * 58, 99_000_000, "run:1")
        self.assertTrue(insert_by_public_key(board, newcomer, 500, 3))
        self.assertEqual(len(board), 3)
        self.assertIn("ABCDEF" + "B" * 58, board)

    def test_different_shapes_are_not_capped_against_each_other(self):
        board = {}
        for index in range(10):
            key = f"{index:06X}" + "C" * 58
            insert_by_public_key(board, synthetic_record(key, 10_000_000 + index, f"run:{index}"), 500, 3)
        self.assertEqual(len(board), 10)

    def test_disabled_cap_keeps_everything(self):
        board = {}
        for index in range(10):
            insert_by_public_key(board, synthetic_record(f"{index:06X}" + "D" * 58, 10_000_000 + index, "run:1"), 500, 0)
        self.assertEqual(len(board), 10)

    def test_diversity_metric(self):
        records = [synthetic_record(f"{i:06X}" + "E" * 58, 1, "a" if i < 8 else "b") for i in range(10)]
        self.assertAlmostEqual(board_diversity(records), 0.2)
        self.assertEqual(board_diversity([]), 0.0)


class CutoffTests(unittest.TestCase):
    def test_cutoff_is_the_floor_until_a_board_fills(self):
        config = BoardConfig()
        self.assertEqual(
            current_cutoff({}, {}, empty_category_boards(config), config),
            config.initial_minimum_score,
        )

    def test_cutoff_stays_at_the_floor_until_every_board_is_full(self):
        """The slowest-filling board gates the cutoff, by design.

        Raising it as soon as one board fills would starve the others: a word
        board would never collect entries once runs pushed the cutoff up.
        """
        config = BoardConfig(top_repeater_ids=2, top_vanity_keys=2, top_category_keys=2)
        floor = config.initial_minimum_score
        unique, hall = {}, {}
        categories = empty_category_boards(config)
        for index in range(2):
            record = synthetic_record(f"{index:06X}" + "A" * 58, floor + 5_000_000 + index, f"s{index}")
            insert_unique(unique, record, config.top_repeater_ids)
            insert_by_public_key(hall, record, config.top_vanity_keys)
            insert_category(categories, record, config)
        self.assertEqual(current_cutoff(unique, hall, categories, config), floor)

    def test_cutoff_rises_once_every_board_is_full(self):
        config = BoardConfig(top_repeater_ids=2, top_vanity_keys=2, top_category_keys=2)
        floor = config.initial_minimum_score
        unique, hall = {}, {}
        categories = empty_category_boards(config)
        index = 0
        for family in config.families:
            for _ in range(2):
                record = synthetic_record(f"{index:06X}" + "A" * 58, floor + 5_000_000 + index, f"s{index}")
                record["pattern_family"] = family
                insert_unique(unique, record, config.top_repeater_ids)
                insert_by_public_key(hall, record, config.top_vanity_keys)
                insert_category(categories, record, config)
                index += 1
        self.assertGreater(current_cutoff(unique, hall, categories, config), floor)


class ValidationTests(unittest.TestCase):
    def test_a_genuine_record_validates(self):
        record = real_record("validation-sample")
        self.assertIsNotNone(record)
        self.assertIsNotNone(validate_record(record))

    def test_a_tampered_public_key_is_rejected(self):
        record = dict(real_record("tamper-sample"))
        flipped = "F" if record["public_key"][10] != "F" else "0"
        record["public_key"] = record["public_key"][:10] + flipped + record["public_key"][11:]
        self.assertIsNone(validate_record(record))

    def test_a_malformed_private_key_is_rejected(self):
        record = dict(real_record("bad-private"))
        record["private_key"] = "00" * 64
        self.assertIsNone(validate_record(record))

    def test_a_record_with_no_score_version_counts_as_rescored(self):
        record = dict(real_record("no-version-sample"))
        record.pop("score_version", None)
        validated = validate_record(record)
        self.assertIsNotNone(validated)
        self.assertEqual(validated["rescored_from_version"], "unknown")

    def test_an_old_score_is_recomputed_not_trusted(self):
        record = dict(real_record("rescore-sample"))
        record["score"] = 1
        record["score_version"] = SCORE_VERSION - 1
        validated = validate_record(record)
        self.assertIsNotNone(validated)
        self.assertNotEqual(validated["score"], 1)
        self.assertEqual(validated["score_version"], SCORE_VERSION)
        self.assertEqual(validated["rescored_from_version"], SCORE_VERSION - 1)


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.config = Config.create(data_directory=Path(self.directory.name))

    def tearDown(self):
        self.directory.cleanup()

    def _seeded_boards(self):
        unique, hall = {}, {}
        categories = empty_category_boards(self.config.boards)
        count = 0
        for index in range(40):
            record = real_record(f"persist-{index}")
            if record is None:
                continue
            insert_unique(unique, record, self.config.boards.top_repeater_ids)
            insert_by_public_key(hall, record, self.config.boards.top_vanity_keys,
                                 self.config.boards.max_per_signature)
            insert_category(categories, record, self.config.boards)
            count += 1
        self.assertGreater(count, 0)
        return unique, hall, categories

    def test_checkpoint_then_restore_round_trips(self):
        unique, hall, categories = self._seeded_boards()
        checkpoint(unique, hall, categories, 1234, 5.0, 7, "new", False, self.config)
        restored = restore_state(self.config)
        self.assertEqual(restored[6], "private_state")
        self.assertEqual(len(restored[0]), len(unique))
        self.assertEqual(restored[3], 1234)

    def test_checkpoint_records_which_node_did_the_work(self):
        config = Config.create(
            data_directory=Path(self.directory.name), node_id="kraken")
        unique, hall, categories = self._seeded_boards()
        checkpoint(unique, hall, categories, 1234, 5.0, 7, "new", False, config)
        payload = json.loads(config.paths.private_state.read_text(encoding="utf-8"))
        self.assertEqual(payload["node_id"], "kraken")
        self.assertEqual(
            payload["compute_sources"]["kraken"],
            compute_source_stats(1234, 5.0, 7),
        )

    def test_public_snapshot_contains_no_secrets(self):
        unique, hall, categories = self._seeded_boards()
        checkpoint(unique, hall, categories, 1, 1.0, 1, "new", False, self.config)
        text = self.config.paths.public_state.read_text(encoding="utf-8")
        payload = json.loads(text)
        self.assertTrue(verify_integrity_hash(payload))
        self.assertFalse(payload["includes_secret_material"])
        self.assertNotIn("node_id", payload)
        self.assertNotIn("compute_sources", payload)

        sections = [payload["repeater_ids"], payload["vanity_hall_of_fame"]]
        sections.extend(payload["categories"].values())
        published = [entry for section in sections for entry in section]
        self.assertGreater(len(published), 0)
        for entry in published:
            for secret in ("private_key", "seed"):
                self.assertNotIn(secret, entry, f"public board leaked {secret}")

        # And no 64-byte private key can appear anywhere in the file, whatever
        # the field happened to be called.
        private = json.loads(self.config.paths.private_state.read_text(encoding="utf-8"))
        for entry in private["vanity_hall_of_fame"]:
            self.assertNotIn(entry["private_key"], text)

    def test_public_text_matches_the_hall_of_fame_rank_order(self):
        unique, hall, categories = self._seeded_boards()
        checkpoint(unique, hall, categories, 1, 1.0, 1, "new", False, self.config)

        public_payload = json.loads(self.config.paths.public_state.read_text(encoding="utf-8"))
        expected = [entry["public_key"] for entry in public_payload["vanity_hall_of_fame"]]
        actual = self.config.paths.public_text.read_text(encoding="utf-8").splitlines()

        self.assertEqual(actual, expected)
        self.assertTrue(all(len(public_key) == 64 for public_key in actual))

    def test_empty_hall_writes_an_empty_public_text_file(self):
        categories = empty_category_boards(self.config.boards)
        checkpoint({}, {}, categories, 0, 0.0, 0, "new", False, self.config)
        self.assertEqual(self.config.paths.public_text.read_text(encoding="utf-8"), "")

    def test_secret_files_are_not_world_readable(self):
        unique, hall, categories = self._seeded_boards()
        checkpoint(unique, hall, categories, 1, 1.0, 1, "new", False, self.config)
        for path in (self.config.paths.private_state, self.config.paths.private_key_map):
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600, f"{path} is too permissive")
        for path in (self.config.paths.public_state, self.config.paths.public_text):
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o644)
        self.assertEqual(os.stat(self.config.paths.data_directory).st_mode & 0o777, 0o700)

    def test_a_corrupted_snapshot_falls_back_to_the_backup(self):
        unique, hall, categories = self._seeded_boards()
        checkpoint(unique, hall, categories, 1, 1.0, 1, "new", False, self.config)
        checkpoint(unique, hall, categories, 2, 2.0, 2, "new", False, self.config)
        self.config.paths.private_state.write_text("{ not json", encoding="utf-8")
        restored = restore_state(self.config)
        self.assertGreater(len(restored[0]), 0)

    def test_tampered_integrity_hash_is_refused(self):
        unique, hall, categories = self._seeded_boards()
        checkpoint(unique, hall, categories, 1, 1.0, 1, "new", False, self.config)
        for path in (self.config.paths.private_state,
                     self.config.paths.private_state.with_name(
                         self.config.paths.private_state.name + ".bak")):
            if path.exists():
                payload = json.loads(path.read_text(encoding="utf-8"))
                payload["cpu_attempts_total"] = 999_999_999
                path.write_text(json.dumps(payload), encoding="utf-8")
        restored = restore_state(self.config)
        self.assertNotEqual(restored[3], 999_999_999)

    def test_public_index_lists_every_repeater_id(self):
        unique, hall, categories = self._seeded_boards()
        checkpoint(unique, hall, categories, 1, 1.0, 1, "new", False, self.config)
        lines = [
            json.loads(line)
            for line in self.config.paths.public_id_list.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertEqual(len(lines), len(unique))
        for entry in lines:
            self.assertEqual(len(entry["public_key"]), 64)
            self.assertNotIn("private_key", entry)


class ComputeLedgerTests(unittest.TestCase):
    def test_a_session_advances_only_its_own_node(self):
        baseline = {
            "gx10": compute_source_stats(200, 20.0, 2),
            "kraken": compute_source_stats(100, 10.0, 1),
        }
        advanced = advance_compute_ledger(baseline, "gx10", 50, 5.0, 1)
        self.assertEqual(advanced["kraken"], baseline["kraken"])
        self.assertEqual(advanced["gx10"], compute_source_stats(250, 25.0, 3))

    def test_remerging_a_shared_baseline_does_not_double_count_it(self):
        baseline = {
            "gx10": compute_source_stats(200, 20.0, 2),
            "kraken": compute_source_stats(100, 10.0, 1),
        }
        gx10_later = advance_compute_ledger(baseline, "gx10", 50, 5.0, 1)
        merged = merge_compute_ledgers((baseline, gx10_later))
        self.assertEqual(compute_ledger_totals(merged), (350, 35.0, 4))


if __name__ == "__main__":
    unittest.main()


class DataDirectoryTests(unittest.TestCase):
    def test_a_checkout_keeps_state_beside_the_code(self):
        from meshcore_vanity.config import PROJECT_DIRECTORY, default_data_directory, is_source_checkout
        if not is_source_checkout():
            self.skipTest("not running from a checkout")
        self.assertEqual(default_data_directory(), PROJECT_DIRECTORY / "data")


class LegacyFormatTests(unittest.TestCase):
    """Section names have changed between releases.

    A reader that looked for fixed names silently ignored a v7 snapshot, losing
    every saved key along with the attempt and runtime totals behind them.
    Records are now found structurally, so any layout still restores.
    """

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.config = Config.create(data_directory=Path(self.directory.name))
        # A genuine v7 file predates per-record score versions, so strip them:
        # the point of the test is that such records are found and rescored.
        self.records = []
        for index in range(12):
            record = real_record(f"legacy-{index}")
            if record is None:
                continue
            record = dict(record)
            record.pop("score_version", None)
            self.records.append(record)
        self.assertGreater(len(self.records), 0)

    def tearDown(self):
        self.directory.cleanup()

    def _write(self, payload):
        from meshcore_vanity.storage import add_integrity_hash, atomic_write_json
        atomic_write_json(self.config.paths.private_state, add_integrity_hash(payload), private=True)

    def test_v7_section_names_still_restore(self):
        self._write({
            "state_format_version": 5,
            "score_version": 7,
            "attempts_total": 26_035_401_205,
            "elapsed_seconds_total": 237_270.232,
            "leaderboard_events": 257,
            "repeater_id_top50": self.records,
            "vanity_hall_of_fame_top50": self.records,
        })
        unique, hall, _cat, attempts, elapsed, events, source, _approx, rescored = restore_state(self.config)
        self.assertEqual(source, "private_state")
        self.assertGreater(len(unique), 0)
        self.assertGreater(len(hall), 0)
        self.assertEqual(attempts, 26_035_401_205)
        self.assertAlmostEqual(elapsed, 237_270.232, places=3)
        self.assertEqual(events, 257)
        # Distinct keys, not board entries: the same key sits on several boards.
        self.assertEqual(rescored, len({r["public_key"] for r in self.records}))
        for record in hall.values():
            self.assertEqual(record["score_version"], SCORE_VERSION)
            self.assertEqual(record["rescored_from_version"], "unknown")

    def test_an_unfamiliar_layout_still_restores(self):
        self._write({
            "state_format_version": 99,
            "boards": {"something_new": {"entries": self.records}},
        })
        unique, hall, *_ = restore_state(self.config)
        self.assertGreater(len(unique), 0)
        self.assertGreater(len(hall), 0)

    def test_a_file_with_no_records_is_not_mistaken_for_state(self):
        self._write({"state_format_version": 9, "notes": ["nothing here"]})
        _unique, _hall, _cat, _att, _el, _ev, source, *_ = restore_state(self.config)
        self.assertEqual(source, "new")


class WorkerAllocationTests(unittest.TestCase):
    """Cores held back should match what is actually being fed."""

    def _cpu(self, count, **kwargs):
        from meshcore_vanity.config import CpuConfig
        return CpuConfig(available_cpu_ids=tuple(range(count)), **kwargs)

    def test_a_cpu_only_machine_keeps_almost_every_core_busy(self):
        self.assertEqual(self._cpu(16).resolved(backend_active=False).workers, 15)

    def test_a_core_is_held_back_to_feed_the_gpu(self):
        self.assertEqual(self._cpu(16).resolved(backend_active=True).workers, 14)

    def test_tiny_machines_still_get_a_worker(self):
        for count in (1, 2):
            self.assertGreaterEqual(self._cpu(count).resolved(True).workers, 1)
            self.assertLessEqual(self._cpu(count).resolved(True).workers, count)

    def test_an_explicit_request_overrides_the_automatic_choice(self):
        self.assertEqual(self._cpu(16, requested_workers=6).resolved(True).workers, 6)
        self.assertEqual(self._cpu(16, requested_workers=6).resolved(False).workers, 6)

    def test_a_request_larger_than_the_machine_is_clamped(self):
        self.assertEqual(self._cpu(8, requested_workers=99).resolved(True).workers, 8)

    def test_resolving_twice_is_stable(self):
        once = self._cpu(16).resolved(True)
        self.assertEqual(once.resolved(True).workers, once.workers)

    def test_worker_cpu_ids_match_the_worker_count(self):
        resolved = self._cpu(16).resolved(True)
        self.assertEqual(len(resolved.worker_cpu_ids), resolved.workers)
        self.assertTrue(resolved.gpu_host_cpu_ids)
