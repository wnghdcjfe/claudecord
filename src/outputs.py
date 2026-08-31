import asyncio
import io
import json
import logging
import os
import re
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import discord

from src.errors import redact_paths

logger = logging.getLogger(__name__)

MAX_ATTACH_BYTES = 24 * 1024 * 1024
SVG_PREVIEW_SIZE = 1400
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".avif"}
SVG_EXTENSION = ".svg"

SVG_FENCE_RE = re.compile(
    r"(?P<fence>`{3,}|~{3,})[ \t]*(?P<lang>svg|xml)?[^\n]*\n(?P<body>.*?)(?:\n(?P=fence))",
    re.IGNORECASE | re.DOTALL,
)

# Discord's hard per-message character cap. DISCORD_CHUNK_LIMIT (below) stays
# under this with headroom rather than filling it exactly — see _chunk.
DISCORD_MESSAGE_LIMIT = 2000
# Headroom below DISCORD_MESSAGE_LIMIT for the closing/reopening fence line
# _chunk may need to inject at a chunk boundary, and for length-counting
# mismatches (e.g. astral-plane emoji count as 2 UTF-16 units to Discord but
# 1 Python codepoint here). Deliberately not "2000 exactly" (see the
# perf handoff's Rejected section).
DISCORD_CHUNK_LIMIT = 1950

_FENCE_LINE_RE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})[^\n]*$")

# Default number of chunks send_outputs will paginate inline before switching
# to a single .md attachment; overridable via OUTPUT_INLINE_MAX_CHUNKS.
OUTPUT_INLINE_MAX_CHUNKS = 3

_PREVIEW_SUFFIX = "\n\n… (본문이 길어 `response.md` 파일로 전체를 첨부했습니다)"


async def send_outputs(
    channel: discord.abc.Messageable,
    job_dir: Path,
    *,
    body_text: str | None = None,
    warn_missing_manifest: bool = True,
):
    """Send a job's answer body and attachments to a Discord channel.

    Body precedence (contract v2): ``output.md`` wins when it exists and is
    non-empty (long/structured answers, backward compatible); otherwise
    ``body_text`` — normally the model's final streamed text — is used. Both
    go through the same inline-SVG extraction and ``DISCORD_CHUNK_LIMIT``
    chunking, with code fences kept intact across chunk boundaries (see
    ``_chunk``).

    Pagination vs. attachment: sending each chunk as its own message queues
    up against Discord's per-channel rate limit (5 sends / 5s) and tail-
    latencies the rest of a long reply. If the body would take more than
    ``OUTPUT_INLINE_MAX_CHUNKS`` (default 3, env-overridable) chunks, it is
    not paginated — instead the whole body is sent once as a single
    ``response.md`` attachment (one send, no rate-limit queueing) alongside
    a short preview of the start of the answer. Short bodies are unaffected
    and still paginate inline as before.

    Attachment precedence is *key-level*, not file-level: ``meta.json``'s
    ``files`` key wins when it resolves to a non-empty list *and* at least
    one listed file actually exists on disk; otherwise the legacy
    ``manifest.json``'s ``files`` is used. This matters during the
    contract-v2 transition where a job may write a ``meta.json`` with only a
    ``workdir`` key (a perfectly normal, artifact-less answer), or one that
    lists files that were never written, alongside a leftover/older
    ``manifest.json`` that still lists real files — picking ``meta.json``
    wholesale would silently drop those attachments. The same fallback also
    covers a malformed/unreadable ``meta.json`` (bad JSON, non-UTF-8 bytes):
    it does not block the ``manifest.json`` attachments from going out. See
    ``_resolve_manifest_entries`` for the exact rule.

    Neither file existing is the normal case (no artifacts produced) and is
    not warned about; only a genuinely empty result — no body text *and* no
    file actually transmitted — triggers a notice, and only when
    ``warn_missing_manifest`` is true. "Actually transmitted" (not "listed in
    the manifest") matters: a manifest entry pointing at a file that no
    longer exists on disk is skipped silently by ``_send_attachment_entries``,
    so counting listed entries would wrongly suppress the notice and leave
    the user with neither a body nor an attachment nor any explanation.
    """
    attachment_entries: list[Any] = []

    body_source = await _resolve_body_text(job_dir, body_text)
    if body_source:
        text, inline_entries = _extract_inline_svg_blocks(job_dir, body_source)
        attachment_entries.extend(inline_entries)
        await _send_body_text(channel, text)

    manifest_entries, manifest_error = await _resolve_manifest_entries(job_dir)
    attachment_entries.extend(manifest_entries)

    sent_file_count = await _send_attachment_entries(channel, job_dir, attachment_entries)

    if manifest_error:
        # Both meta.json and manifest.json failed to yield usable entries,
        # and at least one of them existed but was malformed/invalid.
        await channel.send(manifest_error)
        return

    await _maybe_send_empty_notice(channel, body_source, sent_file_count, warn_missing_manifest)


