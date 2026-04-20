"""Unit tests for SQL connector driver helpers."""

from __future__ import annotations

from unittest.mock import patch

import pytest


class TestConnectOdbc:
    def test_import(self):
        from machina.connectors.sql.drivers import connect_odbc

        assert connect_odbc is not None

    def test_raises_on_missing_pyodbc(self):
        from machina.connectors.sql.drivers import connect_odbc

        with patch.dict("sys.modules", {"pyodbc": None}), pytest.raises(Exception):
            connect_odbc("DSN=test")


class TestConnectJdbc:
    def test_import(self):
        from machina.connectors.sql.drivers import connect_jdbc

        assert connect_jdbc is not None

    def test_raises_on_missing_jaydebeapi(self):
        from machina.connectors.sql.drivers import connect_jdbc

        with patch.dict("sys.modules", {"jaydebeapi": None}), pytest.raises(Exception):
            connect_jdbc("jdbc:test://localhost", "com.test.Driver", None)
