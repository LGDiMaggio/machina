"""Tests for the CliChannel (machina.connectors.comms.cli)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from machina.connectors.comms.cli import CliChannel


class TestBackCompatImportPaths:
    """The published import paths for CliChannel must keep working."""

    def test_telegram_alias_is_same_class(self) -> None:
        """Old path (telegram module) resolves to the same class object."""
        from machina.connectors.comms.cli import CliChannel as New
        from machina.connectors.comms.telegram import CliChannel as Old

        assert Old is New

    def test_public_api_import(self) -> None:
        """Top-level `machina.connectors` export is unchanged."""
        from machina.connectors import CliChannel as Public

        assert Public is CliChannel


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

    @pytest.mark.asyncio
    async def test_send_message_prints(self, capsys: pytest.CaptureFixture[str]) -> None:
        """send_message prints to stdout."""
        cli = CliChannel()
        await cli.send_message("cli", "Test response")
        captured = capsys.readouterr()
        assert "Test response" in captured.out

    @pytest.mark.asyncio
    async def test_listen_exit_command(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """listen exits cleanly on 'exit' command."""
        inputs = iter(["exit"])
        monkeypatch.setattr("builtins.input", lambda _prompt="": next(inputs))

        cli = CliChannel()
        await cli.connect()

        handler = AsyncMock(return_value="response")
        await cli.listen(handler)
        # Handler should never have been called (exit was first input)
        handler.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_listen_with_message_then_exit(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """listen processes message then exits."""
        inputs = iter(["hello", "quit"])
        monkeypatch.setattr("builtins.input", lambda _prompt="": next(inputs))

        cli = CliChannel()
        await cli.connect()

        handler = AsyncMock(return_value="Agent says hi")
        await cli.listen(handler)
        handler.assert_awaited_once()
        captured = capsys.readouterr()
        assert "Agent says hi" in captured.out

    @pytest.mark.asyncio
    async def test_listen_skips_empty_input(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """listen skips empty lines."""
        inputs = iter(["", "  ", "bye"])
        monkeypatch.setattr("builtins.input", lambda _prompt="": next(inputs))

        cli = CliChannel()
        await cli.connect()

        handler = AsyncMock(return_value="response")
        await cli.listen(handler)
        handler.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_listen_eof_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """listen handles EOFError gracefully."""

        def _raise_eof(_prompt: str = "") -> str:
            raise EOFError

        monkeypatch.setattr("builtins.input", _raise_eof)

        cli = CliChannel()
        await cli.connect()

        handler = AsyncMock(return_value="response")
        await cli.listen(handler)  # Should not raise

    def test_custom_prompt(self) -> None:
        cli = CliChannel(prompt=">>> ")
        assert cli._prompt == ">>> "

    @pytest.mark.asyncio
    async def test_listen_handler_returns_none(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Handler returning empty string should not call send_message."""
        inputs = iter(["hello", "exit"])
        monkeypatch.setattr("builtins.input", lambda _prompt="": next(inputs))

        cli = CliChannel()
        await cli.connect()

        handler = AsyncMock(return_value="")
        await cli.listen(handler)
        handler.assert_awaited_once()
        # send_message prints responses with the robot prefix — an empty
        # response must not produce one.
        captured = capsys.readouterr()
        assert "🤖" not in captured.out

    @pytest.mark.asyncio
    async def test_listen_keyboard_interrupt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """listen handles KeyboardInterrupt (Ctrl+C) gracefully."""

        def _raise_interrupt(_prompt: str = "") -> str:
            raise KeyboardInterrupt

        monkeypatch.setattr("builtins.input", _raise_interrupt)

        cli = CliChannel()
        await cli.connect()

        handler = AsyncMock(return_value="response")
        await cli.listen(handler)  # Should not raise


class TestCliChannelConfirmation:
    """Test CliChannel.request_confirmation (the synchronous HITL primitive)."""

    @pytest.mark.parametrize(
        "answer",
        # Shared IT+EN affirmation grammar (machina.connectors.comms.types
        # .is_affirmation), case-insensitive and trimmed — CLI now accepts the
        # same tokens as the runtime's two-turn confirmation path.
        ["y", "Y", "yes", "YES", " yes ", "Yes", "ok", "OK", " ok ", "sì", "si", "conferma"],
    )
    @pytest.mark.asyncio
    async def test_returns_true_on_affirmative(
        self, monkeypatch: pytest.MonkeyPatch, answer: str
    ) -> None:
        """Affirmative input (shared IT+EN tokens, case-insensitive, trimmed) returns True."""
        monkeypatch.setattr("builtins.input", lambda _prompt="": answer)
        cli = CliChannel()
        assert await cli.request_confirmation("cli", "Create WO?") is True

    @pytest.mark.parametrize("answer", ["n", "N", "no", "", "  ", "maybe", "yeah", "ok, but high"])
    @pytest.mark.asyncio
    async def test_returns_false_on_non_affirmative(
        self, monkeypatch: pytest.MonkeyPatch, answer: str
    ) -> None:
        """Non-affirmative / empty / unrelated / compound input returns False (safe default)."""
        monkeypatch.setattr("builtins.input", lambda _prompt="": answer)
        cli = CliChannel()
        assert await cli.request_confirmation("cli", "Create WO?") is False

    @pytest.mark.asyncio
    async def test_prompt_includes_write_description(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The concrete write description passed by the caller is rendered."""
        monkeypatch.setattr("builtins.input", lambda _prompt="": "n")
        cli = CliChannel()
        write_desc = "Create work order for asset P-201: bearing wear (HIGH)"
        await cli.request_confirmation("cli", write_desc)
        captured = capsys.readouterr()
        assert write_desc in captured.out
