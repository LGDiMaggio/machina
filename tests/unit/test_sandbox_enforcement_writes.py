"""Sandbox enforcement on outbound-mutation methods across the connector layer.

These guard the invariant that *every* external side-effect path is blocked
when sandbox mode is active — not just CMMS work-order writes. The audit
behind these tests found comms ``send_message``, MQTT ``publish``, and
calendar ``create_event`` / ``delete_event`` executing for real in sandbox
mode (the MCP send tool even advertised a protection that did not exist).

The ``@sandbox_aware`` check fires *before* the method body, so each method
can be exercised through the class with a ``MagicMock`` self — no real
connection, credentials, or optional dependency is touched.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from machina.connectors.base import set_sandbox_mode
from machina.connectors.calendar.connector import CalendarConnector
from machina.connectors.comms.cli import CliChannel
from machina.connectors.comms.email import EmailConnector
from machina.connectors.comms.slack import SlackConnector
from machina.connectors.comms.telegram import TelegramConnector
from machina.connectors.iot.mqtt import MqttConnector
from machina.exceptions import SandboxViolationError


@pytest.fixture
def _sandbox_on() -> None:
    """Enable sandbox mode for the test and always restore it afterwards."""
    set_sandbox_mode(True)
    try:
        yield
    finally:
        set_sandbox_mode(False)


@pytest.mark.usefixtures("_sandbox_on")
class TestOutboundWritesBlockedInSandbox:
    """Every real external-mutation path raises SandboxViolationError."""

    @pytest.mark.asyncio
    async def test_telegram_send_message_blocked(self) -> None:
        with pytest.raises(SandboxViolationError):
            await TelegramConnector.send_message(MagicMock(), chat_id="123", text="hi")

    @pytest.mark.asyncio
    async def test_slack_send_message_blocked(self) -> None:
        with pytest.raises(SandboxViolationError):
            await SlackConnector.send_message(MagicMock(), channel="#ops", text="hi")

    @pytest.mark.asyncio
    async def test_email_send_message_blocked(self) -> None:
        with pytest.raises(SandboxViolationError):
            await EmailConnector.send_message(MagicMock(), to="a@b.com", text="hi")

    @pytest.mark.asyncio
    async def test_mqtt_publish_blocked(self) -> None:
        with pytest.raises(SandboxViolationError):
            await MqttConnector.publish(MagicMock(), topic="t", payload="p")

    @pytest.mark.asyncio
    async def test_calendar_create_event_blocked(self) -> None:
        with pytest.raises(SandboxViolationError):
            await CalendarConnector.create_event(MagicMock(), event=MagicMock())

    @pytest.mark.asyncio
    async def test_calendar_delete_event_blocked(self) -> None:
        with pytest.raises(SandboxViolationError):
            await CalendarConnector.delete_event(MagicMock(), event_id="evt-1")

    @pytest.mark.asyncio
    async def test_sql_update_work_order_blocked(self) -> None:
        """SQL update_work_order must be sandbox-gated like create_work_order —
        the decorator fires before the body, so the asymmetry that left it
        unguarded cannot resurface."""
        from machina.connectors.sql.generic import GenericSqlConnector

        with pytest.raises(SandboxViolationError):
            await GenericSqlConnector.update_work_order(MagicMock(), "WO-1", {})


class TestCliChannelStillWorksInSandbox:
    """The CLI channel only prints to stdout — it must NOT be sandbox-gated,
    otherwise the agent could not reply to the user in sandbox mode."""

    @pytest.mark.asyncio
    async def test_cli_send_message_not_blocked(self, capsys: pytest.CaptureFixture[str]) -> None:
        set_sandbox_mode(True)
        try:
            chan = CliChannel()
            await chan.send_message("cli", "hello from sandbox")
        finally:
            set_sandbox_mode(False)
        assert "hello from sandbox" in capsys.readouterr().out
