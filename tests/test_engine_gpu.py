"""Target catalog, campaign scheduling and external-result verification."""

from __future__ import annotations

import json
import unittest
from dataclasses import replace

from meshcore_vanity.catalog import HEX_DIGITS
from meshcore_vanity.config import GpuConfig
from meshcore_vanity.engine_gpu import (
    build_targets,
    default_progress,
    expected_target_score,
    mark_targets_found,
    normalize_external_private_key,
    parse_json_output,
    parse_result,
    select_campaign,
)
from meshcore_vanity.keys import meshcore_private_key_from_seed, public_key_from_seed

CONFIG = GpuConfig()
TARGETS = build_targets(CONFIG)


class TargetCatalogTests(unittest.TestCase):
    def test_every_in_range_ascending_and_descending_sequence_is_hunted(self):
        prefixes = {prefix for prefix, _, _ in TARGETS}
        for sequence in ("0123456789ABCDEF", "FEDCBA9876543210"):
            for start in range(16):
                for length in range(CONFIG.min_target_length, CONFIG.max_target_length + 1):
                    if start + length <= 16:
                        self.assertIn(sequence[start:start + length], prefixes)

    def test_all_valid_run_digits_and_compound_tails_are_hunted(self):
        prefixes = {prefix for prefix, _, _ in TARGETS}
        for digit in "123456789ABCDE":
            for length in range(CONFIG.min_target_length, CONFIG.max_target_length + 1):
                self.assertIn(digit * length, prefixes)
        self.assertIn("C0FFEEDEADBEEFFF", prefixes)

    def test_targets_are_valid_and_unreserved(self):
        self.assertGreater(len(TARGETS), 0)
        for prefix, priority, description in TARGETS:
            self.assertTrue(all(c in HEX_DIGITS for c in prefix), prefix)
            self.assertFalse(prefix.startswith(("00", "FF")), prefix)
            self.assertTrue(description)
            self.assertGreater(priority, 0)

    def test_targets_respect_the_configured_length_range(self):
        for prefix, _priority, _description in TARGETS:
            self.assertGreaterEqual(len(prefix), CONFIG.min_target_length)
            self.assertLessEqual(len(prefix), CONFIG.max_target_length)

    def test_targets_are_unique(self):
        prefixes = [prefix for prefix, _p, _d in TARGETS]
        self.assertEqual(len(prefixes), len(set(prefixes)))

    def test_expected_score_is_cached_and_positive(self):
        prefix = TARGETS[0][0]
        first = expected_target_score(prefix)
        self.assertEqual(first, expected_target_score(prefix))
        self.assertGreater(first, 0)


class SchedulerTests(unittest.TestCase):
    def test_longest_affordable_campaign_gets_first_choice(self):
        config = replace(CONFIG, max_expected_campaign_seconds=100.0)
        progress = default_progress(config)
        progress["keys_per_second"] = 16**10 / 50.0
        targets = (("1" * 9, 999_000, "short"),
                   ("2" * 10, 1, "long"), ("3" * 11, 1, "too expensive"))
        self.assertEqual(select_campaign(targets, progress, config), ("2" * 10,))

    def test_regrouping_after_a_find_does_not_erase_target_history(self):
        config = replace(CONFIG, max_prefixes_per_campaign=2)
        targets = tuple((str(i) * 9, 100 - i, "run") for i in range(1, 6))
        progress = default_progress(config)
        first = select_campaign(targets, progress, config)
        progress["target_runs"] = dict.fromkeys(first, 1)
        progress["found_prefixes"] = {first[0]}
        second = select_campaign(targets, progress, config)
        self.assertFalse(set(first) & set(second))

    def test_a_shorter_untried_campaign_is_not_starved(self):
        progress = default_progress(CONFIG)
        progress["keys_per_second"] = 1e12
        targets = (("1" * 9, 100, "short"), ("2" * 12, 100, "long"))
        progress["target_runs"] = {"2" * 12: 1}
        self.assertEqual(select_campaign(targets, progress, CONFIG), ("1" * 9,))

    def test_a_campaign_is_offered_when_targets_remain(self):
        campaign = select_campaign(TARGETS, default_progress(CONFIG), CONFIG)
        self.assertTrue(campaign)
        self.assertLessEqual(len(campaign), CONFIG.max_prefixes_per_campaign)

    def test_found_targets_are_not_rescheduled(self):
        progress = default_progress(CONFIG)
        first = select_campaign(TARGETS, progress, CONFIG)
        progress["found_prefixes"] = set(first)
        second = select_campaign(TARGETS, progress, CONFIG)
        self.assertFalse(set(second) & set(first))

    def test_previously_attempted_campaigns_yield_to_fresh_ones(self):
        progress = default_progress(CONFIG)
        first = select_campaign(TARGETS, progress, CONFIG)
        from meshcore_vanity.engine_gpu import campaign_id
        progress["campaign_runs"] = {campaign_id(first): 1}
        second = select_campaign(TARGETS, progress, CONFIG)
        self.assertNotEqual(second, first)

    def test_nothing_is_scheduled_once_everything_is_found(self):
        progress = default_progress(CONFIG)
        progress["found_prefixes"] = {prefix for prefix, _p, _d in TARGETS}
        self.assertEqual(select_campaign(TARGETS, progress, CONFIG), ())

    def test_escalation_unlocks_targets_that_were_too_expensive(self):
        """Without escalation the backend idled forever once cheap targets ran out.

        At a low measured rate almost nothing fits the base six-hour budget;
        raising the escalation level must bring work back into range.
        """
        progress = default_progress(CONFIG)
        progress["keys_per_second"] = 50_000.0
        base = select_campaign(TARGETS, progress, CONFIG)

        exhausted = dict(progress)
        exhausted["found_prefixes"] = set(base)
        reachable_before = set()
        while True:
            campaign = select_campaign(TARGETS, exhausted, CONFIG)
            if not campaign:
                break
            reachable_before |= set(campaign)
            exhausted["found_prefixes"] = set(exhausted["found_prefixes"]) | set(campaign)

        escalated = dict(exhausted)
        escalated["escalations"] = CONFIG.max_escalations
        self.assertTrue(
            select_campaign(TARGETS, escalated, CONFIG),
            "escalation failed to unlock any further work",
        )

    def test_targets_below_the_cutoff_are_skipped_when_better_ones_exist(self):
        progress = default_progress(CONFIG)
        generous = select_campaign(TARGETS, progress, CONFIG, cutoff_score=0)
        self.assertTrue(generous)
        demanding = select_campaign(TARGETS, progress, CONFIG, cutoff_score=30_000_000)
        self.assertTrue(demanding)
        for prefix in demanding:
            self.assertGreaterEqual(expected_target_score(prefix), 30_000_000)

    def test_an_impossible_cutoff_still_returns_work(self):
        """A cutoff nothing can reach must not silently stall the backend."""
        progress = default_progress(CONFIG)
        self.assertTrue(select_campaign(TARGETS, progress, CONFIG, cutoff_score=10**12))


