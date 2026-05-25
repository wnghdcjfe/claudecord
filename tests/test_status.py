import asyncio
import unittest

from src.status import (
    STATUS_FRAMES,
    WORKING_MESSAGE,
    format_working_status,
    run_spinning_loader,
    stop_spinning_loader,
)


class FakeMessage:
    def __init__(self, target_edits: int):
        self.edits = []
        self.ready = asyncio.Event()
        self.target_edits = target_edits

    async def edit(self, *, content=None):
        self.edits.append(content)
        if len(self.edits) >= self.target_edits:
            self.ready.set()


class StatusTests(unittest.TestCase):
    def test_format_working_status_emphasizes_working_message(self):
        status = format_working_status("job-123")

        self.assertIn(f"**{WORKING_MESSAGE}**", status)
        self.assertIn("▰▱▱▱▱", status)
        self.assertIn("`job-123`", status)
        self.assertGreaterEqual(status.count("\n"), 2)

    def test_format_working_status_cycles_activity_frames(self):
        statuses = {
            format_working_status("job-123", index)
            for index in range(len(STATUS_FRAMES))
        }

        self.assertEqual(len(statuses), len(STATUS_FRAMES))

    def test_spinning_loader_edits_message_until_stopped(self):
        async def scenario():
            message = FakeMessage(target_edits=2)
            task = asyncio.create_task(
                run_spinning_loader(message, "job-123", interval=0)
            )
            await asyncio.wait_for(message.ready.wait(), timeout=1)
            await stop_spinning_loader(task)
            return message.edits

        edits = asyncio.run(scenario())

        self.assertGreaterEqual(len(edits), 2)
        self.assertTrue(all(WORKING_MESSAGE in edit for edit in edits))
        self.assertGreaterEqual(len(set(edits)), 2)
