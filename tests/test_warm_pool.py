import asyncio
import os
import threading
import unittest
from unittest import mock

from src.warm_pool import (
    DEFAULT_IDLE_TTL_SECONDS,
    DEFAULT_MAX_PROCESSES,
    WarmClaudePool,
    WarmKey,
    WarmProcess,
    idle_ttl_seconds,
    max_processes,
    warm_enabled,
)


class _FakeProc:
    """Just enough of asyncio.subprocess.Process for pool bookkeeping."""

    def __init__(self, returncode=None):
        self.returncode = returncode
        self.stdin = None
        self.stdout = None
        self.stderr = None


def _wp(key, session_id, *, returncode=None):
    wp = WarmProcess(_FakeProc(returncode), key)
    wp.session_id = session_id
    return wp


KEY = WarmKey(workdir="/tmp/project", model="sonnet", system_hint=None)
OTHER_KEY = WarmKey(workdir="/tmp/other", model="sonnet", system_hint=None)


class WarmConfigTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        patcher.start()
        for name in (
            "WARM_CLAUDE",
            "WARM_CLAUDE_IDLE_TTL_SECONDS",
            "WARM_CLAUDE_MAX_PROCESSES",
        ):
            os.environ.pop(name, None)
        self.addCleanup(patcher.stop)

    def test_warm_is_enabled_by_default(self):
        self.assertTrue(warm_enabled())

    def test_warm_can_be_switched_off(self):
        for value in ("0", "false", "no", "OFF"):
            with self.subTest(value=value):
                self.assertFalse(warm_enabled({"WARM_CLAUDE": value}))

    def test_warm_accepts_truthy_spellings(self):
        for value in ("1", "true", "YES", "on"):
            with self.subTest(value=value):
                self.assertTrue(warm_enabled({"WARM_CLAUDE": value}))

    def test_unparseable_warm_flag_falls_back_to_default(self):
        self.assertTrue(warm_enabled({"WARM_CLAUDE": "maybe"}))

    def test_idle_ttl_defaults_and_overrides(self):
        self.assertEqual(idle_ttl_seconds(), DEFAULT_IDLE_TTL_SECONDS)
        self.assertEqual(idle_ttl_seconds({"WARM_CLAUDE_IDLE_TTL_SECONDS": "12.5"}), 12.5)

    def test_invalid_or_nonpositive_ttl_falls_back_to_default(self):
        for value in ("", "abc", "0", "-5"):
            with self.subTest(value=value):
                self.assertEqual(
                    idle_ttl_seconds({"WARM_CLAUDE_IDLE_TTL_SECONDS": value}),
                    DEFAULT_IDLE_TTL_SECONDS,
                )

    def test_max_processes_defaults_and_overrides(self):
        self.assertEqual(max_processes(), DEFAULT_MAX_PROCESSES)
        self.assertEqual(max_processes({"WARM_CLAUDE_MAX_PROCESSES": "5"}), 5)

    def test_max_processes_zero_is_honoured_but_negative_is_not(self):
        # 0 is a real setting: spawn in streaming mode but never reuse.
        self.assertEqual(max_processes({"WARM_CLAUDE_MAX_PROCESSES": "0"}), 0)
        self.assertEqual(
            max_processes({"WARM_CLAUDE_MAX_PROCESSES": "-1"}), DEFAULT_MAX_PROCESSES
        )
        self.assertEqual(
            max_processes({"WARM_CLAUDE_MAX_PROCESSES": "two"}), DEFAULT_MAX_PROCESSES
        )


