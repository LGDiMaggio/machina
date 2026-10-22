"""A misspelled key inside a nested settings block fails validation, by name.

Top-level unknown keys were already refused; nested models used pydantic's
default ``extra="ignore"``, so ``write_mod: append`` silently left the sheet
read-only and ``tokn:`` silently dropped the credential.
"""

from __future__ import annotations

from typing import Any

import pytest

from machina.connectors._settings import validate_settings
from machina.connectors.cmms.generic import GenericCmmsConnector
from machina.connectors.cmms.generic_schema import GenericCmmsYamlConfig
from machina.connectors.docs.excel_schema import ExcelConnectorConfig
from machina.connectors.sql.schema import SqlConnectorConfig
from machina.exceptions import ConnectorConfigError


def _sheet(**overrides: Any) -> dict[str, Any]:
    sheet: dict[str, Any] = {
        "path": "odl.xlsx",
        "sheet": "OdL",
        "write_mode": "append",
        "columns": [
            {"column": "ID", "field": "id", "required": True},
            {"column": "Codice Asset", "field": "asset_id", "required": True},
        ],
    }
    sheet.update(overrides)
    return sheet


def _sql_tables(**field_overrides: Any) -> dict[str, Any]:
    return {
        "assets": {
            "query": "SELECT id, name FROM assets",
            "entity": "Asset",
            "fields": {"id": {"column": "id", **field_overrides}, "name": {"column": "name"}},
        }
    }


def _yaml_mapping(**field_overrides: Any) -> dict[str, Any]:
    return {
        "mapping": {
            "asset": {
                "endpoint": {"method": "GET", "path": "/assets"},
                "fields": {
                    "id": {"source": "code", **field_overrides},
                    "name": {"source": "label"},
                },
            }
        }
    }


@pytest.mark.parametrize(
    ("model", "settings", "key"),
    [
        (ExcelConnectorConfig, {"work_orders": _sheet(write_mod="append")}, "write_mod"),
        (
            ExcelConnectorConfig,
            {
                "work_orders": _sheet(
                    columns=[
                        {"column": "ID", "field": "id", "requried": True},
                        {"column": "Codice Asset", "field": "asset_id", "required": True},
                    ]
                )
            },
            "requried",
        ),
        (
            ExcelConnectorConfig,
            {"work_orders": _sheet(), "watcher": {"debounce_msec": 100}},
            "debounce_msec",
        ),
        (SqlConnectorConfig, {"dsn": "x", "tables": _sql_tables(coerse="decimal")}, "coerse"),
        (
            SqlConnectorConfig,
            {
                "dsn": "x",
                "tables": {"assets": {**_sql_tables()["assets"], "insert_tabel": "ASSETS"}},
            },
            "insert_tabel",
        ),
        (
            SqlConnectorConfig,
            {"dsn": "x", "tables": _sql_tables(), "retry": {"max_retry": 5}},
            "max_retry",
        ),
        (GenericCmmsYamlConfig, _yaml_mapping(requird=True), "requird"),
    ],
)
def test_misspelled_nested_key_is_refused_by_name(
    model: Any, settings: dict[str, Any], key: str
) -> None:
    with pytest.raises(ConnectorConfigError, match=key):
        validate_settings(model, settings)


@pytest.mark.parametrize(
    ("setting", "value", "key"),
    [
        ("auth", {"type": "bearer", "token": "t", "tokn": "t"}, "tokn"),
        ("auth", {"type": "basic", "username": "u", "password": "p", "user": "u"}, "user"),
        ("pagination", {"type": "offset_limit", "page_sise": 50}, "page_sise"),
        ("pagination", {"type": "cursor", "cursor_parm": "next"}, "cursor_parm"),
    ],
)
def test_misspelled_auth_or_pagination_key_is_refused(
    setting: str, value: dict[str, Any], key: str
) -> None:
    with pytest.raises(ConnectorConfigError, match=key):
        GenericCmmsConnector(url="https://cmms.example.com", **{setting: value})


def test_refusal_does_not_echo_values() -> None:
    with pytest.raises(ConnectorConfigError) as excinfo:
        GenericCmmsConnector(
            url="https://cmms.example.com",
            auth={"type": "basic", "username": "u", "password": "p", "pasword": "S3CRET-VALUE"},
        )
    assert "pasword" in str(excinfo.value)
    assert "S3CRET-VALUE" not in str(excinfo.value)


def test_valid_nested_settings_still_load() -> None:
    validate_settings(ExcelConnectorConfig, {"work_orders": _sheet(), "watcher": {}})
    validate_settings(SqlConnectorConfig, {"dsn": "x", "tables": _sql_tables()})
    validate_settings(GenericCmmsYamlConfig, _yaml_mapping())
    GenericCmmsConnector(
        url="https://cmms.example.com",
        auth={"type": "bearer", "token": "t"},
        pagination={"type": "offset_limit", "page_size": 50},
    )
