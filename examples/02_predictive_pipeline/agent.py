#!/usr/bin/env python3
"""Autonomous predictive maintenance -- sensor to scheduled work order.

10-step pipeline. 3 LLM steps, 7 deterministic. Zero human intervention.
This is the kind of agent that replaces a manual 3-hour process.

    python agent.py
    python agent.py --sandbox           # log-only mode
    python agent.py --llm ollama:llama3
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_repo_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_repo_root / "src"))

from machina import Agent, Plant
from machina.connectors.cmms import GenericCmmsConnector
from machina.connectors.comms.telegram import CliChannel
from machina.connectors.docs import DocumentStoreConnector
from machina.workflows import Step, Workflow

SAMPLE_DIR = Path(__file__).resolve().parent.parent / "sample_data"


# ── Workflow definition: the star of this example ───────────────

predictive_maintenance = Workflow(
    name="Predictive Maintenance Pipeline",
    description="Sensor alarm to scheduled work order, autonomously.",
    trigger="alarm",
    steps=[
        # Phase 1: Detection
        Step("enrich_alarm",
             action="sensors.get_related_readings",
             description="Read correlated sensor values for the alarmed asset"),

        # Phase 2: Diagnosis (rule-based + LLM synthesis)
        Step("diagnose_rules",
             action="failure_analyzer.diagnose",
             description="Rule-based diagnosis from failure mode taxonomy"),
        Step("search_manuals",
             action="docs.search",
             description="RAG search in equipment manuals"),
        Step("diagnose_llm",
             action="agent.reason",
             prompt=(
                 "You are a diagnostic specialist.\n"
                 "Alarm data: {enrich_alarm}\n"
                 "Rule-based diagnosis: {diagnose_rules}\n"
                 "Manual sections: {search_manuals}\n\n"
                 "Provide: root cause, confidence level, 24h risk assessment."
             )),

        # Phase 3: Action
        Step("check_parts",
             action="cmms.check_spare_parts",
             description="Verify spare parts for the diagnosed failure"),
        Step("check_history",
             action="cmms.get_asset_history",
             description="Recent maintenance history"),
        Step("draft_wo",
             action="agent.reason",
             prompt=(
                 "You are a work order specialist.\n"
                 "Diagnosis: {diagnose_llm}\n"
                 "Spare parts: {check_parts}\n"
                 "History: {check_history}\n\n"
                 "Create: priority, description, skills needed, safety precautions."
             )),
        Step("submit_wo",
             action="work_order_factory.create",
             description="Create work order in CMMS"),

        # Phase 4: Optimization
        Step("find_window",
             action="maintenance_scheduler.find_window",
             description="Find next available maintenance window"),
        Step("optimize_schedule",
             action="agent.reason",
             prompt=(
                 "You are a planning optimizer.\n"
                 "Work order: {submit_wo}\n"
                 "Windows: {find_window}\n\n"
                 "Recommend: optimal timing, grouping opportunities, production impact."
             )),
    ],
)

# ── The agent ───────────────────────────────────────────────────

def build_agent(llm: str = "openai:gpt-4o", sandbox: bool = False) -> Agent:
    cmms = GenericCmmsConnector(data_dir=SAMPLE_DIR / "cmms")
    docs = DocumentStoreConnector(paths=[SAMPLE_DIR / "manuals"])

    # In production, replace with real sensor connectors:
    #   OpcUA(endpoint="opc.tcp://plc:4840", subscriptions=[...])
    #   MQTT(broker="mqtt://broker:1883", topics=["plant/+/sensors/#"])
    from machina.connectors.iot import SimulatedSensorConnector
    sensors = SimulatedSensorConnector(data_dir=SAMPLE_DIR / "sensor_logs")

    return Agent(
        name="Predictive Maintenance Agent",
        plant=Plant(name="North Plant"),
        connectors=[cmms, docs, sensors],
        channels=[CliChannel()],
        llm=llm,
        workflows=[predictive_maintenance],
        sandbox=sandbox,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Predictive Maintenance Pipeline")
    parser.add_argument("--llm", default="openai:gpt-4o", help="LLM provider:model")
    parser.add_argument("--sandbox", action="store_true", help="Log-only mode")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    if args.verbose:
        from machina.observability.logging import configure_logging
        configure_logging(level="DEBUG")

    agent = build_agent(llm=args.llm, sandbox=args.sandbox)

    mode = "SANDBOX" if args.sandbox else "LIVE"
    print(f"\n  Predictive Maintenance Pipeline")
    print(f"  LLM: {args.llm}  |  Mode: {mode}")
    print(f"  Workflow: {len(predictive_maintenance.steps)} steps "
          f"(3 LLM + 7 deterministic)\n")

    agent.run()


if __name__ == "__main__":
    main()
