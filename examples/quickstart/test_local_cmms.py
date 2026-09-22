#!/usr/bin/env python3
"""Your first maintenance agent — wrapped in FastAPI.

Required installations:
pip install machina-ai[litellm] fastapi uvicorn pydantic

To run the server:
uvicorn agent:app --reload
"""

from __future__ import annotations

import sys
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import structlog

_repo_root = Path(__file__).resolve().parent.parent.parent
_examples_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_repo_root / "src"))
sys.path.insert(0, str(_examples_dir))

from machina import Agent, Plant
from machina.connectors.cmms import GenericCmmsConnector
from machina.connectors.cmms.auth import BearerAuth
from machina.connectors.cmms.generic_schema import (
    GenericCmmsYamlConfig,
    EntityMapping,
    EndpointSpec,
    FieldSpec,
    ReverseFieldSpec
)
from machina.connectors.docs import DocumentStoreConnector
from machina.connectors.docs import ExcelCsvConnector
from machina.connectors.sql import GenericSqlConnector
from machina.connectors.sql.schema import SqlConnectorConfig
from machina.connectors.iot.simulated import SimulatedSensorConnector
from machina.connectors.docs.excel_schema import ExcelConnectorConfig, SheetSchema, ColumnMapping
from machina.workflows import Workflow, Step, Trigger, TriggerType, ErrorPolicy
from contextvars import ContextVar
from machina.connectors.cmms.auth import NoAuth

logger = structlog.get_logger()

# Turn these to False to disable specific systems during testing
ENABLE_EXCEL_CMMS = False   
ENABLE_DOCUMENTS = False
ENABLE_SENSORS = False
ENABLE_WORKFLOWS = False
ENABLE_SQL = False

ENABLE_CMMS = True

# --- 1. The Connector Factory ---
SAMPLE_DIR = _examples_dir / "sample_data"
active_connectors = []

if ENABLE_EXCEL_CMMS:
    excel_path = str(SAMPLE_DIR / "test.xlsx")
    
    excel_config = ExcelConnectorConfig(
        asset_registry=SheetSchema(
            path=excel_path,
            sheet="Assets",
            columns=[
                ColumnMapping(column="equip_id", field="id", required=True),
                ColumnMapping(column="equip_desc", field="name", required=True),
                ColumnMapping(column="equip_category", field="type"),
                ColumnMapping(column="risk_level", field="criticality"),
                ColumnMapping(column="iso_class", field="equipment_class_code"),
                ColumnMapping(column="parent_equip_id", field="parent_id")
            ]
        ),
        work_orders=SheetSchema(
            path=excel_path,
            sheet="Sheet1",
            write_mode="append",
            columns=[
                ColumnMapping(column="ticket_num", field="id", required=True),
                ColumnMapping(column="target_equip_id", field="asset_id", required=True),
                ColumnMapping(column="job_type", field="type"),
                ColumnMapping(column="current_state", field="status"),
                ColumnMapping(column="urgency", field="priority"),
                ColumnMapping(column="issue_notes", field="description")
            ]
        )
    )
    active_connectors.append(ExcelCsvConnector(config=excel_config))

if ENABLE_DOCUMENTS:
    active_connectors.append(DocumentStoreConnector(paths=[SAMPLE_DIR / "manuals"]))

