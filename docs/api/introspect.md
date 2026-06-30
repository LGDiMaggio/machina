# Introspection (Self-description spine)

`machina.introspect` is the framework's **self-description spine** — a single, code-derived description of what Machina can do (connectors × capabilities), how it is configured, and where its extension seams are. It is derived entirely from code (the `Capability` enum, the connector type registry, connector `ClassVar` declarations, the config schema, and the seam Protocols), imports **no** heavy optional dependency, and returns a fully deterministic (sorted) structure.

It is the neutral core that the static `llms.txt` index and the generated capability artifacts (`docs/capabilities.md` and `docs/capabilities.json`) render from — so they cannot drift from one another or from the code. The same core is designed to back a `machina describe` CLI and a `machina://v1/capabilities` MCP resource; those surfaces are forthcoming, not yet shipped.

## `describe`

The single public entry point. A pure read: no connector instantiation, no `connect_all`, no I/O beyond imports. Safe on a bare `pip install machina-ai`. Two consecutive calls in one process return identical data.

::: machina.introspect.core.describe

## `Spine`

The complete code-derived self-description returned by [`describe`](#describe).

::: machina.introspect.core.Spine

## Connectors

::: machina.introspect.core.ConnectorInfo

::: machina.introspect.core.ConnectorCapability

## Capabilities

::: machina.introspect.core.CapabilityInfo

## Extension seams

::: machina.introspect.core.Seams

::: machina.introspect.core.ProtocolSeam

::: machina.introspect.core.ConventionSeam

::: machina.introspect.core.SeamMethod

## Gaps

Known introspection gaps surfaced to the consumer (orphaned capabilities with no registered provider, and the open per-connector `settings` dict that the config schema does not capture).

::: machina.introspect.core.Gaps
