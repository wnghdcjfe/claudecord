<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-08-31 | Updated: 2026-08-31 -->

# src

## Purpose
봇 애플리케이션의 전체 소스. Discord 메시지 수신 → 인증 → 명령 파싱 → 작업 디렉터리 생성 → Claude CLI 스트리밍 실행 → 산출물 회신까지의 파이프라인을 모듈별로 나눠 담는다. 각 모듈은 단일 책임을 지고, `main.py`가 이들을 하나의 이벤트 핸들러로 조립한다.

## Key Files

| File | Description |
|------|-------------|
| `main.py` | Discord 이벤트 진입점. `on_message`가 인증 → `/clear`·`종료`·인사 특수명령 처리 → 파싱 → 잡 생성 → 로더 스피너 → 실행 → 산출물 전송 순서를 조율. 만료된 세션에 대한 자동 재시도(`_run_job_with_session_recovery`) 포함 |
| `auth.py` | 화이트리스트 인증. `OWNER_DISCORD_ID` 단일 소유자 + `ALLOWED_CHANNEL_IDS` 채널 목록. **모듈 import 시점에 환경변수를 읽고 검증한다** |
| `parser.py` | 메시지 → `Command(prompt, session_id, workdir, system_hint)` 변환. `@sess-<id>` 접두사로 명시적 세션 지정, `PROJECTS_FILE`(기본 `projects.toml`)에 정의된 `@<태그>`로 프로젝트 디렉터리 전환. 설정 파일이 없으면 태그 없이 동작한다 |
| `orchestrator.py` | 잡 수명주기. `create_job`이 `runs/job-xxxx/`에 프롬프트·메타데이터를 쓰고, `run_job`이 스트림 이벤트를 `logs/stream.jsonl`에 적재하며 최종 result/error 이벤트를 반환. 다음 턴의 작업 디렉터리를 `session_state.json`에서 읽어 이어붙임 |
| `runner.py` | Claude CLI 서브프로세스 계층. 명령줄 조립(`build_claude_command`), 스트리밍 실행(`run_claude_stream`), 실행 중 프로세스 추적 및 원격 일괄 종료(`terminate_active_claude_processes`). Windows 배치 래핑과 POSIX 프로세스 그룹 시그널 처리 포함 |
| `sessions.py` | 채널별 Claude 세션 ID·작업 디렉터리 영속화. `~/.claudecord/sessions.json`에 JSON으로 저장, TTL 1시간 |
| `outputs.py` | 산출물 회신. `output.md`를 1900자로 쪼개 전송하고, 인라인 SVG 코드블록을 실제 파일로 추출하며, `manifest.json`의 `files` 항목을 경로 탈출 검증 후 첨부 |
| `status.py` | 작업 중 상태 표시. 5프레임 진행바를 1.2초 간격으로 회전시키는 `run_spinning_loader` |
| `greetings.py` | `"안녕"` 정확 일치 시 Claude를 거치지 않고 즉답 |

## For AI Agents

### Working In This Directory
- **`auth.py`는 import 시점에 `OWNER_DISCORD_ID`를 요구한다.** 이 모듈을 (직접이든 `main.py`를 통해서든) import하는 코드는 환경변수 없이 실행되지 않는다. 테스트를 추가할 때 이 제약을 먼저 고려할 것.
- `main.py`의 `load_dotenv(...)`는 **의도적으로** `src.*` import보다 위에 있다. `auth.py`가 import 시점에 환경변수를 읽기 때문이며, 순서를 바꾸면 봇이 뜨지 않는다. ruff의 E402/I001 지적을 이유로 재정렬하지 말 것.
- `runner.py`는 `--permission-mode bypassPermissions`와 `--allowedTools`/`--disallowedTools`를 함께 넘긴다. 이 조합의 실효성은 CLI 버전에 의존하므로, 도구 제한을 손볼 때는 실제 CLI 동작을 확인하고 바꿀 것.
- 경로를 다루는 코드(`outputs._safe_manifest_path`, `orchestrator._resolve_target_workdir`, `_read_continuation_workdir`)는 신뢰 경계다. LLM이 만든 `manifest.json`·`session_state.json`을 입력으로 받으므로 검증을 약화시키지 말 것.
- 새 사용자 대면 문자열은 한국어로.

### Testing Requirements
- 모듈 하나당 `tests/test_<module>.py` 하나가 대응한다. 새 모듈을 추가하면 같은 규칙으로 테스트 파일을 만든다.
- 실행법은 루트 `AGENTS.md`의 Testing Requirements 참고 (환경변수 없이 그냥 돈다).
- Discord 객체는 `unittest.mock`으로 대체한다. 실제 Gateway 연결을 요구하는 테스트는 없다.

### Common Patterns
- **에러는 예외 대신 이벤트 dict로**: `run_claude_stream`은 실패 시에도 예외를 올리지 않고 `{"type": "error", "text": ..., "returncode": ...}`를 yield한다. `main.py`가 `meta.get("type") == "error" or meta.get("is_error")`로 성공/실패를 판정한다.
- **순수 함수 분리**: `_format_shutdown_reply`, `format_working_status`, `_is_shutdown_command`처럼 I/O 없는 헬퍼로 떼어내 테스트한다.
- **방어적 역직렬화**: 외부 JSON을 읽는 곳은 타입을 일일이 확인하고 실패 시 기본값으로 되돌아간다 (`sessions._coerce_state`, `orchestrator._read_continuation_workdir`). 단, `sessions._load()`는 예외적으로 손상된 JSON을 처리하지 않는다.
- **설정은 환경변수 + `projects.toml`**: 프로젝트 태그는 `PROJECTS_FILE`이 가리키는 TOML에서 읽는다(이슈 #16, `projects.example.toml` 참고). `SAFE_TOOLS`/`BLOCKED_TOOLS`/`SESSION_TTL_SECONDS`는 여전히 모듈 상수다 — 앞의 두 개가 왜 보안 경계가 아닌지는 `src/runner.py` 상단 NOTE를 읽을 것.

## Dependencies

### Internal
- `main.py` → 그 외 모든 모듈 (유일한 조립 지점)
- `orchestrator.py` → `runner.py`
- 나머지 모듈끼리는 서로 의존하지 않는다 (평평한 구조)

### External
- `discord.py` — `auth`, `main`, `outputs`, `status`가 사용
- `python-dotenv` — `main`만 사용
- Claude Code CLI — `runner`가 서브프로세스로 실행

<!-- MANUAL: -->
