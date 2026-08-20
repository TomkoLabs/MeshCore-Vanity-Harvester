#!/usr/bin/env python3
"""A stand-in for mc-keygen, so the backend path can be tested without a GPU.

It speaks the same command line and prints the same JSON, but searches on the
CPU for a short prefix. Set FAKE_MC_KEYGEN_MODE to change how it behaves:

    gpu      pretend to be a CUDA build (default)
    cpu      pretend to be a CPU-only build, which rejects --gpu-only
    fail     exit non-zero, to exercise failure handling
    garbage  print something that is not a result
"""

from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.environ.get("FAKE_MC_KEYGEN_IMPORT_PATH", ""))

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

MODE = os.environ.get("FAKE_MC_KEYGEN_MODE", "gpu")

HELP_GPU = """mc-keygen
Options:
  -t, --threads <N>
      --json
      --cpu-only
      --gpu-only
      --verify
"""
HELP_CPU = """mc-keygen
Options:
  -t, --threads <N>
      --json
"""


def public_key(seed: bytes) -> str:
    return Ed25519PrivateKey.from_private_bytes(seed).public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    ).hex().upper()


def main() -> int:
    args = sys.argv[1:]
    if "--help" in args:
        sys.stdout.write(HELP_GPU if MODE != "cpu" else HELP_CPU)
        return 0
    if MODE == "cpu" and "--gpu-only" in args:
        sys.stderr.write("error: unexpected argument '--gpu-only'\n")
        return 2
    if "--verify" in args:
        return 0
    if MODE == "fail":
        sys.stderr.write("simulated backend failure\n")
        return 1
    if MODE == "garbage":
        sys.stdout.write("this is not json\n")
        return 0

    # Options that take a value must not have that value mistaken for a
    # prefix. The real binary uses clap; this has to do it by hand.
    valued = {"-t", "--threads"}
    prefixes = []
    skip = False
    for argument in args:
        if skip:
            skip = False
            continue
        if argument in valued:
            skip = True
            continue
        if argument.startswith("-"):
            continue
        prefixes.append(argument)
    if not prefixes:
        sys.stderr.write("no prefix given\n")
        return 2
    started = time.monotonic()
    attempts = 0
    while True:
        seed = os.urandom(32)
        attempts += 1
        candidate = public_key(seed)
        if candidate.startswith(("00", "FF")):
            continue
        for prefix in prefixes:
            if candidate.startswith(prefix):
                json.dump({
                    "public_key": candidate,
                    "seed": seed.hex(),
                    "matched_prefix": prefix,
                    "attempts": attempts,
                    "elapsed_secs": max(1e-6, time.monotonic() - started),
                }, sys.stdout)
                sys.stdout.write("\n")
                return 0


if __name__ == "__main__":
    raise SystemExit(main())
