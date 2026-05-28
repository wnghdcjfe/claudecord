import asyncio
import json
import os
import signal
import shutil
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator

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

_ACTIVE_CLAUDE_PROCESSES: set[asyncio.subprocess.Process] = set()


@dataclass(frozen=True)
class TerminationSummary:
    requested: int
    terminated: int
    killed: int
    still_running: int


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
    except asyncio.TimeoutError:
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


async def terminate_active_claude_processes(
    timeout: float = TERMINATE_TIMEOUT_SECONDS,
) -> TerminationSummary:
    processes = _live_claude_processes()
    requested = len(processes)

    for proc in processes:
        try:
            _terminate_process(proc)
        except ProcessLookupError:
            pass

    exited = await asyncio.gather(
        *(_wait_for_process_exit(proc, timeout) for proc in processes),
        return_exceptions=False,
    )
    stubborn = [proc for proc, did_exit in zip(processes, exited) if not did_exit]

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

    terminated = sum(1 for proc in processes if proc.returncode is not None)
    _ACTIVE_CLAUDE_PROCESSES.difference_update(processes)
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
) -> list[str]:
    cmd = [_resolve_claude_executable(), "-p"]
    if extra_dirs:
        cmd += ["--add-dir", *extra_dirs]

    cmd += [
        "--setting-sources",
        "local",
        "--output-format",
        "stream-json",
        "--verbose",
        "--permission-mode",
        "bypassPermissions",
        f"--allowedTools={SAFE_TOOLS}",
        f"--disallowedTools={BLOCKED_TOOLS}",
    ]

    if resume:
        cmd += ["--resume", resume]
    if system_hint:
        cmd += ["--append-system-prompt", system_hint]
    cmd += ["--", prompt]
    return _wrap_windows_batch_command(cmd)


async def run_claude_stream(
    prompt: str,
    workdir: str | None = None,
    resume: str | None = None,
    system_hint: str | None = None,
    extra_dirs: Iterable[str] | None = None,
) -> AsyncIterator[dict]:
    try:
        subprocess_kwargs = {}
        if os.name != "nt":
            subprocess_kwargs["start_new_session"] = True

        proc = await asyncio.create_subprocess_exec(
            *build_claude_command(
                prompt,
                resume=resume,
                system_hint=system_hint,
                extra_dirs=extra_dirs,
            ),
            cwd=workdir,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **subprocess_kwargs,
        )
    except FileNotFoundError:
        yield {
            "type": "error",
            "text": "claude CLI를 찾을 수 없습니다. PATH와 Claude Code 설치 상태를 확인하세요.",
            "returncode": None,
        }
        return
    except OSError as exc:
        yield {
            "type": "error",
            "text": f"claude CLI 실행 실패: {exc}",
            "returncode": None,
        }
        return

    _ACTIVE_CLAUDE_PROCESSES.add(proc)

    if proc.stdout is None:
        _ACTIVE_CLAUDE_PROCESSES.discard(proc)
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
    finally:
        _ACTIVE_CLAUDE_PROCESSES.discard(proc)
