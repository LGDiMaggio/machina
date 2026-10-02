"""Unit tests for SQL connector driver helpers."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from machina.exceptions import ConnectorDependencyError, ConnectorDriverError, ConnectorError


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


class TestConnectionErrorsDoNotEchoTheDsn:
    """Driver messages may quote the DSN; the raised error must not."""

    SECRET = "hunter2-secret"
    ODBC_DSN = "Driver={ODBC Driver 18};Server=db;UID=svc;PWD=hunter2-secret"
    JDBC_URL = "jdbc:postgresql://svc:hunter2-secret@db:5432/maint?password=hunter2-secret"

    @staticmethod
    def _fake_pyodbc(raised: Exception) -> SimpleNamespace:
        def connect(dsn: str, autocommit: bool) -> None:
            raise raised

        return SimpleNamespace(
            connect=connect, Error=_FakeOdbcError, InterfaceError=_FakeOdbcInterfaceError
        )

    def test_jdbc_error_redacts_the_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from machina.connectors.sql import drivers

        def connect(driver_class: str, dsn: str) -> None:
            raise RuntimeError(f"No suitable driver found for {dsn}")

        fake_jpype = SimpleNamespace(isJVMStarted=lambda: True)
        monkeypatch.setattr(
            drivers, "require_jaydebeapi", lambda: (SimpleNamespace(connect=connect), fake_jpype)
        )

        with pytest.raises(ConnectorDriverError) as excinfo:
            drivers.connect_jdbc(self.JDBC_URL, "org.postgresql.Driver")

        assert self.SECRET not in str(excinfo.value)
        assert excinfo.value.__cause__ is None
        assert excinfo.value.__suppress_context__

    def test_odbc_error_redacts_the_dsn(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from machina.connectors.sql import drivers

        raised = _FakeOdbcError(f"Login failed for connection {self.ODBC_DSN}")
        monkeypatch.setattr(drivers, "require_pyodbc", lambda: self._fake_pyodbc(raised))

        with pytest.raises(ConnectorError, match="ODBC connection failed") as excinfo:
            drivers.connect_odbc(self.ODBC_DSN)

        assert self.SECRET not in str(excinfo.value)
        assert excinfo.value.__cause__ is None

    def test_odbc_missing_driver_is_a_driver_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from machina.connectors.sql import drivers

        raised = _FakeOdbcInterfaceError(f"Driver not found for {self.ODBC_DSN}")
        monkeypatch.setattr(drivers, "require_pyodbc", lambda: self._fake_pyodbc(raised))

        with pytest.raises(ConnectorDriverError, match="driver not found") as excinfo:
            drivers.connect_odbc(self.ODBC_DSN)

        assert self.SECRET not in str(excinfo.value)


class _FakeOdbcError(Exception):
    """Stand-in for pyodbc.Error."""


class _FakeOdbcInterfaceError(_FakeOdbcError):
    """Stand-in for pyodbc.InterfaceError."""
