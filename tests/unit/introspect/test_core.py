"""Tests for the introspection core (``machina.introspect.describe``).

These tests pin the review-hardened invariants of the self-description spine:

* it runs without instantiating any connector and without importing a heavy
  optional dependency,
* it is fully deterministic (two calls return equal ``Spine`` objects),
* every emitted capability is declared, mapped, and backed by a live method,
* stubbed methods are excluded,
* config-gated capabilities are annotated, never double-emitted,
* orphaned sensor capabilities are flagged,
* the capability vocabulary is fully mapped (no unmapped entries),
* the config schema is shape-only, and seams are reflected from code.
"""

from __future__ import annotations

import sys

import pytest

from machina.connectors.capabilities import Capability
from machina.introspect import (
    CapabilityInfo,
    ConnectorInfo,
    Gaps,
    Seams,
    Spine,
    describe,
)
from machina.introspect._methods import CAPABILITY_TO_METHOD, has_live_method
from machina.runtime import _CONNECTOR_FACTORIES

# Heavy optional dependencies a connector imports lazily; importing a connector
# *class* (what describe does) must not pull any of these in.
_HEAVY_MODULES = (
    "asyncua",
    "aiomqtt",
    "chromadb",
    "langchain",
    "langchain_chroma",
    "docling",
    "pyodbc",
    "openpyxl",
    "httpx",
)

# The two capabilities declared only by SimulatedSensorConnector, which is not
# in the default factory registry — the canonical orphans.
_ORPHAN_CAPS = frozenset(
    {
        Capability.GET_LATEST_READING.value,
        Capability.GET_RELATED_READINGS.value,
    }
)


@pytest.fixture
def spine() -> Spine:
    """A fresh ``Spine`` from ``describe()``."""
    return describe()


# ---------------------------------------------------------------------------
# It runs, and returns the right shape
# ---------------------------------------------------------------------------


def test_describe_runs_and_returns_spine(spine: Spine) -> None:
    assert isinstance(spine, Spine)
    assert isinstance(spine.seams, Seams)
    assert isinstance(spine.gaps, Gaps)
    assert spine.connectors
    assert spine.capabilities


def test_connectors_match_factory_registry(spine: Spine) -> None:
    """Every registry type is described; no extras are invented."""
    described = {c.type for c in spine.connectors}
    assert described == set(_CONNECTOR_FACTORIES)


def test_public_reexport_matches_core() -> None:
    """The package re-export is the same object as the core entry point."""
    from machina.introspect import core

    assert describe is core.describe
    for name in ("Spine", "ConnectorInfo", "CapabilityInfo", "Seams", "Gaps"):
        assert hasattr(core, name)


# ---------------------------------------------------------------------------
# Invariant: no instantiation, no heavy imports
# ---------------------------------------------------------------------------


def test_describe_imports_no_heavy_dependency() -> None:
    """describe() must not import any heavy optional transport dependency."""
    before = {m for m in _HEAVY_MODULES if m in sys.modules}
    describe()
    after = {m for m in _HEAVY_MODULES if m in sys.modules}
    newly_imported = after - before
    assert not newly_imported, f"describe() imported heavy modules: {newly_imported}"


# ---------------------------------------------------------------------------
# Invariant: determinism
# ---------------------------------------------------------------------------


def test_describe_is_deterministic() -> None:
    """Two calls in one process return equal (value-compared) Spines."""
    assert describe() == describe()


def test_collections_are_sorted(spine: Spine) -> None:
    conn_types = [c.type for c in spine.connectors]
    assert conn_types == sorted(conn_types)
    cap_values = [c.value for c in spine.capabilities]
    assert cap_values == sorted(cap_values)
    for c in spine.connectors:
        caps = [cc.capability for cc in c.capabilities]
        assert caps == sorted(caps)


# ---------------------------------------------------------------------------
# Invariant: declared AND mapped AND live, stubs excluded
# ---------------------------------------------------------------------------


def test_every_emitted_capability_is_mapped_and_live(spine: Spine) -> None:
    """Each emitted capability has a backing method and is live on its class."""
    by_type: dict[str, ConnectorInfo] = {c.type: c for c in spine.connectors}
    for conn in spine.connectors:
        if conn.degraded:
            continue
        cls = _load_class(conn.dotted_path)
        for cc in conn.capabilities:
            cap = Capability(cc.capability)
            assert cap in CAPABILITY_TO_METHOD
            assert cc.method == CAPABILITY_TO_METHOD[cap]
            assert has_live_method(cls, cap), (
                f"{conn.type} emits {cc.capability} but its method is not live"
            )
    assert by_type  # sanity


