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

# One alternation, tried in order, so each path is consumed exactly once and
# the result is idempotent (orchestrator.py redacts when it raises and main.py
# redacts again when it sends).
#
#   url       matched first and returned untouched. Without it,
#             `https://example.com/a/b` came back as `https:/.../b` -- not a
#             leak, but it destroys the one part of an error a user can act on.
#             Two carve-outs, because a URL is only exempt where it is not
#             secretly a path: `file://` is skipped entirely (it *is* an
#             absolute path, wearing a scheme), and the match stops at `?`/`#`
#             so a path spelled inside a query string still gets redacted.
#             The lookbehind pins the scheme to a word start: without it the
#             engine simply stepped one character past the blocked `file:` and
#             matched `ile://...` as a scheme of its own.
#   winhome / posixhome
#             a home directory plus everything under it, so the replacement is
#             `~` + the useful tail in a single step. Both capture the tail
#             rather than stopping at the account name: matching only
#             `/Users/jane/` (with a trailing slash) let a bare `/Users/jane`
#             fall through to `abspath`, whose "keep the last segment" rule
#             then published the account name as `.../jane`.
#             winhome precedes posixhome so `C:/Users/jane/x` -- a Windows path
#             spelled with forward slashes, which Python echoes back verbatim --
#             is not mistaken for a POSIX one. Its drive letter is optional
#             because a drive-relative path leaks the same account name: on
#             Windows, opening `/Users/jane/x` fails with the drive dropped and
#             the separators flipped, so the OSError reads `\Users\jane\x` --
#             which posixhome and abspath, both forward-slash only, walk past.
#   abspath   any other absolute path: the directories above the last segment
#             describe the machine rather than the failure. The lookbehind
#             keeps it off paths an earlier rule already anchored, so `~/a/b`
#             and `.../b` survive a second pass unchanged; the `(?!~/)` does
#             the same one character later, for the `file://~/a/b` a previous
#             pass produced. Idempotence matters because orchestrator.py
#             redacts when it raises and main.py redacts again when it sends.
# One separator, as it can actually reach this function. `str(OSError)` renders
# the filename with `repr()`, which escapes backslashes -- so the text handed to
# `redact_paths` spells a Windows path `C:\\Users\\jane`, doubled, and a pattern
# that only knows the single form walks straight past the account name in it.
# Deliberately not `[\\/]{1,2}`, which would also swallow the `//` in a URL.
_SEP = r"(?:\\\\|[\\/])"

_REDACT = re.compile(
    r"(?P<url>(?<![A-Za-z0-9+.\-])(?![Ff][Ii][Ll][Ee]:)[A-Za-z][A-Za-z0-9+.\-]*://[^\s?#]*)"
    rf"|(?P<winhome>(?:[A-Za-z]:)?{_SEP}Users{_SEP}[^\\/\s]+(?:[\\/]\S*)?)"
    r"|(?P<posixhome>/(?:Users|home)/[^/\s]+(?:/\S*)?)"
    r"|(?P<abspath>(?<![~\w.])/(?!~/)(?:[^/\s]+/)+[^/\s]*)"
)


def _replace(match: re.Match[str]) -> str:
    if match.group("url") is not None:
        return match.group("url")

    windows = match.group("winhome")
    if windows is not None:
        # Drop `<drive>:<sep>Users<sep><account>`, keep the tail as typed so the
        # separator style the reader saw is the one they get back.
        tail = re.sub(rf"^(?:[A-Za-z]:)?{_SEP}Users{_SEP}[^\\/\s]+", "", windows)
        return "~" + tail

    posix = match.group("posixhome")
    if posix is not None:
        return "~" + re.sub(r"^/(?:Users|home)/[^/\s]+", "", posix)

    segment = match.group("abspath").rstrip("/").rsplit("/", 1)[-1]
    return f".../{segment}" if segment else "..."


def redact_paths(text: str) -> str:
    """Replace absolute filesystem paths with non-identifying equivalents.

    Home directories collapse to ``~``, keeping the part of the path that
    helps the reader ("which file failed") while dropping the account name.
    Any other absolute path is reduced to its last segment. URLs are left
    alone. Applying this twice gives the same answer as applying it once.
    """
    if not text:
        return text

    # The running user's own home first, in case it lives somewhere the
    # patterns below do not recognise (`/var/root`, a relocated profile).
    # The lookahead demands a real boundary so a sibling account whose name
    # merely starts with ours -- `/Users/janet` next to `/Users/jane` -- is
    # left for the generic rule instead of becoming `~t`.
    home = str(Path.home())
    if home and home != os.sep:
        text = re.sub(re.escape(home) + r"(?=[/\\]|\s|$)", "~", text)

    return _REDACT.sub(_replace, text)


def safe_error_text(exc: BaseException, *, limit: int = MAX_ERROR_CHARS) -> str:
    """Render an exception for Discord: type name, redacted message, truncated.

    The type name is kept because it is the part that tells the operator what
    went wrong, and it never contains user data.
    """
    message = redact_paths(str(exc)).strip()
    rendered = f"{type(exc).__name__}: {message}" if message else type(exc).__name__
    # A cap of zero or less means "no room", not "skip the cap" -- slicing with
    # `limit - 1` would otherwise hand back all but the final character.
    if limit <= 0:
        return ""
    if len(rendered) > limit:
        rendered = rendered[: limit - 1].rstrip() + "…"
    return rendered
