# MeshCore-Vanity-Harvester

Find, rank and safely store memorable MeshCore identities — keys whose public
key reads as something rather than nothing: `C0FFEEC0FFEE…`, `111111111111…`,
`514514514…`, `DEADBEEF…`.

It runs continuously on any Debian machine, uses an NVIDIA GPU when one is
available and falls back to CPU when it is not, and keeps six leaderboards of
the best identities it has found.

> **Your private keys are in `data/`.** Anyone who obtains one controls that
> MeshCore identity. The directory is forced to mode `0700`, secret files to
> `0600`, and it is excluded from Git. Never upload it anywhere.

## Install and run

Two commands. You do not need to know Python, Rust or CUDA.

```bash
git clone https://github.com/TomkoLabs/MeshCore-Vanity-Harvester
cd MeshCore-Vanity-Harvester && bash install.sh --run
```

(`bash install.sh` rather than `./install.sh` so it works even if the download
did not preserve the executable bit. The installer sets it for you.)

`install.sh` installs what is missing, sets up a private Python environment,
detects whether you have an NVIDIA GPU, and only then bothers with Rust and the
CUDA backend. On a machine without a GPU it skips all of that — the harvester
runs on Python alone.

Afterwards, one command starts it again:

```bash
./run.sh
```

It picks its own engines, resumes from every previous run, and needs no
arguments. Press `Ctrl+C` at any point — including during startup — and it
stops cleanly with your leaderboards intact. Start it again and it carries on
from where it was, keeping the cumulative key count and runtime.

To look at what you have found without starting a search:

```bash
./run.sh --top 20
```

<details>
<summary>Installer options</summary>

```bash
./install.sh            # install, then tell you how to start
./install.sh --run      # install and start immediately
./install.sh --cpu-only # skip the GPU backend even if a GPU is present
./install.sh --yes      # never prompt (for scripts and unattended installs)
```

If you would rather do it by hand, the installer is a readable shell script;
everything it does is a normal `apt`, `venv`, `pip` or `cargo` command.
</details>

The installer prints the physical checkout path and Git revision it is using.
If that path names an old, renamed or `_DELETE` directory, stop there: the
terminal's displayed path may be a symlink into a stale checkout. Confirm with
`pwd -P` and `git log -1 --oneline`, update the intended checkout, then run
`bash install.sh` again. `run.sh` executes `python -m meshcore_vanity` from its
own checkout so an old editable console script cannot silently select a nearby
copy.

## Which engines are running

You never have to guess. The first thing `./run.sh` prints is the answer:

```text
Engines
  CPU + GPU - both engines running on an NVIDIA GeForce RTX 3080
  CPU and GPU hunt desirable IDs and longer patterns from character zero.

  CPU   12 workers of 16 logical CPUs   desirable first six characters; longer prefix patterns
  GPU   mc-keygen, prefix-pattern harvesting    from character zero, up to 64 characters
```

Both engines run together when a GPU is present. Python workers discover
patterns with the normal board cutoff. Current mc-keygen builds continuously
screen leading runs, sequences, periods, palindromes, words, word tails and
arbitrary catalog-word chains. The **first six characters must already show a
recognizable pattern**; longer patterns count only from character zero. There
is **no 16-character target ceiling in prefix harvesting**.

Native harvesting starts at a 28-bit score floor to keep verification and
result traffic manageable; `--harvest-min-bits` adjusts it. The Python scorer
and independent key-pair verification decide what is saved. Native CPU builds
use the same filter, while older binaries fall back to exact-prefix campaigns.

The older RTX 3080 throughput figure of roughly 460M/s measured exact-prefix
search, not the current harvesting workload. Measure your machine with
`.venv/bin/python scripts/benchmark_harvest.py --seconds 60`; it emits statistics without
printing private keys. CPU-only benchmarking supports `--cpu --threads N`.

If the GPU is idle, the same block says so and says what to fix:

```text
Engines
  CPU only - an NVIDIA GeForce RTX 3080 is present but mc-keygen is not built
  The GPU is idle. Build the native backend to enable accelerated searching.
  → Run ./install.sh again; it will install what the CUDA build needs.
```

How many cores go to the search depends on whether a GPU is actually being
fed: one core is held back for the backend and one for the supervisor, so a
16-thread machine runs 14 workers with a GPU and 15 without. Workers run at a
raised nice value, so the machine stays usable. `--cpu-workers N` overrides it.

Giving mc-keygen its own CPU threads as well was measured and rejected: it
would contend with the Python workers for the same cores to add about 0.03%
throughput.

