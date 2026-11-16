"""A malformed (non-numeric) Discord target id fails via the SendResult contract.

A subscription carrying a thread/chat id like ``"NEW"`` can never become valid, but ``int()``
raised ``ValueError`` out of the channel resolver into ``send()``'s catch-all. That logged "Failed
to send Discord message" at ERROR with a full traceback on every retry of an exponential backoff.
Every outbound path funnels through ``_resolve_channel``, so the contract is pinned there and on
the main entry points. The failure names the offending value; a well-formed but missing id keeps
its own "not found" result.
"""

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.discord.adapter import DiscordAdapter


def _adapter(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="***"))
    adapter._client = SimpleNamespace(get_channel=MagicMock(return_value=None),
                                      fetch_channel=AsyncMock(return_value=None))
    return adapter


@pytest.mark.asyncio
@pytest.mark.parametrize(("chat_id", "metadata", "bad"), [
    ("555", {"thread_id": "NEW"}, "'NEW'"),
    ("not-a-channel", None, "'not-a-channel'"),
])
async def test_send_fails_cleanly_and_names_the_bad_id(monkeypatch, tmp_path, caplog, chat_id, metadata, bad):
    adapter = _adapter(monkeypatch, tmp_path)

    with caplog.at_level(logging.ERROR):
        result = await adapter.send(chat_id, "hello", metadata=metadata)

    assert result.success is False
    assert bad in result.error and "not a numeric Discord ID" in result.error
    assert not result.retryable
    adapter._client.get_channel.assert_not_called()
    adapter._client.fetch_channel.assert_not_called()
    assert "Failed to send Discord message" not in caplog.text


@pytest.mark.asyncio
async def test_file_send_fails_cleanly_on_a_bad_chat_id(monkeypatch, tmp_path):
    adapter = _adapter(monkeypatch, tmp_path)
    doc = tmp_path / "a.txt"
    doc.write_text("x")

    result = await adapter.send_document("NEW", str(doc))

    assert result.success is False
    assert "'NEW'" in result.error and "not a numeric Discord ID" in result.error
    adapter._client.get_channel.assert_not_called()


@pytest.mark.asyncio
async def test_well_formed_missing_thread_keeps_its_not_found_result(monkeypatch, tmp_path):
    adapter = _adapter(monkeypatch, tmp_path)

    result = await adapter.send("555", "hello", metadata={"thread_id": "123456789"})

    assert result.success is False and result.error == "Thread 123456789 not found"
    adapter._client.get_channel.assert_called_once_with(123456789)