def test_stub_method_capability_is_excluded(spine: Spine) -> None:
    """generic_sql.update_work_order is a stub → UPDATE_WORK_ORDER not emitted."""
    sql = next(c for c in spine.connectors if c.type == "generic_sql")
    emitted = {cc.capability for cc in sql.capabilities}
    assert Capability.UPDATE_WORK_ORDER.value not in emitted
    # create_work_order, by contrast, is implemented and stays.
    assert Capability.CREATE_WORK_ORDER.value in emitted


def test_nonmatching_capability_resolves_via_map(spine: Spine) -> None:
    """A capability whose value != its method name resolves and is emitted.

    OPC-UA serves ``subscribe_to_nodes`` via the ``subscribe`` method. A naive
    ``hasattr(cls, capability.value)`` check would miss it; the explicit
    ``CAPABILITY_TO_METHOD`` map must resolve it, and the real OpcUaConnector
    must emit it with the mapped (differently-named) method.
    """
    assert CAPABILITY_TO_METHOD[Capability.SUBSCRIBE_TO_NODES] == "subscribe"
    opcua = next(c for c in spine.connectors if c.type == "opcua")
    by_cap = {cc.capability: cc for cc in opcua.capabilities}
    assert Capability.SUBSCRIBE_TO_NODES.value in by_cap
    assert by_cap[Capability.SUBSCRIBE_TO_NODES.value].method == "subscribe"


def test_no_connector_has_duplicate_capability(spine: Spine) -> None:
    """A capability must appear at most once per connector (base/config split)."""
    for conn in spine.connectors:
        values = [cc.capability for cc in conn.capabilities]
        assert len(values) == len(set(values)), f"{conn.type} has duplicate caps: {values}"


# ---------------------------------------------------------------------------
# Invariant: base = guaranteed minimum, writes = configurable (no overlap)
# ---------------------------------------------------------------------------


def test_calendar_base_is_minimum_writes_are_configurable(spine: Spine) -> None:
    """Calendar: read is guaranteed; create/delete are configurable-only."""
    cal = next(c for c in spine.connectors if c.type == "calendar")
    guaranteed = {cc.capability for cc in cal.capabilities if not cc.configurable}
    configurable = {cc.capability for cc in cal.capabilities if cc.configurable}
    assert guaranteed == {Capability.READ_CALENDAR_EVENTS.value}
    assert configurable == {
        Capability.CREATE_CALENDAR_EVENT.value,
        Capability.DELETE_CALENDAR_EVENT.value,
    }
    # No capability is both guaranteed and configurable.
    assert not (guaranteed & configurable)


def test_instance_computed_base_read_from_class_without_instantiation(spine: Spine) -> None:
    """generic_sql and calendar expose their base set as a class ClassVar.

    ``describe()`` reads ``_BASE_CAPABILITIES`` off the class without ever
    instantiating it; the guaranteed (non-configurable) capabilities it emits
    must equal that ClassVar.
    """
    for conn_type, expected in (
        ("generic_sql", {Capability.READ_ASSETS, Capability.READ_WORK_ORDERS}),
        ("excel", {Capability.READ_ASSETS, Capability.READ_WORK_ORDERS}),
        ("excel_csv", {Capability.READ_ASSETS, Capability.READ_WORK_ORDERS}),
        ("calendar", {Capability.READ_CALENDAR_EVENTS}),
    ):
        info = next(c for c in spine.connectors if c.type == conn_type)
        cls = _load_class(info.dotted_path)
        # The base set is class-readable (no instantiation needed).
        base = cls._BASE_CAPABILITIES
        assert isinstance(base, frozenset)
        assert base == expected
        # And it is exactly what describe emits as guaranteed (live) caps.
        guaranteed = {Capability(cc.capability) for cc in info.capabilities if not cc.configurable}
        assert guaranteed == {c for c in base if has_live_method(cls, c)}
        assert info.instance_computed is True


def test_excel_writes_are_configurable_not_guaranteed(spine: Spine) -> None:
    """Excel writes need a work_orders sheet with a write_mode, so the spine
    renders them configurable — never guaranteed — mirroring SQL/generic_cmms.

    A config-less ``describe()`` cannot know whether a writable work_orders
    sheet is present, so CREATE/UPDATE_WORK_ORDER must be annotated
    "configurable", while the asset/work-order reads stay guaranteed.
    """
    for conn_type in ("excel", "excel_csv"):
        info = next(c for c in spine.connectors if c.type == conn_type)
        guaranteed = {cc.capability for cc in info.capabilities if not cc.configurable}
        configurable = {cc.capability for cc in info.capabilities if cc.configurable}
        assert Capability.CREATE_WORK_ORDER.value in configurable
        assert Capability.UPDATE_WORK_ORDER.value in configurable
        assert Capability.CREATE_WORK_ORDER.value not in guaranteed
        assert Capability.UPDATE_WORK_ORDER.value not in guaranteed
        # Reads remain guaranteed regardless of configuration.
        assert Capability.READ_ASSETS.value in guaranteed
        assert Capability.READ_WORK_ORDERS.value in guaranteed
        # No capability is both guaranteed and configurable.
        assert not (guaranteed & configurable)