## What you get

Results land in `data/`:

| File | Visibility | What it is |
|---|---|---|
| `repeater_ids_public.jsonl` | public | one line per repeater ID — **start here** |
| `leaderboards_public.json` | public | all six boards, no secrets |
| `leaderboards_public.txt` | public | hall-of-fame public keys in rank order, one per line |
| `private_keys_by_public_key.json` | **secret** | public key → private key |
| `leaderboards_private.json` | **secret** | all six boards, with key material |
| `leaderboard_history.private.jsonl` | **secret** | append-only recovery journal |
| `gpu_campaign_state.json` | public | resumable backend progress |
| `before_score_11_*.private.json` | **private** | preserved snapshots from the previous scorer |
| `before_merge_*.private.json` | **private** | complete destination input preserved before merging |

To use an identity: find it in `repeater_ids_public.jsonl`, take its
`public_key`, and look that up in `private_keys_by_public_key.json`.

The six boards are: one best key per six-character repeater ID; an overall
hall of fame; and one board each for word, single-run, periodic and sequence patterns, so
that one kind of pattern cannot crowd out the others.

## How ranking works

The first six characters must show a desirable ID, and every scored feature
must start at character zero. Eligible keys are scored as

```
score = rarity_bits x 1,000,000 + aesthetic tie-breakers
```

with all tie-breakers capped below 950,000. **One extra bit of rarity always
beats every aesthetic bonus combined.**

A rarity bit represents an estimated halving of the probability of a
qualifying pattern. These family estimates are approximate. The model asks one question of every pattern:

> How surprised should someone be by this key, if they did not know in advance
> what we were hunting for?

That framing forces a correction that is easy to miss. A pattern is only as
surprising as its *reference class* is small, so each one is charged for the
alternatives that would have pleased us just as much:

```
rarity_bits = 4 x constrained_nibbles
              - log2(equally acceptable alternative patterns)
              - family correction

Only one start position is eligible: character zero.
```

Concretely, `C0FFEE` at the front of a key is **not** 24 bits of surprise. The
catalog holds many six-character words, and any of them would have been just as
pleasing. Charging for that is what makes a word comparable with a run, a
period, a sequence and a palindrome on a single axis.

Curated preferences (Montreal `514`, all-`4` runs, iconic hexspeak) add capped
style bonuses. When a curated exact prefix is itself the primary pattern, its
rarity is charged for the number of equally acceptable curated prefixes.

### Checking the rarity estimates

```bash
python scripts/calibrate.py --samples 400000
```

The script compares family claims with observed frequencies. In a 400,000-key
check of scoring revision 11, the deepest claims with at least 40 observations
had gaps of +0.23 bits for runs, −1.74 for words, and −1.67 for palindromes.
Positive means optimistic; negative means conservative. Other families were
too rare in that sample to assess. The older family corrections remain
conservative starting estimates after narrowing the search to visible IDs.
Long word-chain tails have not been independently empirically calibrated.
Use measured throughput and explicit target probabilities for waiting-time
estimates; the leaderboard score is not an exact combined-event probability.

### Longer always wins

Each extra character in a run is worth 4 bits, and a bit is worth a million
points, so length dominates absolutely:

| leading `9`s | estimated rarity |
|---|---|
| 37 | 144 bits |
| 14 | 52 bits |
| 9 | 32 bits |

Thirty-seven nines beats fourteen by 92 bits; fourteen beats nine by 20. No
combination of aesthetic preferences can reorder those, by construction.

### Long *and* unique

Rarity grows with length, so long patterns win on their own. Uniqueness needs
its own rule, because rarity alone would happily fill five hundred slots with
five hundred variations of one shape.

Each record therefore carries a canonical **pattern signature** — the repeated
word, the run digit, the periodic unit — and the hall of fame keeps at most
three entries sharing one signature (`max_per_signature` in
`meshcore_vanity/config.py`; set it to `0` to switch the rule off). A better key
still displaces the worst of its own shape, so nothing good is lost — the board
just stops being a list of near-duplicates. The run summary reports the
resulting diversity as a percentage of distinct shapes.

## Patterns it looks for

Hexspeak encodes English and French words using the hex letters plus
`G→6  I/L→1  O→0  S→5  T→7  Z→2`, giving `C0FFEE`, `DEADBEEF`, `FEEDFACE`,
`CAFEBABE`, `FACADE`, `EFFACEE`, `ACCE5` and friends.

