"""Tests for the @sandbox_aware decorator and sandbox context var."""

from __future__ import annotations

import pytest

from machina.connectors.base import get_sandbox_mode, sandbox_aware, set_sandbox_mode
from machina.exceptions import SandboxViolationError


class TestSandboxContextVar:
    def test_default_is_false(self) -> None:
        assert get_sandbox_mode() is False

    def test_set_and_get(self) -> None:
        set_sandbox_mode(True)
        try:
            assert get_sandbox_mode() is True
        finally:
            set_sandbox_mode(False)

    def test_token_restores(self) -> None:
        set_sandbox_mode(True)
        set_sandbox_mode(False)
        assert get_sandbox_mode() is False


class TestSandboxAwareDecorator:
    @pytest.mark.asyncio
    async def test_passes_through_when_sandbox_off(self) -> None:
        @sandbox_aware
        async def write_op() -> str:
            return "written"

        set_sandbox_mode(False)
        assert await write_op() == "written"

    @pytest.mark.asyncio
    async def test_raises_when_sandbox_on(self) -> None:
        @sandbox_aware
        async def write_op() -> str:
            return "written"

        set_sandbox_mode(True)
        try:
            with pytest.raises(SandboxViolationError, match="sandbox mode is active"):
                await write_op()
        finally:
            set_sandbox_mode(False)

    @pytest.mark.asyncio
    async def test_preserves_function_name(self) -> None:
        @sandbox_aware
        async def my_write_method() -> None:
            pass

        assert my_write_method.__name__ == "my_write_method"

    @pytest.mark.asyncio
    async def test_args_passed_through(self) -> None:
        @sandbox_aware
        async def write_op(a: int, b: str, *, c: bool = False) -> tuple[int, str, bool]:
            return (a, b, c)

        set_sandbox_mode(False)
        result = await write_op(1, "x", c=True)
        assert result == (1, "x", True)

    @pytest.mark.asyncio
    async def test_connector_write_blocked_in_sandbox(self) -> None:
        """Simulate a connector write method being blocked."""
        from unittest.mock import MagicMock

        from machina.connectors.capabilities import Capability

        mock_conn = MagicMock()
        mock_conn.capabilities = frozenset({Capability.CREATE_WORK_ORDER})

        @sandbox_aware
        async def create_work_order(self: object, wo: object) -> object:
            return wo

        set_sandbox_mode(True)
        try:
            with pytest.raises(SandboxViolationError):
                await create_work_order(mock_conn, "fake-wo")
        finally:
            set_sandbox_mode(False)
