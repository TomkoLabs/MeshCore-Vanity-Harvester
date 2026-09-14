#!/usr/bin/env bash
#
# One-shot installer for MeshCore-Vanity-Harvester on Debian and derivatives.
#
#   ./install.sh          install, then print how to start
#   ./install.sh --run    install, then start harvesting immediately
#
# What it does, in order:
#   1. installs the handful of apt packages Python needs
#   2. creates a virtual environment and installs the harvester into it
#   3. looks for an NVIDIA GPU, and only if one is present does it set up Rust
#      and build the mc-keygen CUDA backend
#   4. verifies the result
#
# Rust is optional on purpose. Without a GPU the harvester runs fine on Python
# alone, so a plain Debian box never has to wait for a toolchain it cannot use.

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
VENV_DIR="$PROJECT_DIR/.venv"
MIN_RUST_VERSION="1.88"
RUN_AFTER_INSTALL=0
FORCE_CPU=0
ASSUME_YES=0

for argument in "$@"; do
  case "$argument" in
    --run) RUN_AFTER_INSTALL=1 ;;
    --cpu-only) FORCE_CPU=1 ;;
    --yes|-y) ASSUME_YES=1 ;;
    --help|-h)
      sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "unknown option: $argument (try --help)" >&2
      exit 2
      ;;
  esac
done

step()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
info()  { printf '    %s\n' "$*"; }
warn()  { printf '\033[33m    %s\033[0m\n' "$*"; }
fail()  { printf '\033[31merror: %s\033[0m\n' "$*" >&2; exit 1; }

# --- 0. sanity ------------------------------------------------------------

[ "$(uname -s)" = "Linux" ] || warn "This installer targets Debian Linux; continuing anyway."

info "project checkout: $PROJECT_DIR"
if command -v git >/dev/null 2>&1 && git -C "$PROJECT_DIR" rev-parse --is-inside-work-tree \
    >/dev/null 2>&1; then
  info "source revision: $(git -C "$PROJECT_DIR" describe --always --dirty --tags)"
fi

SUDO=""
if [ "$(id -u)" -ne 0 ]; then
  if command -v sudo >/dev/null 2>&1; then
    SUDO="sudo"
  else
    warn "Not root and sudo is unavailable; skipping apt package installation."
  fi
fi

