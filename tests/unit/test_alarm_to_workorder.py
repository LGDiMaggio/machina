"""Integration tests for the alarm_to_workorder built-in workflow.

Runs the 7-step pipeline through the WorkflowEngine with mocked services,
verifying end-to-end execution, template resolution, and error policy
behaviour.
"""

from __future__ import annotations

from typing import Any, ClassVar

import pytest

from machina.connectors.base import ConnectorRegistry
from machina.observability.tracing import ActionTracer
from machina.workflows.builtins.alarm_to_workorder import alarm_to_workorder
from machina.workflows.engine import WorkflowEngine
from machina.workflows.models import ErrorPolicy

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeDiagnoseService:
    """Stub for failure_analyzer domain service."""

    def __init__(self, result: dict[str, Any] | None = None) -> None:
        self._result = result or {"failure_mode": "bearing_wear", "confidence": "high"}
        self.call_count = 0

    def diagnose(self, **kwargs: Any) -> dict[str, Any]:
        self.call_count += 1
        return self._result


class _FakeWoFactory:
    """Stub for work_order_factory domain service."""

    def __init__(self) -> None:
        self.call_count = 0

    def create(self, **kwargs: Any) -> dict[str, Any]:
        self.call_count += 1
        return {"id": "WO-001", "status": "draft", "asset_id": "P-201"}


class _FakeCmmsConnector:
    """CMMS connector providing history, spare parts, and work order creation."""

    capabilities: ClassVar[list[str]] = [
        "get_asset_history",
        "check_spare_parts",
        "create_work_order",
    ]

    def __init__(self) -> None:
        self.created_work_orders: list[dict[str, Any]] = []

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass

    async def health_check(self) -> bool:
        return True

    async def get_asset_history(self, **kwargs: Any) -> list[dict[str, Any]]:
        return [{"id": "WO-PREV-001", "status": "closed", "type": "corrective"}]

    async def check_spare_parts(self, **kwargs: Any) -> list[dict[str, Any]]:
        return [{"sku": "SKF-6310", "available": True, "stock": 3}]

    async def create_work_order(self, **kwargs: Any) -> dict[str, Any]:
        wo = {"id": "WO-SUBMIT-001", "status": "submitted"}
        self.created_work_orders.append(wo)
        return wo


