#!/usr/bin/env python3
"""Alarm fires on pump P-201. Agent handles it end-to-end.

The built-in alarm-to-work-order workflow: diagnose, check parts,
create work order, notify the team. 6 steps, only 2 use the LLM.

    python agent.py                     # sandbox (default)
    python agent.py --live              # execute writes
    python agent.py --llm openai:gpt-4o

Email notifications (optional): set MACHINA_SMTP_HOST, MACHINA_SMTP_USER,
MACHINA_SMTP_PASSWORD and MACHINA_NOTIFY_EMAIL to also send the
notification step via SMTP. Without these the agent uses CliChannel only.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

_repo_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_repo_root / "src"))

from machina import Agent, Plant
from machina.connectors.cmms import GenericCmmsConnector
from machina.connectors.comms.email import EmailConnector
from machina.connectors.comms.telegram import CliChannel
from machina.connectors.docs import DocumentStoreConnector
from machina.domain import Alarm, Severity
from machina.workflows.builtins import alarm_to_workorder

SAMPLE_DIR = Path(__file__).resolve().parent.parent / "sample_data"


def _build_channels() -> list[object]:
    """Always include CliChannel; add EmailConnector when SMTP env vars are set.

    This wiring is the discoverability point for the EmailConnector — running
    `python agent.py` without env vars stays a zero-config demo, but the
    import + construction code is right here for any reader who wants to see
    how Email plugs into a workflow.
    """
    channels: list[object] = [CliChannel()]

    smtp_host = os.environ.get("MACHINA_SMTP_HOST")
    smtp_user = os.environ.get("MACHINA_SMTP_USER")
    smtp_password = os.environ.get("MACHINA_SMTP_PASSWORD")

    if smtp_host and smtp_user and smtp_password:
        channels.append(
            EmailConnector(
                smtp_host=smtp_host,
                smtp_port=int(os.environ.get("MACHINA_SMTP_PORT", "465")),
                username=smtp_user,
                password=smtp_password,
                from_address=os.environ.get("MACHINA_SMTP_FROM", smtp_user),
            )
        )
    return channels


# ── The agent: one workflow registration line ───────────────────
agent = Agent(
    name="Alarm Response Agent",
    plant=Plant(name="North Plant"),
    connectors=[
        GenericCmmsConnector(data_dir=SAMPLE_DIR / "cmms"),
        DocumentStoreConnector(paths=[SAMPLE_DIR / "manuals"]),
    ],
    channels=_build_channels(),  # CliChannel + EmailConnector when configured
    llm="ollama:llama3",
    workflows=[alarm_to_workorder],  # <-- the built-in template
    sandbox=True,  # safe by default
)
# ────────────────────────────────────────────────────────────────


async def run_alarm_demo(llm: str, sandbox: bool) -> None:
    """Simulate an alarm and trigger the workflow."""
    agent.llm = llm
    agent.sandbox = sandbox
    await agent.start()

    # Simulated alarm — in production this comes from OPC-UA / MQTT
    alarm = Alarm(
        id="ALM-2026-0412-001",
        asset_id="P-201",
        parameter="vibration_velocity_mm_s",
        value=7.8,
        threshold=6.0,
        severity=Severity.WARNING,
        message="High vibration on drive-end bearing — exceeds ISO 10816-3 Zone B limit",
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
        print(f"    [{icon}] {sr.name}")

    await agent.stop()
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description="Alarm Response Agent")
    parser.add_argument("--live", action="store_true", help="Execute writes (default: sandbox)")
    parser.add_argument("--llm", default="ollama:llama3", help="LLM provider:model")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    # Pre-flight: check sample data, LLM provider, and required extras
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from _preflight import check

    check(llm=args.llm)

    if args.verbose:
        from machina.observability.logging import configure_logging

        configure_logging(level="DEBUG")

    asyncio.run(run_alarm_demo(llm=args.llm, sandbox=not args.live))


if __name__ == "__main__":
    main()
