import asyncio
import contextlib
import json
import os
import shutil
import signal
import subprocess
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from pathlib import Path

from src.timing import SPAN_FIRST_EVENT, SPAN_SPAWN, JobTimings
from src.warm_pool import WarmClaudePool, WarmKey, WarmProcess, warm_enabled

# NOTE: --permission-mode bypassPermissions is intentionally kept, but read
# this before treating the two tool lists below as a security boundary. They
# are not one. Measured against Claude Code 2.1.251 (issue #11); an earlier
# version of this comment drew the opposite conclusion from the first finding
# alone, so the limits are spelled out here rather than left to inference.
#
#   BLOCKED_TOOLS *is* enforced under bypassPermissions. `rm foo`,
#   `curl --version` and `echo hi && rm foo` all produce a `permission_denied`
#   system event and an `is_error: true` tool_result -- the CLI parses
#   `&&`-joined subcommands (decision_reason_type: subcommandResults). It
#   genuinely stops an accidental `rm`, which is the most likely way this bot
#   destroys something.
#
#   But it matches on the command's leading string. `/bin/rm foo` and
#   `python3 -c "import os; os.remove('foo')"` both ran and both deleted the
#   file. This is a guardrail against mistakes, not a barrier against intent.
#
#   SAFE_TOOLS has no effect here at all. --allowedTools is a pre-approval
#   list ("don't ask about these"), and bypassPermissions already approves
#   everything, so the session keeps all builtin tools -- Bash, WebFetch,
#   WebSearch, Task included. The `init` event reports 28 of them, not the 11
#   named below. Under the *default* permission mode the same flag does gate
#   mutating Bash commands, which is why it is kept rather than deleted.
#
#   What bypassPermissions actually switches off is the filesystem write
#   sandbox: writes outside cwd and outside every --add-dir path succeed with
#   no permission_denied event. The default mode refuses them.
#
# The real boundary is src/auth.py (owner + channel allowlist). README's
# "보안 모델" section states this for users.

DEFAULT_CLAUDE_MODEL = "sonnet"
DEFAULT_STREAM_LIMIT_BYTES = 8 * 1024 * 1024  # 8MiB

CLAUDE_PERMISSION_MODE = "bypassPermissions"

# Whether a parked warm process may serve a turn that asked for a *different*
# --add-dir set. Verified empirically (2026-08-31): under
# --permission-mode bypassPermissions the CLI writes outside its cwd and outside
# every --add-dir path with no permission_denied event, i.e. --add-dir grants
# nothing extra here. Since orchestrator passes a fresh per-job directory on
# every turn, keying on it would mean the pool never hits. Tying this to the
# permission mode means a future change back to a checking mode automatically
# restores extra_dirs to the reuse key instead of silently reusing a process
# that lacks access to the new job directory.
WARM_KEY_IGNORES_EXTRA_DIRS = CLAUDE_PERMISSION_MODE == "bypassPermissions"

SAFE_TOOLS = ",".join(
    [
        "Read",
        "Edit",
        "Write",
        "Glob",
        "Grep",
        "Bash(git status:*)",
        "Bash(git log:*)",
        "Bash(git diff:*)",
        "Bash(npm test:*)",
        "Bash(pytest:*)",
        "Bash(uv run:*)",
    ]
)

WINDOWS_BATCH_EXTENSIONS = {".bat", ".cmd"}
TERMINATE_TIMEOUT_SECONDS = 5.0

# How long a warm turn that ended with is_error waits to see whether the process
# is dying, so its exit code and stderr can be reported like the cold path does.
WARM_ERROR_EXIT_GRACE_SECONDS = 2.0

_ACTIVE_CLAUDE_PROCESSES: set[asyncio.subprocess.Process] = set()


@dataclass(frozen=True)
class TerminationSummary:
    requested: int
    terminated: int
    killed: int
    still_running: int