def test_calendar_nonical_backend_yields_full_capabilities_at_runtime() -> None:
    """The class-level base is the minimum, but a writable backend still gets
    the FULL set at runtime — introspection must not have weakened behavior.
    """
    from machina.connectors.calendar.connector import (
        _FULL_CAPABILITIES,
        _READONLY_CAPABILITIES,
        CalendarConnector,
    )

    # Class attribute = guaranteed minimum.
    assert CalendarConnector._BASE_CAPABILITIES == _READONLY_CAPABILITIES
    # A writable (non-ical) backend instance exposes create/delete at runtime.
    writable = CalendarConnector(backend="google")
    assert writable.capabilities == _FULL_CAPABILITIES
    # The ical backend stays read-only.
    readonly = CalendarConnector(backend="ical")
    assert readonly.capabilities == _READONLY_CAPABILITIES


def test_connector_with_absent_extra_reports_not_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A connector whose optional extra is NOT installed still appears — with
    ``extra_installed=False`` and ``requires_extra`` set — and never raises.

    Forced deterministically by stubbing the extra probe, so the ambient test
    environment (which extras happen to be installed) cannot decide the outcome.
    Every connector that declares an extra must still be described, carry its
    extra name, report it not-installed, and — when its class imported cleanly
    (transport deps are lazy) — still expose its declared capabilities.
    """
    from machina.introspect import core

    monkeypatch.setattr(core, "_probe_extra", lambda meta: False if meta.probe_module else None)
    spine = describe()

    with_extra = [c for c in spine.connectors if c.requires_extra is not None]
    assert with_extra, "expected connectors that declare an optional extra"
    for conn in with_extra:
        assert conn.requires_extra
        assert conn.extra_installed is False
        if not conn.degraded:
            # Class import succeeded (lazy transport deps), so caps are present.
            assert conn.capabilities


def test_degraded_connector_scrubs_user_path_from_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When a connector class fails to import, describe() degrades it (never
    raises) AND the error field is scrubbed of a user-home path at the core.

    Pins the only site that applies ``safe_text`` to the import-failure message:
    a regression dropping the scrub would leak an absolute install path (and OS
    username) into the CLI, the MCP resource, and a regenerated artifact.
    """
    from machina.introspect import core

    def boom(dotted_path: str) -> type:
        raise ModuleNotFoundError(
            r"No module named 'acme' (C:\Users\ci-bot\venv\Lib\acme\__init__.py)"
        )

    monkeypatch.setattr(core, "_import_class", boom)
    spine = describe()

    degraded = [c for c in spine.connectors if c.degraded]
    assert degraded, "every connector must degrade when import raises — not crash"
    for conn in degraded:
        assert conn.error, "a degraded connector must carry an error string"
        assert "ci-bot" not in conn.error, "user-home path must be scrubbed at the core"
        assert "ModuleNotFoundError" in conn.error, "the exception type is preserved"


def test_baseconnector_seam_surfaces_the_capabilities_property(spine: Spine) -> None:
    """The BaseConnector seam manifest must list ``capabilities`` even though it
    is a ``@property`` — it is the single most important member a connector
    author implements, and a function-only reflection would silently omit it.
    """
    base = next((p for p in spine.seams.protocols if p.name == "BaseConnector"), None)
    assert base is not None, "BaseConnector must appear as a protocol seam"
    assert "capabilities" in {m.name for m in base.methods}


def test_is_stub_method_uses_ast_not_string_scan() -> None:
    """A docstring/comment mentioning the phrase must NOT read as a stub, while a
    genuine ``raise NotImplementedError`` body must. Pins the AST-based check
    against the previous raw-source scan (which false-positived on prose).
    """
    from machina.introspect._methods import is_stub_method

    class Fixture:
        def looks_live(self) -> int:
            """Live method whose prose says: raise NotImplementedError (a red herring)."""
            return 42

        def genuine_stub(self) -> None:
            raise NotImplementedError

    assert is_stub_method(Fixture, "looks_live") is False
    assert is_stub_method(Fixture, "genuine_stub") is True


def test_capability_index_has_no_provider_in_both_buckets(spine: Spine) -> None:
    """A type providing a cap live must not also list it as configurable."""
    for ci in spine.capabilities:
        assert not (set(ci.provided_by) & set(ci.configurable_in)), (
            f"{ci.value}: same type in provided_by and configurable_in"
        )


