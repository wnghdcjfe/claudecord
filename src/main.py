import asyncio
import logging
import os
from pathlib import Path

import discord
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).resolve().parents[1] / ".env")

from src.auth import ensure_configured, is_authorized
from src.errors import redact_paths, safe_error_text
from src.greetings import direct_reply_for
from src.orchestrator import allocate_job, cleanup_old_runs, prepare_job, run_job
from src.outputs import send_outputs
from src.parser import parse
from src.runner import (
    JobProcessScope,
    TerminationSummary,
    get_warm_pool,
    terminate_active_claude_processes,
    terminate_job_processes,
)
from src.sessions import clear_all_sessions, clear_session, get_session_state, set_session
from src.status import (
    JobProgress,
    format_queued_status,
    format_working_status,
    make_working_gif_file,
    run_spinning_loader,
    stop_spinning_loader,
)
from src.timing import (
    SPAN_ACK,
    SPAN_OUTPUTS,
    SPAN_TOTAL,
    JobTimings,
    debug_timing_enabled,
)

logger = logging.getLogger(__name__)

MISSING_CONVERSATION_MARKER = "No conversation found with session ID"
SHUTDOWN_COMMAND = "종료"

# Where a failed turn keeps its message, depending on how the CLI reported it.
# The cold path surfaces a dead process as {"type": "error", "text": ...}, but
# a warm process reports the same stale-session failure *in band* as
# {"type": "result", "is_error": true, "result": ...} -- reading only "text"
# there meant the stale session was never detected and the channel stayed
# pinned to a conversation that no longer exists.
ERROR_TEXT_KEYS = ("text", "result", "error")

# Issue #6: a runaway Claude session used to pin the ack at "작업중입니다"
# forever, leaving the user to guess whether to keep waiting or type 종료.
# Set to 0 (or a negative value) to opt out of the timeout entirely.
JOB_TIMEOUT_ENV_VAR = "JOB_TIMEOUT_SECONDS"
DEFAULT_JOB_TIMEOUT_SECONDS = 600.0

# Issue #6: discord.py dispatches every message as its own task, so three
# messages in a row used to spawn three claude CLIs that slow each other
# down. Excess jobs now queue behind this gate and say so.
MAX_CONCURRENT_JOBS_ENV_VAR = "MAX_CONCURRENT_JOBS"
DEFAULT_MAX_CONCURRENT_JOBS = 2

# Issue #25: the recommended deployment is launchd / Task Scheduler, so nobody
# is watching a terminal. LOG_LEVEL is how an operator turns the detail up
# after the fact without editing code.
# INFO, not WARNING: with nobody watching a terminal, the normal-flow records
# (which job ran, which session it resumed) are exactly what makes "가끔 대화가
# 안 이어져요" traceable after the fact.
LOG_LEVEL_ENV_VAR = "LOG_LEVEL"
DEFAULT_LOG_LEVEL = logging.INFO
LOG_LEVEL_NAMES = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

# (limit, loop, semaphore) for the gate currently in use, plus the number of
# jobs holding or waiting for a slot. Rebuilt lazily -- see _resolve_job_gate.
_job_gate_state: tuple[int, asyncio.AbstractEventLoop, asyncio.Semaphore] | None = None
_jobs_pending = 0


def _resolve_log_level(raw: str) -> int | None:
    """The logging level `raw` names, or None if it names none.

    None rather than a silent default so the caller can say so out loud: a
    typo here would otherwise hide every log line the operator went looking
    for, with nothing to explain why.
    """
    if not raw:
        return DEFAULT_LOG_LEVEL
    # getLevelName returns the string "Level FOO" for anything it does not
    # know, which is how an unrecognised name is detected here.
    level = logging.getLevelName(raw.upper())
    return level if isinstance(level, int) else None


