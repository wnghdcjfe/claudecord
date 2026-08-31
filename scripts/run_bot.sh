#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON_BIN="$ROOT_DIR/.venv/bin/python"

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "가상환경을 먼저 준비하세요: scripts/setup.sh" >&2
  exit 1
fi

cd "$ROOT_DIR"
"$PYTHON_BIN" -m src.main
