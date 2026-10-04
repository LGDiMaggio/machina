#!/usr/bin/env python3
"""Alarm fires on pump P-201. Agent handles it end-to-end.

Diagnose the failure, check history and spare parts, draft a work order,
notify the team, submit the work order. 6 deterministic steps — domain
services and connector calls, no LLM call — so it runs without an LLM.

    python agent.py                     # sandbox (default): writes intercepted
    python agent.py --live              # execute the notification and submit
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

_repo_root = Path(__file__).resolve().parent.parent.parent
_examples_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_repo_root / "src"))
sys.path.insert(0, str(_examples_dir))

from _mode import add_mode_flags, resolve_sandbox  # noqa: E402
from _preflight import check  # noqa: E402

from machina import Agent, Plant
from machina.connectors.cmms import GenericCmmsConnector
from machina.connectors.comms.cli import CliChannel
from machina.connectors.docs import DocumentStoreConnector
from machina.domain import Alarm, Severity
from machina.workflows.builtins import alarm_to_workorder

SAMPLE_DIR = _examples_dir / "sample_data"

# ── The agent: one workflow registration line ───────────────────
agent = Agent(
    name="Alarm Response Agent",
    plant=Plant(name="North Plant"),
    connectors=[
        GenericCmmsConnector(data_dir=SAMPLE_DIR / "cmms"),
        DocumentStoreConnector(paths=[SAMPLE_DIR / "manuals"]),
    ],
    channels=[CliChannel()],
    # The Agent needs a provider, but this workflow never calls it.
    llm="ollama:llama3",
    workflows=[alarm_to_workorder],
    sandbox=True,
    # Autonomous-by-design: this demo drives the workflow directly (see
    # trigger_workflow below), which bypasses the LLM loop and its
    # confirmation gate, so the flag is documentary here. The real guard
    # for writes on this path remains `sandbox`.
    confirmations=False,
)
# ────────────────────────────────────────────────────────────────


def _summary(output: Any) -> str:
    """One line per step output, for the console."""
    if isinstance(output, dict) and output.get("sandbox"):
        return "sandbox: intercepted, not executed"
    if isinstance(output, list):
        if not output:
            return "none"
        return ", ".join(
            f"{item.sku} (stock {item.stock_quantity})" if hasattr(item, "sku") else str(item)
            for item in output
        )
    if hasattr(output, "priority") and hasattr(output, "asset_id"):  # a WorkOrder
        return (
            f"{output.id}: {output.type.value}, priority {output.priority.value}, "
            f"failure mode {output.failure_mode}"
        )
    return str(output)


async def run_alarm_demo(sandbox: bool) -> None:
    """Simulate an alarm and trigger the workflow."""
    agent.sandbox = sandbox
    await agent.start()

    alarm = Alarm(
        id="ALM-2026-0412-001",
        asset_id="P-201",
        parameter="vibration_velocity_mm_s",
        value=7.8,
        threshold=6.0,
        severity=Severity.WARNING,
        message="High vibration on drive-end bearing",
    )

    mode = "SANDBOX" if sandbox else "LIVE"
    print(f"\n{'=' * 60}")
    print(f"  Alarm Response Agent  |  Mode: {mode}")
    print(f"{'=' * 60}")
    print(f"  Alarm:  {alarm.id}  |  Asset: {alarm.asset_id}")
    print(f"  {alarm.parameter} = {alarm.value} (threshold: {alarm.threshold})")
    print(f"\n  Workflow: {alarm_to_workorder.name} ({len(alarm_to_workorder.steps)} steps)")
    print(f"{'=' * 60}\n")

    result = await agent.trigger_workflow(
        alarm_to_workorder.name,
        {
            "alarm_id": alarm.id,
            "asset_id": alarm.asset_id,
            "parameter": alarm.parameter,
            "value": alarm.value,
            "threshold": alarm.threshold,
            "severity": alarm.severity.value,
        },
    )

    status = "SUCCESS" if result.success else "FAILED"
    print(f"\n  Result: {status} ({result.duration_seconds:.2f}s)")
    for sr in result.steps:
        icon = "+" if sr.success else "~" if sr.skipped else "x"
        print(f"    [{icon}] {sr.name:<20} {_summary(sr.output)}")

    await agent.stop()
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description="Alarm Response Agent")
    parser.add_argument("--verbose", action="store_true")

    add_mode_flags(parser, default_sandbox=True)
    args = parser.parse_args()

    check(llm=None)  # deterministic workflow: no LLM is called

    if args.verbose:
        from machina.observability.logging import configure_logging

        configure_logging(level="DEBUG")

    sandbox = resolve_sandbox(args, default=True)
    asyncio.run(run_alarm_demo(sandbox=sandbox))


if __name__ == "__main__":
    main()
