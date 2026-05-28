import asyncio
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.runner import (
    build_claude_command,
    get_active_claude_process_count,
    run_claude_stream,
    terminate_active_claude_processes,
)


class RunnerTests(unittest.TestCase):
    def test_build_claude_command_uses_supported_noninteractive_flags(self):
        with mock.patch.dict(os.environ, {"CLAUDE_BIN": "/opt/claude/bin/claude"}, clear=False):
            cmd = build_claude_command("hello", resume="sess", system_hint="hint", extra_dirs=["/tmp/out"])
        self.assertIn("-p", cmd)
        self.assertIn("--output-format", cmd)
        self.assertIn("stream-json", cmd)
        self.assertIn("--setting-sources", cmd)
        self.assertIn("local", cmd)
        self.assertTrue(any(item.startswith("--allowedTools=") for item in cmd))
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
