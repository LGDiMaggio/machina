"""Tests for the Form-A renderers (``machina.introspect.render_llms``).

The renderer is a pure function over a :class:`Spine`, so these tests build
synthetic spines and assert on the rendered Markdown directly — no git, no IO,
no dependency on the live registry. They pin:

* AE1 — a spine that gained a new capability renders it with no manual edit;
* a declared-but-not-live-method-backed capability never reaches the matrix
  (the core excludes it; the renderer only emits what the spine carries);
* AE2 — the seam section names each seam's Protocol/convention and the
  ``connectors/{category}/{name}.py`` location template;
* determinism — the same spine renders byte-identical Markdown twice, and the
  provenance header is a strippable delimited block;
* scrubbing — a seam doc carrying an absolute path renders scrubbed (the core
  scrubs at ``_first_doc_line``; we assert the rendered output is path-free).
"""

from __future__ import annotations

from machina.introspect.core import (
    CapabilityInfo,
    ConnectorCapability,
    ConnectorInfo,
    ConventionSeam,
    Gaps,
    ProtocolSeam,
    SeamMethod,
    Seams,
    Spine,
)
from machina.introspect.render_llms import (
    PROVENANCE_END,
    PROVENANCE_START,
    render_json,
    render_markdown,
    strip_provenance,
)

# ---------------------------------------------------------------------------
# Synthetic spine builders
# ---------------------------------------------------------------------------


def _seams() -> Seams:
    return Seams(
        protocols=(
            ProtocolSeam(
                name="BaseConnector",
                location="machina.connectors.base",
                doc="Protocol that all Machina connectors must satisfy.",
                methods=(
                    SeamMethod(name="connect", is_async=True, doc="Establish a connection."),
                    SeamMethod(name="disconnect", is_async=True, doc="Close the connection."),
                ),
            ),
        ),
        conventions=(
            ConventionSeam(
                name="capability vocabulary",
                note="Add a new action identifier to the Capability StrEnum.",
                location_template="connectors/capabilities.py::Capability.{NEW_MEMBER}",
            ),
        ),
    )


def _spine(
    connectors: tuple[ConnectorInfo, ...],
    capabilities: tuple[CapabilityInfo, ...],
) -> Spine:
    return Spine(
        connectors=connectors,
        capabilities=capabilities,
        seams=_seams(),
        config_schema={
            "properties": {
                "name": {"description": "Agent name. More detail here."},
                "connectors": {"description": "Named connector configurations"},
            },
            "$defs": {"ConnectorConfig": {}},
        },
        gaps=Gaps(orphaned_capabilities=(), settings_note="Settings is an open dict."),
    )


def _base_spine() -> Spine:
    """A small two-connector spine with two live capabilities."""
    cmms = ConnectorInfo(
        type="acme_cmms",
        class_name="AcmeCmmsConnector",
        dotted_path="machina.connectors.cmms.acme.AcmeCmmsConnector",
        requires_extra="cmms-rest",
        extra_installed=True,
        instance_computed=False,
        capabilities=(
            ConnectorCapability(capability="read_assets", method="read_assets"),
            ConnectorCapability(capability="read_work_orders", method="read_work_orders"),
        ),
    )
    caps = (
        CapabilityInfo(value="read_assets", method="read_assets", provided_by=("acme_cmms",)),
        CapabilityInfo(
            value="read_work_orders", method="read_work_orders", provided_by=("acme_cmms",)
        ),
    )
    return _spine((cmms,), caps)


# ---------------------------------------------------------------------------
# (a) AE1 — a new capability renders with no manual edit
# ---------------------------------------------------------------------------


def test_new_capability_is_rendered_without_manual_edit() -> None:
    """AE1: a spine that gained a capability shows it in the rendered matrix."""
    cmms = ConnectorInfo(
        type="acme_cmms",
        class_name="AcmeCmmsConnector",
        dotted_path="machina.connectors.cmms.acme.AcmeCmmsConnector",
        requires_extra=None,
        extra_installed=None,
        instance_computed=False,
        capabilities=(
            ConnectorCapability(capability="read_assets", method="read_assets"),
            # The brand-new capability — never hand-added to any artifact.
            ConnectorCapability(capability="diagnose_vibration", method="diagnose_vibration"),
        ),
    )
    caps = (
        CapabilityInfo(
            value="diagnose_vibration", method="diagnose_vibration", provided_by=("acme_cmms",)
        ),
        CapabilityInfo(value="read_assets", method="read_assets", provided_by=("acme_cmms",)),
    )
    markdown = render_markdown(_spine((cmms,), caps))
    assert "diagnose_vibration" in markdown
    # It appears as a live (yes) cell in the matrix row, not just prose.
    matrix_row = next(
        line for line in markdown.splitlines() if line.startswith("| diagnose_vibration ")
    )
    assert "yes" in matrix_row


