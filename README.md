# MeshCore-Vanity-Harvester

Find, rank and safely store memorable MeshCore identities — keys whose public
key reads as something rather than nothing: `C0FFEEC0FFEE…`, `111111111111…`,
`514514514…`, `DEADBEEF…`.

It runs continuously on any Debian machine, uses an NVIDIA GPU when one is
available and falls back to CPU when it is not, and keeps five leaderboards of
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

## Which engines are running

You never have to guess. The first thing `./run.sh` prints is the answer:

```text
Engines
  CPU + GPU - both engines running on an NVIDIA GeForce RTX 3080
  CPU searches every pattern shape; the GPU hunts exact prefixes far faster.

  CPU   12 workers of 16 logical CPUs   every pattern shape, anywhere in the key
  GPU   mc-keygen, 1,418 exact prefixes    exact prefixes only, far faster
```

Both engines always run together when a GPU is present — they are good at
different things, and neither replaces the other. The CPU workers search
open-endedly (any run, word, period, sequence or palindrome, anywhere in the
key) at tens of thousands of keys per second per core. mc-keygen on a GPU
searches only for fixed prefixes, but does so hundreds of millions of keys per
second. On an RTX 3080 that is roughly 460M/s against 125k/s — so the GPU finds
the deep prefixes while the CPU finds the shapes no prefix list anticipated.

If the GPU is idle, the same block says so and says what to fix:

```text
Engines
  CPU only - an NVIDIA GeForce RTX 3080 is present but mc-keygen is not built
  The GPU is idle. Building the backend would search several thousand times faster.
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
| `leaderboards_public.json` | public | all five boards, no secrets |
| `private_keys_by_public_key.json` | **secret** | public key → private key |
| `leaderboards_private.json` | **secret** | all five boards, with key material |
| `leaderboard_history.private.jsonl` | **secret** | append-only recovery journal |
| `gpu_campaign_state.json` | public | resumable backend progress |

To use an identity: find it in `repeater_ids_public.jsonl`, take its
`public_key`, and look that up in `private_keys_by_public_key.json`.

The five boards are: one best key per six-character repeater ID; an overall
hall of fame; and one board each for word, single-run and periodic patterns, so
that one kind of pattern cannot crowd out the others.

## How ranking works

Every key is scored as

```
score = rarity_bits x 1,000,000 + aesthetic tie-breakers
```

with all tie-breakers capped below 950,000. **One extra bit of rarity always
beats every aesthetic bonus combined.**

A rarity bit is one halving of the probability that a random key looks at least
this good. The model asks one question of every pattern:

> How surprised should someone be by this key, if they did not know in advance
> what we were hunting for?

That framing forces a correction that is easy to miss. A pattern is only as
surprising as its *reference class* is small, so each one is charged for the
alternatives that would have pleased us just as much:

```
rarity_bits = 4 x constrained_nibbles
              - log2(equally acceptable alternative patterns)
              - log2(positions the pattern could have occupied)
```

Concretely, `C0FFEE` at the front of a key is **not** 24 bits of surprise. The
catalog holds many six-character words, and any of them would have been just as
pleasing. Charging for that is what makes a word comparable with a run, a
period, a sequence and a palindrome on a single axis.

For the same reason, curated preferences (Montreal `514`, all-`4` runs, iconic
hexspeak) buy **no** rarity at all. Wanting a pattern cannot make it
mathematically rarer, so a preference only ever contributes to the capped
aesthetic tie-breaker.

### Is it actually calibrated?

You can check, rather than take it on faith:

```bash
python scripts/calibrate.py --samples 400000
```

It scores hundreds of thousands of random keys and compares each family's claim
against how often that claim is actually reached. A gap near zero means the
family is honest; a large positive gap means it is optimistic and will unfairly
outrank the others.

| Family | Gap before | Gap now |
|---|---|---|
| word | **+5.5 bits** | −0.4 |
| periodic | +2.3 | +0.1 |
| palindrome | +1.4 | −1.6 |
| single run | +0.4 | +0.5 |
| sequence | −2.1 | −0.2 |

Cross-family spread fell from about **7.6 bits to about 2 bits**, and what
remains errs conservative. Before this, a "16-bit" word was roughly 40x more
common than a "16-bit" run while scoring the same — words were systematically
over-ranked against exactly the long runs you most want to find.

### Longer always wins

Each extra character in a run is worth 4 bits, and a bit is worth a million
points, so length dominates absolutely:

| leading `9`s | rarity | score |
|---|---|---|
| 37 | 144.00 bits | 144,950,000 |
| 14 | 52.00 bits | 52,876,524 |
| 9 | 32.00 bits | 32,730,116 |

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
words, generic periodic units (`ABABABAB`), ascending and descending hex
sequences, palindromes anywhere in the key, and the `AAABBB` / `AABBCC`
repeater-ID shapes.

Keys beginning `00` or `FF` are rejected — MeshCore reserves them.

## Command line

```text
./run.sh [OPTIONS]

--top [N]              show the saved leaderboards and exit (default 20)
--board WHICH          which board --top shows: hall, ids, word,
                       single_run, periodic, all       (default: hall)
--self-test            validate configuration and scoring, then exit
--no-gpu               do not use mc-keygen even if it is available
--cpu-workers N        number of generic CPU workers
--max-runtime SECONDS  stop and checkpoint after a bounded run
--data-dir PATH        store state elsewhere (or set MESHCORE_VANITY_DATA_DIR)
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
├── scoring.py       the calibrated rarity model and the fast pre-filter
├── leaderboards.py  records, ranking, board insertion, diversity
├── storage.py       atomic integrity-hashed persistence and recovery
├── engine_cpu.py    generic CPU discovery workers
├── engine_gpu.py    mc-keygen campaign driver
├── report.py        console presentation
└── cli.py           argument parsing and the supervisor loop
```

Two engines run side by side. The CPU workers search openly but slowly
(~28,000 keys/s per core); mc-keygen searches only for fixed prefixes but is
far faster, and on a GPU is faster by orders of magnitude. Each key mc-keygen
returns is re-derived here with an independent Ed25519 implementation before it
is allowed near the leaderboards, so a buggy or hostile backend cannot poison
the results.

Finds from either engine are announced as they happen, tagged with the engine
and their position on the hall of fame:

```text
  FOUND GPU   #1 EEEEEEEEEE6A09CDBC72A4113F0E5D8C7B1A2E93D4C60F8B5A7E2C91D3B4068F  36.00 bits  ·  'E' run of 10 characters  ·  471M/s
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

## Development

```bash
python -m unittest discover -s tests -v     # 177 tests
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
