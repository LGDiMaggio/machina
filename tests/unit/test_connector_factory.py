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

    def test_unknown_type_raises(self) -> None:
        with pytest.raises(MachinaError, match="Unknown connector type"):
            create_connector("nonexistent", {})

    def test_types_come_from_the_runtime_registry(self) -> None:
        """One registry: create_connector offers exactly _CONNECTOR_FACTORIES,
        the table MachinaRuntime, the MCP server and `machina describe` read."""
        from machina.runtime import _CONNECTOR_FACTORIES

        with pytest.raises(MachinaError) as excinfo:
            create_connector("nonexistent", {})
        listed = str(excinfo.value).split("Available: ", 1)[1].split(", ")
        assert listed == sorted(_CONNECTOR_FACTORIES)

    def test_simulated_sensor_is_not_a_yaml_type(self, tmp_path: object) -> None:
        """SimulatedSensorConnector is constructed in Python (see the
        predictive_pipeline example); it is not a registered YAML type."""
        with pytest.raises(MachinaError, match="Unknown connector type"):
            create_connector("simulated_sensor", {"data_dir": str(tmp_path)})

    @pytest.mark.parametrize("type_name", ["excel", "excel_csv"])
    def test_excel_from_flat_settings(self, tmp_path: object, type_name: str) -> None:
        from pathlib import Path

        assets = Path(str(tmp_path)) / "assets.csv"
        assets.write_text("Codice,Nome\nP-201,Pompa\n", encoding="utf-8")
        conn = create_connector(
            type_name,
            {
                "asset_registry": {
                    "path": str(assets),
                    "columns": [
                        {"column": "Codice", "field": "id", "required": True},
                        {"column": "Nome", "field": "name", "required": True},
                    ],
                }
            },
        )
        assert type(conn).__name__ == "ExcelCsvConnector"

    @pytest.mark.parametrize("type_name", ["sql", "generic_sql"])
    def test_sql_from_flat_settings(self, type_name: str) -> None:
        conn = create_connector(
            type_name,
            {
                "dsn": "Driver={ODBC Driver 18};Server=localhost;",
                "tables": {
                    "assets": {
                        "query": "SELECT * FROM ASSETS",
                        "entity": "Asset",
                        "fields": {"id": {"column": "ASSET_ID"}, "name": {"column": "NAME"}},
                    }
                },
            },
        )
        assert type(conn).__name__ == "GenericSqlConnector"

    def test_missing_extra_names_the_type(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import machina.runtime

        def _missing(dotted_path: str) -> type:
            raise ImportError("No module named 'aiomqtt'")

        monkeypatch.setattr(machina.runtime, "_import_class", _missing)
        with pytest.raises(MachinaError, match="'mqtt' could not be imported"):
            create_connector("mqtt", {})


class TestCreateChannel:
    """Tests for create_channel()."""

    def test_cli_channel(self) -> None:
        ch = create_channel("cli", {})
        assert hasattr(ch, "send_message")

    def test_unknown_channel_raises(self) -> None:
        with pytest.raises(MachinaError, match="Unknown channel type"):
            create_channel("nonexistent", {})