class _FakeCommsConnector:
    """Communication connector for notifications."""

    capabilities: ClassVar[list[str]] = ["send_message", "wait_for_reply"]

    def __init__(self) -> None:
        self.messages_sent: list[str] = []

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass

    async def health_check(self) -> bool:
        return True

    async def send_message(self, message: str, **kwargs: Any) -> None:
        self.messages_sent.append(message)

    async def wait_for_reply(self, **kwargs: Any) -> str:
        return "confirmed"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestAlarmToWorkorderWorkflow:
    """Integration tests for the alarm_to_workorder built-in workflow."""

    def _build_engine(
        self,
        *,
        sandbox: bool = False,
        cmms: _FakeCmmsConnector | None = None,
        comms: _FakeCommsConnector | None = None,
        diagnose: _FakeDiagnoseService | None = None,
        wo_factory: _FakeWoFactory | None = None,
    ) -> tuple[WorkflowEngine, _FakeCmmsConnector, _FakeCommsConnector]:
        cmms = cmms or _FakeCmmsConnector()
        comms = comms or _FakeCommsConnector()
        diagnose = diagnose or _FakeDiagnoseService()
        wo_factory = wo_factory or _FakeWoFactory()

        registry = ConnectorRegistry()
        registry.register("cmms", cmms)
        registry.register("comms", comms)

        engine = WorkflowEngine(
            registry=registry,
            tracer=ActionTracer(),
            services={
                "failure_analyzer": diagnose,
                "work_order_factory": wo_factory,
            },
            sandbox=sandbox,
        )
        return engine, cmms, comms

    @pytest.mark.asyncio
    async def test_full_execution(self) -> None:
        """All 7 steps execute successfully end-to-end."""
        engine, _cmms, _comms = self._build_engine()
        trigger = {"asset_id": "P-201", "severity": "critical", "parameter": "vibration"}

        result = await engine.execute(alarm_to_workorder, trigger)

        assert result.success is True
        assert result.workflow_name == "Alarm to Work Order"
        assert len(result.step_results) == 7

        # All steps completed
        step_names = [sr.step_name for sr in result.step_results]
        assert step_names == [
            "analyze_alarm",
            "check_history",
            "check_spare_parts",
            "generate_work_order",
            "notify_technician",
            "await_confirmation",
            "submit_work_order",
        ]

        # All steps succeeded
        for sr in result.step_results:
            assert sr.success is True, f"Step {sr.step_name} failed: {sr.error}"

    @pytest.mark.asyncio
    async def test_notification_template_resolved(self) -> None:
        """The notify_technician step resolves template variables."""
        engine, _cmms, comms = self._build_engine()
        trigger = {"asset_id": "P-201", "severity": "warning"}

        await engine.execute(alarm_to_workorder, trigger)

        assert len(comms.messages_sent) == 1
        msg = comms.messages_sent[0]
        # Template should have resolved {trigger.asset_id}
        assert "P-201" in msg
        # Template should have resolved {analyze_alarm} (diagnosis output)
        assert "bearing_wear" in msg

    @pytest.mark.asyncio
    async def test_diagnosis_failure_stops_workflow(self) -> None:
        """If analyze_alarm fails (ErrorPolicy.STOP), workflow halts."""

        class _FailingDiagnose:
            def diagnose(self, **kwargs: Any) -> None:
                raise RuntimeError("Diagnosis service unavailable")

        engine, _cmms, _comms = self._build_engine(
            diagnose=_FakeDiagnoseService()  # Will be replaced
        )
        # Replace with a failing service
        engine._services["failure_analyzer"] = _FailingDiagnose()

        trigger = {"asset_id": "P-201", "severity": "critical"}
        result = await engine.execute(alarm_to_workorder, trigger)

        assert result.success is False
        # Should stop after first step
        assert len(result.step_results) == 1
        assert result.step_results[0].step_name == "analyze_alarm"
        assert result.step_results[0].success is False

    @pytest.mark.asyncio
    async def test_spare_parts_failure_skipped(self) -> None:
        """If check_spare_parts fails (ErrorPolicy.SKIP), workflow continues."""

        class _FailingCmms(_FakeCmmsConnector):
            async def check_spare_parts(self, **kwargs: Any) -> None:
                raise RuntimeError("CMMS timeout")

        engine, _cmms, _comms = self._build_engine(cmms=_FailingCmms())
        trigger = {"asset_id": "P-201", "severity": "critical"}

        result = await engine.execute(alarm_to_workorder, trigger)

        # Workflow should still succeed overall
        assert result.success is True
        # check_spare_parts should be skipped
        spare_step = next(sr for sr in result.step_results if sr.step_name == "check_spare_parts")
        assert spare_step.skipped is True

    @pytest.mark.asyncio
    async def test_sandbox_mode_blocks_writes(self) -> None:
        """In sandbox mode, write actions (create_work_order) are logged not executed."""
        engine, cmms, comms = self._build_engine(sandbox=True)
        trigger = {"asset_id": "P-201", "severity": "warning"}

        await engine.execute(alarm_to_workorder, trigger)

        # Work order should NOT have been submitted
        assert len(cmms.created_work_orders) == 0
        # Notifications should also be sandboxed
        assert len(comms.messages_sent) == 0

    @pytest.mark.asyncio
    async def test_trigger_matches_severity_filter(self) -> None:
        """The trigger filter allows only warning and critical severities."""
        trigger = alarm_to_workorder.trigger
        assert trigger.matches({"severity": "critical"})
        assert trigger.matches({"severity": "warning"})
        assert not trigger.matches({"severity": "info"})

    @pytest.mark.asyncio
    async def test_step_count_and_names(self) -> None:
        """Verify the workflow structure is correct."""
        assert len(alarm_to_workorder.steps) == 7
        assert alarm_to_workorder.step_names == [
            "analyze_alarm",
            "check_history",
            "check_spare_parts",
            "generate_work_order",
            "notify_technician",
            "await_confirmation",
            "submit_work_order",
        ]

    @pytest.mark.asyncio
    async def test_error_policies_correct(self) -> None:
        """Verify error policies match the expected safety profile."""
        policies = {s.name: s.on_error for s in alarm_to_workorder.steps}
        # Critical steps must STOP
        assert policies["analyze_alarm"] == ErrorPolicy.STOP
        assert policies["generate_work_order"] == ErrorPolicy.STOP
        assert policies["submit_work_order"] == ErrorPolicy.STOP
        # Non-critical steps are SKIP or NOTIFY
        assert policies["check_history"] == ErrorPolicy.SKIP
        assert policies["check_spare_parts"] == ErrorPolicy.SKIP
        assert policies["await_confirmation"] == ErrorPolicy.SKIP
        assert policies["notify_technician"] == ErrorPolicy.NOTIFY
