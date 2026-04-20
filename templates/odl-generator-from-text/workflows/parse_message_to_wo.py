"""Message-to-Work-Order workflow.

Comms event → entity resolution → WO creation → reply to technician.

This workflow is channel-agnostic: it works with email, Telegram, or any
future comms connector (WhatsApp in v0.3.1) without changes.
"""

from machina.workflows.models import (
    ErrorPolicy,
    Step,
    Trigger,
    TriggerType,
    Workflow,
)

message_to_workorder = Workflow(
    name="Free-Text Message to Work Order",
    description=(
        "Receives a free-text maintenance request from a technician, "
        "resolves asset references against the plant registry, creates "
        "structured Work Orders, and replies with confirmation."
    ),
    trigger=Trigger(
        type=TriggerType.MANUAL,
        filter={},
    ),
    steps=[
        Step(
            "parse_message",
            action="llm.chat",
            description=(
                "Parse the Italian free-text message to extract asset "
                "references, failure descriptions, and priority hints. "
                "Use the IT entity-resolver prompt template."
            ),
            prompt="{trigger.text}",
            on_error=ErrorPolicy.STOP,
        ),
        Step(
            "resolve_entities",
            action="entity_resolver.resolve",
            description="Match extracted asset references against the plant registry",
            inputs={"text": "{parse_message}"},
            on_error=ErrorPolicy.STOP,
        ),
        Step(
            "create_work_orders",
            action="work_order_factory.create_batch",
            description=(
                "Create structured Work Orders for each resolved asset "
                "with inferred failure mode and priority"
            ),
            inputs={"entities": "{resolve_entities}"},
            on_error=ErrorPolicy.STOP,
            is_write=True,
        ),
        Step(
            "write_to_substrate",
            action="cmms.create_work_order",
            description="Write each Work Order to the configured substrate (Excel or CMMS)",
            inputs={"work_orders": "{create_work_orders}"},
            on_error=ErrorPolicy.STOP,
            is_write=True,
        ),
        Step(
            "reply_to_technician",
            action="channels.send_message",
            description="Send confirmation back to the technician on the original channel",
            template=(
                "OdL creati:\n\n"
                "{create_work_orders}\n\n"
                "Se un asset non corrisponde, rispondi CORREGGI <id>."
            ),
            on_error=ErrorPolicy.NOTIFY,
        ),
    ],
)
