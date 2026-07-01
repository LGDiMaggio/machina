"""Capability invariant across every shipped connector.

The self-description spine promises a hard contract: a capability is surfaced
only when a connector both **declares** it and exposes a **live** (present,
non-stub) backing method. These tests enforce that contract directly against
the real classes in :data:`machina.runtime._CONNECTOR_FACTORIES`, so a
connector cannot declare a capability it cannot serve and quietly pass review.

They pin:

* **declared ⇒ live-backed** — every capability a connector declares (its
  ``capabilities`` ClassVar, or ``_BASE_CAPABILITIES`` for the instance-computed
  connectors) resolves through
  :data:`machina.introspect._methods.CAPABILITY_TO_METHOD` to a method that is
  present and not a :class:`NotImplementedError` stub;
* **vocabulary fully mapped** — every :class:`Capability` enum member appears in
  ``CAPABILITY_TO_METHOD`` (``describe().gaps.unmapped_capabilities`` is empty);
* **no class/runtime base drift** — for the instance-computed connectors whose
  base set is exposed as a ``_BASE_CAPABILITIES`` ClassVar (read by the core
  without instantiating), that ClassVar equals the base set the connector's
  ``__init__`` actually builds under a minimal read-only config, so the
  class-readable copy cannot silently drift from the runtime computation. The
  drift gate alone cannot catch this divergence (it never instantiates).
"""

from __future__ import annotations

import pytest

from machina.connectors.capabilities import Capability
from machina.introspect import describe
from machina.introspect._methods import (
    CAPABILITY_TO_METHOD,
    has_live_method,
    is_stub_method,
    method_name_for,
)
from machina.introspect.core import _class_base_capabilities, _import_class
from machina.runtime import _CONNECTOR_FACTORIES


def _declared_connectors() -> list[tuple[str, type]]:
    """(conn_type, class) for every factory-registered connector, importable.

    Mirrors the core's tolerant harvest: a connector whose module fails to
    import is skipped here (the core marks it ``degraded``) rather than failing
    the whole suite, since import failure is a separate, already-tested concern.
    """
    out: list[tuple[str, type]] = []
    for conn_type, dotted_path in sorted(_CONNECTOR_FACTORIES.items()):
        try:
            cls = _import_class(dotted_path)
        except Exception:  # pragma: no cover - import failure tested elsewhere
            continue
        out.append((conn_type, cls))
    return out


_CONNECTOR_CASES = _declared_connectors()


# ---------------------------------------------------------------------------
# declared ⇒ present AND non-stub backing method
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("conn_type", "cls"),
    _CONNECTOR_CASES,
    ids=[t for t, _ in _CONNECTOR_CASES],
)
def test_declared_capabilities_resolve_to_live_methods(conn_type: str, cls: type) -> None:
    """Every capability the connector declares is backed by a live method.

    Reads the class-readable base set (``capabilities`` ClassVar, or
    ``_BASE_CAPABILITIES`` for instance-computed connectors) and asserts each
    capability maps to a method that is present and not a stub.
    """
    base = _class_base_capabilities(cls, conn_type)
    assert base, f"{cls.__name__} ({conn_type}) exposes no class-readable capabilities"

    for cap in sorted(base, key=lambda c: c.value):
        method_name = method_name_for(cap)
        assert method_name is not None, (
            f"{cls.__name__} declares {cap.value!r}, which is unmapped in CAPABILITY_TO_METHOD"
        )
        assert hasattr(cls, method_name), (
            f"{cls.__name__} declares {cap.value!r} → method {method_name!r}, "
            "but the class has no such attribute"
        )
        assert not is_stub_method(cls, method_name), (
            f"{cls.__name__} declares {cap.value!r} → method {method_name!r}, "
            "but that method is an unimplemented stub"
        )
        # The combined helper the core uses must agree with the granular checks.
        assert has_live_method(cls, cap), (
            f"{cls.__name__} declares {cap.value!r} but has_live_method() is False"
        )