def _configure_logging() -> None:
    raw = os.environ.get(LOG_LEVEL_ENV_VAR, "").strip()
    level = _resolve_log_level(raw)
    # A typo in LOG_LEVEL is a reason to log more than intended, never a
    # reason for the bot not to start.
    logging.basicConfig(level=DEFAULT_LOG_LEVEL if level is None else level, format=LOG_FORMAT)
    if level is None:
        # Warned after basicConfig so it goes through the handler just
        # installed, rather than logging's lastResort stderr fallback.
        logger.warning(
            "%s=%r is not a log level name; using %s. Expected one of %s.",
            LOG_LEVEL_ENV_VAR,
            raw,
            logging.getLevelName(DEFAULT_LOG_LEVEL),
            ", ".join(LOG_LEVEL_NAMES),
        )


def _job_timeout_seconds() -> float | None:
    raw = os.environ.get(JOB_TIMEOUT_ENV_VAR, "")
    if not str(raw).strip():
        return DEFAULT_JOB_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_JOB_TIMEOUT_SECONDS
    # A non-positive value is an explicit opt-out; asyncio.wait_for(None)
    # then waits as long as the old code did.
    return value if value > 0 else None


def _max_concurrent_jobs() -> int:
    raw = os.environ.get(MAX_CONCURRENT_JOBS_ENV_VAR, "")
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return DEFAULT_MAX_CONCURRENT_JOBS
    return max(1, value)


def _resolve_job_gate() -> asyncio.Semaphore:
    """Semaphore limiting how many jobs run at once, for the running loop.

    Built lazily rather than at import time for two reasons: MAX_CONCURRENT_JOBS
    is read at use time, and an asyncio.Semaphore binds itself to the first
    event loop that awaits it (it raises if reused from another one), so it
    has to be rebuilt whenever the loop changes.
    """
    global _job_gate_state, _jobs_pending

    limit = _max_concurrent_jobs()
    loop = asyncio.get_running_loop()
    if (
        _job_gate_state is None
        or _job_gate_state[0] != limit
        or _job_gate_state[1] is not loop
    ):
        _job_gate_state = (limit, loop, asyncio.Semaphore(limit))
        _jobs_pending = 0
    return _job_gate_state[2]


def _pending_job_count() -> int:
    return _jobs_pending


def _is_missing_conversation_error(meta: dict) -> bool:
    if not (meta.get("type") == "error" or meta.get("is_error")):
        return False
    return any(
        meta.get(key) and MISSING_CONVERSATION_MARKER in str(meta[key])
        for key in ERROR_TEXT_KEYS
    )


async def _run_job_with_session_recovery(
    job_dir: Path,
    *,
    resume_id: str | None,
    channel_id: int,
    explicit_session: bool,
    on_event=None,
    timings: JobTimings | None = None,
    scope: JobProcessScope | None = None,
) -> dict:
    meta = await run_job(
        job_dir, resume=resume_id, on_event=on_event, timings=timings, scope=scope
    )

    if resume_id and not explicit_session and _is_missing_conversation_error(meta):
        clear_session(channel_id)
        if timings is not None:
            # The retry runs the CLI a second time for the same Discord
            # message. Without this, its spans would overwrite the first
            # attempt's and timings.json would report the round trip as
            # faster than the user actually experienced.
            timings.start_attempt(reason="stale_session_retry")
        meta = await run_job(
            job_dir, resume=None, on_event=on_event, timings=timings, scope=scope
        )
        meta["retried_without_stale_session"] = True
        meta["stale_session_id"] = resume_id

    return meta


def _resolve_resume_and_workdir(
    cmd, channel_id: int
) -> tuple[str | None, str | None, str | None, bool]:
    """Resolve (resume_id, workdir, system_hint, explicit_session) for a turn.

    The hint is resolved here, not taken straight from the parsed command,
    because only the turn that carries the ``@project`` tag has one. The
    project's rules are re-sent to the CLI on every turn (they ride
    ``--append-system-prompt``, which is not persisted into the resumed
    transcript), so an untagged follow-up with no restored hint would keep
    the project's working directory while quietly dropping its constraints.
    A tag on *this* message still wins, and the resolved value is what gets
    written back to the session by _execute_job.
    """
    explicit_session = cmd.session_id is not None
    if explicit_session:
        return cmd.session_id, cmd.workdir, cmd.system_hint, True

    session_state = get_session_state(channel_id)
    resume_id = session_state.session_id if session_state else None
    workdir = cmd.workdir or (session_state.workdir if session_state else None)
    system_hint = cmd.system_hint or (session_state.system_hint if session_state else None)
    return resume_id, workdir, system_hint, False


