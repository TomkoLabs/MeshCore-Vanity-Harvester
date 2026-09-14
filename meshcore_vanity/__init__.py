"""MeshCore vanity identity harvester.

Generate, validate, rank and safely persist MeshCore Ed25519 vanity identities.

Public entry point::

    from meshcore_vanity.cli import main

The package is split so each concern can be read and tested on its own:

``config``       runtime configuration and file paths (no module-level mutation)
``catalog``      hexspeak word catalog and curated preference lists
``keys``         Ed25519 / MeshCore key derivation and verification
``scoring``      the calibrated rarity model and the fast pre-filter
``leaderboards`` record construction, ranking and board insertion
``storage``      atomic, integrity-hashed persistence and recovery
``engine_cpu``   generic CPU discovery workers
``engine_gpu``   mc-keygen exact-prefix campaign driver
``report``       console presentation
``cli``          argument parsing and the supervisor loop

There is deliberately no project version number anywhere in these files. Releases
are identified by their Git commit, which cannot drift out of date the way a
hand-maintained constant does. The two version numbers below are *data format*
versions, not project versions: they exist so the program can recognise its own
older output and migrate it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional

__all__ = ["SCORE_VERSION", "STATE_FORMAT_VERSION", "project_revision"]

# Bump when analyze_public_key() can return a different score for the same key.
# Saved records carrying an older value are rescored on load.
SCORE_VERSION = 11

# Bump when the on-disk layout changes in a way readers must know about.
STATE_FORMAT_VERSION = 8


def project_revision() -> Optional[str]:
    """Short Git description of this checkout, or None outside a repository."""
    root = Path(__file__).resolve().parent.parent
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "describe", "--always", "--dirty", "--tags"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    revision = completed.stdout.strip()
    return revision or None
