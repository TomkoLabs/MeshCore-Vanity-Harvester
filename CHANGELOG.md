# Changelog

Entries are dated, not numbered. Releases are identified by their Git commit —
`./run.sh --version` prints the one you are running — because a version string
kept in a file goes stale the moment these sources are copied elsewhere.

`SCORE_VERSION` and `STATE_FORMAT_VERSION` in `meshcore_vanity/__init__.py` are
*data format* versions, not project versions. They exist so the program can
recognise and migrate its own older output, and they must keep being bumped.

## 2026-08-29 (offline multi-node aggregation)

- Added `--merge SOURCE [...]` for combining any number of independently
  harvested private snapshots. Inputs can be a data directory or its
  `leaderboards_private.json`; an existing destination snapshot is included
  automatically for safe in-place updates.
- Every imported key pair is independently re-derived and rescored before all
  five boards are rebuilt from the union. Integrity failures, public-only
  snapshots, empty inputs and concurrent writes to the destination are refused.
- Added stable per-node compute provenance (`--node-id`, defaulting to the
  hostname). Repeated merge-and-redistribute cycles take the latest cumulative
  counters per node, so the shared baseline is not counted again on every merge.
- A merge regenerates every normal output atomically, including the concise
  `leaderboards_public.txt`, while private key files retain mode `0600`.
- Chose offline aggregation over a shared SMB state file: harvesters remain
  independent during network outages, do not need cross-host locking, and work
  naturally across an isolated VLAN.
- `install.sh` now prints and verifies its physical checkout and Git revision;
  `run.sh` invokes the module from that checkout directly. A stale editable
  console script or a symlink into a renamed `_DELETE` checkout can no longer
  silently select code from the wrong project copy.

## 2026-08-20 (console reporting)

- **CPU finds stopped being announced once the hall of fame filled up.** A
  find was printed only if it ranked in the overall top ten, so on a machine
  where the GPU had already parked thirty-bit prefixes at the top, a perfectly
  good twenty-bit CPU find scrolled past in silence — the CPU looked idle
  while the system monitor said otherwise. A find is now announced if it ranks
  in the top ten of the overall hall **or** of its own pattern category, so
  each family reports its own best work. The status line also carries a
  running count of what each engine has found.
- **The FOUND line shows the whole public key**, not the first sixteen
  characters. The point of the line is to see the pattern you matched; a
  pattern that runs past the truncation point was invisible.
