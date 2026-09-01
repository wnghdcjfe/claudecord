<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-08-31 | Updated: 2026-09-01 -->

# tests

## Purpose
`src/` 각 모듈에 1:1로 대응하는 `unittest` 기반 테스트 스위트. 총 382개 테스트(+ subtest 14개)가 있으며 pytest로 실행한다. Discord Gateway나 Claude CLI 같은 외부 의존성은 전부 `unittest.mock`으로 대체하므로 네트워크·서브프로세스 없이 돈다.

## Key Files

| File | Tests | Description |
|------|-------|-------------|
| `test_orchestrator.py` | 63 | 잡 디렉터리 생성, 이어하기 작업 디렉터리 해석, 오래된 잡 정리(`RUNS_RETENTION_DAYS`) |
| `test_main.py` | 61 | 이벤트 핸들러 조립. 종료 명령 인식, 만료 세션 자동 복구, 잡 타임아웃, 동시 실행 게이트 |
| `test_runner.py` | 44 | Claude CLI 명령줄 조립, 스트림 파싱, 웜/콜드 경로 분기, 프로세스 종료 요약 |
| `test_outputs.py` | 40 | manifest 경로 탈출 방어, 인라인 SVG 추출, 첨부 분할, SVG 렌더러 폴백 |
| `test_errors.py` | 38 | 경로 삭제(redaction). 홈/절대경로/Windows 경로 축약, URL 보존, 멱등성 |
| `test_status.py` | 31 | 진행바 포맷팅, 대기열 문구, GIF 자산 존재/부재 처리 |
| `test_warm_pool.py` | 28 | 웜 프로세스 대여·반납, 유휴 TTL, 프로세스 상한, `retire` 콜백 위임 |
| `test_sessions.py` | 27 | 세션 영속화, TTL 만료, 원자적 쓰기, 손상 저장소 격리, 레거시 포맷 흡수 |
| `test_timing.py` | 22 | span 기록, 재시도 시 attempt 아카이빙, 기동 오버헤드 산출, `timings.json` 기록 |
| `test_parser.py` | 13 | `@sess-`·프로젝트 태그 파싱, `projects.toml` 로딩과 mtime 캐시 |
| `test_auth.py` | 12 | 소유자·채널 허용 목록, 다중 소유자, 지연 설정 로딩 |
| `test_greetings.py` | 3 | 인사 즉답 정확 일치 |

## For AI Agents

### Working In This Directory
- `conftest.py`는 없다. `pyproject.toml`의 `[tool.pytest.ini_options] pythonpath`가 저장소 루트를 `sys.path`에 넣으므로 `PYTHONPATH=.`도 필요 없다.
- **인증 환경변수를 주지 마라.** 환경변수 없이 수집·통과하는 것 자체가 이슈 #9의 회귀 테스트이고, CI도 그렇게 돌린다. 환경변수를 바꿔가며 검증해야 한다면 `src.auth._config.cache_clear()`를 호출할 것.
- 새 테스트는 `unittest.TestCase` 클래스 + `test_` 메서드 스타일을 따른다. pytest 함수 스타일은 아직 쓰이지 않는다.
- 비동기 코드는 `asyncio.run(scenario())` 패턴으로 감싸 동기 테스트 메서드 안에서 돌린다 (`test_main.py` 참고). `pytest-asyncio`는 쓰지 않는다.
- 테스트 픽스처에 개인 프로젝트 이름·절대경로를 넣지 말 것 (#16에서 걷어냈다).

### Testing Requirements
```bash
uv run pytest -q          # 기대 결과: 382 passed, 14 subtests passed
```
`pytest`는 `[dependency-groups] dev`에 선언되어 있으므로 `uv run`이 알아서 끌어온다.

### Common Patterns
- **파일시스템은 `tempfile.TemporaryDirectory`로**: 실제 홈 디렉터리를 건드리지 않도록 `mock.patch`로 모듈 상수(`_STORE_PATH`, `WORKING_GIF_PATH`)를 임시 경로로 갈아끼운다.
- **`mock.AsyncMock`으로 async 경계 차단**: `terminate_active_claude_processes`, `run_job`처럼 부수효과가 큰 async 함수를 대체한다.
- **모듈 상수·환경변수 패치**: 설정이 모듈 상수 + 환경변수 오버라이드 구조라서, 테스트는 `mock.patch.object(module, "CONST", ...)`와 `mock.patch.dict(os.environ, ...)`를 함께 쓴다.
- **시계 주입**: 시간에 의존하는 코드는 `clock`/`now` 인자를 받도록 되어 있다 (`JobTimings(clock=...)`, `get_session_state(now=...)`). `time.sleep`으로 기다리는 테스트를 새로 만들지 말 것.
- **`subTest`로 표 형태 검증**: 입력·기댓값 쌍이 여럿인 경우(경로 삭제 패턴 등) 반복문 + `with self.subTest(...)`를 쓴다.

## Dependencies

### Internal
- `src/` 전체 — 테스트 대상

### External
- `pytest` — 러너 (`[dependency-groups] dev`)
- `unittest`, `unittest.mock`, `tempfile`, `asyncio` — 표준 라이브러리
- `discord.py` — `src` 모듈 import 시 전이적으로 필요

<!-- MANUAL: -->
