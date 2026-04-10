#!/usr/bin/env python3
"""Machina Predictive Maintenance Pipeline — Full Workflow Example.

This example demonstrates an end-to-end predictive maintenance pipeline
that goes beyond Q&A: it detects anomalies from sensor data, diagnoses
the root cause, creates work orders, optimizes scheduling, and notifies
the maintenance team — all orchestrated as a Machina workflow.

The pipeline follows 4 phases:
    1. Detection  — sensor alarm triggers the workflow
    2. Diagnosis  — rule-based + LLM-powered root cause analysis
    3. Action     — work order creation with spare parts check
    4. Optimization — scheduling with production constraints

Run it:
    cd examples/predictive_pipeline
    python main.py

Or with a custom LLM:
    python main.py --llm ollama:llama3
    python main.py --llm anthropic:claude-sonnet-4-20250514

Sandbox mode (actions logged, nothing executed):
    python main.py --sandbox
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

# Ensure the src directory is importable when running from the examples folder
_repo_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_repo_root / "src"))

from machina import Agent, Plant  # noqa: E402
from machina.connectors.cmms import GenericCmmsConnector  # noqa: E402
from machina.connectors.comms.telegram import CliChannel, TelegramConnector  # noqa: E402
from machina.connectors.docs import DocumentStoreConnector  # noqa: E402
from machina.observability.logging import configure_logging  # noqa: E402
from machina.workflows import Step, Workflow  # noqa: E402

if TYPE_CHECKING:
    from machina.llm.provider import LLMProvider


# ---------------------------------------------------------------------------
# Workflow definition
# ---------------------------------------------------------------------------

predictive_maintenance = Workflow(
    name="Predictive Maintenance Pipeline",
    description=(
        "End-to-end predictive maintenance: from sensor alarm to scheduled "
        "work order with team notification."
    ),
    trigger="alarm",
    steps=[
        # ── Phase 1: Detection ──────────────────────────────────────────
        # The trigger IS the detection: an Alarm entity arrives from a
        # sensor connector (OPC-UA, MQTT, or simulated).  This step
        # enriches it with correlated readings from the same asset.
        Step(
            "enrich_alarm",
            action="sensors.get_related_readings",
            description="Read correlated sensor values for the alarmed asset",
        ),
        # ── Phase 2: Diagnosis ──────────────────────────────────────────
        # First: deterministic diagnosis from the failure mode taxonomy
        Step(
            "diagnose_rules",
            action="failure_analyzer.diagnose",
            description="Rule-based diagnosis using failure mode taxonomy and asset history",
        ),
        # Then: retrieve relevant sections from maintenance manuals
        Step(
            "search_manuals",
            action="docs.search",
            description="RAG search in equipment manuals and troubleshooting guides",
        ),
        # Finally: LLM synthesizes all evidence into a root cause analysis
        Step(
            "diagnose_llm",
            action="agent.reason",
            prompt=(
                "You are a diagnostic specialist for industrial equipment.\n"
                "Given:\n"
                "- Alarm data: {enrich_alarm}\n"
                "- Rule-based diagnosis: {diagnose_rules}\n"
                "- Relevant manual sections: {search_manuals}\n"
                "- Asset maintenance history (from context)\n\n"
                "Provide:\n"
                "1. Root cause analysis (most probable failure mode and why)\n"
                "2. Confidence level (high/medium/low) with reasoning\n"
                "3. Risk assessment if the failure is not addressed within 24h"
            ),
        ),
        # ── Phase 3: Action ─────────────────────────────────────────────
        # Check spare parts availability for the diagnosed failure mode
        Step(
            "check_parts",
            action="cmms.check_spare_parts",
            description="Verify spare parts availability for the diagnosed failure mode",
        ),
        # Fetch recent maintenance history for additional context
        Step(
            "check_history",
            action="cmms.get_asset_history",
            description="Retrieve recent maintenance history for the asset",
        ),
        # LLM decides priority and writes the work order description
        Step(
            "draft_wo",
            action="agent.reason",
            prompt=(
                "You are a maintenance work order specialist.\n"
                "Given:\n"
                "- Diagnosis: {diagnose_llm}\n"
                "- Spare parts availability: {check_parts}\n"
                "- Asset maintenance history: {check_history}\n\n"
                "Create a work order with:\n"
                "- Priority (CRITICAL / HIGH / MEDIUM / LOW) with justification\n"
                "- Clear description of the intervention needed\n"
                "- Required skills and estimated duration\n"
                "- Safety precautions if applicable"
            ),
        ),
        # Create the work order in the CMMS
        Step(
            "submit_wo",
            action="work_order_factory.create",
            description="Create the work order in the CMMS with auto-populated fields",
        ),
        # ── Phase 4: Optimization ───────────────────────────────────────
        # Find the best maintenance window considering production schedule
        Step(
            "find_window",
            action="maintenance_scheduler.find_window",
            description="Find the next available maintenance window",
        ),
        # LLM optimizes scheduling with production constraints
        Step(
            "optimize_schedule",
            action="agent.reason",
            prompt=(
                "You are a maintenance planning optimizer.\n"
                "Given:\n"
                "- New work order: {submit_wo}\n"
                "- Available maintenance windows: {find_window}\n"
                "- Other pending work orders in the same area\n\n"
                "Recommend:\n"
                "- Optimal scheduling (when to intervene)\n"
                "- Whether to group with other pending maintenance\n"
                "- Impact on production and mitigation plan"
            ),
        ),
        # ── Notification ────────────────────────────────────────────────
        Step(
            "notify_team",
            action="channels.send_message",
            template=(
                "⚠️ Predictive Maintenance Alert — {asset.name}\n\n"
                "📊 Alarm: {enrich_alarm.parameter} = {enrich_alarm.value} "
                "(threshold: {enrich_alarm.threshold})\n"
                "🔍 Diagnosis: {diagnose_llm.root_cause}\n"
                "📋 Work Order: {submit_wo.id} — Priority {submit_wo.priority}\n"
                "🔧 Spare parts: {check_parts.summary}\n"
                "📅 Recommended window: {optimize_schedule.recommended_window}"
            ),
        ),
    ],
)


# ---------------------------------------------------------------------------
# Agent construction
# ---------------------------------------------------------------------------

def build_agent(
    *,
    llm: str | LLMProvider = "openai:gpt-4o",
    use_telegram: bool = False,
    sandbox: bool = False,
    temperature: float = 0.1,
) -> Agent:
    """Construct the Predictive Maintenance Agent with sample data.

    Args:
        llm: A ``provider:model`` string or an :class:`LLMProvider` instance.
        use_telegram: If True, use Telegram instead of the interactive CLI.
        sandbox: If True, enable sandbox mode — all actions are logged but
            not executed against real systems.
        temperature: LLM sampling temperature.

    Returns:
        A configured but not-yet-started :class:`Agent`.
    """
    sample_dir = Path(__file__).parent / "sample_data"
    # Reuse the knowledge_agent sample data for CMMS and manuals
    knowledge_data = Path(__file__).parent.parent / "knowledge_agent" / "sample_data"

    # 1. CMMS connector (local mode with sample data)
    cmms = GenericCmmsConnector(data_dir=knowledge_data / "cmms")

    # 2. Document Store (maintenance manuals)
    docs = DocumentStoreConnector(paths=[knowledge_data / "manuals"])

    # 3. Sensor connector (simulated from sample data for this example)
    #    In production, this would be:
    #        OpcUa(endpoint="opc.tcp://plc:4840", subscriptions=[...])
    #    or  Mqtt(broker="mqtt://broker:1883", topics=["plant/+/sensors/#"])
    from machina.connectors.iot import SimulatedSensorConnector  # noqa: E402

    sensors = SimulatedSensorConnector(data_dir=sample_dir / "sensor_logs")

    # 4. Communication channel
    channel: Any
    if use_telegram:
        import os

        bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        if not bot_token:
            raise RuntimeError(
                "TELEGRAM_BOT_TOKEN environment variable is required "
                "when use_telegram=True"
            )
        channel = TelegramConnector(bot_token=bot_token)
    else:
        channel = CliChannel()

    # 5. Build the agent
    plant = Plant(name="North Plant")
    plant.load_assets_from(cmms)

    agent = Agent(
        name="Predictive Maintenance Agent",
        description=(
            "Monitors equipment sensors, diagnoses anomalies, creates work "
            "orders, and optimizes maintenance scheduling — autonomously."
        ),
        plant=plant,
        connectors=[cmms, docs, sensors],
        channels=[channel],
        llm=llm,
        temperature=temperature,
        workflows=[predictive_maintenance],
        sandbox=sandbox,
    )

    return agent


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------

def main() -> None:
    """Run the Predictive Maintenance Pipeline example."""
    parser = argparse.ArgumentParser(
        description="Machina Predictive Maintenance Pipeline"
    )
    parser.add_argument(
        "--telegram",
        action="store_true",
        help="Use Telegram instead of CLI (requires TELEGRAM_BOT_TOKEN)",
    )
    parser.add_argument(
        "--llm",
        default="openai:gpt-4o",
        help="LLM provider:model (default: openai:gpt-4o)",
    )
    parser.add_argument(
        "--sandbox",
        action="store_true",
        help="Enable sandbox mode — actions are logged but not executed",
    )
    parser.add_argument(
        "--simulate-alarm",
        action="store_true",
        default=True,
        help="Simulate a sensor alarm to trigger the workflow (default: True)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args()

    configure_logging(level="DEBUG" if args.verbose else "INFO")

    try:
        agent = build_agent(
            llm=args.llm,
            use_telegram=args.telegram,
            sandbox=args.sandbox,
        )
    except RuntimeError as exc:
        print(f"Error: {exc}")
        sys.exit(1)

    mode = "🔒 SANDBOX" if args.sandbox else "🟢 LIVE"
    print("\n🔧 Starting Machina Predictive Maintenance Pipeline...")
    print(f"   Mode: {mode}")
    print(f"   LLM: {args.llm}")
    print(f"   Channel: {'Telegram' if args.telegram else 'CLI'}")
    print(f"   Workflow: {predictive_maintenance.name}")
    print(f"   Steps: {len(predictive_maintenance.steps)}")

    if args.sandbox:
        print("   ⚠️  Sandbox mode: all actions will be logged but NOT executed")

    if args.simulate_alarm:
        print("\n📡 Simulating sensor alarm on Pump P-201...")
        print("   Parameter: vibration_velocity_mm_s = 7.8 (threshold: 6.0)\n")

    agent.run()


if __name__ == "__main__":
    main()
