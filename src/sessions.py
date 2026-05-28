import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_STORE_PATH = Path("~/.claudecord/sessions.json").expanduser()
SESSION_TTL_SECONDS = 60 * 60


@dataclass(frozen=True)
class SessionState:
    session_id: str
    workdir: str | None = None
    updated_at: datetime | None = None


def _load() -> dict[str, Any]:
    if _STORE_PATH.exists():
        return json.loads(_STORE_PATH.read_text(encoding="utf-8"))
    return {}


def _save(store: dict[str, Any]) -> None:
    _STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _STORE_PATH.write_text(
        json.dumps(store, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


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

    return SessionState(
        session_id=session_id,
        workdir=workdir,
        updated_at=_parse_datetime(raw.get("updated_at")),
    )


def _is_expired(state: SessionState, now: datetime) -> bool:
    if state.updated_at is None:
        return False
    return now - state.updated_at > timedelta(seconds=SESSION_TTL_SECONDS)


def get_session_state(channel_id: int, *, now: datetime | None = None) -> SessionState | None:
    store = _load()
    key = str(channel_id)
    state = _coerce_state(store.get(key))

    if state is None:
        return None

    if _is_expired(state, now or _now()):
        store.pop(key, None)
        _save(store)
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
    now: datetime | None = None,
) -> None:
    store = _load()
    store[str(channel_id)] = {
        "session_id": session_id,
        "workdir": workdir,
        "updated_at": (now or _now()).astimezone(timezone.utc).isoformat(),
    }
    _save(store)


def clear_session(channel_id: int) -> None:
    store = _load()
    store.pop(str(channel_id), None)
    _save(store)


def clear_all_sessions() -> int:
    store = _load()
    cleared = len(store)
    _save({})
    return cleared
