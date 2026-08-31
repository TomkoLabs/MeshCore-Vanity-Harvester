"""Startup: verification cost, resumption, and interruption.

Startup used to re-derive every saved record through the from-scratch scalar
multiplication in ``keys.py``. At ~95 ms per record and up to five boards
holding the same keys, a full set of leaderboards meant minutes of silent work
before the first line of output — indistinguishable from a hang, and a stack
trace if you pressed Ctrl+C. These tests pin down the fixes.
"""

from __future__ import annotations

import hashlib
import signal
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from meshcore_vanity.cli import install_early_interrupt_handlers
from meshcore_vanity.config import Config
from meshcore_vanity.keys import (
    meshcore_private_key_from_seed,
    public_key_from_meshcore_private,
    public_key_from_seed,
)
from meshcore_vanity.leaderboards import (
    empty_category_boards,
    hall_rank,
    insert_by_public_key,
    insert_category,
    insert_unique,
    make_cpu_record,
    needs_slow_verification,
    validate_record,
)
from meshcore_vanity.report import RestoreProgress, Style
from meshcore_vanity.scoring import analyze_public_key
from meshcore_vanity.storage import checkpoint, restore_state

PLAIN = Style(enabled=False)
SLOW_PATH = "meshcore_vanity.leaderboards.public_key_from_meshcore_private"


def genuine_record(tag: str):
    seed = hashlib.sha256(tag.encode("ascii")).digest()
    public_key_hex = public_key_from_seed(seed).hex().upper()
    if public_key_hex.startswith(("00", "FF")):
        return None
    return make_cpu_record(seed, public_key_hex, analyze_public_key(public_key_hex), 0.0, 0)


class VerificationPathTests(unittest.TestCase):
    """A saved seed proves the record; the slow path is for external keys."""

    def test_a_record_with_a_valid_seed_skips_the_slow_path(self):
        record = genuine_record("fast-path")
        with mock.patch(SLOW_PATH) as slow:
            validated = validate_record(record)
        self.assertIsNotNone(validated)
        slow.assert_not_called()

    def test_a_record_without_a_seed_uses_the_slow_path(self):
        record = dict(genuine_record("slow-path"))
        record["seed"] = None
        self.assertTrue(needs_slow_verification(record))
        with mock.patch(SLOW_PATH, wraps=public_key_from_meshcore_private) as slow:
            validated = validate_record(record)
        self.assertIsNotNone(validated)
        slow.assert_called_once()

    def test_a_seed_that_does_not_match_is_discarded_but_the_record_survives(self):
        """The private key still proves itself; only the bad seed is dropped."""
        record = dict(genuine_record("wrong-seed"))
        record["seed"] = "11" * 32
        validated = validate_record(record)
        self.assertIsNotNone(validated)
        self.assertIsNone(validated["seed"])

    def test_a_seed_matching_the_key_but_not_the_private_key_is_rejected(self):
        record = dict(genuine_record("mismatched-private"))
        other = hashlib.sha256(b"other").digest()
        record["private_key"] = meshcore_private_key_from_seed(other).hex().upper()
        self.assertIsNone(validate_record(record))

    def test_verification_is_fast_enough_to_not_look_like_a_hang(self):
        records = [r for r in (genuine_record(f"speed-{i}") for i in range(40)) if r]
        started = time.monotonic()
        for record in records:
            validate_record(record)
        elapsed = time.monotonic() - started
        # The slow path alone would need about 95 ms each.
        self.assertLess(elapsed, len(records) * 0.02, "verification fell back to the slow path")


