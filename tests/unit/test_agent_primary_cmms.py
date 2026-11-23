"""Agent.from_config() sends every CMMS call to the primary CMMS only.

The agent follows the primary-CMMS policy MachinaRuntime follows: an ambiguous
config is refused, CMMS calls reach the primary and no other connector, and a
primary that cannot be built leaves the agent degraded instead of using
another CMMS in its place.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

import pytest
import yaml
from structlog.testing import capture_logs

from machina import runtime as machina_runtime
from machina.agent.entity_resolver import ResolvedEntity
from machina.agent.runtime import Agent, _TurnResolution
from machina.connectors.capabilities import Capability
from machina.domain.asset import Asset, AssetType
from machina.domain.work_order import WorkOrder, WorkOrderType
from machina.exceptions import ConnectorConfigError

if TYPE_CHECKING:
    from pathlib import Path

_SECRET = "s3cr3t-token-value"

# (connector class, method) of every CMMS call a fake connector served.
_CALLS: list[tuple[str, str]] = []


class _ReadOnlyCmms:
    """A CMMS that reads assets and work orders but cannot create work orders."""

    capabilities: ClassVar[frozenset[Capability]] = frozenset(
        {Capability.READ_ASSETS, Capability.READ_WORK_ORDERS}
    )
    asset_id: ClassVar[str] = "P-1"

    def __init__(self, **settings: Any) -> None:
        self.settings = settings

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    async def read_assets(self) -> list[Asset]:
        _CALLS.append((type(self).__name__, "read_assets"))
        return [
            Asset(
                id=self.asset_id,
                name=f"Pump {self.asset_id}",
                type=AssetType.ROTATING_EQUIPMENT,
            )
        ]

    async def read_work_orders(self, **_filters: Any) -> list[WorkOrder]:
        _CALLS.append((type(self).__name__, "read_work_orders"))
        return [
            WorkOrder(
                id=f"WO-{self.asset_id}",
                type=WorkOrderType.CORRECTIVE,
                asset_id=self.asset_id,
            )
        ]


class _FullCmms(_ReadOnlyCmms):
    """A CMMS that also creates work orders."""

    capabilities: ClassVar[frozenset[Capability]] = frozenset(
        {Capability.READ_ASSETS, Capability.READ_WORK_ORDERS, Capability.CREATE_WORK_ORDER}
    )
    asset_id: ClassVar[str] = "S-1"

    async def create_work_order(self, work_order: WorkOrder) -> WorkOrder:
        _CALLS.append((type(self).__name__, "create_work_order"))
        return work_order


class _BrokenCmms(_FullCmms):
    """A CMMS whose constructor fails, with a setting value in the message."""

    def __init__(self, **settings: Any) -> None:
        raise ValueError(f"cannot reach {settings.get('url')}")


@pytest.fixture(autouse=True)
def _fake_types(monkeypatch: pytest.MonkeyPatch) -> None:
    for type_name, cls_name in {
        "fake_read_only": "_ReadOnlyCmms",
        "fake_full": "_FullCmms",
        "fake_broken": "_BrokenCmms",
    }.items():
        monkeypatch.setitem(
            machina_runtime._CONNECTOR_FACTORIES, type_name, f"{__name__}.{cls_name}"
        )
    _CALLS.clear()


def _agent(tmp_path: Path, connectors: dict[str, Any]) -> Agent:
    path = tmp_path / "machina.yaml"
    path.write_text(
        yaml.dump({"sandbox": False, "confirmations": False, "connectors": connectors}),
        encoding="utf-8",
    )
    return Agent.from_config(path)


class TestRefusedConfigs:
    def test_two_cmms_without_a_primary_are_refused(self, tmp_path: Path) -> None:
        with pytest.raises(ConnectorConfigError) as exc_info:
            _agent(
                tmp_path,
                {"cmms": {"type": "fake_full"}, "registry": {"type": "fake_read_only"}},
            )
        message = str(exc_info.value)
        assert "'cmms'" in message
        assert "'registry'" in message

    def test_a_disabled_primary_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(ConnectorConfigError, match="'old'"):
            _agent(
                tmp_path,
                {
                    "old": {"type": "fake_full", "enabled": False, "primary": True},
                    "new": {"type": "fake_read_only"},
                },
            )


# The primary is listed second and cannot create work orders; the CMMS listed
# first can.
_PRIMARY_LISTED_SECOND = {
    "backup": {"type": "fake_full"},
    "main": {"type": "fake_read_only", "primary": True},
}


class TestHealthyPrimary:
    async def test_create_work_order_never_reaches_another_cmms(self, tmp_path: Path) -> None:
        agent = _agent(tmp_path, _PRIMARY_LISTED_SECOND)
        await agent.start()
        agent._turn_resolution["default"] = _TurnResolution.of(
            [
                ResolvedEntity(
                    asset=agent.plant.assets["P-1"], confidence=1.0, match_reason="exact_id"
                )
            ]
        )

        result = await agent._execute_tool(
            "create_work_order",
            {"asset_id": "P-1", "type": "corrective", "priority": "medium", "description": "Leak"},
        )

        assert "No CMMS connector" in result.get("error", "")
        assert ("_FullCmms", "create_work_order") not in _CALLS
        # Workflows look connectors up through the same registry.
        assert agent._engine._registry.find_by_capability(Capability.CREATE_WORK_ORDER) == []

    async def test_read_work_orders_reads_the_primary_listed_second(self, tmp_path: Path) -> None:
        agent = _agent(tmp_path, _PRIMARY_LISTED_SECOND)

        result = await agent._execute_tool("read_work_orders", {})

        assert [wo["id"] for wo in result] == ["WO-P-1"]
        assert _CALLS == [("_ReadOnlyCmms", "read_work_orders")]

    async def test_assets_load_from_the_primary_only(self, tmp_path: Path) -> None:
        agent = _agent(tmp_path, _PRIMARY_LISTED_SECOND)

        await agent.start()

        assert set(agent.plant.assets) == {"P-1"}
        assert _CALLS == [("_ReadOnlyCmms", "read_assets")]


_DEGRADED_PRIMARY = {
    "sap": {"type": "fake_broken", "primary": True, "settings": {"url": _SECRET}},
    "registry": {"type": "fake_full"},
}


class TestDegradedPrimary:
    async def test_a_primary_that_fails_to_build_starts_degraded(self, tmp_path: Path) -> None:
        with capture_logs() as logs:
            agent = _agent(tmp_path, _DEGRADED_PRIMARY)
            await agent.start()

        assert agent.plant.assets == {}
        assert _CALLS == []
        degraded = [entry for entry in logs if entry["event"] == "primary_cmms_degraded"]
        assert len(degraded) == 1
        assert degraded[0]["connector"] == "sap"
        assert degraded[0]["error_type"] == "ValueError"
        assert _SECRET not in repr(logs)

    @pytest.mark.parametrize(
        ("tool", "args"),
        [
            ("list_assets", {}),
            ("search_assets", {"query": "Pump S-1"}),
            ("get_asset_details", {"asset_id": "S-1"}),
            ("read_work_orders", {}),
            ("diagnose_failure", {"asset_id": "S-1", "symptoms": ["vibration"]}),
        ],
    )
    async def test_cmms_tools_report_the_degraded_primary(
        self, tmp_path: Path, tool: str, args: dict[str, Any]
    ) -> None:
        agent = _agent(tmp_path, _DEGRADED_PRIMARY)
        await agent.start()

        result = await agent._execute_tool(tool, args)

        assert list(result) == ["error"]
        message = result["error"]
        assert "'sap'" in message
        assert "degraded" in message
        assert _SECRET not in message
        assert _CALLS == []


class TestNonPrimaryBuildFailure:
    def test_a_non_primary_that_fails_to_build_still_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="cannot reach"):
            _agent(
                tmp_path,
                {
                    "main": {"type": "fake_full", "primary": True},
                    "spare": {"type": "fake_broken", "settings": {"url": "http://cmms.invalid"}},
                },
            )
