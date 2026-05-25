#!/usr/bin/env bash
set -euo pipefail

claude --version
claude -p "ping" --setting-sources local --output-format json
