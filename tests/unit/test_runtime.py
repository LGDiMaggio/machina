"""Tests for MachinaRuntime facade."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from machina.config.schema import ConnectorConfig, MachinaConfig
from machina.connectors.capabilities import Capability
from machina.exceptions import ConnectorError
from machina.runtime import MachinaRuntime


def _mock_connector(*, caps: frozenset[Capability] | None = None) -> MagicMock:
    conn = MagicMock()
    conn.capabilities = caps or frozenset({Capability.READ_ASSETS})
    conn.connect = AsyncMock()
    conn.disconnect = AsyncMock()
    conn.read_assets = AsyncMock(return_value=[])
    return conn


class TestMachinaRuntime:
    def test_init(self) -> None:
        conn = _mock_connector()
        runtime = MachinaRuntime(connectors={"cmms": conn})
        assert "cmms" in runtime.connectors
        assert runtime.sandbox_mode is False

    def test_get_primary_cmms(self) -> None:
        conn = _mock_connector()
        runtime = MachinaRuntime(connectors={"cmms": conn})
        assert runtime.get_primary_cmms() is conn

    def test_get_primary_cmms_no_connector_raises(self) -> None:
        runtime = MachinaRuntime()
        with pytest.raises(ConnectorError, match="No CMMS"):
            runtime.get_primary_cmms()

    def test_find_by_capability(self) -> None:
        conn = _mock_connector(
            caps=frozenset({Capability.READ_ASSETS, Capability.SEARCH_DOCUMENTS})
        )
        runtime = MachinaRuntime(connectors={"multi": conn})
        matches = runtime.find_by_capability(Capability.SEARCH_DOCUMENTS)
        assert len(matches) == 1

    @pytest.mark.asyncio
    async def test_connect_all(self) -> None:
        conn = _mock_connector()
        runtime = MachinaRuntime(connectors={"cmms": conn})
        await runtime.connect_all()
        conn.connect.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_disconnect_all(self) -> None:
        conn = _mock_connector()
        runtime = MachinaRuntime(connectors={"cmms": conn})
        await runtime.disconnect_all()
        conn.disconnect.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_connect_all_tolerates_failure(self) -> None:
        conn = _mock_connector()
        conn.connect = AsyncMock(side_effect=Exception("fail"))
        runtime = MachinaRuntime(connectors={"bad": conn})
        await runtime.connect_all()  # should not raise


class TestFromConfig:
    def test_from_config_generic_cmms(self) -> None:
        config = MachinaConfig(
            connectors={
                "cmms": ConnectorConfig(
                    type="generic_cmms",
                    settings={"data_dir": "sample_data/cmms"},
                ),
            },
        )
        runtime = MachinaRuntime.from_config(config)
        assert "cmms" in runtime.connectors

    def test_from_config_unknown_type_skipped(self) -> None:
        config = MachinaConfig(
            connectors={
                "unknown": ConnectorConfig(type="nonexistent_type"),
            },
        )
        runtime = MachinaRuntime.from_config(config)
        assert len(runtime.connectors) == 0

    def test_from_config_disabled_skipped(self) -> None:
        config = MachinaConfig(
            connectors={
                "cmms": ConnectorConfig(
                    type="generic_cmms",
                    enabled=False,
                    settings={"data_dir": "sample_data/cmms"},
                ),
            },
        )
        runtime = MachinaRuntime.from_config(config)
        assert len(runtime.connectors) == 0

    def test_from_config_sandbox(self) -> None:
        config = MachinaConfig(sandbox=True)
        runtime = MachinaRuntime.from_config(config)
        assert runtime.sandbox_mode is True
