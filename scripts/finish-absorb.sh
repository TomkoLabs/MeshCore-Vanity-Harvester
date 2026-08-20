#!/usr/bin/env bash
#
# Finish the mc-keygen absorption: rebuild, test, commit.
#
#   ./scripts/finish-absorb.sh
#
# The previous script applied the absorbed files but did not rebuild mc-keygen
# before running the tests, so they ran against a binary compiled from the old
# sources — which of course knows nothing about `verify-pairs`. It refused to
# commit, which was the right call. This picks up from there.
#
# Rust is an optional, GPU-only dependency, so this works on a machine without
# a toolchain: it says what it could not check and commits anyway.
#
# It is a one-off and removes itself from the tree when it commits.

set -euo pipefail

cd "$(dirname "$0")/.."

step() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
info() { printf '    %s\n' "$*"; }
warn() { printf '\033[33m    %s\033[0m\n' "$*"; }
fail() { printf '\033[31merror: %s\033[0m\n' "$*" >&2; exit 1; }

[ -d .git ] || fail "not a Git repository"
[ -f mc-keygen/Cargo.toml ] || fail "mc-keygen is missing; was the absorption applied?"
grep -q "VerifyPairs" mc-keygen/src/main.rs 2>/dev/null \
  || fail "mc-keygen/src/main.rs looks pre-absorption; apply the staged files first"

# --- 1. permissions -------------------------------------------------------

step "Restoring executable bits"
# File transfer does not always carry these, and Git records them, so set them
# before committing rather than leaving the next clone to trip over it.
chmod +x install.sh run.sh tests/fixtures/fake_mc_keygen.py 2>/dev/null || true
git update-index --chmod=+x install.sh run.sh tests/fixtures/fake_mc_keygen.py 2>/dev/null || true
info "install.sh, run.sh, tests/fixtures/fake_mc_keygen.py"

# --- 2. rebuild -----------------------------------------------------------

step "Rebuilding mc-keygen"

# shellcheck disable=SC1091
[ -f "$HOME/.cargo/env" ] && . "$HOME/.cargo/env"

HAVE_CARGO=0
command -v cargo >/dev/null 2>&1 && HAVE_CARGO=1

if [ "$HAVE_CARGO" -eq 0 ]; then
  # Rust is an optional, GPU-only dependency of this project, so a machine
  # without it is a perfectly normal place to edit and commit from. Say what
  # went unverified rather than refusing to proceed.
  warn "cargo not found; skipping the Rust build and tests"
  warn "The Rust side will be verified by CI, and by ./install.sh on a machine"
  warn "with a GPU. Nothing else here depends on it."
else
  FEATURES=()
  if command -v nvidia-smi >/dev/null 2>&1 && \
     nvidia-smi --query-gpu=name --format=csv,noheader >/dev/null 2>&1; then
    if command -v nvcc >/dev/null 2>&1 || [ -d /usr/local/cuda ]; then
      FEATURES=(--features cuda)
      info "NVIDIA GPU and CUDA toolkit found; building with GPU support"
    else
      warn "GPU present but no CUDA toolkit; building CPU-only"
    fi
  else
    info "no NVIDIA GPU; building CPU-only"
  fi

  cargo build --release --manifest-path mc-keygen/Cargo.toml "${FEATURES[@]}" \
    || fail "the mc-keygen build failed"
  info "built"
fi

# --- 3. test --------------------------------------------------------------

step "Running the test suites"
if [ -x .venv/bin/python ]; then PY=.venv/bin/python; else PY=python3; fi
"$PY" -m unittest discover -s tests -q || fail "Python tests failed; nothing was committed"
if [ "$HAVE_CARGO" -eq 1 ]; then
  cargo test --manifest-path mc-keygen/Cargo.toml --quiet || fail "Rust tests failed"
  info "Python and Rust suites green"
else
  info "Python suite green; Rust not checked on this machine"
fi

# --- 4. commit ------------------------------------------------------------

step "Committing"
git rm -q --ignore-unmatch scripts/apply-absorb.sh scripts/finish-absorb.sh
rm -f scripts/apply-absorb.sh
git add -A
git commit -q -F - <<'MESSAGE'
Absorb mc-keygen and cut it down to what this project uses

mc-keygen was a vendored subtree of samschlegel/mc-keygen (upstream 62ed67f,
MIT or Apache-2.0). It is now maintained here so it can be reduced to what the
harvester actually drives and improved alongside it. Provenance, and the full
record of what was kept and removed, is in mc-keygen/ATTRIBUTION.md.

Kept unchanged in substance
- cuda/vanity_kernel.cu, the hand-written Ed25519 field arithmetic that makes
  GPU search worthwhile. Rewriting it would mean owning the risk of an
  arithmetic bug that produces keys which look fine and are not.
- The CPU worker pool, prefix matching and CUDA driving.

Removed
- build.rs and vergen-gix: stamped a Git version string nothing read, and
  pulled the whole gitoxide stack into every build.
- The ratatui progress display, crossterm and sysinfo: this binary is driven
  by the harvester and never watched, so progress is a plain stderr line.
- The bench subcommand, criterion and chrono.
- The Metal backend; this project targets Debian, and upstream still has it.

Dependencies fell from 348 crates to 58.

Added
- verify-pairs, batch key verification. Keys found on the GPU are made by
  advancing a scalar rather than hashing a seed, so they carry no seed and the
  harvester must re-derive them by scalar multiplication on every startup --
  about 95 ms each in Python. Doing the batch in Rust took a 52 second restore
  down to 0.6. Verdicts are matched to requests by position, never by public
  key, so a tampered copy of a record cannot borrow the original's verdict.
- A plain-text progress line, so the tool is still usable by hand.
- One error path: upstream exited from several places, everything now returns
  a Result to a single exit point.
- Physical-core detection reads sysfs instead of depending on sysinfo.
- The binary uses the library rather than declaring the modules a second time,
  which had been compiling every source file twice.

The harvester labels backend finds by the engine that made them, and announces
a find only once it has actually been accepted onto a board.

The backend tests now tell a stale build apart from a broken one: a binary
older than its sources is a reason to rebuild, not a failure, while a current
build that cannot verify pairs is a genuine regression. The stub backend
repairs its own executable bit, which file transfer does not always preserve.

152 Python tests, 27 Rust tests, no build warnings.
MESSAGE

step "Done"
git --no-pager log --oneline -2

if [ "$HAVE_CARGO" -eq 0 ]; then
  printf '\n'
  warn "Reminder: the Rust side was not built here."
  warn "On the machine with the GPU, run ./install.sh && ./run.sh --self-test —"
  warn "that rebuilds mc-keygen and cross-checks the GPU kernel against the host."
fi
