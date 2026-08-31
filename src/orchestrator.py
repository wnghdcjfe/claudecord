import json
import logging
import os
import shutil
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Self

from src.errors import redact_paths
from src.runner import JobProcessScope, run_claude_stream
from src.timing import SPAN_RESULT, JobTimings

logger = logging.getLogger(__name__)

JOB_META = "job.json"
SESSION_STATE = "session_state.json"
RESULT_META = "meta.json"

# Issue #22: name is fixed so it matches the .env.example/README entries a
# different worker is writing against this exact name. Default is 30 days;
# see _runs_retention_days for why "unset" and "explicitly blank/0" are
# treated differently.
RUNS_RETENTION_DAYS_ENV_VAR = "RUNS_RETENTION_DAYS"
DEFAULT_RUNS_RETENTION_DAYS = 30


def get_runs_dir() -> Path:
    return Path(os.environ.get("RUNS_DIR", "~/.claudecord/runs")).expanduser()


def _resolve_target_workdir(workdir: str | None) -> Path | None:
    if not workdir:
        return None

    target = Path(workdir).expanduser()
    if not target.is_dir():
        # Issue #26: this ValueError's str() reaches Discord verbatim via
        # main.py's `실행 불가: {exc}` handler, so the raised message must not
        # carry the raw path. The unredacted path still goes to the app log
        # right here -- redaction is a user-text-only concern, not a logging
        # one (see src/errors.py's module docstring).
        logger.warning("Requested workdir does not exist: %s", target)
        raise ValueError(f"작업 디렉터리가 없습니다: {redact_paths(str(target))}")
    return target.resolve()


def _runs_retention_days() -> int | None:
    """Resolve RUNS_RETENTION_DAYS into a day count, or None to skip cleanup
    entirely (issue #22).

    The var being absent is the common case (nobody has touched their .env
    yet), so that gets the documented default rather than silently disabling
    cleanup. An env var explicitly set to "" or "0" is the escape hatch for
    someone who wants cleanup off without deleting the line from their .env --
    that is deliberately different from "unset", which is why this reads
    os.environ directly instead of through .get(..., default).

    A value that fails to parse, or a negative day count, falls back to the
    default rather than being treated as the off-switch: silently reading a
    typo as "disable cleanup" would just reproduce the unbounded-disk-growth
    bug this function exists to fix.
    """
    if RUNS_RETENTION_DAYS_ENV_VAR not in os.environ:
        return DEFAULT_RUNS_RETENTION_DAYS

    raw = os.environ[RUNS_RETENTION_DAYS_ENV_VAR].strip()
    if raw in ("", "0"):
        return None

    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "%s=%r is not an integer; using default of %d days",
            RUNS_RETENTION_DAYS_ENV_VAR,
            raw,
            DEFAULT_RUNS_RETENTION_DAYS,
        )
        return DEFAULT_RUNS_RETENTION_DAYS

    if value < 0:
        logger.warning(
            "%s=%d is negative; using default of %d days",
            RUNS_RETENTION_DAYS_ENV_VAR,
            value,
            DEFAULT_RUNS_RETENTION_DAYS,
        )
        return DEFAULT_RUNS_RETENTION_DAYS

    return value


def _job_dir_age_reference(job_dir: Path) -> datetime | None:
    """Best-effort "how old is this job" timestamp for retention purposes.

    Prefers job.json's created_at (written once by prepare_job and never
    touched again) over the directory's own mtime: appending to
    logs/stream.jsonl does not necessarily bump a directory's mtime, which
    would make a long-idle job dir look artificially fresh. Falls back to the
    directory's mtime for a job that never got past allocate_job (no job.json
    yet) or whose job.json is unreadable.
    """
    meta = _load_json_object(job_dir / JOB_META)
    created_at = meta.get("created_at")
    if isinstance(created_at, str):
        try:
            parsed = datetime.fromisoformat(created_at)
        except ValueError:
            parsed = None
        if parsed is not None:
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed

    try:
        return datetime.fromtimestamp(job_dir.stat().st_mtime, tz=UTC)
    except OSError:
        return None


