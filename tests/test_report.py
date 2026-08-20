"""Console presentation and backend status.

What the user reads is a real interface: if it says "GPU" the GPU must be
running, and if the GPU is idle it must say what to do about it. These tests
pin that down.
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest import mock

from meshcore_vanity.config import GpuConfig
from meshcore_vanity.engine_gpu import describe_backend
from meshcore_vanity.report import (
    Style,
    backend_summary,
    count,
    duration,
    engine_block,
    key_preview,
    labelled,
    leaderboard_table,
    match_line,
    ranking_footnote,
    resume_line,
    status_line,
    supports_colour,
)

PLAIN = Style(enabled=False)


def record(score=18_710_000, bits=18.71, repeater="C0FFEE", reason="vanity word 'C0FFEE'"):
    return {
        "public_key": repeater + "A" * (64 - len(repeater)),
        "repeater_id": repeater,
        "score": score,
        "rarity_bits": bits,
        "reasons": [reason],
    }


class FormattingTests(unittest.TestCase):
    def test_durations_read_naturally(self):
        self.assertEqual(duration(45), "45s")
        self.assertEqual(duration(750), "12m 30s")
        self.assertEqual(duration(12_000), "3h 20m")
        self.assertEqual(duration(237_270), "2d 17h")
        self.assertEqual(duration(-5), "0s")

    def test_counts_are_compact(self):
        self.assertEqual(count(512), "512")
        self.assertEqual(count(51_000), "51k")
        self.assertEqual(count(1_270_000), "1.27M")
        self.assertEqual(count(462_000_000), "462M")
        self.assertEqual(count(26_035_401_205), "26B")

    def test_key_preview_truncates_only_when_needed(self):
        self.assertEqual(key_preview("ABCD", 16), "ABCD")
        self.assertTrue(key_preview("A" * 64, 16).endswith("…"))
        self.assertEqual(len(key_preview("A" * 64, 16)), 17)

    def test_labelled_aligns_and_tolerates_emptiness(self):
        self.assertEqual(labelled([]), [])
        lines = labelled([("A", "1"), ("Longer", "2")], indent="  ")
        self.assertTrue(all(line.startswith("  ") for line in lines))
        self.assertEqual(len({len(line.split(":")[0]) for line in lines}), 2)


class ColourTests(unittest.TestCase):
    def test_plain_style_emits_no_escape_codes(self):
        self.assertNotIn("\033", PLAIN.bold("x") + PLAIN.dim("y") + PLAIN.good("z"))

    def test_no_color_env_disables_colour(self):
        with mock.patch.dict(os.environ, {"NO_COLOR": "1"}, clear=False):
            self.assertFalse(supports_colour())

    def test_zero_valued_switches_mean_off(self):
        """FORCE_COLOR=0 must mean off, not "the variable exists"."""
        with mock.patch.dict(os.environ, {"FORCE_COLOR": "0", "NO_COLOR": ""}, clear=False):
            self.assertFalse(supports_colour())
        with mock.patch.dict(os.environ, {"FORCE_COLOR": "1"}, clear=False):
            self.assertTrue(supports_colour())


class BackendStatusTests(unittest.TestCase):
    """The user must never have to guess whether the GPU is being used."""

    def test_gpu_build_reports_both_engines(self):
        with mock.patch("meshcore_vanity.engine_gpu.gpu_name", return_value="NVIDIA GeForce RTX 3080"):
            status = describe_backend(GpuConfig(), Path("/tmp/mc-keygen"), {"gpu": True})
        self.assertTrue(status.active)
        self.assertEqual(status.mode, "gpu")
        self.assertIn("CPU + GPU", status.headline)
        self.assertIn("RTX 3080", status.headline)
        self.assertIsNone(status.remedy)

    def test_a_cuda_build_on_a_machine_with_no_gpu_does_not_claim_a_gpu(self):
        """The exact contradiction seen in the field.

        The installer said "No NVIDIA GPU detected" and the self-test said
        "CPU + GPU - both engines running", because the status was read off the
        binary's compiled capability rather than the hardware. Worse than
        misleading: the engine would then pass --gpu-only to a binary that
        refuses it, and disable itself after three campaign failures.
        """
        with mock.patch("meshcore_vanity.engine_gpu.gpu_name", return_value=None):
            status = describe_backend(GpuConfig(), Path("/tmp/mc-keygen"), {"gpu": True})
        self.assertEqual(status.mode, "cpu")
        self.assertNotIn("CPU + GPU", status.headline)
        self.assertIn("no GPU detected", status.headline)
        self.assertIn("nvidia-smi", status.remedy or "")

    def test_a_device_that_names_its_vendor_is_not_stuttered(self):
        for name, expected in (("NVIDIA GeForce RTX 3080", "an NVIDIA GeForce RTX 3080"),
                               ("A100", "an NVIDIA A100")):
            with mock.patch("meshcore_vanity.engine_gpu.gpu_name", return_value=name):
                status = describe_backend(GpuConfig(), Path("/tmp/mc-keygen"), {"gpu": True})
            self.assertIn(expected, status.headline)
            self.assertNotIn("NVIDIA NVIDIA", status.headline)

    def test_cpu_build_with_a_gpu_present_says_how_to_fix_it(self):
        with mock.patch("meshcore_vanity.engine_gpu.gpu_name", return_value="NVIDIA GeForce RTX 3080"):
            status = describe_backend(GpuConfig(), Path("/tmp/mc-keygen"), {"gpu": False})
        self.assertTrue(status.active)
        self.assertEqual(status.mode, "cpu")
        self.assertIn("GPU kernel is not compiled in", status.headline)
        self.assertIn("--features cuda", status.remedy or "")

    def test_missing_binary_with_a_gpu_present_points_at_the_installer(self):
        with mock.patch("meshcore_vanity.engine_gpu.gpu_name", return_value="NVIDIA GeForce RTX 3080"):
            status = describe_backend(GpuConfig(), None, None)
        self.assertFalse(status.active)
        self.assertIn("not built", status.headline)
        self.assertIn("install.sh", status.remedy or "")

    def test_no_gpu_hardware_is_stated_plainly_without_nagging(self):
        with mock.patch("meshcore_vanity.engine_gpu.gpu_name", return_value=None):
            status = describe_backend(GpuConfig(), None, None)
        self.assertFalse(status.active)
        self.assertIn("CPU only", status.headline)
        self.assertIsNone(status.remedy)

    def test_explicit_opt_out_is_reported_as_such(self):
        with mock.patch("meshcore_vanity.engine_gpu.gpu_name", return_value=None):
            status = describe_backend(GpuConfig(enabled=False), None, None)
        self.assertFalse(status.active)
        self.assertIn("--no-gpu", status.headline)


class EngineBlockTests(unittest.TestCase):
    def test_block_names_both_engines_when_the_gpu_is_live(self):
        with mock.patch("meshcore_vanity.engine_gpu.gpu_name", return_value="NVIDIA GeForce RTX 3080"):
            status = describe_backend(GpuConfig(), Path("/tmp/mc-keygen"), {"gpu": True})
        text = "\n".join(engine_block(PLAIN, 12, 16, status, 1418))
        self.assertIn("12 workers of 16 logical CPUs", text)
        self.assertIn("1,418 exact prefixes", text)
        self.assertIn("GPU", text)

    def test_block_says_mc_keygen_is_not_running_when_it_is_not(self):
        with mock.patch("meshcore_vanity.engine_gpu.gpu_name", return_value=None):
            status = describe_backend(GpuConfig(), None, None)
        text = "\n".join(engine_block(PLAIN, 4, 4, status, 0))
        self.assertIn("not running", text)


class ResumeTests(unittest.TestCase):
    def test_a_fresh_start_says_so(self):
        text = "\n".join(resume_line(PLAIN, "new", 0, 0, 0, 0.0, 0))
        self.assertIn("fresh", text)

    def test_resuming_reports_the_carried_over_totals(self):
        text = "\n".join(resume_line(PLAIN, "private_state", 500, 500, 26_035_401_205, 237_270, 51))
        self.assertIn("Resuming", text)
        self.assertIn("26B", text)
        self.assertIn("2d 17h", text)
        self.assertIn("51 saved keys", text)

    def test_journal_recovery_explains_itself(self):
        text = "\n".join(resume_line(PLAIN, "history_journal", 10, 10, 1, 1.0, 0))
        self.assertIn("journal", text)


class RunningOutputTests(unittest.TestCase):
    def test_backend_summary_distinguishes_off_gpu_and_cpu(self):
        self.assertEqual(backend_summary(False, "off", 0, 0, 0, 0, (), 0), "GPU off")
        gpu = backend_summary(True, "gpu", 22, 1418, 19, 462_000_000, ("999999999",), 11)
        self.assertIn("GPU", gpu)
        self.assertIn("462M/s", gpu)
        self.assertIn("22/1418", gpu)
        cpu = backend_summary(True, "cpu", 1, 10, 1, 40_000, (), 0)
        self.assertIn("mc-keygen", cpu)
        self.assertIn("idle", cpu)

    def test_status_line_carries_every_number_that_matters(self):
        line = status_line(PLAIN, 407, 50_985_557, 125_000, 500, 500, 500, 500,
                           16.40, record(), "GPU 462M/s (19 found)")
        for fragment in ("51M", "125k/s", "500/500", "16.4 bits", "C0FFEE"):
            self.assertIn(fragment, line)

    def test_status_line_handles_an_empty_board(self):
        line = status_line(PLAIN, 1, 0, 0.0, 0, 500, 0, 500, 16.0, None, "GPU off")
        self.assertIn("none yet", line)

    def test_match_line_shows_the_key_rarity_and_reason(self):
        line = match_line(PLAIN, record(bits=36.0, reason="'E' run of 10 characters"), 471_000_000)
        self.assertIn("FOUND", line)
        self.assertIn("36.00 bits", line)
        self.assertIn("run of 10", line)
        self.assertIn("471M/s", line)

    def test_match_line_omits_a_rate_it_does_not_have(self):
        self.assertNotIn("/s", match_line(PLAIN, record(), None).split("·")[-1])


    def test_the_backend_tag_distinguishes_gpu_from_a_cpu_build(self):
        """A CPU-only backend build is not the GPU and must not claim to be."""
        gpu = record()
        gpu["source"] = "mc_keygen_gpu"
        cpu_backend = record()
        cpu_backend["source"] = "mc_keygen_cpu"
        worker = record()
        worker["source"] = "generic_cpu"

        self.assertIn("FOUND GPU", match_line(PLAIN, gpu))
        self.assertIn("FOUND MCK", match_line(PLAIN, cpu_backend))
        self.assertNotIn("GPU", match_line(PLAIN, cpu_backend))
        self.assertIn("FOUND CPU", match_line(PLAIN, worker))


    def test_the_found_line_carries_the_whole_public_key(self):
        """This is the line people copy a key out of; truncating it is useless."""
        full = "C0FFEE514" + "A" * 55
        entry = record()
        entry["public_key"] = full
        line = match_line(PLAIN, entry, rank=1)
        self.assertIn(full, line)
        self.assertNotIn("…" + " ", line.split(full)[0])

    def test_the_status_line_shows_how_many_the_cpu_has_found(self):
        """Once the hall fills with deep GPU prefixes, CPU finds stop being
        announced. Without a count the CPU looks idle while it is working."""
        line = status_line(PLAIN, 60, 1_300_000, 21_600, 500, 500, 212, 500,
                           16.0, record(), "GPU off", cpu_found=113)
        self.assertIn("113 found", line)

    def test_the_count_is_optional_for_older_callers(self):
        line = status_line(PLAIN, 60, 1, 1.0, 0, 500, 0, 500, 16.0, None, "GPU off")
        self.assertIn("0 found", line)


class LeaderboardTableTests(unittest.TestCase):
    def test_empty_board_is_stated_not_blank(self):
        self.assertIn("no entries", "\n".join(leaderboard_table(PLAIN, [])))

    def test_rank_column_descends_even_when_rarity_does_not(self):
        """Sub-bit tie-breaks reorder rows; the rank column must explain that."""
        records = [
            record(score=18_480_000, bits=17.90, repeater="AAAAAA"),
            record(score=18_200_000, bits=18.02, repeater="BBBBBB"),
        ]
        lines = leaderboard_table(PLAIN, records)[1:]
        ranks = [float(line.split()[3]) for line in lines]
        self.assertEqual(ranks, sorted(ranks, reverse=True))
        rarities = [float(line.split()[2].rstrip("b")) for line in lines]
        self.assertNotEqual(rarities, sorted(rarities, reverse=True))

    def test_long_reasons_are_truncated_not_wrapped(self):
        long_reason = "x" * 200
        lines = leaderboard_table(PLAIN, [record(reason=long_reason)])
        self.assertTrue(all(len(line) < 160 for line in lines))

    def test_overflow_is_reported(self):
        records = [record(repeater=f"{i:06X}") for i in range(30)]
        self.assertIn("and 20 more", "\n".join(leaderboard_table(PLAIN, records, limit=10)))

    def test_footnote_explains_both_columns(self):
        text = ranking_footnote(PLAIN)
        self.assertIn("rarity", text)
        self.assertIn("rank", text)


if __name__ == "__main__":
    unittest.main()