async def _resolve_body_text(job_dir: Path, body_text: str | None) -> str | None:
    """output.md wins when present, readable, and non-empty; else fall back
    to body_text. A read failure (non-UTF-8 bytes, OS error) is treated the
    same as "no usable output.md" — it must not escape and take the whole
    body (and, downstream, the attachments) down with it. Same fallback
    posture as _load_entries_from for meta.json/manifest.json.

    The actual file check+read is blocking disk I/O on the request path, so
    it runs via asyncio.to_thread (see _read_output_md).
    """
    text = await asyncio.to_thread(_read_output_md, job_dir)
    if text:
        return text
    return body_text or None


def _read_output_md(job_dir: Path) -> str | None:
    """Blocking (exists check + read). Must only be invoked via
    asyncio.to_thread — see _resolve_body_text."""
    output_md = job_dir / "output.md"
    if output_md.exists():
        try:
            text = output_md.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            text = None
        if text and text.strip():
            return text
    return None


async def _load_entries_from(path: Path) -> tuple[list[Any] | None, str | None]:
    """Load the ``files`` list from one bookkeeping JSON file.

    Returns ``(entries, error)``:
    - file doesn't exist: ``(None, None)``
    - file exists but is malformed/wrong-shaped/unreadable: ``(None, <message>)``
    - file parses cleanly: ``(entries, None)`` where entries may be ``[]``

    Any failure to read or parse the file (bad JSON, non-UTF-8 bytes, an OS
    error) is caught and turned into an error string rather than left to
    propagate — a broken bookkeeping file must never take down send_outputs
    and, with it, the attachments/notices that still need to go out.

    The actual file check+read+parse is blocking disk I/O on the request
    path, so it runs via asyncio.to_thread (see _read_entries_from).
    """
    return await asyncio.to_thread(_read_entries_from, path)


def _read_entries_from(path: Path) -> tuple[list[Any] | None, str | None]:
    """Blocking (exists check + read + json parse). Must only be invoked via
    asyncio.to_thread — see _load_entries_from."""
    if not path.exists():
        return None, None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        # These two describe the *content* ("Expecting value: line 1 column
        # 1", "'utf-8' codec can't decode byte 0xff") and name no path, so
        # they are safe to show and actually useful to the user.
        return None, f"{path.name}을 읽을 수 없습니다: {exc}"
    except OSError:
        # An OSError's str carries the absolute filesystem path
        # ("[Errno 21] Is a directory: '/Users/.../runs/job-x/meta.json'"),
        # which must not be published to a Discord channel. The user gets the
        # bare filename; the operator gets the detail in the log.
        logger.warning("Failed to read job bookkeeping file %s", path, exc_info=True)
        return None, f"{path.name}을 읽을 수 없습니다."
    if not isinstance(data, dict):
        return None, f"{path.name} 형식이 올바르지 않습니다."
    entries = data.get("files", [])
    if not isinstance(entries, list):
        return None, f"{path.name}의 files 항목이 배열이 아닙니다."
    return entries, None


def _has_any_existing_file(job_dir: Path, entries: list[Any]) -> bool:
    """True if at least one entry resolves (via the same path-safety check
    used to actually send files) to a file that exists on disk."""
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        p = _safe_manifest_path(job_dir, entry.get("path"))
        if p is not None and p.exists():
            return True
    return False


