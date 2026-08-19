# Changelog

## 8.0.0 - 2026-08-18

- Renamed the project to MeshCore-Vanity-Harvester.
- Expanded the unique-ID and overall leaderboards from 50 to 500 entries.
- Added independent top-500 word, single-run, and periodic-pattern boards.
- Limited the word catalog and GPU targets to English and French hexspeak.
- Rebalanced GPU scheduling toward long runs and repeated patterns.
- Made rarity dominance strict by capping all aesthetic bonuses below one bit.
- Fixed quick-filter false negatives for word repeats, extensions, compounds, periodic patterns, and palindromes.
- Added a compact public JSONL ID index and separate private-key lookup.
- Made sorting and equal-score cutoff behavior consistent.
- Added command-line configuration, tests, CI, packaging metadata, and security documentation.