def test_no_healthy_connector_has_zero_capabilities(spine: Spine) -> None:
    """A healthy connector exposing no capability signals a base-set bug."""
    for conn in spine.connectors:
        if conn.degraded:
            continue
        assert conn.capabilities, f"{conn.type} exposes zero capabilities"


# ---------------------------------------------------------------------------
# Invariant: orphans flagged, vocabulary fully mapped
# ---------------------------------------------------------------------------


def test_orphans_are_exactly_the_sensor_caps(spine: Spine) -> None:
    assert set(spine.gaps.orphaned_capabilities) == _ORPHAN_CAPS


def test_orphaned_capability_infos_are_marked(spine: Spine) -> None:
    by_value: dict[str, CapabilityInfo] = {c.value: c for c in spine.capabilities}
    for value in _ORPHAN_CAPS:
        ci = by_value[value]
        assert ci.orphaned
        assert not ci.provided_by
        assert not ci.configurable_in
        assert "SimulatedSensorConnector" in ci.orphan_note


def test_no_unmapped_capabilities(spine: Spine) -> None:
    """Every Capability member is mapped to a method — no coverage gap."""
    assert spine.gaps.unmapped_capabilities == ()
    for cap in Capability:
        assert cap in CAPABILITY_TO_METHOD


# ---------------------------------------------------------------------------
# Config schema is shape-only; seams reflect from code
# ---------------------------------------------------------------------------


def test_config_schema_is_shape_only(spine: Spine) -> None:
    schema = spine.config_schema
    assert isinstance(schema, dict)
    # A JSON schema has a "properties" or "$defs" key but carries no values.
    assert "properties" in schema or "$defs" in schema


def test_seams_reflect_protocols_from_code(spine: Spine) -> None:
    proto_names = {p.name for p in spine.seams.protocols}
    assert {"BaseConnector", "SupportsConfirmation", "RefreshableConnector"} <= proto_names
    base = next(p for p in spine.seams.protocols if p.name == "BaseConnector")
    assert base.methods  # reflected at least one required method
    assert spine.seams.conventions  # convention seams listed


def test_base_connector_seam_lists_required_methods_and_template(spine: Spine) -> None:
    """AE2: the BaseConnector seam reflects its required methods, and the
    canonical 'add a connector' location template is surfaced.
    """
    base = next(p for p in spine.seams.protocols if p.name == "BaseConnector")
    method_names = {m.name for m in base.methods}
    # The Protocol's required lifecycle methods, reflected from code.
    assert {"connect", "disconnect", "health_check"} <= method_names
    # connect/disconnect/health_check are async on the Protocol.
    by_name = {m.name: m for m in base.methods}
    assert by_name["connect"].is_async
    assert by_name["disconnect"].is_async
    assert by_name["health_check"].is_async
    # Methods are sorted by name (deterministic).
    assert [m.name for m in base.methods] == sorted(method_names)
    # The new-connector template names the connectors/{category}/{name}.py path.
    assert spine.seams.add_connector_template == "connectors/{category}/{name}.py"


# ---------------------------------------------------------------------------
# Invariant: inspected seam docstrings are path-scrubbed at the core
# ---------------------------------------------------------------------------


def test_first_doc_line_scrubs_user_home_path() -> None:
    """_first_doc_line reduces a user-home/UNC absolute path to its basename.

    Seam Protocol docstrings flow, via inspect, into ProtocolSeam.doc and
    SeamMethod.doc — which renderers emit into LLM-visible artifacts. The core
    applies ``safe_text`` so an embedded absolute path cannot leak. We exercise
    the core helper directly with a synthetic docstring object.
    """
    from machina.introspect import core

    class _Synthetic:
        """Loads the manual from C:\\Users\\tedib\\plant\\pump_manual.md now."""

    line = core._first_doc_line(_Synthetic)
    # The user-home path is reduced to its basename; the prose around it stays.
    assert "C:\\Users\\tedib" not in line
    assert "pump_manual.md" in line


def test_first_doc_line_applies_safe_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """The core routes inspected docstrings through safe_text (single choke).

    Rather than re-implement the path heuristics, assert that the core's
    ``safe_text`` symbol is the function applied to every non-empty docstring
    line — proving the scrub happens at the core, not per-renderer.
    """
    from machina.introspect import core

    calls: list[str] = []

    def _spy(text: str) -> str:
        calls.append(text)
        return f"SCRUBBED::{text}"

    monkeypatch.setattr(core, "safe_text", _spy)

    class _Synthetic:
        """A connector seam docstring line."""

    out = core._first_doc_line(_Synthetic)
    assert calls == ["A connector seam docstring line."]
    assert out == "SCRUBBED::A connector seam docstring line."


def _load_class(dotted_path: str) -> type:
    import importlib

    module_path, class_name = dotted_path.rsplit(".", 1)
    module = importlib.import_module(module_path)
    return getattr(module, class_name)
