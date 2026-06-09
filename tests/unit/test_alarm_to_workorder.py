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
    """Stub for failure_analyzer domain service.

    Returns a ``DiagnosisResult`` so the ``{analyze_alarm.primary_code}``
    template in the built-in workflow resolves to the same plain ``str``
    shape it would receive in production from
    :class:`machina.domain.services.failure_analyzer.FailureAnalyzer`.
    """

    def __init__(self, result: Any | None = None) -> None:
        from machina.domain.services.failure_analyzer import DiagnosisResult

        if result is None:
            result = DiagnosisResult(
                matches=[
                    {
                        "code": "BEAR-WEAR-01",
                        "name": "bearing_wear",
                        "confidence": "high",
                    }
                ]
            )
        self._result = result
        self.call_count = 0

    def diagnose(self, **kwargs: Any) -> Any:
        self.call_count += 1
        return self._result


class _FakeWoFactory:
    """Stub for work_order_factory domain service.

    Records the kwargs from the most recent ``create`` call so tests
    can assert that the workflow actually wired ``Step.inputs`` through
    instead of calling with an empty payload.
    """

    def __init__(self) -> None:
        self.call_count = 0
        self.last_kwargs: dict[str, Any] = {}

    def create(self, **kwargs: Any) -> dict[str, Any]:
        self.call_count += 1
        self.last_kwargs = kwargs
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
        self.last_create_kwargs: dict[str, Any] = {}

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
        # Record kwargs so tests can assert the workflow wired the
        # factory output through, not an empty payload.
        self.last_create_kwargs = kwargs
        wo = {"id": "WO-SUBMIT-001", "status": "submitted"}
        self.created_work_orders.append(wo)
        return wo


class _FakeCommsConnector:
    """Communication connector for notifications."""

    capabilities: ClassVar[list[str]] = ["send_message"]

    def __init__(self) -> None:
        self.messages_sent: list[str] = []

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass

    async def health_check(self) -> bool:
        return True

    async def send_message(self, channel: str, message: str, **kwargs: Any) -> None:
        self.messages_sent.append(message)


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
        """All 6 steps execute successfully end-to-end."""
        engine, _cmms, _comms = self._build_engine()
        trigger = {"asset_id": "P-201", "severity": "critical", "parameter": "vibration"}

        result = await engine.execute(alarm_to_workorder, trigger)

        assert result.success is True
        assert result.workflow_name == "Alarm to Work Order"
        assert len(result.step_results) == 6

        # All steps completed
        step_names = [sr.step_name for sr in result.step_results]
        assert step_names == [
            "analyze_alarm",
            "check_history",
            "check_spare_parts",
            "generate_work_order",
            "notify_technician",
            "submit_work_order",
        ]

        # All steps succeeded
        for sr in result.step_results:
            assert sr.success is True, f"Step {sr.step_name} failed: {sr.error}"

    @pytest.mark.asyncio
    async def test_generate_work_order_receives_upstream_inputs(self) -> None:
        """generate_work_order must consume trigger and diagnosis, not fire blanks.

        Regression for the empty-inputs defect (report Luigi): the step
        previously had no ``inputs={...}`` declaration and the factory was
        called with no kwargs.  The workflow now extracts
        ``{analyze_alarm.primary_code}`` so ``failure_mode`` is a ``str``
        compatible with the production ``WorkOrder.failure_mode`` field.
        """
        from machina.domain.services.failure_analyzer import DiagnosisResult

        diagnose = _FakeDiagnoseService(
            result=DiagnosisResult(
                matches=[
                    {
                        "code": "BEAR-WEAR-01",
                        "name": "Bearing Wear",
                        "confidence": "high",
                    },
                    {
                        "code": "SEAL-LEAK-01",
                        "name": "Seal Leakage",
                        "confidence": "medium",
                    },
                ]
            ),
        )
        wo_factory = _FakeWoFactory()
        engine, _cmms, _comms = self._build_engine(diagnose=diagnose, wo_factory=wo_factory)
        trigger = {
            "asset_id": "P-201",
            "alarm_id": "ALM-2026-0412-001",
            "severity": "warning",
        }

        await engine.execute(alarm_to_workorder, trigger)

        assert wo_factory.call_count == 1
        assert wo_factory.last_kwargs, "factory called with empty kwargs — inputs not wired"
        assert wo_factory.last_kwargs.get("asset_id") == "P-201"
        # failure_mode resolves to .primary_code — the top-ranked code as str.
        assert wo_factory.last_kwargs.get("failure_mode") == "BEAR-WEAR-01"
        # description is text-with-placeholders, so it interpolates to a string;
        # the full DiagnosisResult str() is appended for context.
        description = wo_factory.last_kwargs.get("description", "")
        assert "ALM-2026-0412-001" in description
        assert "P-201" in description
        assert "BEAR-WEAR-01" in description

    @pytest.mark.asyncio
    async def test_submit_work_order_receives_factory_output(self) -> None:
        """submit_work_order must pass the WorkOrder produced by generate_work_order.

        Regression for the empty-inputs defect: the step previously had
        no ``inputs={...}`` declaration, so cmms.create_work_order was
        called with no kwargs in live mode (and inputs={} in sandbox logs).
        """
        wo_factory = _FakeWoFactory()
        engine, cmms, _comms = self._build_engine(wo_factory=wo_factory)
        trigger = {"asset_id": "P-201", "severity": "warning"}

        await engine.execute(alarm_to_workorder, trigger)

        assert cmms.last_create_kwargs, "create_work_order called with empty kwargs"
        # The factory's create() returned a dict; that dict must flow
        # through unchanged (resolve_input_value preserves raw types).
        work_order_arg = cmms.last_create_kwargs.get("work_order")
        assert work_order_arg == {
            "id": "WO-001",
            "status": "draft",
            "asset_id": "P-201",
        }

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
        # Template should have resolved {analyze_alarm} (DiagnosisResult str())
        assert "BEAR-WEAR-01" in msg

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
        assert len(alarm_to_workorder.steps) == 6
        assert alarm_to_workorder.step_names == [
            "analyze_alarm",
            "check_history",
            "check_spare_parts",
            "generate_work_order",
            "notify_technician",
            "submit_work_order",
        ]


