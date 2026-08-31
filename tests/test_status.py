import asyncio
import os
import tempfile
import time
import unittest
from itertools import pairwise
from pathlib import Path
from unittest import mock

import discord

from src import status
from src.status import (
    EDIT_INTERVAL_GROWTH,
    EDIT_INTERVAL_SECONDS,
    MAX_EDIT_INTERVAL_SECONDS,
    QUEUED_MESSAGE,
    WORKING_GIF_FILENAME,
    WORKING_MESSAGE,
    JobProgress,
    format_queued_status,
    format_working_status,
    make_working_gif_file,
    run_spinning_loader,
    stop_spinning_loader,
)


class FakeMessage:
    """Fake discord.Message-like target for run_spinning_loader.

    If constructed with a `progress`, it mutates that progress object after
    the first recorded edit, so tests can deterministically assert that a
    later edit reflects newly-arrived progress (rather than depending on
    real wall-clock timing).
    """

    def __init__(self, target_edits: int, progress: JobProgress | None = None):
        self.edits = []
        self.ready = asyncio.Event()
        self.target_edits = target_edits
        self.progress = progress

    async def edit(self, *, content=None):
        self.edits.append(content)
        if self.progress is not None and len(self.edits) == 1:
            self.progress.tool = "Bash"
        if len(self.edits) >= self.target_edits:
            self.ready.set()


def _record_sleeps(*, target_edits: int, **loader_kwargs) -> list[float]:
    """Drive run_spinning_loader and return the delays it actually awaited.

    asyncio.sleep is replaced with a spy that yields immediately, so a curve
    that would take minutes of wall clock is observed in milliseconds.
    """

    async def scenario():
        message = FakeMessage(target_edits=target_edits)
        sleep_calls: list[float] = []
        real_sleep = asyncio.sleep

        async def spy_sleep(seconds):
            sleep_calls.append(seconds)
            await real_sleep(0)

        with mock.patch("src.status.asyncio.sleep", spy_sleep):
            task = asyncio.create_task(
                run_spinning_loader(message, "job-123", **loader_kwargs)
            )
            await asyncio.wait_for(message.ready.wait(), timeout=5)
            await stop_spinning_loader(task)

        return sleep_calls

    return asyncio.run(scenario())