class JobProcessScope:
    """The claude processes one job obtained.

    ``종료`` killing every claude process is intended; a *job timeout* doing the
    same is not -- it SIGTERMed healthy jobs in other channels. A job carries
    one scope, ``run_claude_stream`` registers whatever process it ends up
    using, and ``terminate_job_processes`` reaps exactly those.
    """

    __slots__ = ("_procs",)

    def __init__(self) -> None:
        self._procs: set[asyncio.subprocess.Process] = set()

    def add(self, proc: asyncio.subprocess.Process) -> None:
        self._procs.add(proc)

    def discard(self, proc: asyncio.subprocess.Process) -> None:
        self._procs.discard(proc)

    def live(self) -> list[asyncio.subprocess.Process]:
        exited = {proc for proc in self._procs if proc.returncode is not None}
        self._procs.difference_update(exited)
        return [proc for proc in self._procs if proc.returncode is None]

    def __len__(self) -> int:
        return len(self._procs)


def _live_claude_processes() -> list[asyncio.subprocess.Process]:
    exited = {proc for proc in _ACTIVE_CLAUDE_PROCESSES if proc.returncode is not None}
    _ACTIVE_CLAUDE_PROCESSES.difference_update(exited)
    return [proc for proc in _ACTIVE_CLAUDE_PROCESSES if proc.returncode is None]


def get_active_claude_process_count() -> int:
    return len(_live_claude_processes())


async def _wait_for_process_exit(
    proc: asyncio.subprocess.Process,
    timeout: float,
) -> bool:
    try:
        await asyncio.wait_for(proc.wait(), timeout=timeout)
    except TimeoutError:
        return False
    except ProcessLookupError:
        return True
    return proc.returncode is not None


def _terminate_process(proc: asyncio.subprocess.Process) -> None:
    if os.name != "nt":
        os.killpg(proc.pid, signal.SIGTERM)
        return
    proc.terminate()


def _kill_process(proc: asyncio.subprocess.Process) -> None:
    if os.name != "nt":
        os.killpg(proc.pid, signal.SIGKILL)
        return
    proc.kill()


async def _cleanup_runaway_process(
    proc: asyncio.subprocess.Process,
    timeout: float = TERMINATE_TIMEOUT_SECONDS,
) -> None:
    """Best-effort termination for a process we stopped reading from mid-stream
    (e.g. a stdout line exceeded the configured limit and stdout iteration
    raised). Without this, the process keeps running as an orphan that
    _ACTIVE_CLAUDE_PROCESSES no longer tracks, so
    terminate_active_claude_processes can't find or kill it later."""
    if proc.returncode is not None:
        return
    try:
        _terminate_process(proc)
    except ProcessLookupError:
        return
    if await _wait_for_process_exit(proc, timeout):
        return
    try:
        _kill_process(proc)
    except ProcessLookupError:
        return
    await _wait_for_process_exit(proc, timeout)


async def terminate_active_claude_processes(
    timeout: float = TERMINATE_TIMEOUT_SECONDS,
) -> TerminationSummary:
    """Kill *every* live claude process. This is the Discord ``종료`` command:
    the global blast radius is the point. A job timeout must use
    ``terminate_job_processes`` instead."""
    # Drain the warm pool first. Parked processes stay registered in
    # _ACTIVE_CLAUDE_PROCESSES for their whole life, so the loop below already
    # kills them -- but they must also be dropped from the pool's bookkeeping,
    # or a later turn would be handed a process that 종료 just killed.
    for wp in get_warm_pool().reset():
        wp.close_stdin()

    return await _terminate_processes(_live_claude_processes(), timeout)


async def terminate_job_processes(
    scope: JobProcessScope,
    timeout: float = TERMINATE_TIMEOUT_SECONDS,
) -> TerminationSummary:
    """Kill only the processes ``scope`` collected, leaving every other job's
    alone. Any of them that is parked is dropped from the warm pool first: a
    process this job started must never be handed to the next turn once we have
    decided to kill it."""
    processes = scope.live()

    for wp in get_warm_pool().discard_processes(processes):
        wp.close_stdin()

    summary = await _terminate_processes(processes, timeout)

    # Same rule as the global set: a survivor stays in the scope, so a later
    # attempt on this job can still reach it.
    for proc in processes:
        if proc.returncode is not None:
            scope.discard(proc)
    return summary