class TestAlarmToWorkorderLiveIntegration:
    """End-to-end against the *real* domain services + a real CMMS connector.

    The smoke test in the report-luigi fix branch exposed two type
    contracts the unit-level test fakes hid:

    * ``FailureAnalyzer.diagnose`` returns a ``DiagnosisResult`` of
      ranked failure-mode dicts; the workflow now extracts
      ``{analyze_alarm.primary_code}`` so ``WorkOrder.failure_mode``
      receives a ``str``.
    * ``WorkOrder.id`` is validated non-empty; ``WorkOrderFactory.create``
      auto-generates an id when none is supplied.

    This live integration test fails fast if either contract slips.
    """

    @pytest.mark.asyncio
    async def test_live_run_produces_valid_work_order(self) -> None:
        from machina.domain.failure_mode import FailureMode
        from machina.domain.services.failure_analyzer import FailureAnalyzer
        from machina.domain.services.work_order_factory import WorkOrderFactory
        from machina.domain.work_order import WorkOrder  # noqa: TC001

        # Real FailureAnalyzer with a real FailureMode whose indicators
        # match the trigger parameter.
        bearing_wear = FailureMode(
            code="BEAR-WEAR-01",
            name="Bearing Wear",
            mechanism="fatigue",
            category="mechanical",
            detection_methods=["vibration_analysis"],
            typical_indicators=["vibration_velocity_mm_s"],
            recommended_actions=["replace_bearing"],
            iso_14224_code="VIB",
        )
        analyzer = FailureAnalyzer(failure_modes=[bearing_wear])

        # Real WorkOrderFactory — auto-id, real pydantic validation.
        factory = WorkOrderFactory()

        # Simple CMMS stub that exercises the production
        # ``create_work_order(work_order: WorkOrder)`` signature.
        captured_work_orders: list[WorkOrder] = []

        class _CmmsLike:
            capabilities: ClassVar[list[str]] = [
                "get_asset_history",
                "check_spare_parts",
                "create_work_order",
            ]

            async def connect(self) -> None:
                pass

            async def disconnect(self) -> None:
                pass

            async def health_check(self) -> bool:
                return True

            async def get_asset_history(self, **kwargs: Any) -> list[dict[str, Any]]:
                return []

            async def check_spare_parts(self, **kwargs: Any) -> list[dict[str, Any]]:
                return []

            async def create_work_order(self, work_order: WorkOrder) -> WorkOrder:
                captured_work_orders.append(work_order)
                return work_order

        registry = ConnectorRegistry()
        registry.register("cmms", _CmmsLike())
        registry.register("comms", _FakeCommsConnector())

        engine = WorkflowEngine(
            registry=registry,
            tracer=ActionTracer(),
            services={"failure_analyzer": analyzer, "work_order_factory": factory},
            sandbox=False,  # live!
        )

        result = await engine.execute(
            alarm_to_workorder,
            {
                "asset_id": "P-201",
                "alarm_id": "ALM-LIVE-001",
                "parameter": "vibration_velocity_mm_s",
                "value": 7.8,
                "severity": "warning",
            },
        )

        assert result.success, (
            "Live-mode workflow failed — check that DiagnosisResult.primary_code "
            "flows into WorkOrder.failure_mode as a str, and that the factory "
            "auto-generates a non-empty WorkOrder.id."
        )
        assert len(captured_work_orders) == 1
        wo = captured_work_orders[0]
        # Real pydantic validation accepted these — that IS the contract.
        assert wo.asset_id == "P-201"
        assert wo.failure_mode == "BEAR-WEAR-01"
        assert wo.id.startswith("WO-AUTO-")
        assert wo.id != ""

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
        assert policies["notify_technician"] == ErrorPolicy.NOTIFY