async def _release_channel_warm_process(session_id: str | None) -> int:
    """Reclaim the warm claude process parked with ``session_id``.

    /clear tells the user the conversation is gone, but the warm process
    holding that conversation in memory would otherwise stay parked until its
    idle TTL (300s by default) -- the answer and the reality disagree. Drop it
    from the pool so no later turn can be handed it, and close its stdin so
    the CLI exits. Only this channel's own conversation is touched: every
    other parked process belongs to a different session id and is left alone.

    ``forget`` already cancels the entry's TTL task, and the stderr drain is
    deliberately left running: it ends by itself at EOF when the process
    exits, and until then it keeps draining -- cancelling it first could let
    a chatty shutdown fill the 64KB stderr pipe and block the very exit we
    just asked for.
    """
    if not session_id:
        return 0

    pool = get_warm_pool()
    released = 0
    for wp in pool.idle_processes():
        if wp.session_id != session_id:
            continue
        pool.forget(wp)
        wp.close_stdin()
        released += 1
    return released


def _is_shutdown_command(text: str) -> bool:
    return text.strip() == SHUTDOWN_COMMAND


def _format_shutdown_reply(
    summary: TerminationSummary,
    *,
    cleared_sessions: int,
) -> str:
    killed_note = f" (강제 종료 {summary.killed}개)" if summary.killed else ""
    return (
        "Claude 세션 종료 완료.\n"
        f"- 실행 중이던 Claude CLI 프로세스: {summary.terminated}/{summary.requested}개 종료"
        f"{killed_note}\n"
        f"- 저장된 대화 세션: {cleared_sessions}개 초기화"
    )


async def _shutdown_claude_sessions() -> tuple[TerminationSummary, int]:
    summary = await terminate_active_claude_processes()
    cleared_sessions = clear_all_sessions()
    return summary, cleared_sessions


async def _safe_edit_ack(ack: discord.Message, content: str) -> None:
    """Best-effort edit of the ack message with its final status.

    A failure here (message deleted, permission change, transient network
    error) must never prevent delivering the actual job result -- the
    caller always proceeds to send the error text / call send_outputs
    regardless of whether this succeeded.
    """
    try:
        await ack.edit(content=content)
    except (discord.DiscordException, OSError):
        logger.warning("Failed to edit ack message with final status", exc_info=True)


async def _complete_job(
    ack: discord.Message,
    status_line: str,
    job_dir: Path,
    timings: JobTimings,
) -> None:
    """Close out issue #7 instrumentation for a job, however it ended.

    Runs on the success, failure *and* timeout paths -- a job that blew its
    budget is exactly the one whose span breakdown is worth having.
    """
    timings.stop(SPAN_TOTAL)
    timings.write(job_dir)
    summary = timings.format_line()
    logger.info("job %s timings: %s", job_dir.name, summary)
    if debug_timing_enabled():
        await _safe_edit_ack(ack, f"{status_line}\n{summary}")


@client.event
async def on_ready():
    logger.info("logged in as %s", client.user)

    # Issue #22: startup is the only sweep this bot gets, so it has to happen
    # here. Off the event loop, though: this is an rmtree over up to a
    # retention window's worth of job directories (each with its own outputs
    # and logs), and on_ready shares the loop with discord.py's gateway
    # heartbeat -- a long blocking delete there reads as a dead connection and
    # drops the session. Same reason the job-file writes moved off the loop in
    # issue #3.
    #
    # Deliberately not wrapped: cleanup_old_runs catches and logs every one of
    # its own failures, so a try/except here would only obscure that contract.
    removed = await asyncio.to_thread(cleanup_old_runs)
    if removed:
        logger.info("removed %s stale run directories", removed)