# ---------------------------------------------------------------------------
# (b) declared-but-not-live-backed is absent from the rendered matrix
# ---------------------------------------------------------------------------


def test_declared_but_unbacked_capability_absent_from_matrix() -> None:
    """A capability the core did not emit (no live method) is not rendered.

    The core only places live-method-backed capabilities on the spine, so a
    declared-but-stubbed capability never reaches the renderer. Render a spine
    whose connector carries only the live capability and assert the stubbed one
    is nowhere in the matrix.
    """
    cmms = ConnectorInfo(
        type="acme_cmms",
        class_name="AcmeCmmsConnector",
        dotted_path="machina.connectors.cmms.acme.AcmeCmmsConnector",
        requires_extra=None,
        extra_installed=None,
        instance_computed=False,
        # update_work_order is declared by the class but stubbed → core omitted it.
        capabilities=(ConnectorCapability(capability="read_assets", method="read_assets"),),
    )
    caps = (CapabilityInfo(value="read_assets", method="read_assets", provided_by=("acme_cmms",)),)
    markdown = render_markdown(_spine((cmms,), caps))
    matrix_section = markdown.split("## Capabilities")[0]
    assert "update_work_order" not in matrix_section
    assert "read_assets" in matrix_section


def test_orphaned_capability_excluded_from_matrix_listed_in_gaps() -> None:
    """An orphaned capability is not a matrix row but appears under gaps."""
    cmms = ConnectorInfo(
        type="acme_cmms",
        class_name="AcmeCmmsConnector",
        dotted_path="machina.connectors.cmms.acme.AcmeCmmsConnector",
        requires_extra=None,
        extra_installed=None,
        instance_computed=False,
        capabilities=(ConnectorCapability(capability="read_assets", method="read_assets"),),
    )
    caps = (
        CapabilityInfo(value="read_assets", method="read_assets", provided_by=("acme_cmms",)),
        CapabilityInfo(
            value="get_latest_reading",
            method="get_latest_reading",
            orphaned=True,
            orphan_note="Declared by SimulatedSensorConnector; not in the registry.",
        ),
    )
    spine = Spine(
        connectors=(cmms,),
        capabilities=caps,
        seams=_seams(),
        config_schema={"properties": {}},
        gaps=Gaps(
            orphaned_capabilities=("get_latest_reading",),
            settings_note="Settings is an open dict.",
        ),
    )
    markdown = render_markdown(spine)
    matrix_section = markdown.split("## Capabilities")[0]
    assert "get_latest_reading" not in matrix_section
    gaps_section = markdown.split("## Known gaps")[1]
    assert "get_latest_reading" in gaps_section


# ---------------------------------------------------------------------------
# (c) AE2 — seam section names protocols/conventions + location template
# ---------------------------------------------------------------------------


def test_seam_section_names_protocols_conventions_and_template() -> None:
    """AE2: the rendered seam manifest carries the protocol, convention, and
    the connectors/{category}/{name}.py add-connector template.
    """
    markdown = render_markdown(_base_spine())
    seams_section = markdown.split("## Extension seams")[1]
    # Protocol seam named with its location and reflected methods.
    assert "`BaseConnector`" in seams_section
    assert "machina.connectors.base" in seams_section
    assert "async connect" in seams_section
    # Convention seam named with its location template.
    assert "capability vocabulary" in seams_section
    assert "connectors/capabilities.py::Capability.{NEW_MEMBER}" in seams_section
    # The canonical add-connector location template.
    assert "connectors/{category}/{name}.py" in seams_section


# ---------------------------------------------------------------------------
# (d) provenance is strippable; render-from-spine is deterministic
# ---------------------------------------------------------------------------


def test_provenance_header_is_a_strippable_delimited_block() -> None:
    """The provenance block is delimited and removable for body diffing."""
    markdown = render_markdown(_base_spine(), git_sha="deadbeef")
    assert PROVENANCE_START in markdown
    assert PROVENANCE_END in markdown
    assert "deadbeef" in markdown
    body = strip_provenance(markdown)
    assert PROVENANCE_START not in body
    assert "deadbeef" not in body
    # The body still starts with the document heading.
    assert body.lstrip().startswith("# Machina capability matrix")


