import json
import stat
import tempfile
import unittest
from pathlib import Path

import meshcore_vanity_harvester as harvester


def padded(prefix: str) -> str:
    filler = "09F3C6D2E8A1475B" * 4
    return (prefix + filler)[: harvester.PUBLIC_KEY_HEX_LENGTH]


def fake_record(public_key: str, family: str, kind: str, score: int, length: int):
    repeater_id = public_key[:6]
    return {
        "score": score,
        "rarity_bits": score / harvester.RARITY_SCORE_PER_BIT,
        "pattern_length": length,
        "pattern_start": 0,
        "pattern_kind": kind,
        "pattern_family": family,
        "repeater_id": repeater_id,
        "repeater_id_formatted": ":".join(repeater_id[index:index + 2] for index in range(0, 6, 2)),
        "public_key": public_key,
        "private_key": "11" * 64,
        "ed25519_seed": None,
        "set_command": "set prv.key " + "11" * 64,
        "reasons": [f"test {family}"],
        "found_at": "2026-01-01T00:00:00Z",
        "attempts_total_when_found": 1,
        "attempts_counter_scope": "test",
        "source": "test",
    }


class CatalogTests(unittest.TestCase):
    def test_catalog_is_english_and_french_only(self):
        self.assertNotIn("ABECEDA", harvester.HEX_WORDS)
        self.assertIn("C0FFEE", harvester.HEX_WORDS)
        self.assertIn("EFFACEE", harvester.HEX_WORDS)


class RankingTests(unittest.TestCase):
    def test_long_single_run_beats_shorter_word_repeat(self):
        ones = harvester.analyze_public_key(padded("1" * 17))
        bad = harvester.analyze_public_key(padded("BAD" * 5))
        self.assertEqual("single_run", ones["pattern_family"])
        self.assertEqual("word", bad["pattern_family"])
        self.assertGreater(ones["score"], bad["score"])

    def test_longer_word_repeat_wins(self):
        shorter = harvester.analyze_public_key(padded("ACE" * 4))
        longer = harvester.analyze_public_key(padded("ACE" * 6))
        self.assertGreater(longer["score"], shorter["score"])

    def test_one_rarity_bit_always_dominates_aesthetic_bonuses(self):
        lower_ceiling = 10 * harvester.RARITY_SCORE_PER_BIT + harvester.MAX_TOTAL_TIEBREAKER_BONUS
        higher_floor = 11 * harvester.RARITY_SCORE_PER_BIT
        self.assertLess(lower_ceiling, higher_floor)

    def test_quick_filter_admits_full_scorer_candidates(self):
        prefixes = (
            "C0FFEEC0FFEE",
            "C0FFEEEEEEEE",
            "ACEBAD",
            "ABABABABABAB",
            "1" * 17,
            "123456789ABC",
        )
        for prefix in prefixes:
            with self.subTest(prefix=prefix):
                value = padded(prefix)
                analysis = harvester.analyze_public_key(value)
                self.assertGreater(analysis["score"], 0)
                self.assertTrue(harvester.quick_candidate(value, analysis["score"]))

    def test_quick_filter_covers_patterns_away_from_prefix(self):
        base = list(("D09F3C6E2A81475B" * 4)[: harvester.PUBLIC_KEY_HEX_LENGTH])
        patterns = (
            (9, "ABABAB"),
            (8, "123456789ABC123456789ABC"),
            (20, "777777777"),
            (17, "ABC0CBA"),
            (24, "EFFACEE"),
            (13, "1234567"),
        )
        for start, pattern in patterns:
            with self.subTest(start=start, pattern=pattern):
                value = base.copy()
                value[start:start + len(pattern)] = pattern
                public_key = "".join(value)
                analysis = harvester.analyze_public_key(public_key)
                self.assertGreater(analysis["score"], 0)
                self.assertTrue(harvester.quick_candidate(public_key, analysis["score"]))

    def test_equal_score_ties_use_length_then_smaller_public_key(self):
        shorter = fake_record("B" * 64, "word", "word", 20_000_000, 6)
        longer = fake_record("F" * 64, "word", "word", 20_000_000, 7)
        smaller = fake_record("A" * 64, "word", "word", 20_000_000, 7)
        self.assertGreater(harvester.record_sort_key(longer), harvester.record_sort_key(shorter))
        self.assertGreater(harvester.record_sort_key(smaller), harvester.record_sort_key(longer))


class LeaderboardTests(unittest.TestCase):
    def test_category_boards_retain_each_requested_family(self):
        categories = harvester.empty_category_leaderboards()
        records = (
            fake_record("A" * 64, "word", "word_repeat", 30_000_000, 9),
            fake_record("B" * 64, "single_run", "single_run", 31_000_000, 9),
            fake_record("C" * 64, "periodic", "periodic", 32_000_000, 9),
        )
        for record in records:
            self.assertEqual(record["pattern_family"], harvester.insert_category(categories, record))
        self.assertEqual({family: 1 for family in harvester.CATEGORY_BOARD_FAMILIES}, {
            family: len(board) for family, board in categories.items()
        })

    def test_checkpoint_writes_public_index_and_private_map(self):
        old_directory = harvester.DATA_DIRECTORY
        try:
            with tempfile.TemporaryDirectory() as temporary:
                harvester.set_data_directory(Path(temporary))
                harvester.ensure_secure_directory()
                record = fake_record("A12345" + "B" * 58, "word", "word", 24_000_000, 6)
                unique = {record["repeater_id"]: record}
                hall = {record["public_key"]: record}
                categories = harvester.empty_category_leaderboards()
                categories["word"][record["public_key"]] = record
                harvester.checkpoint(unique, hall, categories, 1, 1.0, 1, "test", False)

                lines = harvester.PUBLIC_ID_LIST_PATH.read_text(encoding="utf-8").splitlines()
                self.assertEqual(1, len(lines))
                self.assertEqual(record["public_key"], json.loads(lines[0])["public_key"])

                private_map = json.loads(harvester.PRIVATE_KEY_MAP_PATH.read_text(encoding="utf-8"))
                self.assertEqual(record["private_key"], private_map["keys"][record["public_key"]]["private_key"])
                self.assertEqual(0o600, stat.S_IMODE(harvester.PRIVATE_KEY_MAP_PATH.stat().st_mode))
                self.assertEqual(0o644, stat.S_IMODE(harvester.PUBLIC_ID_LIST_PATH.stat().st_mode))
        finally:
            harvester.set_data_directory(old_directory)


if __name__ == "__main__":
    unittest.main()
