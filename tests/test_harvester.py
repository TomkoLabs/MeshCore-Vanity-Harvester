"""Compatibility surface.

The harvester moved from a single module into the ``meshcore_vanity`` package.
Existing scripts, service units and habits still invoke
``python meshcore_vanity_harvester.py``, so that path is kept working and
tested rather than merely promised.

The substantive tests now live in ``test_scoring.py``, ``test_leaderboards.py``
and ``test_engine_gpu.py``.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SHIM = PROJECT_ROOT / "meshcore_vanity_harvester.py"


def run(*arguments: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SHIM), *arguments],
        cwd=str(PROJECT_ROOT), capture_output=True, text=True, timeout=300, check=False,
    )


class CompatibilityEntryPointTests(unittest.TestCase):
    def test_the_shim_still_exists(self):
        self.assertTrue(SHIM.is_file())

    def test_the_shim_runs_the_self_test(self):
        completed = run("--self-test")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("Self-test passed", completed.stdout)

    def test_the_shim_reports_a_version(self):
        completed = run("--version")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue(completed.stdout.strip())

    def test_a_bounded_run_completes_and_writes_state(self):
        with tempfile.TemporaryDirectory() as directory:
            completed = run("--no-gpu", "--cpu-workers", "1", "--max-runtime", "3",
                            "--data-dir", directory)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertTrue((Path(directory) / "leaderboards_public.json").is_file())

    def test_invalid_arguments_are_rejected(self):
        self.assertEqual(run("--cpu-workers", "0").returncode, 2)
        self.assertEqual(run("--max-runtime", "-1").returncode, 2)


class PackageEntryPointTests(unittest.TestCase):
    def test_python_dash_m_works(self):
        completed = subprocess.run(
            [sys.executable, "-m", "meshcore_vanity", "--self-test"],
            cwd=str(PROJECT_ROOT), capture_output=True, text=True, timeout=300, check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)


if __name__ == "__main__":
    unittest.main()
