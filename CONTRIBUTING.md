# Contributing

Contributions should preserve three invariants:

1. Generated private material never enters Git, logs, public snapshots, or test fixtures.
2. A pattern with one additional rarity bit always outranks aesthetic bonuses.
3. The quick candidate filter may return false positives but must not reject a key that can meet the active cutoff under the full scorer.

Before submitting a change, run:

```bash
python -m unittest discover -s tests -v
python meshcore_vanity_harvester.py --self-test
cargo test --manifest-path mc-keygen/Cargo.toml --locked
```

Scoring changes must include regression tests with concrete public-key patterns. Persistence changes must remain backward-compatible with the existing private snapshot and history formats or include an explicit migration path.
