"""Driver registry — lazy imports for ODBC (pyodbc) and JDBC (jaydebeapi).

Each driver backend is loaded only when first needed, producing clear
error messages when the required extra is not installed.
"""

from __future__ import annotations

import contextlib
from typing import Any

from machina.connectors.sql.dialect import redact_dsn
from machina.exceptions import (
    ConnectorConfigError,
    ConnectorDependencyError,
    ConnectorDriverError,
    ConnectorError,
)


def require_pyodbc() -> Any:
    """Import pyodbc, raising a clear error if the extra is missing."""
    try:
        import pyodbc  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ConnectorDependencyError(
            "pyodbc is required for ODBC connections. Install with: pip install machina-ai[sql]"
        ) from exc
    return pyodbc


def require_jaydebeapi() -> tuple[Any, Any]:
    """Import jaydebeapi + jpype, raising a clear error if the extra is missing."""
    try:
        import jaydebeapi  # type: ignore[import-not-found]
        import jpype  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ConnectorDependencyError(
            "jaydebeapi and JPype1 are required for JDBC connections. "
            "Install with: pip install machina-ai[sql-jdbc]"
        ) from exc
    return jaydebeapi, jpype


def _safe_error_text(exc: BaseException, dsn: str) -> str:
    """Return the driver's error text with the DSN and any password redacted.

    Driver messages can quote the connection string verbatim (the standard
    JDBC "No suitable driver found for <url>" does), so the raw DSN is
    replaced by its redacted form before the usual password patterns are
    redacted too.
    """
    return redact_dsn(str(exc).replace(dsn, redact_dsn(dsn)))


def _timeout_refused(conn: Any, reason: str, error: str) -> ConnectorConfigError:
    """Close ``conn`` and build the error for a driver that refused ``query_timeout``."""
    with contextlib.suppress(Exception):
        conn.close()
    return ConnectorConfigError(
        f"query_timeout cannot be applied: {reason}. Remove query_timeout and bound "
        f"statements on the database side instead. Error: {error}"
    )


def connect_odbc(dsn: str, *, query_timeout: int | None = None) -> Any:
    """Open an ODBC connection via pyodbc.

    Args:
        dsn: The ODBC connection string.
        query_timeout: Seconds each statement may run, set as pyodbc's
            ``Connection.timeout``: the ODBC query timeout of every cursor
            created afterwards, and the connection timeout. ``None`` keeps the
            driver's setting.

    Raises:
        ConnectorDriverError: If the ODBC driver is missing.
        ConnectorConfigError: If the driver refuses ``query_timeout``.
        ConnectorError: If the connection fails for another reason.

    No error echoes the DSN's password, and none chains the driver
    exception, whose text could.
    """
    pyodbc = require_pyodbc()
    try:
        conn = pyodbc.connect(dsn, autocommit=False)
    except pyodbc.InterfaceError as exc:
        error_msg = _safe_error_text(exc, dsn)
        if "driver" in error_msg.lower():
            raise ConnectorDriverError(
                f"ODBC driver not found. Check your DSN and ensure the "
                f"driver is installed. Error: {error_msg}"
            ) from None
        raise ConnectorError(f"ODBC connection failed: {error_msg}") from None
    except pyodbc.Error as exc:
        raise ConnectorError(f"ODBC connection failed: {_safe_error_text(exc, dsn)}") from None
    if query_timeout is not None:
        try:
            # pyodbc sets the ODBC connection timeout here, and the statement
            # timeout on each cursor it creates; a driver may refuse either.
            conn.timeout = query_timeout
            conn.cursor().close()
        except pyodbc.Error as exc:
            raise _timeout_refused(
                conn,
                "the ODBC driver refused the timeout pyodbc sets with it",
                _safe_error_text(exc, dsn),
            ) from None
    return conn


class _QueryTimeoutConnection:
    """A ``java.sql.Connection`` whose prepared statements get a query timeout.

    jaydebeapi creates the statement for every ``execute`` with
    ``jconn.prepareStatement(sql)`` and runs it at once, so this is the one
    place to call ``Statement.setQueryTimeout``. Everything else goes to the
    wrapped connection.
    """

    def __init__(self, jconn: Any, seconds: int) -> None:
        self._jconn = jconn
        self._seconds = seconds

    def prepareStatement(self, *args: Any) -> Any:  # noqa: N802 - java.sql.Connection API
        statement = self._jconn.prepareStatement(*args)
        statement.setQueryTimeout(self._seconds)
        return statement

    def __getattr__(self, name: str) -> Any:
        return getattr(self._jconn, name)


def connect_jdbc(
    dsn: str,
    driver_class: str,
    driver_path: str | None = None,
    *,
    query_timeout: int | None = None,
) -> Any:
    """Open a JDBC connection via jaydebeapi.

    Args:
        dsn: The JDBC URL.
        driver_class: The driver's class name.
        driver_path: The driver ``.jar``, put on the JVM class path.
        query_timeout: Seconds each statement may run, set with
            ``Statement.setQueryTimeout`` on every statement. ``None`` keeps
            the driver's setting.

    Raises:
        ConnectorDriverError: If the connection fails.
        ConnectorConfigError: If the driver refuses ``query_timeout``.

    Neither error echoes the DSN's password, and neither chains the driver
    exception.
    """
    jaydebeapi, jpype = require_jaydebeapi()
    if not jpype.isJVMStarted():
        jvm_path = jpype.getDefaultJVMPath()
        classpath_args = [f"-Djava.class.path={driver_path}"] if driver_path else []
        jpype.startJVM(jvm_path, *classpath_args)
    try:
        conn = jaydebeapi.connect(driver_class, dsn)
    except Exception as exc:
        raise ConnectorDriverError(
            f"JDBC connection failed for driver {driver_class!r}. "
            f"Error: {_safe_error_text(exc, dsn)}"
        ) from None
    if query_timeout is not None:
        try:
            # Check once that the driver takes it, so a refusal fails here
            # rather than in every statement.
            probe = conn.jconn.createStatement()
            try:
                probe.setQueryTimeout(query_timeout)
            finally:
                probe.close()
        except Exception as exc:
            raise _timeout_refused(
                conn,
                "the JDBC driver refused Statement.setQueryTimeout",
                _safe_error_text(exc, dsn),
            ) from None
        conn.jconn = _QueryTimeoutConnection(conn.jconn, query_timeout)
    return conn
