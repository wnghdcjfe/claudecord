<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-08-31 | Updated: 2026-09-01 -->

# src

## Purpose
봇 애플리케이션의 전체 소스. Discord 메시지 수신 → 인증 → 명령 파싱 → 작업 디렉터리 생성 → Claude CLI 스트리밍 실행 → 산출물 회신까지의 파이프라인을 모듈별로 나눠 담는다. 각 모듈은 단일 책임을 지고, `main.py`가 이들을 하나의 이벤트 핸들러로 조립한다.

## Key Files

| File | Description |
|------|-------------|
| `main.py` | Discord 이벤트 진입점. `on_message`가 인증 → `종료`·인사 특수명령 → 파싱 → 동시 실행 게이트 → 잡 생성 → 로더 → 실행 → 산출물 전송을 조율. 만료 세션 자동 재시도(`_run_job_with_session_recovery`), 잡 타임아웃(`JOB_TIMEOUT_SECONDS`, 기본 600초), 동시 잡 상한(`MAX_CONCURRENT_JOBS`, 기본 2) 포함 |
| `auth.py` | 화이트리스트 인증. `OWNER_DISCORD_ID`(쉼표 구분 다중 소유자 가능, #28) + `ALLOWED_CHANNEL_IDS`. **환경변수는 import가 아니라 `_config()` 최초 호출 시 읽는다**(#9). 봇 기동 시 `ensure_configured()`가 fail-fast 검증을 담당 |
| `parser.py` | 메시지 → `Command(prompt, session_id, workdir, system_hint)` 변환. `@sess-<id>`로 명시적 세션 지정, `PROJECTS_FILE`(기본 `projects.toml`)의 `@<태그>`로 프로젝트 디렉터리 전환. 파싱 결과는 파일의 `(mtime, size)`로 캐시한다 — `parse()`는 메시지마다 돌고, 매번 다시 읽으면 이벤트 루프에 동기 디스크 I/O가 돌아온다 |
| `orchestrator.py` | 잡 수명주기. `allocate_job`/`prepare_job`이 `runs/job-xxxx/`를 만들고, `run_job`이 스트림 이벤트를 `logs/stream.jsonl`에 적재하며 result/error 이벤트를 반환. `cleanup_old_runs`가 `RUNS_RETENTION_DAYS`(기본 30일) 지난 잡 디렉터리를 정리한다 |
| `runner.py` | Claude CLI 서브프로세스 계층. 명령줄 조립(`build_claude_command`), 스트리밍 실행(`run_claude_stream`), 웜/콜드 경로 분기, 프로세스 추적과 일괄 종료(`terminate_active_claude_processes`)·잡 단위 종료(`terminate_job_processes`). Windows 배치 래핑과 POSIX 프로세스 그룹 시그널 처리 포함 |
| `warm_pool.py` | 재사용 가능한 claude 프로세스 풀. `--input-format stream-json`으로 띄운 프로세스의 stdin을 열어둔 채 여러 턴을 태워 매번의 1.4~1.6초 기동 비용을 없앤다(#2). **순수 장부일 뿐 프로세스를 직접 죽이지 않는다** — 종료는 `retire` 콜백으로 `runner.py`에 위임 |
| `sessions.py` | 채널별 Claude 세션 ID·작업 디렉터리 영속화. `~/.claudecord/sessions.json`, TTL은 `SESSION_TTL_SECONDS`(기본 1시간). 쓰기는 tmp 파일 + `os.replace`로 원자적이고, 손상된 저장소는 격리(quarantine) 후 새로 시작한다 |
| `outputs.py` | 산출물 회신. `output.md`를 `DISCORD_CHUNK_LIMIT`(1950자)로 쪼개 보내되 조각이 `OUTPUT_INLINE_MAX_CHUNKS`(기본 3)를 넘으면 `.md` 첨부 1건으로 전환. 인라인 SVG 코드블록을 파일로 추출하고 PNG 미리보기를 렌더하며, `manifest.json`의 `files`를 경로 탈출 검증 후 첨부 |
| `errors.py` | **Discord로 나가는 텍스트의 경로 삭제(redaction)**. `redact_paths`는 홈 디렉터리를 `~`로, 그 외 절대경로를 마지막 세그먼트로 줄이고 URL은 남긴다(멱등). `safe_error_text`는 예외를 `타입명: 삭제된 메시지` 400자로 렌더한다 |
| `timing.py` | 잡 단위 구간 계측(#7). `JobTimings`가 `t_ack`/`t_spawn`/`t_first_event`/`t_result`/`t_outputs`/`t_total`을 기록하고, CLI가 보고한 `duration_ms`를 빼서 순수 기동 오버헤드를 산출해 `<job_dir>/timings.json`에 남긴다 |
| `status.py` | 작업 중 상태 표시. 진행바 회전(`run_spinning_loader`), 대기열 문구(`format_queued_status`), 선택적 GIF 첨부. 편집 간격은 2.5초에서 1.5배씩 늘어 30초에서 멈춘다(#19) — 10분짜리 잡의 편집이 240회에서 ~24회로 준다 |
| `greetings.py` | `"안녕"` 정확 일치 시 Claude를 거치지 않고 즉답 |

## For AI Agents

### Working In This Directory
- **`main.py`의 `load_dotenv(...)`는 의도적으로 `src.*` import보다 위에 있다.** `src.main`을 import하는 것이 곧 봇이 `.env`를 읽는 경로이기 때문이다. ruff의 E402/I001은 `pyproject.toml`의 per-file-ignores로 이미 꺼둔 상태이니, 정렬을 이유로 재배치하지 말 것.
- `auth.py`는 더 이상 import 시점에 환경변수를 요구하지 않는다(#9). 테스트에서 환경변수를 바꿔가며 검증할 때는 `_config.cache_clear()`를 호출할 것.
- **`runner.py` 상단의 긴 NOTE를 먼저 읽을 것.** 도구 제한은 보안 경계가 아니다. `--allowedTools`는 `bypassPermissions` 아래에서 아무것도 제한하지 못해 제거됐고, 실제로 스키마를 줄이는 `--tools`(=`ALLOWED_TOOLS`)로 대체됐다. `--disallowedTools`는 명령 선두 문자열로 매칭하므로 `/bin/rm`은 통과한다. 진짜 경계는 `auth.py`다.
- 경로를 다루는 코드(`outputs._safe_manifest_path`, `orchestrator._resolve_target_workdir`, `_read_continuation_workdir`)는 신뢰 경계다. LLM이 만든 `manifest.json`·`session_state.json`을 입력으로 받으므로 검증을 약화시키지 말 것.
- **Discord로 나가는 에러 문자열은 `errors.py`를 거친다.** 새 에러 경로를 만들면 `redact_paths`/`safe_error_text`를 통과시킬 것. 이 봇은 개인 PC에서 돌고, 채널은 공유될 수 있다.
- 웜 경로를 건드릴 때: 재사용 키는 `(WarmKey, session_id)`이고 `resume=None`인 턴은 **항상** 새로 띄운다(이전 대화 문맥 상속 방지). `warm_pool.py`가 프로세스를 죽이게 만들지 말 것 — 종료 지점이 둘로 갈라진다.
- 새 사용자 대면 문자열은 한국어로.

### Testing Requirements
- 모듈 하나당 `tests/test_<module>.py` 하나가 대응한다. 새 모듈을 추가하면 같은 규칙으로 테스트 파일을 만든다.
- 실행법은 루트 `AGENTS.md`의 Testing Requirements 참고 (`uv run pytest -q`, 환경변수 없이 그냥 돈다).
- Discord 객체는 `unittest.mock`으로 대체한다. 실제 Gateway 연결을 요구하는 테스트는 없다.

### Common Patterns
- **에러는 예외 대신 이벤트 dict로**: `run_claude_stream`은 실패 시에도 예외를 올리지 않고 `{"type": "error", "text": ..., "returncode": ...}`를 yield한다. 단 웜 프로세스는 같은 실패를 `{"type": "result", "is_error": true, "result": ...}`로 **in-band** 보고하므로, `main.py`는 `ERROR_TEXT_KEYS = ("text", "result", "error")` 전부를 훑는다.
- **계측은 절대 요청 경로를 깨뜨리지 않는다**: `JobTimings`의 모든 메서드는 best-effort다. 시작 안 된 span을 `stop`해도 `None`을 돌려줄 뿐 예외를 던지지 않는다.
- **순수 함수 분리**: `_format_shutdown_reply`, `format_working_status`, `_is_shutdown_command`처럼 I/O 없는 헬퍼로 떼어내 테스트한다.
- **방어적 역직렬화**: 외부 JSON·TOML을 읽는 곳은 타입을 일일이 확인하고 실패 시 기본값으로 되돌아간다 (`sessions._coerce_state`, `orchestrator._read_continuation_workdir`, `parser._load_projects`).
- **환경변수 파싱은 실패해도 죽지 않는다**: 잘못된 값은 로그를 남기고 모듈 상수 기본값으로 되돌아간다 (`_job_timeout_seconds`, `_runs_retention_days`, `warm_pool._idle_ttl` 등).

## Dependencies

### Internal
- `main.py` → 그 외 모든 모듈 (유일한 조립 지점)
- `orchestrator.py` → `runner.py`, `timing.py`
- `runner.py` → `warm_pool.py`, `timing.py` (역방향 의존 없음)
- `outputs.py` → `errors.py`
- 나머지 모듈끼리는 서로 의존하지 않는다

### External
- `discord.py` — `auth`, `main`, `outputs`, `status`가 사용
- `python-dotenv` — `main`만 사용
- `tomllib` (표준 라이브러리) — `parser`의 프로젝트 설정 읽기
- Claude Code CLI — `runner`가 서브프로세스로 실행
- SVG 렌더러 (선택) — `outputs`가 `qlmanage`/`rsvg-convert`/`inkscape`/`cairosvg` 순으로 시도

<!-- MANUAL: -->
