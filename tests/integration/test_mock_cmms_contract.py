"""Contract test: the Docker demo's Generic CMMS config against its mock CMMS.

``deploy/docker/config.yaml`` points a ``generic_cmms`` connector at the
FastAPI mock in ``deploy/docker/mock-cmms/app.py``. These tests run that
connector, with the endpoints the config declares, against the mock app
in-process (``httpx.ASGITransport``, no network), so a change to either side
that breaks the other fails here rather than in ``docker compose up``.

Requires FastAPI, which is not a Machina dependency: the module is skipped
where it is not installed.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import pytest
import yaml

from machina.connectors.capabilities import Capability
from machina.connectors.cmms.generic import GenericCmmsConnector
from machina.domain.work_order import WorkOrder, WorkOrderStatus, WorkOrderType
from machina.exceptions import ConnectorError

if TYPE_CHECKING:
    from types import ModuleType

pytest.importorskip("fastapi")

_DOCKER_DIR = Path(__file__).resolve().parents[2] / "deploy" / "docker"
_URL = "http://mock-cmms:9000"


def _load_mock_app() -> ModuleType:
    """Import a fresh copy of the mock app, so each test starts from its seed data."""
    spec = importlib.util.spec_from_file_location(
        "machina_test_mock_cmms_app", _DOCKER_DIR / "mock-cmms" / "app.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _configured_endpoints() -> dict[str, Any]:
    config = yaml.safe_load((_DOCKER_DIR / "config.yaml").read_text(encoding="utf-8"))
    endpoints: dict[str, Any] = config["connectors"]["cmms"]["settings"]["endpoints"]
    return endpoints


@pytest.fixture
def mock_app(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """The mock app, with every httpx.AsyncClient routed to it in-process."""
    module = _load_mock_app()
    real_client = httpx.AsyncClient

    def client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.ASGITransport(app=module.app)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    return module


@pytest.fixture
async def cmms(mock_app: ModuleType) -> GenericCmmsConnector:
    """The connector as deploy/docker/config.yaml configures it, connected."""
    conn = GenericCmmsConnector(url=_URL, api_key="demo-token", endpoints=_configured_endpoints())
    await conn.connect()
    return conn


def test_config_declares_every_optional_capability() -> None:
    """The demo config enables every optional REST endpoint the mock serves."""
    conn = GenericCmmsConnector(url=_URL, api_key="demo-token", endpoints=_configured_endpoints())
    assert set(GenericCmmsConnector._OPTIONAL_CAPABILITIES) <= conn.capabilities
    assert Capability.READ_FAILURE_MODES not in conn.capabilities


@pytest.mark.asyncio
async def test_connect_requires_a_bearer_token(mock_app: ModuleType) -> None:
    conn = GenericCmmsConnector(url=_URL, auth={"type": "none"})
    with pytest.raises(ConnectorError, match=r"^CMMS health check failed: HTTP 401$"):
        await conn.connect()


@pytest.mark.asyncio
async def test_assets(cmms: GenericCmmsConnector) -> None:
    assets = await cmms.read_assets()
    assert [a.id for a in assets] == ["P-201", "V-101", "M-301"]
    asset = await cmms.get_asset("P-201")
    assert asset is not None
    assert asset.criticality.value == "A"
    assert await cmms.get_asset("X-999") is None


@pytest.mark.asyncio
async def test_work_order_lifecycle(cmms: GenericCmmsConnector) -> None:
    open_for_pump = await cmms.read_work_orders(asset_id="P-201", status="created")
    assert [wo.id for wo in open_for_pump] == ["WO-2026-001"]

    new = WorkOrder(
        id="WO-TEST-1",
        type=WorkOrderType.CORRECTIVE,
        asset_id="M-301",
        description="Cooling fan noise",
    )
    created = await cmms.create_work_order(new)
    again = await cmms.create_work_order(new)
    assert created.id == again.id == "WO-TEST-1"
    assert [wo.id for wo in await cmms.read_work_orders(asset_id="M-301")].count("WO-TEST-1") == 1

    updated = await cmms.update_work_order("WO-TEST-1", assigned_to="Team C")
    assert updated.assigned_to == "Team C"
    closed = await cmms.close_work_order("WO-TEST-1")
    assert closed.status == WorkOrderStatus.CLOSED
    fetched = await cmms.get_work_order("WO-TEST-1")
    assert fetched is not None
    assert fetched.status == WorkOrderStatus.CLOSED
    assert await cmms.get_work_order("WO-0000") is None


@pytest.mark.asyncio
async def test_create_for_an_unknown_asset_is_refused(cmms: GenericCmmsConnector) -> None:
    new = WorkOrder(id="WO-TEST-2", type=WorkOrderType.CORRECTIVE, asset_id="X-999")
    with pytest.raises(ConnectorError, match=r"^CMMS create work order failed: HTTP 422$"):
        await cmms.create_work_order(new)


@pytest.mark.asyncio
async def test_maintenance_plans(cmms: GenericCmmsConnector) -> None:
    plans = await cmms.read_maintenance_plans()
    assert [p.id for p in plans] == ["MP-P201-Q"]
    assert plans[0].interval.days == 90


@pytest.mark.asyncio
async def test_spare_parts(cmms: GenericCmmsConnector) -> None:
    every_part = await cmms.read_spare_parts()
    assert len(every_part) == 3
    pump_parts = await cmms.read_spare_parts(asset_id="P-201")
    assert {p.sku for p in pump_parts} == {"SKF-6310", "SEAL-CR32-KIT"}
    [fan] = await cmms.read_spare_parts(sku="ABB-FAN-160")
    assert fan.compatible_assets == ["M-301"]
    assert await cmms.read_spare_parts(asset_id="V-101") == []


@pytest.mark.asyncio
async def test_maintenance_history(cmms: GenericCmmsConnector) -> None:
    history = await cmms.read_maintenance_history("P-201")
    assert [wo.id for wo in history] == ["WO-2025-117"]
    assert history[0].status == WorkOrderStatus.CLOSED
    assert [wo.id for wo in await cmms.read_maintenance_history("M-301")] == ["WO-2025-142"]
    assert await cmms.read_maintenance_history("V-101") == []


@pytest.mark.asyncio
async def test_history_of_an_unknown_asset_is_an_error_not_an_empty_history(
    cmms: GenericCmmsConnector,
) -> None:
    with pytest.raises(ConnectorError, match=r"^CMMS read maintenance history failed: HTTP 404$"):
        await cmms.read_maintenance_history("X-999")
