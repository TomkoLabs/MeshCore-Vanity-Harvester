#!/usr/bin/env python3
"""CI guard: fail if any pattern family drifts far from honest rarity.

A family whose claim is optimistic will outrank the others for free, which is
exactly the bug this release fixed. Small samples are noisy, so the tolerance is
deliberately loose — this catches regressions, not rounding.
"""
from __future__ import annotations

import collections
import math
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from meshcore_vanity.scoring import analyze_public_key  # noqa: E402

SAMPLES = int(os.environ.get("CALIBRATION_SAMPLES", "60000"))
MAX_OPTIMISTIC_BITS = 2.5
MIN_HITS = 30
HEX = "0123456789ABCDEF"


def main() -> int:
    rng = random.Random(11)
    per_family = collections.defaultdict(list)
    n = 0
    for _ in range(SAMPLES):
        key = "".join(rng.choice(HEX) for _ in range(64))
        if key.startswith(("00", "FF")):
            continue
        n += 1
        analysis = analyze_public_key(key)
        if analysis["pattern_kind"] != "none":
            per_family[analysis["pattern_family"]].append(analysis["rarity_bits"])

    failures = []
    for family, values in sorted(per_family.items()):
        claims = sorted({round(v * 4) / 4 for v in values if v >= 6.0})
        worst = None
        for claim in claims:
            hits = sum(1 for v in values if v >= claim - 1e-9)
            if hits < MIN_HITS:
                continue
            gap = claim + math.log2(hits / n)
            worst = gap if worst is None else max(worst, gap)
        if worst is None:
            print(f"  {family:<12} not enough samples to judge")
            continue
        status = "ok" if worst <= MAX_OPTIMISTIC_BITS else "TOO OPTIMISTIC"
        print(f"  {family:<12} worst gap {worst:+.2f} bits   {status}")
        if worst > MAX_OPTIMISTIC_BITS:
            failures.append((family, worst))

    if failures:
        print("\nCalibration regression: " + ", ".join(f"{f} at {g:+.2f} bits" for f, g in failures))
        print("Re-fit FAMILY_CALIBRATION_BITS with scripts/calibrate.py.")
        return 1
    print("\nAll families within tolerance.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
