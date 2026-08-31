import logging
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Repo root, two levels up from this file (src/parser.py -> repo root).
_REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Command:
    prompt: str
    session_id: str | None = None
    workdir: str | None = None
    system_hint: str | None = None


def _projects_file_path() -> Path:
    # PROJECTS_FILE lets each user point at their own config without touching
    # source (issue #16); default keeps the previous "next to the repo" spot.
    configured = os.environ.get("PROJECTS_FILE")
    return Path(configured).expanduser() if configured else _REPO_ROOT / "projects.toml"


def _parse_projects_file(path: Path) -> dict[str, tuple[str, str]]:
    """Read and validate one projects.toml. Never raises."""
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        # UnicodeDecodeError is a ValueError, so neither of the other two
        # catches it. parse() runs on every message, so letting it escape
        # would stop the bot from answering anything at all.
        logger.error("could not read projects file %s: %s", path, exc)
        return {}

    projects: dict[str, tuple[str, str]] = {}
    for name, entry in data.items():
        if not isinstance(entry, dict):
            logger.error("projects file %s: [%s] is not a table, skipping", path, name)
            continue
        dirname = entry.get("dir")
        hint = entry.get("hint", "")
        if not isinstance(dirname, str) or not dirname:
            logger.error("projects file %s: [%s] missing required 'dir' string, skipping", path, name)
            continue
        if not isinstance(hint, str):
            logger.error("projects file %s: [%s] has non-string 'hint', skipping", path, name)
            continue
        projects[f"@{name}"] = (dirname, hint)
    return projects


# path -> (stat signature, parsed result). Keyed by (mtime_ns, size) rather
# than a plain lru_cache: parse() runs on the hot path of every Discord
# message (perf epic #1-7 just got rid of exactly this kind of per-message
# disk I/O, see #3/outputs.py), so we don't want to open+parse the TOML each
# time. But unlike a plain cache, editing projects.toml has to take effect
# without restarting the bot, so we still stat() every call (cheap) and only
# re-open+re-parse when mtime or size actually changed. Signature None means
# "file absent" at last check, so a newly-created file is picked up too.
_StatSignature = tuple[int, int] | None
_cache: dict[Path, tuple[_StatSignature, dict[str, tuple[str, str]]]] = {}


def _load_projects() -> dict[str, tuple[str, str]]:
    """Load project tag -> (dir, hint) pairs from PROJECTS_FILE.

    Absent file, corrupt TOML, or malformed entries all degrade to "no
    project tags" rather than crashing the bot; problems are logged so the
    user can fix their config.
    """
    path = _projects_file_path()
    try:
        st = path.stat()
        signature: _StatSignature = (st.st_mtime_ns, st.st_size)
    except OSError:
        # No config = no project tags. Backward compatible: a fresh checkout
        # with nothing configured just never matches a project tag.
        signature = None

    cached = _cache.get(path)
    if cached is not None and cached[0] == signature:
        return cached[1]

    projects = _parse_projects_file(path) if signature is not None else {}
    _cache[path] = (signature, projects)
    return projects


def _resolve_workdir(dirname: str) -> str:
    # An absolute `dir` in config is used as-is; a relative one is resolved
    # under PROJECT_ROOT, same as before this file existed as config.
    dir_path = Path(dirname).expanduser()
    if dir_path.is_absolute():
        return str(dir_path)
    project_root = Path(os.environ.get("PROJECT_ROOT", "~/projects")).expanduser()
    return str(project_root / dir_path)


def project_definitions() -> dict[str, tuple[str, str]]:
    return {
        tag: (_resolve_workdir(dirname), hint)
        for tag, (dirname, hint) in _load_projects().items()
    }


def parse(text: str) -> Command:
    text = text.strip()
    if text.startswith("@sess-"):
        head, _, rest = text.partition(" ")
        return Command(prompt=rest, session_id=head[1:])

    for tag, (workdir, hint) in project_definitions().items():
        if text.startswith(tag + " "):
            return Command(
                prompt=text[len(tag) + 1:],
                workdir=workdir,
                system_hint=hint,
            )

    return Command(prompt=text)
