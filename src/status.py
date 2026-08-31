import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

import discord

logger = logging.getLogger(__name__)

WORKING_MESSAGE = "작업중입니다."
QUEUED_MESSAGE = "대기 중"
WORKING_GIF_PATH = Path(__file__).resolve().parents[1] / "working_m.gif"
WORKING_GIF_FILENAME = "working_m.gif"

# Uploading working_m.gif (192KB) on every ack delayed the very first Discord
# response. The GIF is now opt-in only, via WORKING_GIF=1/true; the default
# ack is text-only and goes out immediately.
WORKING_GIF_ENV_VAR = "WORKING_GIF"
WORKING_GIF_TRUTHY_VALUES = {"1", "true", "yes", "on"}

# Discord's message-edit route allows ~5 edits per 5 seconds before an
# edit queues behind the rate-limit bucket (risking the final "완료" edit
# arriving late). 2.5s keeps us at 0.4 edits/sec, well under that 1/sec
# ceiling.
EDIT_INTERVAL_SECONDS = 2.5
DISCORD_EDIT_RATE_LIMIT_PER_SECOND = 1.0  # 5 edits / 5 seconds

# Issue #19: a *fixed* interval makes the edit count grow linearly with job
# duration, and 5-10 minute Claude Code jobs are normal here -- at a flat 2.5s
# that is 120-240 edits into one channel's edit bucket for a single job. So the
# interval grows after every edit instead, up to a ceiling.
#
# Growth is 1.5 rather than 2.0 because the first minute is the window where
# the user is still asking "did it even take?": 1.5 spends 6 edits there and
# only then thins out, whereas 2.0 would be down to one edit per 20s before the
# job is 40 seconds old. The ceiling is reached ~80s in, after 7 edits.
#
# The ceiling exists because unbounded growth eventually leaves a long job with
# no sign of life for minutes at a time. 30s also bounds how stale the "경과
# MM:SS" counter in the status line can be, which is what tells the user the
# job is still moving once the edits thin out.
#
# Net effect: a 10-minute job costs ~24 edits instead of 240.
EDIT_INTERVAL_GROWTH = 1.5
MAX_EDIT_INTERVAL_SECONDS = 30.0

# Fallback labels keyed by minimum elapsed seconds, used only until we learn
# a real tool name or turn count from the stream. Unlike the old animation,
# these are driven by the wall clock (time.monotonic()), not a looping index.
STAGE_LABELS: tuple[tuple[float, str], ...] = (
    (0, "요청을 정리하는 중"),
    (5, "작업을 수행하는 중"),
    (20, "꼼꼼히 확인하는 중"),
    (60, "마무리하는 중"),
)


def _working_gif_enabled() -> bool:
    raw = os.environ.get(WORKING_GIF_ENV_VAR, "")
    return raw.strip().lower() in WORKING_GIF_TRUTHY_VALUES


def make_working_gif_file() -> discord.File | None:
    if not _working_gif_enabled():
        return None
    if not WORKING_GIF_PATH.is_file():
        return None
    return discord.File(WORKING_GIF_PATH, filename=WORKING_GIF_FILENAME)


@dataclass
class JobProgress:
    """Shared, mutable progress state for one job.

    ``record_event`` is handed straight to ``run_job(..., on_event=...)``, so
    each stream event updates this object the moment the orchestrator sees it;
    a renderer (``run_spinning_loader`` / ``format_working_status``) reads it.

    Issue #3: this used to be filled by a background tailer that polled
    ``<job_dir>/logs/stream.jsonl`` every 0.5s -- i.e. run_job wrote an event
    it already held in memory to disk purely so another task could parse it
    back out. That round-trip is gone: it delayed progress by up to a poll
    interval and required a pile of partial-read defenses for the multi-byte
    truncation a concurrent writer causes. stream.jsonl remains, as a
    debugging artifact only.
    """

    started_at: float = field(default_factory=time.monotonic)
    turn: int = 0
    tool: str | None = None
    event_type: str | None = None

    def elapsed_seconds(self) -> float:
        return max(0.0, time.monotonic() - self.started_at)

    def record_event(self, event: dict) -> None:
        if not isinstance(event, dict):
            return

        event_type = event.get("type")
        if event_type:
            self.event_type = str(event_type)

        if event_type == "assistant":
            self.turn += 1
            for block in _content_blocks(event):
                if block.get("type") == "tool_use":
                    name = block.get("name")
                    if name:
                        self.tool = str(name)
        elif event_type in {"user", "result", "error"}:
            # A tool result (or the job ending) means no tool is in flight.
            self.tool = None


