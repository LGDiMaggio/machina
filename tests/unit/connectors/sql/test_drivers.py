"""Unit tests for SQL connector driver helpers."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from machina.exceptions import (
    ConnectorConfigError,
    ConnectorDependencyError,
    ConnectorDriverError,
    ConnectorError,
)


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


class TestOdbcQueryTimeout:
    """connect_odbc gives every statement on the connection the query timeout."""

    DSN = "Driver={ODBC Driver 18};Server=db;UID=svc;PWD=hunter2-secret"

    @staticmethod
    def _connect(monkeypatch: pytest.MonkeyPatch, conn: _FakeOdbcConnection, **kwargs: Any) -> Any:
        from machina.connectors.sql import drivers

        fake_pyodbc = SimpleNamespace(
            connect=lambda dsn, autocommit: conn,
            Error=_FakeOdbcError,
            InterfaceError=_FakeOdbcInterfaceError,
        )
        monkeypatch.setattr(drivers, "require_pyodbc", lambda: fake_pyodbc)
        return drivers.connect_odbc(TestOdbcQueryTimeout.DSN, **kwargs)

    def test_sets_the_connection_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        conn = _FakeOdbcConnection()
        assert self._connect(monkeypatch, conn, query_timeout=30) is conn
        assert conn.timeouts_set == [30]
        assert not conn.closed

    def test_none_leaves_the_driver_setting(self, monkeypatch: pytest.MonkeyPatch) -> None:
        conn = _FakeOdbcConnection()
        assert self._connect(monkeypatch, conn, query_timeout=None) is conn
        assert conn.timeouts_set == []

    @pytest.mark.parametrize("refuses", ["connection_timeout", "statement_timeout"])
    def test_a_driver_refusing_it_fails_the_connect(
        self, monkeypatch: pytest.MonkeyPatch, refuses: str
    ) -> None:
        """pyodbc sets the ODBC connection timeout with it, and the statement
        timeout on every new cursor; a driver may refuse either."""
        conn = _FakeOdbcConnection(refuses=refuses, quoting=self.DSN)

        with pytest.raises(ConnectorConfigError, match="query_timeout") as excinfo:
            self._connect(monkeypatch, conn, query_timeout=30)

        assert conn.closed
        assert "hunter2" not in str(excinfo.value)
        assert excinfo.value.__cause__ is None


class TestJdbcQueryTimeout:
    """connect_jdbc sets the query timeout on every statement jaydebeapi prepares."""

    URL = "jdbc:as400://host;naming=sql;password=hunter2-secret"

    @staticmethod
    def _connect(
        monkeypatch: pytest.MonkeyPatch, conn: _FakeJaydebeapiConnection, **kwargs: Any
    ) -> Any:
        from machina.connectors.sql import drivers

        fake_jpype = SimpleNamespace(isJVMStarted=lambda: True)
        fake_jaydebeapi = SimpleNamespace(connect=lambda driver_class, dsn: conn)
        monkeypatch.setattr(drivers, "require_jaydebeapi", lambda: (fake_jaydebeapi, fake_jpype))
        return drivers.connect_jdbc(
            TestJdbcQueryTimeout.URL, "com.ibm.as400.access.AS400JDBCDriver", **kwargs
        )

    def test_every_prepared_statement_gets_the_timeout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        jconn = _FakeJavaConnection()
        conn = self._connect(monkeypatch, _FakeJaydebeapiConnection(jconn), query_timeout=30)

        statement = conn.jconn.prepareStatement("SELECT * FROM WO")

        assert statement.sql == "SELECT * FROM WO"
        assert statement.query_timeout == 30
        conn.jconn.commit()  # everything else reaches the real connection
        assert jconn.committed

    def test_none_leaves_the_connection_as_it_is(self, monkeypatch: pytest.MonkeyPatch) -> None:
        jconn = _FakeJavaConnection()
        conn = self._connect(monkeypatch, _FakeJaydebeapiConnection(jconn), query_timeout=None)
        assert conn.jconn is jconn

    def test_a_driver_refusing_it_fails_the_connect(self, monkeypatch: pytest.MonkeyPatch) -> None:
        conn = _FakeJaydebeapiConnection(_FakeJavaConnection(refusing=self.URL))

        with pytest.raises(ConnectorConfigError, match="query_timeout") as excinfo:
            self._connect(monkeypatch, conn, query_timeout=30)

        assert conn.closed
        assert "hunter2" not in str(excinfo.value)
        assert excinfo.value.__cause__ is None


class _FakeOdbcError(Exception):
    """Stand-in for pyodbc.Error."""


class _FakeOdbcInterfaceError(_FakeOdbcError):
    """Stand-in for pyodbc.InterfaceError."""


class _FakeOdbcConnection:
    """Stand-in for a pyodbc connection; records the query timeouts it is given.

    ``refuses`` makes the driver reject the ODBC connection timeout (raised by
    the ``timeout`` setter) or the statement timeout (raised by ``cursor()``),
    with an error quoting ``quoting``, as driver messages may quote the DSN.
    """

    def __init__(self, *, refuses: str = "", quoting: str = "") -> None:
        self._refuses = refuses
        self._quoting = quoting
        self.timeouts_set: list[int] = []
        self.closed = False

    def _refusal(self, function: str) -> _FakeOdbcError:
        return _FakeOdbcError(
            "HYC00",
            f"[HYC00] Optional feature not implemented (0) ({function}) for {self._quoting}",
        )

    @property
    def timeout(self) -> int:
        return self.timeouts_set[-1] if self.timeouts_set else 0

    @timeout.setter
    def timeout(self, seconds: int) -> None:
        if self._refuses == "connection_timeout":
            raise self._refusal("SQLSetConnectAttr")
        self.timeouts_set.append(seconds)

    def cursor(self) -> SimpleNamespace:
        if self._refuses == "statement_timeout" and self.timeouts_set:
            raise self._refusal("SQLSetStmtAttr(SQL_ATTR_QUERY_TIMEOUT)")
        return SimpleNamespace(close=lambda: None)

    def close(self) -> None:
        self.closed = True


class _FakeStatement:
    """Stand-in for a java.sql.Statement reached through JPype."""

    def __init__(self, sql: str | None, *, refusing: str) -> None:
        self.sql = sql
        self.query_timeout: int | None = None
        self._refusing = refusing

    def setQueryTimeout(self, seconds: int) -> None:  # noqa: N802 - the Java method
        if self._refusing:
            raise RuntimeError(
                f"java.sql.SQLFeatureNotSupportedException: setQueryTimeout ({self._refusing})"
            )
        self.query_timeout = seconds

    def close(self) -> None:
        pass


class _FakeJavaConnection:
    """Stand-in for the java.sql.Connection a jaydebeapi.Connection wraps.

    ``refusing`` makes its statements reject setQueryTimeout with an error
    quoting that text.
    """

    def __init__(self, *, refusing: str = "") -> None:
        self._refusing = refusing
        self.committed = False

    def prepareStatement(self, sql: str) -> _FakeStatement:  # noqa: N802 - the Java method
        return _FakeStatement(sql, refusing=self._refusing)

    def createStatement(self) -> _FakeStatement:  # noqa: N802 - the Java method
        return _FakeStatement(None, refusing=self._refusing)

    def commit(self) -> None:
        self.committed = True


class _FakeJaydebeapiConnection:
    """Stand-in for jaydebeapi.Connection: the Java connection is ``jconn``."""

    def __init__(self, jconn: _FakeJavaConnection) -> None:
        self.jconn: Any = jconn
        self.closed = False

    def close(self) -> None:
        self.closed = True
