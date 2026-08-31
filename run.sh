#!/usr/bin/env bash
#
# Start the harvester. Run ./install.sh first if you have not already.
# Any arguments are passed straight through, for example:
#   ./run.sh --node-id kraken
#   ./run.sh --merge /secure/incoming/gx10-leaderboards_private.json
#
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
VENV_PYTHON="$PROJECT_DIR/.venv/bin/python"

if [ ! -x "$VENV_PYTHON" ]; then
  echo "The harvester is not installed yet. Run ./install.sh first." >&2
  exit 1
fi

# The launcher can survive a Python upgrade that broke the environment under it,
# so check rather than let it fail with a bare "No such file or directory".
if ! "$VENV_PYTHON" -c '' >/dev/null 2>&1; then
  echo "The Python environment looks broken (was Python upgraded?)." >&2
  echo "Run ./install.sh again; it will rebuild it. Your results are untouched." >&2
  exit 1
fi

# Keep the Rust toolchain on PATH when rustup installed it for this user.
# shellcheck disable=SC1091
[ -f "$HOME/.cargo/env" ] && . "$HOME/.cargo/env"

# Run the module from this checkout, not a console script that an old editable
# environment may still point at in an adjacent or renamed checkout.
cd "$PROJECT_DIR"
exec "$VENV_PYTHON" -m meshcore_vanity "$@"
