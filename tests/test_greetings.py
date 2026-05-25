import unittest

from src.greetings import GREETING_REPLY, direct_reply_for


class GreetingTests(unittest.TestCase):
    def test_direct_reply_for_korean_greeting(self):
        self.assertEqual(
            direct_reply_for("안녕"),
            GREETING_REPLY,
        )

    def test_direct_reply_ignores_surrounding_whitespace(self):
        self.assertEqual(
            direct_reply_for("  안녕\n"),
            GREETING_REPLY,
        )

    def test_direct_reply_only_matches_exact_greeting(self):
        self.assertIsNone(direct_reply_for("안녕 자비스"))