Beyond the dictionary it recognises identical-character runs of any length,
repeated words (`ACEACEACEACE`), word extensions (`C0FFEEEEEE`), concatenated
words and compound-word tails (`C0FFEEBEEFFFFF`), generic periodic units
(`ABABABAB`), ascending and descending hex
sequences, anchored palindromes, and the `AAABBB` / `AABBCC` repeater-ID shapes.
The visible-ID gate accepts a catalog/curated prefix (including the visible
part of a longer word), four leading identical digits, five ordered digits,
`ABABAB`, `ABCABC`, `AAABBB`, `AABBCC`, or a six-digit palindrome. A random-looking
ID followed by a long tail earns nothing: `A73C91…222222222222` is rejected.
`DEADBEEFFFFF…`, `123456789ABC…`, and `222222222222…` remain targets.
A tail disconnected from a good ID also adds no score.

Keys beginning `00` or `FF` are rejected — MeshCore reserves them.

With `--prefix-campaigns`, the legacy GPU catalog covers lengths 9–16 by default, including every valid starting
digit for ascending and descending sequences. Campaigns rotate using persisted
attempt counts per prefix, with the longest affordable campaigns getting first
choice. The six-hour expected-time budget uses the backend's estimated rate;
the 15-minute slice rotates unfinished work. Finding one target no longer
resets the apparent history of its remaining batchmates.

Prefix harvesting is the default for new native builds and needs no campaign
catalog. Each thread starts from an independent OS-random scalar, retires after
its first hit, and is reseeded on the next launch. Bounded result-buffer
overflow replays the same work in smaller groups. A mandatory GPU/CPU startup
check validates multiple results, sign bits, attempt counts and overflow replay.

After updating this checkout, rebuild with `./install.sh` on the GPU machine,
then start with `./run.sh`. Saved state remains compatible. The harvester
reports whether prefix harvesting or legacy campaigns are actually active.

Each additional fixed hex character costs about **16×** as many attempts.
See [search odds and hardware considerations](docs/search-strategy.md) for
waiting-time examples, what the improvements change, and how to compare
Kraken, GX10 and other machines.

## Command line

```text
./run.sh [OPTIONS]

--top [N]              show the saved leaderboards and exit (default 20)
--board WHICH          which board --top shows: hall, ids, word,
                       single_run, periodic, sequence, all (default: hall)
--self-test            validate configuration and scoring, then exit
--no-gpu               do not use mc-keygen even if it is available
--prefix-campaigns     use the legacy exact-prefix scheduler
--harvest-min-bits N   native harvesting score floor (default 28)
--cpu-workers N        number of generic CPU workers
--max-runtime SECONDS  stop and checkpoint after a bounded run
--data-dir PATH        store state elsewhere (or set MESHCORE_VANITY_DATA_DIR)
--node-id NAME         stable compute-source name (default: hostname)
--merge SOURCE [...]   import current/legacy private files into --data-dir and exit
--no-colour            plain output (also honours NO_COLOR)
--version              print the Git revision of this checkout
```

### Reading the boards

```text
    #  repeater   rarity    rank  public key                reason
    1  666666     28.00b   28.71  66666666BD05F74C205181…   '6' run of 8 characters
    2  ACCE55     19.98b   20.60  ACCE558893DE2FFA572FF7…   word 'ACCE5' extends with 1 more '5'
```

`rarity` is the bits of surprise — each additional 4 bits means 16x rarer.
`rank` is what actually orders the board: rarity plus up to 0.95 of a bit of
pattern-quality preference. That cap is why rarity always wins a whole-bit
argument, and why two keys less than a bit apart can swap places.

## Versioning

There is no version number in these files. Releases are identified by their Git
commit, which `./run.sh --version` prints. A hand-maintained version constant
goes stale the moment sources are copied into another repository, so the commit
is the single source of truth.

The two numbers in `meshcore_vanity/__init__.py` are *data format* versions, not
project versions: `SCORE_VERSION` lets the program notice keys it scored under
an older model and rescore them, and `STATE_FORMAT_VERSION` marks the on-disk
layout. Both must keep being bumped when the thing they describe changes.

## How it is put together

