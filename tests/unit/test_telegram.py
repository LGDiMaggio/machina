"""Tests for the TelegramConnector and CliChannel."""

from __future__ import annotations

import pytest

from machina.connectors.comms.telegram import CliChannel, IncomingMessage, TelegramConnector
from machina.exceptions import ConnectorError


class TestIncomingMessage:
    """Test IncomingMessage data class."""

    def test_basic_message(self) -> None:
        msg = IncomingMessage("Hello", chat_id="123", user_name="Mario")
        assert msg.text == "Hello"
        assert msg.chat_id == "123"
        assert msg.user_name == "Mario"
        assert msg.channel == "telegram"

    def test_cli_channel_message(self) -> None:
        msg = IncomingMessage("Test", channel="cli", user_id="cli_user")
        assert msg.channel == "cli"

    def test_repr(self) -> None:
        msg = IncomingMessage("Hello world", user_name="Test")
        assert "Hello world" in repr(msg)


class TestTelegramConnector:
    """Test TelegramConnector (without actual Telegram connection)."""

    def test_capabilities(self) -> None:
        conn = TelegramConnector(bot_token="fake")
        assert "send_message" in conn.capabilities
        assert "receive_message" in conn.capabilities

    @pytest.mark.asyncio
    async def test_connect_without_token_raises(self) -> None:
        conn = TelegramConnector()
        with pytest.raises(ConnectorError, match="bot_token"):
            await conn.connect()

    @pytest.mark.asyncio
    async def test_not_connected_raises(self) -> None:
        conn = TelegramConnector(bot_token="fake")
        with pytest.raises(ConnectorError, match="Not connected"):
            await conn.send_message("123", "test")

    @pytest.mark.asyncio
    async def test_health_check_not_connected(self) -> None:
        conn = TelegramConnector(bot_token="fake")
        health = await conn.health_check()
        assert health.status.value == "unhealthy"


class TestCliChannel:
    """Test CliChannel."""

    @pytest.mark.asyncio
    async def test_connect_disconnect(self) -> None:
        cli = CliChannel()
        await cli.connect()
        health = await cli.health_check()
        assert health.status.value == "healthy"
        await cli.disconnect()
        health = await cli.health_check()
        # CliChannel is always healthy by design
        assert health.status.value == "healthy"

    def test_capabilities(self) -> None:
        cli = CliChannel()
        assert "send_message" in cli.capabilities
        assert "receive_message" in cli.capabilities
