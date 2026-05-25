import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.outputs import send_outputs


class FakeChannel:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, *, files=None):
        self.sent.append({"content": content, "files": files or []})
        for file in files or []:
            file.close()


class OutputTests(unittest.TestCase):
    def test_send_outputs_extracts_inline_svg_as_preview_attachment(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                job_dir = Path(tmp)
                (job_dir / "output.md").write_text(
                    "완료했습니다.\n\n```svg\n"
                    "<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"10\" height=\"10\"></svg>\n"
                    "```\n",
                    encoding="utf-8",
                )

                def fake_preview(svg_path):
                    preview = svg_path.with_name(svg_path.name + ".png")
                    preview.write_bytes(b"png")
                    return preview

                channel = FakeChannel()
                with mock.patch("src.outputs._render_svg_preview", fake_preview):
                    await send_outputs(channel, job_dir)
                return channel.sent

        sent = asyncio.run(scenario())
        text = "\n".join(message["content"] or "" for message in sent)
        filenames = [file.filename for message in sent for file in message["files"]]

        self.assertIn("SVG 미리보기 첨부", text)
        self.assertNotIn("<svg", text)
        self.assertIn("inline-svg-1.svg.png", filenames)
        self.assertIn("inline-svg-1.svg", filenames)

    def test_send_outputs_adds_png_preview_for_manifest_svg(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                job_dir = Path(tmp)
                (job_dir / "art.svg").write_text(
                    "<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"10\" height=\"10\"></svg>\n",
                    encoding="utf-8",
                )
                (job_dir / "manifest.json").write_text(
                    json.dumps({"files": [{"path": "art.svg", "label": "art"}]}, ensure_ascii=False),
                    encoding="utf-8",
                )

                def fake_preview(svg_path):
                    preview = svg_path.with_name(svg_path.name + ".png")
                    preview.write_bytes(b"png")
                    return preview

                channel = FakeChannel()
                with mock.patch("src.outputs._render_svg_preview", fake_preview):
                    await send_outputs(channel, job_dir)
                return channel.sent

        sent = asyncio.run(scenario())
        filenames = [file.filename for message in sent for file in message["files"]]
        self.assertEqual(filenames, ["art.svg.png", "art.svg"])

    def test_send_outputs_rejects_manifest_path_traversal(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                job_dir = Path(tmp) / "job"
                job_dir.mkdir()
                (job_dir / "output.md").write_text("본문", encoding="utf-8")
                (Path(tmp) / "secret.txt").write_text("secret", encoding="utf-8")
                (job_dir / "manifest.json").write_text(
                    json.dumps({"files": [{"path": "../secret.txt", "label": "secret"}]}, ensure_ascii=False),
                    encoding="utf-8",
                )
                channel = FakeChannel()
                await send_outputs(channel, job_dir)
                return channel.sent

        sent = asyncio.run(scenario())
        self.assertEqual(sent[0]["content"], "본문")
        self.assertFalse(any(message["files"] for message in sent))
        self.assertTrue(any("무시" in (message["content"] or "") for message in sent))

    def test_send_outputs_reports_malformed_manifest_without_raising(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                job_dir = Path(tmp)
                (job_dir / "manifest.json").write_text("{bad json", encoding="utf-8")
                channel = FakeChannel()
                await send_outputs(channel, job_dir)
                return channel.sent

        sent = asyncio.run(scenario())
        self.assertTrue(any("manifest.json" in (message["content"] or "") for message in sent))
