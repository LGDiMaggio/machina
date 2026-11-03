"""A misspelled key inside a nested settings block fails validation, by name.

Top-level unknown keys were already refused; nested models used pydantic's
default ``extra="ignore"``, so ``write_mod: append`` silently left the sheet
read-only and ``tokn:`` silently dropped the credential.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from machina.connectors._settings import validate_settings
from machina.connectors.cmms import auth, generic_schema, pagination
from machina.connectors.cmms.auth import (
    ApiKeyHeaderAuth,
    BasicAuth,
    BearerAuth,
    NoAuth,
    OAuth2ClientCredentials,
)
from machina.connectors.cmms.generic import GenericCmmsConnector
from machina.connectors.cmms.generic_schema import (
    EndpointSpec,
    EntityMapping,
    FieldSpec,
    GenericCmmsYamlConfig,
    ReverseFieldSpec,
)
from machina.connectors.cmms.maximo import MaximoConnector
from machina.connectors.cmms.pagination import (
    CursorPagination,
    NoPagination,
    OffsetLimitPagination,
    PageNumberPagination,
)
from machina.connectors.cmms.sap_pm import SapPmConnector
from machina.connectors.docs import excel_schema
from machina.connectors.docs.excel_schema import (
    ColumnMapping,
    ExcelConnectorConfig,
    SheetSchema,
    WatcherConfig,
)
from machina.connectors.sql import schema as sql_schema
from machina.connectors.sql.generic import GenericSqlConnector
from machina.connectors.sql.schema import (
    FieldMapping,
    SqlConnectorConfig,
    SqlRetryConfig,
    TableMapping,
)
from machina.exceptions import ConnectorConfigError

SECRET = "S3CRET-SENTINEL"


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


@pytest.mark.parametrize(
    ("connector", "value", "key"),
    [
        (SapPmConnector, {"type": "basic", "username": "u", "password": "p", "user": "u"}, "user"),
        (
            SapPmConnector,
            {
                "type": "oauth2_client_credentials",
                "token_url": "https://sap.example.com/oauth/token",
                "client_id": "c",
                "client_secret": "s",
                "scopes": "read",
            },
            "scopes",
        ),
        (MaximoConnector, {"type": "api_key", "header": "apikey", "value": "k"}, "header"),
        (
            MaximoConnector,
            {"type": "basic", "username": "u", "password": "p", "user": "u"},
            "user",
        ),
        (MaximoConnector, {"type": "bearer", "token": "t", "tokn": "t"}, "tokn"),
    ],
)
def test_misspelled_sap_pm_or_maximo_auth_key_is_refused(
    connector: Any, value: dict[str, Any], key: str
) -> None:
    with pytest.raises(ConnectorConfigError, match=key):
        connector(url="https://cmms.example.com", auth=value)


_MAPPING = {"endpoint": {"path": "/assets"}, "fields": {"id": {"source": "code"}}}

# Smallest valid input for every settings model in the five modules.
_VALID: dict[type[BaseModel], dict[str, Any]] = {
    BearerAuth: {"token": "t"},
    BasicAuth: {"username": "u", "password": "p"},
    ApiKeyHeaderAuth: {"value": "k"},
    NoAuth: {},
    OAuth2ClientCredentials: {
        "token_url": "https://cmms.example.com/oauth/token",
        "client_id": "c",
        "client_secret": "s",
    },
    NoPagination: {},
    OffsetLimitPagination: {},
    PageNumberPagination: {},
    CursorPagination: {},
    ColumnMapping: {"column": "ID", "field": "id"},
    SheetSchema: _sheet(),
    WatcherConfig: {},
    ExcelConnectorConfig: {"work_orders": _sheet()},
    FieldMapping: {"column": "id"},
    TableMapping: _sql_tables()["assets"],
    SqlRetryConfig: {},
    SqlConnectorConfig: {"dsn": "x", "tables": _sql_tables()},
    FieldSpec: {"source": "code"},
    ReverseFieldSpec: {"target": "code"},
    EndpointSpec: {"path": "/assets"},
    EntityMapping: _MAPPING,
    GenericCmmsYamlConfig: {"mapping": {"asset": _MAPPING}},
}


def test_every_settings_model_is_covered() -> None:
    defined = {
        cls
        for module in (auth, pagination, generic_schema, excel_schema, sql_schema)
        for cls in vars(module).values()
        if inspect.isclass(cls)
        and issubclass(cls, BaseModel)
        and cls.__module__ == module.__name__
    }
    assert defined == set(_VALID)


@pytest.mark.parametrize("model", list(_VALID), ids=lambda model: model.__name__)
def test_every_settings_model_refuses_an_unknown_key(model: type[BaseModel]) -> None:
    model.model_validate(_VALID[model])
    with pytest.raises(ValidationError) as excinfo:
        model.model_validate({**_VALID[model], "typo_key": 1})
    assert [(err["type"], err["loc"]) for err in excinfo.value.errors()] == [
        ("extra_forbidden", ("typo_key",))
    ]


# Each case gives the secret either under an unknown key or next to a missing
# or invalid field, whose error repeats the whole input.
_LEAKY_INPUTS: list[tuple[type[BaseModel], dict[str, Any]]] = [
    (BearerAuth, {"token": "t", "tokn": SECRET}),
    (BearerAuth, {"tokn": SECRET}),
    (BearerAuth, {"token": {"value": SECRET}}),
    (BasicAuth, {"username": "u", "password": "p", "pasword": SECRET}),
    (BasicAuth, {"username": "u", "pasword": SECRET}),
    (ApiKeyHeaderAuth, {"header_name": "apikey", "valeu": SECRET}),
    (NoAuth, {"token": SECRET}),
    (
        OAuth2ClientCredentials,
        {"token_url": "https://cmms.example.com/oauth/token", "client_id": "c", "secret": SECRET},
    ),
    (SqlConnectorConfig, {"dsn": "x", "tables": _sql_tables(), "pwd": SECRET}),
    (SqlConnectorConfig, {"dsn": f"DSN=cmms;UID=u;PWD={SECRET}"}),
    # pydantic shortens a long input to its ends: keep the secret at the end.
    (SqlConnectorConfig, {"tables": _sql_tables(), "driver_type": "jdbc", "dsn": SECRET}),
]


@pytest.mark.parametrize(("model", "data"), _LEAKY_INPUTS)
def test_direct_construction_errors_do_not_echo_secrets(
    model: type[BaseModel], data: dict[str, Any]
) -> None:
    with pytest.raises(ValidationError) as excinfo:
        model(**data)
    assert SECRET not in str(excinfo.value)
    assert SECRET not in repr(excinfo.value)


@pytest.mark.parametrize(
    "build",
    [
        lambda: GenericCmmsConnector(
            url="https://cmms.example.com",
            auth={"type": "basic", "username": "u", "password": "p", "pasword": SECRET},
        ),
        lambda: GenericCmmsConnector(
            url="https://cmms.example.com", auth={"type": "bearer", "tokn": SECRET}
        ),
        lambda: SapPmConnector(
            url="https://sap.example.com",
            auth={"type": "basic", "username": "u", "password": "p", "pasword": SECRET},
        ),
        lambda: SapPmConnector(
            url="https://sap.example.com",
            auth={"type": "oauth2_client_credentials", "client_id": "c", "client_secret": SECRET},
        ),
        lambda: MaximoConnector(
            url="https://maximo.example.com",
            auth={"type": "api_key", "header": "apikey", "value": SECRET, "valeu": SECRET},
        ),
        lambda: MaximoConnector(
            url="https://maximo.example.com", auth={"type": "api_key", "valeu": SECRET}
        ),
        lambda: GenericSqlConnector(dsn="x", tables=_sql_tables(), retry={"pwd": SECRET}),
        lambda: GenericSqlConnector(dsn=f"DSN=cmms;UID=u;PWD={SECRET}"),
        lambda: GenericSqlConnector(tables=_sql_tables(), driver_type="jdbc", dsn=SECRET),
    ],
    ids=[
        "generic-unknown",
        "generic-missing",
        "sap_pm-unknown",
        "sap_pm-missing",
        "maximo-unknown",
        "maximo-missing",
        "sql-unknown",
        "sql-missing",
        "sql-invalid",
    ],
)
def test_settings_errors_do_not_echo_secrets(build: Any) -> None:
    with pytest.raises(ConnectorConfigError) as excinfo:
        build()
    assert SECRET not in str(excinfo.value)
    assert excinfo.value.__cause__ is None


def test_valid_nested_settings_still_load() -> None:
    validate_settings(ExcelConnectorConfig, {"work_orders": _sheet(), "watcher": {}})
    validate_settings(SqlConnectorConfig, {"dsn": "x", "tables": _sql_tables()})
    validate_settings(GenericCmmsYamlConfig, _yaml_mapping())
    GenericCmmsConnector(
        url="https://cmms.example.com",
        auth={"type": "bearer", "token": "t"},
        pagination={"type": "offset_limit", "page_size": 50},
    )
