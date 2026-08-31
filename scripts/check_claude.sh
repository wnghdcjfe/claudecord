#!/usr/bin/env bash
set -euo pipefail

# check_claude.ps1과 동일한 순서로 실행 파일을 찾는다: .env의 CLAUDE_BIN ->
# PATH -> 실패 시 에러. CLAUDE_BIN을 설정한 사용자가 이 스크립트로 진단할 때
# 봇이 실제로 쓰는 것과 다른 바이너리를 검사하지 않도록 한다 (#24).

_trim() {
  local var="$1"
  var="${var#"${var%%[![:space:]]*}"}"
  var="${var%"${var##*[![:space:]]}"}"
  printf '%s' "$var"
}

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="$ROOT_DIR/.env"

if [[ -f "$ENV_FILE" ]]; then
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%$'\r'}"
    trimmed="$(_trim "$line")"
    case "$trimmed" in
      ''|'#'*) continue ;;
    esac
    [[ "$trimmed" == *"="* ]] || continue
    name="$(_trim "${trimmed%%=*}")"
    value="$(_trim "${trimmed#*=}")"
    value="${value%\"}"; value="${value#\"}"
    value="${value%\'}"; value="${value#\'}"
    [[ -n "$name" ]] && export "$name=$value"
  done < "$ENV_FILE"
fi

if [[ -n "${CLAUDE_BIN:-}" ]]; then
  claude_bin="$CLAUDE_BIN"
else
  claude_bin="$(command -v claude || true)"
  if [[ -z "$claude_bin" ]]; then
    echo "claude CLI를 찾을 수 없습니다. PATH를 확인하거나 .env에 CLAUDE_BIN을 설정하세요." >&2
    exit 1
  fi
fi

"$claude_bin" --version
"$claude_bin" -p "ping" --setting-sources local --output-format json
