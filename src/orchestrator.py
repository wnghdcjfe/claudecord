import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from src.runner import run_claude_stream

JOB_META = "job.json"


def get_runs_dir() -> Path:
    return Path(os.environ.get("RUNS_DIR", "~/.discord-claude/runs")).expanduser()


def _resolve_target_workdir(workdir: str | None) -> Path | None:
    if not workdir:
        return None

    target = Path(workdir).expanduser()
    if not target.is_dir():
        raise ValueError(f"작업 디렉터리가 없습니다: {target}")
    return target.resolve()


def _write_event(job_dir: Path, event: dict) -> None:
    with (job_dir / "logs" / "stream.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


def create_job(user_text: str, workdir: str | None, system_hint: str | None) -> Path:
    job_id = "job-" + uuid.uuid4().hex[:8]
    job_dir = get_runs_dir() / job_id
    (job_dir / "logs").mkdir(parents=True, exist_ok=True)
    job_dir = job_dir.resolve()

    target_workdir = _resolve_target_workdir(workdir)
    claude_cwd = target_workdir or job_dir

    prompt = f"""사용자 요청:
{user_text}

실행 컨텍스트:
- Claude CLI 작업 디렉터리: {claude_cwd}
- Discord 보고/첨부 산출물 디렉터리: {job_dir}

작업 규칙:
- 답변 본문은 {job_dir}/output.md 파일에 한국어로 작성한다.
- 산출 파일이 있으면 {job_dir} 하위에 저장한다.
- SVG, PNG, JPG, GIF, WEBP 등 이미지 결과물은 코드 블록으로 길게 붙여넣지 말고
  {job_dir} 하위의 실제 파일로 저장한 뒤 manifest.json의 files에 반드시 포함한다.
- SVG 결과물은 .svg 원본 파일로 저장한다. Discord 미리보기용 PNG는 봇이 자동 생성하므로
  output.md에는 이미지 코드 대신 짧은 설명만 쓴다.
- 작업 완료 시 {job_dir}/manifest.json 파일을 작성한다. 형식:
  {{ "summary": "<한 줄 요약>",
     "files": [{{"path": "<상대경로>", "label": "<설명>"}}] }}
- 소스 수정이 필요하면 Claude CLI 작업 디렉터리 내부에서만 변경한다.
- 보고/첨부 산출물은 Discord 보고/첨부 산출물 디렉터리 내부에만 저장한다.
- 위 두 디렉터리 밖에는 쓰지 않는다.
"""
    if system_hint:
        prompt = system_hint + "\n\n" + prompt

    (job_dir / "prompt.md").write_text(prompt, encoding="utf-8")
    (job_dir / JOB_META).write_text(
        json.dumps(
            {
                "id": job_id,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "workdir": str(target_workdir) if target_workdir else None,
                "job_dir": str(job_dir),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return job_dir


async def run_job(job_dir: Path, resume: str | None = None) -> dict:
    prompt = (job_dir / "prompt.md").read_text(encoding="utf-8")
    meta_path = job_dir / JOB_META
    job_meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    claude_cwd = Path(job_meta.get("workdir") or job_dir).expanduser()
    last_meta = {}

    if not claude_cwd.is_dir():
        event = {
            "type": "error",
            "text": f"Claude CLI 작업 디렉터리가 없습니다: {claude_cwd}",
            "returncode": None,
        }
        _write_event(job_dir, event)
        return event

    async for event in run_claude_stream(
        prompt,
        workdir=str(claude_cwd.resolve()),
        resume=resume,
        extra_dirs=[str(job_dir.resolve())],
    ):
        if event.get("type") in {"result", "error"}:
            last_meta = event

        _write_event(job_dir, event)

    return last_meta
