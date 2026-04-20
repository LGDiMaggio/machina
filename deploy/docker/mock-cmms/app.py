"""Mock CMMS — tiny FastAPI app for offline Docker demos.

Serves SAP PM / Maximo-compatible endpoints with hardcoded responses
so ``docker compose up`` works without a real CMMS.

Endpoint contract:
  GET  /api/assets              → list of assets
  GET  /api/assets/{asset_id}   → single asset
  GET  /api/work-orders         → list of work orders
  GET  /api/work-orders/{wo_id} → single work order
  POST /api/work-orders         → create work order (returns echo)
  GET  /api/spare-parts         → list of spare parts
  GET  /api/maintenance-plans   → list of maintenance plans
  GET  /health                  → health check
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import FastAPI

app = FastAPI(title="Machina Mock CMMS", version="0.1.0")

ASSETS = [
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
        "type": "safety",
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

WORK_ORDERS = [
    {
        "id": "WO-2026-001",
        "type": "corrective",
        "priority": "high",
        "status": "created",
        "asset_id": "P-201",
        "description": "Replace drive-end bearing — elevated vibration detected",
        "assigned_to": "Mario Rossi",
    },
    {
        "id": "WO-2026-002",
        "type": "preventive",
        "priority": "medium",
        "status": "assigned",
        "asset_id": "V-101",
        "description": "Quarterly valve stroke test",
        "assigned_to": None,
    },
]

SPARE_PARTS = [
    {
        "sku": "SKF-6310",
        "name": "Deep Groove Ball Bearing 6310",
        "stock_quantity": 4,
        "reorder_point": 2,
        "unit_cost": 45.00,
    },
    {
        "sku": "SEAL-CR32-KIT",
        "name": "Mechanical Seal Kit — CR 32",
        "stock_quantity": 1,
        "reorder_point": 1,
        "unit_cost": 280.00,
    },
]

MAINTENANCE_PLANS = [
    {
        "id": "MP-P201-Q",
        "asset_id": "P-201",
        "name": "Quarterly Bearing Inspection",
        "interval_days": 90,
        "tasks": ["Check vibration levels", "Inspect seal condition", "Verify lubrication"],
    },
]


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "healthy", "service": "mock-cmms"}


@app.get("/api/assets")
def list_assets() -> list[dict[str, Any]]:
    return ASSETS


@app.get("/api/assets/{asset_id}")
def get_asset(asset_id: str) -> dict[str, Any]:
    for a in ASSETS:
        if a["id"] == asset_id:
            return a
    return {"error": f"Asset {asset_id!r} not found"}


@app.get("/api/work-orders")
def list_work_orders() -> list[dict[str, Any]]:
    return WORK_ORDERS


@app.get("/api/work-orders/{wo_id}")
def get_work_order(wo_id: str) -> dict[str, Any]:
    for wo in WORK_ORDERS:
        if wo["id"] == wo_id:
            return wo
    return {"error": f"Work order {wo_id!r} not found"}


@app.post("/api/work-orders")
def create_work_order(body: dict[str, Any]) -> dict[str, Any]:
    wo_id = f"WO-{datetime.now().strftime('%Y')}-{len(WORK_ORDERS) + 1:03d}"
    return {
        "id": wo_id,
        "status": "created",
        **body,
    }


@app.get("/api/spare-parts")
def list_spare_parts() -> list[dict[str, Any]]:
    return SPARE_PARTS


@app.get("/api/maintenance-plans")
def list_maintenance_plans() -> list[dict[str, Any]]:
    return MAINTENANCE_PLANS