apt_install() {
  local missing=()
  for package in "$@"; do
    dpkg -s "$package" >/dev/null 2>&1 || missing+=("$package")
  done
  if [ ${#missing[@]} -eq 0 ]; then
    info "system packages already present"
    return 0
  fi
  if ! command -v apt-get >/dev/null 2>&1; then
    warn "apt-get not found. Please install manually: ${missing[*]}"
    return 0
  fi
  if [ -z "$SUDO" ] && [ "$(id -u)" -ne 0 ]; then
    warn "Cannot install ${missing[*]} without root. Install them and re-run."
    return 0
  fi
  info "installing: ${missing[*]}"
  $SUDO apt-get update -qq
  DEBIAN_FRONTEND=noninteractive $SUDO apt-get install -y -qq "${missing[@]}"
}

# --- 0b. executable bits --------------------------------------------------

# Git records these, but files that arrive by download, copy or file share
# often do not carry them, and "./run.sh: Permission denied" after a successful
# install is a miserable first impression. Fix it here rather than explain it.
chmod +x "$PROJECT_DIR/run.sh" "${BASH_SOURCE[0]}" 2>/dev/null || true

# --- 1. system packages ---------------------------------------------------

step "Checking system packages"
apt_install python3 python3-venv python3-pip ca-certificates

command -v python3 >/dev/null 2>&1 || fail "python3 is not installed and could not be installed automatically."

PYTHON_OK=$(python3 - <<'PY'
import sys
print("yes" if sys.version_info >= (3, 10) else "no")
PY
)
[ "$PYTHON_OK" = "yes" ] || fail "Python 3.10 or newer is required (found $(python3 -V 2>&1))."
info "$(python3 -V 2>&1)"

# --- 2. Python environment ------------------------------------------------

step "Creating the Python environment"

# A virtual environment holds hard links to the interpreter that built it. When
# the system Python is upgraded those go stale, and every later command fails
# with a bare "No such file or directory". Check before trusting it.
venv_is_healthy() {
  [ -x "$VENV_DIR/bin/python" ] || return 1
  "$VENV_DIR/bin/python" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' \
    >/dev/null 2>&1 || return 1
  "$VENV_DIR/bin/python" -m pip --version >/dev/null 2>&1 || return 1
  return 0
}

if [ -d "$VENV_DIR" ] && ! venv_is_healthy; then
  warn "the existing environment is unusable (the system Python was probably upgraded)"
  info "rebuilding it from scratch; your results in data/ are untouched"
  rm -rf "$VENV_DIR"
fi

if [ ! -d "$VENV_DIR" ]; then
  if ! python3 -m venv "$VENV_DIR" 2>/tmp/venv-error.$$; then
    sed 's/^/      /' /tmp/venv-error.$$ >&2 || true
    rm -f /tmp/venv-error.$$
    fail "could not create a virtual environment in $VENV_DIR.
       On Debian this usually means the python3-venv package is missing:
           sudo apt-get install python3-venv"
  fi
  rm -f /tmp/venv-error.$$
  info "created $VENV_DIR"
else
  info "reusing $VENV_DIR"
fi

venv_is_healthy || fail "the virtual environment in $VENV_DIR is not usable even after rebuilding"

"$VENV_DIR/bin/python" -m pip install --upgrade pip --quiet \
  || warn "could not upgrade pip; continuing with the version that is installed"
"$VENV_DIR/bin/python" -m pip install --quiet -r "$PROJECT_DIR/requirements.txt" \
  || fail "could not install the Python dependencies (is this machine online?)"
"$VENV_DIR/bin/python" -m pip install --quiet --no-deps -e "$PROJECT_DIR" \
  || fail "could not install the harvester into $VENV_DIR"

INSTALLED_PROJECT_DIR="$(cd "$VENV_DIR" && "$VENV_DIR/bin/python" - <<'PY'
from pathlib import Path
import meshcore_vanity
print(Path(meshcore_vanity.__file__).resolve().parent.parent)
PY
)"
[ "$INSTALLED_PROJECT_DIR" = "$PROJECT_DIR" ] || fail "the environment imports MeshCore Vanity Harvester from
       $INSTALLED_PROJECT_DIR
    instead of this checkout:
       $PROJECT_DIR
    Remove a stale .venv symlink or renamed checkout, then run install.sh again."
info "harvester installed into the environment"

# --- 3. optional GPU backend ---------------------------------------------

GPU_PRESENT=0
if [ "$FORCE_CPU" -eq 0 ] && command -v nvidia-smi >/dev/null 2>&1; then
  if nvidia-smi --query-gpu=name --format=csv,noheader >/dev/null 2>&1; then
    GPU_PRESENT=1
  fi
fi

step "Checking for a GPU"
if [ "$FORCE_CPU" -eq 1 ]; then
  info "--cpu-only requested; skipping the mc-keygen build"
elif [ "$GPU_PRESENT" -eq 0 ]; then
  info "No NVIDIA GPU detected. The harvester will run CPU-only, which needs"
  info "no Rust toolchain and no further setup."
else
  info "NVIDIA GPU detected: $(nvidia-smi --query-gpu=name --format=csv,noheader | head -1)"

  if ! command -v nvcc >/dev/null 2>&1 && [ ! -d /usr/local/cuda ]; then
    warn "The CUDA toolkit was not found. The GPU backend needs it to build."
    warn "On Debian:  sudo apt-get install nvidia-cuda-toolkit"
    warn "Continuing CPU-only for now; re-run this script after installing it."
  else
    apt_install build-essential pkg-config curl git

    rust_version_ok() {
      command -v cargo >/dev/null 2>&1 || return 1
      local have
      have="$(cargo --version | awk '{print $2}')"
      [ "$(printf '%s\n%s\n' "$MIN_RUST_VERSION" "$have" | sort -V | head -1)" = "$MIN_RUST_VERSION" ]
    }

    if ! rust_version_ok; then
      # Debian stable ships a Rust older than mc-keygen requires, so use rustup.
      if [ "$ASSUME_YES" -eq 0 ] && [ -t 0 ]; then
        printf '    Rust >= %s is required to build the GPU backend. Install rustup now? [Y/n] ' "$MIN_RUST_VERSION"
        read -r reply
        case "$reply" in [nN]*) reply=no ;; *) reply=yes ;; esac
      else
        reply=yes
      fi
      if [ "$reply" = "yes" ]; then
        info "installing the Rust toolchain via rustup"
        curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
          | sh -s -- -y --profile minimal --default-toolchain stable >/dev/null
        # shellcheck disable=SC1091
        [ -f "$HOME/.cargo/env" ] && . "$HOME/.cargo/env"
      else
        warn "Skipping the GPU backend."
      fi
    fi

    if rust_version_ok; then
      info "building mc-keygen with CUDA support (this takes a few minutes)"
      if cargo build --release --features cuda --locked \
          --manifest-path "$PROJECT_DIR/mc-keygen/Cargo.toml"; then
        info "GPU backend built"
      else
        warn "The GPU build failed. The harvester will run CPU-only."
        warn "The CUDA feature is pinned to a toolkit version in mc-keygen/Cargo.toml;"
        warn "a different CUDA install may need that 'cudarc' feature changed."
      fi
    fi
  fi
fi

# --- 4. verify ------------------------------------------------------------

step "Verifying the installation"
(cd "$PROJECT_DIR" && "$VENV_DIR/bin/python" -m meshcore_vanity --self-test) \
  || fail "self-test failed"

DATA_DIR="$(cd "$PROJECT_DIR" && "$VENV_DIR/bin/python" -c \
  'from meshcore_vanity.config import default_data_directory; print(default_data_directory())')"

step "Ready"
cat <<MESSAGE
    Start harvesting:      ./run.sh
    Stop it:               press Ctrl+C (state is saved on exit)
    Results appear in:     $DATA_DIR

    Keep data/ private. Anyone holding a saved private key controls that
    MeshCore identity.
MESSAGE

if [ "$RUN_AFTER_INSTALL" -eq 1 ]; then
  step "Starting"
  exec "$PROJECT_DIR/run.sh"
fi