@client.event
async def on_message(msg: discord.Message):
    if not is_authorized(msg):
        return
    if not msg.clean_content.strip():
        return

    # Issue #7: the clock starts the moment the message lands, so t_ack and
    # t_total measure the wait the user actually experienced. job_id is
    # filled in below, once allocate_job has named the job.
    timings = JobTimings()
    timings.start(SPAN_TOTAL)
    timings.start(SPAN_ACK)

    text = msg.clean_content.strip()

    if text == "/clear":
        state = get_session_state(msg.channel.id)
        clear_session(msg.channel.id)
        await _release_channel_warm_process(state.session_id if state else None)
        await msg.reply("세션을 초기화했습니다. 다음 메시지부터 새 대화로 시작합니다.")
        return

    if _is_shutdown_command(text):
        summary, cleared_sessions = await _shutdown_claude_sessions()
        await msg.reply(
            _format_shutdown_reply(summary, cleared_sessions=cleared_sessions)
        )
        return

    direct_reply = direct_reply_for(text)
    if direct_reply:
        await msg.reply(direct_reply)
        return

    cmd = parse(text)
    # Only the id is claimed here -- the ack below names the job, but what the
    # job actually runs against is decided inside the gate (see _dispatch_job).
    # Even this much is real disk I/O (two mkdirs) sitting in front of the ack
    # the user is waiting on, so it goes off the event loop thread (issue #3).
    job_dir = await asyncio.to_thread(allocate_job)
    timings.job_id = job_dir.name

    await _dispatch_job(msg, cmd, job_dir, timings)


async def _dispatch_job(
    msg: discord.Message,
    cmd,
    job_dir: Path,
    timings: JobTimings,
) -> None:
    """Ack, wait for a concurrency slot, then build and run the job.

    The ack goes out *before* the gate is awaited so a queued job says so
    immediately (issue #6) instead of looking indistinguishable from a
    running one. The spinner only starts once the slot is actually held --
    it edits the ack every 2.5s, so starting it earlier would overwrite the
    "대기 중" notice with "작업중입니다" while nothing was running.

    Everything else, though, waits for the slot. Resolving the session before
    the gate froze resume_id at the value it had when the message arrived, so
    with MAX_CONCURRENT_JOBS=1 a queued turn resumed the conversation as it
    looked *before* the turn ahead of it answered -- the queue only appeared
    to serialize the channel. The job's contents are therefore written by
    prepare_job below, once the slot is held.
    """
    global _jobs_pending

    gate = _resolve_job_gate()
    jobs_ahead = _pending_job_count()
    queued = gate.locked()
    _jobs_pending += 1
    try:
        if queued:
            ack = await msg.reply(format_queued_status(job_dir.name, jobs_ahead))
        else:
            # Ack goes out immediately: no GIF upload blocks it unless
            # WORKING_GIF opts in (see src/status.py).
            working_gif = make_working_gif_file()
            if working_gif:
                ack = await msg.reply(format_working_status(job_dir.name), file=working_gif)
            else:
                ack = await msg.reply(format_working_status(job_dir.name))
        timings.stop(SPAN_ACK)

        async with gate:
            resolved = await _prepare_queued_job(msg, ack, cmd, job_dir, timings)
            if resolved is None:
                return
            resume_id, system_hint, explicit_session = resolved

            # Progress is driven by a JobProgress object that run_job fills in
            # directly via on_event, not by a fake looping animation and no
            # longer by re-reading the job's stream log (issue #3).
            progress = JobProgress()
            if queued:
                await _safe_edit_ack(ack, format_working_status(job_dir.name, progress))
            await _execute_job(
                msg,
                ack,
                job_dir,
                progress,
                timings,
                resume_id=resume_id,
                system_hint=system_hint,
                explicit_session=explicit_session,
            )
    finally:
        # Clamped: _resolve_job_gate resets the counter when it rebuilds the
        # gate (a changed limit), which can otherwise leave in-flight jobs
        # decrementing past zero and reporting a negative backlog.
        _jobs_pending = max(0, _jobs_pending - 1)


