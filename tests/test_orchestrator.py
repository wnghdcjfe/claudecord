import asyncio
import json
import os
import shutil
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from src import orchestrator
from src.runner import JobProcessScope
from src.timing import SPAN_RESULT, JobTimings

# prompt.md is now just "사용자 요청 + 작업 디렉터리 + 산출물 디렉터리" (issue #5): the
# invariant rule block moved to job.json's system_prompt / --append-system-prompt,
# so a typical prompt.md is ~220 chars. This budget is generous enough to absorb
# longer tmp-dir paths on other machines/CI while still failing hard if the rule
# block (~400 chars) ever leaks back into the per-turn body.
PROMPT_BUDGET_CHARS = 500


def _assistant_event(*texts, tool_calls=0, session_id="s"):
    content = [{"type": "text", "text": text} for text in texts]
    content += [
        {"type": "tool_use", "id": f"t{i}", "name": "Read", "input": {}}
        for i in range(tool_calls)
    ]
    return {"type": "assistant", "message": {"content": content}, "session_id": session_id}


def _stream(*events):
    async def fake_stream(prompt, workdir=None, resume=None, system_hint=None, extra_dirs=None, **kwargs):
        for event in events:
            yield event

    return fake_stream


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
            self.assertIn(orchestrator.RESULT_META, prompt)
            # The caller-supplied hint ("힌트") is no longer re-typed into the
            # per-turn body -- it now lives in job.json's system_prompt.
            self.assertNotIn("힌트", prompt)
            self.assertIn("힌트", meta["system_prompt"])

    def test_create_job_prompt_excludes_bookkeeping_and_rule_block(self):
        with tempfile.TemporaryDirectory() as runs, tempfile.TemporaryDirectory() as project:
            with mock.patch.dict(os.environ, {"RUNS_DIR": runs}, clear=False):
                job_dir = orchestrator.create_job("테스트", project, None)

            prompt = (job_dir / "prompt.md").read_text(encoding="utf-8")
            self.assertNotIn("manifest.json", prompt)
            self.assertNotIn(orchestrator.SESSION_STATE, prompt)
            # The rule block (issue #5) moved out of the per-turn body entirely.
            self.assertNotIn("마지막 메시지", prompt)
            self.assertNotIn("경우에만", prompt)

    def test_create_job_system_prompt_carries_rule_block_verbatim(self):
        with tempfile.TemporaryDirectory() as runs, tempfile.TemporaryDirectory() as project:
            with mock.patch.dict(os.environ, {"RUNS_DIR": runs}, clear=False):
                job_dir = orchestrator.create_job("테스트", project, None)

            meta = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))
            system_prompt = meta["system_prompt"]
            self.assertNotIn("manifest.json", system_prompt)
            self.assertNotIn(orchestrator.SESSION_STATE, system_prompt)
            self.assertIn("마지막 메시지", system_prompt)
            self.assertIn("경우에만", system_prompt)
            self.assertIn(orchestrator.RESULT_META, system_prompt)
            # Wording must match the original rule block exactly (outputs.py /
            # main.py depend on the contract it describes).
            self.assertEqual(system_prompt, orchestrator._RULE_BLOCK)

    def test_create_job_prompt_stays_within_budget(self):
        with tempfile.TemporaryDirectory() as runs, tempfile.TemporaryDirectory() as project:
            with mock.patch.dict(os.environ, {"RUNS_DIR": runs}, clear=False):
                job_dir = orchestrator.create_job("테스트", project, None)

            prompt = (job_dir / "prompt.md").read_text(encoding="utf-8")
            self.assertLessEqual(len(prompt), PROMPT_BUDGET_CHARS)

    def test_create_job_rejects_missing_target_workdir(self):
        with tempfile.TemporaryDirectory() as runs:
            missing = Path(runs) / "missing"
            with (
                mock.patch.dict(os.environ, {"RUNS_DIR": runs}, clear=False),
                self.assertRaises(ValueError),
            ):
                orchestrator.create_job("테스트", str(missing), None)

    def test_run_job_uses_persisted_target_workdir(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as runs, tempfile.TemporaryDirectory() as project:
                with mock.patch.dict(os.environ, {"RUNS_DIR": runs}, clear=False):
                    job_dir = orchestrator.create_job("테스트", project, None)
                seen = {}

                async def fake_stream(prompt, workdir=None, resume=None, system_hint=None, extra_dirs=None, **kwargs):
                    seen["prompt"] = prompt
                    seen["workdir"] = workdir
                    seen["resume"] = resume
                    seen["system_hint"] = system_hint
                    seen["extra_dirs"] = extra_dirs
                    seen["timings"] = kwargs.get("timings")
                    yield {"type": "result", "total_cost_usd": 0.01, "session_id": "s"}

                with mock.patch.object(orchestrator, "run_claude_stream", fake_stream):
                    meta = await orchestrator.run_job(job_dir, resume="s")

                self.assertEqual(seen["workdir"], str(Path(project).resolve()))
                self.assertEqual(seen["resume"], "s")
                self.assertEqual(seen["extra_dirs"], [str(job_dir.resolve())])
                # The rule block reaches the CLI via --append-system-prompt now.
                self.assertEqual(seen["system_hint"], orchestrator._RULE_BLOCK)
                self.assertIsNone(seen["timings"])
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

                async def fake_stream(prompt, workdir=None, resume=None, system_hint=None, extra_dirs=None, **kwargs):
                    state_path = Path(extra_dirs[0]) / orchestrator.SESSION_STATE
                    state_path.write_text(
                        json.dumps({"workdir": str(next_dir)}, ensure_ascii=False),
                        encoding="utf-8",
                    )
                    yield {"type": "result", "total_cost_usd": 0.01, "session_id": "s"}

                with mock.patch.object(orchestrator, "run_claude_stream", fake_stream):
                    meta = await orchestrator.run_job(job_dir)

                self.assertEqual(meta["workdir"], str(next_dir.resolve()))

        asyncio.run(scenario())


class TwoPhaseJobCreationTests(unittest.TestCase):
    """main.py names a job in its ack before it holds a concurrency slot, but
    must not read the channel's session state until it does -- so the id is
    claimed by allocate_job and the contents written later by prepare_job."""

    def test_allocate_job_reserves_a_dir_without_committing_to_a_workdir(self):
        with tempfile.TemporaryDirectory() as runs:
            with mock.patch.dict(os.environ, {"RUNS_DIR": runs}, clear=False):
                job_dir = orchestrator.allocate_job()

            self.assertTrue(job_dir.is_dir())
            self.assertTrue((job_dir / "logs").is_dir())
            self.assertTrue(job_dir.name.startswith("job-"))
            # Nothing that depends on the session is written yet.
            self.assertFalse((job_dir / "prompt.md").exists())
            self.assertFalse((job_dir / orchestrator.JOB_META).exists())

    def test_allocate_job_gives_each_call_its_own_id(self):
        with tempfile.TemporaryDirectory() as runs:
            with mock.patch.dict(os.environ, {"RUNS_DIR": runs}, clear=False):
                first = orchestrator.allocate_job()
                second = orchestrator.allocate_job()

            self.assertNotEqual(first.name, second.name)

    def test_prepare_job_fills_the_allocated_dir_and_keeps_its_id(self):
        with tempfile.TemporaryDirectory() as runs, tempfile.TemporaryDirectory() as project:
            with mock.patch.dict(os.environ, {"RUNS_DIR": runs}, clear=False):
                job_dir = orchestrator.allocate_job()
                prepared = orchestrator.prepare_job(job_dir, "테스트", project, "힌트")

            self.assertEqual(prepared, job_dir)
            meta = json.loads((job_dir / orchestrator.JOB_META).read_text(encoding="utf-8"))
            # The id in job.json must be the directory that was acked, not a
            # freshly minted one.
            self.assertEqual(meta["id"], job_dir.name)
            self.assertEqual(meta["workdir"], str(Path(project).resolve()))
            self.assertIn("힌트", meta["system_prompt"])
            self.assertIn(str(job_dir), (job_dir / "prompt.md").read_text(encoding="utf-8"))

    def test_prepare_job_rejects_a_missing_workdir_like_create_job_does(self):
        with tempfile.TemporaryDirectory() as runs:
            with mock.patch.dict(os.environ, {"RUNS_DIR": runs}, clear=False):
                job_dir = orchestrator.allocate_job()
                with self.assertRaises(ValueError):
                    orchestrator.prepare_job(job_dir, "테스트", str(Path(runs) / "nope"), None)

            # The rejected attempt must leave the dir reusable for the retry
            # main.py does with workdir=None.
            with mock.patch.dict(os.environ, {"RUNS_DIR": runs}, clear=False):
                orchestrator.prepare_job(job_dir, "테스트", None, None)
            self.assertTrue((job_dir / "prompt.md").exists())

    def test_create_job_still_allocates_and_prepares_in_one_call(self):
        # Contract v2/v3 pins create_job's signature and behaviour; it is now
        # the composition of the two halves.
        with tempfile.TemporaryDirectory() as runs, tempfile.TemporaryDirectory() as project:
            with mock.patch.dict(os.environ, {"RUNS_DIR": runs}, clear=False):
                job_dir = orchestrator.create_job("테스트", project, None)

            meta = json.loads((job_dir / orchestrator.JOB_META).read_text(encoding="utf-8"))
            self.assertEqual(meta["id"], job_dir.name)
            self.assertTrue((job_dir / "prompt.md").exists())
            self.assertTrue((job_dir / "logs").is_dir())


class JobProcessScopeForwardingTests(unittest.TestCase):
    """H1: a job's claude processes are registered in that job's own scope so a
    timeout reaps only them."""

    def _run(self, scope):
        async def scenario():
            with tempfile.TemporaryDirectory() as runs, tempfile.TemporaryDirectory() as project:
                with mock.patch.dict(os.environ, {"RUNS_DIR": runs}, clear=False):
                    job_dir = orchestrator.create_job("테스트", project, None)

                seen = {}

                async def fake_stream(prompt, workdir=None, resume=None, system_hint=None,
                                      extra_dirs=None, **kwargs):
                    seen["scope"] = kwargs.get("scope")
                    seen["kwargs"] = kwargs
                    yield {"type": "result", "session_id": "s"}

                with mock.patch.object(orchestrator, "run_claude_stream", fake_stream):
                    if scope is None:
                        await orchestrator.run_job(job_dir)
                    else:
                        await orchestrator.run_job(job_dir, scope=scope)

                return seen

        return asyncio.run(scenario())

    def test_scope_is_passed_straight_through_to_run_claude_stream(self):
        scope = JobProcessScope()
        seen = self._run(scope)
        self.assertIs(seen["scope"], scope)

    def test_scope_is_always_passed_as_a_keyword_even_when_absent(self):
        seen = self._run(None)
        self.assertIn("scope", seen["kwargs"])
        self.assertIsNone(seen["scope"])


class SystemPromptDeliveryTests(unittest.TestCase):
    """Issue #5 completion criterion: the rule block must reappear automatically
    on a resume-less retry (the stale-session recovery path in main.py calls
    run_job(job_dir, resume=None) on the *same* job_dir) without any separate
    restoration logic."""

    def test_rule_block_is_identical_across_resumed_and_resume_less_calls(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as runs, tempfile.TemporaryDirectory() as project:
                with mock.patch.dict(os.environ, {"RUNS_DIR": runs}, clear=False):
                    job_dir = orchestrator.create_job("테스트", project, "프로젝트 힌트")

                seen_hints = []

                async def fake_stream(prompt, workdir=None, resume=None, system_hint=None, extra_dirs=None, **kwargs):
                    seen_hints.append(system_hint)
                    yield {"type": "result", "session_id": "s"}

                with mock.patch.object(orchestrator, "run_claude_stream", fake_stream):
                    await orchestrator.run_job(job_dir, resume="stale-session")
                    await orchestrator.run_job(job_dir, resume=None)

                return seen_hints

        seen_hints = asyncio.run(scenario())
        self.assertEqual(len(seen_hints), 2)
        self.assertEqual(seen_hints[0], seen_hints[1])
        self.assertIn("프로젝트 힌트", seen_hints[0])
        self.assertIn("마지막 메시지", seen_hints[0])


class ContinuationWorkdirFallbackTests(unittest.TestCase):
    def _run(self, files):
        async def scenario():
            with (
                tempfile.TemporaryDirectory() as runs,
                tempfile.TemporaryDirectory() as project,
            ):
                for name in ("a", "b"):
                    (Path(project) / name).mkdir()
                with mock.patch.dict(os.environ, {"RUNS_DIR": runs}, clear=False):
                    job_dir = orchestrator.create_job("테스트", project, None)

                async def fake_stream(prompt, workdir=None, resume=None, system_hint=None, extra_dirs=None, **kwargs):
                    for name, payload in files(Path(project)).items():
                        (Path(extra_dirs[0]) / name).write_text(payload, encoding="utf-8")
                    yield {"type": "result", "session_id": "s"}

                with mock.patch.object(orchestrator, "run_claude_stream", fake_stream):
                    meta = await orchestrator.run_job(job_dir)

                return meta["workdir"], str(Path(project).resolve())

        return asyncio.run(scenario())

    def test_meta_json_workdir_wins_over_session_state(self):
        got, project = self._run(
            lambda p: {
                orchestrator.RESULT_META: json.dumps({"workdir": str(p / "a")}),
                orchestrator.SESSION_STATE: json.dumps({"workdir": str(p / "b")}),
            }
        )
        self.assertEqual(got, str((Path(project) / "a").resolve()))

    def test_falls_back_to_session_state_when_meta_json_absent(self):
        got, project = self._run(
            lambda p: {orchestrator.SESSION_STATE: json.dumps({"workdir": str(p / "b")})}
        )
        self.assertEqual(got, str((Path(project) / "b").resolve()))

    def test_falls_back_to_session_state_when_meta_json_omits_workdir(self):
        got, project = self._run(
            lambda p: {
                orchestrator.RESULT_META: json.dumps(
                    {"files": [{"path": "out.png", "label": "그림"}]}
                ),
                orchestrator.SESSION_STATE: json.dumps({"workdir": str(p / "b")}),
            }
        )
        self.assertEqual(got, str((Path(project) / "b").resolve()))

    def test_falls_back_to_claude_cwd_when_no_state_files(self):
        got, project = self._run(lambda p: {})
        self.assertEqual(got, project)

    def test_malformed_meta_json_falls_back_to_session_state(self):
        got, project = self._run(
            lambda p: {
                orchestrator.RESULT_META: "{not json",
                orchestrator.SESSION_STATE: json.dumps({"workdir": str(p / "b")}),
            }
        )
        self.assertEqual(got, str((Path(project) / "b").resolve()))

    def test_non_object_meta_json_falls_back_to_claude_cwd(self):
        got, project = self._run(lambda p: {orchestrator.RESULT_META: json.dumps(["a"])})
        self.assertEqual(got, project)

    def test_relative_meta_json_workdir_resolves_against_claude_cwd(self):
        got, project = self._run(
            lambda p: {orchestrator.RESULT_META: json.dumps({"workdir": "a"})}
        )
        self.assertEqual(got, str((Path(project) / "a").resolve()))

    def test_missing_meta_json_workdir_directory_falls_back(self):
        got, project = self._run(
            lambda p: {orchestrator.RESULT_META: json.dumps({"workdir": str(p / "nope")})}
        )
        self.assertEqual(got, project)

    def test_blank_meta_json_workdir_falls_back(self):
        got, project = self._run(
            lambda p: {orchestrator.RESULT_META: json.dumps({"workdir": "   "})}
        )
        self.assertEqual(got, project)


class TextBodyTests(unittest.TestCase):
    def _run(self, *events):
        async def scenario():
            with (
                tempfile.TemporaryDirectory() as runs,
                tempfile.TemporaryDirectory() as project,
            ):
                with mock.patch.dict(os.environ, {"RUNS_DIR": runs}, clear=False):
                    job_dir = orchestrator.create_job("테스트", project, None)

                with mock.patch.object(orchestrator, "run_claude_stream", _stream(*events)):
                    return await orchestrator.run_job(job_dir)

        return asyncio.run(scenario())

    def test_text_body_is_last_non_empty_assistant_text(self):
        meta = self._run(
            _assistant_event("첫 번째"),
            _assistant_event("마지막 답변"),
            {"type": "result", "session_id": "s"},
        )
        self.assertEqual(meta["text_body"], "마지막 답변")

    def test_tool_only_turns_do_not_clear_text_body(self):
        meta = self._run(
            _assistant_event("실제 답변"),
            _assistant_event(tool_calls=2),
            {"type": "user", "message": {"content": [{"type": "tool_result"}]}},
            {"type": "result", "session_id": "s"},
        )
        self.assertEqual(meta["text_body"], "실제 답변")

    def test_whitespace_only_assistant_text_is_ignored(self):
        meta = self._run(
            _assistant_event("유효한 답변"),
            _assistant_event("   \n  "),
            {"type": "result", "session_id": "s"},
        )
        self.assertEqual(meta["text_body"], "유효한 답변")

    def test_multiple_text_blocks_in_one_turn_are_joined(self):
        meta = self._run(
            _assistant_event("앞부분 ", "뒷부분"),
            {"type": "result", "session_id": "s"},
        )
        self.assertEqual(meta["text_body"], "앞부분 뒷부분")

    def test_text_body_is_empty_when_stream_has_no_assistant_text(self):
        meta = self._run({"type": "result", "session_id": "s"})
        self.assertEqual(meta["text_body"], "")

    def test_malformed_assistant_events_are_tolerated(self):
        meta = self._run(
            {"type": "assistant"},
            {"type": "assistant", "message": "문자열"},
            {"type": "assistant", "message": {"content": None}},
            {"type": "assistant", "message": {"content": [None, {"type": "text"}]}},
            _assistant_event("살아남은 답변"),
            {"type": "result", "session_id": "s"},
        )
        self.assertEqual(meta["text_body"], "살아남은 답변")

    def test_string_content_assistant_event_is_accepted(self):
        meta = self._run(
            {"type": "assistant", "message": {"content": "문자열 본문"}},
            {"type": "result", "session_id": "s"},
        )
        self.assertEqual(meta["text_body"], "문자열 본문")

    def test_missing_claude_cwd_still_returns_text_body_key(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as runs, tempfile.TemporaryDirectory() as project:
                with mock.patch.dict(os.environ, {"RUNS_DIR": runs}, clear=False):
                    job_dir = orchestrator.create_job("테스트", project, None)

                job_meta = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))
                job_meta["workdir"] = str(Path(project) / "gone")
                (job_dir / "job.json").write_text(
                    json.dumps(job_meta, ensure_ascii=False), encoding="utf-8"
                )
                return await orchestrator.run_job(job_dir)

        meta = asyncio.run(scenario())
        self.assertEqual(meta["type"], "error")
        self.assertEqual(meta["text_body"], "")


class OnEventCallbackTests(unittest.TestCase):
    def _run_with_on_event(self, on_event, *events):
        async def scenario():
            with tempfile.TemporaryDirectory() as runs, tempfile.TemporaryDirectory() as project:
                with mock.patch.dict(os.environ, {"RUNS_DIR": runs}, clear=False):
                    job_dir = orchestrator.create_job("테스트", project, None)

                with mock.patch.object(orchestrator, "run_claude_stream", _stream(*events)):
                    return await orchestrator.run_job(job_dir, on_event=on_event)

        return asyncio.run(scenario())

    def test_on_event_called_once_per_stream_event_in_order(self):
        events = [
            _assistant_event("첫 번째"),
            {"type": "result", "session_id": "s"},
        ]
        seen = []
        self._run_with_on_event(seen.append, *events)
        self.assertEqual(seen, events)

    def test_on_event_none_does_not_break_run_job(self):
        meta = self._run_with_on_event(
            None,
            _assistant_event("답변"),
            {"type": "result", "session_id": "s"},
        )
        self.assertEqual(meta["text_body"], "답변")

    def test_on_event_exception_is_swallowed_and_logged_not_raised(self):
        def exploding(event):
            raise RuntimeError("progress renderer is broken")

        with self.assertLogs("src.orchestrator", level="ERROR"):
            meta = self._run_with_on_event(
                exploding,
                _assistant_event("답변"),
                {"type": "result", "session_id": "s"},
            )
        # The job result must still be delivered even though every on_event
        # call raised.
        self.assertEqual(meta["text_body"], "답변")
        self.assertEqual(meta["session_id"], "s")

    def test_on_event_is_called_for_missing_claude_cwd_error_path(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as runs, tempfile.TemporaryDirectory() as project:
                with mock.patch.dict(os.environ, {"RUNS_DIR": runs}, clear=False):
                    job_dir = orchestrator.create_job("테스트", project, None)

                job_meta = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))
                job_meta["workdir"] = str(Path(project) / "gone")
                (job_dir / "job.json").write_text(
                    json.dumps(job_meta, ensure_ascii=False), encoding="utf-8"
                )
                seen = []
                meta = await orchestrator.run_job(job_dir, on_event=seen.append)
                return meta, seen

        meta, seen = asyncio.run(scenario())
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["type"], "error")
        self.assertEqual(meta["type"], "error")