class StatusTests(unittest.TestCase):
    def test_format_working_status_emphasizes_working_message(self):
        status_text = format_working_status("job-123")

        self.assertEqual(WORKING_MESSAGE, "작업중입니다.")
        self.assertIn(f"**{WORKING_MESSAGE}**", status_text)
        self.assertIn("경과 00:00", status_text)
        self.assertIn("`job-123`", status_text)
        self.assertGreaterEqual(status_text.count("\n"), 2)

    def test_format_working_status_shows_tool_name_when_known(self):
        progress = JobProgress()
        progress.tool = "Bash"
        status_text = format_working_status("job-123", progress)

        self.assertIn("Bash", status_text)
        self.assertIn("도구 실행 중", status_text)

    def test_format_working_status_shows_turn_count_when_no_tool_active(self):
        progress = JobProgress()
        progress.turn = 3
        status_text = format_working_status("job-123", progress)

        self.assertIn("3번째 턴", status_text)

    def test_format_working_status_falls_back_to_real_elapsed_time_stage(self):
        progress = JobProgress()
        progress.started_at -= 30  # simulate 30s of real elapsed time

        status_text = format_working_status("job-123", progress)

        self.assertIn("경과 00:30", status_text)
        # 30s with no tool/turn info yet should reflect the >=20s stage,
        # not the initial "요청을 정리하는 중" label.
        self.assertIn("꼼꼼히 확인하는 중", status_text)

    def test_job_progress_record_event_tracks_tool_and_turn_from_real_events(self):
        progress = JobProgress()

        progress.record_event({"type": "system", "subtype": "init"})
        self.assertEqual(progress.turn, 0)
        self.assertIsNone(progress.tool)

        progress.record_event(
            {
                "type": "assistant",
                "message": {"content": [{"type": "tool_use", "name": "Read"}]},
            }
        )
        self.assertEqual(progress.turn, 1)
        self.assertEqual(progress.tool, "Read")

        progress.record_event(
            {"type": "user", "message": {"content": [{"type": "tool_result"}]}}
        )
        self.assertIsNone(progress.tool)

        progress.record_event(
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "final answer"}]},
            }
        )
        self.assertEqual(progress.turn, 2)
        self.assertIsNone(progress.tool)

    def test_job_progress_ignores_malformed_events(self):
        progress = JobProgress()
        progress.record_event({})
        progress.record_event({"type": "assistant", "message": "not-a-dict"})
        progress.record_event({"type": "assistant", "message": {"content": "not-a-list"}})

        self.assertEqual(progress.turn, 2)
        self.assertIsNone(progress.tool)

    def test_edit_interval_meets_discord_rate_limit_safety_margin(self):
        # Design-time guard for the configured constant. Real emission
        # timing against this exact constant is verified below by actually
        # driving run_spinning_loader and observing asyncio.sleep/edit calls
        # -- not by inspecting a default parameter that is never invoked.
        self.assertGreaterEqual(EDIT_INTERVAL_SECONDS, 2.5)
        implied_edits_per_second = 1 / EDIT_INTERVAL_SECONDS
        self.assertLess(implied_edits_per_second, status.DISCORD_EDIT_RATE_LIMIT_PER_SECOND)

    def test_run_spinning_loader_sleeps_for_edit_interval_seconds_when_uncalled_with_override(self):
        # Exercises the exact call main.py makes (no `interval=` override)
        # and proves the *running* loop waits EDIT_INTERVAL_SECONDS before its
        # first edit, by spying on the real asyncio.sleep it awaits -- not by
        # reading an unexercised function-signature default. The first wait is
        # what the user actually feels; later waits back off (see the backoff
        # tests below).
        sleep_calls = _record_sleeps(target_edits=2)

        self.assertGreaterEqual(len(sleep_calls), 2)
        self.assertEqual(sleep_calls[0], EDIT_INTERVAL_SECONDS)

    def test_spinning_loader_spaces_real_edits_by_at_least_the_given_interval(self):
        # No mocked clock: a real (short) interval, real asyncio.sleep, real
        # wall-clock timestamps recorded at each edit -- proves edits are
        # actually spaced out in time, not just that a constant was read.
        async def scenario():
            progress = JobProgress()
            interval = 0.05
            message = FakeMessage(target_edits=3, progress=progress)
            timestamps = []
            original_edit = message.edit

            async def timed_edit(*, content=None):
                timestamps.append(time.monotonic())
                await original_edit(content=content)

            message.edit = timed_edit

            task = asyncio.create_task(
                run_spinning_loader(message, "job-123", progress, interval=interval)
            )
            await asyncio.wait_for(message.ready.wait(), timeout=2)
            await stop_spinning_loader(task)
            return timestamps

        timestamps = asyncio.run(scenario())

        self.assertGreaterEqual(len(timestamps), 3)
        gaps = [b - a for a, b in pairwise(timestamps)]
        for gap in gaps:
            self.assertGreaterEqual(gap, 0.05 * 0.8)

    def test_run_spinning_loader_stops_quietly_on_network_error_without_propagating(self):
        # A transient network blip (aiohttp.ClientOSError is an OSError
        # subclass) must end the loop like a DiscordException does -- the
        # task finishes normally (task.exception() is None), it does not
        # raise out through whoever awaits the task later.
        async def scenario():
            class FlakyMessage:
                def __init__(self):
                    self.attempts = 0

                async def edit(self, *, content=None):
                    self.attempts += 1
                    raise OSError("simulated aiohttp.ClientOSError")

            message = FlakyMessage()
            task = asyncio.create_task(run_spinning_loader(message, "job-123", interval=0))

            for _ in range(50):
                await asyncio.sleep(0)
                if task.done():
                    break

            self.assertTrue(task.done())
            self.assertIsNone(task.exception())
            return message.attempts

        attempts = asyncio.run(scenario())
        self.assertGreaterEqual(attempts, 1)

    def test_stop_spinning_loader_swallows_and_logs_non_cancel_exceptions(self):
        # M1: a background status task that has already crashed (e.g. the
        # stream tailer) must not make stop_spinning_loader raise -- that
        # would abort the caller's cleanup before it reaches send_outputs.
        # The failure must still be logged, not silently dropped.
        async def crashing_task():
            raise RuntimeError("boom")

        async def scenario():
            task = asyncio.create_task(crashing_task())
            # Let the task actually run to completion (and crash) *before*
            # we try to stop it -- this is the real M1 scenario: by the time
            # cleanup runs, the task is already done-with-exception, so
            # task.cancel() is a no-op and await task must not re-raise.
            for _ in range(10):
                await asyncio.sleep(0)
                if task.done():
                    break
            self.assertTrue(task.done())  # sanity: it really crashed already

            with self.assertLogs("src.status", level="ERROR"):
                await stop_spinning_loader(task)  # must not raise
            return task

        task = asyncio.run(scenario())
        self.assertTrue(task.done())

    def test_spinning_loader_edits_message_and_reflects_progress_updates(self):
        async def scenario():
            progress = JobProgress()
            message = FakeMessage(target_edits=2, progress=progress)
            task = asyncio.create_task(
                run_spinning_loader(message, "job-123", progress, interval=0)
            )
            await asyncio.wait_for(message.ready.wait(), timeout=1)
            await stop_spinning_loader(task)
            return message.edits

        edits = asyncio.run(scenario())

        self.assertGreaterEqual(len(edits), 2)
        self.assertTrue(all(WORKING_MESSAGE in edit for edit in edits))
        # First edit predates the progress update; the second reflects it.
        self.assertNotIn("Bash", edits[0])
        self.assertIn("Bash", edits[1])

    def test_make_working_gif_file_disabled_by_default_even_if_asset_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            gif_path = Path(tmp) / WORKING_GIF_FILENAME
            gif_path.write_bytes(b"GIF89a")

            with (
                mock.patch("src.status.WORKING_GIF_PATH", gif_path),
                mock.patch.dict(os.environ, {}, clear=True),
            ):
                file = make_working_gif_file()

            self.assertIsNone(file)

    def test_make_working_gif_file_uses_configured_asset_when_env_enabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            gif_path = Path(tmp) / WORKING_GIF_FILENAME
            gif_path.write_bytes(b"GIF89a")

            with (
                mock.patch("src.status.WORKING_GIF_PATH", gif_path),
                mock.patch.dict(os.environ, {"WORKING_GIF": "1"}, clear=False),
            ):
                file = make_working_gif_file()

            self.assertIsNotNone(file)
            self.assertEqual(file.filename, WORKING_GIF_FILENAME)
            file.close()

    def test_make_working_gif_file_accepts_true_string_case_insensitively(self):
        with tempfile.TemporaryDirectory() as tmp:
            gif_path = Path(tmp) / WORKING_GIF_FILENAME
            gif_path.write_bytes(b"GIF89a")

            with (
                mock.patch("src.status.WORKING_GIF_PATH", gif_path),
                mock.patch.dict(os.environ, {"WORKING_GIF": "True"}, clear=False),
            ):
                file = make_working_gif_file()

            self.assertIsNotNone(file)
            file.close()

    def test_make_working_gif_file_treats_falsey_string_as_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            gif_path = Path(tmp) / WORKING_GIF_FILENAME
            gif_path.write_bytes(b"GIF89a")

            with (
                mock.patch("src.status.WORKING_GIF_PATH", gif_path),
                mock.patch.dict(os.environ, {"WORKING_GIF": "0"}, clear=False),
            ):
                self.assertIsNone(make_working_gif_file())

    def test_make_working_gif_file_returns_none_when_missing_even_if_enabled(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch("src.status.WORKING_GIF_PATH", Path(tmp) / "missing.gif"),
            mock.patch.dict(os.environ, {"WORKING_GIF": "true"}, clear=False),
        ):
            self.assertIsNone(make_working_gif_file())