# ---------------------------------------------------------------------------
# every Capability enum member is mapped (no unmapped vocabulary)
# ---------------------------------------------------------------------------


def test_every_capability_member_is_mapped() -> None:
    """Each ``Capability`` member has an entry in ``CAPABILITY_TO_METHOD``."""
    missing = [cap.value for cap in Capability if cap not in CAPABILITY_TO_METHOD]
    assert not missing, f"Capability members absent from CAPABILITY_TO_METHOD: {missing}"


def test_spine_reports_no_unmapped_capabilities() -> None:
    """``describe().gaps.unmapped_capabilities`` is empty (the spine agrees)."""
    assert describe().gaps.unmapped_capabilities == ()


# ---------------------------------------------------------------------------
# instance-computed connectors: class-readable base == __init__-built base
# ---------------------------------------------------------------------------


def test_sql_base_classvar_matches_init_built_base() -> None:
    """``GenericSqlConnector._BASE_CAPABILITIES`` == its read-only ``__init__`` set.

    Under a minimal read-only config (no ``read_write``, no FailureMode table),
    ``__init__`` adds no config-driven capabilities, so the live capability set
    must equal the class-readable ``_BASE_CAPABILITIES`` the core reads without
    instantiating. Any divergence means the class copy has drifted from runtime.
    """
    from machina.connectors.sql.generic import GenericSqlConnector
    from machina.connectors.sql.schema import (
        FieldMapping,
        SqlConnectorConfig,
        TableMapping,
    )

    # Minimal read-only config: one non-FailureMode table (so READ_FAILURE_MODES
    # is NOT added), capabilities defaulting to "read_only" (no write caps).
    config = SqlConnectorConfig(
        dsn="DRIVER={x};SERVER=localhost",
        tables={
            "assets": TableMapping(
                query="SELECT * FROM assets",
                entity="Asset",
                fields={"id": FieldMapping(column="ASSET_ID")},
            )
        },
    )
    connector = GenericSqlConnector(config=config)

    assert connector.capabilities == GenericSqlConnector._BASE_CAPABILITIES


def test_excel_base_classvar_matches_init_built_base() -> None:
    """``ExcelCsvConnector._BASE_CAPABILITIES`` == its read-only ``__init__`` set.

    Under a minimal read-only config (an ``asset_registry`` sheet only, no
    ``work_orders`` ``write_mode``, no ``failure_modes``), ``__init__`` adds no
    config-driven capabilities, so the live capability set must equal the
    class-readable ``_BASE_CAPABILITIES`` the core reads without instantiating.
    Any divergence means the class copy has drifted from runtime.
    """
    from machina.connectors.docs.excel import ExcelCsvConnector
    from machina.connectors.docs.excel_schema import (
        ColumnMapping,
        ExcelConnectorConfig,
        SheetSchema,
    )

    config = ExcelConnectorConfig(
        asset_registry=SheetSchema(
            path="assets.csv",
            columns=[ColumnMapping(column="Codice", field="id", required=True)],
        )
    )
    connector = ExcelCsvConnector(config=config)

    assert connector.capabilities == ExcelCsvConnector._BASE_CAPABILITIES


def test_calendar_base_classvar_matches_init_built_base() -> None:
    """``CalendarConnector._BASE_CAPABILITIES`` == the read-only ``ical`` base.

    The ``ical`` backend is the read-only minimum; ``__init__`` sets the
    instance capabilities to that minimum, which must equal the class-readable
    ``_BASE_CAPABILITIES`` the core reads. The writable google/outlook backends
    add create/delete, which the core annotates "configurable" — those are not
    part of the base and are covered by the configurable-capabilities path.
    """
    from machina.connectors.calendar.connector import CalendarConnector

    connector = CalendarConnector(backend="ical")

    assert connector.capabilities == CalendarConnector._BASE_CAPABILITIES
