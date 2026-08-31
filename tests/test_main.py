import asyncio
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from src import main, orchestrator, runner, sessions
from src.parser import PROJECTS, Command
from src.sessions import SessionState
from src.status import QUEUED_MESSAGE, WORKING_MESSAGE, run_spinning_loader
from src.timing import SPAN_ACK, SPAN_OUTPUTS, SPAN_SPAWN, SPAN_TOTAL, JobTimings
from src.warm_pool import WarmClaudePool, WarmKey, WarmProcess


async def _spawn_sleeper():
    """A real child process standing in for a claude CLI, registered the same
    way run_claude_stream registers one: its own session, so killpg reaches
    it and only it."""
    return await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import time; time.sleep(60)",
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )


async def _reap(proc) -> None:
    if proc.returncode is None:
        proc.kill()
        await proc.wait()


class MainSessionRecoveryTests(unittest.TestCase):
    def test_shutdown_command_matches_exact_korean_exit_word(self):
        self.assertTrue(main._is_shutdown_command("종료"))
        self.assertTrue(main._is_shutdown_command("  종료\n"))
        self.assertFalse(main._is_shutdown_command("종료해줘"))

    def test_shutdown_clears_all_sessions_and_reports_processes(self):
        async def scenario():
            summary = main.TerminationSummary(
                requested=2,
                terminated=2,
                killed=1,
                still_running=0,
            )

            with (
                mock.patch.object(
                    main,
                    "terminate_active_claude_processes",
                    mock.AsyncMock(return_value=summary),
                ) as terminate,
                mock.patch.object(main, "clear_all_sessions", return_value=3) as clear_all,
            ):
                actual_summary, cleared_sessions = await main._shutdown_claude_sessions()

            return terminate, clear_all, actual_summary, cleared_sessions

        terminate, clear_all, summary, cleared_sessions = asyncio.run(scenario())
        reply = main._format_shutdown_reply(summary, cleared_sessions=cleared_sessions)

        terminate.assert_awaited_once()
        clear_all.assert_called_once_with()
        self.assertEqual(cleared_sessions, 3)
        self.assertIn("2/2개 종료", reply)
        self.assertIn("강제 종료 1개", reply)
        self.assertIn("3개 초기화", reply)

    def test_resolve_resume_and_workdir_uses_recent_channel_session(self):
        with mock.patch.object(
            main,
            "get_session_state",
            return_value=SessionState("sess-1", workdir="/tmp/project"),
        ):
            resume_id, workdir, system_hint, explicit_session = main._resolve_resume_and_workdir(
                Command(prompt="계속"),
                123,
            )

        self.assertEqual(resume_id, "sess-1")
        self.assertEqual(workdir, "/tmp/project")
        self.assertIsNone(system_hint)
        self.assertFalse(explicit_session)

    def test_resolve_resume_and_workdir_keeps_command_workdir_over_session_workdir(self):
        with mock.patch.object(
            main,
            "get_session_state",
            return_value=SessionState("sess-1", workdir="/tmp/old"),
        ):
            resume_id, workdir, _hint, explicit_session = main._resolve_resume_and_workdir(
                Command(prompt="계속", workdir="/tmp/new"),
                123,
            )

        self.assertEqual(resume_id, "sess-1")
        self.assertEqual(workdir, "/tmp/new")
        self.assertFalse(explicit_session)

    def test_resolve_resume_and_workdir_ignores_cached_state_for_explicit_session(self):
        with mock.patch.object(main, "get_session_state") as get_session_state:
            resume_id, workdir, system_hint, explicit_session = main._resolve_resume_and_workdir(
                Command(prompt="계속", session_id="sess-manual"),
                123,
            )

        get_session_state.assert_not_called()
        self.assertEqual(resume_id, "sess-manual")
        self.assertIsNone(workdir)
        self.assertIsNone(system_hint)
        self.assertTrue(explicit_session)

    # --- H2: an untagged follow-up must keep the project's rules, not just
    # its working directory. ---

    def test_resolve_restores_the_stored_hint_for_an_untagged_follow_up(self):
        with mock.patch.object(
            main,
            "get_session_state",
            return_value=SessionState(
                "sess-1",
                workdir="/tmp/avisspick",
                system_hint="어비스픽 투자 리포트 서비스. 보안 정보 출력 금지.",
            ),
        ):
            resume_id, workdir, system_hint, _explicit = main._resolve_resume_and_workdir(
                Command(prompt="API 키 설정은?"),
                123,
            )

        self.assertEqual(resume_id, "sess-1")
        self.assertEqual(workdir, "/tmp/avisspick")
        self.assertEqual(system_hint, "어비스픽 투자 리포트 서비스. 보안 정보 출력 금지.")

    def test_a_tag_on_this_message_wins_over_the_stored_hint(self):
        with mock.patch.object(
            main,
            "get_session_state",
            return_value=SessionState("sess-1", workdir="/tmp/old", system_hint="이전 힌트"),
        ):
            _resume, workdir, system_hint, _explicit = main._resolve_resume_and_workdir(
                Command(prompt="분석", workdir="/tmp/book", system_hint="새 힌트"),
                123,
            )

        self.assertEqual(workdir, "/tmp/book")
        self.assertEqual(system_hint, "새 힌트")

    def test_missing_conversation_error_is_detected(self):
        self.assertTrue(
            main._is_missing_conversation_error(
                {
                    "type": "error",
                    "text": "No conversation found with session ID: stale",
                }
            )
        )
        self.assertFalse(
            main._is_missing_conversation_error(
                {
                    "type": "result",
                    "text": "No conversation found with session ID: stale",
                }
            )
        )

    def test_missing_conversation_error_is_detected_in_an_in_band_result_event(self):
        # M1: a warm process reports a fatal stale --resume in band, as a
        # result event whose message lives under "result", not "text".
        # Reading only "text" left the channel pinned to a dead session
        # forever, because the recovery path never fired.
        self.assertTrue(
            main._is_missing_conversation_error(
                {
                    "type": "result",
                    "is_error": True,
                    "result": "No conversation found with session ID: stale",
                }
            )
        )
        self.assertTrue(
            main._is_missing_conversation_error(
                {
                    "type": "error",
                    "error": "No conversation found with session ID: stale",
                }
            )
        )
        # A successful result that merely mentions the phrase is not an error.
        self.assertFalse(
            main._is_missing_conversation_error(
                {
                    "type": "result",
                    "is_error": False,
                    "result": "No conversation found with session ID: stale",
                }
            )
        )

    def test_in_band_stale_result_triggers_the_same_recovery_as_a_dead_process(self):
        async def scenario():
            calls = []

            async def fake_run_job(job_dir, resume=None, *, on_event=None, timings=None,
                                   scope=None):
                calls.append(resume)
                if len(calls) == 1:
                    return {
                        "type": "result",
                        "is_error": True,
                        "result": "No conversation found with session ID: stale",
                    }
                return {"type": "result", "session_id": "fresh"}

            with (
                mock.patch.object(main, "run_job", fake_run_job),
                mock.patch.object(main, "clear_session") as clear_session,
            ):
                meta = await main._run_job_with_session_recovery(
                    Path("job"),
                    resume_id="stale",
                    channel_id=123,
                    explicit_session=False,
                )
            return calls, clear_session, meta

        calls, clear_session, meta = asyncio.run(scenario())

        self.assertEqual(calls, ["stale", None])
        clear_session.assert_called_once_with(123)
        self.assertEqual(meta["session_id"], "fresh")

    def test_a_stale_session_retry_archives_the_first_attempts_spans(self):
        # M2: both CLI runs belong to one Discord message. Without
        # start_attempt the second run's spans overwrite the first's and
        # timings.json reports the round trip as faster than it was.
        async def scenario():
            calls = []

            async def fake_run_job(job_dir, resume=None, *, on_event=None, timings=None,
                                   scope=None):
                calls.append(resume)
                timings.record_ms(SPAN_SPAWN, 1500 if len(calls) == 1 else 20)
                if len(calls) == 1:
                    return {
                        "type": "error",
                        "text": "No conversation found with session ID: stale",
                    }
                return {"type": "result", "session_id": "fresh"}

            timings = JobTimings("job")
            with (
                mock.patch.object(main, "run_job", fake_run_job),
                mock.patch.object(main, "clear_session"),
            ):
                await main._run_job_with_session_recovery(
                    Path("job"),
                    resume_id="stale",
                    channel_id=123,
                    explicit_session=False,
                    timings=timings,
                )
            return timings.snapshot()

        snapshot = asyncio.run(scenario())

        self.assertEqual(snapshot["meta"]["attempts"], 2)
        self.assertEqual(len(snapshot["attempts"]), 1)
        self.assertEqual(snapshot["attempts"][0]["reason"], "stale_session_retry")
        # The abandoned attempt's cost is preserved, not overwritten.
        self.assertEqual(snapshot["attempts"][0]["spans_ms"][SPAN_SPAWN], 1500)
        self.assertEqual(snapshot["spans_ms"][SPAN_SPAWN], 20)

    def test_auto_resume_missing_conversation_clears_and_retries_fresh(self):
        async def scenario():
            calls = []
            forwarded = []

            async def fake_run_job(job_dir, resume=None, *, on_event=None, timings=None, scope=None):
                calls.append(resume)
                forwarded.append((on_event, timings, scope))
                if len(calls) == 1:
                    return {
                        "type": "error",
                        "text": "No conversation found with session ID: stale",
                    }
                return {"type": "result", "session_id": "fresh"}

            def on_event(event):
                return None

            timings = JobTimings("job")
            scope = main.JobProcessScope()

            with (
                mock.patch.object(main, "run_job", fake_run_job),
                mock.patch.object(main, "clear_session") as clear_session,
            ):
                meta = await main._run_job_with_session_recovery(
                    Path("job"),
                    resume_id="stale",
                    channel_id=123,
                    explicit_session=False,
                    on_event=on_event,
                    timings=timings,
                    scope=scope,
                )

            return calls, clear_session, meta, forwarded, on_event, timings, scope

        calls, clear_session, meta, forwarded, on_event, timings, scope = asyncio.run(scenario())

        self.assertEqual(calls, ["stale", None])
        clear_session.assert_called_once_with(123)
        # Contract v3: the retry keeps the same progress callback and the
        # same timing recorder, so a recovered job still reports progress
        # and still lands in timings.json. The retry's process must also land
        # in the same per-job scope, or a timeout during the retry would find
        # nothing of its own to reap.
        self.assertEqual(
            forwarded, [(on_event, timings, scope), (on_event, timings, scope)]
        )
        self.assertEqual(meta["session_id"], "fresh")
        self.assertTrue(meta["retried_without_stale_session"])
        self.assertEqual(meta["stale_session_id"], "stale")

    def test_explicit_missing_session_does_not_retry_or_clear(self):
        async def scenario():
            calls = []

            async def fake_run_job(job_dir, resume=None, *, on_event=None, timings=None, scope=None):
                calls.append(resume)
                return {
                    "type": "error",
                    "text": "No conversation found with session ID: stale",
                }

            with (
                mock.patch.object(main, "run_job", fake_run_job),
                mock.patch.object(main, "clear_session") as clear_session,
            ):
                meta = await main._run_job_with_session_recovery(
                    Path("job"),
                    resume_id="stale",
                    channel_id=123,
                    explicit_session=True,
                )

            return calls, clear_session, meta

        calls, clear_session, meta = asyncio.run(scenario())

        self.assertEqual(calls, ["stale"])
        clear_session.assert_not_called()
        self.assertEqual(meta["type"], "error")