class SpinnerBackoffTests(unittest.TestCase):
    """Issue #19: the edit interval grows as a job runs long, so total edits
    against Discord's per-channel edit bucket stop scaling linearly with job
    duration."""

    def test_the_first_edit_still_lands_at_the_base_interval(self):
        # Backing off must not delay the first sign that the job started --
        # the curve is there to thin out *later* ticks.
        delays = _record_sleeps(target_edits=1)

        self.assertEqual(delays[0], EDIT_INTERVAL_SECONDS)

    def test_each_successive_wait_grows_by_the_configured_factor(self):
        delays = _record_sleeps(target_edits=5)

        expected = [EDIT_INTERVAL_SECONDS * EDIT_INTERVAL_GROWTH**i for i in range(5)]
        for actual, want in zip(delays[:5], expected, strict=True):
            self.assertAlmostEqual(actual, want, places=6)
        # Sanity: this is a real increase, not a rounding artefact.
        self.assertGreater(delays[4], delays[0] * 2)

    def test_the_interval_stops_growing_at_the_ceiling(self):
        # Unbounded growth would eventually leave a long job with no visible
        # sign of life for minutes at a time.
        delays = _record_sleeps(target_edits=40)

        self.assertLessEqual(max(delays), MAX_EDIT_INTERVAL_SECONDS)
        self.assertEqual(delays[-1], MAX_EDIT_INTERVAL_SECONDS)

    def test_the_ceiling_is_reached_within_the_first_couple_of_minutes(self):
        # If the curve took most of a job to flatten, the cap would be doing
        # no work on the 5-10 minute jobs this exists for.
        delays = _record_sleeps(target_edits=40)

        elapsed = 0.0
        for index, delay in enumerate(delays):
            elapsed += delay
            if delay == MAX_EDIT_INTERVAL_SECONDS:
                self.assertLess(elapsed, 120)
                self.assertLess(index, 12)
                return
        self.fail("the interval never reached the ceiling")

    def test_a_ten_minute_job_costs_far_fewer_edits_than_a_flat_interval(self):
        # Issue #19's completion criterion, measured against the curve the
        # loader actually runs rather than by re-deriving it from constants.
        delays = _record_sleeps(target_edits=200)

        job_seconds = 600.0
        elapsed = 0.0
        edits = 0
        for delay in delays:
            elapsed += delay
            if elapsed > job_seconds:
                break
            edits += 1

        flat_interval_edits = job_seconds / EDIT_INTERVAL_SECONDS
        self.assertEqual(flat_interval_edits, 240)
        self.assertLess(edits, flat_interval_edits / 5)
        # ...while still updating often enough to read as a live job.
        self.assertGreater(edits, 10)

    def test_the_backoff_curve_is_overridable_for_callers_and_tests(self):
        delays = _record_sleeps(target_edits=4, interval=1.0, growth=3.0, max_interval=5.0)

        self.assertEqual(delays[:4], [1.0, 3.0, 5.0, 5.0])


