"""Ranking model tests.

The properties worth protecting are the ones a future change could silently
break: that rarity dominates aesthetics, that families are comparable with each
other, and above all that the fast pre-filter never hides a qualifying key.
"""

from __future__ import annotations

import hashlib
import random
import unittest

from meshcore_vanity.catalog import HEX_WORDS, chain_count, words_of_length
from meshcore_vanity.config import MAX_TOTAL_TIEBREAKER_BONUS, RARITY_SCORE_PER_BIT
from meshcore_vanity.scoring import (
    FAMILY_CALIBRATION_BITS,
    analyze_public_key,
    pattern_family,
    quick_candidate,
    rarity_bits,
    required_rarity_bits,
)

HEX = "0123456789ABCDEF"


def padded(prefix: str) -> str:
    filler = hashlib.sha256((prefix + "|test").encode("ascii")).hexdigest().upper()
    return (prefix + filler * 2)[:64]


def score(prefix: str) -> int:
    return int(analyze_public_key(padded(prefix))["score"])


class RarityModelTests(unittest.TestCase):
    def test_one_rarity_bit_outranks_every_aesthetic_bonus(self):
        self.assertLess(MAX_TOTAL_TIEBREAKER_BONUS, RARITY_SCORE_PER_BIT)

    def test_longer_patterns_score_higher_within_a_family(self):
        self.assertLess(score("C0FFEE"), score("C0FFEEC0FFEE"))
        self.assertLess(score("C0FFEEC0FFEE"), score("C0FFEEC0FFEEC0FFEE"))
        self.assertLess(score("11111111"), score("1111111111111111"))
        self.assertLess(score("514514"), score("514514514514"))

    def test_long_run_beats_shorter_word_repeat(self):
        self.assertGreater(score("11111111111111111"), score("BADBADBADBADBAD"))

    def test_word_no_longer_gets_a_free_rarity_premium_over_a_run(self):
        """A six-character word is not as rare as six constrained nibbles.

        The catalog offers many six-character words, and the model must charge
        for that. This is the correction that made families comparable.
        """
        word_bits = analyze_public_key(padded("C0FFEE"))["rarity_bits"]
        self.assertLess(word_bits, 24.0 - 1.0)
        self.assertGreater(word_bits, 24.0 - 6.0)

    def test_preference_does_not_buy_rarity(self):
        """Wanting a pattern cannot make it mathematically rarer.

        A curated prefix and a comparable uncurated one of the same shape must
        land within aesthetic distance of each other, never a whole bit apart.
        """
        curated = analyze_public_key(padded("444444444444"))["rarity_bits"]
        neutral = analyze_public_key(padded("777777777777"))["rarity_bits"]
        self.assertAlmostEqual(curated, neutral, places=6)

    def test_position_penalty_applies_away_from_the_prefix(self):
        """The same word is less surprising when the key offered 58 places for it."""
        filler = hashlib.sha256(b"position-test").hexdigest().upper()
        at_prefix = analyze_public_key(("DEADBEEF" + filler * 2)[:64])["rarity_bits"]
        buried = analyze_public_key(("9C3" + "DEADBEEF" + filler * 2)[:64])["rarity_bits"]
        self.assertGreater(at_prefix, buried)

    def test_reference_class_counts_are_sane(self):
        self.assertGreaterEqual(words_of_length(6), 1)
        self.assertGreaterEqual(chain_count(8), 1)
        self.assertEqual(words_of_length(999), 1)

    def test_every_family_has_a_calibration_entry(self):
        families = {pattern_family(kind) for kind in (
            "word", "word_repeat", "single_run", "periodic",
            "sequence", "palindrome", "structured_id", "preference",
        )}
        for family in families:
            self.assertIn(family, FAMILY_CALIBRATION_BITS)

    def test_rarity_never_negative(self):
        self.assertGreaterEqual(rarity_bits("word", 1, 10_000, 64), 0.0)

    def test_analysis_reports_a_signature(self):
        analysis = analyze_public_key(padded("C0FFEEC0FFEE"))
        self.assertTrue(analysis["pattern_signature"])
        self.assertIn("C0FFEE", analysis["pattern_signature"])

    def test_rejects_malformed_keys(self):
        with self.assertRaises(ValueError):
            analyze_public_key("TOO SHORT")
        with self.assertRaises(ValueError):
            analyze_public_key("Z" * 64)