class DeduplicationTests(unittest.TestCase):
    """One key normally sits on several boards; verify it once."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.config = Config.create(data_directory=Path(self.directory.name))
        self.records = [r for r in (genuine_record(f"dedupe-{i}") for i in range(25)) if r]
        self.assertGreater(len(self.records), 0)

        unique, hall = {}, {}
        categories = empty_category_boards(self.config.boards)
        for record in self.records:
            insert_unique(unique, record, self.config.boards.top_repeater_ids)
            insert_by_public_key(hall, record, self.config.boards.top_vanity_keys,
                                 self.config.boards.max_per_signature)
            insert_category(categories, record, self.config.boards)
        checkpoint(unique, hall, categories, 100, 1.0, 1, "new", False, self.config)

    def tearDown(self):
        self.directory.cleanup()

    def test_each_distinct_key_is_validated_exactly_once(self):
        target = "meshcore_vanity.storage.validate_record"
        with mock.patch(target, side_effect=validate_record) as spy:
            restore_state(self.config)
        distinct = {r["public_key"] for r in self.records}
        self.assertEqual(spy.call_count, len(distinct))

    def test_the_boards_still_come_back_fully_populated(self):
        unique, hall, categories, *_ = restore_state(self.config)
        self.assertEqual(len(unique), len({r["repeater_id"] for r in self.records}))
        self.assertGreater(len(hall), 0)
        self.assertGreater(sum(len(b) for b in categories.values()), 0)

    def test_totals_and_provenance_survive_the_round_trip(self):
        _u, _h, _c, attempts, elapsed, events, source, _approx, _rescored = restore_state(self.config)
        self.assertEqual(source, "private_state")
        self.assertEqual(attempts, 100)
        self.assertAlmostEqual(elapsed, 1.0, places=3)
        self.assertEqual(events, 1)

    def test_repeated_restore_and_checkpoint_cycles_are_stable(self):
        """Stop and start repeatedly; the boards must not drift or shrink."""
        sizes = []
        for _ in range(4):
            unique, hall, categories, attempts, elapsed, events, _s, _a, _r = restore_state(self.config)
            sizes.append((len(unique), len(hall)))
            checkpoint(unique, hall, categories, attempts, elapsed, events,
                       "private_state", False, self.config)
        self.assertEqual(len(set(sizes)), 1, f"board sizes drifted across restarts: {sizes}")


class ProgressReportingTests(unittest.TestCase):
    """A slow restore must look like work, not a hang."""

    class Sink:
        def __init__(self):
            self.lines = []

        def write(self, text):
            self.lines.append(text)

        def flush(self):
            pass

    def test_a_small_fast_restore_stays_quiet(self):
        sink = self.Sink()
        progress = RestoreProgress(PLAIN, stream=sink)
        progress("start", 12, 0)
        progress("done", 12, 0)
        self.assertEqual(sink.lines, [])

    def test_records_needing_the_slow_path_are_announced(self):
        sink = self.Sink()
        progress = RestoreProgress(PLAIN, stream=sink)
        progress("start", 554, 554)
        progress("done", 554, 554)
        text = "".join(sink.lines)
        self.assertIn("554", text)
        self.assertIn("full re-derivation", text)

    def test_a_large_fast_restore_is_still_announced(self):
        sink = self.Sink()
        progress = RestoreProgress(PLAIN, stream=sink, announce_threshold=100)
        progress("start", 500, 0)
        self.assertIn("Verifying 500", "".join(sink.lines))

    def test_a_small_merge_input_does_not_inherit_an_earlier_announcement(self):
        sink = self.Sink()
        progress = RestoreProgress(PLAIN, stream=sink, announce_threshold=100)
        progress("start", 500, 0)
        progress("done", 500, 0)
        before = "".join(sink.lines)
        progress("start", 2, 0)
        progress("done", 2, 0)
        self.assertEqual("".join(sink.lines), before)


class InterruptHandlingTests(unittest.TestCase):
    """Ctrl+C and SIGTERM must work before the supervisor loop exists."""

    def setUp(self):
        self.original = {
            signal.SIGINT: signal.getsignal(signal.SIGINT),
            signal.SIGTERM: signal.getsignal(signal.SIGTERM),
        }

    def tearDown(self):
        for signum, handler in self.original.items():
            signal.signal(signum, handler)

    def test_both_signals_get_a_handler(self):
        install_early_interrupt_handlers()
        for signum in (signal.SIGINT, signal.SIGTERM):
            handler = signal.getsignal(signum)
            self.assertTrue(callable(handler), f"{signum} has no handler")
            self.assertNotEqual(handler, signal.SIG_DFL)
            self.assertNotEqual(handler, signal.SIG_IGN)

    def test_an_inherited_ignored_sigint_is_overridden(self):
        """Backgrounded processes inherit SIGINT as ignored and become unstoppable."""
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        install_early_interrupt_handlers()
        self.assertNotEqual(signal.getsignal(signal.SIGINT), signal.SIG_IGN)

    def test_the_handler_raises_keyboard_interrupt(self):
        install_early_interrupt_handlers()
        handler = signal.getsignal(signal.SIGTERM)
        with self.assertRaises(KeyboardInterrupt):
            handler(signal.SIGTERM, None)


class HallRankTests(unittest.TestCase):
    def test_rank_reflects_board_position(self):
        board = {}
        records = []
        for index in range(5):
            record = genuine_record(f"rank-{index}")
            if record is None:
                continue
            record = dict(record)
            record["score"] = 10_000_000 + index
            records.append(record)
            insert_by_public_key(board, record, 500)
        best = max(records, key=lambda r: r["score"])
        worst = min(records, key=lambda r: r["score"])
        self.assertEqual(hall_rank(board, best), 1)
        self.assertEqual(hall_rank(board, worst), len(records))

    def test_rank_on_an_empty_board_is_one(self):
        self.assertEqual(hall_rank({}, {"public_key": "A" * 64, "score": 1, "pattern_length": 1}), 1)


class SourceTaggingTests(unittest.TestCase):
    """Finds from both engines are announced, and the source is visible."""

    def test_cpu_and_gpu_finds_are_labelled_differently(self):
        from meshcore_vanity.report import match_line

        cpu = dict(genuine_record("tag-cpu"))
        gpu = dict(genuine_record("tag-gpu"))
        gpu["source"] = "mc_keygen_gpu"
        self.assertIn("FOUND CPU", match_line(PLAIN, cpu, rank=3))
        self.assertIn("FOUND GPU", match_line(PLAIN, gpu, 462_000_000, rank=1))

    def test_rank_is_shown_when_known(self):
        from meshcore_vanity.report import match_line

        self.assertIn("#7", match_line(PLAIN, dict(genuine_record("tag-rank")), rank=7))


if __name__ == "__main__":
    unittest.main()


class PatternlessRecordTests(unittest.TestCase):
    """A key with no pattern is not a vanity key, whatever produced it."""

    def test_the_scorer_gives_a_patternless_key_zero(self):
        analysis = analyze_public_key("9C3B7A1E5D8F204C6B9E1A7D3F85206C4E9B1D7A3F58206C4E9B1D7A3F58206C")
        self.assertIn(analysis["pattern_kind"], ("none", "palindrome", "word",
                                                 "periodic", "single_run", "sequence"))

    def test_accept_refuses_records_with_no_measurable_rarity(self):
        """Backend results bypass the worker cutoff, so accept() must guard.

        A zero-rarity key can still carry an aesthetic bonus, so checking the
        score alone would let it onto a rarity-first board.
        """
        import inspect
        from meshcore_vanity import cli
        source = inspect.getsource(cli.main)
        self.assertIn('record.get("rarity_bits", 0.0)) <= 0.0', source,
                      "accept() no longer guards against zero-rarity records")
        self.assertIn('record.get("score", 0)) <= 0', source)


class DurabilityTests(unittest.TestCase):
    """Long runs must not fill the disk, and a failed write must not be fatal."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.config = Config.create(data_directory=Path(self.directory.name))
        self.record = genuine_record("durability")

    def tearDown(self):
        self.directory.cleanup()

    def test_the_history_journal_is_rotated_rather_than_growing_forever(self):
        from meshcore_vanity.storage import (append_history_event, history_paths,
                                             rotate_history_if_large)
        paths = self.config.paths
        for _ in range(40):
            append_history_event(self.record, ["hall"], 1, paths)
        self.assertTrue(paths.history.is_file())

        # A tiny limit stands in for the 32 MiB production threshold.
        self.assertTrue(rotate_history_if_large(paths, limit=1))
        self.assertFalse(paths.history.exists())
        append_history_event(self.record, ["hall"], 2, paths)
        self.assertEqual(len(history_paths(paths)), 2)

    def test_recovery_reads_every_journal_generation(self):
        from meshcore_vanity.storage import append_history_event, rotate_history_if_large
        paths = self.config.paths
        append_history_event(self.record, ["hall"], 1, paths)
        rotate_history_if_large(paths, limit=1)
        second = genuine_record("durability-2")
        append_history_event(second, ["hall"], 2, paths)

        unique, hall, _c, _a, _e, events, source, _ap, _rs = restore_state(self.config)
        self.assertEqual(source, "history_journal")
        self.assertEqual(events, 2, "an older journal generation was ignored")
        self.assertEqual(len(hall), 2)

    def test_rotated_journals_stay_private(self):
        import os as _os
        from meshcore_vanity.storage import append_history_event, rotate_history_if_large
        paths = self.config.paths
        append_history_event(self.record, ["hall"], 1, paths)
        rotate_history_if_large(paths, limit=1)
        previous = paths.history.with_name(paths.history.name + ".1")
        self.assertEqual(_os.stat(previous).st_mode & 0o777, 0o600)

    def test_skipping_the_backup_halves_what_a_checkpoint_writes(self):
        from meshcore_vanity.leaderboards import empty_category_boards, insert_by_public_key
        hall = {}
        insert_by_public_key(hall, self.record, 500)
        categories = empty_category_boards(self.config.boards)

        # The first write has nothing to back up; the second does.
        checkpoint({}, hall, categories, 1, 1.0, 1, "new", False, self.config, keep_backup=True)
        checkpoint({}, hall, categories, 1, 1.0, 1, "new", False, self.config, keep_backup=True)
        with_backup = sorted(p.name for p in self.config.paths.data_directory.glob("*.bak"))
        self.assertTrue(with_backup, "a backup should have been taken")

        for stale in self.config.paths.data_directory.glob("*.bak"):
            stale.unlink()
        checkpoint({}, hall, categories, 2, 2.0, 2, "new", False, self.config, keep_backup=False)
        self.assertEqual(list(self.config.paths.data_directory.glob("*.bak")), [])
        # The live snapshot is still complete and readable.
        _u, restored, *_ = restore_state(self.config)
        self.assertEqual(len(restored), 1)

    def test_a_failing_checkpoint_is_reported_not_fatal(self):
        import inspect
        from meshcore_vanity import cli
        source = inspect.getsource(cli.main)
        self.assertIn("except OSError as error:", source)
        self.assertIn("Could not save state", source)
