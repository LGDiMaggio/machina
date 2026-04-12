"""Tests for the connector and channel factory."""

from __future__ import annotations

import pytest

from machina.connectors.factory import create_channel, create_connector
from machina.exceptions import MachinaError


class TestCreateConnector:
    """Tests for create_connector()."""

    def test_generic_cmms(self, tmp_path: object) -> None:
        conn = create_connector("generic_cmms", {"data_dir": str(tmp_path)})
        assert hasattr(conn, "capabilities")

    def test_document_store(self) -> None:
        conn = create_connector("document_store", {"paths": []})
        assert hasattr(conn, "capabilities")

    def test_simulated_sensor(self, tmp_path: object) -> None:
        conn = create_connector("simulated_sensor", {"data_dir": str(tmp_path)})
        assert hasattr(conn, "capabilities")

    def test_unknown_type_raises(self) -> None:
        with pytest.raises(MachinaError, match="Unknown connector type"):
            create_connector("nonexistent", {})

    def test_all_known_types_are_documented(self) -> None:
        """Every type in the factory must be a string, class must be callable."""
        from machina.connectors.factory import _connector_registry

        registry = _connector_registry()
        assert len(registry) >= 10  # at least the core connectors
        for name, cls in registry.items():
            assert isinstance(name, str)
            assert callable(cls)


class TestCreateChannel:
    """Tests for create_channel()."""

    def test_cli_channel(self) -> None:
        ch = create_channel("cli", {})
        assert hasattr(ch, "send_message")

    def test_unknown_channel_raises(self) -> None:
        with pytest.raises(MachinaError, match="Unknown channel type"):
            create_channel("nonexistent", {})
