import json
import os
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from src import sessions


class SessionsTests(unittest.TestCase):
    def setUp(self):
        # TTL is env-configurable, so an ambient SESSION_TTL_SECONDS would
        # otherwise silently change what the default-TTL tests below assert.
        # Tests that exercise the override nest their own patch.dict on top.
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        patcher.start()
        os.environ.pop("SESSION_TTL_SECONDS", None)
        self.addCleanup(patcher.stop)

    def test_session_state_persists_session_workdir_and_timestamp(self):
        now = datetime(2026, 5, 25, 7, 0, tzinfo=UTC)

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
        now = datetime(2026, 5, 25, 7, 0, tzinfo=UTC)

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

    def test_session_ttl_can_be_overridden_by_env_var(self):
        now = datetime(2026, 5, 25, 7, 0, tzinfo=UTC)

        with tempfile.TemporaryDirectory() as tmp:
            store_path = Path(tmp) / "sessions.json"

            with mock.patch.object(sessions, "_STORE_PATH", store_path), \
                    mock.patch.dict(os.environ, {"SESSION_TTL_SECONDS": "5"}):
                sessions.set_session(123, "sess-1", workdir="/tmp/project", now=now)

                still_alive = sessions.get_session_state(123, now=now + timedelta(seconds=4))
                self.assertIsNotNone(still_alive)

                sessions.set_session(123, "sess-1", workdir="/tmp/project", now=now)
                expired = sessions.get_session_state(123, now=now + timedelta(seconds=6))
                self.assertIsNone(expired)

    def test_session_ttl_falls_back_to_default_on_invalid_env_value(self):
        now = datetime(2026, 5, 25, 7, 0, tzinfo=UTC)

        with tempfile.TemporaryDirectory() as tmp:
            store_path = Path(tmp) / "sessions.json"

            with mock.patch.object(sessions, "_STORE_PATH", store_path), \
                    mock.patch.dict(os.environ, {"SESSION_TTL_SECONDS": "not-a-number"}):
                sessions.set_session(123, "sess-1", workdir="/tmp/project", now=now)
                state = sessions.get_session_state(123, now=now + timedelta(minutes=59))

            self.assertIsNotNone(state)
            self.assertEqual(state.session_id, "sess-1")

    def test_session_ttl_falls_back_to_default_when_env_is_non_positive(self):
        now = datetime(2026, 5, 25, 7, 0, tzinfo=UTC)

        with tempfile.TemporaryDirectory() as tmp:
            store_path = Path(tmp) / "sessions.json"

            with mock.patch.object(sessions, "_STORE_PATH", store_path), \
                    mock.patch.dict(os.environ, {"SESSION_TTL_SECONDS": "0"}):
                sessions.set_session(123, "sess-1", workdir="/tmp/project", now=now)
                state = sessions.get_session_state(123, now=now + timedelta(minutes=59))

            self.assertIsNotNone(state)

    def test_session_ttl_default_is_unchanged_without_env_var(self):
        self.assertEqual(sessions.SESSION_TTL_SECONDS, 3600)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SESSION_TTL_SECONDS", None)
            self.assertEqual(sessions._effective_ttl_seconds(), 3600)

    # --- Issue #3: sessions.py must not do a full disk read+parse of the
    # whole store on every get/set call once warm. ---

    def test_repeated_reads_do_not_hit_disk_once_warm(self):
        now = datetime(2026, 5, 25, 7, 0, tzinfo=UTC)

        with tempfile.TemporaryDirectory() as tmp:
            store_path = Path(tmp) / "sessions.json"

            with mock.patch.object(sessions, "_STORE_PATH", store_path):
                # First call is the warm-up: it may (and does) touch disk to
                # persist the new session.
                sessions.set_session(123, "sess-1", workdir="/tmp/project", now=now)

                original_read_text = Path.read_text
                with mock.patch.object(Path, "read_text", autospec=True) as spy:
                    spy.side_effect = lambda self, *a, **kw: original_read_text(self, *a, **kw)

                    for _ in range(5):
                        state = sessions.get_session_state(123, now=now)
                        self.assertIsNotNone(state)
                        self.assertEqual(state.session_id, "sess-1")

                    # A subsequent write must not re-read the store from disk
                    # either — only the cached dict is mutated and re-saved.
                    sessions.set_session(123, "sess-1", workdir="/tmp/project2", now=now)
                    state = sessions.get_session_state(123, now=now)
                    self.assertEqual(state.workdir, "/tmp/project2")

                spy.assert_not_called()

    def test_store_cache_is_scoped_to_the_store_path(self):
        # Switching _STORE_PATH (as tests do, and as would happen if the
        # store location were ever reconfigured) must not leak a cached
        # store from one path into another.
        with tempfile.TemporaryDirectory() as tmp1, tempfile.TemporaryDirectory() as tmp2:
            path1 = Path(tmp1) / "sessions.json"
            path2 = Path(tmp2) / "sessions.json"

            with mock.patch.object(sessions, "_STORE_PATH", path1):
                sessions.set_session(1, "sess-a")

            with mock.patch.object(sessions, "_STORE_PATH", path2):
                self.assertIsNone(sessions.get_session_state(1))
                sessions.set_session(1, "sess-b")
                state = sessions.get_session_state(1)
                self.assertEqual(state.session_id, "sess-b")

            with mock.patch.object(sessions, "_STORE_PATH", path1):
                state = sessions.get_session_state(1)
                self.assertEqual(state.session_id, "sess-a")

    def test_clear_session_updates_cache_so_later_reads_see_it_cleared(self):
        with tempfile.TemporaryDirectory() as tmp:
            store_path = Path(tmp) / "sessions.json"

            with mock.patch.object(sessions, "_STORE_PATH", store_path):
                sessions.set_session(123, "sess-1", workdir="/tmp/project")
                self.assertIsNotNone(sessions.get_session_state(123))

                sessions.clear_session(123)
                self.assertIsNone(sessions.get_session_state(123))

    # --- H2: the project hint must survive to the next turn. ---

    def test_system_hint_round_trips_through_the_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            store_path = Path(tmp) / "sessions.json"

            with mock.patch.object(sessions, "_STORE_PATH", store_path):
                sessions.set_session(
                    123,
                    "sess-1",
                    workdir="/tmp/avisspick",
                    system_hint="어비스픽 투자 리포트 서비스. 보안 정보 출력 금지.",
                )
                state = sessions.get_session_state(123)

            self.assertEqual(state.system_hint, "어비스픽 투자 리포트 서비스. 보안 정보 출력 금지.")
            self.assertEqual(state.workdir, "/tmp/avisspick")

    def test_set_session_without_a_hint_stores_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            store_path = Path(tmp) / "sessions.json"

            with mock.patch.object(sessions, "_STORE_PATH", store_path):
                sessions.set_session(123, "sess-1", workdir="/tmp/project")
                state = sessions.get_session_state(123)

            self.assertIsNone(state.system_hint)

    def test_stores_written_before_hints_existed_are_still_readable(self):
        # Both legacy shapes: the bare-string store and the dict store that
        # predates the system_hint key.
        with tempfile.TemporaryDirectory() as tmp:
            store_path = Path(tmp) / "sessions.json"
            store_path.write_text(
                json.dumps(
                    {
                        "1": "legacy-string-sess",
                        "2": {"session_id": "legacy-dict-sess", "workdir": "/tmp/old"},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            with mock.patch.object(sessions, "_STORE_PATH", store_path):
                legacy_string = sessions.get_session_state(1)
                legacy_dict = sessions.get_session_state(2)

            self.assertEqual(legacy_string.session_id, "legacy-string-sess")
            self.assertIsNone(legacy_string.system_hint)
            self.assertEqual(legacy_dict.session_id, "legacy-dict-sess")
            self.assertEqual(legacy_dict.workdir, "/tmp/old")
            self.assertIsNone(legacy_dict.system_hint)

    def test_non_string_system_hint_in_the_store_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            store_path = Path(tmp) / "sessions.json"
            store_path.write_text(
                json.dumps({"1": {"session_id": "s", "system_hint": {"nope": 1}}}),
                encoding="utf-8",
            )

            with mock.patch.object(sessions, "_STORE_PATH", store_path):
                state = sessions.get_session_state(1)

            self.assertEqual(state.session_id, "s")
            self.assertIsNone(state.system_hint)

    # --- L4: a store write that fails must not raise into the request path. ---

    def _unwritable_store_path(self, tmp: str) -> Path:
        # The store's parent is a regular file, so _save's mkdir raises
        # NotADirectoryError (an OSError) on every platform.
        blocker = Path(tmp) / "blocker"
        blocker.write_text("not a directory", encoding="utf-8")
        return blocker / "sessions.json"

    def test_set_session_swallows_a_write_failure_instead_of_raising(self):
        with tempfile.TemporaryDirectory() as tmp:
            store_path = self._unwritable_store_path(tmp)

            with mock.patch.object(sessions, "_STORE_PATH", store_path):
                with self.assertLogs("src.sessions", level="WARNING"):
                    sessions.set_session(123, "sess-1", workdir="/tmp/project")

                # The cache must not claim a session the disk never got --
                # a later read would otherwise resume a conversation that
                # survives only in this process's memory.
                self.assertIsNone(sessions.get_session_state(123))

    def test_clear_session_swallows_a_write_failure_instead_of_raising(self):
        with tempfile.TemporaryDirectory() as tmp:
            store_path = self._unwritable_store_path(tmp)

            with (
                mock.patch.object(sessions, "_STORE_PATH", store_path),
                self.assertLogs("src.sessions", level="WARNING"),
            ):
                sessions.clear_session(123)  # must not raise

    def test_clear_all_sessions_reports_zero_when_the_write_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            store_path = Path(tmp) / "sessions.json"

            with mock.patch.object(sessions, "_STORE_PATH", store_path):
                sessions.set_session(123, "sess-1")
                sessions.set_session(456, "sess-2")

                # The count reported back to the user ("N개 초기화") must not
                # describe a cleanup the disk never accepted.
                with (
                    mock.patch.object(Path, "write_text", side_effect=OSError("disk full")),
                    self.assertLogs("src.sessions", level="WARNING"),
                ):
                    self.assertEqual(sessions.clear_all_sessions(), 0)

                self.assertIsNotNone(sessions.get_session_state(123))

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
