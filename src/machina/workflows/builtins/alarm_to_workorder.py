"""Alarm-to-Work-Order workflow — the most common maintenance pattern.

When a sensor alarm fires, this workflow:

1. Diagnoses the probable failure mode
2. Checks recent maintenance history
3. Verifies spare parts availability
4. Generates a work order with auto-populated fields
5. Notifies the maintenance technician
6. Submits the work order to the CMMS
"""

from machina.workflows.models import (
    ErrorPolicy,
    Step,
    Trigger,
    TriggerType,
    Workflow,
)

alarm_to_workorder = Workflow(
    name="Alarm to Work Order",
    description=(
        "End-to-end workflow: sensor alarm triggers failure diagnosis, "
        "spare part check, work order creation, and technician notification."
    ),
    trigger=Trigger(
        type=TriggerType.ALARM,
        filter={"severity": ["warning", "critical"]},
    ),
    steps=[
        Step(
            "analyze_alarm",
            action="failure_analyzer.diagnose",
            description="Diagnose probable failure modes from the alarm data",
            inputs={
                "asset_id": "{trigger.asset_id}",
                "parameter": "{trigger.parameter}",
                "value": "{trigger.value}",
                "severity": "{trigger.severity}",
            },
            on_error=ErrorPolicy.STOP,
        ),
        Step(
            "check_history",
            action="cmms.read_maintenance_history",
            description="Retrieve recent maintenance history for the asset",
            inputs={"asset_id": "{trigger.asset_id}"},
            on_error=ErrorPolicy.SKIP,
        ),
        Step(
            "check_spare_parts",
            action="cmms.read_spare_parts",
            description="Verify spare parts availability for the diagnosed failure mode",
            inputs={"asset_id": "{trigger.asset_id}"},
            on_error=ErrorPolicy.SKIP,
        ),
        Step(
            "generate_work_order",
            action="work_order_factory.create",
            description="Create a work order with auto-populated fields",
            inputs={
                "asset_id": "{trigger.asset_id}",
                # `analyze_alarm` returns a DiagnosisResult; .failure_mode_for_write
                # extracts the top-ranked code ONLY when the diagnosis is at least
                # medium-confidence, else None (U6) — so a low-confidence guess is
                # never stamped onto the WO as fact. `WorkOrder.failure_mode` is
                # `str | None`; the notification still surfaces the full ranked
                # diagnosis with its confidence labels via `{analyze_alarm}`.
                "failure_mode": "{analyze_alarm.failure_mode_for_write}",
                "description": (
                    "Auto-generated from alarm {trigger.alarm_id} on {trigger.asset_id}. "
                    "Diagnosis: {analyze_alarm}"
                ),
            },
            on_error=ErrorPolicy.STOP,
        ),
        Step(
            "notify_technician",
            action="channels.send_message",
            description="Send notification to the assigned technician",
            is_write=True,  # external side effect — gate explicitly, never via heuristic (U11)
            template=(
                "⚠️ New Work Order — {trigger.asset_id}\n\n"
                "Diagnosis: {analyze_alarm}\n"
                "Work Order: {generate_work_order}\n"
                "Spare Parts: {check_spare_parts}"
            ),
            on_error=ErrorPolicy.NOTIFY,
        ),
        Step(
            "submit_work_order",
            action="cmms.create_work_order",
            description="Submit the confirmed work order to the CMMS",
            is_write=True,  # the real external CMMS write — gate explicitly (U11)
            inputs={"work_order": "{generate_work_order}"},
            on_error=ErrorPolicy.STOP,
        ),
    ],
)
