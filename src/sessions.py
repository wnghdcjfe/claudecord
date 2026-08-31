import json
import logging
import os
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_STORE_PATH = Path("~/.claudecord/sessions.json").expanduser()

# Default TTL; overridable per-process via the SESSION_TTL_SECONDS env var
# (see _effective_ttl_seconds). An unset or invalid env value falls back to
# this constant.
SESSION_TTL_SECONDS = 60 * 60


def _effective_ttl_seconds() -> int:
    raw = os.environ.get("SESSION_TTL_SECONDS")
    if raw is None or not raw.strip():
        return SESSION_TTL_SECONDS
    try:
        value = int(raw)
    except ValueError:
        return SESSION_TTL_SECONDS
    if value <= 0:
        return SESSION_TTL_SECONDS
    return value


@dataclass(frozen=True)
class SessionState:
    session_id: str
    workdir: str | None = None
    updated_at: datetime | None = None
    # The project hint (parser.PROJECTS) the conversation was started with.
    # Persisted because the rule block reaches the CLI through
    # --append-system-prompt, which is re-supplied on every turn while the
    # hint used to live only in the message that carried the @project tag:
    # the second, untagged turn kept the project's workdir but silently lost
    # its constraints. See main._resolve_resume_and_workdir.
    system_hint: str | None = None


# Process-local cache of the on-disk store, keyed by store path. Every
# incoming message does at least a get_session_state + set_session round
# trip, and both previously did a full disk read + JSON parse of the whole
# store on *every* call (set_session's read was immediately thrown away by
# the write that followed). This bot process is the store's sole writer, so
# once loaded, the cached dict is trusted as authoritative for that path —
# _save keeps it in sync on every mutation — and a hot read never touches
# disk again. Keyed by path (rather than a single global) so tests that
# repoint _STORE_PATH at a fresh temp file per test don't see stale data.
_store_cache: dict[Path, dict[str, Any]] = {}

# Serializes the read-modify-write cycles below (issue #14). Each public
# function reads the whole store, edits it, and writes it back; without a
# lock two concurrent turns both read the same snapshot and the second write
# silently drops the first one's session.
#
# Why a threading lock is enough here:
#   - Every caller today (main.py) invokes these synchronous functions
#     straight from the event-loop thread, where they already run to
#     completion without interleaving. The lock is uncontended there, so it
#     costs nothing on the hot path -- but it means correctness no longer
#     *depends* on that being true.
#   - Neighbouring modules push blocking file work onto asyncio.to_thread
#     workers (outputs.py, main.py's prepare_job). The day a session call
#     moves off the loop thread the same way, real OS threads race here, and
#     only a threading primitive covers that. An asyncio.Lock would not: it
#     protects nothing between threads, and it would force every caller in
#     main.py to become a coroutine.
#   - It is re-entrant because the public functions hold it across _load and
#     _save, which take it too.
#   - Cross-*process* locking is deliberately out of scope: one bot process
#     owns this store (that is the premise _store_cache is built on), and a
#     second writer would already be fighting the cache, not just the file.
_store_lock = threading.RLock()


def _unlink_quietly(path: Path) -> None:
    """Best-effort cleanup of a temp file; a leftover is not worth raising."""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.debug("Failed to remove temp session store %s", path, exc_info=True)


def _quarantine_corrupt_store(path: Path) -> None:
    """Move an unreadable store aside so the bot can start over.

    get_session_state runs on *every* incoming message, so a store that fails
    to parse used to raise on every turn and take the bot down until someone
    deleted the file by hand. Dropping the resume ids costs the user one
    "the conversation didn't continue"; raising costs them the whole bot.
    The file is moved rather than deleted so the damage can still be looked
    at afterwards.
    """
    backup = path.with_name(path.name + ".corrupt")
    try:
        os.replace(path, backup)
    except OSError:
        # The store stays on disk, but the empty cache adopted by _load means
        # nothing reads it again and the next _save overwrites it.
        logger.warning("Failed to back up corrupt session store %s", path, exc_info=True)
    else:
        logger.warning(
            "Session store %s was unreadable; moved it to %s and started empty",
            path,
            backup,
        )


