"""The Machina error message contract, checked over the source tree.

A ``MachinaError`` message is shown outside the process (the agent's tool
results, MCP errors, workflow step errors; see
:func:`machina.agent.tool_errors.public_error_text`), so it must not carry a
library exception's text, which can hold a URL, a host, a token, a connection
string or a backend value. A connector names the library error's type and
chains it: ``raise ConnectorError(f"SQL read failed ({type(exc).__name__})")
from exc``.

This test fails on a new raise site that puts an exception's text into an
error message (``f"...{exc}"``, ``{exc!r}``, ``str(exc)``, ``repr(exc)``).
The sites below are reviewed exceptions, each with its reason; the list may
only shrink.
"""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

import machina

_SRC = Path(machina.__file__).resolve().parent

# Names an exception is usually bound to, plus every ``except ... as name``.
_EXCEPTION_NAMES = frozenset({"exc", "e", "err", "error", "ex", "exception", "cause"})

Site = tuple[str, str, str]  # (path under src/machina, enclosing qualname, error class)

# Reviewed: each embeds an exception's text but reaches no model, MCP or
# workflow output, or the text is Machina-authored or value-free.
_ALLOWED: Counter[Site] = Counter(
    {
        # Caller-facing LLM failure, not one of the three boundaries.
        ("agent/runtime.py", "Agent.handle_message_full", "LLMError"): 1,
        # _describe() names field paths and problems only, never input values.
        ("connectors/_settings.py", "validate_settings", "ConnectorConfigError"): 1,
        ("connectors/_settings.py", "validate_setting", "ConnectorConfigError"): 1,
        # WorkOrder.transition_to's own message ("Cannot transition from ...").
        (
            "connectors/cmms/generic.py",
            "GenericCmmsConnector._local_update_work_order",
            "ConnectorError",
        ): 1,
        ("connectors/docs/excel.py", "ExcelCsvConnector._apply_update", "ConnectorError"): 1,
        # MalformedPageError: Machina-authored pagination messages.
        (
            "connectors/cmms/generic.py",
            "GenericCmmsConnector._rest_read_pages",
            "ConnectorSchemaError",
        ): 1,
        # The UpKeep mapper's own message, which repeats the caller's status.
        (
            "connectors/cmms/upkeep.py",
            "UpKeepConnector.read_work_orders",
            "ConnectorUnsupportedFilterError",
        ): 1,
        ("connectors/cmms/upkeep.py", "UpKeepConnector.update_work_order", "ConnectorError"): 1,
        # Collection reset and index build: reached from connect() only.
        (
            "connectors/docs/document_store.py",
            "DocumentStoreConnector._build_rag_index",
            "ConnectorError",
        ): 2,
        # OSError.strerror: the OS's text for the errno, with no path.
        ("connectors/docs/excel.py", "_file_write_error", "ConnectorError"): 1,
        # ImportError text at connector construction.
        ("connectors/factory.py", "create_connector", "MachinaError"): 1,
        # Driver text with the DSN redacted (_safe_error_text), at connect time.
        ("connectors/sql/drivers.py", "_timeout_refused", "ConnectorConfigError"): 1,
        ("connectors/sql/drivers.py", "connect_odbc", "ConnectorError"): 1,
        ("connectors/sql/drivers.py", "connect_jdbc", "ConnectorDriverError"): 1,
        # json.JSONDecodeError text (a position) for the token variable, at startup.
        ("mcp/auth.py", "load_tokens_from_env", "ConnectorError"): 1,
    }
)


