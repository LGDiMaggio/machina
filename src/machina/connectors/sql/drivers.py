"""Driver registry — lazy imports for ODBC (pyodbc) and JDBC (jaydebeapi).

Each driver backend is loaded only when first needed, producing clear
error messages when the required extra is not installed.
"""

from __future__ import annotations

from typing import Any

from machina.connectors.sql.dialect import redact_dsn
from machina.exceptions import ConnectorDependencyError, ConnectorDriverError, ConnectorError


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


def connect_odbc(dsn: str) -> Any:
    """Open an ODBC connection via pyodbc.

    Raises:
        ConnectorDriverError: If the ODBC driver is missing.
        ConnectorError: If the connection fails for another reason.

    Neither error echoes the DSN's password, and neither chains the driver
    exception, whose text could.
    """
    pyodbc = require_pyodbc()
    try:
        return pyodbc.connect(dsn, autocommit=False)
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


def connect_jdbc(
    dsn: str,
    driver_class: str,
    driver_path: str | None = None,
) -> Any:
    """Open a JDBC connection via jaydebeapi.

    Raises:
        ConnectorDriverError: If the connection fails. The message never
            echoes the DSN's password, and the driver exception is not
            chained.
    """
    jaydebeapi, jpype = require_jaydebeapi()
    if not jpype.isJVMStarted():
        jvm_path = jpype.getDefaultJVMPath()
        classpath_args = [f"-Djava.class.path={driver_path}"] if driver_path else []
        jpype.startJVM(jvm_path, *classpath_args)
    try:
        return jaydebeapi.connect(driver_class, dsn)
    except Exception as exc:
        raise ConnectorDriverError(
            f"JDBC connection failed for driver {driver_class!r}. "
            f"Error: {_safe_error_text(exc, dsn)}"
        ) from None
