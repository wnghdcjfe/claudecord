<!-- Generated: 2026-08-31 | Updated: 2026-08-31 -->

# claudecord (discord-claude-assistant)

## Purpose
Discord 봇으로 로컬 PC의 Claude Code CLI를 원격 조종하는 파이썬 애플리케이션. 화이트리스트로 인증된 사용자가 Discord 메시지를 보내면, 봇이 `claude -p` 서브프로세스를 스트리밍 모드로 실행하고 그 산출물(`output.md`, `manifest.json`에 등재된 파일)을 다시 Discord 채널로 첨부해 돌려준다. 클라우드 호스팅 없이 Discord Gateway의 outbound WebSocket만으로 동작한다.

## Key Files

| File | Description |
|------|-------------|
| `pyproject.toml` | 패키지 메타데이터. `requires-python >=3.11`, 런타임 의존성은 `discord.py>=2.4`와 `python-dotenv>=1.0` 두 개뿐. 콘솔 스크립트 `discord-claude-assistant = src.main:main` 등록 |
| `.env.example` | 필수 환경변수 템플릿 (`DISCORD_BOT_TOKEN`, `OWNER_DISCORD_ID`, `ALLOWED_CHANNEL_IDS`, `PROJECT_ROOT`, `RUNS_DIR`) |
| `README.md` | 한국어 사용자 문서. Discord 앱 생성 → 권한 설정 → ID 확보 → `.env` 작성 → 실행 순서 |
| `working_m.gif` | 작업 중 상태 메시지에 첨부되는 애니메이션 (192KB). `src/status.py`가 참조하는 유일한 GIF |
| `logo.png`, `example.jpeg` | README용 이미지 |

## Subdirectories

| Directory | Purpose |
|-----------|---------|
| `src/` | 봇 애플리케이션 전체 소스 (see `src/AGENTS.md`) |
| `tests/` | `unittest` 기반 테스트 스위트, 374개 테스트 (see `tests/AGENTS.md`) |
| `scripts/` | macOS(bash)/Windows(PowerShell) 셋업·실행·상시구동 스크립트 (see `scripts/AGENTS.md`) |
| `examples/` | Claude CLI `stream-json` 이벤트 샘플 (see `examples/AGENTS.md`) |
| `.omc/` | oh-my-claudecode 런타임 상태. 애플리케이션과 무관하며 커밋 대상이 아님 |

## For AI Agents

### Working In This Directory
- 진입점은 `src/main.py`이고, 실행은 `python -m src.main` 또는 `scripts/run_bot.sh`다. 이 저장소에는 `bot.py`도 `requirements.txt`도 없다 (README가 그 둘을 안내하던 것은 이슈 #10에서 고쳤다).
- 설치는 `pip install -e .` (= `scripts/setup.sh`). 의존성은 `pyproject.toml`이 단일 진실 원천이다.
- 사용자 대면 문자열은 전부 한국어다. 새 메시지를 추가할 때 같은 톤(존댓말, 간결한 상태 문구)을 유지할 것.

### Testing Requirements
환경변수 없이 그냥 돈다. `pyproject.toml`의 `[tool.pytest.ini_options]`가 `pythonpath`를 잡고
(이슈 #8), `src/auth.py`는 import가 아니라 `ensure_configured()` 호출 시점에 검증한다(이슈 #9).

```bash
uv run --with pytest --with discord.py --with python-dotenv pytest -q
# 기대 결과: 374 passed
```

인증 환경변수를 **일부러 주지 않는 것**이 이슈 #9의 회귀 테스트다. CI(`.github/workflows/ci.yml`)도
같은 이유로 환경변수 없이 돌린다. 프로젝트 `.venv`에는 pytest가 없으므로 `--with pytest`가 필요하다
(또는 `uv sync --group dev` 후 `uv run pytest`).

린트는 `uv run ruff check .`이며 `pyproject.toml`의 `[tool.ruff]`가 기준선이다(이슈 #21). 초록이어야 한다.

### Common Patterns
- 모듈은 얇은 단일 책임 단위로 나뉘고, `src/main.py`가 이들을 조립한다.
- 부수효과가 있는 함수는 `async`, 순수 로직(포맷팅·파싱·경로 검증)은 동기 함수로 분리해 테스트 가능하게 유지한다.
- 실패는 예외를 던지기보다 `{"type": "error", "text": ...}` 형태의 이벤트 dict로 흘려보내는 경우가 많다.

## Dependencies

### External
- `discord.py>=2.4` — Discord Gateway 클라이언트. `message_content` intent 필수
- `python-dotenv>=1.0` — 루트 `.env` 로딩
- **Claude Code CLI** — `claude` 실행 파일이 PATH에 있거나 `CLAUDE_BIN`으로 지정되어야 함. `pyproject.toml`에는 잡히지 않는 암묵적 시스템 의존성
- `qlmanage` (macOS 전용, 선택) — SVG → PNG 미리보기 렌더링

<!-- MANUAL: Any manually added notes below this line are preserved on regeneration -->
