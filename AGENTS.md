<!-- Generated: 2026-08-31 | Updated: 2026-09-01 -->

# claudecord (discord-claude-assistant)

## Purpose
Discord 봇으로 로컬 PC의 Claude Code CLI를 원격 조종하는 파이썬 애플리케이션. 화이트리스트로 인증된 사용자가 Discord 메시지를 보내면, 봇이 `claude -p` 서브프로세스를 스트리밍 모드로 실행하고 그 산출물(`output.md`, `manifest.json`에 등재된 파일)을 다시 Discord 채널로 첨부해 돌려준다. 클라우드 호스팅 없이 Discord Gateway의 outbound WebSocket만으로 동작한다.

## Key Files

| File | Description |
|------|-------------|
| `pyproject.toml` | 패키지 메타데이터 + 도구 설정의 단일 진실 원천. `requires-python >=3.11`, 런타임 의존성은 `discord.py>=2.4`와 `python-dotenv>=1.0` 두 개뿐. `[dependency-groups] dev`에 `pytest`·`ruff`, `[tool.pytest.ini_options]`에 `pythonpath`, `[tool.ruff.lint]`에 선택 규칙과 **각 규칙을 왜 뺐는지**가 주석으로 붙어 있다 |
| `.env.example` | 환경변수 템플릿. 필수는 `DISCORD_BOT_TOKEN`·`OWNER_DISCORD_ID`·`ALLOWED_CHANNEL_IDS` 셋뿐이고 나머지(`PROJECTS_FILE`, `RUNS_RETENTION_DAYS`, `CLAUDE_MODEL`, `WARM_CLAUDE*`, `JOB_TIMEOUT_SECONDS`, `MAX_CONCURRENT_JOBS`, `OUTPUT_INLINE_MAX_CHUNKS`, `DEBUG_TIMING` 등)는 전부 기본값이 있다 |
| `projects.example.toml` | `@태그` → 프로젝트 디렉터리 매핑 예시. `projects.toml`로 복사해 쓰며 그 사본은 git-ignored. 파일이 없으면 태그 없이 정상 동작한다 |
| `README.md` | 한국어 사용자 문서. Discord 앱 생성 → 권한 설정 → ID 확보 → `.env` 작성 → 실행 순서와 성능 튜닝 변수 표 |
| `LICENSE` | MIT. `pyproject.toml`의 `license-files`가 이 파일을 가리킨다 |
| `working_m.gif` | 작업 중 상태 메시지에 첨부되는 애니메이션 (192KB). `src/status.py`가 참조하며 `WORKING_GIF=1`일 때만 쓰인다 |
| `logo.png`, `example.jpeg` | README용 이미지 |

## Subdirectories

| Directory | Purpose |
|-----------|---------|
| `src/` | 봇 애플리케이션 전체 소스 (see `src/AGENTS.md`) |
| `tests/` | `unittest` 기반 테스트 스위트, 382개 테스트 (see `tests/AGENTS.md`) |
| `scripts/` | macOS(bash)/Windows(PowerShell) 셋업·실행·상시구동 스크립트 (see `scripts/AGENTS.md`) |
| `examples/` | Claude CLI `stream-json` 이벤트 샘플 (see `examples/AGENTS.md`) |
| `.github/` | GitHub Actions CI. 3개 OS × 파이썬 2버전 테스트 + 린트 (see `.github/AGENTS.md`) |
| `.omc/` | oh-my-claudecode 런타임 상태. 애플리케이션과 무관하며 커밋 대상이 아님 |

## For AI Agents

### Working In This Directory
- 진입점은 `src/main.py`이고, 실행은 `python -m src.main` 또는 `scripts/run_bot.sh`다. 이 저장소에는 `bot.py`도 `requirements.txt`도 없다 (README가 그 둘을 안내하던 것은 이슈 #10에서 고쳤다).
- 설치는 `pip install -e .` (= `scripts/setup.sh`) 또는 `uv sync --group dev`. 의존성은 `pyproject.toml`이 단일 진실 원천이다.
- 사용자 대면 문자열은 전부 한국어다. 새 메시지를 추가할 때 같은 톤(존댓말, 간결한 상태 문구)을 유지할 것.
- **Discord로 나가는 텍스트에 절대경로를 실어 보내지 말 것.** 예외 메시지는 `src/errors.py`의 `redact_paths`/`safe_error_text`를 반드시 거친다 (#19·#25·#26 묶음).
- 이 봇은 Windows도 지원 대상이다. CI에 windows 레인이 있고 블로킹이다. 경로·프로세스·인코딩을 다룰 때 POSIX 전용 가정을 넣지 말 것.

### Testing Requirements
환경변수 없이 그냥 돈다. `pyproject.toml`의 `[tool.pytest.ini_options]`가 `pythonpath`를 잡고
(이슈 #8), `src/auth.py`는 import가 아니라 `ensure_configured()` 호출 시점에 검증한다(이슈 #9).

```bash
uv run pytest -q          # 기대 결과: 382 passed, 14 subtests passed
uv run ruff check .       # 기대 결과: All checks passed!
```

`pytest`와 `ruff`는 `[dependency-groups] dev`에 선언되어 있으므로 `uv run`이 알아서 끌어온다.
(`--with pytest`를 붙이던 예전 방식은 더 이상 필요 없다.)

인증 환경변수를 **일부러 주지 않는 것**이 이슈 #9의 회귀 테스트다. CI(`.github/workflows/ci.yml`)도
같은 이유로 환경변수 없이 돌린다.

린트 기준선은 `pyproject.toml`의 `[tool.ruff]`이며(이슈 #21) 초록이어야 한다. 어떤 규칙을 왜 제외했는지는
그 파일의 주석에 적혀 있다 — 규칙을 추가·제거하기 전에 먼저 읽을 것.

### Common Patterns
- 모듈은 얇은 단일 책임 단위로 나뉘고, `src/main.py`가 이들을 조립한다.
- 부수효과가 있는 함수는 `async`, 순수 로직(포맷팅·파싱·경로 검증)은 동기 함수로 분리해 테스트 가능하게 유지한다.
- 실패는 예외를 던지기보다 `{"type": "error", "text": ...}` 형태의 이벤트 dict로 흘려보내는 경우가 많다.
- 설정값은 **모듈 상수 + 환경변수 오버라이드**가 기본형이다. 상수는 기본값을, 환경변수는 운영 튜닝을 담당하며, 파싱 실패는 예외 대신 기본값으로 되돌아간다.

## Dependencies

### External
- `discord.py>=2.4` — Discord Gateway 클라이언트. `message_content` intent 필수
- `python-dotenv>=1.0` — 루트 `.env` 로딩
- **Claude Code CLI** — `claude` 실행 파일이 PATH에 있거나 `CLAUDE_BIN`으로 지정되어야 함. `pyproject.toml`에는 잡히지 않는 암묵적 시스템 의존성
- SVG → PNG 미리보기 렌더러 (전부 선택): `qlmanage`(macOS), `rsvg-convert`(librsvg), `inkscape`, `cairosvg`(파이썬 패키지). 하나도 없으면 미리보기 없이 SVG 원본만 첨부한다

<!-- MANUAL: Any manually added notes below this line are preserved on regeneration -->
