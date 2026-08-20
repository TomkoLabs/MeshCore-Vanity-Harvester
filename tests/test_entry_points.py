"""The shell entry points.

`install.sh` and `run.sh` are the whole interface for anyone who does not read
Python, so their failure messages matter as much as their success paths. These
tests cover the parts that can be checked in a second; CI additionally runs a
real installation twice over to prove it is idempotent and self-healing.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
INSTALL = PROJECT_ROOT / "install.sh"
RUN = PROJECT_ROOT / "run.sh"


def sh(script: Path, *args: str, cwd: Path = None, timeout: int = 60):
    return subprocess.run(
        ["bash", str(script), *args],
        cwd=str(cwd or PROJECT_ROOT), capture_output=True, text=True, timeout=timeout,
    )


class ScriptShapeTests(unittest.TestCase):
    def test_both_scripts_exist_and_declare_an_interpreter(self):
        for script in (INSTALL, RUN):
            self.assertTrue(script.is_file(), f"{script.name} is missing")
            self.assertTrue(script.read_text().startswith("#!"), f"{script.name} has no shebang")

    def test_both_scripts_fail_loudly_rather_than_continuing(self):
        """Without `set -e` a failed step would be followed by more steps."""
        for script in (INSTALL, RUN):
            self.assertIn("set -e", script.read_text(), f"{script.name} does not stop on error")

    def test_install_help_works(self):
        result = sh(INSTALL, "--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("install", result.stdout.lower())

    def test_install_rejects_an_unknown_option(self):
        result = sh(INSTALL, "--wat")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unknown option", result.stderr.lower())


class LauncherFailureMessageTests(unittest.TestCase):
    """run.sh is what people type. Its errors have to be actionable."""

    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())
        shutil.copy(RUN, self.directory / "run.sh")

    def tearDown(self):
        shutil.rmtree(self.directory, ignore_errors=True)

    def test_missing_installation_points_at_the_installer(self):
        result = sh(self.directory / "run.sh", cwd=self.directory)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("install.sh", result.stderr)

    def test_a_broken_environment_is_diagnosed_not_dumped(self):
        """A Python upgrade leaves the venv's interpreter links dangling.

        The bare failure is "No such file or directory", which tells nobody
        anything. It has to name the cause and the fix.
        """
        venv_bin = self.directory / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        launcher = venv_bin / "meshcore-vanity-harvester"
        launcher.write_text("#!/bin/sh\necho should not run\n")
        launcher.chmod(0o755)
        # An interpreter that is present but unusable, as after an upgrade.
        broken = venv_bin / "python"
        broken.write_text("#!/nonexistent/python3\n")
        broken.chmod(0o755)

        result = sh(self.directory / "run.sh", cwd=self.directory)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("broken", result.stderr.lower())
        self.assertIn("install.sh", result.stderr)
        self.assertIn("untouched", result.stderr.lower(),
                      "the message should reassure that saved results survive")


class InstallerSelfHealingTests(unittest.TestCase):
    """The installer must repair an environment, not trip over it."""

    def test_it_checks_the_environment_before_reusing_it(self):
        text = INSTALL.read_text()
        self.assertIn("venv_is_healthy", text,
                      "install.sh reuses .venv without checking that it works")
        self.assertIn("rm -rf \"$VENV_DIR\"", text,
                      "install.sh has no way to rebuild a broken environment")

    def test_it_names_the_package_needed_when_venv_creation_fails(self):
        self.assertIn("python3-venv", INSTALL.read_text(),
                      "a failed venv creation should name the missing Debian package")

    def test_rust_is_not_required_without_a_gpu(self):
        """Rust is an optional, GPU-only dependency; the installer must say so."""
        text = INSTALL.read_text()
        self.assertIn("No NVIDIA GPU detected", text)
        self.assertIn("no Rust toolchain", text)

    def test_missing_apt_is_a_warning_not_a_failure(self):
        """Fedora, Arch and Alpine have no apt; the installer still has work to do."""
        text = INSTALL.read_text()
        self.assertIn("apt-get not found", text)
        marker = text.index("apt-get not found")
        self.assertIn("return 0", text[marker:marker + 200],
                      "a missing apt-get should not abort the installation")


if __name__ == "__main__":
    unittest.main()


class ExecutableBitTests(unittest.TestCase):
    """"Permission denied" after a successful install is a miserable welcome.

    Git records the executable bit, but a download, copy or file share often
    does not carry it. The installer repairs it rather than leaving the user to
    work out what `./run.sh: Permission denied` means.
    """

    def test_the_installer_repairs_the_executable_bits(self):
        text = INSTALL.read_text()
        self.assertIn("chmod +x", text, "install.sh never sets the executable bits")
        self.assertIn("run.sh", text.split("chmod +x")[1][:120],
                      "install.sh does not make run.sh executable")

    def test_it_repairs_them_before_doing_any_work(self):
        text = INSTALL.read_text()
        self.assertLess(text.index("chmod +x"), text.index("Creating the Python environment"),
                        "the bits should be fixed before anything can fail")

    def test_the_readme_installs_without_needing_the_bit(self):
        """`./install.sh` cannot run on a file that arrived without +x."""
        readme = (PROJECT_ROOT / "README.md").read_text()
        install_block = readme[readme.index("## Install and run"):][:600]
        self.assertIn("bash install.sh", install_block,
                      "the documented install command depends on the executable bit")
