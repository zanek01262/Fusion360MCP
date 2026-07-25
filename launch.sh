#!/usr/bin/env bash
# launch.sh — macOS/Linux wrapper for the Fusion 360 MCP preflight helper.
#
#   ./launch.sh          # full preflight check (default)
#   ./launch.sh ping     # just ping the Fusion listener
#   ./launch.sh serve    # run the server standalone (debugging)
#
# Make executable once:  chmod +x launch.sh

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Prefer python3, fall back to python.
if command -v python3 >/dev/null 2>&1; then
  PY=python3
elif command -v python >/dev/null 2>&1; then
  PY=python
else
  echo "FAIL: no python found on PATH. Install Python 3.10+." >&2
  exit 1
fi

exec "$PY" "$HERE/launch.py" "${1:-check}"