async def _resolve_manifest_entries(job_dir: Path) -> tuple[list[Any], str | None]:
    """Key-level fallback: meta.json's files win when non-empty *and* at
    least one listed file actually exists; otherwise manifest.json's files
    are used. A meta.json parse/format/read error does not block the
    manifest.json fallback — meta.json is optional bookkeeping, not a hard
    dependency (contract v2).

    The existence check guards a narrow transition-period case: meta.json
    lists files that were never written (e.g. a stale/short-lived path),
    while manifest.json still has the real attachments from the same job.
    Picking meta.json purely because it is non-empty would silently drop
    those — the same silent-loss failure mode this fallback exists to fix.
    When meta.json's own entries are malformed/unsafe (not merely
    nonexistent) and no manifest.json fallback is available, meta_entries is
    still returned so the existing per-entry error reporting (invalid shape,
    path traversal) in _send_attachment_entries continues to fire.
    """
    meta_entries, meta_error = await _load_entries_from(job_dir / "meta.json")
    if meta_entries and await asyncio.to_thread(_has_any_existing_file, job_dir, meta_entries):
        return meta_entries, None

    manifest_entries, manifest_error = await _load_entries_from(job_dir / "manifest.json")
    if manifest_entries:
        return manifest_entries, None

    if meta_entries:
        return meta_entries, None

    return [], meta_error or manifest_error


async def _maybe_send_empty_notice(
    channel: discord.abc.Messageable,
    body_source: str | None,
    sent_file_count: int,
    warn_missing_manifest: bool,
) -> None:
    if body_source or sent_file_count > 0:
        return
    if not warn_missing_manifest:
        return
    await channel.send("(전달할 결과가 없습니다.)")


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


async def _send_attachment_entries(channel: discord.abc.Messageable, job_dir: Path, entries: list[Any]) -> int:
    """Send attachments described by ``entries``. Returns the count of files
    actually transmitted (as opposed to merely listed) — used by
    ``_maybe_send_empty_notice`` to tell a real empty result from one where
    entries were listed but pointed at files that no longer exist."""
    files = []
    sent_paths = set()
    sent_count = 0

    for entry in entries:
        if not isinstance(entry, dict):
            await channel.send("manifest 파일 항목이 올바르지 않아 무시했습니다.")
            continue

        p = _safe_manifest_path(job_dir, entry.get("path"))
        if p is None:
            # entry.get("path") is job-controlled text and, for an absolute
            # or traversal attempt, may itself be a real local path (#26) --
            # redact before it reaches Discord.
            await channel.send(f"manifest 경로를 무시했습니다: {redact_paths(str(entry.get('path')))}")
            continue
        if not p.exists():
            continue

        for attach_path in await _attachment_paths_for(p):
            resolved = attach_path.resolve()
            if resolved in sent_paths:
                continue
            sent_paths.add(resolved)

            if attach_path.stat().st_size > MAX_ATTACH_BYTES:
                label = entry.get("label") or attach_path.name
                # Derived from MAX_ATTACH_BYTES so this text can never drift
                # from the actual limit again (#27); the path is redacted
                # since it's a real local absolute path (#26).
                limit_mb = MAX_ATTACH_BYTES // (1024 * 1024)
                await channel.send(
                    f"파일 {label}이 {limit_mb}MB를 초과해 첨부 불가. 경로: {redact_paths(str(attach_path))}"
                )
                continue

            # discord.File(path) opens the file synchronously in its
            # constructor — blocking disk I/O — so it runs off the event
            # loop thread.
            file = await asyncio.to_thread(discord.File, attach_path, filename=attach_path.name)
            files.append(file)
            sent_count += 1
            if len(files) >= 10:
                await channel.send(files=files)
                files = []

    if files:
        await channel.send(files=files)

    return sent_count


async def _attachment_paths_for(path: Path) -> list[Path]:
    suffix = path.suffix.lower()
    if suffix == SVG_EXTENSION:
        # SVG preview rendering shells out to an external tool (or the
        # optional cairosvg package) and can block for a while — run it off
        # the event loop thread so a single SVG preview doesn't stall the
        # whole bot.
        preview = await asyncio.to_thread(_render_svg_preview, path)
        return [p for p in [preview, path] if p is not None]
    if suffix in IMAGE_EXTENSIONS:
        return [path]
    return [path]


