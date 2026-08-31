import json
import os
import tempfile
import threading
import time
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from src import sessions

# A stand-in for whatever a user configures in projects.toml. Kept generic so
# the repo carries no one's real project names (issue #16).
SAMPLE_PROJECT_HINT = "샘플 프로젝트. 보안 정보 출력 금지."


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
                    workdir="/tmp/sample",
                    system_hint=SAMPLE_PROJECT_HINT,
                )
                state = sessions.get_session_state(123)

            self.assertEqual(state.system_hint, SAMPLE_PROJECT_HINT)
            self.assertEqual(state.workdir, "/tmp/sample")

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

    # --- Issue #27.3: an already-empty store must not be rewritten. ---

    def test_clear_all_sessions_does_not_touch_disk_when_the_store_is_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            store_path = Path(tmp) / "sessions.json"

            with mock.patch.object(sessions, "_STORE_PATH", store_path):
                with mock.patch.object(Path, "write_text", autospec=True) as spy:
                    self.assertEqual(sessions.clear_all_sessions(), 0)
                spy.assert_not_called()

            self.assertFalse(store_path.exists())

    def test_repeated_clear_all_sessions_writes_only_the_first_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            store_path = Path(tmp) / "sessions.json"

            with mock.patch.object(sessions, "_STORE_PATH", store_path):
                sessions.set_session(123, "sess-1")
                self.assertEqual(sessions.clear_all_sessions(), 1)

                mtime_after_clear = store_path.stat().st_mtime_ns
                with mock.patch.object(Path, "write_text", autospec=True) as spy:
                    self.assertEqual(sessions.clear_all_sessions(), 0)
                spy.assert_not_called()
                self.assertEqual(store_path.stat().st_mtime_ns, mtime_after_clear)

    # --- Issue #14.1: a corrupt store must not take the bot down. ---

    def test_corrupt_store_is_backed_up_and_the_bot_keeps_working(self):
        truncated = '{"123": {"session_id": "sess-1", "workd'

        with tempfile.TemporaryDirectory() as tmp:
            store_path = Path(tmp) / "sessions.json"
            store_path.write_text(truncated, encoding="utf-8")

            with mock.patch.object(sessions, "_STORE_PATH", store_path):
                # get_session_state sits on every incoming message; it used to
                # raise JSONDecodeError here and keep raising forever.
                with self.assertLogs("src.sessions", level="WARNING"):
                    self.assertIsNone(sessions.get_session_state(123))

                sessions.set_session(123, "sess-2", workdir="/tmp/project")
                recovered = sessions.get_session_state(123)

            self.assertEqual(recovered.session_id, "sess-2")

            backup = store_path.with_name("sessions.json.corrupt")
            self.assertEqual(backup.read_text(encoding="utf-8"), truncated)

            store = json.loads(store_path.read_text(encoding="utf-8"))
            self.assertEqual(store["123"]["session_id"], "sess-2")

    def test_valid_json_that_is_not_an_object_is_treated_as_corrupt(self):
        with tempfile.TemporaryDirectory() as tmp:
            store_path = Path(tmp) / "sessions.json"
            store_path.write_text('["not", "a", "store"]', encoding="utf-8")

            with mock.patch.object(sessions, "_STORE_PATH", store_path):
                with self.assertLogs("src.sessions", level="WARNING"):
                    self.assertIsNone(sessions.get_session_state(123))
                sessions.set_session(123, "sess-1")
                self.assertEqual(sessions.get_session_state(123).session_id, "sess-1")

            self.assertTrue(store_path.with_name("sessions.json.corrupt").exists())

    def test_a_corrupt_store_that_cannot_be_backed_up_still_does_not_raise(self):
        with tempfile.TemporaryDirectory() as tmp:
            store_path = Path(tmp) / "sessions.json"
            store_path.write_text("{oops", encoding="utf-8")

            with (
                mock.patch.object(sessions, "_STORE_PATH", store_path),
                mock.patch.object(os, "replace", side_effect=OSError("read-only fs")),
                self.assertLogs("src.sessions", level="WARNING"),
            ):
                self.assertIsNone(sessions.get_session_state(123))

    # --- Issue #14.2a: writes must be atomic. ---

    def test_saves_never_write_the_store_path_in_place(self):
        original_write_text = Path.write_text

        with tempfile.TemporaryDirectory() as tmp:
            store_path = Path(tmp) / "sessions.json"

            with mock.patch.object(sessions, "_STORE_PATH", store_path):
                with mock.patch.object(Path, "write_text", autospec=True) as spy:
                    spy.side_effect = lambda self, *a, **kw: original_write_text(self, *a, **kw)
                    sessions.set_session(123, "sess-1")

                written = [call.args[0] for call in spy.call_args_list]

            self.assertTrue(written)
            # Every byte goes to a temp sibling that os.replace then swaps in.
            self.assertNotIn(store_path, written)
            self.assertEqual(json.loads(store_path.read_text(encoding="utf-8"))["123"]["session_id"], "sess-1")
            self.assertEqual([p.name for p in Path(tmp).iterdir()], ["sessions.json"])

    def test_a_write_that_dies_halfway_leaves_the_previous_store_intact(self):
        original_write_text = Path.write_text

        def half_written_then_crash(self, data, *args, **kwargs):
            # Models the process dying mid-write: half the bytes land, the
            # rest never do.
            original_write_text(self, data[: len(data) // 2], *args, **kwargs)
            raise OSError("no space left on device")

        with tempfile.TemporaryDirectory() as tmp:
            store_path = Path(tmp) / "sessions.json"

            with mock.patch.object(sessions, "_STORE_PATH", store_path):
                sessions.set_session(123, "sess-1", workdir="/tmp/a")

                with (
                    mock.patch.object(
                        Path, "write_text", autospec=True, side_effect=half_written_then_crash
                    ),
                    self.assertLogs("src.sessions", level="WARNING"),
                ):
                    sessions.set_session(456, "sess-2", workdir="/tmp/b")

            store = json.loads(store_path.read_text(encoding="utf-8"))
            self.assertEqual(store["123"]["session_id"], "sess-1")
            self.assertNotIn("456", store)
            # The half-written temp file is cleaned up rather than left to be
            # mistaken for a store later.
            self.assertEqual([p.name for p in Path(tmp).iterdir()], ["sessions.json"])

    # --- Issue #14.2b: concurrent writes must not lose sessions. ---

    def test_concurrent_writes_do_not_lose_sessions(self):
        channels = list(range(1, 9))
        start = threading.Barrier(len(channels))
        failures: list[BaseException] = []

        def slow_now():
            # Widens the read-modify-write window: set_session calls _now()
            # after reading the store and before writing it back. Unserialized,
            # all eight threads would read the same empty store and the last
            # writer would win, silently dropping seven sessions.
            time.sleep(0.005)
            return datetime.now(UTC)

        def worker(channel_id: int) -> None:
            try:
                start.wait(timeout=5)
                sessions.set_session(channel_id, f"sess-{channel_id}", workdir=f"/tmp/{channel_id}")
            # Anything at all: a thread that dies silently would let this test
            # pass on a store that never got written. Re-raised on the main
            # thread once every worker has joined.
            except BaseException as exc:
                failures.append(exc)

        with tempfile.TemporaryDirectory() as tmp:
            store_path = Path(tmp) / "sessions.json"

            with (
                mock.patch.object(sessions, "_STORE_PATH", store_path),
                mock.patch.object(sessions, "_now", side_effect=slow_now),
            ):
                threads = [threading.Thread(target=worker, args=(c,)) for c in channels]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=30)

                self.assertEqual(failures, [])
                self.assertFalse([t for t in threads if t.is_alive()])

                for channel_id in channels:
                    state = sessions.get_session_state(channel_id)
                    self.assertIsNotNone(state, f"channel {channel_id} lost its session")
                    self.assertEqual(state.session_id, f"sess-{channel_id}")

            on_disk = json.loads(store_path.read_text(encoding="utf-8"))
            self.assertEqual(sorted(on_disk), sorted(str(c) for c in channels))

    def test_concurrent_clears_and_writes_leave_the_store_readable(self):
        # A clear racing a write may legitimately win or lose, but it must
        # never leave a store that cannot be parsed back.
        channels = list(range(1, 6))
        failures: list[BaseException] = []

        with tempfile.TemporaryDirectory() as tmp:
            store_path = Path(tmp) / "sessions.json"

            def writer(channel_id: int) -> None:
                try:
                    for _ in range(20):
                        sessions.set_session(channel_id, f"sess-{channel_id}")
                        sessions.clear_session(channel_id)
                # Same reason as above: collected here, re-raised on the main
                # thread so a worker failure cannot pass as success.
                except BaseException as exc:
                    failures.append(exc)

            with mock.patch.object(sessions, "_STORE_PATH", store_path):
                threads = [threading.Thread(target=writer, args=(c,)) for c in channels]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=30)

            self.assertEqual(failures, [])
            self.assertEqual(json.loads(store_path.read_text(encoding="utf-8")), {})