class FakeChannel:
    def __init__(self, channel_id: int = 111):
        self.id = channel_id
        self.sent = []

    async def send(self, content=None, *, files=None):
        self.sent.append({"content": content, "files": files})


class FakeAck:
    def __init__(self):
        self.edits = []

    async def edit(self, *, content=None):
        self.edits.append(content)


class FakeAuthor:
    bot = False
    id = 1


class FakeMessage:
    def __init__(self, text: str, channel: FakeChannel, ack: FakeAck):
        self.clean_content = text
        self.channel = channel
        self.author = FakeAuthor()
        self._ack = ack
        self.reply_calls = []

    async def reply(self, content=None, *, file=None):
        self.reply_calls.append({"content": content, "file": file})
        return self._ack


class OnMessageFlowTests(unittest.TestCase):
    def test_on_message_acks_immediately_without_gif_and_forwards_text_body(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                job_dir = Path(tmp) / "job-abc123"
                (job_dir / "logs").mkdir(parents=True)

                ack = FakeAck()
                channel = FakeChannel()
                msg = FakeMessage("테스트 요청", channel, ack)

                async def fake_run_job(
                    passed_job_dir, resume=None, *, on_event=None, timings=None, scope=None
                ):
                    self_check.append((passed_job_dir, on_event, timings))
                    return {
                        "type": "result",
                        "session_id": "sess-1",
                        "workdir": tmp,
                        "text_body": "실제 최종 답변",
                    }

                self_check = []

                with (
                    mock.patch.object(main, "is_authorized", return_value=True),
                    mock.patch.object(main, "allocate_job", return_value=job_dir),
                    mock.patch.object(main, "run_job", fake_run_job),
                    mock.patch.object(main, "get_session_state", return_value=None),
                    mock.patch.object(main, "set_session") as set_session,
                    mock.patch.object(main, "send_outputs", mock.AsyncMock()) as send_outputs,
                    # m4: isolate from the ambient environment -- must stay
                    # gif-off even when the suite runs with WORKING_GIF=1.
                    mock.patch.dict(os.environ, {"WORKING_GIF": "0"}, clear=False),
                ):
                    await main.on_message(msg)

                return msg, ack, self_check, set_session, send_outputs

        msg, ack, run_job_calls, set_session, send_outputs = asyncio.run(scenario())

        # Contract v3: run_job is handed the progress callback (issue #3 --
        # no stream.jsonl round-trip) and the job's timing recorder (#7).
        self.assertEqual(len(run_job_calls), 1)
        _job_dir, on_event, timings = run_job_calls[0]
        self.assertTrue(callable(on_event))
        self.assertIsInstance(timings, JobTimings)

        # AC1: ack sent immediately, no GIF attached (WORKING_GIF unset).
        self.assertEqual(len(msg.reply_calls), 1)
        self.assertIsNone(msg.reply_calls[0]["file"])
        self.assertIn("작업중입니다", msg.reply_calls[0]["content"])

        # Final ack edit reflects success.
        self.assertTrue(ack.edits)
        self.assertIn("작업 완료", ack.edits[-1])

        set_session.assert_called_once_with(
            111, "sess-1", workdir=mock.ANY, system_hint=None
        )

        # AC4: send_outputs receives body_text=meta.get("text_body").
        send_outputs.assert_awaited_once()
        _, kwargs = send_outputs.call_args
        self.assertEqual(kwargs.get("body_text"), "실제 최종 답변")
        self.assertTrue(kwargs.get("warn_missing_manifest"))

    def test_on_message_attaches_gif_when_working_gif_env_enabled(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                job_dir = Path(tmp) / "job-def456"
                (job_dir / "logs").mkdir(parents=True)

                ack = FakeAck()
                channel = FakeChannel()
                msg = FakeMessage("테스트 요청", channel, ack)

                async def fake_run_job(
                    passed_job_dir, resume=None, *, on_event=None, timings=None, scope=None
                ):
                    return {"type": "result", "session_id": "sess-2", "workdir": tmp}

                with (
                    mock.patch.object(main, "is_authorized", return_value=True),
                    mock.patch.object(main, "allocate_job", return_value=job_dir),
                    mock.patch.object(main, "run_job", fake_run_job),
                    mock.patch.object(main, "get_session_state", return_value=None),
                    mock.patch.object(main, "set_session"),
                    mock.patch.object(main, "send_outputs", mock.AsyncMock()),
                    mock.patch.dict(os.environ, {"WORKING_GIF": "1"}, clear=False),
                ):
                    await main.on_message(msg)

                return msg

        msg = asyncio.run(scenario())

        self.assertEqual(len(msg.reply_calls), 1)
        gif_file = msg.reply_calls[0]["file"]
        self.assertIsNotNone(gif_file)
        gif_file.close()  # m5: avoid a ResourceWarning from the open handle

    def test_on_message_completes_and_forwards_answer_even_if_a_status_task_crashes(self):
        # m6 / M1 regression, migrated from the deleted stream tailer to the
        # background task that remains (the spinner): a status task is
        # best-effort UI. If it dies with an exception, the job still
        # completed successfully and the user must still get their answer via
        # send_outputs -- the crash must not abort on_message's cleanup path.
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                job_dir = Path(tmp) / "job-crash1"
                (job_dir / "logs").mkdir(parents=True)

                ack = FakeAck()
                channel = FakeChannel()
                msg = FakeMessage("테스트 요청", channel, ack)

                async def crashing_loader(message, job_name, progress=None, **kwargs):
                    raise RuntimeError("boom: status task crashed")

                async def fake_run_job(
                    passed_job_dir, resume=None, *, on_event=None, timings=None, scope=None
                ):
                    # A real run_job does real async subprocess I/O with many
                    # suspension points, during which the event loop dispatches
                    # the background task created just before this await. A
                    # fake with zero internal awaits wouldn't give the loop a
                    # chance to actually run (and crash) crashing_loader before
                    # cleanup -- this sleep(0) reproduces that real scheduling
                    # so the test genuinely exercises the crash, not a no-op.
                    await asyncio.sleep(0)
                    return {
                        "type": "result",
                        "session_id": "sess-3",
                        "workdir": tmp,
                        "text_body": "완료된 답변",
                    }

                # assertLogs positively proves the status task really crashed
                # and was routed through status.stop_spinning_loader's
                # swallow-and-log path -- not just that the happy-path
                # outcome happens to look right regardless of scheduling.
                with (
                    mock.patch.object(main, "is_authorized", return_value=True),
                    mock.patch.object(main, "allocate_job", return_value=job_dir),
                    mock.patch.object(main, "run_job", fake_run_job),
                    mock.patch.object(main, "run_spinning_loader", crashing_loader),
                    mock.patch.object(main, "get_session_state", return_value=None),
                    mock.patch.object(main, "set_session"),
                    mock.patch.object(main, "send_outputs", mock.AsyncMock()) as send_outputs,
                    mock.patch.dict(os.environ, {"WORKING_GIF": "0"}, clear=False),
                    self.assertLogs("src.status", level="ERROR"),
                ):
                    await main.on_message(msg)

                return ack, send_outputs

        ack, send_outputs = asyncio.run(scenario())

        # The job succeeded, so the ack must reflect completion, not failure.
        self.assertTrue(ack.edits)
        self.assertIn("작업 완료", ack.edits[-1])

        # And the answer must actually reach the user.
        send_outputs.assert_awaited_once()
        _, kwargs = send_outputs.call_args
        self.assertEqual(kwargs.get("body_text"), "완료된 답변")


class RacyAck:
    """Fake ack message whose `edit()` has a uniform artificial network
    delay, and records edits in COMPLETION order (matching real Discord:
    whichever edit's HTTP round-trip finishes last is what the user sees).

    Used to reproduce N1: if the spinner loader is still alive while the
    except branch's failure edit is in flight, the loader can tick during
    that window and its own edit call -- started later, so it also
    *completes* later under a uniform delay -- clobbers the failure text.

    Each edit is shielded from the caller's own cancellation: a real HTTP
    edit request, once dispatched, isn't un-sent just because the local
    asyncio task awaiting its response gets cancelled -- Discord can still
    process it and the message can still change. stop_spinning_loader's
    task.cancel() must therefore happen *before* the loader ever starts an
    edit, not merely be trusted to abort one already in flight.
    """

    def __init__(self, *, delay: float = 0.05):
        self.edits = []
        self._delay = delay

    async def edit(self, *, content=None):
        await asyncio.shield(self._complete(content))

    async def _complete(self, content):
        await asyncio.sleep(self._delay)
        self.edits.append(content)


class OnMessageFailureRaceTests(unittest.TestCase):
    def test_failure_ack_is_not_clobbered_by_a_still_running_spinner(self):
        # N1 regression: moving task cleanup into `finally` (M1 fix) meant
        # the loader task could still be alive *while* the except branch's
        # ack.edit("작업 실패") round-trips over the network. The loader
        # must be stopped BEFORE that edit is even attempted, not just
        # eventually in `finally` -- otherwise a still-ticking spinner edit
        # can complete after the failure edit and overwrite it.
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                job_dir = Path(tmp) / "job-race"
                (job_dir / "logs").mkdir(parents=True)

                # Uniform delay on every edit call, spinner or failure alike
                # -- mirrors the reviewer's repro (both ack.edit calls
                # delayed), not a delay targeted at one specific message.
                ack = RacyAck(delay=0.05)
                channel = FakeChannel()
                msg = FakeMessage("테스트 요청", channel, ack)

                async def fake_run_job(
                    passed_job_dir, resume=None, *, on_event=None, timings=None, scope=None
                ):
                    raise RuntimeError("boom: job crashed")

                def fast_loader(message, job_name, progress=None, *, interval=None):
                    # A tight interval so the spinner would get a full
                    # tick-and-edit cycle in during the failure edit's
                    # artificial delay window if it were not stopped first.
                    return run_spinning_loader(message, job_name, progress, interval=0.01)

                with (
                    mock.patch.object(main, "is_authorized", return_value=True),
                    mock.patch.object(main, "allocate_job", return_value=job_dir),
                    mock.patch.object(main, "run_job", fake_run_job),
                    mock.patch.object(main, "run_spinning_loader", fast_loader),
                    mock.patch.object(main, "get_session_state", return_value=None),
                    mock.patch.object(main, "send_outputs", mock.AsyncMock()),
                    mock.patch.dict(os.environ, {"WORKING_GIF": "0"}, clear=False),
                ):
                    await main.on_message(msg)

                # Let any shielded in-flight edit (see RacyAck) actually
                # finish landing before we inspect the recorded history --
                # on_message returning doesn't mean a shielded background
                # completion has resolved yet.
                await asyncio.sleep(0.2)

                return ack

        ack = asyncio.run(scenario())

        self.assertTrue(ack.edits)
        self.assertIn("작업 실패", ack.edits[-1])
        # A stale spinner edit must never be the last word.
        self.assertNotIn("작업중입니다", ack.edits[-1])


class JobLimitConfigTests(unittest.TestCase):
    """Issue #6 knobs. Both are read at use time so a running bot picks up a
    changed .env on the next message rather than needing a restart."""

    def test_job_timeout_defaults_to_ten_minutes(self):
        for value in ({}, {"JOB_TIMEOUT_SECONDS": ""}, {"JOB_TIMEOUT_SECONDS": "잘못된값"}):
            with mock.patch.dict(os.environ, value, clear=True):
                self.assertEqual(main._job_timeout_seconds(), 600.0)

    def test_job_timeout_reads_the_configured_value(self):
        with mock.patch.dict(os.environ, {"JOB_TIMEOUT_SECONDS": "45.5"}, clear=True):
            self.assertEqual(main._job_timeout_seconds(), 45.5)

    def test_non_positive_job_timeout_disables_the_deadline(self):
        # asyncio.wait_for(timeout=None) waits exactly as the pre-#6 code did.
        for raw in ("0", "-1"):
            with mock.patch.dict(os.environ, {"JOB_TIMEOUT_SECONDS": raw}, clear=True):
                self.assertIsNone(main._job_timeout_seconds())

    def test_max_concurrent_jobs_defaults_to_two_and_never_drops_below_one(self):
        for value, expected in (
            ({}, 2),
            ({"MAX_CONCURRENT_JOBS": ""}, 2),
            ({"MAX_CONCURRENT_JOBS": "여러개"}, 2),
            ({"MAX_CONCURRENT_JOBS": "4"}, 4),
            ({"MAX_CONCURRENT_JOBS": "0"}, 1),
            ({"MAX_CONCURRENT_JOBS": "-3"}, 1),
        ):
            with mock.patch.dict(os.environ, value, clear=True):
                self.assertEqual(main._max_concurrent_jobs(), expected)


class OnMessageTimeoutTests(unittest.TestCase):
    def test_timed_out_job_reports_the_timeout_and_still_delivers_partial_output(self):
        # Issue #6: a runaway session used to sit at "작업중입니다" forever.
        # It must now end at the deadline, say so, and hand over whatever the
        # job had already written -- via the real send_outputs, so "부분 결과
        # 전달" is proven end to end rather than by a mock's call args.
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                job_dir = Path(tmp) / "job-timeout1"
                (job_dir / "logs").mkdir(parents=True)
                (job_dir / "output.md").write_text("여기까지 진행된 부분 결과", encoding="utf-8")

                ack = FakeAck()
                channel = FakeChannel()
                msg = FakeMessage("오래 걸리는 요청", channel, ack)

                async def hanging_run_job(
                    passed_job_dir, resume=None, *, on_event=None, timings=None, scope=None
                ):
                    await asyncio.sleep(30)
                    raise AssertionError("run_job should have been cancelled")

                summary = main.TerminationSummary(
                    requested=1, terminated=1, killed=0, still_running=0
                )

                with (
                    mock.patch.object(main, "is_authorized", return_value=True),
                    mock.patch.object(main, "allocate_job", return_value=job_dir),
                    mock.patch.object(main, "run_job", hanging_run_job),
                    mock.patch.object(main, "get_session_state", return_value=None),
                    mock.patch.object(main, "set_session") as set_session,
                    mock.patch.object(
                        main,
                        "terminate_job_processes",
                        mock.AsyncMock(return_value=summary),
                    ) as terminate,
                    mock.patch.object(
                        main,
                        "terminate_active_claude_processes",
                        mock.AsyncMock(return_value=summary),
                    ) as terminate_globally,
                    mock.patch.dict(
                        os.environ,
                        {"JOB_TIMEOUT_SECONDS": "0.05", "WORKING_GIF": "0"},
                        clear=False,
                    ),
                ):
                    await main.on_message(msg)
                    terminate_globally.assert_not_awaited()

                recorded = json.loads(
                    (job_dir / "timings.json").read_text(encoding="utf-8")
                )
                return ack, channel, terminate, set_session, job_dir.name, recorded

        ack, channel, terminate, set_session, job_name, recorded = asyncio.run(scenario())

        self.assertTrue(ack.edits)
        self.assertIn("작업 시간 초과", ack.edits[-1])
        self.assertIn(job_name, ack.edits[-1])
        self.assertNotIn(WORKING_MESSAGE, ack.edits[-1])

        # The stream was cancelled mid-flight, so the CLI it spawned has to
        # be reaped rather than left running unread -- but only the processes
        # belonging to *this* job (H1). The global reap stays reserved for 종료.
        terminate.assert_awaited_once()
        (scope_arg,), _ = terminate.call_args
        self.assertIsInstance(scope_arg, main.JobProcessScope)

        # Partial output actually reached the channel.
        sent = [entry["content"] for entry in channel.sent if entry["content"]]
        self.assertTrue(any("여기까지 진행된 부분 결과" in text for text in sent))

        # No result event arrived, so there is no session id to remember.
        set_session.assert_not_called()

        # Instrumentation survives the failure path (issue #7).
        self.assertEqual(recorded["job_id"], job_name)
        self.assertIn(SPAN_TOTAL, recorded["spans_ms"])
        self.assertTrue(recorded["meta"].get("timed_out"))

    @unittest.skipIf(os.name == "nt", "posix process-group termination only")
    def test_timed_out_job_leaves_no_active_claude_process(self):
        # Issue #6's completion criterion, measured with the function the
        # issue names: get_active_claude_process_count() must be 0 afterwards.
        # A real child process is registered exactly the way run_claude_stream
        # registers one -- own session so killpg reaches it, and in the job's
        # own JobProcessScope (H1) -- and nothing about the termination path
        # is mocked.
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                job_dir = Path(tmp) / "job-timeout2"
                (job_dir / "logs").mkdir(parents=True)

                proc = await _spawn_sleeper()
                runner._ACTIVE_CLAUDE_PROCESSES.add(proc)
                try:
                    before = runner.get_active_claude_process_count()

                    ack = FakeAck()
                    channel = FakeChannel()
                    msg = FakeMessage("무한 루프에 빠진 요청", channel, ack)

                    async def hanging_run_job(
                        passed_job_dir, resume=None, *, on_event=None, timings=None, scope=None
                    ):
                        scope.add(proc)
                        await asyncio.sleep(60)

                    with (
                        mock.patch.object(main, "is_authorized", return_value=True),
                        mock.patch.object(main, "allocate_job", return_value=job_dir),
                        mock.patch.object(main, "run_job", hanging_run_job),
                        mock.patch.object(main, "get_session_state", return_value=None),
                        mock.patch.object(main, "set_session"),
                        mock.patch.object(main, "send_outputs", mock.AsyncMock()),
                        mock.patch.dict(
                            os.environ,
                            {"JOB_TIMEOUT_SECONDS": "0.05", "WORKING_GIF": "0"},
                            clear=False,
                        ),
                    ):
                        await main.on_message(msg)

                    after = runner.get_active_claude_process_count()
                    returncode = proc.returncode
                finally:
                    runner._ACTIVE_CLAUDE_PROCESSES.discard(proc)
                    await _reap(proc)

                return before, after, returncode, ack

        before, after, returncode, ack = asyncio.run(scenario())

        self.assertGreaterEqual(before, 1)  # the process really was live
        self.assertEqual(after, 0)
        self.assertIsNotNone(returncode)  # and really exited
        self.assertIn("작업 시간 초과", ack.edits[-1])

    @unittest.skipIf(os.name == "nt", "posix process-group termination only")
    def test_a_timeout_in_one_channel_does_not_kill_another_channels_job(self):
        # H1: _handle_job_timeout used to call the *global* reaper, so a
        # runaway job in one channel SIGTERM'd every claude process the bot
        # had -- including a healthy job in another channel, whose user then
        # got "작업 실패" having done nothing wrong. Verified live by the
        # verifier at MAX_CONCURRENT_JOBS=2: job B's subprocess came back with
        # returncode -15 while still inside its own budget.
        #
        # Three collateral targets, matching that repro: a healthy concurrent
        # job's process, and a third channel's *parked warm* process (the
        # global path also drains the whole warm pool). Real processes, real
        # termination path, nothing mocked: only the runaway must be reaped.
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                slow_dir = Path(tmp) / "job-slow"
                healthy_dir = Path(tmp) / "job-healthy"
                for path in (slow_dir, healthy_dir):
                    (path / "logs").mkdir(parents=True)

                slow_proc = await _spawn_sleeper()
                healthy_proc = await _spawn_sleeper()
                for proc in (slow_proc, healthy_proc):
                    runner._ACTIVE_CLAUDE_PROCESSES.add(proc)

                # A third channel's conversation, parked in the warm pool and
                # not involved in either job.
                pool = WarmClaudePool(retire=mock.AsyncMock())
                parked = WarmProcess(
                    FakeWarmProc(),
                    WarmKey(workdir="/tmp/other", model="sonnet", system_hint=None),
                )
                parked.session_id = "sess-bystander"
                pool.park(parked)

                healthy_registered = asyncio.Event()
                release_healthy = asyncio.Event()

                slow_ack, healthy_ack = FakeAck(), FakeAck()
                slow_msg = FakeMessage("멈춘 요청", FakeChannel(111), slow_ack)
                healthy_msg = FakeMessage("정상 요청", FakeChannel(222), healthy_ack)

                async def fake_run_job(
                    passed_job_dir, resume=None, *, on_event=None, timings=None, scope=None
                ):
                    if passed_job_dir == slow_dir:
                        scope.add(slow_proc)
                        await asyncio.sleep(60)
                        raise AssertionError("the slow job should have timed out")
                    scope.add(healthy_proc)
                    healthy_registered.set()
                    await release_healthy.wait()
                    return {
                        "type": "result",
                        "session_id": "sess-healthy",
                        "workdir": tmp,
                        "text_body": "정상 답변",
                    }

                try:
                    with (
                        mock.patch.object(main, "is_authorized", return_value=True),
                        mock.patch.object(main, "run_job", fake_run_job),
                        mock.patch.object(
                            main, "allocate_job", side_effect=[healthy_dir, slow_dir]
                        ),
                        # The real pool runner's terminator consults.
                        mock.patch.object(runner, "_WARM_POOL", pool),
                        mock.patch.object(main, "get_session_state", return_value=None),
                        mock.patch.object(main, "set_session"),
                        mock.patch.object(main, "send_outputs", mock.AsyncMock()),
                        mock.patch.dict(
                            os.environ,
                            {
                                # Generous for the healthy job, which reads the
                                # deadline as it starts; tightened below so only
                                # the runaway one blows it.
                                "JOB_TIMEOUT_SECONDS": "30",
                                "MAX_CONCURRENT_JOBS": "2",
                                "WORKING_GIF": "0",
                            },
                            clear=False,
                        ),
                    ):
                        healthy_task = asyncio.create_task(main.on_message(healthy_msg))
                        await asyncio.wait_for(healthy_registered.wait(), timeout=5)

                        os.environ["JOB_TIMEOUT_SECONDS"] = "0.4"
                        await main.on_message(slow_msg)

                        # The healthy job's process must have survived the
                        # other channel's timeout; it only finishes now.
                        healthy_alive = healthy_proc.returncode is None
                        # The bystander conversation must still be parked and
                        # usable -- the global reaper would have drained it.
                        still_parked = [wp.session_id for wp in pool.idle_processes()]
                        bystander_closed = parked.proc.stdin.closed

                        release_healthy.set()
                        await asyncio.wait_for(healthy_task, timeout=5)

                    pool.reset()
                    return (
                        slow_proc.returncode,
                        healthy_alive,
                        healthy_proc.returncode,
                        still_parked,
                        bystander_closed,
                        slow_ack,
                        healthy_ack,
                    )
                finally:
                    for proc in (slow_proc, healthy_proc):
                        runner._ACTIVE_CLAUDE_PROCESSES.discard(proc)
                        await _reap(proc)

        (
            slow_rc,
            healthy_alive,
            healthy_rc,
            still_parked,
            bystander_closed,
            slow_ack,
            healthy_ack,
        ) = asyncio.run(scenario())

        self.assertIsNotNone(slow_rc)  # the runaway job's own process died
        self.assertTrue(healthy_alive)  # the other channel's did not
        self.assertIsNone(healthy_rc)
        self.assertEqual(still_parked, ["sess-bystander"])
        self.assertFalse(bystander_closed)
        self.assertIn("작업 시간 초과", slow_ack.edits[-1])
        self.assertIn("작업 완료", healthy_ack.edits[-1])


class OnMessageConcurrencyTests(unittest.TestCase):
    def test_a_job_over_the_limit_acks_as_queued_then_switches_to_working(self):
        # Issue #6: three messages in a row used to spawn three claude CLIs
        # that slowed each other down, with no queue and no notice. The
        # overflow job must say "대기 중" *immediately* -- and the spinner
        # must not be started early, or its 2.5s tick would overwrite that
        # notice with "작업중입니다" while nothing was actually running.
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                first = Path(tmp) / "job-first"
                second = Path(tmp) / "job-second"
                for path in (first, second):
                    (path / "logs").mkdir(parents=True)

                first_started = asyncio.Event()
                release_first = asyncio.Event()

                async def fake_run_job(
                    job_dir, resume=None, *, on_event=None, timings=None, scope=None
                ):
                    if job_dir == first:
                        first_started.set()
                        await release_first.wait()
                    return {
                        "type": "result",
                        "session_id": "sess-q",
                        "workdir": tmp,
                        "text_body": "답변",
                    }

                ack_one, ack_two = FakeAck(), FakeAck()
                channel = FakeChannel()
                msg_one = FakeMessage("첫 요청", channel, ack_one)
                msg_two = FakeMessage("두 번째 요청", channel, ack_two)

                with (
                    mock.patch.object(main, "is_authorized", return_value=True),
                    mock.patch.object(main, "allocate_job", side_effect=[first, second]),
                    mock.patch.object(main, "run_job", fake_run_job),
                    mock.patch.object(main, "get_session_state", return_value=None),
                    mock.patch.object(main, "set_session"),
                    mock.patch.object(main, "send_outputs", mock.AsyncMock()),
                    mock.patch.dict(
                        os.environ,
                        {"MAX_CONCURRENT_JOBS": "1", "WORKING_GIF": "0"},
                        clear=False,
                    ),
                ):
                    task_one = asyncio.create_task(main.on_message(msg_one))
                    await asyncio.wait_for(first_started.wait(), timeout=2)

                    task_two = asyncio.create_task(main.on_message(msg_two))
                    for _ in range(200):
                        if msg_two.reply_calls:
                            break
                        await asyncio.sleep(0)

                    queued_ack = msg_two.reply_calls[0]["content"]
                    # Still queued: the only slot is held by the first job.
                    first_still_running = not task_one.done()
                    edits_while_queued = list(ack_two.edits)

                    release_first.set()
                    await asyncio.wait_for(
                        asyncio.gather(task_one, task_two), timeout=5
                    )

                return queued_ack, first_still_running, edits_while_queued, ack_two.edits

        queued_ack, first_still_running, edits_while_queued, edits = asyncio.run(scenario())

        self.assertTrue(first_still_running)
        self.assertIn(QUEUED_MESSAGE, queued_ack)
        self.assertIn("앞에 1건", queued_ack)
        self.assertNotIn(WORKING_MESSAGE, queued_ack)

        # Nothing overwrote the notice while the job was still waiting.
        self.assertEqual(edits_while_queued, [])

        # Once the slot frees, it switches to the normal working status and
        # finishes like any other job.
        self.assertIn(WORKING_MESSAGE, edits[0])
        self.assertIn("작업 완료", edits[-1])

    def test_a_job_within_the_limit_acks_as_working_without_queueing(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                job_dir = Path(tmp) / "job-solo"
                (job_dir / "logs").mkdir(parents=True)

                ack = FakeAck()
                channel = FakeChannel()
                msg = FakeMessage("요청", channel, ack)

                async def fake_run_job(
                    passed_job_dir, resume=None, *, on_event=None, timings=None, scope=None
                ):
                    return {"type": "result", "workdir": tmp, "text_body": "답변"}

                with (
                    mock.patch.object(main, "is_authorized", return_value=True),
                    mock.patch.object(main, "allocate_job", return_value=job_dir),
                    mock.patch.object(main, "run_job", fake_run_job),
                    mock.patch.object(main, "get_session_state", return_value=None),
                    mock.patch.object(main, "set_session"),
                    mock.patch.object(main, "send_outputs", mock.AsyncMock()),
                    mock.patch.dict(
                        os.environ,
                        {"MAX_CONCURRENT_JOBS": "2", "WORKING_GIF": "0"},
                        clear=False,
                    ),
                ):
                    await main.on_message(msg)

                return msg

        msg = asyncio.run(scenario())

        self.assertIn(WORKING_MESSAGE, msg.reply_calls[0]["content"])
        self.assertNotIn(QUEUED_MESSAGE, msg.reply_calls[0]["content"])


class JobTimingIntegrationTests(unittest.TestCase):
    @staticmethod
    async def _run_one_job(tmp, job_dir, ack, env):
        channel = FakeChannel()
        msg = FakeMessage("계측 대상 요청", channel, ack)

        async def fake_run_job(
            passed_job_dir, resume=None, *, on_event=None, timings=None, scope=None
        ):
            return {
                "type": "result",
                "session_id": "sess-t",
                "workdir": tmp,
                "text_body": "답변",
            }

        with (
            mock.patch.object(main, "is_authorized", return_value=True),
            mock.patch.object(main, "allocate_job", return_value=job_dir),
            mock.patch.object(main, "run_job", fake_run_job),
            mock.patch.object(main, "get_session_state", return_value=None),
            mock.patch.object(main, "set_session"),
            mock.patch.object(main, "send_outputs", mock.AsyncMock()),
            mock.patch.dict(os.environ, {"WORKING_GIF": "0", **env}, clear=False),
        ):
            await main.on_message(msg)

    def test_a_finished_job_records_mains_spans_in_timings_json(self):
        # Issue #7: main.py owns t_ack (message in -> ack out), t_outputs
        # (run_job returned -> send_outputs done) and t_total (message in ->
        # everything delivered). The CLI-side spans are filled in by
        # runner/orchestrator through the same object.
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                job_dir = Path(tmp) / "job-timing1"
                (job_dir / "logs").mkdir(parents=True)
                await self._run_one_job(tmp, job_dir, FakeAck(), {"DEBUG_TIMING": "0"})
                return json.loads((job_dir / "timings.json").read_text(encoding="utf-8"))

        recorded = asyncio.run(scenario())

        self.assertEqual(recorded["job_id"], "job-timing1")
        for span in (SPAN_ACK, SPAN_OUTPUTS, SPAN_TOTAL):
            self.assertIn(span, recorded["spans_ms"])
        # t_total covers the whole request, so it can't be shorter than the
        # ack it contains.
        self.assertGreaterEqual(
            recorded["spans_ms"][SPAN_TOTAL], recorded["spans_ms"][SPAN_ACK]
        )

    def test_debug_timing_appends_the_span_summary_to_the_final_ack(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                job_dir = Path(tmp) / "job-timing2"
                (job_dir / "logs").mkdir(parents=True)
                ack = FakeAck()
                await self._run_one_job(tmp, job_dir, ack, {"DEBUG_TIMING": "1"})
                return ack.edits

        edits = asyncio.run(scenario())

        self.assertIn("작업 완료", edits[-1])
        self.assertIn(SPAN_TOTAL, edits[-1])
        self.assertEqual(len(edits[-1].splitlines()), 2)

    def test_a_crashed_job_still_records_its_timings(self):
        # A job that blew up is exactly the one whose span breakdown is worth
        # keeping, so the failure path must reach the same bookkeeping.
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                job_dir = Path(tmp) / "job-timing4"
                (job_dir / "logs").mkdir(parents=True)

                ack = FakeAck()
                channel = FakeChannel()
                msg = FakeMessage("실패하는 요청", channel, ack)

                async def failing_run_job(
                    passed_job_dir, resume=None, *, on_event=None, timings=None, scope=None
                ):
                    raise RuntimeError("boom")

                with (
                    mock.patch.object(main, "is_authorized", return_value=True),
                    mock.patch.object(main, "allocate_job", return_value=job_dir),
                    mock.patch.object(main, "run_job", failing_run_job),
                    mock.patch.object(main, "get_session_state", return_value=None),
                    mock.patch.object(main, "set_session"),
                    mock.patch.object(main, "send_outputs", mock.AsyncMock()),
                    mock.patch.dict(
                        os.environ, {"WORKING_GIF": "0", "DEBUG_TIMING": "0"}, clear=False
                    ),
                ):
                    await main.on_message(msg)

                recorded = json.loads(
                    (job_dir / "timings.json").read_text(encoding="utf-8")
                )
                return ack, channel, recorded

        ack, channel, recorded = asyncio.run(scenario())

        self.assertIn("작업 실패", ack.edits[-1])
        self.assertTrue(any("내부 오류" in (e["content"] or "") for e in channel.sent))
        self.assertEqual(recorded["job_id"], "job-timing4")
        self.assertIn(SPAN_ACK, recorded["spans_ms"])
        self.assertIn(SPAN_TOTAL, recorded["spans_ms"])

    def test_timing_summary_is_not_shown_to_users_by_default(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                job_dir = Path(tmp) / "job-timing3"
                (job_dir / "logs").mkdir(parents=True)
                ack = FakeAck()
                await self._run_one_job(tmp, job_dir, ack, {"DEBUG_TIMING": "0"})
                return ack.edits

        edits = asyncio.run(scenario())

        self.assertEqual(edits[-1].splitlines(), [edits[-1]])
        self.assertNotIn(SPAN_TOTAL, edits[-1])


class ProgressWiringTests(unittest.TestCase):
    def test_an_event_run_job_reports_reaches_the_ack_with_no_disk_round_trip(self):
        # Issue #3's completion criterion, end to end: run_job hands the
        # event straight to JobProgress.record_event, so the next spinner
        # tick renders it -- no stream.jsonl write, no 0.5s poll, and the
        # job directory's log file is never even read.
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                job_dir = Path(tmp) / "job-progress"
                (job_dir / "logs").mkdir(parents=True)

                ack = FakeAck()
                channel = FakeChannel()
                msg = FakeMessage("도구를 쓰는 요청", channel, ack)

                async def fake_run_job(
                    passed_job_dir, resume=None, *, on_event=None, timings=None, scope=None
                ):
                    on_event(
                        {
                            "type": "assistant",
                            "message": {
                                "content": [{"type": "tool_use", "name": "Bash"}]
                            },
                        }
                    )
                    for _ in range(200):
                        if any("Bash" in (edit or "") for edit in ack.edits):
                            break
                        await asyncio.sleep(0.01)
                    return {"type": "result", "workdir": tmp, "text_body": "답변"}

                def fast_loader(message, job_name, progress=None, *, interval=None):
                    return run_spinning_loader(message, job_name, progress, interval=0.01)

                with (
                    mock.patch.object(main, "is_authorized", return_value=True),
                    mock.patch.object(main, "allocate_job", return_value=job_dir),
                    mock.patch.object(main, "run_job", fake_run_job),
                    mock.patch.object(main, "run_spinning_loader", fast_loader),
                    mock.patch.object(main, "get_session_state", return_value=None),
                    mock.patch.object(main, "set_session"),
                    mock.patch.object(main, "send_outputs", mock.AsyncMock()),
                    mock.patch.dict(os.environ, {"WORKING_GIF": "0"}, clear=False),
                ):
                    await main.on_message(msg)

                stream_log = job_dir / "logs" / "stream.jsonl"
                return ack.edits, stream_log.exists()

        edits, wrote_stream_log = asyncio.run(scenario())

        self.assertTrue(any("Bash" in (edit or "") for edit in edits))
        self.assertIn("작업 완료", edits[-1])
        # main.py never touches the stream log -- progress no longer depends
        # on it existing at all.
        self.assertFalse(wrote_stream_log)


class ProjectHintPersistenceTests(unittest.TestCase):
    """H2: `@avisspick 분석해줘` followed by an untagged `API 키 설정은?`.

    The project's rules ride --append-system-prompt and are therefore re-sent
    on every turn, but the hint that produces them used to live only in the
    message that carried the tag. The follow-up kept the project's working
    directory and quietly lost its constraints ("보안 정보 출력 금지") -- the
    worst possible half-failure. The hint now lives in the session.
    """

    def test_an_untagged_follow_up_keeps_the_projects_rules_and_warm_key(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                projects = Path(tmp) / "projects"
                (projects / "avisspick").mkdir(parents=True)
                runs = Path(tmp) / "runs"
                runs.mkdir()
                store = Path(tmp) / "sessions.json"
                project_dir = projects / "avisspick"

                seen = []

                async def fake_run_job(
                    job_dir, resume=None, *, on_event=None, timings=None, scope=None
                ):
                    seen.append((job_dir, resume))
                    return {
                        "type": "result",
                        "session_id": "sess-avisspick",
                        "workdir": str(project_dir),
                        "text_body": "답변",
                    }

                async def send(text):
                    await main.on_message(FakeMessage(text, FakeChannel(777), FakeAck()))

                with (
                    mock.patch.object(main, "is_authorized", return_value=True),
                    mock.patch.object(main, "run_job", fake_run_job),
                    mock.patch.object(main, "send_outputs", mock.AsyncMock()),
                    mock.patch.object(sessions, "_STORE_PATH", store),
                    mock.patch.dict(
                        os.environ,
                        {
                            "RUNS_DIR": str(runs),
                            "PROJECT_ROOT": str(projects),
                            "WORKING_GIF": "0",
                        },
                        clear=False,
                    ),
                ):
                    await send("@avisspick 보안 점검해줘")
                    await send("API 키 설정은?")
                    stored = sessions.get_session_state(777)

                metas = [
                    json.loads((job_dir / "job.json").read_text(encoding="utf-8"))
                    for job_dir, _ in seen
                ]
                return metas, [resume for _, resume in seen], stored, str(project_dir.resolve())

        metas, resumes, stored, project_path = asyncio.run(scenario())

        self.assertEqual(len(metas), 2)
        # Turn 2 continues the same conversation, in the same directory...
        self.assertEqual(resumes, [None, "sess-avisspick"])
        self.assertEqual(metas[1]["workdir"], project_path)
        # ...and, the actual fix, under the same constraints.
        self.assertIn("보안 정보 출력 금지", metas[0]["system_prompt"])
        self.assertIn("보안 정보 출력 금지", metas[1]["system_prompt"])
        self.assertEqual(metas[0]["system_prompt"], metas[1]["system_prompt"])

        # The hint is what makes that survivable across restarts, so it has to
        # be in the store, not just in this process.
        self.assertEqual(stored.session_id, "sess-avisspick")
        self.assertEqual(stored.system_hint, PROJECTS["@avisspick"][1])

    def test_a_stable_hint_keeps_the_warm_pool_key_stable_between_turns(self):
        # Side effect of the same fix, worth pinning: WarmKey includes the
        # system hint, so a hint that evaporates on turn 2 changes the key and
        # the parked process from turn 1 can never be reused. Issue #2's whole
        # point is that turn 2 is the turn that gets to skip the 1.4~1.6s
        # cold spawn.
        turn_one = orchestrator._build_system_prompt(PROJECTS["@avisspick"][1])
        turn_two_fixed = orchestrator._build_system_prompt(PROJECTS["@avisspick"][1])
        turn_two_broken = orchestrator._build_system_prompt(None)

        def key(system_prompt):
            return WarmKey(
                workdir="/tmp/avisspick",
                model=runner.DEFAULT_CLAUDE_MODEL,
                system_hint=system_prompt,
            )

        self.assertEqual(key(turn_one), key(turn_two_fixed))
        self.assertNotEqual(key(turn_one), key(turn_two_broken))


class QueuedJobSessionFreshnessTests(unittest.TestCase):
    def test_a_queued_job_resumes_the_session_the_job_ahead_of_it_created(self):
        # M3: the session used to be resolved when the message arrived, i.e.
        # before the job had a concurrency slot. With MAX_CONCURRENT_JOBS=1
        # the queue therefore only *looked* like it serialized the channel:
        # the second turn still continued from the conversation as it stood
        # before the first turn answered, losing that answer's context.
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                runs = Path(tmp) / "runs"
                runs.mkdir()
                store = Path(tmp) / "sessions.json"

                seen = []
                first_started = asyncio.Event()
                release_first = asyncio.Event()

                async def fake_run_job(
                    job_dir, resume=None, *, on_event=None, timings=None, scope=None
                ):
                    seen.append(resume)
                    if len(seen) == 1:
                        first_started.set()
                        await release_first.wait()
                        return {
                            "type": "result",
                            "session_id": "sess-A",
                            "workdir": tmp,
                            "text_body": "첫 답변",
                        }
                    return {
                        "type": "result",
                        "session_id": "sess-B",
                        "workdir": tmp,
                        "text_body": "두 번째 답변",
                    }

                ack_two = FakeAck()
                channel = FakeChannel(888)
                msg_one = FakeMessage("첫 요청", channel, FakeAck())
                msg_two = FakeMessage("이어서 알려줘", channel, ack_two)

                with (
                    mock.patch.object(main, "is_authorized", return_value=True),
                    mock.patch.object(main, "run_job", fake_run_job),
                    mock.patch.object(main, "send_outputs", mock.AsyncMock()),
                    mock.patch.object(sessions, "_STORE_PATH", store),
                    mock.patch.dict(
                        os.environ,
                        {
                            "RUNS_DIR": str(runs),
                            "MAX_CONCURRENT_JOBS": "1",
                            "WORKING_GIF": "0",
                        },
                        clear=False,
                    ),
                ):
                    task_one = asyncio.create_task(main.on_message(msg_one))
                    await asyncio.wait_for(first_started.wait(), timeout=5)

                    task_two = asyncio.create_task(main.on_message(msg_two))
                    for _ in range(500):
                        if msg_two.reply_calls:
                            break
                        await asyncio.sleep(0)
                    queued_ack = msg_two.reply_calls[0]["content"]

                    release_first.set()
                    await asyncio.wait_for(asyncio.gather(task_one, task_two), timeout=10)

                return seen, queued_ack, ack_two.edits

        seen, queued_ack, edits = asyncio.run(scenario())

        # The queued job still says so immediately -- the ack must not wait
        # for the slot even though everything else now does.
        self.assertIn(QUEUED_MESSAGE, queued_ack)
        self.assertIn("앞에 1건", queued_ack)

        self.assertEqual(seen, [None, "sess-A"])
        self.assertIn("작업 완료", edits[-1])


class FakeStdin:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class FakeWarmProc:
    def __init__(self):
        self.returncode = None
        self.stdin = FakeStdin()
        self.stdout = None
        self.stderr = None


class ClearCommandWarmProcessTests(unittest.TestCase):
    def test_clear_reclaims_this_channels_warm_process_only(self):
        # S1: /clear answered "세션을 초기화했습니다" while the warm process
        # still holding that conversation stayed parked for its full idle TTL
        # (300s) -- the answer and the reality disagreed. Other channels'
        # parked processes must not be touched.
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                store = Path(tmp) / "sessions.json"
                pool = WarmClaudePool(retire=mock.AsyncMock())

                key = WarmKey(workdir="/tmp/p", model="sonnet", system_hint=None)
                mine = WarmProcess(FakeWarmProc(), key)
                mine.session_id = "sess-mine"
                other = WarmProcess(FakeWarmProc(), key)
                other.session_id = "sess-other"
                for wp in (mine, other):
                    self.assertEqual(pool.park(wp), [])

                ack = FakeAck()
                msg = FakeMessage("/clear", FakeChannel(777), ack)

                with (
                    mock.patch.object(main, "is_authorized", return_value=True),
                    mock.patch.object(main, "get_warm_pool", return_value=pool),
                    mock.patch.object(sessions, "_STORE_PATH", store),
                ):
                    sessions.set_session(777, "sess-mine", workdir="/tmp/p")
                    sessions.set_session(999, "sess-other", workdir="/tmp/p")
                    await main.on_message(msg)

                    still_parked = [wp.session_id for wp in pool.idle_processes()]
                    cleared = sessions.get_session_state(777)
                    untouched = sessions.get_session_state(999)

                pool.reset()  # cancel the surviving entry's expiry task
                return still_parked, mine, other, cleared, untouched, msg.reply_calls

        still_parked, mine, other, cleared, untouched, replies = asyncio.run(scenario())

        # This channel's process is out of the pool and told to exit...
        self.assertEqual(still_parked, ["sess-other"])
        self.assertTrue(mine.proc.stdin.closed)
        # ...and the other channel's conversation is completely unaffected.
        self.assertFalse(other.proc.stdin.closed)
        self.assertIsNone(cleared)
        self.assertEqual(untouched.session_id, "sess-other")
        self.assertIn("세션을 초기화했습니다", replies[0]["content"])

    def test_clear_without_a_stored_session_touches_no_warm_process(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                store = Path(tmp) / "sessions.json"
                pool = WarmClaudePool(retire=mock.AsyncMock())
                key = WarmKey(workdir="/tmp/p", model="sonnet", system_hint=None)
                other = WarmProcess(FakeWarmProc(), key)
                other.session_id = "sess-other"
                pool.park(other)

                msg = FakeMessage("/clear", FakeChannel(777), FakeAck())
                with (
                    mock.patch.object(main, "is_authorized", return_value=True),
                    mock.patch.object(main, "get_warm_pool", return_value=pool),
                    mock.patch.object(sessions, "_STORE_PATH", store),
                ):
                    await main.on_message(msg)
                    parked = [wp.session_id for wp in pool.idle_processes()]

                pool.reset()
                return parked, other

        parked, other = asyncio.run(scenario())
        self.assertEqual(parked, ["sess-other"])
        self.assertFalse(other.proc.stdin.closed)


class SessionWriteFailureTests(unittest.TestCase):
    def test_a_failed_session_write_still_delivers_the_answer(self):
        # L4: set_session runs after run_job but before send_outputs, so an
        # OSError escaping it skipped the delivery entirely and the user lost
        # a completed answer over a bookkeeping failure.
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                runs = Path(tmp) / "runs"
                runs.mkdir()
                # The session store's parent is a regular file, so every write
                # to it raises NotADirectoryError.
                blocker = Path(tmp) / "blocker"
                blocker.write_text("not a directory", encoding="utf-8")

                ack = FakeAck()
                msg = FakeMessage("요청", FakeChannel(555), ack)

                async def fake_run_job(
                    job_dir, resume=None, *, on_event=None, timings=None, scope=None
                ):
                    return {
                        "type": "result",
                        "session_id": "sess-1",
                        "workdir": tmp,
                        "text_body": "반드시 전달되어야 하는 답변",
                    }

                with (
                    mock.patch.object(main, "is_authorized", return_value=True),
                    mock.patch.object(main, "run_job", fake_run_job),
                    mock.patch.object(main, "send_outputs", mock.AsyncMock()) as send_outputs,
                    mock.patch.object(sessions, "_STORE_PATH", blocker / "sessions.json"),
                    mock.patch.dict(
                        os.environ,
                        {"RUNS_DIR": str(runs), "WORKING_GIF": "0"},
                        clear=False,
                    ),
                    self.assertLogs("src.sessions", level="WARNING"),
                ):
                    await main.on_message(msg)  # must not raise

                return ack, send_outputs

        ack, send_outputs = asyncio.run(scenario())

        send_outputs.assert_awaited_once()
        _, kwargs = send_outputs.call_args
        self.assertEqual(kwargs.get("body_text"), "반드시 전달되어야 하는 답변")
        self.assertIn("작업 완료", ack.edits[-1])


class AckIsNotBlockedByJobFileWritesTests(unittest.TestCase):
    """L5 / issue #3 suggestion 4: create_job's synchronous prompt.md +
    job.json writes used to run before the ack the user is waiting on."""

    def test_no_job_file_is_written_before_the_ack_goes_out(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                job_dir = Path(tmp) / "job-ack-order"
                (job_dir / "logs").mkdir(parents=True)

                snapshot = {}

                class SnapshottingMessage(FakeMessage):
                    async def reply(self, content=None, *, file=None):
                        # What exists on disk at the moment the ack is sent.
                        snapshot["prompt_md"] = (job_dir / "prompt.md").exists()
                        snapshot["job_json"] = (job_dir / "job.json").exists()
                        return await super().reply(content, file=file)

                msg = SnapshottingMessage("요청", FakeChannel(321), FakeAck())

                async def fake_run_job(
                    passed_job_dir, resume=None, *, on_event=None, timings=None, scope=None
                ):
                    return {"type": "result", "workdir": tmp, "text_body": "답변"}

                with (
                    mock.patch.object(main, "is_authorized", return_value=True),
                    mock.patch.object(main, "allocate_job", return_value=job_dir),
                    mock.patch.object(main, "run_job", fake_run_job),
                    mock.patch.object(main, "get_session_state", return_value=None),
                    mock.patch.object(main, "set_session"),
                    mock.patch.object(main, "send_outputs", mock.AsyncMock()),
                    mock.patch.dict(os.environ, {"WORKING_GIF": "0"}, clear=False),
                ):
                    await main.on_message(msg)

                return snapshot, (job_dir / "prompt.md").exists(), (job_dir / "job.json").exists()

        snapshot, prompt_after, job_after = asyncio.run(scenario())

        # Nothing written yet when the user got their ack...
        self.assertFalse(snapshot["prompt_md"])
        self.assertFalse(snapshot["job_json"])
        # ...and both written by the time the job actually ran.
        self.assertTrue(prompt_after)
        self.assertTrue(job_after)

    def test_the_job_files_are_written_off_the_event_loop_thread(self):
        # The writes moved after the ack, but they are still on the request
        # path -- they must not block the loop and stall other channels.
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                job_dir = Path(tmp) / "job-thread"
                (job_dir / "logs").mkdir(parents=True)

                threads = []
                real_prepare = orchestrator.prepare_job

                def recording_prepare(*args, **kwargs):
                    threads.append(threading.current_thread())
                    return real_prepare(*args, **kwargs)

                msg = FakeMessage("요청", FakeChannel(322), FakeAck())

                async def fake_run_job(
                    passed_job_dir, resume=None, *, on_event=None, timings=None, scope=None
                ):
                    return {"type": "result", "workdir": tmp, "text_body": "답변"}

                with (
                    mock.patch.object(main, "is_authorized", return_value=True),
                    mock.patch.object(main, "allocate_job", return_value=job_dir),
                    mock.patch.object(main, "prepare_job", recording_prepare),
                    mock.patch.object(main, "run_job", fake_run_job),
                    mock.patch.object(main, "get_session_state", return_value=None),
                    mock.patch.object(main, "set_session"),
                    mock.patch.object(main, "send_outputs", mock.AsyncMock()),
                    mock.patch.dict(os.environ, {"WORKING_GIF": "0"}, clear=False),
                ):
                    await main.on_message(msg)

                return threads

        threads = asyncio.run(scenario())
        self.assertEqual(len(threads), 1)
        self.assertNotEqual(threads[0], threading.main_thread())