if ENABLE_CMMS:
    # Import the Pagination helper
    from machina.connectors.cmms.pagination import PageNumberPagination
    from machina.connectors.cmms.auth import NoAuth
    from contextvars import ContextVar

    current_jwt = ContextVar("current_jwt", default="eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpZCI6MiwidXNlcm5hbWUiOiJ0ZXN0IiwiZmlyc3RfbmFtZSI6InRlc3QiLCJyb2xlIjoiQURNSU4iLCJjb21wYW55X2lkIjoxLCJjb21wYW55X25hbWUiOiJGZXJyZXJvIiwiY3VycmVudF9hY3RpdmVfbG9jYXRpb25faWQiOm51bGwsImFsbG93ZWRfbG9jYXRpb25zIjpbXSwiaWF0IjoxNzg4NDI0NzExLCJleHAiOjE3ODg0NTM1MTF9.p5muoRPEVOpg6oMTsNJCykTYSp8JzCgCX02c-aNwP9Y")

    cmms_schema = GenericCmmsYamlConfig(
        mapping={
            # ==========================================
            # 1. ASSETS (Machines)
            # ==========================================
            "asset": EntityMapping(
                endpoint=EndpointSpec(path="asset/machines"), 
                fields={
                    "id": FieldSpec(source="id"),
                    "name": FieldSpec(source="name"),
                    "manufacturer": FieldSpec(source="manufacturer"),
                    "model": FieldSpec(source="model"),
                    "serial_number": FieldSpec(source="serial_number"),
                    "parent_id": FieldSpec(source="line_id")
                }
            ),
            
            # ==========================================
            # 2. WORK ORDERS
            # ==========================================
            "work_order": EntityMapping(
                endpoint=EndpointSpec(path="work-orders"), 
                create_endpoint=EndpointSpec(path="work-orders", method="POST"),
                fields={
                    "id": FieldSpec(source="id"),
                    "asset_id": FieldSpec(source="machine_id"),
                    "type": FieldSpec(source="wo_type", coerce="lowercase"),
                    "priority": FieldSpec(source="priority", coerce="lowercase"),
                    "description": FieldSpec(source="description", default=""),
                    "status": FieldSpec(
                        source="status",
                        coerce="enum_map",
                        enum_map={
                            "OPEN": "created",
                            "ASSIGNED": "assigned",
                            "IN_PROGRESS": "in_progress",
                            "WAITING_FOR_PARTS": "in_progress",
                            "COMPLETED": "completed",
                            "RESOLVED": "completed",
                            "CANCELLED": "cancelled"
                        }
                    )
                },
                reverse_fields={
                    "asset_id": "machine_id",
                    "description": "title", 
                    "type": ReverseFieldSpec(
                        target="wo_type",
                        reverse_enum_map={
                            "preventive": "PREVENTIVE",
                            "corrective": "CORRECTIVE",
                            "predictive": "PREDICTIVE",
                            "inspection": "INSPECTION"
                        }
                    ),
                    "priority": ReverseFieldSpec(
                        target="priority",
                        reverse_enum_map={
                            "low": "LOW",
                            "medium": "MEDIUM",
                            "high": "HIGH",
                            "critical": "CRITICAL"
                        }
                    ),
                    "status": ReverseFieldSpec(
                        target="status",
                        reverse_enum_map={
                            "created": "OPEN",
                            "assigned": "ASSIGNED",
                            "in_progress": "IN_PROGRESS",
                            "completed": "COMPLETED",
                            "cancelled": "CANCELLED"
                        }
                    )
                }
            )
        }
    )

    rest_cmms_connector = GenericCmmsConnector(
        url="http://localhost:5000/api/", 
        auth=NoAuth(),
        pagination=PageNumberPagination(
            page_param="page",
            page_size_param="limit",
            page_size=25,
            items_path="data"
        ),
        yaml_mapping=cmms_schema,
        endpoints={
            "assets": {"path": "asset/machines"}, 
            "work_orders": {"path": "work-orders"},
            "get_work_order": {"path": "work-orders/{id}"},
            "update_work_order": {"path": "work-orders/{id}", "method": "PUT"},
            "read_maintenance_plans": {"path": "maintenance/plans"}
        }
    )

    # Patch the headers method to dynamically inject the user's JWT
    def _dynamic_headers() -> dict[str, str]:
        token = current_jwt.get()
        return {"Authorization": f"Bearer {token}"} if token else {}

    rest_cmms_connector._rest_headers = _dynamic_headers

    active_connectors.append(rest_cmms_connector)

if ENABLE_SENSORS:
    active_connectors.append(SimulatedSensorConnector(data_dir=SAMPLE_DIR / "sensor_logs"))

if ENABLE_SQL:
    db_path = SAMPLE_DIR / "custom_cmms.db"
    
    sql_config = SqlConnectorConfig(
        dsn=f"Driver={{SQLite3 ODBC Driver}};Database={db_path};", 
        driver_type="odbc",
        capabilities="read_write", 
        tables={
            "assets": {
                "entity": "Asset",
                "query": "SELECT * FROM plant_equipment",
                "fields": {
                    "id": {"column": "equip_id"},
                    "name": {"column": "equip_desc"},
                    "type": {"column": "equip_category"},
                    "criticality": {"column": "risk_level"},
                    "equipment_class_code": {"column": "iso_class"},
                    "parent_id": {"column": "parent_equip_id"}
                }
            },
            "work_orders": {
                "entity": "WorkOrder",
                "query": "SELECT * FROM maintenance_tickets",
                "insert_table": "maintenance_tickets",
                "insert_columns": {
                    "id": "ticket_num",
                    "asset_id": "target_equip_id",
                    "type": "job_type",
                    "status": "current_state",
                    "priority": "urgency",
                    "description": "issue_notes",
                    "failure_impact": "functional_impact"
                },
                "fields": {
                    "id": {"column": "ticket_num"},
                    "asset_id": {"column": "target_equip_id"},
                    "type": {"column": "job_type"},
                    "status": {"column": "current_state"},
                    "priority": {"column": "urgency"},
                    "description": {"column": "issue_notes"},
                    "failure_impact": {"column": "functional_impact"}
                }
            },
            "failure_modes": {
                "entity": "FailureMode",
                "query": "SELECT * FROM known_faults",
                "fields": {
                    "code": {"column": "fault_code"},
                    "name": {"column": "fault_label"},
                    "iso_14224_code": {"column": "iso_fault_id"},
                    "mechanism": {"column": "root_cause_mech"},
                    "typical_indicators": {"column": "symptoms_list"},
                    "recommended_actions": {"column": "fix_procedures"}
                }
            },
        }
    )
    active_connectors.append(GenericSqlConnector(config=sql_config))