class _Finder(ast.NodeVisitor):
    """Collect calls to ``...Error``/``...Exception`` whose arguments embed an exception."""

    def __init__(self) -> None:
        self._bound: list[str] = []
        self._scope: list[str] = []
        self.sites: list[tuple[str, str, int]] = []

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._scope.append(node.name)
        self.generic_visit(node)
        self._scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self._scope.append(node.name)
        self.generic_visit(node)
        self._scope.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.visit_FunctionDef(node)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.name:
            self._bound.append(node.name)
        self.generic_visit(node)
        if node.name:
            self._bound.pop()

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        name = (
            func.id
            if isinstance(func, ast.Name)
            else func.attr
            if isinstance(func, ast.Attribute)
            else ""
        )
        # Private error classes too: _UnreadableRowError's text ends up in a
        # ConnectorError message.
        if name.lstrip("_")[:1].isupper() and name.endswith(("Error", "Exception")):
            arguments = [*node.args, *(keyword.value for keyword in node.keywords)]
            if any(self._embeds(argument) for argument in arguments):
                self.sites.append((".".join(self._scope), name, node.lineno))
        self.generic_visit(node)

    def _is_exception(self, node: ast.AST) -> bool:
        return isinstance(node, ast.Name) and node.id in (_EXCEPTION_NAMES | set(self._bound))

    def _embeds(self, node: ast.AST) -> bool:
        for sub in ast.walk(node):
            if isinstance(sub, ast.FormattedValue):
                if _is_type_name(sub.value):
                    continue  # {type(exc).__name__}: the type, never the text
                if any(self._is_exception(n) for n in ast.walk(sub.value)):
                    return True
            if (
                isinstance(sub, ast.Call)
                and isinstance(sub.func, ast.Name)
                and sub.func.id in {"str", "repr", "format"}
                and sub.args
                and self._is_exception(sub.args[0])
            ):
                return True
        return False


def _is_type_name(node: ast.expr) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "__name__"
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "type"
    )


def _sites(source: str) -> list[tuple[str, str, int]]:
    finder = _Finder()
    finder.visit(ast.parse(source))
    return finder.sites


def _tree_sites() -> tuple[Counter[Site], list[str]]:
    found: Counter[Site] = Counter()
    where: list[str] = []
    for path in sorted(_SRC.rglob("*.py")):
        relative = path.relative_to(_SRC).as_posix()
        for qualname, error, line in _sites(path.read_text(encoding="utf-8")):
            found[(relative, qualname, error)] += 1
            where.append(f"src/machina/{relative}:{line} {qualname} raises {error}")
    return found, where


def test_no_new_error_message_embeds_an_exception_text() -> None:
    found, where = _tree_sites()

    unreviewed = found - _ALLOWED
    assert not unreviewed, (
        "An error message embeds an exception's text. Name its type instead "
        "(f'... ({type(exc).__name__})') and chain it with 'from exc':\n"
        + "\n".join(line for line in where if any(s[0] in line for s in unreviewed))
    )


def test_the_reviewed_list_has_no_stale_entry() -> None:
    """A fixed site leaves the list, so the list only shrinks."""
    found, _ = _tree_sites()

    assert not (_ALLOWED - found)


def test_the_check_fires_on_each_way_of_embedding_the_text() -> None:
    source = """
def read(conn):
    try:
        conn.read()
    except OSError as failure:
        raise ConnectorError(f"read failed: {failure}") from failure
    except KeyError as exc:
        raise ConnectorError(f"read failed: {exc!r}") from exc
    except ValueError as exc:
        raise ConnectorError("read failed: " + str(exc)) from exc
    except TypeError as exc:
        raise MyLibraryException(repr(exc)) from exc

class Connector:
    async def write(self, exc):
        return ConnectorTimeoutError(f"timed out: {exc.args}")
"""

    assert [(qualname, error) for qualname, error, _ in _sites(source)] == [
        ("read", "ConnectorError"),
        ("read", "ConnectorError"),
        ("read", "ConnectorError"),
        ("read", "MyLibraryException"),
        ("Connector.write", "ConnectorTimeoutError"),
    ]


def test_the_check_accepts_the_type_name_and_unrelated_values() -> None:
    source = """
def read(conn, node_id):
    try:
        conn.read(node_id)
    except OSError as exc:
        raise ConnectorError(f"Failed to read node {node_id} ({type(exc).__name__})") from exc
    raise ValueError(str(node_id))
"""

    assert _sites(source) == []
