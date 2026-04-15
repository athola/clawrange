"""Tests for the Telegram notification module."""

import httpx
import pytest
from unittest.mock import AsyncMock, patch

import telegram


def _mock_response(status_code: int = 200, body: dict | None = None) -> httpx.Response:
    """Build a fake httpx.Response for Telegram API calls."""
    import json

    content = json.dumps(body or {"ok": True, "result": {}}).encode()
    return httpx.Response(status_code=status_code, content=content)


CONFIGURED = {
    "BOT_TOKEN": "123:ABC",
    "CHAT_ID": "999",
    "API_BASE": "https://api.telegram.org/bot123:ABC",
}
UNCONFIGURED = {
    "BOT_TOKEN": "",
    "CHAT_ID": "",
    "API_BASE": "https://api.telegram.org/bot",
}


# ─── notify() ────────────────────────────────────────────────────


class TestNotify:
    """GIVEN a configured Telegram bot
    WHEN notify() is called
    THEN it should POST to sendMessage and return success/failure."""

    @pytest.mark.asyncio
    async def test_returns_false_when_not_configured(self):
        with patch.multiple(telegram, **UNCONFIGURED):
            assert await telegram.notify("test") is False

    @pytest.mark.asyncio
    async def test_sends_message_on_success(self):
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=_mock_response(200))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with (
            patch.multiple(telegram, **CONFIGURED),
            patch("telegram.httpx.AsyncClient", return_value=mock_client),
        ):
            result = await telegram.notify("hello")

        assert result is True
        call_args = mock_client.post.call_args
        assert "/sendMessage" in call_args.args[0]
        assert call_args.kwargs["json"]["text"] == "hello"
        assert call_args.kwargs["json"]["parse_mode"] == "Markdown"

    @pytest.mark.asyncio
    async def test_returns_false_on_api_error(self):
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=_mock_response(400, {"ok": False}))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with (
            patch.multiple(telegram, **CONFIGURED),
            patch("telegram.httpx.AsyncClient", return_value=mock_client),
        ):
            assert await telegram.notify("test") is False

    @pytest.mark.asyncio
    async def test_returns_false_on_network_error(self):
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(
            side_effect=httpx.ConnectError("connection refused")
        )
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with (
            patch.multiple(telegram, **CONFIGURED),
            patch("telegram.httpx.AsyncClient", return_value=mock_client),
        ):
            assert await telegram.notify("test") is False


# ─── send_typing() ───────────────────────────────────────────────


class TestSendTyping:
    """GIVEN a configured bot WHEN send_typing() is called
    THEN it should POST sendChatAction with action=typing."""

    @pytest.mark.asyncio
    async def test_returns_false_when_not_configured(self):
        with patch.multiple(telegram, **UNCONFIGURED):
            assert await telegram.send_typing() is False

    @pytest.mark.asyncio
    async def test_sends_typing_action(self):
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=_mock_response(200))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with (
            patch.multiple(telegram, **CONFIGURED),
            patch("telegram.httpx.AsyncClient", return_value=mock_client),
        ):
            result = await telegram.send_typing()

        assert result is True
        assert "/sendChatAction" in mock_client.post.call_args.args[0]
        assert mock_client.post.call_args.kwargs["json"]["action"] == "typing"


# ─── send_status() ───────────────────────────────────────────────


class TestSendStatus:
    """GIVEN a configured bot WHEN send_status() is called
    THEN it should return the message_id from the API response."""

    @pytest.mark.asyncio
    async def test_returns_none_when_not_configured(self):
        with patch.multiple(telegram, **UNCONFIGURED):
            assert await telegram.send_status("test") is None

    @pytest.mark.asyncio
    async def test_returns_message_id_on_success(self):
        body = {"ok": True, "result": {"message_id": 42}}
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=_mock_response(200, body))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with (
            patch.multiple(telegram, **CONFIGURED),
            patch("telegram.httpx.AsyncClient", return_value=mock_client),
        ):
            result = await telegram.send_status("processing...")

        assert result == 42

    @pytest.mark.asyncio
    async def test_returns_none_on_failure(self):
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=_mock_response(500))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with (
            patch.multiple(telegram, **CONFIGURED),
            patch("telegram.httpx.AsyncClient", return_value=mock_client),
        ):
            assert await telegram.send_status("test") is None


# ─── edit_status() ───────────────────────────────────────────────


class TestEditStatus:
    """GIVEN a previously sent message
    WHEN edit_status() is called with its message_id
    THEN it should POST editMessageText."""

    @pytest.mark.asyncio
    async def test_returns_false_when_not_configured(self):
        with patch.multiple(telegram, **UNCONFIGURED):
            assert await telegram.edit_status(42, "updated") is False

    @pytest.mark.asyncio
    async def test_returns_false_with_zero_message_id(self):
        with patch.multiple(telegram, **CONFIGURED):
            assert await telegram.edit_status(0, "updated") is False

    @pytest.mark.asyncio
    async def test_edits_message_on_success(self):
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=_mock_response(200))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with (
            patch.multiple(telegram, **CONFIGURED),
            patch("telegram.httpx.AsyncClient", return_value=mock_client),
        ):
            result = await telegram.edit_status(42, "new text")

        assert result is True
        call_json = mock_client.post.call_args.kwargs["json"]
        assert call_json["message_id"] == 42
        assert call_json["text"] == "new text"


# ─── delete_message() ────────────────────────────────────────────


class TestDeleteMessage:
    """GIVEN a previously sent message
    WHEN delete_message() is called
    THEN it should POST deleteMessage."""

    @pytest.mark.asyncio
    async def test_returns_false_when_not_configured(self):
        with patch.multiple(telegram, **UNCONFIGURED):
            assert await telegram.delete_message(42) is False

    @pytest.mark.asyncio
    async def test_returns_false_with_zero_message_id(self):
        with patch.multiple(telegram, **CONFIGURED):
            assert await telegram.delete_message(0) is False

    @pytest.mark.asyncio
    async def test_deletes_message_on_success(self):
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=_mock_response(200))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with (
            patch.multiple(telegram, **CONFIGURED),
            patch("telegram.httpx.AsyncClient", return_value=mock_client),
        ):
            result = await telegram.delete_message(42)

        assert result is True
        assert "/deleteMessage" in mock_client.post.call_args.args[0]

    @pytest.mark.asyncio
    async def test_returns_false_on_network_error(self):
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=httpx.TimeoutException("timeout"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with (
            patch.multiple(telegram, **CONFIGURED),
            patch("telegram.httpx.AsyncClient", return_value=mock_client),
        ):
            assert await telegram.delete_message(42) is False
