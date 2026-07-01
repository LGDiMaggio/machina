"""Neutral introspection core — a code-derived description of Machina.

:func:`describe` returns a structured, deterministic snapshot of what the
framework can do (connectors x capabilities), how it is configured, and
where its extension seams are — derived entirely from code:

* connector *types* from :data:`machina.runtime._CONNECTOR_FACTORIES`,
* declared capabilities read off each connector *class* (never instantiated),
* the explicit :data:`~machina.introspect._methods.CAPABILITY_TO_METHOD`
  map (capability values are not method names),
* the config schema from :meth:`MachinaConfig.model_json_schema`,
* seam Protocols reflected via :mod:`inspect`.

It imports **no heavy optional dependency**.  Connector modules import their
transport deps (asyncua, aiomqtt, httpx, chromadb, langchain, docling,
pymodbus, openpyxl, pyodbc) lazily inside methods, so importing a connector
*class* is safe on a bare ``pip install machina-ai``.  Optional-extra
presence is probed with :func:`importlib.util.find_spec`, never an import.

The returned structure is **deterministic**: every collection is sorted by a
stable key (capabilities by enum value, connectors by type string, methods by
name), so two ``describe()`` calls in one process return identical data and a
drift gate never flaps on iteration order.
"""

from __future__ import annotations

import importlib
import importlib.util
import inspect
from dataclasses import dataclass, field
from typing import Any

from machina.agent.prompts import safe_text
from machina.config.schema import MachinaConfig
from machina.connectors.capabilities import Capability
from machina.introspect._methods import (
    CAPABILITY_TO_METHOD,
    has_live_method,
    method_name_for,
)


# ---------------------------------------------------------------------------
# Per-connector-type metadata (kept beside the type→factory registry it mirrors)
# ---------------------------------------------------------------------------
#
# ``requires_extra`` is the pip extra a deployer installs to use the connector
# (``pip install "machina-ai[<extra>]"``); ``None`` means the connector works on
# the core install. ``probe_module`` is the import name find_spec checks to tell
# whether that extra is actually installed — pip does not record extras, so the
# import name is the reliable signal. Local/offline modes of some connectors
# (GenericCmms data_dir, DocumentStore keyword fallback) work without the extra;
# the extra is still recorded because the full feature set needs it.
@dataclass(frozen=True)
class _ConnectorMeta:
    """Static metadata for one connector type."""

    requires_extra: str | None
    probe_module: str | None


_CONNECTOR_META: dict[str, _ConnectorMeta] = {
    "generic_cmms": _ConnectorMeta("cmms-rest", "httpx"),
    "sap_pm": _ConnectorMeta("cmms-rest", "httpx"),
    "maximo": _ConnectorMeta("cmms-rest", "httpx"),
    "upkeep": _ConnectorMeta("cmms-rest", "httpx"),
    "opcua": _ConnectorMeta("opcua", "asyncua"),
    "mqtt": _ConnectorMeta("mqtt", "aiomqtt"),
    "document_store": _ConnectorMeta("docs-rag", "langchain_chroma"),
    "excel": _ConnectorMeta("excel", "openpyxl"),
    "excel_csv": _ConnectorMeta("excel", "openpyxl"),
    "sql": _ConnectorMeta("sql", "pyodbc"),
    "generic_sql": _ConnectorMeta("sql", "pyodbc"),
    "calendar": _ConnectorMeta("calendar", "icalendar"),
    "telegram": _ConnectorMeta("telegram", "telegram"),
    "slack": _ConnectorMeta("slack", "slack_bolt"),
    # EmailConnector uses the stdlib (smtplib/imaplib) by default; the Gmail
    # backend is the only optional path, hence the [gmail] extra.
    "email": _ConnectorMeta("gmail", "googleapiclient"),
}

# Connector types whose capability set is computed at instance level and whose
# *base* set is exposed as a ``_BASE_CAPABILITIES`` ClassVar. The remaining
# (config-driven) capabilities cannot be resolved without a config, so they are
# annotated "configurable" rather than emitted as guaranteed.
_INSTANCE_COMPUTED_TYPES: frozenset[str] = frozenset(
    {"generic_cmms", "excel", "excel_csv", "sql", "generic_sql", "calendar"}
)


