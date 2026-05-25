import asyncio
import unittest
from pathlib import Path
from unittest import mock

import src.main as main


class MainSessionRecoveryTests(unittest.TestCase):
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

    def test_auto_resume_missing_conversation_clears_and_retries_fresh(self):
        async def scenario():
            calls = []

            async def fake_run_job(job_dir, resume=None):
                calls.append(resume)
                if len(calls) == 1:
                    return {
                        "type": "error",
                        "text": "No conversation found with session ID: stale",
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
        self.assertTrue(meta["retried_without_stale_session"])
        self.assertEqual(meta["stale_session_id"], "stale")

    def test_explicit_missing_session_does_not_retry_or_clear(self):
        async def scenario():
            calls = []

            async def fake_run_job(job_dir, resume=None):
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

