import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import src.orchestrator as orchestrator


class OrchestratorTests(unittest.TestCase):
    def test_create_job_persists_target_workdir_and_prompt_contract(self):
        with tempfile.TemporaryDirectory() as runs, tempfile.TemporaryDirectory() as project:
            with mock.patch.dict(os.environ, {"RUNS_DIR": runs}, clear=False):
                job_dir = orchestrator.create_job("테스트", project, "힌트")

            self.assertTrue(job_dir.is_dir())
            meta = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["workdir"], str(Path(project).resolve()))
            prompt = (job_dir / "prompt.md").read_text(encoding="utf-8")
            self.assertIn(str(Path(project).resolve()), prompt)
            self.assertIn(str(job_dir.resolve()), prompt)
            self.assertIn("session_state.json", prompt)
            self.assertIn("힌트", prompt)

    def test_create_job_rejects_missing_target_workdir(self):
        with tempfile.TemporaryDirectory() as runs:
            missing = Path(runs) / "missing"
            with mock.patch.dict(os.environ, {"RUNS_DIR": runs}, clear=False):
                with self.assertRaises(ValueError):
                    orchestrator.create_job("테스트", str(missing), None)

    def test_run_job_uses_persisted_target_workdir(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as runs, tempfile.TemporaryDirectory() as project:
                with mock.patch.dict(os.environ, {"RUNS_DIR": runs}, clear=False):
                    job_dir = orchestrator.create_job("테스트", project, None)
                seen = {}

                async def fake_stream(prompt, workdir=None, resume=None, system_hint=None, extra_dirs=None):
                    seen["prompt"] = prompt
                    seen["workdir"] = workdir
                    seen["resume"] = resume
                    seen["extra_dirs"] = extra_dirs
                    yield {"type": "result", "total_cost_usd": 0.01, "session_id": "s"}

                with mock.patch.object(orchestrator, "run_claude_stream", fake_stream):
                    meta = await orchestrator.run_job(job_dir, resume="s")

                self.assertEqual(seen["workdir"], str(Path(project).resolve()))
                self.assertEqual(seen["resume"], "s")
                self.assertEqual(seen["extra_dirs"], [str(job_dir.resolve())])
                self.assertEqual(meta["session_id"], "s")
                self.assertEqual(meta["workdir"], str(Path(project).resolve()))
                stream_log = job_dir / "logs" / "stream.jsonl"
                self.assertIn("total_cost_usd", stream_log.read_text(encoding="utf-8"))

        asyncio.run(scenario())

    def test_run_job_returns_continuation_workdir_from_session_state_file(self):
        async def scenario():
            with (
                tempfile.TemporaryDirectory() as runs,
                tempfile.TemporaryDirectory() as project,
            ):
                next_dir = Path(project) / "a"
                next_dir.mkdir()
                with mock.patch.dict(os.environ, {"RUNS_DIR": runs}, clear=False):
                    job_dir = orchestrator.create_job("테스트", project, None)

                async def fake_stream(prompt, workdir=None, resume=None, system_hint=None, extra_dirs=None):
                    state_path = Path(extra_dirs[0]) / "session_state.json"
                    state_path.write_text(
                        json.dumps({"workdir": str(next_dir)}, ensure_ascii=False),
                        encoding="utf-8",
                    )
                    yield {"type": "result", "total_cost_usd": 0.01, "session_id": "s"}

                with mock.patch.object(orchestrator, "run_claude_stream", fake_stream):
                    meta = await orchestrator.run_job(job_dir)

                self.assertEqual(meta["workdir"], str(next_dir.resolve()))

        asyncio.run(scenario())