# ---------------------------------------------------------------------------
# Public data model — frozen dataclasses, fully sorted
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConnectorCapability:
    """One capability a connector provides, with how it is provided.

    Args:
        capability: The capability value (stable wire string).
        method: The connector method that backs it.
        configurable: ``True`` when the capability is only available under
            certain configuration (e.g. SQL writes need ``capabilities:
            read_write``, calendar writes need a writable backend). Such
            capabilities are annotated, never resolved, because ``describe()``
            runs without a :class:`MachinaConfig`.
    """

    capability: str
    method: str
    configurable: bool = False


@dataclass(frozen=True)
class ConnectorInfo:
    """A connector type known to the default factory registry.

    Args:
        type: Registry key (e.g. ``"opcua"``).
        class_name: Connector class name (e.g. ``"OpcUaConnector"``).
        dotted_path: Import path from ``_CONNECTOR_FACTORIES``.
        requires_extra: pip extra needed for full functionality, or ``None``.
        extra_installed: Whether that extra's package is importable now
            (probed via ``find_spec``, ``None`` when there is no extra).
        instance_computed: ``True`` when the capability set is computed at
            instance level (base set read from ``_BASE_CAPABILITIES``).
        capabilities: Capabilities this connector provides, sorted by value.
        degraded: ``True`` when the class could not be imported/introspected
            (entry kept rather than dropped); ``error`` carries the reason.
        error: Import/introspection failure message, empty when healthy.
    """

    type: str
    class_name: str
    dotted_path: str
    requires_extra: str | None
    extra_installed: bool | None
    instance_computed: bool
    capabilities: tuple[ConnectorCapability, ...] = ()
    degraded: bool = False
    error: str = ""


@dataclass(frozen=True)
class CapabilityInfo:
    """A capability in the vocabulary, with which connectors provide it.

    Args:
        value: The capability value (stable wire string).
        method: The backing method name from ``CAPABILITY_TO_METHOD``
            (empty if the capability is unmapped — a coverage gap).
        provided_by: Connector *types* that provide it (live), sorted.
        configurable_in: Connector types where it is configuration-gated.
        orphaned: ``True`` when no factory-registered connector provides it
            (declared only by a connector absent from the registry, e.g.
            ``SimulatedSensorConnector``).
        orphan_note: Human-readable explanation when ``orphaned``.
    """

    value: str
    method: str
    provided_by: tuple[str, ...] = ()
    configurable_in: tuple[str, ...] = ()
    orphaned: bool = False
    orphan_note: str = ""


@dataclass(frozen=True)
class SeamMethod:
    """A method on a seam Protocol, reflected via ``inspect``.

    Args:
        name: Method name.
        is_async: Whether the method is declared ``async``.
        doc: First line of the method docstring (may be empty).
    """

    name: str
    is_async: bool
    doc: str = ""


@dataclass(frozen=True)
class ProtocolSeam:
    """A seam that is a reflectable ``Protocol``.

    Args:
        name: Protocol class name.
        location: Module path where it is defined.
        doc: First line of the Protocol docstring.
        methods: Required methods, sorted by name.
    """

    name: str
    location: str
    doc: str
    methods: tuple[SeamMethod, ...] = ()


@dataclass(frozen=True)
class ConventionSeam:
    """A seam that is a *convention*, not a Protocol (cannot be reflected).

    Args:
        name: Human name of the seam.
        note: One-line description of what implementing it does.
        location_template: Where the implementer adds code.
    """

    name: str
    note: str
    location_template: str


@dataclass(frozen=True)
class Seams:
    """The framework's extension seams.

    Args:
        protocols: Reflectable Protocol seams, sorted by name.
        conventions: Convention (non-Protocol) seams, sorted by name.
        add_connector_template: Canonical location for a new connector.
    """

    protocols: tuple[ProtocolSeam, ...]
    conventions: tuple[ConventionSeam, ...]
    add_connector_template: str = "connectors/{category}/{name}.py"