class TimingsIntegrationTests(unittest.TestCase):
    def test_timings_none_does_not_break_run_job(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as runs, tempfile.TemporaryDirectory() as project:
                with mock.patch.dict(os.environ, {"RUNS_DIR": runs}, clear=False):
                    job_dir = orchestrator.create_job("테스트", project, None)

                with mock.patch.object(
                    orchestrator,
                    "run_claude_stream",
                    _stream(_assistant_event("답변"), {"type": "result", "session_id": "s"}),
                ):
                    return await orchestrator.run_job(job_dir)

        meta = asyncio.run(scenario())
        self.assertNotIn("timings", meta)
        self.assertEqual(meta["text_body"], "답변")

    def test_timings_snapshot_is_attached_and_forwarded_to_run_claude_stream(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as runs, tempfile.TemporaryDirectory() as project:
                with mock.patch.dict(os.environ, {"RUNS_DIR": runs}, clear=False):
                    job_dir = orchestrator.create_job("테스트", project, None)

                timings = JobTimings("job-x")
                seen = {}

                async def fake_stream(prompt, workdir=None, resume=None, system_hint=None, extra_dirs=None, **kwargs):
                    seen["timings"] = kwargs.get("timings")
                    yield _assistant_event("답변")
                    yield {
                        "type": "result",
                        "session_id": "s",
                        "duration_ms": 1234,
                        "duration_api_ms": 900,
                        "num_turns": 3,
                    }

                with mock.patch.object(orchestrator, "run_claude_stream", fake_stream):
                    meta = await orchestrator.run_job(job_dir, timings=timings)

                return meta, seen, timings

        meta, seen, timings = asyncio.run(scenario())
        self.assertIs(seen["timings"], timings)
        self.assertIn("timings", meta)
        self.assertEqual(meta["timings"], timings.snapshot())
        self.assertIn(SPAN_RESULT, meta["timings"]["spans_ms"])
        self.assertEqual(meta["timings"]["meta"]["duration_ms"], 1234)
        self.assertEqual(meta["timings"]["meta"]["num_turns"], 3)

    def test_span_result_covers_first_event_to_result_event(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as runs, tempfile.TemporaryDirectory() as project:
                with mock.patch.dict(os.environ, {"RUNS_DIR": runs}, clear=False):
                    job_dir = orchestrator.create_job("테스트", project, None)

                # Deterministic fake clock: 0, 1, 2, 3, ... seconds per call.
                ticks = iter(range(100))
                timings = JobTimings("job-x", clock=lambda: next(ticks))

                async def fake_stream(prompt, workdir=None, resume=None, system_hint=None, extra_dirs=None, **kwargs):
                    yield _assistant_event("답변")
                    yield {"type": "result", "session_id": "s"}

                with mock.patch.object(orchestrator, "run_claude_stream", fake_stream):
                    meta = await orchestrator.run_job(job_dir, timings=timings)

                return meta

        meta = asyncio.run(scenario())
        # JobTimings.clock is called once at __init__ (origin), then once per
        # start()/stop() -- the span must be a positive, finite duration.
        self.assertGreater(meta["timings"]["spans_ms"][SPAN_RESULT], 0)

    def test_timings_snapshot_attached_on_missing_claude_cwd_error_path(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as runs, tempfile.TemporaryDirectory() as project:
                with mock.patch.dict(os.environ, {"RUNS_DIR": runs}, clear=False):
                    job_dir = orchestrator.create_job("테스트", project, None)

                job_meta = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))
                job_meta["workdir"] = str(Path(project) / "gone")
                (job_dir / "job.json").write_text(
                    json.dumps(job_meta, ensure_ascii=False), encoding="utf-8"
                )
                timings = JobTimings("job-x")
                return await orchestrator.run_job(job_dir, timings=timings), timings

        meta, timings = asyncio.run(scenario())
        self.assertEqual(meta["timings"], timings.snapshot())


class StreamWriterTests(unittest.TestCase):
    """Issue #3: stream.jsonl must be opened once per job, not once per event."""

    def test_single_file_handle_open_for_many_writes(self):
        with tempfile.TemporaryDirectory() as runs:
            job_dir = Path(runs) / "job-x"
            (job_dir / "logs").mkdir(parents=True)

            open_calls = []
            real_open = Path.open

            def counting_open(path_self, *args, **kwargs):
                open_calls.append(path_self)
                return real_open(path_self, *args, **kwargs)

            with (
                mock.patch.object(Path, "open", counting_open),
                orchestrator._StreamWriter(job_dir) as writer,
            ):
                for i in range(5):
                    writer.write({"type": "assistant", "i": i})

            self.assertEqual(len(open_calls), 1)
            lines = (job_dir / "logs" / "stream.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 5)
            self.assertEqual(json.loads(lines[3])["i"], 3)

    def test_handle_is_closed_even_when_caller_raises(self):
        with tempfile.TemporaryDirectory() as runs:
            job_dir = Path(runs) / "job-y"
            (job_dir / "logs").mkdir(parents=True)

            with self.assertRaises(RuntimeError), orchestrator._StreamWriter(job_dir) as writer:
                writer.write({"type": "x"})
                raise RuntimeError("boom")

            content = (job_dir / "logs" / "stream.jsonl").read_text(encoding="utf-8")
            self.assertIn('"x"', content)

    def test_run_job_reuses_one_handle_across_a_multi_event_stream(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as runs, tempfile.TemporaryDirectory() as project:
                with mock.patch.dict(os.environ, {"RUNS_DIR": runs}, clear=False):
                    job_dir = orchestrator.create_job("테스트", project, None)

                events = [_assistant_event(f"turn {i}") for i in range(10)]
                events.append({"type": "result", "session_id": "s"})

                open_calls = []
                real_open = Path.open

                def counting_open(path_self, *args, **kwargs):
                    if path_self.name == "stream.jsonl":
                        open_calls.append(path_self)
                    return real_open(path_self, *args, **kwargs)

                with (
                    mock.patch.object(Path, "open", counting_open),
                    mock.patch.object(orchestrator, "run_claude_stream", _stream(*events)),
                ):
                    await orchestrator.run_job(job_dir)

                lines = (job_dir / "logs" / "stream.jsonl").read_text(encoding="utf-8").splitlines()
                return open_calls, lines

        open_calls, lines = asyncio.run(scenario())
        self.assertEqual(len(open_calls), 1)
        self.assertEqual(len(lines), 11)


def _seed_job_dir(runs_dir: Path, *, created_at: datetime) -> Path:
    """Create a real job dir (via create_job) under runs_dir, then stamp its
    job.json created_at so cleanup_old_runs' age math is deterministic
    instead of depending on wall-clock sleeps or filesystem mtime quirks."""
    with (
        tempfile.TemporaryDirectory() as project,
        mock.patch.dict(os.environ, {"RUNS_DIR": str(runs_dir)}, clear=False),
    ):
        job_dir = orchestrator.create_job("테스트", project, None)

    meta_path = job_dir / orchestrator.JOB_META
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["created_at"] = created_at.isoformat()
    meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return job_dir


class RunsRetentionDaysParsingTests(unittest.TestCase):
    """Issue #22: RUNS_RETENTION_DAYS parsing rules, isolated from the
    filesystem side of cleanup_old_runs."""

    def test_unset_env_var_uses_default(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(orchestrator.RUNS_RETENTION_DAYS_ENV_VAR, None)
            self.assertEqual(
                orchestrator._runs_retention_days(),
                orchestrator.DEFAULT_RUNS_RETENTION_DAYS,
            )

    def test_empty_string_disables_cleanup(self):
        with mock.patch.dict(
            os.environ, {orchestrator.RUNS_RETENTION_DAYS_ENV_VAR: ""}, clear=False
        ):
            self.assertIsNone(orchestrator._runs_retention_days())

    def test_zero_disables_cleanup(self):
        with mock.patch.dict(
            os.environ, {orchestrator.RUNS_RETENTION_DAYS_ENV_VAR: "0"}, clear=False
        ):
            self.assertIsNone(orchestrator._runs_retention_days())

    def test_valid_positive_value_is_used(self):
        with mock.patch.dict(
            os.environ, {orchestrator.RUNS_RETENTION_DAYS_ENV_VAR: "7"}, clear=False
        ):
            self.assertEqual(orchestrator._runs_retention_days(), 7)

    def test_non_integer_value_falls_back_to_default(self):
        with mock.patch.dict(
            os.environ, {orchestrator.RUNS_RETENTION_DAYS_ENV_VAR: "abc"}, clear=False
        ):
            with self.assertLogs("src.orchestrator", level="WARNING"):
                value = orchestrator._runs_retention_days()
            self.assertEqual(value, orchestrator.DEFAULT_RUNS_RETENTION_DAYS)

    def test_negative_value_falls_back_to_default(self):
        with mock.patch.dict(
            os.environ, {orchestrator.RUNS_RETENTION_DAYS_ENV_VAR: "-5"}, clear=False
        ):
            with self.assertLogs("src.orchestrator", level="WARNING"):
                value = orchestrator._runs_retention_days()
            self.assertEqual(value, orchestrator.DEFAULT_RUNS_RETENTION_DAYS)


class CleanupOldRunsTests(unittest.TestCase):
    """Issue #22: retention cleanup, driven entirely through
    tempfile.TemporaryDirectory + monkeypatched RUNS_DIR -- never touches the
    real ~/.claudecord."""

    def test_old_job_dir_is_removed_recent_is_kept(self):
        now = datetime(2026, 8, 31, tzinfo=UTC)
        with tempfile.TemporaryDirectory() as runs:
            old_dir = _seed_job_dir(Path(runs), created_at=now - timedelta(days=40))
            recent_dir = _seed_job_dir(Path(runs), created_at=now - timedelta(days=5))

            with mock.patch.dict(
                os.environ,
                {"RUNS_DIR": runs, orchestrator.RUNS_RETENTION_DAYS_ENV_VAR: "30"},
                clear=False,
            ):
                removed = orchestrator.cleanup_old_runs(now=now)

            self.assertEqual(removed, 1)
            self.assertFalse(old_dir.exists())
            self.assertTrue(recent_dir.exists())

    def test_zero_retention_disables_cleanup_entirely(self):
        now = datetime(2026, 8, 31, tzinfo=UTC)
        with tempfile.TemporaryDirectory() as runs:
            ancient_dir = _seed_job_dir(Path(runs), created_at=now - timedelta(days=9999))

            with mock.patch.dict(
                os.environ,
                {"RUNS_DIR": runs, orchestrator.RUNS_RETENTION_DAYS_ENV_VAR: "0"},
                clear=False,
            ):
                removed = orchestrator.cleanup_old_runs(now=now)

            self.assertEqual(removed, 0)
            self.assertTrue(ancient_dir.exists())

    def test_blank_retention_disables_cleanup_entirely(self):
        now = datetime(2026, 8, 31, tzinfo=UTC)
        with tempfile.TemporaryDirectory() as runs:
            ancient_dir = _seed_job_dir(Path(runs), created_at=now - timedelta(days=9999))

            with mock.patch.dict(
                os.environ,
                {"RUNS_DIR": runs, orchestrator.RUNS_RETENTION_DAYS_ENV_VAR: ""},
                clear=False,
            ):
                removed = orchestrator.cleanup_old_runs(now=now)

            self.assertEqual(removed, 0)
            self.assertTrue(ancient_dir.exists())

    def test_invalid_retention_falls_back_to_default_and_still_cleans_very_old_dirs(self):
        now = datetime(2026, 8, 31, tzinfo=UTC)
        with tempfile.TemporaryDirectory() as runs:
            ancient_dir = _seed_job_dir(Path(runs), created_at=now - timedelta(days=9999))
            recent_dir = _seed_job_dir(Path(runs), created_at=now - timedelta(days=1))

            with mock.patch.dict(
                os.environ,
                {"RUNS_DIR": runs, orchestrator.RUNS_RETENTION_DAYS_ENV_VAR: "not-a-number"},
                clear=False,
            ):
                removed = orchestrator.cleanup_old_runs(now=now)

            # A malformed value falls back to DEFAULT_RUNS_RETENTION_DAYS (30
            # days), not to "disabled" -- a 9999-day-old dir is still well
            # past that default, so it must still go.
            self.assertEqual(removed, 1)
            self.assertFalse(ancient_dir.exists())
            self.assertTrue(recent_dir.exists())

    def test_missing_runs_dir_returns_zero_without_error(self):
        with tempfile.TemporaryDirectory() as runs:
            missing = Path(runs) / "does-not-exist"
            with mock.patch.dict(os.environ, {"RUNS_DIR": str(missing)}, clear=False):
                removed = orchestrator.cleanup_old_runs()
            self.assertEqual(removed, 0)

    def test_non_job_prefixed_entries_are_left_alone(self):
        now = datetime(2026, 8, 31, tzinfo=UTC)
        with tempfile.TemporaryDirectory() as runs:
            stray_dir = Path(runs) / "not-a-job-dir"
            stray_dir.mkdir()
            stray_file = Path(runs) / "job-looks-like-one-but-is-a-file"
            stray_file.write_text("x", encoding="utf-8")
            old_utime = (now - timedelta(days=9999)).timestamp()
            os.utime(stray_dir, (old_utime, old_utime))
            os.utime(stray_file, (old_utime, old_utime))

            with mock.patch.dict(
                os.environ,
                {"RUNS_DIR": runs, orchestrator.RUNS_RETENTION_DAYS_ENV_VAR: "30"},
                clear=False,
            ):
                removed = orchestrator.cleanup_old_runs(now=now)

            self.assertEqual(removed, 0)
            self.assertTrue(stray_dir.exists())
            self.assertTrue(stray_file.exists())

    def test_allocate_only_job_dir_falls_back_to_mtime_without_crashing(self):
        """A job dir that never got past allocate_job (two-phase creation,
        issue #4) has no job.json yet -- cleanup must still age it off via
        directory mtime instead of raising."""
        now = datetime(2026, 8, 31, tzinfo=UTC)
        with tempfile.TemporaryDirectory() as runs:
            with mock.patch.dict(os.environ, {"RUNS_DIR": runs}, clear=False):
                job_dir = orchestrator.allocate_job()
            old_time = (now - timedelta(days=40)).timestamp()
            os.utime(job_dir, (old_time, old_time))

            with mock.patch.dict(
                os.environ,
                {"RUNS_DIR": runs, orchestrator.RUNS_RETENTION_DAYS_ENV_VAR: "30"},
                clear=False,
            ):
                removed = orchestrator.cleanup_old_runs(now=now)

            self.assertEqual(removed, 1)
            self.assertFalse(job_dir.exists())

    def test_rmtree_failure_on_one_dir_does_not_stop_cleanup_or_raise(self):
        now = datetime(2026, 8, 31, tzinfo=UTC)
        with tempfile.TemporaryDirectory() as runs:
            broken_dir = _seed_job_dir(Path(runs), created_at=now - timedelta(days=40))
            other_old_dir = _seed_job_dir(Path(runs), created_at=now - timedelta(days=40))

            real_rmtree = shutil.rmtree

            def flaky_rmtree(path, *args, **kwargs):
                # Compare by name, not full path equality: cleanup_old_runs
                # walks get_runs_dir() as configured (RUNS_DIR, possibly the
                # unresolved side of a symlink like macOS's /var ->
                # /private/var), while broken_dir came back from create_job
                # already .resolve()'d -- same directory, different string.
                # Job dir names carry a random hex suffix, so this is safe.
                if Path(path).name == broken_dir.name:
                    raise OSError("permission denied")
                return real_rmtree(path, *args, **kwargs)

            with (
                mock.patch.dict(
                    os.environ,
                    {"RUNS_DIR": runs, orchestrator.RUNS_RETENTION_DAYS_ENV_VAR: "30"},
                    clear=False,
                ),
                mock.patch.object(orchestrator.shutil, "rmtree", side_effect=flaky_rmtree),
                self.assertLogs("src.orchestrator", level="WARNING"),
            ):
                removed = orchestrator.cleanup_old_runs(now=now)

            self.assertEqual(removed, 1)
            self.assertTrue(broken_dir.exists())
            self.assertFalse(other_old_dir.exists())

    def test_failure_resolving_retention_settings_is_logged_and_does_not_raise(self):
        with tempfile.TemporaryDirectory() as runs:
            with (
                mock.patch.dict(os.environ, {"RUNS_DIR": runs}, clear=False),
                mock.patch.object(
                    orchestrator, "_runs_retention_days", side_effect=RuntimeError("boom")
                ),
                self.assertLogs("src.orchestrator", level="ERROR"),
            ):
                removed = orchestrator.cleanup_old_runs()

            self.assertEqual(removed, 0)


class PathRedactionTests(unittest.TestCase):
    """Issue #26 (orchestrator.py portion only): text that can reach Discord
    must not carry local absolute paths, while the local job/app log keeps
    the full path for diagnosis."""

    def test_missing_target_workdir_error_message_has_no_absolute_path(self):
        with tempfile.TemporaryDirectory() as runs:
            missing = Path(runs) / "missing-project"
            with (
                mock.patch.dict(os.environ, {"RUNS_DIR": runs}, clear=False),
                self.assertRaises(ValueError) as ctx,
            ):
                orchestrator.create_job("테스트", str(missing), None)

            message = str(ctx.exception)
            self.assertNotIn(str(missing.parent), message)
            self.assertNotIn(str(missing.resolve().parent), message)
            # The redacted tail (the part that actually helps diagnose which
            # request failed) must still be there.
            self.assertIn("missing-project", message)

    def test_missing_target_workdir_full_path_is_logged(self):
        with tempfile.TemporaryDirectory() as runs:
            missing = Path(runs) / "missing-project"
            with (
                mock.patch.dict(os.environ, {"RUNS_DIR": runs}, clear=False),
                self.assertLogs("src.orchestrator", level="WARNING") as logs,
                self.assertRaises(ValueError),
            ):
                orchestrator.create_job("테스트", str(missing), None)

            self.assertTrue(any(str(missing) in line for line in logs.output))

    def test_run_job_missing_claude_cwd_error_text_has_no_absolute_path(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as runs, tempfile.TemporaryDirectory() as project:
                with mock.patch.dict(os.environ, {"RUNS_DIR": runs}, clear=False):
                    job_dir = orchestrator.create_job("테스트", project, None)

                gone = Path(project) / "gone"
                job_meta = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))
                job_meta["workdir"] = str(gone)
                (job_dir / "job.json").write_text(
                    json.dumps(job_meta, ensure_ascii=False), encoding="utf-8"
                )
                return await orchestrator.run_job(job_dir), gone

        meta, gone = asyncio.run(scenario())
        self.assertNotIn(str(gone.parent), meta["text"])
        self.assertIn("gone", meta["text"])

    def test_run_job_missing_claude_cwd_stream_log_keeps_full_path(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as runs, tempfile.TemporaryDirectory() as project:
                with mock.patch.dict(os.environ, {"RUNS_DIR": runs}, clear=False):
                    job_dir = orchestrator.create_job("테스트", project, None)

                gone = Path(project) / "gone"
                job_meta = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))
                job_meta["workdir"] = str(gone)
                (job_dir / "job.json").write_text(
                    json.dumps(job_meta, ensure_ascii=False), encoding="utf-8"
                )
                await orchestrator.run_job(job_dir)
                # Read while the tempdirs are still alive -- both go away the
                # instant this `with` block exits, which a `return`
                # statement here triggers immediately.
                stream_log = (job_dir / "logs" / "stream.jsonl").read_text(encoding="utf-8")
                return stream_log, gone

        stream_log, gone = asyncio.run(scenario())
        self.assertIn(str(gone), stream_log)

    def test_run_job_missing_claude_cwd_on_event_receives_redacted_text(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as runs, tempfile.TemporaryDirectory() as project:
                with mock.patch.dict(os.environ, {"RUNS_DIR": runs}, clear=False):
                    job_dir = orchestrator.create_job("테스트", project, None)

                gone = Path(project) / "gone"
                job_meta = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))
                job_meta["workdir"] = str(gone)
                (job_dir / "job.json").write_text(
                    json.dumps(job_meta, ensure_ascii=False), encoding="utf-8"
                )
                seen = []
                await orchestrator.run_job(job_dir, on_event=seen.append)
                return seen, gone

        seen, gone = asyncio.run(scenario())
        self.assertEqual(len(seen), 1)
        self.assertNotIn(str(gone.parent), seen[0]["text"])