def test_provenance_sha_does_not_change_body() -> None:
    """Two different SHAs produce identical bodies (gate never flaps on SHA)."""
    spine = _base_spine()
    a = render_markdown(spine, git_sha="aaaaaaa")
    b = render_markdown(spine, git_sha="bbbbbbb")
    assert a != b  # headers differ
    assert strip_provenance(a) == strip_provenance(b)  # bodies identical


def test_render_is_deterministic_across_two_calls() -> None:
    """The same spine renders byte-identical Markdown (and JSON) twice."""
    spine = _base_spine()
    assert render_markdown(spine, git_sha="x") == render_markdown(spine, git_sha="x")
    assert render_json(spine) == render_json(spine)


def test_render_json_has_no_provenance() -> None:
    """The JSON carries a schema discriminant but no provenance (so it diffs fully)."""
    payload = render_json(_base_spine())
    assert set(payload) == {
        "schema_version",
        "connectors",
        "capabilities",
        "seams",
        "config_schema",
        "gaps",
    }
    assert payload["schema_version"] == "1"
    # The capability is present and structured.
    values = {c["value"] for c in payload["capabilities"]}
    assert "read_assets" in values


def test_connector_json_omits_environment_dependent_extra_installed() -> None:
    """The serialized connector carries the static ``requires_extra`` but NOT the
    live ``extra_installed`` — a find_spec result baked into the byte-compared
    artifact would make the drift gate flap across environments."""
    payload = render_json(_base_spine())
    connector = payload["connectors"][0]
    assert "requires_extra" in connector
    assert "extra_installed" not in connector


# ---------------------------------------------------------------------------
# (e) scrubbing — an absolute path in a seam doc renders scrubbed
# ---------------------------------------------------------------------------


def test_seam_doc_with_absolute_path_renders_scrubbed() -> None:
    """A ProtocolSeam.doc carrying a user-home path renders without the path.

    The core scrubs at ``_first_doc_line`` (single choke point), so by the time
    a doc reaches the renderer it is already a basename. We model that contract:
    a spine whose seam doc is the *scrubbed* form renders path-free, and we also
    assert the renderer itself does not reintroduce a raw path.
    """
    leaky_seams = Seams(
        protocols=(
            ProtocolSeam(
                name="BaseConnector",
                location="machina.connectors.base",
                # Already scrubbed by the core: only the basename survives.
                doc="Loads config from machina.yaml at startup.",
                methods=(
                    SeamMethod(
                        name="connect",
                        is_async=True,
                        doc="Reads creds.json then connects.",
                    ),
                ),
            ),
        ),
        conventions=(),
    )
    spine = Spine(
        connectors=(),
        capabilities=(),
        seams=leaky_seams,
        config_schema={"properties": {}},
        gaps=Gaps(orphaned_capabilities=(), settings_note=""),
    )
    markdown = render_markdown(spine)
    assert "C:\\Users" not in markdown
    assert "/home/" not in markdown
    assert "machina.yaml" in markdown


def test_core_scrubs_seam_doc_then_renders_scrubbed() -> None:
    """End-to-end: the core scrubs an absolute path in a docstring, and a
    spine carrying that scrubbed doc renders path-free.

    ``_first_doc_line`` (the core helper the seam reflection path uses) reduces
    a user-home path to its basename; feeding that scrubbed doc into the
    renderer keeps the artifact path-free.
    """
    from machina.introspect import core

    class _LeakyProto:
        """Reads C:\\Users\\tedib\\secrets\\creds.json before connecting."""

    scrubbed = core._first_doc_line(_LeakyProto)
    assert "C:\\Users\\tedib" not in scrubbed
    assert "creds.json" in scrubbed

    spine = Spine(
        connectors=(),
        capabilities=(),
        seams=Seams(
            protocols=(
                ProtocolSeam(
                    name="BaseConnector",
                    location="machina.connectors.base",
                    doc=scrubbed,
                    methods=(),
                ),
            ),
            conventions=(),
        ),
        config_schema={"properties": {}},
        gaps=Gaps(orphaned_capabilities=(), settings_note=""),
    )
    markdown = render_markdown(spine)
    assert "C:\\Users\\tedib" not in markdown
    assert "creds.json" in markdown