def _content_blocks(event: dict) -> list[dict]:
    message = event.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if not isinstance(content, list):
        return []
    return [block for block in content if isinstance(block, dict)]


def _stage_label(elapsed: float) -> str:
    label = STAGE_LABELS[0][1]
    for threshold, text in STAGE_LABELS:
        if elapsed >= threshold:
            label = text
    return label


def _format_elapsed(elapsed: float) -> str:
    total_seconds = int(elapsed)
    minutes, seconds = divmod(total_seconds, 60)
    return f"{minutes:02d}:{seconds:02d}"


def _activity_label(progress: "JobProgress | None") -> str:
    if progress is None:
        return STAGE_LABELS[0][1]
    if progress.tool:
        return f"{progress.tool} 도구 실행 중"
    if progress.turn > 0:
        return f"{progress.turn}번째 턴 진행 중"
    return _stage_label(progress.elapsed_seconds())


def format_working_status(job_name: str, progress: "JobProgress | None" = None) -> str:
    elapsed = progress.elapsed_seconds() if progress else 0.0
    return (
        f"⚙️ **{WORKING_MESSAGE}**\n"
        f"{_activity_label(progress)} · 경과 {_format_elapsed(elapsed)}\n"
        f"`{job_name}`"
    )


def format_queued_status(job_name: str, ahead: int) -> str:
    """Ack text for a job that is waiting on the MAX_CONCURRENT_JOBS gate.

    Issue #6: without this the user saw the normal "작업중입니다" while
    nothing had actually started, and every queued job silently slowed the
    running ones down. ``ahead`` counts the jobs already holding or waiting
    for a slot when this one arrived.
    """
    return (
        f"⏳ **{QUEUED_MESSAGE} (앞에 {max(0, ahead)}건)**\n"
        f"앞선 작업이 끝나면 바로 시작합니다\n"
        f"`{job_name}`"
    )


async def run_spinning_loader(
    message: discord.Message,
    job_name: str,
    progress: "JobProgress | None" = None,
    *,
    interval: float = EDIT_INTERVAL_SECONDS,
    growth: float = EDIT_INTERVAL_GROWTH,
    max_interval: float = MAX_EDIT_INTERVAL_SECONDS,
) -> None:
    delay = interval
    while True:
        await asyncio.sleep(delay)
        # Backed off *after* the sleep, so the first edit still lands at
        # `interval` -- the point of the curve is to thin out later ticks,
        # not to delay the first sign that the job started.
        delay = min(delay * growth, max_interval)
        try:
            await message.edit(content=format_working_status(job_name, progress))
        except (discord.DiscordException, OSError):
            # Message gone / permissions changed (DiscordException), or a
            # transient network hiccup such as aiohttp.ClientOSError (an
            # OSError subclass). The loader is best-effort UI, so bail out
            # quietly rather than dying with an exception that would
            # otherwise surface when the caller awaits this task.
            logger.warning("run_spinning_loader: edit failed, stopping loader", exc_info=True)
            return


async def stop_spinning_loader(task: asyncio.Task[None]) -> None:
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:
        # A background status task must never take the caller down with it --
        # log for debuggability and swallow so the real job result (e.g.
        # send_outputs) still gets delivered.
        logger.exception("Background status task ended with an unexpected error")
