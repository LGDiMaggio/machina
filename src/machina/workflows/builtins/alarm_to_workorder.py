"""Alarm-to-Work-Order workflow — the most common maintenance pattern.

When a sensor alarm fires, this workflow:

1. Diagnoses the probable failure mode
2. Checks recent maintenance history
3. Verifies spare parts availability
4. Generates a work order with auto-populated fields
5. Notifies the maintenance technician
6. Awaits confirmation
7. Submits the work order to the CMMS
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
            on_error=ErrorPolicy.STOP,
        ),
        Step(
            "check_history",
            action="cmms.get_asset_history",
            description="Retrieve recent maintenance history for the asset",
            inputs={"asset_id": "{trigger.asset_id}"},
            on_error=ErrorPolicy.SKIP,
        ),
        Step(
            "check_spare_parts",
            action="cmms.check_spare_parts",
            description="Verify spare parts availability for the diagnosed failure mode",
            inputs={"asset_id": "{trigger.asset_id}"},
            on_error=ErrorPolicy.SKIP,
        ),
        Step(
            "generate_work_order",
            action="work_order_factory.create",
            description="Create a work order with auto-populated fields",
            on_error=ErrorPolicy.STOP,
        ),
        Step(
            "notify_technician",
            action="channels.send_message",
            description="Send notification to the assigned technician",
            template=(
                "⚠️ New Work Order — {trigger.asset_id}\n\n"
                "Diagnosis: {analyze_alarm}\n"
                "Work Order: {generate_work_order}\n"
                "Spare Parts: {check_spare_parts}"
            ),
            on_error=ErrorPolicy.NOTIFY,
        ),
        Step(
            "await_confirmation",
            action="channels.wait_for_reply",
            description="Wait for the technician to confirm the work order",
            on_error=ErrorPolicy.SKIP,
            timeout_seconds=3600,
        ),
        Step(
            "submit_work_order",
            action="cmms.create_work_order",
            description="Submit the confirmed work order to the CMMS",
            on_error=ErrorPolicy.STOP,
        ),
    ],
)
