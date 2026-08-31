"""Per-job wall-clock instrumentation.

Issue #7: nothing in the bot recorded *where* a Discord round-trip spent its
time, so "느리다" reports could not be attributed to CLI startup, model
thinking, or the final Discord sends -- and no other performance change could
prove its own effect. ``JobTimings`` is the shared measurement object threaded
through the whole request path: main.py owns it, runner.py and orchestrator.py
fill in their spans, and the result is persisted next to the job.

This module is the integration contract between those call sites. Keep the
span names below stable -- they are what the issue's before/after numbers are
quoted against.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

TIMINGS_FILENAME = "timings.json"

# Canonical span names (issue #7's table). Measured in milliseconds.
SPAN_ACK = "t_ack"                  # message received -> ack message sent
SPAN_SPAWN = "t_spawn"              # create_subprocess_exec call -> returned
SPAN_FIRST_EVENT = "t_first_event"  # spawn returned -> first stdout event
SPAN_RESULT = "t_result"            # first event -> result/error event
SPAN_OUTPUTS = "t_outputs"          # run_job returned -> send_outputs finished
SPAN_TOTAL = "t_total"              # message received -> everything delivered

SPAN_ORDER: tuple[str, ...] = (
    SPAN_ACK,
    SPAN_SPAWN,
    SPAN_FIRST_EVENT,
    SPAN_RESULT,
    SPAN_OUTPUTS,
    SPAN_TOTAL,
)

# The three spans that together cover the claude CLI process's wall time.
# Subtracting the CLI's self-reported duration_ms from their sum yields the
# pure process-startup overhead the issue measured at 1.4~1.6s.
_CLI_WALL_SPANS = (SPAN_SPAWN, SPAN_FIRST_EVENT, SPAN_RESULT)

# One Discord message can run the CLI more than once -- main.py retries
# without the stale session id when a resumed conversation has gone missing.
# Every span and cost figure below belongs to a single CLI attempt, so a retry
# would otherwise overwrite the first attempt's numbers and leave t_total
# unexplainably larger than the spans that are supposed to add up to it.
# ``start_attempt`` archives them instead; see JobTimings.attempts.
_ATTEMPT_SPANS = (SPAN_SPAWN, SPAN_FIRST_EVENT, SPAN_RESULT)
_ATTEMPT_META = (
    "duration_ms",
    "duration_api_ms",
    "num_turns",
    "cli_wall_ms",
    "cli_startup_overhead_ms",
    "warm",
)

DEBUG_TIMING_ENV_VAR = "DEBUG_TIMING"
_TRUTHY = {"1", "true", "yes", "on"}


def debug_timing_enabled(environ: dict[str, str] | None = None) -> bool:
    import os

    source = os.environ if environ is None else environ
    return str(source.get(DEBUG_TIMING_ENV_VAR, "")).strip().lower() in _TRUTHY


class JobTimings:
    """Mutable, single-job timing recorder.

    Deliberately not a dataclass: call sites mutate it from several modules
    and the open-span bookkeeping (``start``/``stop``) is stateful.

    Every method is best-effort and must never raise into the request path --
    instrumentation that can break a job is worse than no instrumentation.
    ``stop`` on a span that was never started returns ``None`` instead of
    raising, so a call site that bails early (an exception between start and
    stop) simply leaves that span unrecorded.
    """

    def __init__(
        self,
        job_id: str | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._clock = clock
        self.job_id = job_id
        self.spans_ms: dict[str, float] = {}
        self.meta: dict[str, Any] = {}
        self.attempts: list[dict[str, Any]] = []
        self._open: dict[str, float] = {}
        self._origin = clock()

    # -- span recording -------------------------------------------------

    def start(self, name: str) -> None:
        self._open[name] = self._clock()

    def stop(self, name: str) -> float | None:
        started = self._open.pop(name, None)
        if started is None:
            return None
        elapsed_ms = (self._clock() - started) * 1000.0
        self.record_ms(name, elapsed_ms)
        return self.spans_ms[name]

    def record_ms(self, name: str, milliseconds: float) -> None:
        try:
            value = float(milliseconds)
        except (TypeError, ValueError):
            return
        self.spans_ms[name] = round(max(0.0, value), 1)

    def since_origin_ms(self) -> float:
        """Milliseconds since this object was created (i.e. since the Discord
        message was received, given main.py constructs it on arrival)."""
        return round(max(0.0, (self._clock() - self._origin) * 1000.0), 1)

    @contextmanager
    def timer(self, name: str) -> Iterator[None]:
        self.start(name)
        try:
            yield
        finally:
            self.stop(name)

    # -- retries --------------------------------------------------------

    def start_attempt(self, reason: str | None = None) -> None:
        """Archive the CLI spans recorded so far and begin a fresh attempt.

        Call this *before* re-running the CLI for the same Discord message
        (e.g. main.py's stale-session retry). Spans that belong to the message
        rather than to one CLI run -- ``t_ack``, ``t_outputs``, ``t_total`` --
        are deliberately left in place; only the per-attempt spans and the
        CLI's own cost figures move into ``attempts``.

        A no-op when the current attempt recorded nothing, so calling it
        before the first run is harmless.
        """
        recorded = {name: self.spans_ms[name] for name in _ATTEMPT_SPANS if name in self.spans_ms}
        if not recorded:
            return

        for name in recorded:
            self.spans_ms.pop(name, None)
        archived_meta = {key: self.meta.pop(key) for key in _ATTEMPT_META if key in self.meta}

        entry: dict[str, Any] = {"spans_ms": recorded, "meta": archived_meta}
        if reason:
            entry["reason"] = reason
        self.attempts.append(entry)

    # -- metadata -------------------------------------------------------

    def set_meta(self, **values: Any) -> None:
        for key, value in values.items():
            if value is not None:
                self.meta[key] = value

    def absorb_result_event(self, event: Any) -> None:
        """Pull the CLI's self-reported cost figures out of a stream ``result``
        event and derive the startup overhead from them."""
        if not isinstance(event, dict):
            return
        for key in ("duration_ms", "duration_api_ms", "num_turns"):
            value = event.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                self.meta[key] = value
        self._derive_overhead()

    def _derive_overhead(self) -> None:
        recorded = [self.spans_ms[name] for name in _CLI_WALL_SPANS if name in self.spans_ms]
        if not recorded:
            return
        cli_wall_ms = round(sum(recorded), 1)
        self.meta["cli_wall_ms"] = cli_wall_ms

        duration_ms = self.meta.get("duration_ms")
        if not isinstance(duration_ms, (int, float)) or isinstance(duration_ms, bool):
            return

        # Only meaningful when the CLI's whole wall time is accounted for. With
        # a span missing, cli_wall_ms understates the run and the subtraction
        # goes negative -- a harness that records t_spawn/t_first_event but not
        # t_result would report an alarming "cli_startup -1400ms" rather than
        # simply omitting a figure it cannot compute.
        if not all(name in self.spans_ms for name in _CLI_WALL_SPANS):
            self.meta.pop("cli_startup_overhead_ms", None)
            return

        self.meta["cli_startup_overhead_ms"] = round(cli_wall_ms - float(duration_ms), 1)

    # -- output ---------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        self._derive_overhead()
        ordered = {name: self.spans_ms[name] for name in SPAN_ORDER if name in self.spans_ms}
        # Preserve any non-canonical spans a caller added, after the known ones.
        ordered.update({k: v for k, v in self.spans_ms.items() if k not in ordered})

        meta = dict(self.meta)
        payload: dict[str, Any] = {
            "job_id": self.job_id,
            "spans_ms": ordered,
            "meta": meta,
        }
        if self.attempts:
            # Without this, t_total would silently exceed the sum of spans_ms
            # by however long the abandoned attempts took, with nothing in the
            # file to explain the gap.
            meta["attempts"] = len(self.attempts) + 1
            payload["attempts"] = [dict(entry) for entry in self.attempts]
        return payload

    def format_line(self) -> str:
        snapshot = self.snapshot()
        parts = [f"{name} {value:.0f}ms" for name, value in snapshot["spans_ms"].items()]
        overhead = snapshot["meta"].get("cli_startup_overhead_ms")
        if isinstance(overhead, (int, float)):
            parts.append(f"cli_startup {overhead:.0f}ms")
        warm = snapshot["meta"].get("warm")
        if warm is not None:
            parts.append("warm" if warm else "cold")
        attempts = snapshot["meta"].get("attempts")
        if isinstance(attempts, int) and attempts > 1:
            parts.append(f"시도 {attempts}회")
        return " · ".join(parts) if parts else "계측 없음"

    def write(self, job_dir: Path) -> Path | None:
        """Persist to ``<job_dir>/timings.json``. Best-effort: a failure here
        is logged by the caller, never raised into the request path."""
        try:
            path = Path(job_dir) / TIMINGS_FILENAME
            path.write_text(
                json.dumps(self.snapshot(), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except OSError:
            return None
        return path