@dataclass(frozen=True)
class Gaps:
    """Known introspection gaps surfaced to the consumer.

    Args:
        orphaned_capabilities: Capability values with no registered provider,
            sorted.
        settings_note: Why per-connector ``settings`` are not in the schema.
        unmapped_capabilities: Capability values absent from
            ``CAPABILITY_TO_METHOD`` (should be empty; a guard signal).
    """

    orphaned_capabilities: tuple[str, ...]
    settings_note: str
    unmapped_capabilities: tuple[str, ...] = ()


@dataclass(frozen=True)
class Spine:
    """The complete code-derived self-description of Machina.

    Args:
        connectors: All registered connector types, sorted by ``type``.
        capabilities: The full capability vocabulary, sorted by ``value``.
        seams: Protocol and convention extension seams.
        config_schema: ``MachinaConfig.model_json_schema()`` (shape only,
            never any configured values).
        gaps: Known introspection gaps.
    """

    connectors: tuple[ConnectorInfo, ...]
    capabilities: tuple[CapabilityInfo, ...]
    seams: Seams
    config_schema: dict[str, Any] = field(default_factory=dict)
    gaps: Gaps = field(default_factory=lambda: Gaps(orphaned_capabilities=(), settings_note=""))


# ---------------------------------------------------------------------------
# Seam definitions
# ---------------------------------------------------------------------------
#
# Protocol seams are reflected live (so they can never drift from the code).
# Convention seams are NOT reflectable (no Protocol to inspect) and are listed
# with a short note + a location template each.
_PROTOCOL_SEAMS: tuple[tuple[str, str], ...] = (
    ("machina.connectors.base", "BaseConnector"),
    ("machina.connectors.comms.types", "SupportsConfirmation"),
    ("machina.connectors.docs.watcher", "RefreshableConnector"),
)

_CONVENTION_SEAMS: tuple[ConventionSeam, ...] = (
    ConventionSeam(
        name="transport/mapper split",
        note=(
            "Vendor payload↔domain mapping lives as pure functions, separate "
            "from the connector's transport code; the connector imports the "
            "mapper."
        ),
        location_template="connectors/cmms/mappers/{vendor}.py",
    ),
    ConventionSeam(
        name="connector-type registration",
        note=(
            "Register a new connector type → dotted factory path so the "
            "runtime and MCP layer can discover it."
        ),
        location_template="runtime._CONNECTOR_FACTORIES['{type}'] = "
        "'{dotted.path.ConnectorClass}'",
    ),
    ConventionSeam(
        name="capability vocabulary",
        note="Add a new action identifier to the Capability StrEnum.",
        location_template="connectors/capabilities.py::Capability.{NEW_MEMBER}",
    ),
    ConventionSeam(
        name="capability→method map",
        note=(
            "Map the new capability to its backing method name (capability "
            "values are not always method names)."
        ),
        location_template="introspect/_methods.py::CAPABILITY_TO_METHOD",
    ),
    ConventionSeam(
        name="MCP tool registration",
        note=("Map a capability to the MCP tool(s) auto-registered when a connector declares it."),
        location_template="mcp/tools.py::CAPABILITY_TO_TOOL",
    ),
    ConventionSeam(
        name="workflow builtins",
        note="Drop a built-in workflow template into the builtins package.",
        location_template="workflows/builtins/{workflow_name}.py",
    ),
)


