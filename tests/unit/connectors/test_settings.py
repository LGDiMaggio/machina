"""Tests for flat-settings validation shared by YAML-built connectors."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from machina.connectors._settings import validate_settings
from machina.exceptions import ConnectorConfigError


class _Model(BaseModel):
    dsn: str
    retries: int = 3


def test_valid_settings_return_the_model() -> None:
    model = validate_settings(_Model, {"dsn": "Driver=x;PWD=hunter2", "retries": 5})
    assert model.retries == 5


def test_error_names_the_field_without_echoing_input() -> None:
    secret = "Driver={ODBC};UID=u;PWD=hunter2-secret"
    with pytest.raises(ConnectorConfigError) as excinfo:
        validate_settings(_Model, {"dsn": secret, "retries": "not-a-number"})
    message = str(excinfo.value)
    assert "retries" in message
    assert "hunter2" not in message
    assert excinfo.value.__cause__ is None  # the pydantic error is not chained


def test_missing_field_is_reported() -> None:
    with pytest.raises(ConnectorConfigError, match="dsn"):
        validate_settings(_Model, {})