```text
meshcore_vanity/
├── config.py        runtime configuration and paths (no mutable globals)
├── catalog.py       hexspeak word catalog and reference-class counts
├── keys.py          Ed25519 / MeshCore derivation and verification
├── scoring.py       the prefix rarity model and fast pre-filter
├── leaderboards.py  records, ranking, board insertion, diversity
├── storage.py       atomic integrity-hashed persistence and recovery
├── importing.py     generic legacy/current private-material discovery
├── engine_cpu.py    generic CPU discovery workers
├── engine_gpu.py    native harvesting and legacy campaign driver
├── harvest.py       native screening policy compiled from the scorer
├── report.py        console presentation
└── cli.py           argument parsing and the supervisor loop
```

Two engines run side by side. Python workers derive random-seed identities;
mc-keygen uses native CPU or CUDA chains with prefix-pattern screening.
Each accepted key mc-keygen returns is re-derived here with an independent Ed25519 implementation before it
is allowed near the leaderboards, so a buggy or hostile backend cannot poison
the results.

Finds from either engine are announced as they happen, tagged with the engine
and their position on the hall of fame:

```text
  FOUND GPU   #1 EEEEEEEEEE6A09CDBC72A4113F0E5D8C7B1A2E93D4C60F8B5A7E2C91D3B4068F  36.00 bits  ·  'E' run of 10 characters
  FOUND CPU   #4 5AFEEE0EEFEF724C3DA491B7E85C0D2F6A3B98E14C7D50F2A6B8E3C91D7405A6B  18.69 bits  ·  word '5AFE' extends with 2 more 'E'
```

A line is printed only for a key that actually earned a place — the top ten of
the overall hall of fame, or the top ten of its own pattern category. The vast
majority of keys examined never appear; they are scored and discarded. Ranking
against the category boards as well as the hall is what keeps the CPU visible:
once the GPU has filled the overall top ten with thirty-bit prefixes, a strong
CPU find in a different family still gets announced under its own board.

A cheap pre-filter stands in front of the full scorer and rejects about 99.95%
of keys. Its one hard contract is that it must never reject a key the full
scorer would have accepted; `tests/test_scoring.py` fuzzes that property across
the whole cutoff range.

## Running it as a service

For an unattended machine, `scripts/meshcore-vanity-harvester.service` is a
systemd unit with the sandboxing already set up:

```bash
sudo cp scripts/meshcore-vanity-harvester.service /etc/systemd/system/
sudo systemctl edit --full meshcore-vanity-harvester   # set User and paths
sudo systemctl enable --now meshcore-vanity-harvester
journalctl -u meshcore-vanity-harvester -f
```

Stopping the service sends SIGTERM, which the harvester treats exactly like
Ctrl+C: it checkpoints and exits cleanly.

## Stopping and resuming

Everything is designed around being interrupted. State is checkpointed every
30 seconds and immediately on any high-scoring find, written atomically with a
backup and a SHA-256 integrity field, so a kill at the wrong moment cannot
leave a half-written board.

On restart the saved boards are re-verified — each key is re-derived from its
seed rather than taken on trust — and then rescored under the current model
before the search resumes. A restore that will take a while says so as it goes.

```text
Resuming from the saved leaderboards.
  500 repeater IDs, 500 hall entries, 26B keys tried over 2d 17h
```

## Using multiple computers

Run one independent harvester per computer and periodically merge their private
snapshots. This is deliberately offline: there is no shared-file locking, no
SMB dependency, and no requirement that Kraken and GX10 can reach each other
while they search. Each machine keeps running at full speed if the other one is
off, busy, or isolated on another VLAN.

The hostname is used as the compute-source identity by default. Naming it
explicitly makes the setup self-documenting:

```bash
# On Kraken
./run.sh --node-id kraken

# On GX10
./run.sh --node-id gx10
```

When it is time to combine them, securely transfer GX10's
`data/leaderboards_private.json` to Kraken. Stop Kraken's harvester, then run:

```bash
./run.sh --node-id kraken --merge imports/gx10-leaderboards_private.json
```

The destination defaults to Kraken's normal `data/` directory. Its existing
private snapshot is included automatically, so the command above merges both
Kraken and GX10 in place. Sources may be private JSON files or complete data
directories, and any number can be supplied:

```bash
./run.sh --node-id kraken --merge imports/gx10.json imports/node3.json

# Or build a separate merged directory without touching the normal one:
./run.sh --node-id merge-hub --data-dir /secure/merged-data \
  --merge /secure/kraken-data /secure/gx10-data
```

The command accepts both current and old private files. It walks nested
objects and lists without depending on board names or fixed nesting levels,
then verifies and rescores every recovered identity before rebuilding the boards.
Supported inputs include:

