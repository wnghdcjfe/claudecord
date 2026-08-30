<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-08-31 | Updated: 2026-08-31 -->

# tests

## Purpose
`src/` 각 모듈에 1:1로 대응하는 `unittest` 기반 테스트 스위트. 총 38개 테스트가 있으며 pytest로 실행한다. Discord Gateway나 Claude CLI 같은 외부 의존성은 전부 `unittest.mock`으로 대체하므로 네트워크·서브프로세스 없이 돈다.

## Key Files

| File | Description |
|------|-------------|
| `test_main.py` | 이벤트 핸들러 조립 로직. 종료 명령 인식, 만료 세션 자동 복구, 세션/작업디렉터리 해석 |
| `test_runner.py` | Claude CLI 명령줄 조립, 스트림 파싱, 프로세스 종료 요약 |
| `test_outputs.py` | manifest 경로 탈출 방어, 인라인 SVG 추출, 첨부 분할 |
| `test_orchestrator.py` | 잡 디렉터리 생성, 이어하기 작업 디렉터리 해석 |
| `test_sessions.py` | 세션 영속화, TTL 만료, 레거시 문자열 포맷 흡수 |
| `test_status.py` | 진행바 포맷팅, GIF 자산 존재/부재 처리 |
| `test_parser.py` | `@sess-`·프로젝트 태그 파싱 |
| `test_greetings.py` | 인사 즉답 정확 일치 |

## For AI Agents

### Working In This Directory
- `conftest.py`가 **없다.** 그래서 `PYTHONPATH=.` 없이는 `import src.*`가 깨진다.
- `test_main.py`는 `src.main` → `src.auth` 연쇄 import 때문에 **수집 단계에서** `OWNER_DISCORD_ID`를 요구한다. 환경변수가 없으면 이 파일 하나 때문에 전체 수집이 중단된다.
- 새 테스트는 `unittest.TestCase` 클래스 + `test_` 메서드 스타일을 따른다. pytest 함수 스타일은 아직 쓰이지 않는다.
- 비동기 코드는 `asyncio.run(scenario())` 패턴으로 감싸 동기 테스트 메서드 안에서 돌린다 (`test_main.py` 참고). `pytest-asyncio`는 쓰지 않는다.

### Testing Requirements
```bash
PYTHONPATH=. OWNER_DISCORD_ID=1 ALLOWED_CHANNEL_IDS=2 \
  uv run --with pytest --with discord.py --with python-dotenv pytest -q
# 기대 결과: 38 passed
```
프로젝트 `.venv`에는 pytest가 설치되어 있지 않아 `uv run --with pytest`가 필요하다.

### Common Patterns
- **파일시스템은 `tempfile.TemporaryDirectory`로**: 실제 홈 디렉터리를 건드리지 않도록 `mock.patch`로 모듈 상수(`_STORE_PATH`, `WORKING_GIF_PATH`)를 임시 경로로 갈아끼운다.
- **`mock.AsyncMock`으로 async 경계 차단**: `terminate_active_claude_processes`, `run_job`처럼 부수효과가 큰 async 함수를 대체한다.
- **모듈 상수 패치**: 설정이 모듈 수준 상수라서, 테스트는 `mock.patch.object(module, "CONST", ...)`로 주입한다.

## Dependencies

### Internal
- `src/` 전체 — 테스트 대상

### External
- `pytest` — 러너 (프로젝트 의존성에는 선언되어 있지 않음)
- `unittest`, `unittest.mock`, `tempfile`, `asyncio` — 표준 라이브러리
- `discord.py` — `src` 모듈 import 시 전이적으로 필요

<!-- MANUAL: -->