async def _terminate_processes(
    processes: list[asyncio.subprocess.Process],
    timeout: float,
) -> TerminationSummary:
    requested = len(processes)

    for proc in processes:
        # Already gone between the caller's snapshot and now -- that is the
        # outcome we wanted anyway.
        with contextlib.suppress(ProcessLookupError):
            _terminate_process(proc)

    exited = await asyncio.gather(
        *(_wait_for_process_exit(proc, timeout) for proc in processes),
        return_exceptions=False,
    )
    # strict=True: `exited` is gather() over `processes`, so a length
    # mismatch would mean a logic error, not a short input.
    stubborn = [
        proc for proc, did_exit in zip(processes, exited, strict=True) if not did_exit
    ]

    killed = 0
    for proc in stubborn:
        try:
            _kill_process(proc)
            killed += 1
        except ProcessLookupError:
            pass

    if stubborn:
        await asyncio.gather(
            *(_wait_for_process_exit(proc, timeout) for proc in stubborn),
            return_exceptions=False,
        )

    reaped = [proc for proc in processes if proc.returncode is not None]
    terminated = len(reaped)
    # Untrack only what actually died. A process that shrugged off SIGTERM *and*
    # SIGKILL is reported as still_running, and dropping it here as well would
    # make it invisible to every later 종료 -- unkillable for the rest of the
    # bot's life, holding a model session nobody can reach.
    _ACTIVE_CLAUDE_PROCESSES.difference_update(reaped)
    still_running = requested - terminated

    return TerminationSummary(
        requested=requested,
        terminated=terminated,
        killed=killed,
        still_running=still_running,
    )


def _resolve_claude_executable() -> str:
    configured = os.environ.get("CLAUDE_BIN")
    if configured:
        return str(Path(configured).expanduser())

    candidates = ["claude"]
    if os.name == "nt":
        candidates = ["claude.exe", "claude.cmd", "claude.bat", "claude"]

    for candidate in candidates:
        resolved = shutil.which(candidate)
        if resolved:
            return resolved

    return "claude"


def _resolve_claude_model() -> str:
    return os.environ.get("CLAUDE_MODEL") or DEFAULT_CLAUDE_MODEL


def _resolve_stream_limit_bytes() -> int:
    configured = os.environ.get("CLAUDE_STREAM_LIMIT_BYTES")
    if not configured:
        return DEFAULT_STREAM_LIMIT_BYTES
    try:
        value = int(configured)
    except ValueError:
        return DEFAULT_STREAM_LIMIT_BYTES
    return value if value > 0 else DEFAULT_STREAM_LIMIT_BYTES


