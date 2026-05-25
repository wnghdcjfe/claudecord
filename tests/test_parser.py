import os
import tempfile
import unittest
from unittest import mock

from src.parser import parse


class ParserTests(unittest.TestCase):
    def test_session_command_extracts_resume_id_and_prompt(self):
        cmd = parse("@sess-abc123 계속 진행")
        self.assertEqual(cmd.session_id, "sess-abc123")
        self.assertEqual(cmd.prompt, "계속 진행")
        self.assertIsNone(cmd.workdir)

    def test_project_alias_uses_single_project_root(self):
        with tempfile.TemporaryDirectory() as root:
            with mock.patch.dict(os.environ, {"PROJECT_ROOT": root}, clear=False):
                cmd = parse("@book 원고를 점검해줘")
        self.assertEqual(cmd.prompt, "원고를 점검해줘")
        self.assertEqual(cmd.workdir, os.path.join(root, "book"))
        self.assertIn("한국어", cmd.system_hint or "")

    def test_project_alias_ignores_old_per_project_path_variables(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as old_override:
            with mock.patch.dict(
                os.environ,
                {"PROJECT_ROOT": root, "PROJECT_BOOK_DIR": old_override},
                clear=False,
            ):
                cmd = parse("@book 수정")
        self.assertEqual(cmd.workdir, os.path.join(root, "book"))
