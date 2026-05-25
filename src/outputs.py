import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import discord

MAX_ATTACH_BYTES = 24 * 1024 * 1024
SVG_PREVIEW_SIZE = 1400
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".avif"}
SVG_EXTENSION = ".svg"

SVG_FENCE_RE = re.compile(
    r"(?P<fence>`{3,}|~{3,})[ \t]*(?P<lang>svg|xml)?[^\n]*\n(?P<body>.*?)(?:\n(?P=fence))",
    re.IGNORECASE | re.DOTALL,
)


async def send_outputs(
    channel: discord.abc.Messageable,
    job_dir: Path,
    *,
    warn_missing_manifest: bool = True,
):
    attachment_entries = []
    output_md = job_dir / "output.md"
    if output_md.exists():
        text = output_md.read_text(encoding="utf-8")
        text, inline_entries = _extract_inline_svg_blocks(job_dir, text)
        attachment_entries.extend(inline_entries)
        for chunk in _chunk(text, 1900):
            await channel.send(chunk)

    manifest_path = job_dir / "manifest.json"
    if not manifest_path.exists():
        await _send_attachment_entries(channel, job_dir, attachment_entries)
        if warn_missing_manifest and not attachment_entries:
            await channel.send("(manifest.json이 없습니다. 산출 파일을 확인할 수 없음.)")
        return

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        await _send_attachment_entries(channel, job_dir, attachment_entries)
        await channel.send(f"manifest.json을 읽을 수 없습니다: {exc}")
        return

    if not isinstance(manifest, dict):
        await _send_attachment_entries(channel, job_dir, attachment_entries)
        await channel.send("manifest.json 형식이 올바르지 않습니다.")
        return

    entries = manifest.get("files", [])
    if not isinstance(entries, list):
        await _send_attachment_entries(channel, job_dir, attachment_entries)
        await channel.send("manifest.json의 files 항목이 배열이 아닙니다.")
        return

    attachment_entries.extend(entries)
    await _send_attachment_entries(channel, job_dir, attachment_entries)


def _extract_inline_svg_blocks(job_dir: Path, text: str) -> tuple[str, list[dict[str, str]]]:
    entries = []
    inline_dir = job_dir / "inline-assets"

    def replace(match: re.Match[str]) -> str:
        body = match.group("body").strip()
        if not _looks_like_svg(body):
            return match.group(0)

        inline_dir.mkdir(parents=True, exist_ok=True)
        filename = f"inline-svg-{len(entries) + 1}.svg"
        path = inline_dir / filename
        path.write_text(body + "\n", encoding="utf-8")
        entries.append(
            {
                "path": str(path.relative_to(job_dir)),
                "label": filename,
            }
        )
        return f"🖼️ SVG 미리보기 첨부: {filename}"

    return SVG_FENCE_RE.sub(replace, text), entries


def _looks_like_svg(text: str) -> bool:
    stripped = text.lstrip()
    return stripped.startswith("<svg") or (stripped.startswith("<?xml") and "<svg" in stripped[:500])


async def _send_attachment_entries(channel: discord.abc.Messageable, job_dir: Path, entries: list[Any]):
    files = []
    sent_paths = set()

    for entry in entries:
        if not isinstance(entry, dict):
            await channel.send("manifest 파일 항목이 올바르지 않아 무시했습니다.")
            continue

        p = _safe_manifest_path(job_dir, entry.get("path"))
        if p is None:
            await channel.send(f"manifest 경로를 무시했습니다: {entry.get('path')}")
            continue
        if not p.exists():
            continue

        for attach_path in _attachment_paths_for(p):
            resolved = attach_path.resolve()
            if resolved in sent_paths:
                continue
            sent_paths.add(resolved)

            if attach_path.stat().st_size > MAX_ATTACH_BYTES:
                label = entry.get("label") or attach_path.name
                await channel.send(f"파일 {label}이 25MB를 초과해 첨부 불가. 경로: {attach_path}")
                continue

            files.append(discord.File(attach_path, filename=attach_path.name))
            if len(files) >= 10:
                await channel.send(files=files)
                files = []

    if files:
        await channel.send(files=files)


def _attachment_paths_for(path: Path) -> list[Path]:
    suffix = path.suffix.lower()
    if suffix == SVG_EXTENSION:
        preview = _render_svg_preview(path)
        return [p for p in [preview, path] if p is not None]
    if suffix in IMAGE_EXTENSIONS:
        return [path]
    return [path]


def _render_svg_preview(svg_path: Path) -> Path | None:
    qlmanage = shutil.which("qlmanage")
    if not qlmanage:
        return None

    preview_dir = svg_path.parent / ".discord-previews"
    preview_dir.mkdir(parents=True, exist_ok=True)
    preview_path = preview_dir / f"{svg_path.name}.png"
    if preview_path.exists() and preview_path.stat().st_mtime >= svg_path.stat().st_mtime:
        return preview_path

    preview_path.unlink(missing_ok=True)
    try:
        result = subprocess.run(
            [qlmanage, "-t", "-s", str(SVG_PREVIEW_SIZE), "-o", str(preview_dir), str(svg_path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None

    if result.returncode != 0 or not preview_path.exists():
        return None
    return preview_path


def _chunk(text: str, n: int):
    for i in range(0, len(text), n):
        yield text[i:i + n]


def _safe_manifest_path(job_dir: Path, raw_path: Any) -> Path | None:
    if not isinstance(raw_path, str) or not raw_path:
        return None

    rel_path = Path(raw_path)
    if rel_path.is_absolute():
        return None

    root = job_dir.resolve()
    candidate = (root / rel_path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate
