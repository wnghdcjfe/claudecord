<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-08-31 | Updated: 2026-08-31 -->

# examples

## Purpose
Claude Code CLI가 `--output-format stream-json`으로 내보내는 이벤트의 실제 형태를 기록한 참고용 샘플. 코드가 import하지도, 테스트가 픽스처로 쓰지도 않는 순수 문서 자료다. `src/runner.py`가 파싱하고 `src/orchestrator.py`·`src/main.py`가 해석하는 dict의 스키마를 눈으로 확인할 때 본다.

## Key Files

| File | Description |
|------|-------------|
| `claude_ping_result.json` | `scripts/check_claude.sh`의 `-p "ping"` 왕복이 돌려주는 최소 형태의 result 이벤트 |
| `stream_result_event.json` | 실제 작업이 끝났을 때의 result 이벤트. `is_error`, `duration_ms`, `num_turns`, `total_cost_usd`, `session_id` 포함 |

## Subdirectories
없음.

## For AI Agents

### Working In This Directory
- 이 파일들은 **관찰 기록**이다. 원하는 스키마를 여기에 적어 넣는다고 CLI 동작이 바뀌지 않는다. 실제 출력이 달라졌을 때만 갱신할 것.
- `session_id` 같은 값은 이미 `"abc12345-..."`로 마스킹되어 있다. 새 샘플을 넣을 때도 실제 세션 ID·토큰·절대경로는 지우고 넣을 것.
- 코드가 실제로 의존하는 키는 `type`, `subtype`, `is_error`, `result`, `session_id` 다. `src/runner.py`와 `src/orchestrator.py`가 이 키들을 읽는다.

### Testing Requirements
테스트 대상이 아니다. 다만 스트림 이벤트 처리 로직을 고칠 때는 여기 있는 형태를 기준으로 테스트 픽스처를 만들면 실제와 어긋나지 않는다.

### Common Patterns
- 한 파일에 이벤트 하나, 들여쓰기 2칸 JSON.
- 긴 문자열 값은 `"..."`로 잘라 적는다.

## Dependencies

### Internal
- `src/runner.py` — 이 형태를 파싱하는 코드
- `scripts/check_claude.sh` — `claude_ping_result.json`을 만들어내는 명령

### External
- Claude Code CLI의 `stream-json` 출력 규격

<!-- MANUAL: -->