def _load() -> dict[str, Any]:
    path = _STORE_PATH
    with _store_lock:
        cached = _store_cache.get(path)
        if cached is not None:
            return cached

        store: dict[str, Any] = {}
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                # orchestrator._read_continuation_workdir has always handled
                # exactly this; sessions.py was the one reader that did not.
                _quarantine_corrupt_store(path)
            else:
                if isinstance(loaded, dict):
                    store = loaded
                else:
                    # Valid JSON that is not an object (a bare list, a string)
                    # would only blow up later on store.get, which is no
                    # better than a decode error.
                    _quarantine_corrupt_store(path)
        _store_cache[path] = store
        return store


def _save(store: dict[str, Any]) -> bool:
    """Persist ``store`` atomically and adopt it as the cache.

    Returns whether it landed. Never raises: main.py records the session
    *after* the job finished but *before* it sends the answer, so an OSError
    escaping from here used to skip send_outputs entirely and swallow the
    whole reply -- losing the answer over a bookkeeping failure. Callers hand
    in a fresh dict rather than mutating the cached one in place, so a failed
    write also leaves the cache exactly as consistent with disk as it was
    before.
    """
    path = _STORE_PATH
    # One temp name per process: _store_lock already keeps this process's own
    # saves from overlapping on it, and the pid keeps a second process (or a
    # test run beside the bot) from scribbling on the half-written file this
    # one is about to rename.
    tmp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    payload = json.dumps(store, ensure_ascii=False, indent=2) + "\n"
    with _store_lock:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Writing the store path directly left a truncated file behind if
            # the process died mid-write -- precisely the corruption _load now
            # has to recover from. os.replace is atomic, so a reader sees
            # either the whole old store or the whole new one. There is no
            # fsync: this defends against the process dying, not against power
            # loss, and the residue power loss could leave is exactly what
            # _quarantine_corrupt_store handles.
            tmp_path.write_text(payload, encoding="utf-8")
            os.replace(tmp_path, path)
        except OSError:
            logger.warning("Failed to persist session store at %s", path, exc_info=True)
            _unlink_quietly(tmp_path)
            return False
        _store_cache[path] = store
        return True


def _now() -> datetime:
    return datetime.now(UTC)


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _coerce_state(raw: Any) -> SessionState | None:
    if isinstance(raw, str):
        return SessionState(session_id=raw)
    if not isinstance(raw, dict):
        return None

    session_id = raw.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return None

    workdir = raw.get("workdir")
    if not isinstance(workdir, str) or not workdir:
        workdir = None

    # Absent in every store written before the hint was persisted, so it is
    # read the same tolerant way as workdir rather than being required.
    system_hint = raw.get("system_hint")
    if not isinstance(system_hint, str) or not system_hint:
        system_hint = None

    return SessionState(
        session_id=session_id,
        workdir=workdir,
        updated_at=_parse_datetime(raw.get("updated_at")),
        system_hint=system_hint,
    )


def _is_expired(state: SessionState, now: datetime) -> bool:
    if state.updated_at is None:
        return False
    return now - state.updated_at > timedelta(seconds=_effective_ttl_seconds())


def get_session_state(channel_id: int, *, now: datetime | None = None) -> SessionState | None:
    key = str(channel_id)
    # Held across the whole read-modify-write: the expiry prune below is a
    # full-store rewrite, so it races with set_session exactly like the
    # mutators do.
    with _store_lock:
        store = _load()
        state = _coerce_state(store.get(key))

        if state is None:
            return None

        if _is_expired(state, now or _now()):
            pruned = dict(store)
            pruned.pop(key, None)
            _save(pruned)
            return None

        return state


def get_session(channel_id: int) -> str | None:
    state = get_session_state(channel_id)
    return state.session_id if state else None


def set_session(
    channel_id: int,
    session_id: str,
    *,
    workdir: str | None = None,
    system_hint: str | None = None,
    now: datetime | None = None,
) -> None:
    with _store_lock:
        store = dict(_load())
        store[str(channel_id)] = {
            "session_id": session_id,
            "workdir": workdir,
            "system_hint": system_hint,
            "updated_at": (now or _now()).astimezone(UTC).isoformat(),
        }
        _save(store)


def clear_session(channel_id: int) -> None:
    with _store_lock:
        store = dict(_load())
        store.pop(str(channel_id), None)
        _save(store)


def clear_all_sessions() -> int:
    with _store_lock:
        cleared = len(_load())
        if not cleared:
            # `종료` gets sent repeatedly; rewriting an already-empty store
            # each time is disk churn for a no-op (issue #27).
            return 0
        # Reporting a count the write never actually committed would tell the
        # user their sessions are gone while the store still holds them.
        return cleared if _save({}) else 0
