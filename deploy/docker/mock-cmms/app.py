"""Mock CMMS — tiny FastAPI app for offline Docker demos.

Serves the REST contract that Machina's ``GenericCmmsConnector`` speaks, with
in-memory data, so ``docker compose up`` works without a real CMMS. Every
request must carry a bearer token; any non-empty token is accepted.

Endpoint contract (paths relative to the connector's ``url``):
  GET   /health                  → health check (called on connect)
  GET   /assets                  → list of assets
  GET   /assets/{asset_id}       → single asset
  GET   /work_orders             → list of work orders (?asset_id=, ?status=)
  POST  /work_orders             → create a work order (idempotent on ``id``)
  GET   /work_orders/{wo_id}     → single work order   (endpoint get_work_order)
  PATCH /work_orders/{wo_id}     → update a work order (endpoint update_work_order)
  GET   /maintenance_plans       → maintenance plans   (endpoint read_maintenance_plans)

Data lives in memory and resets when the container restarts.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException

app = FastAPI(title="Machina Mock CMMS", version="0.2.0")


def require_bearer(authorization: str = Header(default="")) -> None:
    """Reject requests without a bearer token (any non-empty token passes)."""
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(status_code=401, detail="Bearer token required")


ASSETS: list[dict[str, Any]] = [
    {
        "id": "P-201",
        "name": "Centrifugal Pump — Cooling Loop A",
        "type": "rotating_equipment",
        "location": "Building A / Floor 1 / Bay 3",
        "criticality": "A",
        "manufacturer": "Grundfos",
        "model": "CR 32-2",
    },
    {
        "id": "V-101",
        "name": "Isolation Valve — Steam Header",
        "type": "piping",
        "location": "Building B / Floor 0 / Bay 1",
        "criticality": "B",
        "manufacturer": "Emerson",
        "model": "Fisher GX",
    },
    {
        "id": "M-301",
        "name": "Induction Motor — Conveyor Drive",
        "type": "electrical",
        "location": "Warehouse / Line 3",
        "criticality": "C",
        "manufacturer": "ABB",
        "model": "M3BP 160 MLA",
    },
]

WORK_ORDERS: list[dict[str, Any]] = [
    {
        "id": "WO-2026-001",
        "type": "corrective",
        "priority": "high",
        "status": "created",
        "asset_id": "P-201",
        "description": "Replace drive-end bearing — elevated vibration detected",
        "assigned_to": None,
    },
    {
        "id": "WO-2026-002",
        "type": "preventive",
        "priority": "medium",
        "status": "assigned",
        "asset_id": "V-101",
        "description": "Quarterly valve stroke test",
        "assigned_to": "Maintenance Team B",
    },
]

MAINTENANCE_PLANS: list[dict[str, Any]] = [
    {
        "id": "MP-P201-Q",
        "asset_id": "P-201",
        "name": "Quarterly Bearing Inspection",
        "interval": {"days": 90},
        "tasks": ["Check vibration levels", "Inspect seal condition", "Verify lubrication"],
        "active": True,
    },
]

_UPDATABLE_FIELDS = ("status", "assigned_to", "description")


def _find(items: list[dict[str, Any]], item_id: str, kind: str) -> dict[str, Any]:
    for item in items:
        if item["id"] == item_id:
            return item
    raise HTTPException(status_code=404, detail=f"{kind} {item_id!r} not found")


@app.get("/health", dependencies=[Depends(require_bearer)])
def health() -> dict[str, str]:
    return {"status": "healthy", "service": "mock-cmms"}


@app.get("/assets", dependencies=[Depends(require_bearer)])
def list_assets() -> list[dict[str, Any]]:
    return ASSETS


@app.get("/assets/{asset_id}", dependencies=[Depends(require_bearer)])
def get_asset(asset_id: str) -> dict[str, Any]:
    return _find(ASSETS, asset_id, "Asset")


@app.get("/work_orders", dependencies=[Depends(require_bearer)])
def list_work_orders(asset_id: str = "", status: str = "") -> list[dict[str, Any]]:
    return [
        wo
        for wo in WORK_ORDERS
        if (not asset_id or wo["asset_id"] == asset_id) and (not status or wo["status"] == status)
    ]


@app.post("/work_orders", dependencies=[Depends(require_bearer)])
def create_work_order(body: dict[str, Any]) -> dict[str, Any]:
    wo_id = str(body.get("id") or f"WO-{datetime.now(UTC):%Y}-{len(WORK_ORDERS) + 1:03d}")
    for wo in WORK_ORDERS:
        if wo["id"] == wo_id:
            return wo  # same id → same work order, never a duplicate
    if not any(a["id"] == body.get("asset_id") for a in ASSETS):
        raise HTTPException(status_code=422, detail=f"Unknown asset {body.get('asset_id')!r}")
    wo = {"status": "created", "assigned_to": None, **body, "id": wo_id}
    WORK_ORDERS.append(wo)
    return wo


@app.get("/work_orders/{wo_id}", dependencies=[Depends(require_bearer)])
def get_work_order(wo_id: str) -> dict[str, Any]:
    return _find(WORK_ORDERS, wo_id, "Work order")


@app.patch("/work_orders/{wo_id}", dependencies=[Depends(require_bearer)])
def update_work_order(wo_id: str, body: dict[str, Any]) -> dict[str, Any]:
    wo = _find(WORK_ORDERS, wo_id, "Work order")
    wo.update({k: v for k, v in body.items() if k in _UPDATABLE_FIELDS})
    return wo


@app.get("/maintenance_plans", dependencies=[Depends(require_bearer)])
def list_maintenance_plans() -> list[dict[str, Any]]:
    return MAINTENANCE_PLANS