def cleanup_old_runs(*, now: datetime | None = None) -> int:
    """Delete job directories under get_runs_dir() past RUNS_RETENTION_DAYS.

    Issue #22: this bot's recommended deployment is to stay running
    (launchd / Task Scheduler) indefinitely, and every turn allocates a new
    job-xxxx directory that is never otherwise removed -- disk usage climbs
    monotonically until the operator notices because the disk is full.
    Called once at bot startup (the simplest point that does not need its own
    scheduler): a long-running process's disk usage then only grows within
    one retention window instead of forever, and a fresh cleanup on every
    restart means a changed RUNS_RETENTION_DAYS takes effect promptly rather
    than waiting for some later periodic tick.

    Never raises: a bad RUNS_DIR permission setup, a race with another
    process, or any other failure here must not stop the bot from starting.
    Every failure -- resolving the env var, listing the runs dir, deleting one
    specific job dir -- is caught and logged (full paths, since this goes to
    the app log, not Discord); only directories actually removed count toward
    the returned total.
    """
    try:
        retention_days = _runs_retention_days()
        if retention_days is None:
            return 0

        runs_dir = get_runs_dir()
        if not runs_dir.is_dir():
            return 0

        cutoff = (now or datetime.now(UTC)) - timedelta(days=retention_days)
        entries = list(runs_dir.iterdir())
    except Exception:
        logger.exception("Run directory cleanup failed before it could start")
        return 0

    removed = 0
    for entry in entries:
        try:
            if not entry.is_dir() or not entry.name.startswith("job-"):
                continue
            age_ref = _job_dir_age_reference(entry)
            if age_ref is None or age_ref >= cutoff:
                continue
            shutil.rmtree(entry)
            removed += 1
        except Exception:
            logger.warning("Failed to remove stale run directory %s", entry, exc_info=True)
    return removed


# Issue #5: this block used to be re-typed into the user-message body of
# *every* turn (create_job's prompt.md), so it piled up again and again in
# the --resume transcript even though its content never changes. It is now
# sent once per call via --append-system-prompt (run_claude_stream's
# system_hint), which the CLI does not persist into the resumed transcript.
# The wording is byte-for-byte the original body text -- outputs.py/main.py
# depend on the {"files": ..., "workdir": ...} contract it describes, and
# changing so much as a word here is out of scope for this move.
_RULE_BLOCK = f"""규칙:
- 답변은 마지막 메시지에 한국어로 바로 쓴다. 같은 내용을 파일에 옮겨 적지 않는다.
- 소스 수정은 작업 디렉터리, 산출 파일은 산출물 디렉터리 안에서만. 두 곳 밖에는 쓰지 않는다.
- 이미지(SVG/PNG/JPG/GIF/WEBP)는 코드 블록에 붙여넣지 말고 실제 파일로 저장한다.
- 첨부할 산출 파일이 생겼거나 이어갈 작업 디렉터리가 바뀐 경우에만 {RESULT_META}을 쓴다.
  해당 없는 키는 생략하고, 둘 다 없으면 파일을 만들지 않는다.
  {{"files": [{{"path": "<산출물 디렉터리 기준 상대경로>", "label": "<설명>"}}], "workdir": "<이어갈 절대경로>"}}
- 답변이 아주 길거나 구조적일 때만 output.md를 써도 된다. 쓰면 그게 최종 답변을 대체한다."""


def _build_system_prompt(system_hint: str | None) -> str:
    """Combine the caller-supplied hint (parser/project hint) with the fixed
    rule block into the one string sent via --append-system-prompt."""
    if system_hint:
        return system_hint + "\n\n" + _RULE_BLOCK
    return _RULE_BLOCK


class _StreamWriter:
    """Append-only writer for <job_dir>/logs/stream.jsonl.

    Issue #3: run_job used to open/write/close this file once per stream
    event (9~119 events per job in sampled logs). This keeps a single file
    handle open for the job's whole lifetime instead -- one open, buffered
    writes, one close (always via __exit__, exception paths included) --
    while keeping the same one-JSON-object-per-line debugging format.
    """

    def __init__(self, job_dir: Path) -> None:
        self._handle = (job_dir / "logs" / "stream.jsonl").open("a", encoding="utf-8")

    def write(self, event: dict) -> None:
        self._handle.write(json.dumps(event, ensure_ascii=False) + "\n")

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self._handle.close()


def _load_json_object(path: Path) -> dict:
    if not path.exists():
        return {}

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}

    return data if isinstance(data, dict) else {}


def _coerce_workdir(raw: object, default_workdir: Path) -> Path | None:
    if not isinstance(raw, str) or not raw.strip():
        return None

    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = default_workdir / candidate
    if not candidate.is_dir():
        return None
    return candidate.resolve()


def _read_continuation_workdir(job_dir: Path, default_workdir: Path) -> Path:
    for name in (RESULT_META, SESSION_STATE):
        state = _load_json_object(job_dir / name)
        candidate = _coerce_workdir(state.get("workdir"), default_workdir)
        if candidate is not None:
            return candidate
    return default_workdir.resolve()