class SpinnerFailureLoggingTests(unittest.TestCase):
    def test_a_loader_killed_by_discord_says_so_in_the_log(self):
        # Issue #19/#25: the loop used to swallow DiscordException and stop,
        # so a rate-limited spinner froze the status line with no trace of why.
        async def scenario():
            class DeadMessage:
                async def edit(self, *, content=None):
                    raise discord.DiscordException("rate limited")

            task = asyncio.create_task(
                run_spinning_loader(DeadMessage(), "job-123", interval=0)
            )
            for _ in range(50):
                await asyncio.sleep(0)
                if task.done():
                    break
            self.assertTrue(task.done())
            await stop_spinning_loader(task)

        with self.assertLogs("src.status", level="WARNING") as captured:
            asyncio.run(scenario())

        self.assertTrue(any("stopping loader" in line for line in captured.output))
        # exc_info=True: the reason the spinner died is in the log too, which
        # is the whole point -- a frozen status line is otherwise unexplained.
        self.assertTrue(any("DiscordException" in line for line in captured.output))


class QueuedStatusTests(unittest.TestCase):
    def test_format_queued_status_states_the_queue_position_and_job_name(self):
        text = format_queued_status("job-abc123", 2)

        self.assertEqual(QUEUED_MESSAGE, "대기 중")
        self.assertIn(f"**{QUEUED_MESSAGE} (앞에 2건)**", text)
        self.assertIn("`job-abc123`", text)
        # It must not read as a running job -- that is the whole point of
        # issue #6's distinct queued ack.
        self.assertNotIn(WORKING_MESSAGE, text)

    def test_format_queued_status_never_renders_a_negative_backlog(self):
        self.assertIn("앞에 0건", format_queued_status("job-abc123", -3))


