import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import src.sessions as sessions


class SessionsTests(unittest.TestCase):
    def test_session_state_persists_session_workdir_and_timestamp(self):
        now = datetime(2026, 5, 25, 7, 0, tzinfo=timezone.utc)

        with tempfile.TemporaryDirectory() as tmp:
            store_path = Path(tmp) / "sessions.json"

            with mock.patch.object(sessions, "_STORE_PATH", store_path):
                sessions.set_session(123, "sess-1", workdir="/tmp/project", now=now)
                state = sessions.get_session_state(
                    123,
                    now=now + timedelta(minutes=59),
                )

            self.assertIsNotNone(state)
            self.assertEqual(state.session_id, "sess-1")
            self.assertEqual(state.workdir, "/tmp/project")
            self.assertEqual(state.updated_at, now)

    def test_session_state_expires_after_one_hour(self):
        now = datetime(2026, 5, 25, 7, 0, tzinfo=timezone.utc)

        with tempfile.TemporaryDirectory() as tmp:
            store_path = Path(tmp) / "sessions.json"

            with mock.patch.object(sessions, "_STORE_PATH", store_path):
                sessions.set_session(123, "sess-1", workdir="/tmp/project", now=now)
                state = sessions.get_session_state(
                    123,
                    now=now + timedelta(hours=1, seconds=1),
                )
                store = json.loads(store_path.read_text(encoding="utf-8"))

            self.assertIsNone(state)
            self.assertNotIn("123", store)

    def test_legacy_string_store_is_still_readable(self):
        with tempfile.TemporaryDirectory() as tmp:
            store_path = Path(tmp) / "sessions.json"
            store_path.write_text('{"123": "legacy-sess"}\n', encoding="utf-8")

            with mock.patch.object(sessions, "_STORE_PATH", store_path):
                state = sessions.get_session_state(123)

            self.assertIsNotNone(state)
            self.assertEqual(state.session_id, "legacy-sess")
            self.assertIsNone(state.workdir)

    def test_clear_all_sessions_removes_every_channel_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            store_path = Path(tmp) / "sessions.json"

            with mock.patch.object(sessions, "_STORE_PATH", store_path):
                sessions.set_session(123, "sess-1", workdir="/tmp/a")
                sessions.set_session(456, "sess-2", workdir="/tmp/b")
                cleared = sessions.clear_all_sessions()
                store = json.loads(store_path.read_text(encoding="utf-8"))

            self.assertEqual(cleared, 2)
            self.assertEqual(store, {})
