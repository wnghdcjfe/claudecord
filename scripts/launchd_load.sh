#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON_BIN="$ROOT_DIR/.venv/bin/python"
PLIST_PATH="$HOME/Library/LaunchAgents/com.discord-claude-assistant.plist"

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "가상환경을 먼저 준비하세요: scripts/setup.sh" >&2
  exit 1
fi

mkdir -p "$(dirname "$PLIST_PATH")"

python - "$ROOT_DIR" "$PYTHON_BIN" "$PLIST_PATH" "${PATH:-}" <<'PY'
import plistlib
import sys
from pathlib import Path

root_dir = Path(sys.argv[1])
python_bin = Path(sys.argv[2])
plist_path = Path(sys.argv[3])
path_value = sys.argv[4] or "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

plist = {
    "Label": "com.discord-claude-assistant",
    "ProgramArguments": [str(python_bin), "-m", "src.main"],
    "WorkingDirectory": str(root_dir),
    "RunAtLoad": True,
    "KeepAlive": True,
    "StandardOutPath": "/tmp/discord-claude-assistant.out.log",
    "StandardErrorPath": "/tmp/discord-claude-assistant.err.log",
    "EnvironmentVariables": {
        "PATH": path_value,
    },
}

with plist_path.open("wb") as f:
    plistlib.dump(plist, f)
PY

launchctl unload "$PLIST_PATH" 2>/dev/null || true
launchctl load "$PLIST_PATH"
echo "loaded $PLIST_PATH"