def _assistant_text(event: dict) -> str:
    message = event.get("message")
    if not isinstance(message, dict):
        return ""

    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""

    parts = [
        block.get("text")
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    return "".join(part for part in parts if isinstance(part, str)).strip()


def allocate_job() -> Path:
    """Reserve a job directory (and with it the job id) and nothing else.

    Split out of ``create_job`` for main.py: the ack it sends the instant a
    message arrives names the job, but the session state that decides the
    job's workdir and resume id must not be read until the job actually holds
    a concurrency slot (otherwise a queued job continues from the
    conversation as it looked before the jobs ahead of it answered). So the
    id is claimed up front and the contents are written later by
    ``prepare_job``.
    """
    job_dir = get_runs_dir() / ("job-" + uuid.uuid4().hex[:8])
    (job_dir / "logs").mkdir(parents=True, exist_ok=True)
    return job_dir.resolve()


def prepare_job(
    job_dir: Path,
    user_text: str,
    workdir: str | None,
    system_hint: str | None,
) -> Path:
    """Write ``prompt.md``/``job.json`` into an already-allocated job dir.

    Raises ``ValueError`` when ``workdir`` does not exist, exactly as
    ``create_job`` does -- it is the same check, just deferred.
    """
    job_id = job_dir.name
    target_workdir = _resolve_target_workdir(workdir)
    claude_cwd = target_workdir or job_dir

    # prompt.md is now just the per-turn variable content -- the user's
    # request plus where things live. The invariant rule block lives in
    # job.json's system_prompt (see _RULE_BLOCK above) and is sent via
    # --append-system-prompt instead, so it never re-accumulates here.
    prompt = f"""사용자 요청:
{user_text}

작업 디렉터리: {claude_cwd}
산출물 디렉터리(첨부 파일·{RESULT_META}·output.md는 모두 여기): {job_dir}
"""

    (job_dir / "prompt.md").write_text(prompt, encoding="utf-8")
    (job_dir / JOB_META).write_text(
        json.dumps(
            {
                "id": job_id,
                "created_at": datetime.now(UTC).isoformat(),
                "workdir": str(target_workdir) if target_workdir else None,
                "job_dir": str(job_dir),
                "system_prompt": _build_system_prompt(system_hint),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return job_dir


def create_job(user_text: str, workdir: str | None, system_hint: str | None) -> Path:
    return prepare_job(allocate_job(), user_text, workdir, system_hint)


async def run_job(
    job_dir: Path,
    resume: str | None = None,
    *,
    on_event: Callable[[dict], None] | None = None,
    timings: JobTimings | None = None,
    scope: JobProcessScope | None = None,
) -> dict:
    prompt = (job_dir / "prompt.md").read_text(encoding="utf-8")
    meta_path = job_dir / JOB_META
    job_meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    claude_cwd = Path(job_meta.get("workdir") or job_dir).expanduser()
    # Read from job.json every call (including a stale-session retry that
    # re-invokes run_job with resume=None on the same job_dir) rather than
    # deriving it from `resume` -- that is what makes the rule block show up
    # again automatically on a resume-less retry with no separate recovery
    # path needed.
    system_prompt = job_meta.get("system_prompt")

    if not claude_cwd.is_dir():
        detail = f"Claude CLI 작업 디렉터리가 없습니다: {claude_cwd}"
        event = {"type": "error", "text": detail, "returncode": None}
        with _StreamWriter(job_dir) as writer:
            # Full path kept here -- stream.jsonl is a local diagnostic log
            # under job_dir, not something that reaches Discord.
            writer.write(event)

        # Issue #26: everything past this point is user-facing (on_event
        # feeds Discord progress edits; the returned dict's "text" is what
        # main.py sends verbatim on a failed job), so redact before either
        # one sees it.
        user_event = {**event, "text": redact_paths(detail)}
        if on_event is not None:
            try:
                on_event(user_event)
            except Exception:
                logger.exception("on_event callback raised for job %s", job_dir)
        result = {**user_event, "text_body": ""}
        if timings is not None:
            result["timings"] = timings.snapshot()
        return result

    last_meta: dict = {}
    session_id = None
    text_body = ""
    first_event_seen = False

    with _StreamWriter(job_dir) as writer:
        async for event in run_claude_stream(
            prompt,
            workdir=str(claude_cwd.resolve()),
            resume=resume,
            system_hint=system_prompt,
            extra_dirs=[str(job_dir.resolve())],
            timings=timings,
            # The processes this job spawns are registered here so a job that
            # blows its timeout can reap its own CLI without touching the
            # processes another channel's job is still using.
            scope=scope,
        ):
            if timings is not None and not first_event_seen:
                first_event_seen = True
                timings.start(SPAN_RESULT)

            event_type = event.get("type")
            if event_type in {"result", "error"}:
                last_meta = event
                if timings is not None:
                    timings.stop(SPAN_RESULT)
                    timings.absorb_result_event(event)
            if event_type == "assistant":
                chunk = _assistant_text(event)
                if chunk:
                    text_body = chunk
            if event.get("session_id"):
                session_id = event["session_id"]

            writer.write(event)

            # Progress display now updates straight from this callback
            # instead of round-tripping through stream.jsonl (issue #3). A
            # broken renderer must never take the job down with it.
            if on_event is not None:
                try:
                    on_event(event)
                except Exception:
                    logger.exception("on_event callback raised for job %s", job_dir)

    last_meta["session_id"] = session_id
    last_meta["workdir"] = str(_read_continuation_workdir(job_dir, claude_cwd))
    last_meta["text_body"] = text_body
    if timings is not None:
        last_meta["timings"] = timings.snapshot()
    return last_meta
