<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-09-01 | Updated: 2026-09-01 -->

# .github

## Purpose
GitHub Actions 설정. 모든 `main` 푸시와 모든 PR에서 테스트와 린트를 돌린다. 이 저장소는 macOS·Linux·Windows를 모두 지원 대상으로 삼으므로(`scripts/`에 `.sh`/`.ps1` 짝이 있다) CI도 세 OS를 전부 돈다.

## Key Files

| File | Description |
|------|-------------|
| `workflows/ci.yml` | `test` 잡(ubuntu·macOS × py3.11·3.13, windows × py3.11)과 `lint` 잡(ruff). `uv sync --group dev` 후 `uv run --no-sync`로 실행 |

## Subdirectories

| Directory | Purpose |
|-----------|---------|
| `workflows/` | Actions 워크플로 정의 |

## For AI Agents

### Working In This Directory
- **windows 레인의 `continue-on-error`를 되살리지 말 것.** CI 도입 시점부터 그 예외 때문에 레인이 매 실행 빨간 채로 방치됐고, 잡이 초록으로 만들어진 뒤 블로킹으로 되돌렸다. 워크플로는 성공을 보고하는데 잡만 실패해 있으면 아무도 쫓지 않는다.
- **테스트 스텝에 환경변수를 넣지 말 것.** `OWNER_DISCORD_ID` 없이 테스트가 수집·통과하는 것이 이슈 #9의 회귀 테스트다. 빈 환경 자체가 검증 대상이다.
- 파이썬 버전 매트릭스에는 이유가 있다: 3.11은 `pyproject.toml`이 선언한 하한, 3.13은 사전 경고용(`discord.py`가 3.13에서 제거된 `audioop`을 아직 import한다).
- `concurrency` 그룹이 같은 브랜치의 이전 실행을 취소한다. 이걸 끄면 푸시할 때마다 결과가 무의미해진 실행이 쌓인다.

### Testing Requirements
워크플로 자체를 로컬에서 검증하려면 같은 명령을 그대로 돌려보면 된다:
```bash
uv sync --group dev
uv run --no-sync pytest -q
uv run --no-sync ruff check .
```
YAML 문법만 확인할 거라면 `python -c "import yaml,sys; yaml.safe_load(open('.github/workflows/ci.yml'))"`.

### Common Patterns
- 결정 하나마다 **왜 그렇게 했는지**를 YAML 주석으로 남긴다. 이 파일의 주석은 장식이 아니라 되돌리기 방지 장치다.
- 액션은 메이저 태그로 고정한다 (`actions/checkout@v4`, `astral-sh/setup-uv@v5`).
- `enable-cache: true`로 uv 캐시를 재사용하고, 설치와 실행을 `uv sync` / `uv run --no-sync`로 분리한다.

## Dependencies

### Internal
- `pyproject.toml` — 의존성 그룹(`dev`), pytest·ruff 설정의 출처
- `tests/` — 테스트 잡의 대상

### External
- GitHub Actions 러너 (`ubuntu-latest`, `macos-latest`, `windows-latest`)
- `astral-sh/setup-uv` — uv 설치와 캐시

<!-- MANUAL: -->