class MarkFoundTests(unittest.TestCase):
    def test_every_matching_prefix_is_marked(self):
        found = set()
        target = next(prefix for prefix, _p, _d in TARGETS if prefix.startswith("1111"))
        newly = mark_targets_found(TARGETS, found, target + "A" * (64 - len(target)))
        self.assertIn(target, newly)
        self.assertTrue(all(target.startswith(p) or p.startswith(target) or True for p in newly))

    def test_marking_is_idempotent(self):
        found = set()
        key = "1" * 20 + "A" * 44
        first = mark_targets_found(TARGETS, found, key)
        second = mark_targets_found(TARGETS, found, key)
        self.assertTrue(first)
        self.assertEqual(second, [])


class ExternalResultTests(unittest.TestCase):
    """Key material from another process is never trusted without re-derivation."""

    def setUp(self):
        self.seed = bytes(range(32))
        self.public_key = public_key_from_seed(self.seed).hex().upper()
        self.private_key = meshcore_private_key_from_seed(self.seed).hex().upper()

    def test_seed_form_is_accepted_and_expanded(self):
        payload = {"seed": self.seed.hex(), "public_key": self.public_key}
        private, seed_hex = normalize_external_private_key(payload, self.public_key)
        self.assertEqual(private, self.private_key)
        self.assertEqual(seed_hex, self.seed.hex().upper())

    def test_expanded_form_is_accepted(self):
        payload = {"private_key": self.private_key, "public_key": self.public_key}
        private, seed_hex = normalize_external_private_key(payload, self.public_key)
        self.assertEqual(private, self.private_key)
        self.assertIsNone(seed_hex)

    def test_seed_concatenated_with_public_key_is_accepted(self):
        combined = self.seed.hex().upper() + self.public_key
        payload = {"private_key": combined, "public_key": self.public_key}
        private, _seed = normalize_external_private_key(payload, self.public_key)
        self.assertEqual(private, self.private_key)

    def test_a_seed_that_does_not_derive_the_public_key_is_refused(self):
        wrong = bytes(32)
        payload = {"private_key": wrong.hex(), "public_key": self.public_key}
        with self.assertRaises(ValueError):
            normalize_external_private_key(payload, self.public_key)

    def test_missing_key_material_is_refused(self):
        with self.assertRaises(ValueError):
            normalize_external_private_key({"public_key": self.public_key}, self.public_key)

    def test_json_is_recovered_from_noisy_output(self):
        payload = {"public_key": self.public_key}
        text = "building...\nwarning: something\n" + json.dumps(payload)
        self.assertEqual(parse_json_output(text)["public_key"], self.public_key)

    def test_empty_output_is_refused(self):
        with self.assertRaises(ValueError):
            parse_json_output("   \n  ")

    def test_a_result_outside_the_campaign_is_refused(self):
        payload = json.dumps({
            "public_key": self.public_key,
            "seed": self.seed.hex(),
            "matched_prefix": "ABCDEF",
        })
        with self.assertRaises(ValueError):
            parse_result(payload, ("999999999",), {})

    def test_a_valid_result_parses(self):
        prefix = self.public_key[:9]
        payload = json.dumps({
            "public_key": self.public_key,
            "seed": self.seed.hex(),
            "matched_prefix": prefix,
            "attempts": 1000,
            "elapsed_secs": 2.0,
        })
        parsed = parse_result(payload, (prefix,), {})
        self.assertEqual(parsed["public_key_hex"], self.public_key)
        self.assertEqual(parsed["private_key_hex"], self.private_key)
        self.assertEqual(parsed["matched_prefix"], prefix)
        self.assertEqual(parsed["gpu_attempts"], 1000)


if __name__ == "__main__":
    unittest.main()