def _run_qlmanage(svg_path: Path, preview_path: Path) -> bool:
    """macOS Quick Look thumbnailer. Writes ``<svg name>.png`` into the
    output dir, which is exactly how ``preview_path`` is named below."""
    exe = shutil.which("qlmanage")
    if not exe:
        return False
    try:
        result = subprocess.run(
            [exe, "-t", "-s", str(SVG_PREVIEW_SIZE), "-o", str(preview_path.parent), str(svg_path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and preview_path.exists()


def _run_rsvg_convert(svg_path: Path, preview_path: Path) -> bool:
    """librsvg's CLI converter — common on Linux, unlike qlmanage (#20)."""
    exe = shutil.which("rsvg-convert")
    if not exe:
        return False
    try:
        result = subprocess.run(
            [exe, "-w", str(SVG_PREVIEW_SIZE), "-o", str(preview_path), str(svg_path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and preview_path.exists()


def _run_inkscape(svg_path: Path, preview_path: Path) -> bool:
    """Inkscape's CLI export — heavier than rsvg-convert but widely
    available cross-platform, including Windows, when installed (#20)."""
    exe = shutil.which("inkscape")
    if not exe:
        return False
    try:
        result = subprocess.run(
            [
                exe,
                "--export-type=png",
                f"--export-filename={preview_path}",
                "-w",
                str(SVG_PREVIEW_SIZE),
                str(svg_path),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and preview_path.exists()


def _run_cairosvg(svg_path: Path, preview_path: Path) -> bool:
    """Pure-Python last resort for hosts with no rendering CLI on PATH at
    all. Imported lazily and optionally — cairosvg must not become a hard
    runtime dependency just to send Discord messages (#20)."""
    try:
        import cairosvg
    except Exception:
        # Not just ImportError: cairosvg pulls in cairocffi, which dlopens
        # libcairo at import time and raises OSError when the system library
        # is missing -- the exact state you land in after `pip install
        # cairosvg` with no system cairo. Letting that escape kills the whole
        # send_outputs call, so the job reports success with no attachments.
        logger.debug("cairosvg is unavailable", exc_info=True)
        return False
    try:
        cairosvg.svg2png(url=str(svg_path), write_to=str(preview_path), output_width=SVG_PREVIEW_SIZE)
    except Exception:
        # cairosvg surfaces a mix of its own errors and lxml/cairo errors for
        # malformed input; any of them just means "this renderer can't do it."
        logger.debug("cairosvg failed to render %s", svg_path.name, exc_info=True)
        return False
    return preview_path.exists()


# Tried in this order; the first renderer that produces preview_path wins.
# qlmanage stays first since it needs no install on the platform this bot
# was originally built for; the rest cover Linux/Windows hosts (#20).
_SVG_RENDERERS: list[tuple[str, Callable[[Path, Path], bool]]] = [
    ("qlmanage", _run_qlmanage),
    ("rsvg-convert", _run_rsvg_convert),
    ("inkscape", _run_inkscape),
    ("cairosvg", _run_cairosvg),
]


def _render_svg_preview(svg_path: Path) -> Path | None:
    """Blocking. Must only be invoked via asyncio.to_thread (see
    _attachment_paths_for). Tries each candidate in _SVG_RENDERERS in turn
    and returns the first PNG produced.
    """
    preview_dir = svg_path.parent / ".discord-previews"
    preview_dir.mkdir(parents=True, exist_ok=True)
    preview_path = preview_dir / f"{svg_path.name}.png"
    if preview_path.exists() and preview_path.stat().st_mtime >= svg_path.stat().st_mtime:
        return preview_path

    preview_path.unlink(missing_ok=True)

    for name, renderer in _SVG_RENDERERS:
        try:
            if renderer(svg_path, preview_path):
                return preview_path
        except Exception:
            logger.debug("SVG renderer %s raised while converting %s", name, svg_path.name, exc_info=True)

    # Previously this failed silently (#25) — no PNG and no explanation.
    # Attaching the raw .svg alone is still correct behavior when nothing is
    # installed, but the operator should be able to tell why from the log.
    logger.warning(
        "No SVG-to-PNG renderer available for %s (tried: %s); attaching the raw .svg only. "
        "Install qlmanage (macOS)/rsvg-convert/inkscape, or `pip install cairosvg`, for inline previews.",
        svg_path.name,
        ", ".join(name for name, _ in _SVG_RENDERERS),
    )
    return None


def _effective_inline_max_chunks() -> int:
    raw = os.environ.get("OUTPUT_INLINE_MAX_CHUNKS")
    if raw is None or not raw.strip():
        return OUTPUT_INLINE_MAX_CHUNKS
    try:
        value = int(raw)
    except ValueError:
        return OUTPUT_INLINE_MAX_CHUNKS
    if value <= 0:
        return OUTPUT_INLINE_MAX_CHUNKS
    return value


async def _send_body_text(channel: discord.abc.Messageable, text: str) -> None:
    """Send the answer body, chunked to fit Discord's per-message limit.

    Sending each chunk as its own message queues up against Discord's
    per-channel rate limit (5 sends / 5s), tail-latencying the rest of a
    long reply. If pagination would take more than
    ``_effective_inline_max_chunks()`` messages, skip pagination entirely
    and ship the whole body as one ``response.md`` attachment (a single
    send, so it never queues) plus a short preview of the start of the
    answer.
    """
    chunks = _chunk(text, DISCORD_CHUNK_LIMIT)
    if len(chunks) <= _effective_inline_max_chunks():
        for chunk in chunks:
            await channel.send(chunk)
        return

    preview = _build_preview(text)
    file = discord.File(io.BytesIO(text.encode("utf-8")), filename="response.md")
    await channel.send(preview, files=[file])


def _build_preview(text: str) -> str:
    budget = max(DISCORD_CHUNK_LIMIT - len(_PREVIEW_SUFFIX), 200)
    chunks = _chunk(text, budget)
    head = chunks[0] if chunks else ""
    return head.rstrip() + _PREVIEW_SUFFIX


def _chunk(text: str, limit: int = DISCORD_CHUNK_LIMIT) -> list[str]:
    """Split ``text`` into pieces of at most ``limit`` characters, safe to
    send as separate Discord messages.

    - Operates on ``str`` (Unicode codepoints), not bytes, so a chunk
      boundary never lands in the middle of a multi-byte/multi-codeunit
      character.
    - Tracks open/close code fences (```` ``` ```` or ``~~~``) line by line.
      If a chunk boundary would otherwise fall inside an open fence, the
      fence is closed at the end of that chunk and the same opening fence
      line (backticks/tildes, count, and language) is re-emitted at the
      start of the next chunk — so a fenced block never renders broken
      just because it was split across messages.
    """
    if not text:
        return []

    lines = text.splitlines(keepends=True)
    chunks: list[str] = []
    buf = ""
    open_fence_line: str | None = None  # full opening line, re-emitted on split
    open_fence_run: str | None = None   # bare marker (```` ``` ```` / ``~~~``), used to close

    def closer() -> str:
        return f"\n{open_fence_run}\n" if open_fence_run else ""

    def flush() -> None:
        nonlocal buf
        if not buf:
            return
        piece = buf
        if open_fence_run is not None:
            sep = "" if piece.endswith("\n") else "\n"
            piece = f"{piece}{sep}{open_fence_run}\n"
        chunks.append(piece)
        buf = ""

    for original_line in lines:
        fence_match = _FENCE_LINE_RE.match(original_line)
        line = original_line

        if buf and len(buf) + len(line) + len(closer()) > limit:
            flush()
            if open_fence_line is not None:
                buf = open_fence_line

        # A single line longer than the limit by itself (rare) is hard-split
        # so it can never block progress.
        while len(line) > limit:
            chunks.append(line[:limit])
            line = line[limit:]

        buf += line

        if fence_match:
            if open_fence_run is None:
                open_fence_run = fence_match.group(1)
                open_fence_line = original_line if original_line.endswith("\n") else original_line + "\n"
            else:
                open_fence_run = None
                open_fence_line = None

    flush()
    return chunks


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
