#!/usr/bin/env python3
"""Build your own maintenance workflows -- the workflow DSL is your superpower.

Two complete custom workflows that mix deterministic steps with LLM reasoning:
  1. Spare Part Reorder -- triggered when inventory drops below reorder point
  2. Preventive Maintenance Scheduler -- runs every Monday at 6 AM

    python agent.py                     # sandbox (default)
    python agent.py --live              # execute writes
    python agent.py --llm openai:gpt-4o
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

_repo_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_repo_root / "src"))

from machina import Agent, Plant
from machina.connectors.cmms import GenericCmmsConnector
from machina.connectors.comms.telegram import CliChannel
from machina.connectors.docs import DocumentStoreConnector
from machina.workflows import (
    ErrorPolicy,
    GuardCondition,
    Step,
    Trigger,
    TriggerType,
    Workflow,
)

SAMPLE_DIR = Path(__file__).resolve().parent.parent / "sample_data"


# ── Workflow 1: Spare Part Reorder ──────────────────────────────
#
# Stock drops below reorder point? The agent checks dependencies,
# assesses urgency with LLM reasoning, and places the order.

spare_part_reorder = Workflow(
    name="Spare Part Reorder",
    description="Assess urgency and reorder when stock is low.",
    trigger=Trigger(
        type=TriggerType.CONDITION,
        filter={"condition": "stock_below_reorder_point"},
    ),
    steps=[
        Step("lookup_part",
             action="cmms.read_spare_parts",
             inputs={"part_id": "{trigger.part_id}"},
             on_error=ErrorPolicy.STOP),

        Step("check_dependencies",
             action="cmms.read_assets",
             inputs={"part_id": "{trigger.part_id}"},
             on_error=ErrorPolicy.SKIP),

        Step("verify_criticality",
             action="domain.check_asset_criticality",
             guard=GuardCondition(
                 check=lambda ctx: bool(ctx.get("check_dependencies")),
                 description="Skip if no dependent assets found",
             ),
             on_error=ErrorPolicy.SKIP),

        # LLM decides: standard or expedited procurement?
        Step("assess_urgency",
             action="agent.reason",
             prompt=(
                 "Spare part {trigger.part_id} is below reorder point.\n"
                 "Stock: {trigger.current_stock}, reorder: {trigger.reorder_point}\n"
                 "Part: {lookup_part}\n"
                 "Dependent assets: {check_dependencies}\n"
                 "Criticality: {verify_criticality}\n\n"
                 "Assess urgency. Standard or expedited? Recommend quantity."
             ),
             on_error=ErrorPolicy.SKIP),

        Step("place_order",
             action="erp.create_purchase_order",
             is_write=True,
             on_error=ErrorPolicy.STOP),

        Step("notify_warehouse",
             action="channels.send_message",
             template=(
                 "Spare Part Reorder\n"
                 "Part: {trigger.part_id} -- {lookup_part.name}\n"
                 "Stock: {trigger.current_stock}\n"
                 "Assessment: {assess_urgency}"
             ),
             on_error=ErrorPolicy.NOTIFY),
    ],
)


# ── Workflow 2: Preventive Maintenance Scheduler ────────────────
#
# Every Monday at 6 AM: scan for due plans, let the LLM prioritize,
# batch-create work orders, notify planners.

preventive_scheduling = Workflow(
    name="Preventive Maintenance Scheduler",
    description="Weekly scan and prioritize due maintenance plans.",
    trigger=Trigger(
        type=TriggerType.SCHEDULE,
        filter={"cron": "0 6 * * MON"},
    ),
    steps=[
        Step("scan_plans",
             action="maintenance_scheduler.scan_due_plans",
             inputs={"horizon_days": "14"},
             on_error=ErrorPolicy.STOP),

        # LLM prioritizes by criticality and risk
        Step("prioritize_work",
             action="agent.reason",
             prompt=(
                 "Maintenance plans due within 14 days:\n"
                 "{scan_plans}\n\n"
                 "Prioritize by asset criticality and failure risk. "
                 "Return a ranked list with scheduling order."
             ),
             on_error=ErrorPolicy.SKIP),

        Step("create_work_orders",
             action="work_order_factory.create_batch",
             is_write=True,
             on_error=ErrorPolicy.RETRY,
             retries=3),

        Step("notify_planners",
             action="channels.send_message",
             template=(
                 "Weekly Preventive Plan\n"
                 "Plans due: {scan_plans.count}\n"
                 "Priority: {prioritize_work}\n"
                 "WOs created: {create_work_orders.count}"
             ),
             on_error=ErrorPolicy.NOTIFY),
    ],
)


# ── The agent with both workflows ───────────────────────────────

def build_agent(llm: str = "ollama:llama3", sandbox: bool = True) -> Agent:
    return Agent(
        name="Workflow Agent",
        plant=Plant(name="North Plant"),
        connectors=[
            GenericCmmsConnector(data_dir=SAMPLE_DIR / "cmms"),
            DocumentStoreConnector(paths=[SAMPLE_DIR / "manuals"]),
        ],
        channels=[CliChannel()],
        llm=llm,
        workflows=[spare_part_reorder, preventive_scheduling],
        sandbox=sandbox,
    )


async def run_demo(llm: str, sandbox: bool) -> None:
    """Trigger the spare part reorder workflow as a demo."""
    agent = build_agent(llm=llm, sandbox=sandbox)
    await agent.start()

    mode = "SANDBOX" if sandbox else "LIVE"
    print(f"\n  Custom Workflow Agent  |  Mode: {mode}")
    print(f"  Registered: {spare_part_reorder.name}, {preventive_scheduling.name}\n")

    # Show workflow structure
    for wf in [spare_part_reorder, preventive_scheduling]:
        print(f"  {wf.name} ({len(wf.steps)} steps):")
        for i, step in enumerate(wf.steps, 1):
            tags = []
            if step.action == "agent.reason":
                tags.append("LLM")
            if step.guard:
                tags.append("guarded")
            if step.is_write:
                tags.append("write")
            tag_str = f" [{', '.join(tags)}]" if tags else ""
            print(f"    {i}. {step.name}{tag_str}")
        print()

    # Trigger spare part reorder
    print(f"  Triggering '{spare_part_reorder.name}'...")
    result = await agent.trigger_workflow(spare_part_reorder.name, {
        "part_id": "SKF-6310",
        "current_stock": 1,
        "reorder_point": 2,
        "condition": "stock_below_reorder_point",
    })

    status = "SUCCESS" if result.success else "PARTIAL"
    print(f"\n  Result: {status} ({result.duration_seconds:.2f}s)")
    for sr in result.steps:
        icon = "+" if sr.success else "~" if sr.skipped else "x"
        print(f"    [{icon}] {sr.name}")

    await agent.stop()
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description="Custom Workflow Agent")
    parser.add_argument("--live", action="store_true", help="Execute writes")
    parser.add_argument("--llm", default="ollama:llama3", help="LLM provider:model")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    if args.verbose:
        from machina.observability.logging import configure_logging
        configure_logging(level="DEBUG")

    asyncio.run(run_demo(llm=args.llm, sandbox=not args.live))


if __name__ == "__main__":
    main()
