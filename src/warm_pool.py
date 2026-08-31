"""Reusable ("warm") claude CLI processes.

Issue #2: ``run_claude_stream`` spawned a fresh ``claude`` process per Discord
message, and that spawn cost a flat 1.4~1.6s every single time -- the floor
under even a "안녕". The CLI can serve many turns from one process when it is
started with ``--input-format stream-json`` and its stdin is kept open, so this
module keeps finished processes alive and hands them back for the next turn of
the same conversation.

Measured on the real CLI with claudecord's exact flag set (2026-08-31):

    turn 1  wall 2911ms   first event 1279ms   (cold spawn)
    turn 2  wall 1392ms   first event    8ms   (warm reuse)
    turn 3  wall 1324ms   first event    6ms   (warm reuse)

This module is deliberately pure bookkeeping: it never terminates a process
itself. ``src.runner`` owns process termination (it also owns
``_ACTIVE_CLAUDE_PROCESSES``, which the Discord ``종료`` command drains), so the
pool takes a ``retire`` callback and hands processes back to it. That keeps the
dependency one-way -- runner imports warm_pool, never the reverse -- and means
there is exactly one place that knows how to kill a claude process.

Reuse is keyed by ``(WarmKey, session_id)``: a parked process is holding one
specific conversation in memory, so it may only be handed to a turn that asked
to resume that same conversation. A turn with ``resume=None`` always spawns
fresh, otherwise it would inherit the previous conversation's context.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass

WARM_ENABLED_ENV_VAR = "WARM_CLAUDE"
WARM_IDLE_TTL_ENV_VAR = "WARM_CLAUDE_IDLE_TTL_SECONDS"
WARM_MAX_PROCESSES_ENV_VAR = "WARM_CLAUDE_MAX_PROCESSES"

DEFAULT_WARM_ENABLED = True
DEFAULT_IDLE_TTL_SECONDS = 300.0
DEFAULT_MAX_PROCESSES = 2

# Keep only the tail of a warm process's stderr: it is drained continuously (an
# unread stderr pipe would eventually block the CLI), but only the last chunk is
# useful, and only when the process dies unexpectedly.
STDERR_TAIL_BYTES = 4000

_TRUTHY = {"1", "true", "yes", "on"}
_FALSEY = {"0", "false", "no", "off"}


def _environ(environ: dict[str, str] | None) -> dict[str, str]:
    return os.environ if environ is None else environ


def _running_loop() -> asyncio.AbstractEventLoop | None:
    """The loop to schedule pool housekeeping on, or ``None`` when the pool is
    driven from synchronous code (test helpers)."""
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


def warm_enabled(environ: dict[str, str] | None = None) -> bool:
    raw = str(_environ(environ).get(WARM_ENABLED_ENV_VAR, "")).strip().lower()
    if raw in _TRUTHY:
        return True
    if raw in _FALSEY:
        return False
    return DEFAULT_WARM_ENABLED


def idle_ttl_seconds(environ: dict[str, str] | None = None) -> float:
    raw = str(_environ(environ).get(WARM_IDLE_TTL_ENV_VAR, "")).strip()
    if not raw:
        return DEFAULT_IDLE_TTL_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_IDLE_TTL_SECONDS
    return value if value > 0 else DEFAULT_IDLE_TTL_SECONDS


def max_processes(environ: dict[str, str] | None = None) -> int:
    raw = str(_environ(environ).get(WARM_MAX_PROCESSES_ENV_VAR, "")).strip()
    if not raw:
        return DEFAULT_MAX_PROCESSES
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_MAX_PROCESSES
    # 0 is a meaningful setting: keep streaming-input spawns but never reuse.
    return value if value >= 0 else DEFAULT_MAX_PROCESSES


@dataclass(frozen=True)
class WarmKey:
    """Everything fixed at spawn time that a reused process must still match.

    ``extra_dirs`` (``--add-dir``) is deliberately *not* here by default -- see
    ``src.runner.WARM_KEY_IGNORES_EXTRA_DIRS`` for the empirical reason and the
    guard that re-introduces it if the permission mode ever changes.
    """

    workdir: str | None
    model: str
    system_hint: str | None
    extra_dirs: tuple[str, ...] = ()


class WarmProcess:
    """One live ``claude`` process in streaming-input mode."""

    def __init__(self, proc, key: WarmKey) -> None:
        self.proc = proc
        self.key = key
        self.session_id: str | None = None
        self.turns = 0
        self.parked_at: float | None = None
        # The dict key this process is parked under, recorded by the pool at
        # park time. Every later removal uses *this* value instead of rebuilding
        # (key, session_id) from the object: session_id is mutated by the runner
        # between turns, so a rebuilt key can miss the entry it means to drop.
        self.parked_slot: tuple[WarmKey, str] | None = None
        self._stderr_buf = bytearray()
        self._stderr_task: asyncio.Task | None = None
        self._expiry_task: asyncio.Task | None = None

    @property
    def alive(self) -> bool:
        return self.proc.returncode is None

    # -- stderr ---------------------------------------------------------

    def start_stderr_drain(self) -> None:
        if self.proc.stderr is None or self._stderr_task is not None:
            return
        self._stderr_task = asyncio.create_task(self._drain_stderr())

    async def _drain_stderr(self) -> None:
        try:
            while True:
                chunk = await self.proc.stderr.read(4096)
                if not chunk:
                    return
                self._stderr_buf.extend(chunk)
                if len(self._stderr_buf) > STDERR_TAIL_BYTES:
                    del self._stderr_buf[:-STDERR_TAIL_BYTES]
        except (asyncio.CancelledError, ValueError, OSError):
            return

    def stderr_tail(self) -> str:
        return bytes(self._stderr_buf).decode("utf-8", errors="replace").strip()

    def cancel_stderr_drain(self) -> None:
        task, self._stderr_task = self._stderr_task, None
        if task is not None and not task.done():
            task.cancel()

    async def wait_stderr_drained(self, timeout: float = 1.0) -> None:
        """Let the drain task reach EOF so ``stderr_tail`` is complete. Only
        worth awaiting once the process is known to be exiting."""
        task = self._stderr_task
        if task is None or task.done():
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        except (TimeoutError, asyncio.CancelledError):
            return

    # -- turn I/O -------------------------------------------------------

    async def send_user_message(self, text: str) -> None:
        """Push one user turn. Raises on a broken pipe so the caller can fall
        back to a cold spawn."""
        stdin = self.proc.stdin
        if stdin is None:
            raise BrokenPipeError("warm claude process has no stdin pipe")
        payload = json.dumps(
            {"type": "user", "message": {"role": "user", "content": text}},
            ensure_ascii=False,
        )
        stdin.write((payload + "\n").encode("utf-8"))
        await stdin.drain()

    async def read_event(self) -> dict | None:
        """Next stdout event, or ``None`` at EOF (the process ended).

        Propagates ``ValueError``/``LimitOverrunError`` from the stream reader
        exactly like the cold path does, so the caller applies one policy.
        """
        stdout = self.proc.stdout
        if stdout is None:
            return None
        while True:
            raw = await stdout.readline()
            if not raw:
                return None
            line = raw.decode("utf-8", errors="replace").rstrip()
            if not line:
                continue
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                return {"type": "raw", "text": line}

    def close_stdin(self) -> None:
        """Ask the CLI to exit by closing its input. Best-effort."""
        stdin = self.proc.stdin
        if stdin is None:
            return
        # Best-effort: the pipe may already be torn down (OSError) or the
        # loop gone (RuntimeError), and either way stdin is closed enough.
        with contextlib.suppress(OSError, RuntimeError):
            stdin.close()

    # -- expiry bookkeeping ---------------------------------------------

    def cancel_expiry(self) -> None:
        task, self._expiry_task = self._expiry_task, None
        if task is not None and not task.done():
            task.cancel()


RetireCallback = Callable[[WarmProcess], Awaitable[None]]


class WarmClaudePool:
    """Idle warm processes, keyed by ``(WarmKey, session_id)``.

    At most one process per key: a conversation only ever has one live process.
    """

    def __init__(
        self,
        *,
        retire: RetireCallback,
        clock: Callable[[], float] = time.monotonic,
        environ: dict[str, str] | None = None,
    ) -> None:
        self._retire = retire
        self._clock = clock
        self._environ = environ
        self._idle: dict[tuple[WarmKey, str], WarmProcess] = {}
        # Strong references to in-flight retire tasks; without them the loop
        # may drop a pending task before it has cleaned anything up.
        self._retiring: set[asyncio.Task] = set()

    # -- config ---------------------------------------------------------

    def enabled(self) -> bool:
        return warm_enabled(self._environ)

    def ttl(self) -> float:
        return idle_ttl_seconds(self._environ)

    def capacity(self) -> int:
        return max_processes(self._environ)

    def __len__(self) -> int:
        return len(self._idle)

    def idle_processes(self) -> list[WarmProcess]:
        return list(self._idle.values())

    # -- checkout / checkin ----------------------------------------------

    def acquire(self, key: WarmKey, session_id: str | None) -> WarmProcess | None:
        """Check out the process holding ``session_id`` for ``key``.

        ``session_id=None`` never matches: a brand-new conversation must not
        inherit a parked process's context.
        """
        if session_id is None or not self.enabled():
            return None
        entry = self._idle.pop((key, session_id), None)
        if entry is None:
            return None
        entry.parked_slot = None
        entry.cancel_expiry()
        entry.parked_at = None
        if not entry.alive:
            # Died while parked. Popping it is not enough: its stdin pipe and
            # its still-running stderr drain task would then only be released
            # whenever the process object happens to be collected. Hand it to
            # the retire path, which is the one place that closes both.
            self._schedule_retire(entry)
            return None
        return entry

    def _schedule_retire(self, wp: WarmProcess) -> None:
        """Run the retire callback for ``wp`` from synchronous code."""
        loop = _running_loop()
        if loop is None:
            # No running loop (sync test helpers): do the part that needs no
            # awaiting rather than leaving the pipes open.
            wp.close_stdin()
            wp.cancel_stderr_drain()
            return
        task = loop.create_task(self._retire(wp))
        self._retiring.add(task)
        task.add_done_callback(self._retiring.discard)

    def park(self, wp: WarmProcess) -> list[WarmProcess]:
        """Return ``wp`` to the pool.

        Returns the processes the caller must retire: ``wp`` itself when it is
        not parkable, any other process still holding the same conversation,
        plus any process evicted to stay under the cap.
        """
        if not self.enabled() or not wp.alive or wp.session_id is None:
            return [wp]

        capacity = self.capacity()
        if capacity <= 0:
            return [wp]

        retire: list[WarmProcess] = []

        # One conversation, one process -- whatever key it was parked under.
        # The key is not stable across turns (workdir is part of it), so an
        # A -> B -> A rotation would otherwise leave the A-keyed process parked
        # holding only turn 1 while B's process holds turns 1-2. Turn 3, keyed
        # back to A, would be handed the short one and the user watches the bot
        # forget the previous turn.
        for stale_slot in [slot for slot in self._idle if slot[1] == wp.session_id]:
            previous = self._idle.pop(stale_slot)
            previous.parked_slot = None
            previous.cancel_expiry()
            if previous is not wp:
                retire.append(previous)

        slot = (wp.key, wp.session_id)
        wp.parked_at = self._clock()
        wp.parked_slot = slot
        self._idle[slot] = wp

        # Oldest-parked-first eviction; dict preserves insertion order and we
        # re-insert on every park, so the front of the dict is the LRU entry.
        # Delete the key we just read: rebuilding it from the entry can miss,
        # and a miss means len() never shrinks -- an infinite loop on the event
        # loop thread, i.e. the whole bot wedged.
        while len(self._idle) > capacity:
            evicted_slot, evicted = next(iter(self._idle.items()))
            del self._idle[evicted_slot]
            evicted.parked_slot = None
            evicted.cancel_expiry()
            retire.append(evicted)

        if wp in retire:
            return retire

        wp.cancel_expiry()
        loop = _running_loop()
        if loop is None:
            # No running loop (only reachable from sync test helpers): the
            # process stays parked without a TTL rather than failing the turn.
            wp._expiry_task = None
        else:
            wp._expiry_task = loop.create_task(self._expire_later(wp))
        return retire

    async def _expire_later(self, wp: WarmProcess) -> None:
        try:
            await asyncio.sleep(self.ttl())
        except asyncio.CancelledError:
            return
        slot = wp.parked_slot
        if slot is None or self._idle.get(slot) is not wp:
            return
        del self._idle[slot]
        wp.parked_slot = None
        wp._expiry_task = None
        await self._retire(wp)

    def forget(self, wp: WarmProcess) -> None:
        """Drop ``wp`` from bookkeeping without retiring it."""
        wp.cancel_expiry()
        slot, wp.parked_slot = wp.parked_slot, None
        if slot is None:
            return
        if self._idle.get(slot) is wp:
            del self._idle[slot]

    def discard_processes(self, procs: Iterable) -> list[WarmProcess]:
        """Drop every parked entry whose process is one of ``procs``.

        Returns them for the caller to terminate. Used when a single job's
        processes are being reaped (a job timeout): a process that job started
        must not stay parked, or the next turn is handed a corpse.
        """
        targets = set(procs)
        dropped: list[WarmProcess] = []
        for slot in [s for s, wp in self._idle.items() if wp.proc in targets]:
            wp = self._idle.pop(slot)
            wp.parked_slot = None
            wp.cancel_expiry()
            dropped.append(wp)
        return dropped

    def reset(self) -> list[WarmProcess]:
        """Empty the pool, returning every parked process for the caller to
        terminate. Used by the Discord ``종료`` command."""
        parked = list(self._idle.values())
        self._idle.clear()
        for wp in parked:
            wp.parked_slot = None
            wp.cancel_expiry()
        return parked