- **`install.sh` now sets its own executable bit** (and `run.sh`'s) before
  doing anything else, so a checkout that arrived without the permission bit —
  a zip download, a copy across filesystems — no longer fails with
  `Permission denied`. The README leads with `bash install.sh --run`, which
  works regardless.
- A restore that finishes quickly no longer prints a lone progress line
  before "done".

## 2026-08-19 (production audit)

Reviewed against a battery of adversarial probes: corrupted, truncated,
empty and wrong-shaped state files; missing, hanging, failing and
garbage-producing backends; interrupts at every phase; unusual paths;
concurrent instances; and long-run growth. Everything held except the two
below.

- **A virtual environment broken by a system Python upgrade is now repaired
  rather than reused.** `install.sh` saw the directory, reused it, and died on
  `.venv/bin/python: No such file or directory` — a bare shell error with no
  hint at the cause. It now checks the environment actually works and rebuilds
  it if not, saying so. `run.sh` makes the same check and names the fix.
- A failed `python3 -m venv` now names the Debian package that provides it,
  and a failed dependency install says so rather than continuing.
- Added tests for the shell entry points, which nothing covered, and a CI job
  that installs from a clean checkout, runs, installs again, breaks the
  environment the way an upgrade would, and proves both scripts recover.
- Verified: state survives interruption with its integrity hash intact; a
  corrupt snapshot always falls back rather than crashing; secret files stay
  at mode 0600 through every path; a second instance is refused; the campaign
  bookkeeping is bounded; and the harvester keeps running through every
  backend failure mode rather than dying with it.

## 2026-08-19 (backend reporting)

- **A CUDA-capable build is no longer mistaken for a GPU.** The backend status
  was read off the binary's compiled capability and never asked whether a card
  was present, so a machine with a CUDA build and no GPU was told "CPU + GPU -
  both engines running" moments after the installer said "No NVIDIA GPU
  detected". Worse than misleading: the engine then passed `--gpu-only` to a
  binary that exits without a device, which read as three campaign failures and
  disabled the backend on a machine that should simply have used the processor.
  GPU mode now requires both the capability and a device; without one, the
  backend runs on the CPU and says so.
- The device is probed once per run rather than once per status line, and
  `--verify` is skipped when there is no GPU kernel to cross-check.
- Backend tests tell a stale build apart from a broken one: a binary older than
  its sources is a reason to rebuild, not a failure.
- The stub backend repairs its own executable bit, which file transfer does not
  always preserve, and parses valued options rather than mistaking `--threads N`
  for a prefix.

## 2026-08-19 (production readiness)

- **More cores go to the search.** Cores are now held back only for work that
  exists: one for feeding the backend and one for the supervisor. A 16-thread
  machine runs 14 workers with a GPU and 15 without, against 12 before — the
  old rule reserved a quarter of the machine whether or not there was a GPU to
  reserve it for. `--cpu-workers N` still overrides.
- **The history journal is now bounded.** It grew forever at roughly 1.5 KiB
  per find; one previous generation is kept and recovery reads both.
- **Snapshot backups are taken periodically rather than on every checkpoint**,
  halving steady-state disk traffic. The write itself is already atomic and
  integrity-hashed, and the journal is a third line of defence, so copying the
  previous file aside every 30 seconds insured a risk already covered.
- **A failed checkpoint no longer ends the run.** A full or unwritable disk is
  reported and retried; the boards are in memory and the next attempt may
  succeed. Dying over one lost checkpoint was the worse outcome.
- Added `scripts/meshcore-vanity-harvester.service`, a sandboxed systemd unit.
  Stopping the service sends SIGTERM, which checkpoints and exits cleanly.
- Recorded in THIRD_PARTY.md why mc-keygen should stay an upstream subtree
  rather than being absorbed, with the dependency measurements behind it.

## 2026-08-19 (later) — startup, interruption and find reporting

- **Startup no longer stalls for minutes on a populated set of boards.** Every
  saved record was re-derived through the from-scratch scalar multiplication in
  `keys.py` — about 95 ms each, and the same key appears on up to five boards.
  A full set meant roughly two minutes of silent work before the first line of
  output. Records are now verified once per distinct key, and via the saved seed
  (OpenSSL, ~2,200x faster) whenever one is present. The independent
  implementation still runs for records that carry no seed, which is the case it
  was written for. Measured: 106s to 0.64s on a full set.
- A restore that genuinely will be slow now reports progress instead of looking
  like a hang.
- **Ctrl+C during startup printed a stack trace.** Interrupt handlers are now
  installed before the restore, so both SIGINT and SIGTERM exit cleanly with
  "Stopped before harvesting began. Nothing was changed." This also fixes a
  process started in the background, which inherits SIGINT as *ignored* and
  previously could not be stopped by signal at all — relevant to anyone running
  this under `nohup` or a service manager.
- **CPU finds are now announced too**, not just backend matches. Each line is
  tagged `FOUND CPU` or `FOUND GPU` and carries its hall-of-fame position.
  CPU announcements are rate limited because the boards fill in a burst at
  startup; a new number one always gets through.
- A key with no pattern at all can no longer occupy a board slot, whichever
  engine produced it.
- Added an end-to-end backend test that runs a stub mc-keygen as a real
  subprocess, so capability detection, result parsing, independent
  re-verification, progress persistence, failure handling and prompt shutdown
  are all covered without needing a GPU.

## 2026-08-19 — calibrated ranking, package split, one-command install

### Ranking

- **Recalibrated the rarity model so pattern families are comparable.** Every
  pattern is now charged for its reference class: the number of equally
  acceptable alternatives, and the number of positions it could have occupied.
  Words were previously credited about 5.5 bits more than they deserved
  relative to single runs — a "16-bit" word was roughly 40x more common than a
  "16-bit" run while scoring the same. Cross-family spread fell from about
  7.6 bits to about 2, and what remains errs conservative.
- Curated preferences (Montreal 514, all-4 runs, iconic hexspeak) no longer buy
  rarity. Wanting a pattern cannot make it mathematically rarer, so preferences
  now contribute only to the capped aesthetic tie-breaker.
- Palindrome rarity counts expansion centres (2n-1) rather than start positions;
  sequence rarity accounts for leading digits that cannot start a run of that
  length.
- Added `scripts/calibrate.py` to measure each family's claim against its
  observed frequency, and `scripts/check_calibration.py` to fail CI on drift.
- Saved keys scored under an older model are rescored on load and the boards
  rebuilt, so old and new finds rank consistently.

### Uniqueness

- Records carry a canonical pattern signature, and the hall of fame keeps at
  most `max_per_signature` entries sharing one shape. A better key still
  displaces the worst of its own shape, so quality is preserved while the board
  stops filling with near-duplicates. Board diversity is reported at exit.

### Fixes

- The pre-filter floored its threshold at 8 bits, so whenever the cutoff was
  below about 9 bits it rejected keys the full scorer would have accepted —
  measured at a 12.5% miss rate in that range. The threshold is now derived
  exactly, with epsilon slack for score rounding, and fuzzed at every cutoff.
- State snapshots were read by fixed section name, so a v7-era file
  (`repeater_id_top50`) was silently ignored — losing every saved key and the
  attempt totals behind it. Records are now found structurally.
- The mc-keygen backend idled forever once every affordable target was found.
  The difficulty budget now escalates instead.
- Targets of 11 characters and longer could never be scheduled at the default
  rate, leaving roughly 460 of 787 catalog entries unreachable.
- Campaign selection skips targets that could not beat the current cutoff.
- A mc-keygen build without a GPU feature rejects `--gpu-only` outright.
  Capabilities are probed once and the command line adapts.
- The secondary-bonus loop let overlapping views of one pattern stack; it now
  dedupes on kind, offset and signature.

### Console and operation

- The startup report now states plainly which engines are running — "CPU + GPU
  - both engines running on an NVIDIA GeForce RTX 3080" — and when the GPU is
  idle it says why and what to do about it.
- Added `--top N [--board ...]` to browse the saved leaderboards without
  starting a search, and `--version` to print the Git revision.
- Human-readable counts and durations (26B keys over 2d 17h), a clearer status
  line, a ranked summary at exit, and colour that respects `NO_COLOR`.
- Added `install.sh` and `run.sh`: a two-command install on Debian that detects
  a GPU and only sets up Rust and CUDA when one is present. Rust is now an
  optional, GPU-only dependency.
- Added `scripts/vendor-mc-keygen.sh` to import and update mc-keygen as a git
  subtree, keeping upstream provenance while `git clone` stays self-contained.

### Structure

- Split the 2,520-line module into a `meshcore_vanity` package with one concern
  per file. `meshcore_vanity_harvester.py` remains as a compatibility shim.
- Replaced roughly thirty mutable module globals with frozen `Config`, `Paths`,
  `CpuConfig`, `GpuConfig` and `BoardConfig` dataclasses, passed explicitly.
  Spawned workers receive their settings rather than re-deriving defaults.
- Merged three near-identical board-insertion functions and two near-identical
  atomic-write functions into one each; dropped the legacy v4/v5/v6 import paths.
- Removed the project version constant. Test suite grew from 9 tests to 98.