def _wrap_windows_batch_command(cmd: list[str]) -> list[str]:
    if os.name != "nt":
        return cmd

    suffix = Path(cmd[0]).suffix.lower()
    if suffix not in WINDOWS_BATCH_EXTENSIONS:
        return cmd

    command_line = subprocess.list2cmdline(cmd)
    return [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c", command_line]

BLOCKED_TOOLS = ",".join(
    [
        "Bash(rm:*)",
        "Bash(sudo:*)",
        "Bash(dd:*)",
        "Bash(mkfs:*)",
        "Bash(chmod -R 777:*)",
        "Bash(git push --force:*)",
        "Bash(curl:*)",
        "Bash(wget:*)",
    ]
)


def build_claude_command(
    prompt: str,
    *,
    resume: str | None = None,
    system_hint: str | None = None,
    extra_dirs: Iterable[str] | None = None,
    stream_input: bool = False,
) -> list[str]:
    """Build the CLI invocation.

    With ``stream_input=True`` the prompt is *not* passed as an argument: the
    process reads newline-delimited user messages from stdin instead, which is
    what lets one process serve several turns (see ``src.warm_pool``).
    """
    cmd = [_resolve_claude_executable(), "-p"]
    if extra_dirs:
        cmd += ["--add-dir", *extra_dirs]

    if stream_input:
        cmd += ["--input-format", "stream-json"]

    cmd += [
        "--setting-sources",
        "local",
        "--output-format",
        "stream-json",
        "--verbose",
        "--model",
        _resolve_claude_model(),
        "--permission-mode",
        CLAUDE_PERMISSION_MODE,
        f"--allowedTools={SAFE_TOOLS}",
        f"--disallowedTools={BLOCKED_TOOLS}",
    ]

    if resume:
        cmd += ["--resume", resume]
    if system_hint:
        cmd += ["--append-system-prompt", system_hint]
    if not stream_input:
        cmd += ["--", prompt]
    return _wrap_windows_batch_command(cmd)


async def _spawn_claude_process(
    prompt: str,
    *,
    workdir: str | None,
    resume: str | None,
    system_hint: str | None,
    extra_dirs: Iterable[str] | None,
    stream_input: bool,
) -> asyncio.subprocess.Process:
    subprocess_kwargs = {}
    if os.name != "nt":
        subprocess_kwargs["start_new_session"] = True

    return await asyncio.create_subprocess_exec(
        *build_claude_command(
            prompt,
            resume=resume,
            system_hint=system_hint,
            extra_dirs=extra_dirs,
            stream_input=stream_input,
        ),
        cwd=workdir,
        # The cold path MUST keep stdin=DEVNULL: with stdin left open the CLI
        # waits an extra 3s and warns "no stdin data received in 3s" (issue #1).
        # The warm path keeps a pipe on purpose -- that pipe *is* the mechanism.
        stdin=asyncio.subprocess.PIPE if stream_input else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=_resolve_stream_limit_bytes(),
        **subprocess_kwargs,
    )


def _warm_key(
    workdir: str | None,
    system_hint: str | None,
    extra_dirs: tuple[str, ...],
) -> WarmKey:
    return WarmKey(
        workdir=str(workdir) if workdir else None,
        model=_resolve_claude_model(),
        system_hint=system_hint,
        extra_dirs=() if WARM_KEY_IGNORES_EXTRA_DIRS else extra_dirs,
    )


_WARM_POOL: WarmClaudePool | None = None


def get_warm_pool() -> WarmClaudePool:
    global _WARM_POOL
    if _WARM_POOL is None:
        _WARM_POOL = WarmClaudePool(retire=_retire_warm_process)
    return _WARM_POOL


async def _retire_warm_process(wp: WarmProcess) -> None:
    """Permanently dispose of a warm process (evicted, expired, or broken)."""
    wp.cancel_expiry()
    wp.close_stdin()
    await _cleanup_runaway_process(wp.proc)
    wp.cancel_stderr_drain()
    _ACTIVE_CLAUDE_PROCESSES.discard(wp.proc)


def _warm_failure_text(wp: WarmProcess) -> str:
    tail = wp.stderr_tail()[:1500]
    return tail or "claude CLI 프로세스가 종료되었습니다."


def _inband_error_text(event: dict) -> str:
    """The failure text a `result` event carries. The real CLI puts it under
    `result`; `error`/`text` are accepted for the shapes the fakes and older
    CLI versions emit."""
    for field in ("result", "error", "text"):
        value = event.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()[:1500]
    return ""


def _warm_grace_exceeded_text(event: dict, wp: WarmProcess) -> str:
    detail = "\n".join(
        part for part in (_inband_error_text(event), wp.stderr_tail()[:1500]) if part
    )
    note = (
        "claude CLI가 오류로 끝난 뒤 "
        f"{WARM_ERROR_EXIT_GRACE_SECONDS}초 안에 종료되지 않아 프로세스를 폐기했습니다."
    )
    return f"{detail}\n{note}" if detail else note


async def _await_warm_exit(
    wp: WarmProcess,
    timeout: float = TERMINATE_TIMEOUT_SECONDS,
) -> int | None:
    """stdout hit EOF mid-turn: let the process finish so the error event can
    carry its real exit code and complete stderr, like the cold path's does."""
    await _wait_for_process_exit(wp.proc, timeout)
    await wp.wait_stderr_drained()
    return wp.proc.returncode


@dataclass
class _WarmTurn:
    wp: WarmProcess
    first_event: dict
    warm: bool


async def _abandon_warm_process(
    pool: WarmClaudePool,
    wp: WarmProcess,
    scope: JobProcessScope | None,
) -> None:
    """Drop a warm process for good: no pool entry, no scope entry, no process."""
    pool.forget(wp)
    if scope is not None:
        scope.discard(wp.proc)
    await _retire_warm_process(wp)


async def _start_warm_turn(
    prompt: str,
    *,
    workdir: str | None,
    resume: str | None,
    system_hint: str | None,
    extra_dirs: tuple[str, ...],
    timings: JobTimings | None,
    scope: JobProcessScope | None,
) -> _WarmTurn | None:
    """Get a streaming-input process and pull its first stdout event.

    Returns ``None`` when anything failed *before* a single event could reach
    the caller. That is the whole contract of this function: the caller can then
    run the ordinary cold path and the warm attempt stays invisible. Once the
    first event exists we are committed, and later failures are reported as
    error events exactly like the cold path reports them.
    """
    pool = get_warm_pool()
    key = _warm_key(workdir, system_hint, extra_dirs)

    if timings is not None:
        timings.start(SPAN_SPAWN)

    wp = pool.acquire(key, resume)
    warm = wp is not None
    if wp is None:
        try:
            proc = await _spawn_claude_process(
                prompt,
                workdir=workdir,
                resume=resume,
                system_hint=system_hint,
                extra_dirs=extra_dirs,
                stream_input=True,
            )
        except (FileNotFoundError, OSError):
            if timings is not None:
                timings.stop(SPAN_SPAWN)
            return None
        if proc.stdout is None or proc.stdin is None:
            await _cleanup_runaway_process(proc)
            if timings is not None:
                timings.stop(SPAN_SPAWN)
            return None
        _ACTIVE_CLAUDE_PROCESSES.add(proc)
        wp = WarmProcess(proc, key)
        wp.start_stderr_drain()

    if scope is not None:
        scope.add(wp.proc)

    if timings is not None:
        timings.stop(SPAN_SPAWN)
        timings.set_meta(warm=warm)
        timings.start(SPAN_FIRST_EVENT)

    try:
        await wp.send_user_message(prompt)
        first_event = await wp.read_event()
    except (OSError, RuntimeError, ValueError, asyncio.LimitOverrunError):
        await _abandon_warm_process(pool, wp, scope)
        return None

    if first_event is None:
        await _abandon_warm_process(pool, wp, scope)
        return None

    if timings is not None:
        timings.stop(SPAN_FIRST_EVENT)
    return _WarmTurn(wp, first_event, warm)


async def _stream_warm_turn(
    turn: _WarmTurn,
    scope: JobProcessScope | None = None,
) -> AsyncIterator[dict]:
    wp = turn.wp
    session_id = wp.session_id
    completed = False
    try:
        event: dict | None = turn.first_event
        while True:
            if event is None:
                returncode = await _await_warm_exit(wp)
                yield {
                    "type": "error",
                    "text": _warm_failure_text(wp),
                    "returncode": returncode,
                }
                return
            if event.get("session_id"):
                session_id = event["session_id"]
            yield event
            if event.get("type") == "result":
                if event.get("is_error"):
                    # A failed turn is reported in-band, but a *fatal* one (a
                    # stale --resume, say) also kills the process a beat later
                    # and writes the reason to stderr. The cold path turns that
                    # into a trailing error event, and consumers depend on it:
                    # main.py's stale-session recovery keys off the
                    # "No conversation found with session ID" text. The warm
                    # process outliving its turn must not swallow that.
                    returncode = await _await_warm_exit(
                        wp, timeout=WARM_ERROR_EXIT_GRACE_SECONDS
                    )
                    if returncode is None:
                        # Grace exceeded. The cap stays (waiting forever would
                        # pin a warm process on a CLI that is simply slow to
                        # exit), but going quiet here would drop the only copy
                        # of the reason: the process is discarded below either
                        # way, and main.py's stale-session recovery reads the
                        # marker off a trailing error event. Carry the in-band
                        # result text -- and whatever stderr was drained so far
                        # -- out instead of swallowing it.
                        yield {
                            "type": "error",
                            "text": _warm_grace_exceeded_text(event, wp),
                            "returncode": None,
                        }
                    elif returncode != 0:
                        yield {
                            "type": "error",
                            "text": _warm_failure_text(wp),
                            "returncode": returncode,
                        }
                    # Never park after an errored turn: the process may be dead
                    # or in an undefined state, and reusing it would strand the
                    # next turn on it.
                    return
                completed = True
                return
            event = await wp.read_event()
    except (ValueError, asyncio.LimitOverrunError) as exc:
        yield {
            "type": "error",
            "text": f"claude CLI 출력 스트림 처리 실패(라인 리밋 초과 가능성): {exc}",
            "returncode": None,
        }
    finally:
        pool = get_warm_pool()
        if completed:
            wp.session_id = session_id
            wp.turns += 1
            for retiring in pool.park(wp):
                if scope is not None:
                    scope.discard(retiring.proc)
                await _retire_warm_process(retiring)
        else:
            await _abandon_warm_process(pool, wp, scope)


async def run_claude_stream(
    prompt: str,
    workdir: str | None = None,
    resume: str | None = None,
    system_hint: str | None = None,
    extra_dirs: Iterable[str] | None = None,
    *,
    timings: JobTimings | None = None,
    scope: JobProcessScope | None = None,
) -> AsyncIterator[dict]:
    dirs = tuple(extra_dirs) if extra_dirs else ()

    if warm_enabled():
        turn = await _start_warm_turn(
            prompt,
            workdir=workdir,
            resume=resume,
            system_hint=system_hint,
            extra_dirs=dirs,
            timings=timings,
            scope=scope,
        )
        if turn is not None:
            async for event in _stream_warm_turn(turn, scope):
                yield event
            return

    async for event in _stream_cold(
        prompt,
        workdir=workdir,
        resume=resume,
        system_hint=system_hint,
        extra_dirs=dirs,
        timings=timings,
        scope=scope,
    ):
        yield event


async def _stream_cold(
    prompt: str,
    *,
    workdir: str | None,
    resume: str | None,
    system_hint: str | None,
    extra_dirs: tuple[str, ...],
    timings: JobTimings | None,
    scope: JobProcessScope | None = None,
) -> AsyncIterator[dict]:
    if timings is not None:
        timings.start(SPAN_SPAWN)
        timings.set_meta(warm=False)
    try:
        proc = await _spawn_claude_process(
            prompt,
            workdir=workdir,
            resume=resume,
            system_hint=system_hint,
            extra_dirs=extra_dirs,
            stream_input=False,
        )
    except FileNotFoundError:
        if timings is not None:
            timings.stop(SPAN_SPAWN)
        yield {
            "type": "error",
            "text": "claude CLI를 찾을 수 없습니다. PATH와 Claude Code 설치 상태를 확인하세요.",
            "returncode": None,
        }
        return
    except OSError as exc:
        if timings is not None:
            timings.stop(SPAN_SPAWN)
        yield {
            "type": "error",
            "text": f"claude CLI 실행 실패: {exc}",
            "returncode": None,
        }
        return

    if timings is not None:
        timings.stop(SPAN_SPAWN)
        timings.start(SPAN_FIRST_EVENT)

    _ACTIVE_CLAUDE_PROCESSES.add(proc)
    if scope is not None:
        scope.add(proc)

    if proc.stdout is None:
        _ACTIVE_CLAUDE_PROCESSES.discard(proc)
        if scope is not None:
            scope.discard(proc)
        yield {
            "type": "error",
            "text": "claude CLI stdout 파이프를 열 수 없습니다.",
            "returncode": None,
        }
        return
    stderr_task = asyncio.create_task(proc.stderr.read()) if proc.stderr else None

    try:
        async for raw in proc.stdout:
            line = raw.decode("utf-8", errors="replace").rstrip()
            if not line:
                continue
            if timings is not None:
                timings.stop(SPAN_FIRST_EVENT)
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                yield {"type": "raw", "text": line}

        returncode = await proc.wait()
        stderr = await stderr_task if stderr_task else b""

        if returncode != 0:
            stderr_text = stderr.decode("utf-8", errors="replace")[:1500]
            if not stderr_text:
                stderr_text = "claude CLI 프로세스가 종료되었습니다."
            yield {
                "type": "error",
                "text": stderr_text,
                "returncode": returncode,
            }
    except (ValueError, asyncio.LimitOverrunError) as exc:
        # A single stdout line exceeded the configured stream limit (see
        # CLAUDE_STREAM_LIMIT_BYTES / _resolve_stream_limit_bytes) and
        # asyncio's StreamReader gave up trying to find the line separator.
        # The subprocess is still running at this point -- report it the same
        # way as the other failure paths above instead of letting the
        # exception escape as an opaque crash, and clean it up in `finally`.
        yield {
            "type": "error",
            "text": f"claude CLI 출력 스트림 처리 실패(라인 리밋 초과 가능성): {exc}",
            "returncode": None,
        }
    finally:
        if stderr_task and not stderr_task.done():
            stderr_task.cancel()
        await _cleanup_runaway_process(proc)
        _ACTIVE_CLAUDE_PROCESSES.discard(proc)
        if scope is not None:
            scope.discard(proc)
