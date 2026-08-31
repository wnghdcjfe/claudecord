import os
from functools import lru_cache

import discord


def _owner_ids(name: str) -> frozenset[int]:
    # #28: same comma-separated shape as ALLOWED_CHANNEL_IDS, but a single
    # bare value (the previous format) still works unchanged.
    raw = os.environ.get(name)
    if raw is None:
        raise RuntimeError(f"환경변수 {name}이 필요합니다.")
    owners = set()
    for value in raw.split(","):
        value = value.strip()
        if value:
            try:
                owners.add(int(value))
            except ValueError as exc:
                raise RuntimeError(f"환경변수 {name}은 쉼표로 구분한 정수 Discord ID여야 합니다.") from exc
    if not owners:
        raise RuntimeError(f"환경변수 {name}이 필요합니다.")
    return frozenset(owners)


def _allowed_channels() -> frozenset[int]:
    raw = os.environ.get("ALLOWED_CHANNEL_IDS", "")
    channels = set()
    for value in raw.split(","):
        value = value.strip()
        if value:
            try:
                channels.add(int(value))
            except ValueError as exc:
                raise RuntimeError("ALLOWED_CHANNEL_IDS는 쉼표로 구분한 정수 Discord ID여야 합니다.") from exc
    return frozenset(channels)


# #9: reading env vars at import time made every test that merely imports
# src.main (and therefore src.auth) require Discord config. Deferring the
# read behind a cached function lets unrelated tests import this module
# freely, while ensure_configured() still gives the bot an explicit,
# fail-fast check at startup. Tests that need to vary the env vars should
# call _config.cache_clear() between runs.
@lru_cache(maxsize=1)
def _config() -> tuple[frozenset[int], frozenset[int]]:
    return _owner_ids("OWNER_DISCORD_ID"), _allowed_channels()


def ensure_configured() -> None:
    """Validate auth-related env vars now, so bad config fails at startup, not on the first message."""
    _config()


def is_authorized(msg: discord.Message) -> bool:
    owner_ids, allowed_channels = _config()
    if msg.author.bot or msg.author.id not in owner_ids:
        return False
    if isinstance(msg.channel, discord.DMChannel):
        return True
    return msg.channel.id in allowed_channels
