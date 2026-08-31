import json
import logging
import os
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


def _load() -> dict[str, Any]:
    path = _STORE_PATH
    cached = _store_cache.get(path)
    if cached is not None:
        return cached

    if path.exists():
        store = json.loads(path.read_text(encoding="utf-8"))
    else:
        store = {}
    _store_cache[path] = store
    return store


def _save(store: dict[str, Any]) -> bool:
    """Persist ``store`` and adopt it as the cache. Returns whether it landed.

    Never raises. main.py records the session *after* the job finished but
    *before* it sends the answer, so an OSError escaping from here used to
    skip send_outputs entirely and swallow the whole reply -- losing the
    answer over a bookkeeping failure. Callers hand in a fresh dict rather
    than mutating the cached one in place, so a failed write also leaves the
    cache exactly as consistent with disk as it was before.
    """
    path = _STORE_PATH
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(store, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError:
        logger.warning("Failed to persist session store at %s", path, exc_info=True)
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
    store = _load()
    key = str(channel_id)
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
    store = dict(_load())
    store[str(channel_id)] = {
        "session_id": session_id,
        "workdir": workdir,
        "system_hint": system_hint,
        "updated_at": (now or _now()).astimezone(UTC).isoformat(),
    }
    _save(store)


def clear_session(channel_id: int) -> None:
    store = dict(_load())
    store.pop(str(channel_id), None)
    _save(store)


def clear_all_sessions() -> int:
    cleared = len(_load())
    # Reporting a count the write never actually committed would tell the
    # user their sessions are gone while the store still holds them.
    return cleared if _save({}) else 0