class TestDiagnosisConfidenceGate:
    """U6 — a low-confidence diagnosis is not stamped onto the work order."""

    def _engine(self, diagnose: Any, wo_factory: _FakeWoFactory) -> WorkflowEngine:
        registry = ConnectorRegistry()
        registry.register("cmms", _FakeCmmsConnector())
        registry.register("comms", _FakeCommsConnector())
        return WorkflowEngine(
            registry=registry,
            tracer=ActionTracer(),
            services={"failure_analyzer": diagnose, "work_order_factory": wo_factory},
            sandbox=False,
        )

    @pytest.mark.asyncio
    async def test_low_confidence_diagnosis_not_written(self) -> None:
        from machina.domain.services.failure_analyzer import DiagnosisResult

        diagnose = _FakeDiagnoseService(
            result=DiagnosisResult(
                matches=[{"code": "VIB", "name": "vibration", "confidence": "low"}]
            )
        )
        wo_factory = _FakeWoFactory()
        result = await self._engine(diagnose, wo_factory).execute(
            alarm_to_workorder,
            {"asset_id": "P-201", "alarm_id": "ALM-1", "severity": "warning"},
        )
        assert result.success is True
        # The low-confidence code is NOT recorded as the failure mode.
        assert wo_factory.last_kwargs.get("failure_mode") is None
        # The diagnosis is still visible to the technician (with its confidence).
        assert wo_factory.call_count == 1

    @pytest.mark.asyncio
    async def test_confident_diagnosis_is_written(self) -> None:
        wo_factory = _FakeWoFactory()  # default fake diagnoses BEAR-WEAR-01 / high
        result = await self._engine(_FakeDiagnoseService(), wo_factory).execute(
            alarm_to_workorder,
            {"asset_id": "P-201", "alarm_id": "ALM-2", "severity": "warning"},
        )
        assert result.success is True
        assert wo_factory.last_kwargs.get("failure_mode") == "BEAR-WEAR-01"
