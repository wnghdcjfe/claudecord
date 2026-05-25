import os

import discord


def _required_int(name: str) -> int:
    value = os.environ.get(name)
    if value is None:
        raise RuntimeError(f"환경변수 {name}이 필요합니다.")
    try:
        return int(value)
    except ValueError as exc:
        raise RuntimeError(f"환경변수 {name}은 정수 Discord ID여야 합니다.") from exc


def _allowed_channels() -> set[int]:
    raw = os.environ.get("ALLOWED_CHANNEL_IDS", "")
    channels = set()
    for value in raw.split(","):
        value = value.strip()
        if value:
            try:
                channels.add(int(value))
            except ValueError as exc:
                raise RuntimeError("ALLOWED_CHANNEL_IDS는 쉼표로 구분한 정수 Discord ID여야 합니다.") from exc
    return channels


OWNER_ID = _required_int("OWNER_DISCORD_ID")
ALLOWED_CHANNELS = _allowed_channels()


def is_authorized(msg: discord.Message) -> bool:
    if msg.author.bot or msg.author.id != OWNER_ID:
        return False
    if isinstance(msg.channel, discord.DMChannel):
        return True
    return msg.channel.id in ALLOWED_CHANNELS