- Current integrity-hashed snapshots and older hashed or unhashed JSON layouts.
- Public-key-to-private-key maps, including `private_keys_by_public_key.json`.
- JSON Lines history journals, plain expanded-key lists, and `set prv.key …` lines.
- Expanded MeshCore private keys, `seed || public` encodings, and labelled
  32-byte seeds (`seed`, `ed25519_seed`, `private_seed`, or private-key fields).
- Hex strings, optional `0x`/whitespace, and JSON byte arrays.

Unlabelled 64-byte values can be private-key candidates. **Unlabelled 32-byte
values are never assumed to be seeds**, because public keys and checksums have
that same length. A supplied public key must agree with the private material;
if it is missing, the importer derives it. Seedless imports without public keys
use the independent Python implementation and can take longer; progress is shown.

```bash
./run.sh --merge /secure/old-v7-private.json /secure/private_keys_by_public_key.json
./run.sh --merge /secure/leaderboard_history.private.jsonl /secure/expanded-keys.txt
```

Any integrity hash present must verify, including hashes inside JSON Lines
records. A corrupt hashed file is never silently treated as unhashed; a valid
backup may be used. Malformed JSON/lines abort the import without replacing the
destination snapshot. Explicit public snapshots and inputs without verifiable
private material are refused. Inputs are limited to 64 MiB each; supply larger
collections as multiple files.

Duplicate identities are combined before board limits are applied. Invalid
candidates and verified identities that no longer meet the visible-prefix rules
are reported separately. Source files remain untouched, and in-place
inputs are preserved as `before_merge_*.private.json` before replacement,
including unhashed files and identities retired by the current scorer.
The destination’s backup is included if its main snapshot is missing. All normal public and private
outputs are regenerated atomically. Stop any harvester using the destination
before merging.

Current per-node work counters are retained. Legacy files with aggregate
counters use stable source fingerprints to avoid counting identical copies
twice. Files without recognizable work metadata contribute keys without
inventing attempt totals; their combined statistics are marked approximate.

To give GX10 the combined baseline, securely copy Kraken's newly merged
`leaderboards_private.json` to a temporary path on GX10, stop GX10's harvester,
and merge it there:

```bash
./run.sh --node-id gx10 --merge /secure/incoming/merged-private.json
./run.sh --node-id gx10
```

GX10's current local snapshot is again included automatically, so finds made
after the transfer are not discarded. Per-node work counters travel with the
snapshot; future merges take the newest counter for each node instead of
double-counting the shared baseline. Use a unique `--node-id` for every
simultaneously running instance, especially if two instances share a hostname.

`leaderboards_private.json` contains every private identity key. Transfer it
only over a trusted encrypted channel or encrypted removable storage, keep
temporary copies mode `0600`, and remove them when the merge has been verified.

## Development

```bash
python -m unittest discover -s tests -v     # 191 tests
./run.sh --self-test
python scripts/calibrate.py --samples 200000
cargo test --manifest-path mc-keygen/Cargo.toml --locked   # 27 Rust tests
```

A bounded CPU-only run against a throwaway state directory:

```bash
./run.sh --no-gpu --cpu-workers 1 --max-runtime 5 --data-dir "$(mktemp -d)"
```

## About mc-keygen

`mc-keygen/` began as
[samschlegel/mc-keygen](https://github.com/samschlegel/mc-keygen) and is now
maintained as part of this project, cut down to what the harvester needs. Its
dependency graph went from 348 crates to 58: the interactive terminal UI, the
benchmarking harness, the Metal backend and a build-time Git version stamp that
nothing read are all gone.

The one thing kept untouched is `cuda/vanity_kernel.cu` — upstream's
hand-written Ed25519 field arithmetic, which is why GPU search is worth doing
at all and is not the sort of code to rewrite for fun.

Absorbing it also bought something the harvester could not have had otherwise:
`mc-keygen verify-pairs`. Keys found on the GPU are made by advancing a scalar
rather than hashing a seed, so they carry no seed and have to be re-derived by
scalar multiplication on every startup — about 95 ms each in Python. Handing
the batch to Rust turned a **52 second** startup into **0.6**.

See [mc-keygen/ATTRIBUTION.md](mc-keygen/ATTRIBUTION.md) for the full record of
what changed, and [THIRD_PARTY.md](THIRD_PARTY.md) for licensing.

## Security

Read [SECURITY.md](SECURITY.md) before publishing output, moving a state
directory, or running this under another account.

## License

MIT. The vendored `mc-keygen` is dual-licensed MIT or Apache-2.0; its original
license files are retained in that directory.