_SETTINGS_NOTE = (
    "Per-connector `settings` is an open `dict[str, Any]` (extra=allow), so "
    "connector-specific settings keys are NOT captured by "
    "MachinaConfig.model_json_schema(). Introspecting connector __init__ "
    "signatures to surface them is a deferred, security-constrained stretch."
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _import_class(dotted_path: str) -> type[Any]:
    """Import a class from a dotted module path (no heavy transitive imports)."""
    module_path, class_name = dotted_path.rsplit(".", 1)
    module = importlib.import_module(module_path)
    return getattr(module, class_name)  # type: ignore[no-any-return]


def _probe_extra(meta: _ConnectorMeta) -> bool | None:
    """Whether a connector's optional extra is installed (via ``find_spec``).

    Returns ``None`` when the connector needs no extra. Never imports the
    module — only checks for an import spec, so a missing heavy dependency
    cannot raise here.
    """
    if meta.probe_module is None:
        return None
    try:
        return importlib.util.find_spec(meta.probe_module) is not None
    except (ImportError, ValueError):
        # Parent package missing entirely, or a broken/None __spec__.
        return False


def _class_base_capabilities(cls: type[Any], conn_type: str) -> frozenset[Capability]:
    """Read a connector class's base capability set without instantiating.

    Most connectors expose a static ``capabilities: ClassVar[frozenset]``.
    The four instance-computed connectors expose a ``_BASE_CAPABILITIES``
    ClassVar instead (their ``capabilities`` is an instance property that
    needs a config/backend). This reads whichever is class-readable.
    """
    base = getattr(cls, "_BASE_CAPABILITIES", None)
    if isinstance(base, frozenset):
        return base
    caps = getattr(cls, "capabilities", None)
    if isinstance(caps, frozenset):
        return caps
    # ``capabilities`` is a property (instance-computed) but no
    # _BASE_CAPABILITIES was exposed — return empty so the entry degrades
    # visibly rather than crashing. (No shipped connector hits this.)
    return frozenset()


def _configurable_capabilities(conn_type: str, base: frozenset[Capability]) -> set[Capability]:
    """Config-gated capabilities for an instance-computed connector type.

    These are declared at instance level depending on configuration, so the
    config-less core annotates them "configurable" rather than resolving them.
    Computed as (full declared maximum) - (base): the base subtraction is
    enforced below, not merely coincidental, so a capability later promoted
    into a connector's ``_BASE_CAPABILITIES`` can never also be double-emitted
    as configurable.
    """
    full: set[Capability]
    if conn_type == "calendar":
        # Base is the read-only minimum (READ_CALENDAR_EVENTS, guaranteed on
        # every backend); CREATE/DELETE are configurable on the backend (the
        # writable google/outlook backends expose them, ical does not).
        full = {
            Capability.CREATE_CALENDAR_EVENT,
            Capability.DELETE_CALENDAR_EVENT,
        }
    elif conn_type in ("sql", "generic_sql"):
        # capabilities: read_write adds writes; a FailureMode table adds reads.
        full = {
            Capability.CREATE_WORK_ORDER,
            Capability.UPDATE_WORK_ORDER,
            Capability.READ_FAILURE_MODES,
        }
    elif conn_type in ("excel", "excel_csv"):
        # A failure_modes sheet adds READ_FAILURE_MODES.
        full = {Capability.READ_FAILURE_MODES}
    elif conn_type == "generic_cmms":
        # Local mode / configured endpoints add the optional WO lifecycle and
        # maintenance-plan reads; a catalog source adds READ_FAILURE_MODES.
        full = {
            Capability.GET_WORK_ORDER,
            Capability.UPDATE_WORK_ORDER,
            Capability.CLOSE_WORK_ORDER,
            Capability.CANCEL_WORK_ORDER,
            Capability.READ_MAINTENANCE_PLANS,
            Capability.READ_FAILURE_MODES,
        }
    else:
        full = set()
    return full - base


def _first_doc_line(obj: Any) -> str:
    """Return the first non-empty line of an object's docstring, scrubbed.

    The line is passed through :func:`machina.agent.prompts.safe_text` before
    it is returned, so a user-home / UNC absolute path embedded in a seam
    Protocol docstring is reduced to its basename **at the core**. This is the
    single choke point: every renderer (the Form-A artifact, the CLI, the MCP
    resource) serves already-scrubbed seam text rather than re-scrubbing, so
    an inspected docstring can never leak an identity- or infra-revealing path
    into LLM-visible output.
    """
    doc = inspect.getdoc(obj)
    if not doc:
        return ""
    for line in doc.splitlines():
        stripped = line.strip()
        if stripped:
            return safe_text(stripped)
    return ""


def _reflect_protocol(module_path: str, class_name: str) -> ProtocolSeam:
    """Reflect a seam Protocol's required members via ``inspect``.

    Emits both methods and ``@property`` members. Properties matter: the single
    most important member a connector author must implement — ``capabilities``
    on :class:`~machina.connectors.base.BaseConnector` — is a property, and a
    seam manifest that lists only functions would silently omit it.

    Import failures degrade to a marked seam (empty methods + scrubbed error)
    rather than propagating, mirroring the connector harvest — so ``describe()``
    keeps its "never raises" contract even if a future seam module grows a
    top-level optional import.
    """
    try:
        module = importlib.import_module(module_path)
        proto = getattr(module, class_name)
    except Exception as exc:
        return ProtocolSeam(
            name=class_name,
            location=module_path,
            doc=safe_text(f"{type(exc).__name__}: {exc}"),
            methods=(),
        )
    methods: list[SeamMethod] = []
    for name, member in inspect.getmembers(proto):
        if name.startswith("_"):
            continue
        if isinstance(member, property):
            fget = member.fget
            methods.append(
                SeamMethod(
                    name=name,
                    is_async=False,
                    doc=_first_doc_line(fget) if fget is not None else "",
                )
            )
            continue
        if not (inspect.isfunction(member) or inspect.ismethod(member)):
            continue
        methods.append(
            SeamMethod(
                name=name,
                is_async=inspect.iscoroutinefunction(member),
                doc=_first_doc_line(member),
            )
        )
    methods.sort(key=lambda m: m.name)
    return ProtocolSeam(
        name=class_name,
        location=module_path,
        doc=_first_doc_line(proto),
        methods=tuple(methods),
    )


def _build_connectors(
    factories: dict[str, str],
) -> tuple[list[ConnectorInfo], dict[Capability, set[str]], dict[Capability, set[str]]]:
    """Build connector entries and the capability→provider indexes.

    Returns:
        (connectors, provided_by, configurable_in) — the connector list plus
        two maps from capability to the set of connector *types* that provide
        it live and that gate it behind configuration.
    """
    connectors: list[ConnectorInfo] = []
    provided_by: dict[Capability, set[str]] = {}
    configurable_in: dict[Capability, set[str]] = {}

    for conn_type in sorted(factories):
        dotted_path = factories[conn_type]
        class_name = dotted_path.rsplit(".", 1)[1]
        meta = _CONNECTOR_META.get(conn_type, _ConnectorMeta(None, None))
        instance_computed = conn_type in _INSTANCE_COMPUTED_TYPES

        try:
            cls = _import_class(dotted_path)
        except Exception as exc:
            # Degrade gracefully — a missing module is a marked entry, not a
            # crash (mirrors the runtime's tolerant connector harvest). Emit a
            # WARNING so the degradation is auditable in CI logs rather than a
            # silent hole in the capability surface (the exception *type* only —
            # the raw message may carry an install path and is scrubbed below).
            import structlog

            structlog.get_logger(__name__).warning(
                "connector introspection degraded",
                connector=conn_type,
                error=type(exc).__name__,
            )
            connectors.append(
                ConnectorInfo(
                    type=conn_type,
                    class_name=class_name,
                    dotted_path=dotted_path,
                    requires_extra=meta.requires_extra,
                    extra_installed=_probe_extra(meta),
                    instance_computed=instance_computed,
                    degraded=True,
                    # Scrub: an import failure message can embed an absolute
                    # install path (and OS username); this field flows into the
                    # LLM-/client-facing renderers, so it must pass the same core
                    # choke point as every other free-text field.
                    error=safe_text(f"{type(exc).__name__}: {exc}"),
                )
            )
            continue

        base = _class_base_capabilities(cls, conn_type)
        configurable = _configurable_capabilities(conn_type, base) if instance_computed else set()

        conn_caps: list[ConnectorCapability] = []
        for cap in sorted(base, key=lambda c: c.value):
            if not has_live_method(cls, cap):
                # Declared (in the base set) but not live-method-backed — skip.
                continue
            conn_caps.append(
                ConnectorCapability(
                    capability=cap.value,
                    method=method_name_for(cap) or "",
                    configurable=False,
                )
            )
            provided_by.setdefault(cap, set()).add(conn_type)

        for cap in sorted(configurable, key=lambda c: c.value):
            if not has_live_method(cls, cap):
                continue
            conn_caps.append(
                ConnectorCapability(
                    capability=cap.value,
                    method=method_name_for(cap) or "",
                    configurable=True,
                )
            )
            configurable_in.setdefault(cap, set()).add(conn_type)

        conn_caps.sort(key=lambda c: c.capability)
        connectors.append(
            ConnectorInfo(
                type=conn_type,
                class_name=class_name,
                dotted_path=dotted_path,
                requires_extra=meta.requires_extra,
                extra_installed=_probe_extra(meta),
                instance_computed=instance_computed,
                capabilities=tuple(conn_caps),
            )
        )

    return connectors, provided_by, configurable_in


def _build_capabilities(
    provided_by: dict[Capability, set[str]],
    configurable_in: dict[Capability, set[str]],
) -> tuple[list[CapabilityInfo], list[str], list[str]]:
    """Build the capability view, flagging orphans and unmapped entries.

    Returns:
        (capabilities, orphaned_values, unmapped_values).
    """
    capabilities: list[CapabilityInfo] = []
    orphaned: list[str] = []
    unmapped: list[str] = []

    for cap in sorted(Capability, key=lambda c: c.value):
        method = method_name_for(cap) or ""
        if cap not in CAPABILITY_TO_METHOD:
            unmapped.append(cap.value)
        providers = provided_by.get(cap, set())
        config_providers = configurable_in.get(cap, set())
        is_orphan = not providers and not config_providers
        note = ""
        if is_orphan:
            orphaned.append(cap.value)
            note = (
                "Declared by SimulatedSensorConnector (machina.connectors.iot."
                "simulated), which is not in the default factory registry "
                "(_CONNECTOR_FACTORIES); no registered connector provides it."
                if cap in (Capability.GET_LATEST_READING, Capability.GET_RELATED_READINGS)
                else (
                    "No connector in the default factory registry "
                    "(_CONNECTOR_FACTORIES) provides this capability."
                )
            )
        capabilities.append(
            CapabilityInfo(
                value=cap.value,
                method=method,
                provided_by=tuple(sorted(providers)),
                configurable_in=tuple(sorted(config_providers)),
                orphaned=is_orphan,
                orphan_note=note,
            )
        )

    return capabilities, orphaned, unmapped


def _build_seams() -> Seams:
    """Reflect Protocol seams and assemble convention seams (all sorted)."""
    protocols = [_reflect_protocol(mod, name) for mod, name in _PROTOCOL_SEAMS]
    protocols.sort(key=lambda p: p.name)
    conventions = sorted(_CONVENTION_SEAMS, key=lambda c: c.name)
    return Seams(protocols=tuple(protocols), conventions=tuple(conventions))


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def describe() -> Spine:
    """Return a deterministic, code-derived self-description of Machina.

    A pure read: no I/O beyond imports, no connector instantiation, no
    ``connect_all``, no heavy optional-dependency import. Safe on a bare
    ``pip install machina-ai`` (core only). Two consecutive calls in one
    process return identical data (every collection is stably sorted).

    Returns:
        A :class:`Spine` describing connectors x capabilities, the config
        schema shape, the extension seams, and known introspection gaps.
    """
    # Imported here (not at module top) to keep the dependency on the runtime
    # module explicit and local to the read; runtime imports no heavy deps.
    from machina.runtime import _CONNECTOR_FACTORIES

    connectors, provided_by, configurable_in = _build_connectors(_CONNECTOR_FACTORIES)
    capabilities, orphaned, unmapped = _build_capabilities(provided_by, configurable_in)
    seams = _build_seams()

    return Spine(
        connectors=tuple(connectors),
        capabilities=tuple(capabilities),
        seams=seams,
        config_schema=MachinaConfig.model_json_schema(),
        gaps=Gaps(
            orphaned_capabilities=tuple(sorted(orphaned)),
            settings_note=_SETTINGS_NOTE,
            unmapped_capabilities=tuple(sorted(unmapped)),
        ),
    )


__all__ = [
    "CapabilityInfo",
    "ConnectorCapability",
    "ConnectorInfo",
    "ConventionSeam",
    "Gaps",
    "ProtocolSeam",
    "SeamMethod",
    "Seams",
    "Spine",
    "describe",
]
