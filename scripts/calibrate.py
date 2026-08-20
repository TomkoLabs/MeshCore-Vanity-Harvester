#!/usr/bin/env python3
"""Measure how well the rarity model predicts reality.

Rarity claims are quantised (a run of 6 and a run of 7 differ by 4 bits with
nothing between), so comparing against fixed thresholds is misleading. Instead,
for every claim value a family actually produces, this compares that claim
against the observed frequency of random keys reaching it.

  gap > 0  the family is optimistic and will unfairly outrank the others
  gap < 0  the family is conservative and will be slightly under-ranked
"""
from __future__ import annotations

import argparse
import collections
import math
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from meshcore_vanity.scoring import analyze_public_key  # noqa: E402

HEX = "0123456789ABCDEF"
MIN_HITS = 40


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=200_000)
    parser.add_argument("--seed", type=int, default=20260819)
    parser.add_argument("--min-claim", type=float, default=6.0)
    args = parser.parse_args()

    rng = random.Random(args.seed)
    per_family = collections.defaultdict(list)
    n = 0
    for _ in range(args.samples):
        key = "".join(rng.choice(HEX) for _ in range(64))
        if key.startswith(("00", "FF")):
            continue
        n += 1
        a = analyze_public_key(key)
        if a["pattern_kind"] != "none":
            per_family[a["pattern_family"]].append(a["rarity_bits"])

    print(f"samples: {n}   (a family's claim is compared against how often it is reached)\n")
    print(f"{'family':<12}{'claim':>8}{'hits':>7}{'observed P':>13}{'true bits':>11}{'gap':>8}")
    print("-" * 60)
    worst = 0.0
    summary = {}
    for family in sorted(per_family):
        values = sorted(per_family[family], reverse=True)
        claims = sorted({round(v * 4) / 4 for v in values if v >= args.min_claim})
        shown = []
        for claim in claims:
            hits = sum(1 for v in values if v >= claim - 1e-9)
            if hits < MIN_HITS:
                continue
            frac = hits / n
            gap = claim + math.log2(frac)
            shown.append((claim, hits, frac, gap))
        for claim, hits, frac, gap in shown[-4:]:
            print(f"{family:<12}{claim:>8.2f}{hits:>7}{frac:>13.6f}{-math.log2(frac):>11.2f}{gap:>+8.2f}")
        if shown:
            deepest = shown[-1]
            summary[family] = deepest[3]
            worst = max(worst, abs(deepest[3]))
        print()

    print("Deepest measurable claim per family (this is the number that matters):")
    for family, gap in sorted(summary.items(), key=lambda kv: -abs(kv[1])):
        verdict = "optimistic" if gap > 0.5 else ("conservative" if gap < -0.5 else "calibrated")
        print(f"  {family:<12}{gap:>+7.2f} bits   {verdict}")
    spread = max(summary.values()) - min(summary.values()) if summary else 0.0
    print(f"\ncross-family spread: {spread:.2f} bits   (0 = perfectly comparable)")
    print(f"worst single gap:    {worst:.2f} bits")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
