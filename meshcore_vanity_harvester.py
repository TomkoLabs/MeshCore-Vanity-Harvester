#!/usr/bin/env python3
"""Compatibility entry point.

The harvester now lives in the ``meshcore_vanity`` package. This shim keeps
``python meshcore_vanity_harvester.py`` working for existing scripts, service
units and muscle memory.
"""

from __future__ import annotations

import sys

from meshcore_vanity.cli import main

if __name__ == "__main__":
    sys.exit(main())