class WarmPoolTests(unittest.TestCase):
    def _pool(self, **env):
        self.retired = []

        async def retire(wp):
            self.retired.append(wp)

        return WarmClaudePool(retire=retire, environ=env)

    def test_park_then_acquire_returns_the_same_process(self):
        async def scenario():
            pool = self._pool()
            wp = _wp(KEY, "sess-1")
            self.assertEqual(pool.park(wp), [])
            self.assertEqual(len(pool), 1)
            got = pool.acquire(KEY, "sess-1")
            self.assertIs(got, wp)
            self.assertEqual(len(pool), 0)
            self.assertEqual(self.retired, [])

        asyncio.run(scenario())

    def test_acquire_requires_a_matching_session_id(self):
        async def scenario():
            pool = self._pool()
            pool.park(_wp(KEY, "sess-1"))
            # A different conversation must never inherit this process.
            self.assertIsNone(pool.acquire(KEY, "sess-2"))
            # ...and neither must a brand-new one.
            self.assertIsNone(pool.acquire(KEY, None))
            self.assertEqual(len(pool), 1)
            pool.reset()

        asyncio.run(scenario())

    def test_acquire_requires_a_matching_key(self):
        async def scenario():
            pool = self._pool()
            pool.park(_wp(KEY, "sess-1"))
            self.assertIsNone(pool.acquire(OTHER_KEY, "sess-1"))
            pool.reset()

        asyncio.run(scenario())

    def test_acquire_skips_a_process_that_died_while_parked(self):
        async def scenario():
            pool = self._pool()
            wp = _wp(KEY, "sess-1")
            pool.park(wp)
            wp.proc.returncode = 1
            self.assertIsNone(pool.acquire(KEY, "sess-1"))
            self.assertEqual(len(pool), 0)
            # Let the retire acquire scheduled for the corpse run before the
            # loop closes (see the L1 test below for what it must do).
            await asyncio.sleep(0)

        asyncio.run(scenario())

    def test_acquire_retires_a_process_that_died_while_parked(self):
        # L1: popping the dead entry is not enough. Its stdin pipe and its
        # still-running stderr drain task are only released by the retire
        # callback, so acquire must hand the corpse over instead of dropping it
        # and waiting for garbage collection.
        async def scenario():
            pool = self._pool()
            wp = _wp(KEY, "sess-1")
            pool.park(wp)
            wp.proc.returncode = 1

            self.assertIsNone(pool.acquire(KEY, "sess-1"))
            for _ in range(500):
                if self.retired:
                    break
                await asyncio.sleep(0.01)

            self.assertEqual(self.retired, [wp])
            self.assertEqual(len(pool), 0)

        asyncio.run(scenario())

    def test_dead_or_sessionless_process_is_never_parked(self):
        async def scenario():
            pool = self._pool()
            dead = _wp(KEY, "sess-1", returncode=0)
            self.assertEqual(pool.park(dead), [dead])

            anonymous = _wp(KEY, None)
            self.assertEqual(pool.park(anonymous), [anonymous])
            self.assertEqual(len(pool), 0)

        asyncio.run(scenario())

    def test_park_is_refused_when_warm_is_disabled(self):
        async def scenario():
            pool = self._pool(WARM_CLAUDE="0")
            wp = _wp(KEY, "sess-1")
            self.assertEqual(pool.park(wp), [wp])
            self.assertEqual(len(pool), 0)

        asyncio.run(scenario())

    def test_zero_capacity_never_parks(self):
        async def scenario():
            pool = self._pool(WARM_CLAUDE_MAX_PROCESSES="0")
            wp = _wp(KEY, "sess-1")
            self.assertEqual(pool.park(wp), [wp])
            self.assertEqual(len(pool), 0)

        asyncio.run(scenario())

    def test_capacity_evicts_the_oldest_parked_process(self):
        async def scenario():
            pool = self._pool(WARM_CLAUDE_MAX_PROCESSES="2")
            first = _wp(KEY, "sess-1")
            second = _wp(KEY, "sess-2")
            third = _wp(KEY, "sess-3")
            self.assertEqual(pool.park(first), [])
            self.assertEqual(pool.park(second), [])
            self.assertEqual(pool.park(third), [first])
            self.assertEqual(len(pool), 2)
            self.assertIsNone(pool.acquire(KEY, "sess-1"))
            self.assertIsNotNone(pool.acquire(KEY, "sess-2"))
            self.assertIsNotNone(pool.acquire(KEY, "sess-3"))

        asyncio.run(scenario())

    def test_reparking_the_same_session_replaces_the_previous_process(self):
        async def scenario():
            pool = self._pool()
            first = _wp(KEY, "sess-1")
            replacement = _wp(KEY, "sess-1")
            pool.park(first)
            self.assertEqual(pool.park(replacement), [first])
            self.assertEqual(len(pool), 1)
            self.assertIs(pool.acquire(KEY, "sess-1"), replacement)

        asyncio.run(scenario())

    def test_the_same_session_is_never_parked_under_two_keys(self):
        # M4: WarmKey carries the workdir, so a channel whose workdir goes
        # A -> B -> A used to leave two parked processes claiming one
        # conversation. Turn 3, keyed back to A, would be handed the process
        # that stopped listening after turn 1 and the bot would look amnesic.
        async def scenario():
            pool = self._pool()
            in_a = _wp(KEY, "sess-1")
            in_b = _wp(OTHER_KEY, "sess-1")
            pool.park(in_a)

            self.assertEqual(pool.park(in_b), [in_a])
            self.assertEqual(len(pool), 1)
            # The stale copy under the old key is gone, not merely shadowed.
            self.assertIsNone(pool.acquire(KEY, "sess-1"))
            self.assertIs(pool.acquire(OTHER_KEY, "sess-1"), in_b)

        asyncio.run(scenario())

    def test_parking_leaves_other_conversations_under_other_keys_alone(self):
        async def scenario():
            pool = self._pool()
            unrelated = _wp(KEY, "sess-1")
            pool.park(unrelated)
            self.assertEqual(pool.park(_wp(OTHER_KEY, "sess-2")), [])
            self.assertEqual(len(pool), 2)
            self.assertIs(pool.acquire(KEY, "sess-1"), unrelated)

        asyncio.run(scenario())

    def test_eviction_terminates_even_when_an_entry_no_longer_matches_its_key(self):
        # S5: the eviction loop used to rebuild the dict key from the entry and
        # pop that. A rebuilt key that misses leaves len() unchanged -- an
        # infinite loop on the event loop thread, i.e. every channel frozen.
        # park() runs on a daemon thread here so a regression fails this test
        # instead of hanging the whole suite (and blocking interpreter exit).
        pool = self._pool(WARM_CLAUDE_MAX_PROCESSES="1")
        first = _wp(KEY, "sess-1")
        pool.park(first)
        first.session_id = "renamed-behind-the-pools-back"

        result = {}

        def park_second():
            result["retire"] = pool.park(_wp(KEY, "sess-2"))

        thread = threading.Thread(target=park_second, daemon=True)
        thread.start()
        thread.join(5)

        self.assertFalse(thread.is_alive(), "park() never returned: eviction spun")
        self.assertEqual(result["retire"], [first])
        self.assertEqual(len(pool), 1)

    def test_idle_process_is_retired_after_the_ttl(self):
        async def scenario():
            pool = self._pool(WARM_CLAUDE_IDLE_TTL_SECONDS="0.05")
            wp = _wp(KEY, "sess-1")
            pool.park(wp)
            self.assertEqual(len(pool), 1)
            # Poll on the retire itself: expiry pops the pool entry *before*
            # awaiting retire, so an empty pool does not yet mean it ran.
            # (A fixed sleep here is also flaky under load.)
            for _ in range(500):
                if self.retired:
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(len(pool), 0)
            self.assertEqual(self.retired, [wp])

        asyncio.run(scenario())

    def test_acquiring_cancels_the_expiry_so_a_busy_process_is_not_retired(self):
        async def scenario():
            pool = self._pool(WARM_CLAUDE_IDLE_TTL_SECONDS="0.05")
            wp = _wp(KEY, "sess-1")
            pool.park(wp)
            self.assertIs(pool.acquire(KEY, "sess-1"), wp)
            await asyncio.sleep(0.2)
            self.assertEqual(self.retired, [])

        asyncio.run(scenario())

    def test_reset_returns_every_parked_process_and_empties_the_pool(self):
        async def scenario():
            pool = self._pool()
            first = _wp(KEY, "sess-1")
            second = _wp(KEY, "sess-2")
            pool.park(first)
            pool.park(second)
            self.assertEqual(set(pool.reset()), {first, second})
            self.assertEqual(len(pool), 0)
            self.assertIsNone(pool.acquire(KEY, "sess-1"))

        asyncio.run(scenario())

    def test_reset_cancels_pending_expiry_tasks(self):
        async def scenario():
            pool = self._pool(WARM_CLAUDE_IDLE_TTL_SECONDS="0.05")
            wp = _wp(KEY, "sess-1")
            pool.park(wp)
            pool.reset()
            await asyncio.sleep(0.2)
            # reset() hands the process to the caller; the expiry task must not
            # also retire it behind the caller's back.
            self.assertEqual(self.retired, [])

        asyncio.run(scenario())

    def test_forget_drops_bookkeeping_without_retiring(self):
        async def scenario():
            pool = self._pool()
            wp = _wp(KEY, "sess-1")
            pool.park(wp)
            pool.forget(wp)
            self.assertEqual(len(pool), 0)
            self.assertEqual(self.retired, [])

        asyncio.run(scenario())


class WarmProcessMessageTests(unittest.TestCase):
    def test_send_user_message_writes_one_stream_json_line(self):
        written = []

        class _Stdin:
            def write(self, data):
                written.append(data)

            async def drain(self):
                return None

        async def scenario():
            proc = _FakeProc()
            proc.stdin = _Stdin()
            wp = WarmProcess(proc, KEY)
            await wp.send_user_message("한글 프롬프트")

        asyncio.run(scenario())

        self.assertEqual(len(written), 1)
        payload = written[0].decode("utf-8")
        self.assertTrue(payload.endswith("\n"))
        import json

        parsed = json.loads(payload)
        self.assertEqual(parsed["type"], "user")
        self.assertEqual(parsed["message"], {"role": "user", "content": "한글 프롬프트"})

    def test_send_user_message_raises_without_a_stdin_pipe(self):
        async def scenario():
            wp = WarmProcess(_FakeProc(), KEY)
            with self.assertRaises(BrokenPipeError):
                await wp.send_user_message("hi")

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