# --- 2. The Workflow Factory ---
active_workflows = []

if ENABLE_WORKFLOWS:
    predictive_workflow = Workflow(
        name="Automated Alarm Response",
        description="Diagnose critical alarms and draft a work order.",
        trigger=Trigger(type=TriggerType.ALARM, filter={"severity": ["critical"]}),
        steps=[
            Step(name="diagnose", action="failure_analyzer.diagnose", on_error=ErrorPolicy.STOP),
            Step(name="create_wo", action="work_order_factory.create", on_error=ErrorPolicy.STOP),
            Step(
                name="notify", 
                action="channels.send_message", 
                template="WO Drafted for {trigger.asset_id} based on {trigger.parameter} alarm. Diagnosis: {diagnose}", 
                on_error=ErrorPolicy.STOP
            ),
        ]
    )
    active_workflows.append(predictive_workflow)

    scheduled_pm_workflow = Workflow(
        name="Weekly Extruder Inspection",
        description="Drafts a routine inspection work order every Monday.",
        trigger=Trigger(type=TriggerType.SCHEDULE, filter={"cron": "0 8 * * 1"}),
        steps=[
            Step(name="check_history", action="cmms.read_work_orders", inputs={"asset_id": "EXT-01"}, on_error=ErrorPolicy.SKIP),
            Step(name="draft_pm", action="work_order_factory.create", prompt="Create a PREVENTIVE work order for EXT-01. Note any recurring issues found in the history: {check_history}", on_error=ErrorPolicy.STOP)
        ]
    )
    active_workflows.append(scheduled_pm_workflow)

    p201_predictive_workflow = Workflow(
        name="P-201 Predictive Bearing Analysis",
        description="Monitor P-201 for climbing temperature/vibration, check its history, and draft a predictive work order before failure.",
        trigger=Trigger(type=TriggerType.ALARM, filter={"asset_id": ["P-201"]}),
        steps=[
            Step(name="get_trend", action="sensors.get_related_readings", inputs={"asset_id": "P-201"}, on_error=ErrorPolicy.SKIP),
            Step(name="check_history", action="cmms.read_work_orders", on_error=ErrorPolicy.SKIP),
            Step(
                name="draft_wo",
                action="cmms.create_work_order",
                prompt=(
                    "Create a PREVENTIVE work order for asset P-201 with HIGH priority. "
                    "Status should be CREATED. "
                    "Review the recent sensor trend: {get_trend}. "
                    "Review the maintenance history: {check_history}. "
                    "In the description, explicitly state that temperature and vibration are steadily climbing. "
                    "Note that this matches historical bearing wear patterns, and recommend replacing "
                    "the specific SKF bearing mentioned in the past history."
                ),
                on_error=ErrorPolicy.STOP
            ),
            Step(
                name="notify",
                action="channels.send_message",
                template="⚠️ PREDICTIVE ALERT: P-201 shows climbing temp/vibration. Work order {draft_wo.id} drafted based on historical bearing failure patterns.",
                on_error=ErrorPolicy.STOP
            ),
        ]
    )
    active_workflows.append(p201_predictive_workflow)


# --- 3. FastAPI Application & Agent Lifecycle ---
machina_agent = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global machina_agent
    logger.info("Initializing Machina Agent and Connectors...")

    # We build the agent here without the CliChannel
    machina_agent = Agent(
        name="Maintenance Assistant",
        plant=Plant(name="Demo Plant"),
        connectors=active_connectors,
        llm="ollama:llama3", # Set to your desired LLM
        workflows=active_workflows,
        sandbox=False # Set to True if you want a read-only testing environment
    )

    await machina_agent.start()
    logger.info("Machina Agent is online and ready for REST requests.")

    yield # The FastAPI server runs while yielding here

    logger.info("Shutting down Machina Agent...")
    await machina_agent.stop()

# Initialize FastAPI
app = FastAPI(title="Machina CMMS API", lifespan=lifespan)

# Add CORS Middleware so React can communicate with it
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://localhost:5173"], 
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- 4. API Endpoints ---
class ChatRequest(BaseModel):
    message: str
    chat_id: str = "default_session" 

class ChatResponse(BaseModel):
    reply: str

@app.post("/api/chat", response_model=ChatResponse)
async def chat_endpoint(request: ChatRequest):
    """Passes the React message directly into the Machina Agent loop."""
    if machina_agent is None:
        raise HTTPException(status_code=503, detail="Agent is not initialized yet.")

    try:
        # Pass the message content positionally rather than using content= keyword
        response_obj = await machina_agent.handle_message_full(
            request.message,
            chat_id=request.chat_id
        )
        return ChatResponse(reply=str(response_obj))
    except Exception as e:
        logger.error(f"Error handling message: {e}")
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn
    # This allows you to run it via `python agent.py` or `uvicorn agent:app --reload`
    uvicorn.run(app, host="0.0.0.0", port=8000)