class QuickFilterTests(unittest.TestCase):
    """The pre-filter's one hard contract: no false negatives, ever."""

    def _assert_admits(self, key: str, cutoffs):
        actual = int(analyze_public_key(key)["score"])
        for cutoff in cutoffs:
            if actual >= cutoff:
                self.assertTrue(
                    quick_candidate(key, cutoff),
                    f"filter rejected a qualifying key at cutoff {cutoff}: {key}",
                )

    def test_admits_known_patterns_at_their_own_score(self):
        for prefix in (
            "C0FFEE", "C0FFEEC0FFEE", "C0FFEEEEEEEE", "DEADBEEF", "CAFEBABE",
            "444444444444", "11111111111111111", "ACEACEACEACE", "514514514",
            "123456789ABCDEF", "FEDCBA98765", "ABABABABABAB", "EFFACEE",
        ):
            key = padded(prefix)
            actual = int(analyze_public_key(key)["score"])
            self._assert_admits(key, (actual, actual - 1, actual // 2, 1))

    def test_no_false_negatives_across_cutoffs_on_random_keys(self):
        rng = random.Random(20260819)
        cutoffs = (1_000_000, 8_000_000, 16_000_000, 24_000_000, 32_000_000)
        for _ in range(4_000):
            key = "".join(rng.choice(HEX) for _ in range(64))
            if key.startswith(("00", "FF")):
                continue
            self._assert_admits(key, cutoffs)

    def test_no_false_negatives_on_pattern_rich_keys(self):
        """Random keys rarely exercise the interesting branches; force them."""
        rng = random.Random(4242)
        cutoffs = (1_000_000, 12_000_000, 20_000_000, 28_000_000)
        words = list(HEX_WORDS)
        for index in range(3_000):
            key = ["".join(rng.choice(HEX) for _ in range(64))[i] for i in range(64)]
            mode = index % 6
            if mode == 0:
                length = rng.randint(4, 20)
                start = rng.randint(0, 64 - length)
                key[start:start + length] = [rng.choice(HEX)] * length
            elif mode == 1:
                word = rng.choice(words)
                start = rng.randint(0, 64 - len(word))
                key[start:start + len(word)] = list(word)
            elif mode == 2:
                unit = "".join(rng.choice(HEX) for _ in range(rng.randint(2, 6)))
                repeats = rng.randint(2, 8)
                start = rng.randint(0, max(0, 64 - len(unit) * repeats))
                block = (unit * repeats)[:64 - start]
                key[start:start + len(block)] = list(block)
            elif mode == 3:
                word = rng.choice(words)
                key[0:len(word)] = list(word)
                extra = rng.randint(1, 10)
                key[len(word):len(word) + extra] = [word[-1]] * extra
            elif mode == 4:
                length = rng.randint(6, 14)
                start = rng.randint(0, 64 - length)
                half = [rng.choice(HEX) for _ in range(length // 2)]
                middle = [] if length % 2 == 0 else [rng.choice(HEX)]
                key[start:start + length] = (half + middle + half[::-1])[:length]
            else:
                source = "0123456789ABCDEF" if index % 2 else "FEDCBA9876543210"
                length = rng.randint(5, 12)
                offset = rng.randint(0, 16 - length)
                start = rng.randint(0, 64 - length)
                key[start:start + length] = list(source[offset:offset + length])
            candidate = "".join(key)[:64]
            if len(candidate) != 64 or candidate.startswith(("00", "FF")):
                continue
            self._assert_admits(candidate, cutoffs)

    def test_low_cutoff_admits_everything(self):
        """The old filter floored its threshold at 8 bits and lost keys below it."""
        self.assertLessEqual(required_rarity_bits(500_000), 0.0)
        self.assertTrue(quick_candidate("0" * 63 + "1", 500_000))

    def test_filter_actually_rejects_at_a_high_cutoff(self):
        """A filter that admitted everything would also pass the tests above."""
        rng = random.Random(7)
        admitted = 0
        total = 2_000
        for _ in range(total):
            key = "".join(rng.choice(HEX) for _ in range(64))
            if quick_candidate(key, 24 * RARITY_SCORE_PER_BIT):
                admitted += 1
        self.assertLess(admitted / total, 0.05)


if __name__ == "__main__":
    unittest.main()
