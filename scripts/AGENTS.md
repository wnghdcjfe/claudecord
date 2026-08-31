<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-08-31 | Updated: 2026-08-31 -->

# scripts

## Purpose
셋업·실행·상시구동·진단 스크립트. macOS/Linux용 `.sh`와 Windows용 `.ps1`이 짝을 이루며, 같은 이름의 두 파일은 같은 일을 하도록 유지한다. 봇을 로그인 시 자동 기동하는 데몬으로 등록하는 스크립트도 여기 있다.

## Key Files

| File | Description |
|------|-------------|
| `setup.sh` / `setup.ps1` | `.venv` 생성 후 `pip install -e .`. Windows판은 `py -3` 런처를 우선 시도 |
| `run_bot.sh` / `run_bot.ps1` | 봇 포그라운드 실행. `.venv`의 파이썬(`.sh`는 `.venv/bin/python`, `.ps1`은 `.venv\Scripts\python.exe`)으로 `python -m src.main`. `.venv`가 없으면 `scripts/setup`을 안내하며 중단 |
| `check_claude.sh` / `check_claude.ps1` | Claude CLI 연결 진단. `--version` 후 `-p "ping"`으로 왕복 확인. 둘 다 `.env`를 직접 파싱해 `CLAUDE_BIN`을 존중하고, 없으면 PATH에서 찾는다 |
| `launchd_load.sh` | macOS LaunchAgent 등록. 인라인 파이썬으로 plist를 만들어 `~/Library/LaunchAgents/`에 쓰고 `launchctl load`. `RunAtLoad` + `KeepAlive`로 상시 구동 |
| `register_scheduled_task.ps1` | Windows 작업 스케줄러 등록. 로그온 시 기동, 3회 재시작, 로그는 `%LOCALAPPDATA%\discord-claude-assistant\logs\bot.log` |
| `unregister_scheduled_task.ps1` | 위 작업 해제 |

## Subdirectories
없음.

## For AI Agents

### Working In This Directory
- **플랫폼 짝을 맞춰라.** `.sh`의 동작을 바꾸면 대응하는 `.ps1`도 같이 고쳐야 한다. 한쪽만 고치면 조용히 갈라진다. `run_bot.sh`/`check_claude.sh`가 각각 `.venv` 요구와 `CLAUDE_BIN` 우선순위에서 `.ps1`과 갈라져 있던 것을 #24에서 맞췄다 — 다시 갈라뜨리지 말 것.
- 셸 스크립트는 `set -euo pipefail`, PowerShell은 `$ErrorActionPreference = "Stop"`으로 시작한다. 새 스크립트도 동일하게.
- 경로는 스크립트 위치 기준으로 계산한다 (`$(dirname "$0")/..`, `Split-Path -Parent $PSScriptRoot`). 호출자의 cwd에 의존하지 말 것.
- 에러 메시지는 한국어, 성공 로그는 영어 소문자 한 줄이라는 관행이 있다.

### Testing Requirements
자동화된 테스트가 없다. 변경 시 해당 플랫폼에서 직접 실행해 확인해야 한다:
- `bash -n script.sh` 로 최소한 문법은 검사할 수 있다.
- 데몬 등록 스크립트(`launchd_load.sh`, `register_scheduled_task.ps1`)는 시스템 상태를 바꾸므로 실행 전 내용을 반드시 읽을 것.

### Common Patterns
- 실행 파일 탐색: 환경변수(`CLAUDE_BIN`) → PATH 탐색 → 실패 시 에러 순서.
- `.venv` 부재 시 `scripts/setup` 을 안내하며 중단.
- PowerShell 쪽은 문자열 주입을 막기 위해 경로를 단일 인용 리터럴로 이스케이프한다 (`ConvertTo-PowerShellSingleQuotedLiteral`).

## Dependencies

### Internal
- `src.main` — 모든 실행 스크립트의 진입점
- 프로젝트 루트 `.env`, `.venv`, `pyproject.toml`

### External
- `python` / `py` 런처, `pip`
- `launchctl` (macOS), `Register-ScheduledTask` (Windows)
- `claude` CLI — 진단 스크립트 대상

<!-- MANUAL: -->
