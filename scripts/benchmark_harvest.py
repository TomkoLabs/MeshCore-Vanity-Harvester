#!/usr/bin/env python3
"""Benchmark the actual prefix-pattern search without printing private keys."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from meshcore_vanity.config import GpuConfig  # noqa: E402
from meshcore_vanity.engine_gpu import detect_capabilities, find_binary, has_nvidia_gpu  # noqa: E402
from meshcore_vanity.harvest import build_policy  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=float, default=60.0)
    parser.add_argument("--bits", type=float, default=28.0)
    parser.add_argument("--cpu", action="store_true", help="benchmark native CPU harvesting even if CUDA is available")
    parser.add_argument("--threads", type=int, default=1, help="native CPU workers (default 1)")
    args = parser.parse_args()
    if not 0 < args.seconds <= 86400 or not 16 <= args.bits <= 256 or not 1 <= args.threads <= 1024:
        parser.error("invalid duration, bits or thread count")
    binary = find_binary(GpuConfig(), allow_build=False)
    if binary is None or not detect_capabilities(binary).get("harvest"):
        parser.error("rebuild mc-keygen with ./install.sh to enable prefix-pattern harvesting")
    gpu = not args.cpu and has_nvidia_gpu()
    if gpu and not detect_capabilities(binary).get("gpu"):
        parser.error("GPU detected but this binary is CPU-only; rebuild with CUDA or specify --cpu")
    with tempfile.TemporaryDirectory(prefix="meshcore-benchmark-") as directory:
        policy = Path(directory) / "policy.json"
        policy.write_text(json.dumps(build_policy(int(args.bits * 1_000_000))), encoding="ascii")
        command = [str(binary), "harvest", "--policy", str(policy), "--seconds", str(args.seconds), "--benchmark"]
        command += ["--gpu-only"] if gpu else ["--threads", str(args.threads)]
        print(f"Benchmark: {'GPU' if gpu else 'CPU'}, {args.bits:g}-bit screening, {args.seconds:g}s", flush=True)
        return subprocess.call(command, env=os.environ.copy())


if __name__ == "__main__":
    raise SystemExit(main())
