"""Tests for src/auth.py: lazy config (#9) and multi-owner support (#28)."""

import sys

import discord
import pytest

from src import auth


@pytest.fixture(autouse=True)
def _clear_config_cache():
    # Each test sets its own env vars, so the cached config from a previous
    # test (or from module import) must not leak in.
    auth._config.cache_clear()
    yield
    auth._config.cache_clear()


def _make_message(author_id: int, *, is_bot: bool = False, channel=None):
    msg = object.__new__(discord.Message)
    author = object.__new__(discord.User)
    author.id = author_id
    author.bot = is_bot
    msg.author = author
    msg.channel = channel if channel is not None else object.__new__(discord.DMChannel)
    return msg


def test_import_succeeds_without_env_vars(monkeypatch):
    # #9: importing src.auth must not require OWNER_DISCORD_ID / ALLOWED_CHANNEL_IDS.
    monkeypatch.delenv("OWNER_DISCORD_ID", raising=False)
    monkeypatch.delenv("ALLOWED_CHANNEL_IDS", raising=False)
    for mod in ("src.auth",):
        sys.modules.pop(mod, None)
    import src.auth as reimported  # noqa: F401 -- just verifying it doesn't raise


def test_ensure_configured_raises_without_owner_env(monkeypatch):
    monkeypatch.delenv("OWNER_DISCORD_ID", raising=False)
    with pytest.raises(RuntimeError, match="OWNER_DISCORD_ID"):
        auth.ensure_configured()


def test_ensure_configured_raises_on_blank_owner_env(monkeypatch):
    monkeypatch.setenv("OWNER_DISCORD_ID", "   ")
    with pytest.raises(RuntimeError, match="OWNER_DISCORD_ID"):
        auth.ensure_configured()


def test_ensure_configured_raises_on_invalid_owner_id(monkeypatch):
    monkeypatch.setenv("OWNER_DISCORD_ID", "not-an-int")
    with pytest.raises(RuntimeError, match="정수 Discord ID"):
        auth.ensure_configured()


def test_ensure_configured_ok_with_valid_env(monkeypatch):
    monkeypatch.setenv("OWNER_DISCORD_ID", "111")
    auth.ensure_configured()  # should not raise


def test_single_owner_id_backward_compatible(monkeypatch):
    monkeypatch.setenv("OWNER_DISCORD_ID", "111")
    monkeypatch.delenv("ALLOWED_CHANNEL_IDS", raising=False)

    dm_msg = _make_message(111)
    assert auth.is_authorized(dm_msg) is True

    other_msg = _make_message(222)
    assert auth.is_authorized(other_msg) is False


def test_multiple_owner_ids_comma_separated(monkeypatch):
    monkeypatch.setenv("OWNER_DISCORD_ID", "111, 222,333")
    monkeypatch.delenv("ALLOWED_CHANNEL_IDS", raising=False)

    assert auth.is_authorized(_make_message(111)) is True
    assert auth.is_authorized(_make_message(222)) is True
    assert auth.is_authorized(_make_message(333)) is True
    assert auth.is_authorized(_make_message(444)) is False


def test_rejects_unauthorized_user(monkeypatch):
    monkeypatch.setenv("OWNER_DISCORD_ID", "111")
    monkeypatch.delenv("ALLOWED_CHANNEL_IDS", raising=False)
    assert auth.is_authorized(_make_message(999)) is False


def test_rejects_bots_even_if_owner_id_matches(monkeypatch):
    monkeypatch.setenv("OWNER_DISCORD_ID", "111")
    monkeypatch.delenv("ALLOWED_CHANNEL_IDS", raising=False)
    bot_msg = _make_message(111, is_bot=True)
    assert auth.is_authorized(bot_msg) is False


def test_allows_dm_regardless_of_channel_whitelist(monkeypatch):
    monkeypatch.setenv("OWNER_DISCORD_ID", "111")
    monkeypatch.setenv("ALLOWED_CHANNEL_IDS", "555")
    dm_msg = _make_message(111)  # default channel is a DMChannel
    assert auth.is_authorized(dm_msg) is True


def test_channel_whitelist_enforced_for_non_dm(monkeypatch):
    monkeypatch.setenv("OWNER_DISCORD_ID", "111")
    monkeypatch.setenv("ALLOWED_CHANNEL_IDS", "555,666")

    allowed_channel = object.__new__(discord.TextChannel)
    allowed_channel.id = 555
    allowed_msg = _make_message(111, channel=allowed_channel)
    assert auth.is_authorized(allowed_msg) is True

    other_channel = object.__new__(discord.TextChannel)
    other_channel.id = 777
    other_msg = _make_message(111, channel=other_channel)
    assert auth.is_authorized(other_msg) is False


def test_invalid_channel_id_raises(monkeypatch):
    monkeypatch.setenv("OWNER_DISCORD_ID", "111")
    monkeypatch.setenv("ALLOWED_CHANNEL_IDS", "not-an-int")
    with pytest.raises(RuntimeError, match="ALLOWED_CHANNEL_IDS"):
        auth.ensure_configured()
