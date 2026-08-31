"""Redaction for text that leaves this machine.

Python exception messages routinely carry absolute filesystem paths --
``FileNotFoundError`` and ``PermissionError`` both embed the path they failed
on. This bot runs on someone's personal computer, so those paths spell out the
operator's real account name and the layout of unrelated projects. Discord
keeps every message it is handed, and the bot's own README recommends running
it against channels that may be shared, so a path that reaches a channel has
left the machine permanently.

Everything the bot sends to Discord on an error path goes through here first.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

# Longest sensible error line to forward. Discord truncates at 2000 anyway, and
# a stack-trace-sized `str(exc)` is noise rather than a diagnosis -- the full
# text stays in the log where it is useful.
MAX_ERROR_CHARS = 400

# `/Users/<name>/` (macOS), `/home/<name>/` (Linux). The trailing separator is
# required so a bare `/home` -- which names no individual -- is left alone.
_POSIX_HOME = re.compile(r"/(?:Users|home)/[^/\s]+/")

# `C:\Users\<name>\`. Drive letter is any letter; separators may be either
# slash, since Windows accepts both and Python echoes back whichever it got.
_WINDOWS_HOME = re.compile(r"[A-Za-z]:[\\/]Users[\\/][^\\/\s]+[\\/]")

# A remaining absolute POSIX path: a leading slash followed by at least two
# segments. One segment (`/tmp`) names nothing personal; two or more
# (`/opt/secret-client/build`) can. The lookbehind keeps this off paths that
# an earlier rule already anchored -- `~/Desktop/x` must not have its
# `/Desktop/x` tail clipped a second time.
_ABSOLUTE_PATH = re.compile(r"(?<![~\w.])/(?:[^/\s]+/)+[^/\s]*")


def redact_paths(text: str) -> str:
    """Replace absolute filesystem paths with non-identifying equivalents.

    Home directories collapse to ``~/``, keeping the part of the path that
    actually helps the reader ("which file failed") while dropping the account
    name. Any other absolute path is reduced to its last segment, since the
    directories above it describe the machine rather than the failure.
    """
    if not text:
        return text

    # The running user's own home first, so `/Users/jane/x` becomes `~/x`
    # rather than being caught by the generic rule below.
    home = str(Path.home())
    if home and home != os.sep:
        text = text.replace(home + os.sep, "~" + os.sep)
        text = text.replace(home, "~")

    text = _POSIX_HOME.sub("~/", text)
    text = _WINDOWS_HOME.sub("~\\\\", text)

    def _tail(match: re.Match[str]) -> str:
        segment = match.group(0).rstrip("/").rsplit("/", 1)[-1]
        return f".../{segment}" if segment else "..."

    return _ABSOLUTE_PATH.sub(_tail, text)


def safe_error_text(exc: BaseException, *, limit: int = MAX_ERROR_CHARS) -> str:
    """Render an exception for Discord: type name, redacted message, truncated.

    The type name is kept because it is the part that tells the operator what
    went wrong, and it never contains user data.
    """
    message = redact_paths(str(exc)).strip()
    rendered = f"{type(exc).__name__}: {message}" if message else type(exc).__name__
    if len(rendered) > limit:
        rendered = rendered[: limit - 1].rstrip() + "…"
    return rendered
