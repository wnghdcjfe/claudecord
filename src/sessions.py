import json
from pathlib import Path

_STORE_PATH = Path("~/.claudecord/sessions.json").expanduser()


def _load() -> dict[str, str]:
    if _STORE_PATH.exists():
        return json.loads(_STORE_PATH.read_text(encoding="utf-8"))
    return {}


def _save(store: dict[str, str]) -> None:
    _STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _STORE_PATH.write_text(json.dumps(store, indent=2) + "\n", encoding="utf-8")


def get_session(channel_id: int) -> str | None:
    return _load().get(str(channel_id))


def set_session(channel_id: int, session_id: str) -> None:
    store = _load()
    store[str(channel_id)] = session_id
    _save(store)


def clear_session(channel_id: int) -> None:
    store = _load()
    store.pop(str(channel_id), None)
    _save(store)
