"""Unit tests for SQL connector driver helpers."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from machina.exceptions import ConnectorDependencyError


class TestRequirePyodbc:
    def test_import(self):
        from machina.connectors.sql.drivers import connect_odbc

        assert connect_odbc is not None

    def test_raises_on_missing_pyodbc(self):
        from machina.connectors.sql.drivers import require_pyodbc

        with (
            patch.dict("sys.modules", {"pyodbc": None}),
            pytest.raises(ConnectorDependencyError, match="pyodbc"),
        ):
            require_pyodbc()


class TestRequireJaydebeapi:
    def test_import(self):
        from machina.connectors.sql.drivers import connect_jdbc

        assert connect_jdbc is not None

    def test_raises_on_missing_jaydebeapi(self):
        from machina.connectors.sql.drivers import require_jaydebeapi

        with (
            patch.dict("sys.modules", {"jaydebeapi": None}),
            pytest.raises(ConnectorDependencyError, match="jaydebeapi"),
        ):
            require_jaydebeapi()
