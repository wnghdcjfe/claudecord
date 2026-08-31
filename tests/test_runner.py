import asyncio
import gc
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src import runner
from src.runner import (
    JobProcessScope,
    build_claude_command,
    get_active_claude_process_count,
    get_warm_pool,
    run_claude_stream,
    terminate_active_claude_processes,
    terminate_job_processes,
)
from src.timing import SPAN_FIRST_EVENT, SPAN_SPAWN, JobTimings

# A stand-in for the claude CLI that speaks both input formats, so the warm
# path can be exercised for real (subprocess, pipes, stream-json framing)
# instead of being mocked away.
#
# Env knobs: FAKE_SPAWN_LOG (append one JSON line per spawn -- this is how the
# tests prove a second turn reused a process instead of spawning), FAKE_SESSION,
# FAKE_PID_FILE, FAKE_STREAM_BROKEN (fail only in streaming-input mode, to force
# the cold fallback), FAKE_HANG (never finish the turn), FAKE_HUGE (emit an
# oversized stdout line after a normal one).
FAKE_CLI = r'''#!/usr/bin/env python3
import json, os, sys, time

argv = sys.argv[1:]
stream_input = "--input-format" in argv
resume = argv[argv.index("--resume") + 1] if "--resume" in argv else None
session = resume or os.environ.get("FAKE_SESSION", "sess-1")

log = os.environ.get("FAKE_SPAWN_LOG")
if log:
    with open(log, "a") as fh:
        fh.write(json.dumps({"stream_input": stream_input, "resume": resume}) + "\n")

pid_file = os.environ.get("FAKE_PID_FILE")
if pid_file:
    with open(pid_file, "w") as fh:
        fh.write(str(os.getpid()))


def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


if stream_input and os.environ.get("FAKE_STREAM_BROKEN"):
    sys.stderr.write("streaming input not supported\n")
    sys.stderr.flush()
    sys.exit(3)


def die_on_stale_resume():
    """Mimic the real CLI's stale --resume: an in-band is_error result, then a
    moment later the process dies with the reason on stderr."""
    emit({"type": "result", "subtype": "error_during_execution", "is_error": True,
          "num_turns": 0, "duration_ms": 0, "session_id": session})
    time.sleep(0.2)
    sys.stderr.write("No conversation found with session ID: %s\n" % session)
    sys.stderr.flush()
    sys.exit(1)


def error_then_hang():
    """Same failure, but the process does not die inside the warm grace
    window: the reason exists only in the in-band result event."""
    emit({"type": "result", "subtype": "error_during_execution", "is_error": True,
          "result": "No conversation found with session ID: %s" % session,
          "num_turns": 0, "duration_ms": 0, "session_id": session})
    time.sleep(30)


def reply(turn, text):
    if turn == 1:
        emit({"type": "system", "subtype": "init", "session_id": session})
    emit({"type": "assistant", "session_id": session,
          "message": {"content": [{"type": "text", "text": "turn%d:%s" % (turn, text)}]}})
    if os.environ.get("FAKE_HUGE"):
        emit({"type": "assistant", "session_id": session,
              "message": {"content": [{"type": "text", "text": "x" * 200000}]}})
    if os.environ.get("FAKE_HANG"):
        time.sleep(30)
    emit({"type": "result", "session_id": session, "subtype": "success",
          "result": "turn%d" % turn, "duration_ms": 10, "is_error": False})


if not stream_input:
    if os.environ.get("FAKE_STALE_RESUME") and resume:
        die_on_stale_resume()
    reply(1, argv[-1])
    sys.exit(0)

turn = 0
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    if os.environ.get("FAKE_ERROR_HANG"):
        error_then_hang()
    if os.environ.get("FAKE_STALE_RESUME") and resume:
        die_on_stale_resume()
    turn += 1
    reply(turn, json.loads(line)["message"]["content"])
sys.exit(0)
'''


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _install_fake_cli(tmp: str) -> Path:
    script = Path(tmp) / "claude"
    script.write_text(FAKE_CLI, encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


class RunnerTests(unittest.TestCase):
    """Cold-path behaviour.

    These pin WARM_CLAUDE=0 deliberately. They describe the one-process-per-turn
    contract -- stdout EOF ends the stream, the exit code lands on the final
    error event, stdin is DEVNULL -- which is still exactly what the fallback
    path does. The warm path has its own equivalents in WarmRunnerTests below.
    """

    def setUp(self):
        # CLAUDE_MODEL and CLAUDE_STREAM_LIMIT_BYTES are env-configurable, so
        # an ambient value would otherwise silently change what the
        # default-behaviour tests below assert. Tests that exercise an
        # override nest their own patch.dict on top.
        patcher = mock.patch.dict(os.environ, {"WARM_CLAUDE": "0"}, clear=False)
        patcher.start()
        for name in ("CLAUDE_MODEL", "CLAUDE_STREAM_LIMIT_BYTES"):
            os.environ.pop(name, None)
        self.addCleanup(patcher.stop)

    def test_build_claude_command_uses_supported_noninteractive_flags(self):
        with mock.patch.dict(os.environ, {"CLAUDE_BIN": "/opt/claude/bin/claude"}, clear=False):
            cmd = build_claude_command("hello", resume="sess", system_hint="hint", extra_dirs=["/tmp/out"])
        self.assertIn("-p", cmd)
        self.assertIn("--output-format", cmd)
        self.assertIn("stream-json", cmd)
        self.assertIn("--setting-sources", cmd)
        self.assertIn("local", cmd)
        self.assertIn("--tools", cmd)
        self.assertTrue(any(item.startswith("--disallowedTools=") for item in cmd))
        self.assertIn("--permission-mode", cmd)
        self.assertIn("bypassPermissions", cmd)
        self.assertIn("--resume", cmd)
        self.assertIn("--append-system-prompt", cmd)
        self.assertIn("--add-dir", cmd)
        self.assertIn("/tmp/out", cmd)
        self.assertEqual(cmd[-2], "--")
        self.assertNotIn("--max-turns", cmd)
        self.assertNotIn("--max-budget-usd", cmd)
        self.assertEqual(cmd[-1], "hello")

    def test_the_tool_set_is_passed_as_tools_not_allowedtools(self):
        # --allowedTools is a pre-approval list, and under bypassPermissions
        # everything is already approved -- it restricted nothing, which is
        # what issue #11 measured. --tools is the flag that actually replaces
        # the session's tool set, so passing the old one is a silent no-op.
        cmd = build_claude_command("hello", resume=None, system_hint=None, extra_dirs=[])

        self.assertFalse(any(item.startswith("--allowedTools") for item in cmd))
        self.assertEqual(runner.ALLOWED_TOOLS, cmd[cmd.index("--tools") + 1])

    def test_the_tool_set_covers_what_the_job_prompt_asks_for(self):
        # orchestrator's rule block tells the model to edit sources, write
        # artifacts and meta.json, and the README's examples are shell work.
        # Dropping any of these turns a normal request into a dead end.
        tools = runner.ALLOWED_TOOLS.split(",")

        for required in ("Read", "Edit", "Write", "Glob", "Grep", "Bash"):
            self.assertIn(required, tools)

    def test_the_tool_set_omits_the_agent_infrastructure(self):
        # Team, scheduling and worktree tooling this bot never reaches for.
        # Task is out too: without TaskOutput/TaskStop/Monitor there is no
        # verified way to end a background subagent it starts.
        tools = runner.ALLOWED_TOOLS.split(",")

        for absent in (
            "Task",
            "TaskOutput",
            "TaskStop",
            "Monitor",
            "Workflow",
            "SendMessage",
            "CronCreate",
            "ScheduleWakeup",
            "RemoteTrigger",
            "EnterWorktree",
            "ToolSearch",
        ):
            self.assertNotIn(absent, tools)

    def test_web_access_is_kept(self):
        # A deliberate choice, not an oversight: "summarise this link" is a
        # normal request. It is not a containment boundary either way -- Bash
        # is in the list, so the network stays reachable regardless.
        tools = runner.ALLOWED_TOOLS.split(",")

        self.assertIn("WebFetch", tools)
        self.assertIn("WebSearch", tools)

    def test_build_claude_command_defaults_model_to_sonnet(self):
        env = dict(os.environ)
        env.pop("CLAUDE_MODEL", None)
        with mock.patch.dict(os.environ, env, clear=True):
            cmd = build_claude_command("hello")
        self.assertIn("--model", cmd)
        self.assertEqual(cmd[cmd.index("--model") + 1], "sonnet")

    def test_build_claude_command_respects_claude_model_env(self):
        with mock.patch.dict(os.environ, {"CLAUDE_MODEL": "opus"}, clear=False):
            cmd = build_claude_command("hello")
        self.assertIn("--model", cmd)
        self.assertEqual(cmd[cmd.index("--model") + 1], "opus")

    def test_build_claude_command_ignores_old_budget_env(self):
        with mock.patch.dict(os.environ, {"CLAUDE_MAX_BUDGET_USD": "1.0"}, clear=False):
            cmd = build_claude_command("hello")
        self.assertNotIn("--max-budget-usd", cmd)

    def test_build_claude_command_uses_configured_claude_bin(self):
        with mock.patch.dict(os.environ, {"CLAUDE_BIN": "/opt/claude/bin/claude"}, clear=False):
            cmd = build_claude_command("hello")

        self.assertEqual(cmd[0], "/opt/claude/bin/claude")

    def test_build_claude_command_wraps_windows_cmd_shim(self):
        with (
            mock.patch("src.runner.os.name", "nt"),
            mock.patch.dict(
                os.environ,
                {
                    "CLAUDE_BIN": r"C:\Users\me\AppData\Roaming\npm\claude.cmd",
                    "COMSPEC": r"C:\Windows\System32\cmd.exe",
                },
                clear=False,
            ),
        ):
            cmd = build_claude_command("hello & goodbye")

        self.assertEqual(cmd[:4], [r"C:\Windows\System32\cmd.exe", "/d", "/s", "/c"])
        self.assertIn("claude.cmd", cmd[4])
        self.assertIn("hello & goodbye", cmd[4])

    def test_run_claude_stream_returns_json_and_error_event_on_nonzero_exit(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                script = Path(tmp) / "claude"
                script.write_text(
                    "#!/usr/bin/env python3\n"
                    "import sys\n"
                    "print('{\"type\": \"assistant\", \"message\": {\"content\": []}}')\n"
                    "print('not-json')\n"
                    "sys.stderr.write('boom')\n"
                    "sys.exit(7)\n",
                    encoding="utf-8",
                )
                script.chmod(script.stat().st_mode | stat.S_IXUSR)
                with mock.patch.dict(os.environ, {"PATH": tmp + os.pathsep + os.environ.get("PATH", "")}, clear=False):
                    events = [event async for event in run_claude_stream("hello", workdir=tmp)]

            self.assertEqual(events[0]["type"], "assistant")
            self.assertEqual(events[1], {"type": "raw", "text": "not-json"})
            self.assertEqual(events[-1]["type"], "error")
            self.assertIn("boom", events[-1]["text"])
            self.assertEqual(events[-1]["returncode"], 7)

        asyncio.run(scenario())

    def test_run_claude_stream_passes_default_stream_limit_to_subprocess(self):
        async def scenario():
            captured = {}

            async def fake_create_subprocess_exec(*args, **kwargs):
                captured.update(kwargs)
                raise FileNotFoundError()

            env = dict(os.environ)
            env.pop("CLAUDE_STREAM_LIMIT_BYTES", None)
            with mock.patch.dict(os.environ, env, clear=True), mock.patch(
                "src.runner.asyncio.create_subprocess_exec",
                fake_create_subprocess_exec,
            ):
                events = [event async for event in run_claude_stream("hello")]
            return captured, events

        captured, events = asyncio.run(scenario())
        self.assertEqual(captured.get("limit"), 8388608)
        self.assertEqual(events[-1]["type"], "error")

    def test_run_claude_stream_respects_stream_limit_env_override(self):
        async def scenario():
            captured = {}

            async def fake_create_subprocess_exec(*args, **kwargs):
                captured.update(kwargs)
                raise FileNotFoundError()

            with mock.patch.dict(
                os.environ, {"CLAUDE_STREAM_LIMIT_BYTES": "1048576"}, clear=False
            ), mock.patch(
                "src.runner.asyncio.create_subprocess_exec",
                fake_create_subprocess_exec,
            ):
                events = [event async for event in run_claude_stream("hello")]
            return captured, events

        captured, events = asyncio.run(scenario())
        self.assertEqual(captured.get("limit"), 1048576)
        self.assertEqual(events[-1]["type"], "error")

    def test_run_claude_stream_handles_lines_larger_than_default_asyncio_limit(self):
        # Default asyncio StreamReader line limit is 64KiB. A single large
        # JSON line used to blow up with
        # "ValueError: Separator is not found, and chunk exceed the limit"
        # unless create_subprocess_exec is given a bigger `limit=`.
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                script = Path(tmp) / "claude"
                script.write_text(
                    "#!/usr/bin/env python3\n"
                    "import json\n"
                    "payload = 'x' * 200000\n"
                    "print(json.dumps({'type': 'assistant', 'message': "
                    "{'content': [{'type': 'text', 'text': payload}]}}))\n"
                    "print(json.dumps({'type': 'result'}))\n",
                    encoding="utf-8",
                )
                script.chmod(script.stat().st_mode | stat.S_IXUSR)
                with mock.patch.dict(
                    os.environ,
                    {"PATH": tmp + os.pathsep + os.environ.get("PATH", "")},
                    clear=False,
                ):
                    events = [event async for event in run_claude_stream("hello", workdir=tmp)]
            return events

        events = asyncio.run(scenario())
        self.assertEqual(events[0]["type"], "assistant")
        self.assertEqual(len(events[0]["message"]["content"][0]["text"]), 200000)
        self.assertEqual(events[1]["type"], "result")

    def test_run_claude_stream_reaps_process_when_line_exceeds_configured_limit(self):
        # Regression test: with a tiny CLAUDE_STREAM_LIMIT_BYTES, a stdout
        # line that exceeds it makes StreamReader raise ValueError while the
        # subprocess is still running (and, here, still sleeping). Assert we
        # (a) surface an {"type": "error", ...} event instead of letting the
        # exception escape, and (b) actually terminate the process instead of
        # leaking it as an untracked orphan.
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                pid_file = Path(tmp) / "pid.txt"
                script = Path(tmp) / "claude"
                script.write_text(
                    "#!/usr/bin/env python3\n"
                    "import json, os, time\n"
                    f"open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
                    "payload = 'x' * 2000\n"
                    "print(json.dumps({'type': 'assistant', 'message': "
                    "{'content': [{'type': 'text', 'text': payload}]}}), flush=True)\n"
                    "time.sleep(30)\n",
                    encoding="utf-8",
                )
                script.chmod(script.stat().st_mode | stat.S_IXUSR)
                with mock.patch.dict(
                    os.environ,
                    {
                        "PATH": tmp + os.pathsep + os.environ.get("PATH", ""),
                        "CLAUDE_STREAM_LIMIT_BYTES": "100",
                    },
                    clear=False,
                ):
                    events = [
                        event async for event in run_claude_stream("hello", workdir=tmp)
                    ]
                pid = int(pid_file.read_text().strip()) if pid_file.exists() else None
            return events, pid

        events, pid = asyncio.run(scenario())

        self.assertIsNotNone(pid)
        self.assertEqual(events[-1]["type"], "error")
        self.assertIn("리밋", events[-1]["text"])
        self.assertEqual(get_active_claude_process_count(), 0)
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_active_claude_processes_can_be_terminated(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                script = Path(tmp) / "claude"
                script.write_text(
                    "#!/usr/bin/env python3\n"
                    "import json, sys, time\n"
                    "print(json.dumps({'type': 'assistant'}), flush=True)\n"
                    "time.sleep(30)\n",
                    encoding="utf-8",
                )
                script.chmod(script.stat().st_mode | stat.S_IXUSR)
                events = []

                async def collect_events():
                    with mock.patch.dict(
                        os.environ,
                        {"PATH": tmp + os.pathsep + os.environ.get("PATH", "")},
                        clear=False,
                    ):
                        async for event in run_claude_stream("hello", workdir=tmp):
                            events.append(event)

                task = asyncio.create_task(collect_events())
                for _ in range(100):
                    if get_active_claude_process_count() and events:
                        break
                    await asyncio.sleep(0.01)

                summary = await terminate_active_claude_processes(timeout=1)
                await asyncio.wait_for(task, timeout=3)
                return summary, events, get_active_claude_process_count()

        summary, events, active_count = asyncio.run(scenario())

        self.assertEqual(summary.requested, 1)
        self.assertEqual(summary.terminated, 1)
        self.assertEqual(active_count, 0)
        self.assertEqual(events[0]["type"], "assistant")
        self.assertEqual(events[-1]["type"], "error")

    def test_a_process_that_outlives_sigkill_stays_killable_by_a_later_shutdown(self):
        # Pre-existing bug (b9c0f09): every requested process was untracked
        # unconditionally, so one that shrugged off SIGTERM *and* SIGKILL was
        # reported as still_running and then dropped from
        # _ACTIVE_CLAUDE_PROCESSES -- invisible to every later 종료, i.e.
        # unkillable for the rest of the bot's life. A process that survives
        # SIGKILL cannot be produced for real, so the signals are stubbed out;
        # everything else is the real termination path.
        class _UnkillableProcess:
            pid = -1
            returncode = None

            async def wait(self):
                await asyncio.Event().wait()

        async def scenario():
            stubborn = _UnkillableProcess()
            runner._ACTIVE_CLAUDE_PROCESSES.add(stubborn)
            try:
                with (
                    mock.patch.object(runner, "_terminate_process"),
                    mock.patch.object(runner, "_kill_process"),
                ):
                    first = await terminate_active_claude_processes(timeout=0.01)
                    tracked = get_active_claude_process_count()
                    # 종료 pressed again must still find it.
                    second = await terminate_active_claude_processes(timeout=0.01)
                return first, tracked, second
            finally:
                runner._ACTIVE_CLAUDE_PROCESSES.discard(stubborn)

        first, tracked, second = asyncio.run(scenario())

        self.assertEqual(first.requested, 1)
        self.assertEqual(first.terminated, 0)
        self.assertEqual(first.killed, 1)
        self.assertEqual(first.still_running, 1)
        self.assertEqual(tracked, 1)
        self.assertEqual(second.requested, 1)

    def test_a_cold_process_is_reaped_through_its_job_scope(self):
        # H1: the scope has to work on the fallback path too, not only on the
        # warm one -- a job that fell back to a cold spawn still has to be
        # reapable without touching anybody else's process.
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                _install_fake_cli(tmp)
                scope = JobProcessScope()
                events = []

                async def collect():
                    with mock.patch.dict(
                        os.environ,
                        {
                            "PATH": tmp + os.pathsep + os.environ.get("PATH", ""),
                            "FAKE_HANG": "1",
                        },
                        clear=False,
                    ):
                        async for event in run_claude_stream(
                            "느린 질문", workdir=tmp, scope=scope
                        ):
                            events.append(event)

                task = asyncio.create_task(collect())
                for _ in range(300):
                    if scope.live() and events:
                        break
                    await asyncio.sleep(0.01)

                summary = await terminate_job_processes(scope, timeout=2)
                await asyncio.wait_for(task, timeout=5)
                return summary, events, len(scope), get_active_claude_process_count()

        summary, events, scope_size, active = asyncio.run(scenario())

        self.assertEqual(summary.requested, 1)
        self.assertEqual(summary.terminated, 1)
        self.assertEqual(events[-1]["type"], "error")
        self.assertEqual(scope_size, 0)
        self.assertEqual(active, 0)

    def test_cold_spawn_keeps_stdin_on_devnull(self):
        # Regression guard (issue #1): with stdin left open the CLI waits an
        # extra 3s for "no stdin data received in 3s".
        async def scenario():
            captured = {}

            async def fake_create_subprocess_exec(*args, **kwargs):
                captured.update(kwargs)
                captured["argv"] = args
                raise FileNotFoundError()

            with mock.patch(
                "src.runner.asyncio.create_subprocess_exec", fake_create_subprocess_exec
            ):
                [event async for event in run_claude_stream("hello")]
            return captured

        captured = asyncio.run(scenario())
        self.assertEqual(captured.get("stdin"), asyncio.subprocess.DEVNULL)
        self.assertNotIn("--input-format", captured["argv"])

    def test_build_claude_command_streaming_input_drops_the_prompt_argument(self):
        cmd = build_claude_command("hello", stream_input=True)
        self.assertIn("--input-format", cmd)
        self.assertEqual(cmd[cmd.index("--input-format") + 1], "stream-json")
        self.assertNotIn("--", cmd)
        self.assertNotIn("hello", cmd)

    def test_build_claude_command_default_is_unchanged_by_streaming_support(self):
        self.assertNotIn("--input-format", build_claude_command("hello"))


class WarmRunnerTests(unittest.TestCase):
    """Warm path: one claude process serving several turns (issue #2)."""

    def setUp(self):
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        patcher.start()
        for name in (
            "CLAUDE_MODEL",
            "CLAUDE_STREAM_LIMIT_BYTES",
            "WARM_CLAUDE",
            "WARM_CLAUDE_IDLE_TTL_SECONDS",
            "WARM_CLAUDE_MAX_PROCESSES",
            "FAKE_STREAM_BROKEN",
            "FAKE_HANG",
            "FAKE_HUGE",
            "FAKE_SESSION",
        ):
            os.environ.pop(name, None)
        self.addCleanup(patcher.stop)
        self.addCleanup(self._assert_pool_drained)

    def _assert_pool_drained(self):
        # Every warm test must leave the process table clean; a leak here would
        # otherwise surface as a mysterious failure in a later test.
        self.assertEqual(get_active_claude_process_count(), 0)
        self.assertEqual(len(get_warm_pool()), 0)

    def _run(self, scenario):
        """Run a warm scenario and always drain the pool inside the same event
        loop -- a parked process holds transports bound to that loop."""

        spawned = []
        real_spawn = runner._spawn_claude_process

        async def tracking_spawn(*args, **kwargs):
            proc = await real_spawn(*args, **kwargs)
            spawned.append(proc)
            return proc

        async def wrapped():
            try:
                with mock.patch.object(runner, "_spawn_claude_process", tracking_spawn):
                    return await scenario()
            finally:
                await terminate_active_claude_processes(timeout=2)
                # Close every transport inside the loop that owns it. Retiring a
                # warm process cancels its pending stderr read, so the pipe never
                # reaches EOF and the transport stays open until __del__ -- which,
                # once asyncio.run() has closed the loop, raises "Event loop is
                # closed" as an intermittent unraisable-exception warning. The
                # bot's loop never closes, so this is a harness-only concern.
                for proc in spawned:
                    transport = getattr(proc, "_transport", None)
                    if transport is not None:
                        transport.close()
                gc.collect()
                await asyncio.sleep(0)

        return asyncio.run(wrapped())

    def _env(self, tmp, **extra):
        env = {
            "PATH": tmp + os.pathsep + os.environ.get("PATH", ""),
            "FAKE_SPAWN_LOG": str(Path(tmp) / "spawns.jsonl"),
        }
        env.update(extra)
        return env

    @staticmethod
    def _spawns(tmp):
        path = Path(tmp) / "spawns.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line]

    @staticmethod
    async def _collect(*args, **kwargs):
        return [event async for event in run_claude_stream(*args, **kwargs)]

    def test_second_turn_reuses_the_process_instead_of_spawning(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                _install_fake_cli(tmp)
                with mock.patch.dict(os.environ, self._env(tmp), clear=False):
                    first = await self._collect("첫 질문", workdir=tmp)
                    session = first[-1]["session_id"]
                    second = await self._collect("두 번째 질문", workdir=tmp, resume=session)
                return first, second, session, self._spawns(tmp)

        first, second, session, spawns = self._run(scenario)

        self.assertEqual(first[-1]["result"], "turn1")
        # Same process: the CLI's own turn counter advanced rather than resetting.
        self.assertEqual(second[-1]["result"], "turn2")
        self.assertEqual(second[-1]["session_id"], session)
        self.assertEqual(len(spawns), 1, f"expected one spawn, got {spawns}")
        self.assertTrue(spawns[0]["stream_input"])

    def test_a_new_conversation_never_inherits_a_parked_process(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                _install_fake_cli(tmp)
                with mock.patch.dict(os.environ, self._env(tmp), clear=False):
                    await self._collect("첫 질문", workdir=tmp)
                    # resume=None -> must be a fresh conversation.
                    second = await self._collect("무관한 질문", workdir=tmp)
                return second, self._spawns(tmp)

        second, spawns = self._run(scenario)

        self.assertEqual(second[-1]["result"], "turn1")
        self.assertEqual(len(spawns), 2)

    def test_a_different_workdir_never_reuses_a_process(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as other:
                _install_fake_cli(tmp)
                with mock.patch.dict(os.environ, self._env(tmp), clear=False):
                    first = await self._collect("첫 질문", workdir=tmp)
                    session = first[-1]["session_id"]
                    second = await self._collect("다른 곳", workdir=other, resume=session)
                return second, self._spawns(tmp)

        second, spawns = self._run(scenario)

        self.assertEqual(second[-1]["result"], "turn1")
        self.assertEqual(len(spawns), 2)

    def test_changing_extra_dirs_still_reuses_under_bypass_permissions(self):
        # orchestrator passes a fresh per-job --add-dir on every turn. Verified
        # empirically that --add-dir grants nothing under bypassPermissions, so
        # it must not defeat reuse. See runner.WARM_KEY_IGNORES_EXTRA_DIRS.
        self.assertTrue(runner.WARM_KEY_IGNORES_EXTRA_DIRS)

        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                _install_fake_cli(tmp)
                with mock.patch.dict(os.environ, self._env(tmp), clear=False):
                    first = await self._collect("첫 질문", workdir=tmp, extra_dirs=[tmp + "/job-1"])
                    session = first[-1]["session_id"]
                    second = await self._collect(
                        "두 번째", workdir=tmp, resume=session, extra_dirs=[tmp + "/job-2"]
                    )
                return second, self._spawns(tmp)

        second, spawns = self._run(scenario)

        self.assertEqual(second[-1]["result"], "turn2")
        self.assertEqual(len(spawns), 1)

    def test_warm_disabled_falls_back_to_the_cold_path_entirely(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                _install_fake_cli(tmp)
                with mock.patch.dict(
                    os.environ, self._env(tmp, WARM_CLAUDE="0"), clear=False
                ):
                    first = await self._collect("첫 질문", workdir=tmp)
                    session = first[-1]["session_id"]
                    await self._collect("두 번째", workdir=tmp, resume=session)
                return self._spawns(tmp)

        spawns = self._run(scenario)

        self.assertEqual(len(spawns), 2)
        self.assertFalse(any(spawn["stream_input"] for spawn in spawns))

    def test_a_cli_that_cannot_stream_input_falls_back_silently(self):
        # The safety requirement: if streaming-input mode is broken for any
        # reason, the user must still get their answer from a cold spawn.
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                _install_fake_cli(tmp)
                with mock.patch.dict(
                    os.environ, self._env(tmp, FAKE_STREAM_BROKEN="1"), clear=False
                ):
                    events = await self._collect("첫 질문", workdir=tmp)
                return events, self._spawns(tmp)

        events, spawns = self._run(scenario)

        self.assertEqual(events[-1]["type"], "result")
        self.assertEqual(events[-1]["result"], "turn1")
        # No error event ever reached the caller.
        self.assertFalse([event for event in events if event.get("type") == "error"])
        self.assertEqual([spawn["stream_input"] for spawn in spawns], [True, False])

    def test_spawn_failure_in_warm_mode_still_reports_the_missing_cli(self):
        async def scenario():
            with mock.patch(
                "src.runner.asyncio.create_subprocess_exec",
                mock.AsyncMock(side_effect=FileNotFoundError()),
            ):
                return await self._collect("hello")

        events = self._run(scenario)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "error")
        self.assertIn("찾을 수 없습니다", events[0]["text"])

    def test_shutdown_drains_parked_warm_processes(self):
        # Issue #2's completion criterion: 종료 must clear the warm pool.
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                _install_fake_cli(tmp)
                pid_file = Path(tmp) / "pid.txt"
                with mock.patch.dict(
                    os.environ, self._env(tmp, FAKE_PID_FILE=str(pid_file)), clear=False
                ):
                    await self._collect("첫 질문", workdir=tmp)

                parked_count = get_active_claude_process_count()
                pool_size = len(get_warm_pool())

                summary = await terminate_active_claude_processes(timeout=2)
                pid = int(pid_file.read_text().strip())
                return parked_count, pool_size, summary, pid

        parked_count, pool_size, summary, pid = self._run(scenario)

        # The parked process is still a live claude process, and 종료 owns it.
        self.assertEqual(parked_count, 1)
        self.assertEqual(pool_size, 1)
        self.assertEqual(summary.requested, 1)
        self.assertEqual(summary.terminated, 1)
        self.assertEqual(get_active_claude_process_count(), 0)
        self.assertEqual(len(get_warm_pool()), 0)
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_warm_processes_join_the_shared_active_process_set(self):
        # Cross-worker contract: warm processes are tracked in the very same
        # _ACTIVE_CLAUDE_PROCESSES set, as raw asyncio.subprocess.Process
        # objects (not wrappers), so get_active_claude_process_count() sums
        # warm and cold and 종료 reaches both. tests/test_main.py's issue #6
        # timeout test registers a process into this set directly and asserts
        # on that count, so neither the name nor the element type may change.
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                _install_fake_cli(tmp)
                external = await asyncio.create_subprocess_exec(
                    "/bin/sh", "-c", "sleep 60",
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                    start_new_session=True,
                )
                runner._ACTIVE_CLAUDE_PROCESSES.add(external)
                try:
                    with mock.patch.dict(os.environ, self._env(tmp), clear=False):
                        await self._collect("첫 질문", workdir=tmp)
                    tracked = set(runner._ACTIVE_CLAUDE_PROCESSES)
                    # 1 externally registered + 1 parked warm process.
                    return tracked, get_active_claude_process_count()
                finally:
                    runner._ACTIVE_CLAUDE_PROCESSES.discard(external)
                    if external.returncode is None:
                        external.kill()
                        await external.wait()
                    # Drop the transport inside the loop that owns it; letting
                    # it reach __del__ after asyncio.run() closes the loop
                    # raises "Event loop is closed" as an unraisable warning.
                    transport = getattr(external, "_transport", None)
                    if transport is not None:
                        transport.close()

        tracked, count = self._run(scenario)

        self.assertIsInstance(runner._ACTIVE_CLAUDE_PROCESSES, set)
        for proc in tracked:
            self.assertIsInstance(proc, asyncio.subprocess.Process)
        self.assertEqual(count, 2)

    def test_a_terminated_warm_process_is_never_handed_to_a_later_turn(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                _install_fake_cli(tmp)
                with mock.patch.dict(os.environ, self._env(tmp), clear=False):
                    first = await self._collect("첫 질문", workdir=tmp)
                    session = first[-1]["session_id"]
                    await terminate_active_claude_processes(timeout=2)
                    second = await self._collect("두 번째", workdir=tmp, resume=session)
                return second, self._spawns(tmp)

        second, spawns = self._run(scenario)

        self.assertEqual(second[-1]["result"], "turn1")
        self.assertEqual(len(spawns), 2)

    def test_terminating_mid_turn_surfaces_an_error_and_reaps_the_process(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                _install_fake_cli(tmp)
                events = []

                async def collect():
                    with mock.patch.dict(
                        os.environ, self._env(tmp, FAKE_HANG="1"), clear=False
                    ):
                        async for event in run_claude_stream("느린 질문", workdir=tmp):
                            events.append(event)

                task = asyncio.create_task(collect())
                for _ in range(200):
                    if get_active_claude_process_count() and events:
                        break
                    await asyncio.sleep(0.01)

                summary = await terminate_active_claude_processes(timeout=2)
                await asyncio.wait_for(task, timeout=5)
                return summary, events

        summary, events = self._run(scenario)

        self.assertEqual(summary.requested, 1)
        self.assertEqual(summary.terminated, 1)
        self.assertEqual(events[-1]["type"], "error")
        self.assertEqual(get_active_claude_process_count(), 0)

    def test_oversized_line_mid_turn_errors_and_reaps_the_warm_process(self):
        # The cold path's LimitOverrunError/ValueError defence must hold on the
        # warm path too, including killing the process it stopped reading from.
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                _install_fake_cli(tmp)
                pid_file = Path(tmp) / "pid.txt"
                with mock.patch.dict(
                    os.environ,
                    self._env(
                        tmp,
                        FAKE_HUGE="1",
                        FAKE_HANG="1",
                        FAKE_PID_FILE=str(pid_file),
                        CLAUDE_STREAM_LIMIT_BYTES="100",
                    ),
                    clear=False,
                ):
                    events = await self._collect("긴 답변", workdir=tmp)
                return events, int(pid_file.read_text().strip())

        events, pid = self._run(scenario)

        self.assertEqual(events[-1]["type"], "error")
        self.assertIn("리밋", events[-1]["text"])
        self.assertEqual(get_active_claude_process_count(), 0)
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_idle_warm_process_is_retired_after_the_ttl(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                _install_fake_cli(tmp)
                with mock.patch.dict(
                    os.environ,
                    self._env(tmp, WARM_CLAUDE_IDLE_TTL_SECONDS="0.05"),
                    clear=False,
                ):
                    await self._collect("첫 질문", workdir=tmp)
                    parked = get_active_claude_process_count()
                    # Poll the process count, not the pool: expiry pops the
                    # pool entry *before* awaiting termination, so an empty
                    # pool does not yet mean the process is reaped. (A fixed
                    # sleep here also turns machine load into a flaky failure.)
                    for _ in range(500):
                        if not get_active_claude_process_count():
                            break
                        await asyncio.sleep(0.01)
                return parked, get_active_claude_process_count(), len(get_warm_pool())

        parked, after_ttl, pool_size = self._run(scenario)

        self.assertEqual(parked, 1)
        self.assertEqual(after_ttl, 0)
        self.assertEqual(pool_size, 0)

    def test_pool_capacity_caps_the_number_of_live_warm_processes(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                _install_fake_cli(tmp)
                with mock.patch.dict(
                    os.environ,
                    self._env(tmp, WARM_CLAUDE_MAX_PROCESSES="1"),
                    clear=False,
                ):
                    first = await self._collect("A", workdir=tmp, extra_dirs=None)
                    with mock.patch.dict(os.environ, {"FAKE_SESSION": "sess-2"}, clear=False):
                        await self._collect("B", workdir=tmp)
                    return first[-1]["session_id"], get_active_claude_process_count(), len(
                        get_warm_pool()
                    )

        _, live, pool_size = self._run(scenario)

        self.assertEqual(pool_size, 1)
        self.assertEqual(live, 1)

    def test_stale_resume_still_yields_the_marker_main_py_recovers_on(self):
        # A warm process outlives its turn, so returning at the result event
        # would swallow the trailing stderr-derived error event that
        # main.py._is_missing_conversation_error keys off. Verified against the
        # real CLI: a stale --resume emits is_error in-band, then exits 1 with
        # "No conversation found with session ID" on stderr.
        marker = "No conversation found with session ID"

        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                _install_fake_cli(tmp)
                with mock.patch.dict(
                    os.environ, self._env(tmp, FAKE_STALE_RESUME="1"), clear=False
                ):
                    warm = await self._collect("안녕", workdir=tmp, resume="sess-gone")
                    cold_env = self._env(tmp, FAKE_STALE_RESUME="1", WARM_CLAUDE="0")
                    with mock.patch.dict(os.environ, cold_env, clear=False):
                        cold = await self._collect("안녕", workdir=tmp, resume="sess-gone")
                return warm, cold

        warm, cold = self._run(scenario)

        for label, events in (("warm", warm), ("cold", cold)):
            with self.subTest(path=label):
                last = events[-1]
                self.assertEqual(last["type"], "error", events)
                self.assertIn(marker, last["text"])
                self.assertEqual(last["returncode"], 1)
        # The broken process must not have been parked for the next turn.
        self.assertEqual(len(get_warm_pool()), 0)

    def test_terminating_one_job_leaves_another_jobs_process_untouched(self):
        # H1: 종료 killing every claude process is intended; a job timeout
        # doing it is not. Two real processes, one scope each -- reaping job A
        # must leave job B's process alive and still usable, which is exactly
        # what swapping the terminate call for an AsyncMock would not prove.
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                _install_fake_cli(tmp)
                pid_a = Path(tmp) / "pid-a.txt"
                pid_b = Path(tmp) / "pid-b.txt"
                scope_a = JobProcessScope()
                scope_b = JobProcessScope()
                events_a = []

                async def hanging_job():
                    async for event in run_claude_stream(
                        "느린 질문", workdir=tmp, scope=scope_a
                    ):
                        events_a.append(event)

                with mock.patch.dict(
                    os.environ,
                    self._env(tmp, FAKE_HANG="1", FAKE_PID_FILE=str(pid_a)),
                    clear=False,
                ):
                    task = asyncio.create_task(hanging_job())
                    for _ in range(300):
                        if scope_a.live() and events_a:
                            break
                        await asyncio.sleep(0.01)

                with mock.patch.dict(
                    os.environ,
                    self._env(tmp, FAKE_SESSION="sess-b", FAKE_PID_FILE=str(pid_b)),
                    clear=False,
                ):
                    b_first = await self._collect("빠른 질문", workdir=tmp, scope=scope_b)
                    session_b = b_first[-1]["session_id"]

                    summary = await terminate_job_processes(scope_a, timeout=2)
                    await asyncio.wait_for(task, timeout=5)
                    b_alive = _pid_alive(int(pid_b.read_text().strip()))
                    a_alive = _pid_alive(int(pid_a.read_text().strip()))

                    # B's process is not just alive, it still holds B's
                    # conversation: the CLI's own turn counter advances.
                    b_second = await self._collect(
                        "이어서", workdir=tmp, resume=session_b, scope=scope_b
                    )
                return summary, events_a, a_alive, b_alive, b_second, self._spawns(tmp)

        summary, events_a, a_alive, b_alive, b_second, spawns = self._run(scenario)

        self.assertEqual(summary.requested, 1)
        self.assertEqual(summary.terminated, 1)
        self.assertEqual(events_a[-1]["type"], "error")
        self.assertFalse(a_alive, "the timed-out job's process must be reaped")
        self.assertTrue(b_alive, "another job's process must survive the timeout")
        self.assertEqual(b_second[-1]["result"], "turn2")
        self.assertEqual(len(spawns), 2, f"expected two spawns, got {spawns}")

    def test_a_timed_out_jobs_parked_process_is_discarded_not_reused(self):
        # H1: whatever the job left parked goes with it. A warm process this
        # job started must not survive into the next turn once we have decided
        # to kill the job -- the next turn would be handed a corpse.
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                _install_fake_cli(tmp)
                parked_pid = Path(tmp) / "parked.txt"
                hung_pid = Path(tmp) / "hung.txt"
                scope = JobProcessScope()
                events = []

                with mock.patch.dict(
                    os.environ,
                    self._env(tmp, FAKE_PID_FILE=str(parked_pid)),
                    clear=False,
                ):
                    await self._collect("첫 질문", workdir=tmp, scope=scope)
                parked_before = len(get_warm_pool())

                # Same job, second attempt (what a stale-session retry does),
                # and this one is the one that hangs until the job times out.
                async def hanging_attempt():
                    with mock.patch.dict(
                        os.environ,
                        self._env(
                            tmp,
                            FAKE_HANG="1",
                            FAKE_SESSION="sess-2",
                            FAKE_PID_FILE=str(hung_pid),
                        ),
                        clear=False,
                    ):
                        async for event in run_claude_stream(
                            "재시도", workdir=tmp, scope=scope
                        ):
                            events.append(event)

                task = asyncio.create_task(hanging_attempt())
                for _ in range(300):
                    if len(scope.live()) == 2 and events:
                        break
                    await asyncio.sleep(0.01)

                summary = await terminate_job_processes(scope, timeout=2)
                await asyncio.wait_for(task, timeout=5)
                return (
                    parked_before,
                    summary,
                    len(get_warm_pool()),
                    get_active_claude_process_count(),
                    _pid_alive(int(parked_pid.read_text().strip())),
                    _pid_alive(int(hung_pid.read_text().strip())),
                )

        parked_before, summary, pool_size, active, parked_alive, hung_alive = self._run(
            scenario
        )

        self.assertEqual(parked_before, 1)
        self.assertEqual(summary.requested, 2)
        self.assertEqual(summary.terminated, 2)
        self.assertEqual(pool_size, 0, "the job's warm process must leave the pool")
        self.assertEqual(active, 0)
        self.assertFalse(parked_alive)
        self.assertFalse(hung_alive)

    def test_a_workdir_round_trip_never_leaves_two_processes_on_one_session(self):
        # M4 end to end: turn 1 in A parks P1. Turn 2 in B cannot reuse it (the
        # key carries the workdir), so it spawns P2 and resumes the same
        # conversation -- P1 now holds a stale prefix of it. Parking P2 must
        # dispose of P1, or turn 3, keyed back to A, is handed P1 and the user
        # watches the bot forget turn 2.
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as other:
                _install_fake_cli(tmp)
                first_pid = Path(tmp) / "first.txt"
                with mock.patch.dict(
                    os.environ, self._env(tmp, FAKE_PID_FILE=str(first_pid)), clear=False
                ):
                    first = await self._collect("첫 질문", workdir=tmp)
                    session = first[-1]["session_id"]
                    pid = int(first_pid.read_text().strip())

                with mock.patch.dict(os.environ, self._env(tmp), clear=False):
                    await self._collect("다른 곳", workdir=other, resume=session)
                    after_b = (
                        len(get_warm_pool()),
                        get_active_claude_process_count(),
                        _pid_alive(pid),
                    )
                    third = await self._collect("다시 원래 곳", workdir=tmp, resume=session)
                return after_b, third, self._spawns(tmp)

        (pool_size, live, first_alive), third, spawns = self._run(scenario)

        self.assertEqual(pool_size, 1, "one conversation must own one process")
        self.assertEqual(live, 1)
        self.assertFalse(first_alive, "turn 1's process must be disposed of, not parked")
        # Turn 3 could not have been served by a leftover turn-1 process.
        self.assertEqual(len(spawns), 3, f"expected three spawns, got {spawns}")
        self.assertEqual(third[-1]["result"], "turn1")

    def test_a_warm_error_that_outlives_the_grace_window_still_reports_why(self):
        # M1: the grace cap stays -- waiting forever would pin a warm process
        # on a CLI that is merely slow to exit. But exceeding it must not
        # swallow the reason: main.py's stale-session recovery only ever sees
        # this trailing error event, and here the marker exists nowhere but the
        # in-band result the CLI already sent.
        marker = "No conversation found with session ID"

        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                _install_fake_cli(tmp)
                pid_file = Path(tmp) / "pid.txt"
                with mock.patch.dict(
                    os.environ,
                    self._env(tmp, FAKE_ERROR_HANG="1", FAKE_PID_FILE=str(pid_file)),
                    clear=False,
                ), mock.patch.object(runner, "WARM_ERROR_EXIT_GRACE_SECONDS", 0.05):
                    events = await self._collect("안녕", workdir=tmp, resume="sess-gone")
                return (
                    events,
                    _pid_alive(int(pid_file.read_text().strip())),
                    len(get_warm_pool()),
                    get_active_claude_process_count(),
                )

        events, still_alive, pool_size, live = self._run(scenario)

        self.assertEqual(events[-1]["type"], "error")
        self.assertIn(marker, events[-1]["text"])
        self.assertIn("폐기", events[-1]["text"])
        self.assertIsNone(events[-1]["returncode"])
        # The process that would not die is gone, and was never parked.
        self.assertFalse(still_alive)
        self.assertEqual(pool_size, 0)
        self.assertEqual(live, 0)

    def test_an_errored_turn_is_never_parked_for_reuse(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                _install_fake_cli(tmp)
                with mock.patch.dict(
                    os.environ, self._env(tmp, FAKE_STALE_RESUME="1"), clear=False
                ):
                    await self._collect("안녕", workdir=tmp, resume="sess-gone")
                return len(get_warm_pool()), get_active_claude_process_count()

        pool_size, live = self._run(scenario)

        self.assertEqual(pool_size, 0)
        self.assertEqual(live, 0)

    # -- issue #7 instrumentation ---------------------------------------

    def test_timings_record_spawn_and_first_event_and_mark_cold_then_warm(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                _install_fake_cli(tmp)
                cold = JobTimings("job-cold")
                warm = JobTimings("job-warm")
                with mock.patch.dict(os.environ, self._env(tmp), clear=False):
                    first = await self._collect("첫 질문", workdir=tmp, timings=cold)
                    session = first[-1]["session_id"]
                    await self._collect(
                        "두 번째", workdir=tmp, resume=session, timings=warm
                    )
                return cold.snapshot(), warm.snapshot()

        cold, warm = self._run(scenario)

        for snapshot in (cold, warm):
            self.assertIn(SPAN_SPAWN, snapshot["spans_ms"])
            self.assertIn(SPAN_FIRST_EVENT, snapshot["spans_ms"])
        self.assertIs(cold["meta"]["warm"], False)
        self.assertIs(warm["meta"]["warm"], True)

    def test_cold_path_timings_are_recorded_when_warm_is_disabled(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                _install_fake_cli(tmp)
                timings = JobTimings("job-1")
                with mock.patch.dict(
                    os.environ, self._env(tmp, WARM_CLAUDE="0"), clear=False
                ):
                    await self._collect("첫 질문", workdir=tmp, timings=timings)
                return timings.snapshot()

        snapshot = self._run(scenario)

        self.assertIn(SPAN_SPAWN, snapshot["spans_ms"])
        self.assertIn(SPAN_FIRST_EVENT, snapshot["spans_ms"])
        self.assertIs(snapshot["meta"]["warm"], False)

    def test_timings_is_optional_so_existing_callers_keep_working(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                _install_fake_cli(tmp)
                with mock.patch.dict(os.environ, self._env(tmp), clear=False):
                    # Positional call in the pre-#7 argument order.
                    return [
                        event
                        async for event in run_claude_stream("hi", tmp, None, None, None)
                    ]

        events = self._run(scenario)

        self.assertEqual(events[-1]["type"], "result")
