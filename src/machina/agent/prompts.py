"""Domain-aware prompt templates and context injection.

Provides system prompts that ground the LLM in maintenance domain
knowledge, and utilities to inject asset context, alarms, and work
order history into the conversation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from machina.agent.entity_resolver import ResolvedEntity
    from machina.domain.alarm import Alarm
    from machina.domain.asset import Asset
    from machina.domain.spare_part import SparePart
    from machina.domain.work_order import WorkOrder

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are a specialised maintenance assistant powered by the Machina framework.
Your role is to help maintenance technicians and engineers with:

- **Equipment information**: asset details, hierarchy, criticality, location
- **Maintenance history**: past work orders, repair records, failure patterns
- **Procedures and manuals**: step-by-step maintenance procedures from technical documents
- **Failure diagnosis**: analysing symptoms and suggesting probable failure modes
- **Spare parts**: checking inventory availability and compatibility
- **Work orders**: creating and tracking maintenance tasks
- **Maintenance schedules**: upcoming preventive maintenance, overdue tasks

## Guidelines

1. **Be precise and factual.** Base your answers on data from the CMMS, documents, \
and sensor readings. Always cite your sources.
2. **Use domain terminology.** Refer to assets by their ID and name. \
Use standard maintenance terms (work order, failure mode, criticality, RUL, MTBF).
3. **Be safety-conscious.** If a question involves safety-critical equipment \
(criticality A), highlight this prominently.
4. **Be concise.** Technicians are on the plant floor. \
Give clear, actionable answers.
5. **Use tools proactively.** Search for assets, query work orders, \
and look up documents before answering — do not guess.
6. **Respond in the user's language.** If the user writes in Italian, \
respond in Italian. Same for any other language.

## Plant Context

{plant_context}

## Available Capabilities

{capabilities_context}
"""


def build_system_prompt(
    *,
    plant_name: str = "",
    asset_count: int = 0,
    capabilities: list[str] | None = None,
) -> str:
    """Build the system prompt with plant and capability context.

    Args:
        plant_name: Name of the plant.
        asset_count: Number of assets in the registry.
        capabilities: List of available connector capabilities.

    Returns:
        The formatted system prompt string.
    """
    plant_ctx = (
        f"Plant: {plant_name} | Assets registered: {asset_count}"
        if plant_name
        else "No plant configured."
    )

    cap_ctx = "None configured."
    if capabilities:
        cap_ctx = ", ".join(sorted(set(capabilities)))

    return SYSTEM_PROMPT.format(
        plant_context=plant_ctx,
        capabilities_context=cap_ctx,
    )


# ---------------------------------------------------------------------------
# Context injection helpers
# ---------------------------------------------------------------------------


def format_asset_context(asset: Asset) -> str:
    """Format an asset's details for injection into the prompt.

    Args:
        asset: The asset to describe.

    Returns:
        A human-readable summary suitable for LLM context.
    """
    lines = [
        f"**Asset: {asset.name}** (ID: {asset.id})",
        f"  Type: {asset.type.value}",
        f"  Location: {asset.location}" if asset.location else "",
        f"  Manufacturer: {asset.manufacturer} {asset.model}".strip()
        if asset.manufacturer
        else "",
        f"  Criticality: {asset.criticality.value}",
        f"  Parent: {asset.parent}" if asset.parent else "",
    ]
    if asset.failure_modes:
        lines.append(f"  Known failure modes: {', '.join(asset.failure_modes)}")
    return "\n".join(line for line in lines if line)


def format_work_orders_context(work_orders: list[WorkOrder]) -> str:
    """Format work order history for the prompt.

    Args:
        work_orders: List of work orders to summarise.

    Returns:
        A formatted summary string.
    """
    if not work_orders:
        return "No work orders found."

    lines = [f"**Work Orders ({len(work_orders)}):**"]
    for wo in work_orders[:10]:  # Limit to 10 most recent
        lines.append(
            f"  - [{wo.id}] {wo.type.value} | {wo.priority.value} | "
            f"{wo.status.value} | {wo.description[:80]}"
        )
    if len(work_orders) > 10:
        lines.append(f"  ... and {len(work_orders) - 10} more")
    return "\n".join(lines)


def format_alarms_context(alarms: list[Alarm]) -> str:
    """Format active alarms for the prompt.

    Args:
        alarms: List of active alarms.

    Returns:
        A formatted alarm summary.
    """
    if not alarms:
        return "No active alarms."

    lines = [f"**Active Alarms ({len(alarms)}):**"]
    for alarm in alarms:
        lines.append(
            f"  - [{alarm.severity.value.upper()}] {alarm.parameter}: "
            f"{alarm.value} {alarm.unit} (threshold: {alarm.threshold})"
        )
    return "\n".join(lines)


def format_spare_parts_context(parts: list[SparePart]) -> str:
    """Format spare parts availability for the prompt.

    Args:
        parts: List of spare parts.

    Returns:
        A formatted spare parts summary.
    """
    if not parts:
        return "No spare parts data available."

    lines = [f"**Spare Parts ({len(parts)}):**"]
    for part in parts:
        status = "✅ In stock" if part.stock_quantity > 0 else "❌ Out of stock"
        lines.append(f"  - {part.name} (SKU: {part.sku}) — Qty: {part.stock_quantity} | {status}")
    return "\n".join(lines)


def format_resolved_entities(entities: list[ResolvedEntity]) -> str:
    """Format entity resolution results for the prompt.

    Args:
        entities: Resolved entity matches.

    Returns:
        A summary of matched assets.
    """
    if not entities:
        return ""

    lines = ["**Resolved assets from your question:**"]
    for ent in entities[:3]:
        lines.append(
            f"  - {ent.asset.name} (ID: {ent.asset.id}) "
            f"[confidence: {ent.confidence:.0%}, match: {ent.match_reason}]"
        )
    return "\n".join(lines)


def format_document_results(results: list[dict[str, Any]]) -> str:
    """Format document search results for the prompt.

    Args:
        results: List of dicts with ``content``, ``source``, ``page`` keys.

    Returns:
        Formatted document excerpts.
    """
    if not results:
        return "No relevant documents found."

    lines = [f"**Relevant Documents ({len(results)}):**"]
    for i, result in enumerate(results[:5], 1):
        source = result.get("source", "unknown")
        page = result.get("page", "")
        content = result.get("content", "")[:300]
        page_ref = f" (p. {page})" if page else ""
        lines.append(f"\n  [{i}] Source: {source}{page_ref}")
        lines.append(f"  {content}")
    return "\n".join(lines)


def build_context_message(
    *,
    resolved_entities: list[ResolvedEntity] | None = None,
    asset: Asset | None = None,
    work_orders: list[WorkOrder] | None = None,
    alarms: list[Alarm] | None = None,
    spare_parts: list[SparePart] | None = None,
    document_results: list[dict[str, Any]] | None = None,
) -> str:
    """Build a context injection message from all available data.

    This is injected as a system message before the user's question
    to ground the LLM's response in real data.

    Returns:
        A formatted context string, or empty string if no context.
    """
    sections: list[str] = []

    if resolved_entities:
        sections.append(format_resolved_entities(resolved_entities))

    if asset:
        sections.append(format_asset_context(asset))

    if work_orders is not None:
        sections.append(format_work_orders_context(work_orders))

    if alarms is not None:
        sections.append(format_alarms_context(alarms))

    if spare_parts is not None:
        sections.append(format_spare_parts_context(spare_parts))

    if document_results is not None:
        sections.append(format_document_results(document_results))

    return "\n\n".join(sections)