async def _prepare_queued_job(
    msg: discord.Message,
    ack: discord.Message,
    cmd,
    job_dir: Path,
    timings: JobTimings,
) -> tuple[str | None, str | None, bool] | None:
    """Resolve the session and write the job's files, now that the slot is held.

    Returns ``(resume_id, system_hint, explicit_session)``, or ``None`` when
    the turn cannot run at all -- in which case the user has already been told
    why and the caller must not proceed.
    """
    resume_id, workdir, system_hint, explicit_session = _resolve_resume_and_workdir(
        cmd, msg.channel.id
    )
    # prompt.md + job.json are synchronous writes; they no longer sit in front
    # of the ack at all (that is the point of the allocate/prepare split), and
    # they run off the event loop thread so they cannot stall other channels'
    # jobs either.
    try:
        await asyncio.to_thread(prepare_job, job_dir, cmd.prompt, workdir, system_hint)
    except ValueError as exc:
        # A remembered working directory that has since been deleted must not
        # strand the channel: drop the session and run the turn from scratch.
        if resume_id and workdir and cmd.workdir is None and not explicit_session:
            clear_session(msg.channel.id)
            await asyncio.to_thread(prepare_job, job_dir, cmd.prompt, None, system_hint)
            return None, system_hint, explicit_session

        # The full message (with the real path) goes to the log, where the
        # operator can read it; the channel gets the redacted form. Issue #26:
        # prepare_job names the missing directory by absolute path, and a
        # Discord message is permanent.
        logger.warning("job %s could not be prepared: %s", job_dir.name, exc)
        status_line = f"작업 실패 · {job_dir.name}"
        await _safe_edit_ack(ack, status_line)
        await msg.channel.send(f"실행 불가: {redact_paths(str(exc))}")
        await _complete_job(ack, status_line, job_dir, timings)
        return None

    return resume_id, system_hint, explicit_session


async def _execute_job(
    msg: discord.Message,
    ack: discord.Message,
    job_dir: Path,
    progress: JobProgress,
    timings: JobTimings,
    *,
    resume_id: str | None,
    system_hint: str | None,
    explicit_session: bool,
) -> None:
    loader_task = asyncio.create_task(run_spinning_loader(ack, job_dir.name, progress))
    timeout = _job_timeout_seconds()
    # One scope per job: on timeout it is the *only* thing reaped, so a
    # runaway job in this channel cannot kill a healthy job in another one.
    scope = JobProcessScope()

    # Task cleanup lives in `finally` so it always runs -- whether run_job
    # succeeds, raises, or the spinner task itself dies -- and never skips the
    # send_outputs() call below (stop_spinning_loader itself no longer raises
    # for a crashed background task; see status.py).
    try:
        meta = await asyncio.wait_for(
            _run_job_with_session_recovery(
                job_dir,
                resume_id=resume_id,
                channel_id=msg.channel.id,
                explicit_session=explicit_session,
                on_event=progress.record_event,
                timings=timings,
                scope=scope,
            ),
            timeout=timeout,
        )
        timings.start(SPAN_OUTPUTS)
    except TimeoutError:
        # N1 (below) applies here too: stop the spinner before any edit.
        await stop_spinning_loader(loader_task)
        await _handle_job_timeout(msg, ack, job_dir, timings, timeout, scope)
        return
    except Exception as exc:
        # N1: stop the loader *before* editing the ack with the failure
        # status. If it were still alive, the ack.edit below is a real
        # network round-trip during which the loader could tick and issue
        # its own (stale "작업중입니다") edit that completes afterward,
        # clobbering the failure message the user actually sees.
        await stop_spinning_loader(loader_task)
        # Issue #26: `str(exc)` on this path is routinely a FileNotFoundError
        # or PermissionError carrying an absolute path, i.e. the operator's
        # account name and the layout of their disk. The stack trace and the
        # unredacted message stay in the log; the channel gets the type name,
        # the redacted message, and the job id to look the rest up by.
        logger.exception("job %s raised while running", job_dir.name)
        status_line = f"작업 실패 · {job_dir.name}"
        await _safe_edit_ack(ack, status_line)
        await msg.channel.send(f"내부 오류: {safe_error_text(exc)} (`{job_dir.name}`)")
        await _complete_job(ack, status_line, job_dir, timings)
        return
    finally:
        # Cheap no-op re-stop of loader_task on the exception paths above;
        # the only path that reaches here for the first time on success.
        await stop_spinning_loader(loader_task)

    if meta.get("session_id"):
        # Storing the hint is what keeps a project's rules attached to the
        # conversation on later, untagged turns (see
        # _resolve_resume_and_workdir). sessions.set_session logs and
        # swallows a write failure rather than raising -- losing the answer
        # below over a bookkeeping error would be far worse.
        set_session(
            msg.channel.id,
            meta["session_id"],
            workdir=meta.get("workdir"),
            system_hint=system_hint,
        )

    failed = meta.get("type") == "error" or meta.get("is_error")
    status = "작업 실패" if failed else "작업 완료"
    status_line = f"{status} · {job_dir.name}"
    await _safe_edit_ack(ack, status_line)
    if failed and meta.get("text"):
        # The CLI's own failure text names the directory it could not use, so
        # it needs the same redaction (#26). Redacted before truncation --
        # slicing first could cut a path in half and hand the tail through
        # unmatched.
        logger.warning("job %s failed: %s", job_dir.name, meta["text"])
        await msg.channel.send(redact_paths(str(meta["text"]))[:1900])
    await send_outputs(
        msg.channel,
        job_dir,
        body_text=meta.get("text_body"),
        warn_missing_manifest=not failed,
    )
    timings.stop(SPAN_OUTPUTS)
    await _complete_job(ack, status_line, job_dir, timings)


