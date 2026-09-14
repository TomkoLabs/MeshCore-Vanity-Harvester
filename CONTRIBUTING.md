# Contributing

Changes should preserve four invariants. Each has a test that will catch you if
you break it; please keep those tests honest rather than relaxing them.

1. **Generated private material never leaves the data directory.** Not into
   Git, logs, public snapshots or test fixtures.
   Guarded by `PersistenceTests.test_public_snapshot_contains_no_secrets`.

2. **One extra rarity bit always outranks every aesthetic bonus combined.**
   Guarded by `RarityModelTests.test_one_rarity_bit_outranks_every_aesthetic_bonus`.

3. **Rarity is comparable across pattern families.** A family may not claim
   more surprise than it actually delivers, or it will outrank the others for
   free. Every pattern must be charged for its reference class — the number of
   equally acceptable alternatives and the positions it could have occupied.
   Guarded by `scripts/check_calibration.py` in CI.

4. **The pre-filter may return false positives, but never a false negative.**
   It must not reject a key the full scorer would have accepted at the active
   cutoff. A false negative hides a leaderboard-worthy key permanently and
   silently.
   Guarded by `QuickFilterTests`, which fuzzes the property across the whole
   cutoff range.

Before submitting:

```bash
python -m unittest discover -s tests -v
python -m meshcore_vanity --self-test
python scripts/check_calibration.py
shellcheck install.sh run.sh
cargo test --manifest-path mc-keygen/Cargo.toml --locked
```

## Changing the scoring model

Any change that can alter a score for the same key must bump `SCORE_VERSION` in
`meshcore_vanity/__init__.py`. Saved records carrying an older value are
rescored on load, so old and new finds stay consistently ranked.

Re-fit the per-family constants afterwards and show your work:

```bash
python scripts/calibrate.py --samples 400000
```

Include regression tests using concrete public-key patterns, not just
synthetic scores.

## Changing persistence

Stay backward-compatible with the existing private snapshot and history
formats, or add an explicit migration path and bump `STATE_FORMAT_VERSION`.

## Changing mc-keygen

`mc-keygen/` is ours now, so edit it freely — with one exception.

`cuda/vanity_kernel.cu` is upstream's hand-written Ed25519 field arithmetic,
kept unchanged deliberately. A mistake in it produces keys that look perfectly
fine and are not, and nothing in the test suite would catch that. If it has to
change, `mc-keygen --verify` cross-checks the GPU against the host
implementation and must pass on real hardware before the change goes anywhere.

Record anything material in `mc-keygen/ATTRIBUTION.md`, which is what tells the
next reader which parts came from upstream and which are ours.

Rust changes must keep the build clean:

```bash
cargo test --manifest-path mc-keygen/Cargo.toml --locked
cargo build --release --manifest-path mc-keygen/Cargo.toml --features cuda
```
