# MeshCore-Vanity-Harvester

Continuously generate, validate, rank, and safely persist MeshCore vanity identities. The harvester combines generic CPU discovery with optional NVIDIA CUDA prefix campaigns and ranks patterns across the full 64-character Ed25519 public key.

> **Private-key warning:** anyone who obtains a saved private key can use that MeshCore identity. The `data/` directory is intentionally excluded from Git and is forced to mode `0700`; private files are written as `0600`.

## Highlights

- English and French hexspeak dictionary with accent normalization.
- First-class ranking for long single-character runs, repeated words, generic periodic patterns, compounds, sequences, palindromes, and structured repeater IDs.
- Strict rarity-first scoring: one additional rarity bit always beats every aesthetic bonus combined.
- Five complementary top-500 views:
  - one best identity per six-character repeater ID;
  - unrestricted overall hall of fame;
  - repeated/dictionary word patterns;
  - single-character runs;
  - other periodic patterns.
- Atomic, integrity-hashed checkpoints with backups and an append-only recovery journal.
- Human-scannable JSON Lines public index and a separate private-key lookup keyed by full public key.
- Defensive verification of all CPU- and GPU-produced Ed25519 key material.
- Graceful shutdown, process locking, worker health checks, and resumable GPU campaign state.

## Requirements

- Linux or macOS with Python 3.10+
- `cryptography`
- Rust 1.85+ to build the bundled `mc-keygen`
- Optional NVIDIA CUDA 12.4 runtime/driver for the CUDA backend
- Optional Metal support on macOS

The Python harvester can run CPU-only without Rust or a GPU binary.

## Quick start

```bash
git clone <your-repository-url> MeshCore-Vanity-Harvester
cd MeshCore-Vanity-Harvester

python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install --no-deps -e .

meshcore-vanity-harvester --self-test
meshcore-vanity-harvester --no-gpu
```

Stop with `Ctrl+C`. The harvester checkpoints before exiting.

### Build the CUDA backend

```bash
cargo build --manifest-path mc-keygen/Cargo.toml --release --features cuda
python meshcore_vanity_harvester.py
```

The startup verification runs a short GPU/CPU equivalence check before any campaign. If CUDA is unavailable or verification fails, the GPU engine is disabled and CPU harvesting continues.

For Apple Metal:

```bash
cargo build --manifest-path mc-keygen/Cargo.toml --release --features metal
```

## Command line

```text
python meshcore_vanity_harvester.py [OPTIONS]

--self-test              Validate configuration, cryptography, and scoring, then exit
--no-gpu                 Disable the mc-keygen GPU backend
--cpu-workers N          Set the number of generic CPU workers
--max-runtime SECONDS    Stop and checkpoint after a bounded run
--data-dir PATH          Store state in a different directory
--version                Print the version
```

`MESHCORE_VANITY_DATA_DIR` is the environment-variable equivalent of `--data-dir`. A source checkout defaults to `./data`. An installed module defaults to `$XDG_STATE_HOME/meshcore-vanity-harvester`, or `~/.local/state/meshcore-vanity-harvester` when `XDG_STATE_HOME` is unset.

## What it searches for

The curated word catalog encodes English and French words using hexadecimal letters plus these substitutions:

```text
G → 6    I/L → 1    O → 0    S → 5    T → 7    Z → 2
```

Examples include `C0FFEE`, `DEADBEEF`, `FEEDFACE`, `CAFEBABE`, `DEC0DE`, `FACADE`, `EFFACEE`, and `ACCE5`.

The full scorer also recognizes:

- arbitrarily long identical-character runs such as `11111111111111111`;
- repeated known words such as `ACEACEACEACEACEACE`;
- generic periodic units such as `ABABABABABAB`;
- leading word extensions such as `C0FFEEEEEEEE`;
- concatenated dictionary words;
- ascending and descending hexadecimal sequences;
- palindromes anywhere in the key;
- `AAABBB`, `AABBCC`, `ABABAB`, and `ABCABC` repeater IDs;
- Montreal `514` patterns and selected exact preferences.

Public keys beginning with `00` or `FF` are rejected because MeshCore reserves them.

## Ranking model

The dominant term is estimated rarity:

```text
score = round(rarity_bits × 1,000,000) + aesthetic_tiebreakers
```

All aesthetic and secondary bonuses together are capped at `950,000`, so one extra rarity bit always wins.

- A fixed exact prefix of length `L` contributes `4L` bits.
- A generic single-character run contributes `4(L - 1)` bits because its first character is free.
- A generic repeated unit contributes `4(matched_length - unit_length)` bits.
- Generic sequences and patterns found away from the prefix receive appropriate direction/location penalties.

This means a sufficiently longer single-character run naturally outranks a shorter word repeat. For example, seventeen leading `1` characters rank above five repetitions of `BAD`. Independent family boards prevent words or runs from monopolizing every useful list.

## Output files

All files live in the configured data directory.

| File | Visibility | Purpose |
|---|---|---|
| `leaderboards_public.json` | Public | Full top-500 boards without secret material |
| `repeater_ids_public.jsonl` | Public | One compact repeater-ID record per line |
| `leaderboards_private.json` | Secret | Full boards with private keys and seeds |
| `private_keys_by_public_key.json` | Secret | Direct full-public-key → private-key mapping |
| `leaderboard_history.private.jsonl` | Secret | Append-only recovery journal |
| `gpu_campaign_state.json` | Public | Resumable GPU scheduling statistics |

Every `repeater_ids_public.jsonl` line contains both the six-character repeater ID and full public key. Use that full public key to retrieve the corresponding secret entry from `private_keys_by_public_key.json`.

Public files are mode `0644`; secret files are `0600`. JSON snapshots contain SHA-256 integrity fields and are replaced atomically with backups.

## Development

```bash
python -m unittest discover -s tests -v
python meshcore_vanity_harvester.py --self-test
cargo test --manifest-path mc-keygen/Cargo.toml --locked
```

Run a bounded CPU-only smoke test without touching normal state:

```bash
tmp_dir="$(mktemp -d)"
python meshcore_vanity_harvester.py \
  --no-gpu --cpu-workers 1 --max-runtime 3 --data-dir "$tmp_dir"
```

## Repository layout

```text
.
├── meshcore_vanity_harvester.py  # Harvester, scorer, persistence, scheduler
├── mc-keygen/                    # Vendored Rust CPU/CUDA/Metal key generator
├── tests/                        # Python regression tests
├── data/README.md                # Runtime-data safety notes
├── pyproject.toml                # Python package metadata and console entry point
└── .github/workflows/ci.yml      # Python and Rust CI
```

The bundled `mc-keygen` is derived from the upstream project identified in [THIRD_PARTY.md](THIRD_PARTY.md) and retains its original licenses.

## Security

Read [SECURITY.md](SECURITY.md) before publishing output, moving a state directory, or running the service under another account. Never commit or upload `data/`, even to a private repository unless you explicitly accept the key-exposure risk.

## License

The harvester is available under the MIT License. The vendored `mc-keygen` component is dual-licensed under MIT or Apache-2.0; see its included license files and [THIRD_PARTY.md](THIRD_PARTY.md).
