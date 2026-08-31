import asyncio
import json
import os
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from src import outputs
from src.outputs import send_outputs


class FakeChannel:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, *, files=None):
        files = files or []
        # Snapshot each file's bytes before closing so tests can assert on
        # attachment content (e.g. the full body shipped as response.md).
        file_bytes = [file.fp.read() for file in files]
        self.sent.append({"content": content, "files": files, "file_bytes": file_bytes})
        for file in files:
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

    def test_send_outputs_no_warning_when_output_present_but_no_manifest(self):
        # Contract v2: producing no artifacts is the normal case and must
        # not trigger the old "manifest.json이 없습니다" style warning, even
        # with the default warn_missing_manifest=True.
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                job_dir = Path(tmp)
                (job_dir / "output.md").write_text("부분 결과", encoding="utf-8")
                channel = FakeChannel()
                await send_outputs(channel, job_dir)
                return channel.sent

        sent = asyncio.run(scenario())
        self.assertEqual([message["content"] for message in sent], ["부분 결과"])

    def test_send_outputs_sends_notice_for_genuinely_empty_result(self):
        # No output.md, no body_text, no meta.json/manifest.json at all: a
        # real empty result should still surface *some* notice by default.
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                job_dir = Path(tmp)
                channel = FakeChannel()
                await send_outputs(channel, job_dir)
                return channel.sent

        sent = asyncio.run(scenario())
        self.assertEqual(len(sent), 1)
        self.assertNotIn("manifest.json", sent[0]["content"] or "")

    def test_send_outputs_can_suppress_empty_result_notice(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                job_dir = Path(tmp)
                channel = FakeChannel()
                await send_outputs(channel, job_dir, warn_missing_manifest=False)
                return channel.sent

        sent = asyncio.run(scenario())
        self.assertEqual(sent, [])

    def test_send_outputs_uses_body_text_when_no_output_md(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                job_dir = Path(tmp)
                channel = FakeChannel()
                await send_outputs(channel, job_dir, body_text="스트림 최종 텍스트입니다.")
                return channel.sent

        sent = asyncio.run(scenario())
        self.assertEqual([message["content"] for message in sent], ["스트림 최종 텍스트입니다."])

    def test_send_outputs_prefers_existing_nonempty_output_md_over_body_text(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                job_dir = Path(tmp)
                (job_dir / "output.md").write_text("파일 본문 우선", encoding="utf-8")
                channel = FakeChannel()
                await send_outputs(channel, job_dir, body_text="무시되어야 하는 텍스트")
                return channel.sent

        sent = asyncio.run(scenario())
        contents = [message["content"] for message in sent]
        self.assertIn("파일 본문 우선", contents)
        self.assertNotIn("무시되어야 하는 텍스트", contents)

    def test_send_outputs_falls_back_to_body_text_when_output_md_empty(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                job_dir = Path(tmp)
                (job_dir / "output.md").write_text("   \n", encoding="utf-8")
                channel = FakeChannel()
                await send_outputs(channel, job_dir, body_text="빈 파일 대신 이 텍스트")
                return channel.sent

        sent = asyncio.run(scenario())
        self.assertEqual([message["content"] for message in sent], ["빈 파일 대신 이 텍스트"])

    def test_send_outputs_non_utf8_output_md_falls_back_to_body_text(self):
        # output.md's read is the last unguarded read in send_outputs — a
        # non-UTF-8 file must not raise UnicodeDecodeError out of the
        # function (which would drop the body AND every attachment).
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                job_dir = Path(tmp)
                (job_dir / "output.md").write_bytes(b"\xff\xfe\x00bad-utf8")
                channel = FakeChannel()
                await send_outputs(channel, job_dir, body_text="정상 폴백 텍스트")  # must not raise
                return channel.sent

        sent = asyncio.run(scenario())
        self.assertEqual([message["content"] for message in sent], ["정상 폴백 텍스트"])

    def test_send_outputs_extracts_inline_svg_from_body_text_too(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                job_dir = Path(tmp)

                def fake_preview(svg_path):
                    preview = svg_path.with_name(svg_path.name + ".png")
                    preview.write_bytes(b"png")
                    return preview

                channel = FakeChannel()
                body_text = (
                    "완료했습니다.\n\n```svg\n"
                    "<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"10\" height=\"10\"></svg>\n"
                    "```\n"
                )
                with mock.patch("src.outputs._render_svg_preview", fake_preview):
                    await send_outputs(channel, job_dir, body_text=body_text)
                return channel.sent

        sent = asyncio.run(scenario())
        text = "\n".join(message["content"] or "" for message in sent)
        filenames = [file.filename for message in sent for file in message["files"]]

        self.assertIn("SVG 미리보기 첨부", text)
        self.assertNotIn("<svg", text)
        self.assertIn("inline-svg-1.svg.png", filenames)

    def test_send_outputs_prefers_meta_json_over_manifest_json(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                job_dir = Path(tmp)
                (job_dir / "from-meta.txt").write_text("meta", encoding="utf-8")
                (job_dir / "from-manifest.txt").write_text("manifest", encoding="utf-8")
                (job_dir / "meta.json").write_text(
                    json.dumps({"files": [{"path": "from-meta.txt", "label": "m"}]}, ensure_ascii=False),
                    encoding="utf-8",
                )
                (job_dir / "manifest.json").write_text(
                    json.dumps({"files": [{"path": "from-manifest.txt", "label": "l"}]}, ensure_ascii=False),
                    encoding="utf-8",
                )
                channel = FakeChannel()
                await send_outputs(channel, job_dir)
                return channel.sent

        sent = asyncio.run(scenario())
        filenames = [file.filename for message in sent for file in message["files"]]
        self.assertEqual(filenames, ["from-meta.txt"])

    def test_send_outputs_falls_back_to_manifest_json_when_meta_json_missing(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                job_dir = Path(tmp)
                (job_dir / "legacy.txt").write_text("legacy", encoding="utf-8")
                (job_dir / "manifest.json").write_text(
                    json.dumps({"files": [{"path": "legacy.txt", "label": "l"}]}, ensure_ascii=False),
                    encoding="utf-8",
                )
                channel = FakeChannel()
                await send_outputs(channel, job_dir)
                return channel.sent

        sent = asyncio.run(scenario())
        filenames = [file.filename for message in sent for file in message["files"]]
        self.assertEqual(filenames, ["legacy.txt"])

    # --- M2 regression: fallback must be key-level, not file-level. A
    # meta.json that exists but has no usable "files" key must not shadow a
    # legacy manifest.json that still lists real attachments. ---

    def test_send_outputs_meta_with_only_workdir_falls_back_to_manifest_files(self):
        # meta.json = {"workdir": ...} only (a perfectly normal, artifact-less
        # meta.json per the "omit keys that don't apply" instruction) must not
        # swallow a manifest.json that still lists real files.
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                job_dir = Path(tmp)
                (job_dir / "report.png").write_bytes(b"png-bytes")
                (job_dir / "meta.json").write_text(
                    json.dumps({"workdir": "/tmp/project"}, ensure_ascii=False),
                    encoding="utf-8",
                )
                (job_dir / "manifest.json").write_text(
                    json.dumps({"files": [{"path": "report.png", "label": "report"}]}, ensure_ascii=False),
                    encoding="utf-8",
                )
                channel = FakeChannel()
                await send_outputs(channel, job_dir)
                return channel.sent

        sent = asyncio.run(scenario())
        filenames = [file.filename for message in sent for file in message["files"]]
        self.assertEqual(filenames, ["report.png"])

    def test_send_outputs_meta_empty_object_falls_back_to_manifest_files(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                job_dir = Path(tmp)
                (job_dir / "report.png").write_bytes(b"png-bytes")
                (job_dir / "meta.json").write_text("{}", encoding="utf-8")
                (job_dir / "manifest.json").write_text(
                    json.dumps({"files": [{"path": "report.png", "label": "report"}]}, ensure_ascii=False),
                    encoding="utf-8",
                )
                channel = FakeChannel()
                await send_outputs(channel, job_dir)
                return channel.sent

        sent = asyncio.run(scenario())
        filenames = [file.filename for message in sent for file in message["files"]]
        self.assertEqual(filenames, ["report.png"])

    def test_send_outputs_malformed_meta_falls_back_to_manifest_files(self):
        # A broken meta.json must not block manifest.json's attachments, and
        # must not leave the user with a parse-error message instead of the
        # files they actually produced.
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                job_dir = Path(tmp)
                (job_dir / "report.png").write_bytes(b"png-bytes")
                (job_dir / "meta.json").write_text("{bad json", encoding="utf-8")
                (job_dir / "manifest.json").write_text(
                    json.dumps({"files": [{"path": "report.png", "label": "report"}]}, ensure_ascii=False),
                    encoding="utf-8",
                )
                channel = FakeChannel()
                await send_outputs(channel, job_dir)
                return channel.sent

        sent = asyncio.run(scenario())
        filenames = [file.filename for message in sent for file in message["files"]]
        self.assertEqual(filenames, ["report.png"])
        contents = [message["content"] or "" for message in sent]
        self.assertFalse(any("읽을 수 없습니다" in content for content in contents))

    def test_send_outputs_non_utf8_meta_falls_back_to_manifest_files(self):
        # Residual (a): Path.read_text(encoding="utf-8") raises
        # UnicodeDecodeError for non-UTF-8 bytes, which is NOT a
        # json.JSONDecodeError — must not escape send_outputs and must not
        # block the manifest.json fallback.
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                job_dir = Path(tmp)
                (job_dir / "report.png").write_bytes(b"png-bytes")
                (job_dir / "meta.json").write_bytes(b"\xff\xfe\x00bad-utf8")
                (job_dir / "manifest.json").write_text(
                    json.dumps({"files": [{"path": "report.png", "label": "report"}]}, ensure_ascii=False),
                    encoding="utf-8",
                )
                channel = FakeChannel()
                await send_outputs(channel, job_dir)  # must not raise
                return channel.sent

        sent = asyncio.run(scenario())
        filenames = [file.filename for message in sent for file in message["files"]]
        self.assertEqual(filenames, ["report.png"])

    def test_send_outputs_non_utf8_manifest_reports_error_without_raising(self):
        # Same fix applies on the manifest.json read path (shared helper).
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                job_dir = Path(tmp)
                (job_dir / "manifest.json").write_bytes(b"\xff\xfe\x00bad-utf8")
                channel = FakeChannel()
                await send_outputs(channel, job_dir, body_text="본문")  # must not raise
                return channel.sent

        sent = asyncio.run(scenario())
        self.assertEqual(sent[0]["content"], "본문")
        self.assertTrue(any("manifest.json" in (message["content"] or "") for message in sent))

    def test_unreadable_manifest_reports_the_filename_without_leaking_its_path(self):
        # S3 / issue #26: an OSError's str embeds the absolute filesystem path
        # ("[Errno 21] Is a directory: '/Users/.../runs/job-x/manifest.json'").
        # Widening the except clause to OSError put that string on its way to
        # a Discord channel. The user gets the filename; the path stays in the
        # log.
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                job_dir = Path(tmp) / "job-secret-path"
                job_dir.mkdir()
                # A directory where a file is expected: read_text raises
                # IsADirectoryError, an OSError carrying the full path.
                (job_dir / "manifest.json").mkdir()

                channel = FakeChannel()
                with self.assertLogs("src.outputs", level="WARNING"):
                    await send_outputs(channel, job_dir, body_text="본문")
                return channel.sent, str(job_dir)

        sent, job_path = asyncio.run(scenario())
        contents = [message["content"] or "" for message in sent]

        self.assertIn("본문", contents)
        self.assertTrue(any("manifest.json" in content for content in contents))
        for content in contents:
            self.assertNotIn(job_path, content)
            self.assertNotIn(tempfile.gettempdir(), content)

    def test_unreadable_meta_json_path_is_not_leaked_either(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                job_dir = Path(tmp) / "job-secret-path"
                job_dir.mkdir()
                (job_dir / "meta.json").mkdir()

                channel = FakeChannel()
                with self.assertLogs("src.outputs", level="WARNING"):
                    await send_outputs(channel, job_dir, body_text="본문")
                return channel.sent, str(job_dir)

        sent, job_path = asyncio.run(scenario())
        for message in sent:
            self.assertNotIn(job_path, message["content"] or "")

    def test_malformed_json_detail_is_still_shown_to_the_user(self):
        # Only the OSError branch is redacted: a JSONDecodeError's message
        # names no path and tells the user what is actually wrong.
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                job_dir = Path(tmp)
                (job_dir / "manifest.json").write_text("{bad json", encoding="utf-8")
                channel = FakeChannel()
                await send_outputs(channel, job_dir, body_text="본문")
                return channel.sent

        sent = asyncio.run(scenario())
        contents = [message["content"] or "" for message in sent]
        self.assertTrue(any("manifest.json을 읽을 수 없습니다:" in c for c in contents))

    # --- m3 regression: an "empty result" must be judged by files actually
    # transmitted, not by how many entries were merely listed. ---

    def test_send_outputs_entries_pointing_at_missing_files_still_notify(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                job_dir = Path(tmp)
                # No output.md, no body_text, and the manifest lists a file
                # that was never actually written to disk.
                (job_dir / "manifest.json").write_text(
                    json.dumps({"files": [{"path": "ghost.png", "label": "ghost"}]}, ensure_ascii=False),
                    encoding="utf-8",
                )
                channel = FakeChannel()
                await send_outputs(channel, job_dir)
                return channel.sent

        sent = asyncio.run(scenario())
        self.assertFalse(any(message["files"] for message in sent))
        self.assertTrue(len(sent) >= 1)
        self.assertTrue(any((message["content"] or "").strip() for message in sent))

    def test_send_outputs_meta_lists_only_missing_files_falls_back_to_manifest(self):
        # Residual (b): meta.json is non-empty but none of its listed files
        # exist on disk — must not shadow manifest.json's real attachment.
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                job_dir = Path(tmp)
                (job_dir / "report.png").write_bytes(b"png-bytes")
                (job_dir / "meta.json").write_text(
                    json.dumps({"files": [{"path": "ghost.png", "label": "ghost"}]}, ensure_ascii=False),
                    encoding="utf-8",
                )
                (job_dir / "manifest.json").write_text(
                    json.dumps({"files": [{"path": "report.png", "label": "report"}]}, ensure_ascii=False),
                    encoding="utf-8",
                )
                channel = FakeChannel()
                await send_outputs(channel, job_dir)
                return channel.sent

        sent = asyncio.run(scenario())
        filenames = [file.filename for message in sent for file in message["files"]]
        self.assertEqual(filenames, ["report.png"])

    def test_send_outputs_rejects_meta_json_path_traversal(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                job_dir = Path(tmp) / "job"
                job_dir.mkdir()
                (Path(tmp) / "secret.txt").write_text("secret", encoding="utf-8")
                (job_dir / "meta.json").write_text(
                    json.dumps({"files": [{"path": "../secret.txt", "label": "secret"}]}, ensure_ascii=False),
                    encoding="utf-8",
                )
                channel = FakeChannel()
                await send_outputs(channel, job_dir, body_text="본문")
                return channel.sent

        sent = asyncio.run(scenario())
        self.assertFalse(any(message["files"] for message in sent))
        self.assertTrue(any("무시" in (message["content"] or "") for message in sent))

    def test_send_outputs_rejects_absolute_meta_json_path(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                job_dir = Path(tmp) / "job"
                job_dir.mkdir()
                outside = Path(tmp) / "outside.txt"
                outside.write_text("outside", encoding="utf-8")
                (job_dir / "meta.json").write_text(
                    json.dumps({"files": [{"path": str(outside), "label": "abs"}]}, ensure_ascii=False),
                    encoding="utf-8",
                )
                channel = FakeChannel()
                await send_outputs(channel, job_dir, body_text="본문")
                return channel.sent

        sent = asyncio.run(scenario())
        self.assertFalse(any(message["files"] for message in sent))
        self.assertTrue(any("무시" in (message["content"] or "") for message in sent))

    def test_send_outputs_rejects_absolute_meta_json_path_without_leaking_it(self):
        # Issue #26: entry.get("path") is echoed back in the "무시했습니다"
        # notice. When the rejected path was itself a real local absolute
        # path (as here), that echo must not leak it.
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                job_dir = Path(tmp) / "job-secret-path"
                job_dir.mkdir()
                outside = Path(tmp) / "outside-secret.txt"
                outside.write_text("outside", encoding="utf-8")
                (job_dir / "meta.json").write_text(
                    json.dumps({"files": [{"path": str(outside), "label": "abs"}]}, ensure_ascii=False),
                    encoding="utf-8",
                )
                channel = FakeChannel()
                await send_outputs(channel, job_dir, body_text="본문")
                return channel.sent, str(outside)

        sent, outside_path = asyncio.run(scenario())
        contents = [message["content"] or "" for message in sent]
        ignored_message = next(c for c in contents if "무시" in c)
        self.assertNotIn(outside_path, ignored_message)

    # --- Issue #27 (item 1): the over-limit message must be derived from
    # MAX_ATTACH_BYTES, not a hardcoded "25MB" that can drift from it. ---

    def test_send_outputs_attachment_over_limit_message_derives_from_constant(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                job_dir = Path(tmp)
                (job_dir / "big.bin").write_bytes(b"x" * (4 * 1024 * 1024))
                (job_dir / "manifest.json").write_text(
                    json.dumps({"files": [{"path": "big.bin", "label": "big"}]}, ensure_ascii=False),
                    encoding="utf-8",
                )
                channel = FakeChannel()
                with mock.patch.object(outputs, "MAX_ATTACH_BYTES", 3 * 1024 * 1024):
                    await send_outputs(channel, job_dir)
                return channel.sent

        sent = asyncio.run(scenario())
        contents = [message["content"] or "" for message in sent]
        self.assertTrue(any("3MB를 초과" in c for c in contents))
        self.assertFalse(any("25MB" in c for c in contents))

    # --- Issue #26: the over-limit notice also echoes the attachment's
    # absolute path; it must go out redacted. ---

    def test_send_outputs_attachment_over_limit_message_does_not_leak_absolute_path(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                job_dir = Path(tmp) / "job-secret-path"
                job_dir.mkdir()
                (job_dir / "big.bin").write_bytes(b"x" * (4 * 1024 * 1024))
                (job_dir / "manifest.json").write_text(
                    json.dumps({"files": [{"path": "big.bin", "label": "big"}]}, ensure_ascii=False),
                    encoding="utf-8",
                )
                channel = FakeChannel()
                with mock.patch.object(outputs, "MAX_ATTACH_BYTES", 3 * 1024 * 1024):
                    await send_outputs(channel, job_dir)
                return channel.sent, str(job_dir)

        sent, job_path = asyncio.run(scenario())
        contents = [message["content"] or "" for message in sent]
        over_limit_message = next(c for c in contents if "초과" in c)
        self.assertNotIn(job_path, over_limit_message)
        self.assertIn("big.bin", over_limit_message)

    # --- Issue #4: long-answer delivery must not tail-latency behind
    # Discord's per-channel rate limit. ---

    def test_send_outputs_switches_to_attachment_when_too_many_chunks(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                job_dir = Path(tmp)
                # Long enough that pagination at DISCORD_CHUNK_LIMIT would
                # take well over OUTPUT_INLINE_MAX_CHUNKS (default 3).
                long_text = "안녕하세요, 성능 개선 테스트입니다. " * 2000
                channel = FakeChannel()
                await send_outputs(channel, job_dir, body_text=long_text)
                return channel.sent, long_text

        sent, long_text = asyncio.run(scenario())

        # A single send: one message carrying a short preview plus the
        # full body as a one-shot attachment (no rate-limit queueing).
        self.assertEqual(len(sent), 1)
        message = sent[0]

        filenames = [file.filename for file in message["files"]]
        self.assertEqual(filenames, ["response.md"])

        attached_text = message["file_bytes"][0].decode("utf-8")
        self.assertEqual(attached_text, long_text)

        self.assertIsNotNone(message["content"])
        self.assertLess(len(message["content"]), len(long_text))
        self.assertLessEqual(len(message["content"]), outputs.DISCORD_CHUNK_LIMIT)
        self.assertIn("response.md", message["content"])

    def test_send_outputs_still_paginates_inline_within_chunk_budget(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                job_dir = Path(tmp)
                # Two chunks' worth of body — within OUTPUT_INLINE_MAX_CHUNKS
                # (default 3) — must still be sent inline, not attached.
                short_text = "가나다라마바사아자차. " * 250
                channel = FakeChannel()
                await send_outputs(channel, job_dir, body_text=short_text)
                return channel.sent, short_text

        sent, short_text = asyncio.run(scenario())

        self.assertGreater(len(sent), 1)
        for message in sent:
            self.assertEqual(message["files"], [])
        reassembled = "".join(message["content"] for message in sent)
        self.assertEqual(reassembled, short_text)

    def test_send_outputs_respects_output_inline_max_chunks_override(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                job_dir = Path(tmp)
                short_text = "가나다라마바사아자차. " * 250
                channel = FakeChannel()
                with mock.patch.dict(os.environ, {"OUTPUT_INLINE_MAX_CHUNKS": "1"}):
                    await send_outputs(channel, job_dir, body_text=short_text)
                return channel.sent

        sent = asyncio.run(scenario())
        # With the cap lowered to 1, the same body that paginated inline
        # above must now switch to the attachment path.
        self.assertEqual(len(sent), 1)
        filenames = [file.filename for file in sent[0]["files"]]
        self.assertEqual(filenames, ["response.md"])

    def test_chunk_does_not_split_a_code_fence_across_chunks(self):
        lines = ["```python"] + [f"line_{i} = {i}" for i in range(60)] + ["```", "끝."]
        text = "\n".join(lines) + "\n"

        chunks = outputs._chunk(text, 80)

        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 80 + len("```\n"))
            fence_lines = [ln for ln in chunk.splitlines() if outputs._FENCE_LINE_RE.match(ln)]
            # Every chunk must open and close its own fences — a fence must
            # never end a chunk without being closed, nor start one without
            # having been (re)opened.
            self.assertEqual(len(fence_lines) % 2, 0)

        # The original code content survives, split-fence bookkeeping aside.
        for i in range(60):
            self.assertTrue(any(f"line_{i} = {i}" in chunk for chunk in chunks))
        self.assertTrue(any("끝." in chunk for chunk in chunks))

    def test_chunk_never_exceeds_the_requested_limit_for_plain_text(self):
        text = "가" * 5000
        chunks = outputs._chunk(text, 500)
        self.assertTrue(all(len(chunk) <= 500 for chunk in chunks))
        self.assertEqual("".join(chunks), text)


class SvgRendererFallbackTests(unittest.TestCase):
    # --- Issue #20: qlmanage-only preview rendering leaves Linux/Windows
    # hosts with no inline PNG preview, ever. _render_svg_preview must try
    # the fallback chain in order and stop at the first success. ---

    def _write_svg(self, job_dir: Path) -> Path:
        svg_path = job_dir / "art.svg"
        svg_path.write_text(
            "<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"10\" height=\"10\"></svg>\n",
            encoding="utf-8",
        )
        return svg_path

    def test_falls_back_through_renderers_in_order_and_stops_at_first_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_dir = Path(tmp)
            svg_path = self._write_svg(job_dir)

            which_calls = []

            def fake_which(name):
                which_calls.append(name)
                # Only rsvg-convert is "installed" in this scenario.
                return "/usr/bin/rsvg-convert" if name == "rsvg-convert" else None

            run_calls = []

            def fake_run(args, **kwargs):
                run_calls.append(args)
                # rsvg-convert's invocation is [exe, -w, size, -o, preview, svg]
                preview_path = Path(args[-2])
                preview_path.write_bytes(b"png")
                return subprocess.CompletedProcess(args, 0)

            with mock.patch("src.outputs.shutil.which", fake_which), \
                    mock.patch("src.outputs.subprocess.run", fake_run):
                result = outputs._render_svg_preview(svg_path)

            self.assertEqual(result, job_dir / ".discord-previews" / "art.svg.png")
            # qlmanage was probed (and skipped, not installed); inkscape and
            # cairosvg were never even probed once rsvg-convert succeeded.
            self.assertEqual(which_calls, ["qlmanage", "rsvg-convert"])
            self.assertEqual(len(run_calls), 1)
            self.assertIn("rsvg-convert", run_calls[0][0])

    def test_uses_cairosvg_when_no_cli_renderer_is_installed(self):
        # cairosvg must work as a graceful, optional-import fallback — never
        # a hard dependency (see the team-lead brief for #20).
        import types

        with tempfile.TemporaryDirectory() as tmp:
            job_dir = Path(tmp)
            svg_path = self._write_svg(job_dir)

            calls = []
            fake_cairosvg = types.ModuleType("cairosvg")

            def fake_svg2png(url, write_to, output_width=None):
                calls.append((url, write_to))
                Path(write_to).write_bytes(b"png")

            fake_cairosvg.svg2png = fake_svg2png

            with mock.patch("src.outputs.shutil.which", return_value=None), \
                    mock.patch.dict("sys.modules", {"cairosvg": fake_cairosvg}):
                result = outputs._render_svg_preview(svg_path)

            self.assertEqual(result, job_dir / ".discord-previews" / "art.svg.png")
            self.assertEqual(len(calls), 1)

    def test_returns_none_and_logs_when_no_renderer_is_available(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_dir = Path(tmp)
            svg_path = self._write_svg(job_dir)

            # Force the optional cairosvg import to fail regardless of
            # whether the real package happens to be installed in this env.
            with mock.patch("src.outputs.shutil.which", return_value=None), \
                    mock.patch.dict("sys.modules", {"cairosvg": None}), \
                    self.assertLogs("src.outputs", level="WARNING") as logs:
                result = outputs._render_svg_preview(svg_path)

            self.assertIsNone(result)
            self.assertTrue(any("art.svg" in message for message in logs.output))


class AttachmentPathsForTests(unittest.TestCase):
    def test_render_svg_preview_runs_off_the_event_loop_thread(self):
        # Non-blocking guarantee: qlmanage is invoked via asyncio.to_thread,
        # so it must run on a worker thread, never on the event loop thread.
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                job_dir = Path(tmp)
                svg_path = job_dir / "art.svg"
                svg_path.write_text(
                    "<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"10\" height=\"10\"></svg>\n",
                    encoding="utf-8",
                )

                captured = {}

                def fake_run(args, **kwargs):
                    captured["thread"] = threading.current_thread()
                    return subprocess.CompletedProcess(args, 0)

                with mock.patch("src.outputs.shutil.which", return_value="/usr/bin/qlmanage"), \
                        mock.patch("src.outputs.subprocess.run", fake_run):
                    await outputs._attachment_paths_for(svg_path)

                return captured.get("thread")

        worker_thread = asyncio.run(scenario())
        self.assertIsNotNone(worker_thread)
        self.assertNotEqual(worker_thread, threading.main_thread())

    def test_attachment_paths_for_does_not_block_concurrent_tasks(self):
        # A slow qlmanage call must not stall other coroutines scheduled on
        # the same event loop.
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                job_dir = Path(tmp)
                svg_path = job_dir / "art.svg"
                svg_path.write_text(
                    "<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"10\" height=\"10\"></svg>\n",
                    encoding="utf-8",
                )

                def slow_run(args, **kwargs):
                    import time

                    time.sleep(0.2)
                    return subprocess.CompletedProcess(args, 0)

                ticks = 0

                async def ticker():
                    nonlocal ticks
                    for _ in range(15):
                        await asyncio.sleep(0.01)
                        ticks += 1

                with mock.patch("src.outputs.shutil.which", return_value="/usr/bin/qlmanage"), \
                        mock.patch("src.outputs.subprocess.run", slow_run):
                    await asyncio.gather(outputs._attachment_paths_for(svg_path), ticker())

                return ticks

        ticks = asyncio.run(scenario())
        # If _render_svg_preview were called synchronously on the event loop
        # thread, the ticker would be starved and ticks would stay near 0.
        self.assertGreater(ticks, 5)