async def _handle_job_timeout(
    msg: discord.Message,
    ack: discord.Message,
    job_dir: Path,
    timings: JobTimings,
    timeout: float | None,
    scope: JobProcessScope,
) -> None:
    """Issue #6: wait_for cancelled the stream mid-flight, so the claude CLI
    it spawned is still alive and no longer being read. Reap it, tell the
    user the job hit the ceiling, and hand over whatever the job managed to
    write before it was cut off.

    Scoped to this job's own processes on purpose. The global reap belongs to
    the 종료 command, where killing everything is what the user asked for;
    here it meant one channel's runaway job SIGTERM'd another channel's
    perfectly healthy one, whose user then got "작업 실패" for no reason.
    """
    logger.warning(
        "job %s exceeded JOB_TIMEOUT_SECONDS=%s; terminating its claude processes",
        job_dir.name,
        timeout,
    )
    summary = await terminate_job_processes(scope)
    logger.warning("job %s timeout cleanup: %s", job_dir.name, summary)

    timings.set_meta(timed_out=True, timeout_seconds=timeout)
    status_line = f"작업 시간 초과 · {job_dir.name}"

    timings.start(SPAN_OUTPUTS)
    await _safe_edit_ack(ack, status_line)
    # warn_missing_manifest=False: "결과가 없습니다" would just restate the
    # ack the user is already looking at.
    await send_outputs(msg.channel, job_dir, body_text=None, warn_missing_manifest=False)
    timings.stop(SPAN_OUTPUTS)
    await _complete_job(ack, status_line, job_dir, timings)


def main():
    # Logging first, so a configuration failure below is itself reportable.
    _configure_logging()
    # Issue #9: bad or missing auth config must stop the bot here, with a
    # Korean message, rather than silently rejecting every message later.
    ensure_configured()
    # Same contract as ensure_configured(): a missing token is a configuration
    # error the operator should read, not a KeyError traceback. ensure_configured
    # does not cover it because auth.py is about who may talk to the bot, not
    # about the bot's own credential.
    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        raise RuntimeError("환경변수 DISCORD_BOT_TOKEN이 필요합니다.")
    client.run(token)


if __name__ == "__main__":
    main()