class ProgressEventFeedTests(unittest.TestCase):
    """Migrated from the deleted tail_stream_progress tests (issue #3).

    The properties those tests protected -- events reach the renderer, none
    are lost, none are recorded twice, malformed or multi-byte content never
    kills progress reporting -- still matter. What changed is the transport:
    run_job now calls JobProgress.record_event directly via ``on_event``
    instead of writing stream.jsonl for a 0.5s poller to parse back.
    """

    def test_progress_reaches_the_rendered_ack_as_soon_as_an_event_arrives(self):
        # Was: test_tail_stream_progress_updates_from_real_stream_log.
        # Same end-to-end claim (a real stream event ends up on screen)
        # without the disk round-trip -- and without waiting a poll
        # interval for it, which is issue #3's completion criterion.
        async def scenario():
            progress = JobProgress()
            message = FakeMessage(target_edits=1)
            task = asyncio.create_task(
                run_spinning_loader(message, "job-123", progress, interval=0)
            )
            try:
                # Exactly what run_job(on_event=progress.record_event) does.
                progress.record_event(
                    {
                        "type": "assistant",
                        "message": {"content": [{"type": "tool_use", "name": "Bash"}]},
                    }
                )
                await asyncio.wait_for(message.ready.wait(), timeout=1)
            finally:
                await stop_spinning_loader(task)
            return progress, message.edits

        progress, edits = asyncio.run(scenario())

        self.assertEqual(progress.tool, "Bash")
        self.assertEqual(progress.turn, 1)
        self.assertIn("Bash", edits[-1])

    def test_every_event_is_recorded_exactly_once(self):
        # Was: test_tail_stream_progress_does_not_lose_events_split_across_polls
        # and ..._does_not_duplicate_events_when_a_poll_batch_ends_in_a_
        # truncated_multibyte_line. Both were artifacts of reading a file a
        # writer was still appending to: a partially-written line could be
        # dropped, or re-read and re-recorded (record_event isn't
        # idempotent -- it increments `turn`), inflating the turn count.
        # A direct in-memory handoff has no such boundary, and this pins the
        # invariant those tests were really about.
        progress = JobProgress()
        events = [
            {
                "type": "assistant",
                "message": {"content": [{"type": "tool_use", "name": f"Tool{i}"}]},
            }
            for i in range(6)
        ]
        for event in events:
            progress.record_event(event)

        self.assertEqual(progress.turn, 6)
        self.assertEqual(progress.tool, "Tool5")

    def test_multibyte_event_content_records_without_any_error_log(self):
        # Was: test_tail_stream_progress_downgrades_partial_multibyte_reads_
        # to_debug. Korean payloads used to make the *decoder* raise midway
        # through a poll; there is no decoding step left, so the bar is now
        # simply that such an event records cleanly and logs nothing at
        # ERROR (which used to fire twice a second and bury real failures).
        progress = JobProgress()
        with self.assertNoLogs("src.status", level="ERROR"):
            progress.record_event(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {"type": "tool_use", "name": "한글도구"},
                            {"type": "text", "text": "한글 본문"},
                        ]
                    },
                }
            )

        self.assertEqual(progress.tool, "한글도구")
        self.assertEqual(progress.turn, 1)

    def test_a_malformed_event_does_not_stop_later_events_from_recording(self):
        # Was: test_tail_stream_progress_keeps_polling_after_a_read_error.
        # Progress is best-effort UI: one junk event must not raise into
        # run_job's stream loop nor wedge the rest of the feed.
        progress = JobProgress()
        for junk in (None, "not-a-dict", 42, [], {"type": "assistant", "message": None}):
            progress.record_event(junk)

        progress.record_event(
            {
                "type": "assistant",
                "message": {"content": [{"type": "tool_use", "name": "Read"}]},
            }
        )

        self.assertEqual(progress.tool, "Read")

    def test_status_no_longer_polls_a_stream_log_off_disk(self):
        # Was: test_tail_stream_progress_tolerates_missing_log_file, which
        # only existed because the renderer read a file it did not own.
        # Issue #3's fix is the removal itself, so that is what is pinned:
        # reintroducing the tailer (or its poll interval) fails here.
        for removed in (
            "tail_stream_progress",
            "PROGRESS_POLL_INTERVAL_SECONDS",
            "STREAM_LOG_RELATIVE_PATH",
        ):
            self.assertFalse(
                hasattr(status, removed),
                f"{removed} is back -- progress must come from run_job's "
                "on_event callback, not a stream.jsonl round-trip",
            )

        # And behaviourally: recording an event and rendering it must not
        # touch the filesystem at all, so there is no log file left to be
        # missing, partially written, or re-read.
        def refuse_open(*args, **kwargs):
            raise AssertionError("progress reporting must not open any file")

        progress = JobProgress()
        with (
            mock.patch("builtins.open", refuse_open),
            mock.patch.object(Path, "open", refuse_open),
        ):
            progress.record_event(
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "tool_use", "name": "Grep"}]},
                }
            )
            rendered = format_working_status("job-123", progress)

        self.assertIn("Grep", rendered)
