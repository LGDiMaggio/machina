"""Tests for the WorkflowEngine execution logic."""

from __future__ import annotations

import asyncio
from typing import Any, ClassVar

import pytest

from machina.connectors.base import ConnectorRegistry
from machina.observability.tracing import ActionTracer
from machina.workflows.engine import WorkflowEngine
from machina.workflows.models import (
    ErrorPolicy,
    GuardCondition,
    Step,
    Workflow,
    WorkflowContext,
)

# ---------------------------------------------------------------------------
# Fakes / stubs
# ---------------------------------------------------------------------------


class _FakeLLM:
    """Minimal LLM stub for workflow reasoning steps."""

    def __init__(self, response: str = "LLM response") -> None:
        self.model = "fake:model"
        self._response = response
        self.call_count = 0

    async def complete(
        self,
        messages: list[dict[str, str]],
        **kwargs: Any,
    ) -> str:
        self.call_count += 1
        return self._response


class _FakeDiagnoseService:
    """Stub for failure_analyzer domain service."""

    def diagnose(self, **kwargs: Any) -> dict[str, Any]:
        return {"failure_mode": "bearing_wear", "confidence": "high"}


class _FakeWoFactory:
    """Stub for work_order_factory domain service."""

    def create(self, **kwargs: Any) -> dict[str, Any]:
        return {"id": "WO-001", "status": "created"}


class _AsyncService:
    """An async domain service."""

    async def run(self, **kwargs: Any) -> str:
        return "async_result"


class _FakeCommsConnector:
    """Communication connector stub."""

    capabilities: ClassVar[list[str]] = ["send_message"]

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass

    async def health_check(self) -> bool:
        return True

    async def send_message(self, message: str, **kwargs: Any) -> None:
        self.last_message = message


class _FakeReadConnector:
    """Connector with read capabilities."""

    capabilities: ClassVar[list[str]] = ["read_work_orders", "check_spare_parts"]

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass

    async def health_check(self) -> bool:
        return True

    async def read_work_orders(self, **kwargs: Any) -> list[dict[str, Any]]:
        return [{"id": "WO-001", "status": "open"}]

    async def check_spare_parts(self, **kwargs: Any) -> list[dict[str, Any]]:
        return [{"sku": "SKF-6310", "available": True}]


class _FakeErrorConnector:
    """Connector that always raises."""

    capabilities: ClassVar[list[str]] = ["flaky_operation"]

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass

    async def health_check(self) -> bool:
        return True

    async def flaky_operation(self, **kwargs: Any) -> None:
        raise RuntimeError("Connection reset")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tracer() -> ActionTracer:
    return ActionTracer()


@pytest.fixture
def registry() -> ConnectorRegistry:
    return ConnectorRegistry()


@pytest.fixture
def engine(registry: ConnectorRegistry, tracer: ActionTracer) -> WorkflowEngine:
    return WorkflowEngine(registry=registry, tracer=tracer)


# ---------------------------------------------------------------------------
# Tests — basic execution
# ---------------------------------------------------------------------------


class TestBasicExecution:
    """Test basic workflow execution paths."""

    @pytest.mark.asyncio
    async def test_empty_workflow(self, engine: WorkflowEngine) -> None:
        wf = Workflow(name="Empty")
        result = await engine.execute(wf)
        assert result.success is True
        assert result.step_results == []
        assert result.workflow_name == "Empty"

    @pytest.mark.asyncio
    async def test_single_step_no_action(self, engine: WorkflowEngine) -> None:
        wf = Workflow(name="NoOp", steps=[Step("noop")])
        result = await engine.execute(wf)
        assert result.success is True
        assert len(result.step_results) == 1
        assert result.step_results[0].output is None

    @pytest.mark.asyncio
    async def test_service_steps(self, tracer: ActionTracer) -> None:
        engine = WorkflowEngine(
            tracer=tracer,
            services={
                "failure_analyzer": _FakeDiagnoseService(),
                "work_order_factory": _FakeWoFactory(),
            },
        )
        wf = Workflow(
            name="Diagnose+WO",
            steps=[
                Step("diagnose", action="failure_analyzer.diagnose"),
                Step("create_wo", action="work_order_factory.create"),
            ],
        )
        result = await engine.execute(wf)
        assert result.success is True
        assert len(result.step_results) == 2
        assert result.step_results[0].output["confidence"] == "high"
        assert result.step_results[1].output["id"] == "WO-001"

    @pytest.mark.asyncio
    async def test_async_service(self, tracer: ActionTracer) -> None:
        engine = WorkflowEngine(
            tracer=tracer,
            services={"async_svc": _AsyncService()},
        )
        wf = Workflow(
            name="AsyncTest",
            steps=[Step("step1", action="async_svc.run")],
        )
        result = await engine.execute(wf)
        assert result.success is True
        assert result.step_results[0].output == "async_result"

    @pytest.mark.asyncio
    async def test_duration_is_recorded(self, engine: WorkflowEngine) -> None:
        wf = Workflow(name="Timer", steps=[Step("noop")])
        result = await engine.execute(wf)
        assert result.duration_ms >= 0
        assert result.step_results[0].duration_ms >= 0


# ---------------------------------------------------------------------------
# Tests — LLM reasoning steps
# ---------------------------------------------------------------------------


class TestLLMSteps:
    """Test agent.reason action dispatch."""

    @pytest.mark.asyncio
    async def test_llm_step(self, tracer: ActionTracer) -> None:
        llm = _FakeLLM("Root cause: bearing wear")
        engine = WorkflowEngine(tracer=tracer, llm=llm)
        wf = Workflow(
            name="LLMTest",
            steps=[
                Step("reason", action="agent.reason", prompt="Diagnose this alarm"),
            ],
        )
        result = await engine.execute(wf)
        assert result.success is True
        assert "bearing wear" in result.step_results[0].output
        assert llm.call_count == 1

    @pytest.mark.asyncio
    async def test_llm_step_no_llm_raises(self, tracer: ActionTracer) -> None:
        engine = WorkflowEngine(tracer=tracer, llm=None)
        wf = Workflow(
            name="NoLLM",
            steps=[Step("reason", action="agent.reason", prompt="test")],
        )
        result = await engine.execute(wf)
        assert result.success is False
        assert "LLM" in (result.step_results[0].error or "")

    @pytest.mark.asyncio
    async def test_llm_prompt_resolution(self, tracer: ActionTracer) -> None:
        llm = _FakeLLM("resolved answer")
        engine = WorkflowEngine(tracer=tracer, llm=llm)
        wf = Workflow(
            name="PromptResolve",
            steps=[
                Step("s1", action=""),  # no-op, output=None
                Step(
                    "s2",
                    action="agent.reason",
                    prompt="Based on {s1}: what happened?",
                ),
            ],
        )
        result = await engine.execute(wf)
        assert result.success is True


# ---------------------------------------------------------------------------
# Tests — connector dispatch
# ---------------------------------------------------------------------------


class TestConnectorDispatch:
    """Test connector action dispatch."""

    @pytest.mark.asyncio
    async def test_connector_step(self, tracer: ActionTracer) -> None:
        registry = ConnectorRegistry()
        registry.register("cmms", _FakeReadConnector())
        engine = WorkflowEngine(registry=registry, tracer=tracer)
        wf = Workflow(
            name="ConnTest",
            steps=[Step("read_wos", action="cmms.read_work_orders")],
        )
        result = await engine.execute(wf)
        assert result.success is True
        assert result.step_results[0].output[0]["id"] == "WO-001"

    @pytest.mark.asyncio
    async def test_missing_connector(self, engine: WorkflowEngine) -> None:
        wf = Workflow(
            name="MissConn",
            steps=[Step("step", action="cmms.read_work_orders")],
        )
        result = await engine.execute(wf)
        assert result.success is False
        assert "no connector" in (result.step_results[0].error or "").lower()


# ---------------------------------------------------------------------------
# Tests — notification dispatch
# ---------------------------------------------------------------------------


class TestNotificationDispatch:
    """Test channels.send_message action."""

    @pytest.mark.asyncio
    async def test_send_notification(self, tracer: ActionTracer) -> None:
        comms = _FakeCommsConnector()
        registry = ConnectorRegistry()
        registry.register("comms", comms)
        engine = WorkflowEngine(registry=registry, tracer=tracer)
        wf = Workflow(
            name="NotifyTest",
            steps=[
                Step(
                    "notify",
                    action="channels.send_message",
                    template="Alert: {trigger.asset_id}",
                ),
            ],
        )
        result = await engine.execute(wf, {"asset_id": "P-201"})
        assert result.success is True
        assert result.step_results[0].output["sent"] is True
        assert "P-201" in comms.last_message

    @pytest.mark.asyncio
    async def test_notification_no_connector(self, engine: WorkflowEngine) -> None:
        wf = Workflow(
            name="NotifyMiss",
            steps=[
                Step("notify", action="channels.send_message", template="test"),
            ],
        )
        result = await engine.execute(wf)
        assert result.success is True  # returns result, not crash
        assert result.step_results[0].output["sent"] is False


# ---------------------------------------------------------------------------
# Tests — template variable resolution
# ---------------------------------------------------------------------------


class TestTemplateResolution:
    """Test variable interpolation across steps."""

    @pytest.mark.asyncio
    async def test_step_output_in_prompt(self, tracer: ActionTracer) -> None:
        llm = _FakeLLM("final answer")
        engine = WorkflowEngine(
            tracer=tracer,
            llm=llm,
            services={"failure_analyzer": _FakeDiagnoseService()},
        )
        wf = Workflow(
            name="ChainTest",
            steps=[
                Step("diagnose", action="failure_analyzer.diagnose"),
                Step(
                    "reason",
                    action="agent.reason",
                    prompt="Based on {diagnose}: explain",
                ),
            ],
        )
        result = await engine.execute(wf)
        assert result.success is True
        assert len(result.step_results) == 2

    @pytest.mark.asyncio
    async def test_trigger_in_inputs(self, tracer: ActionTracer) -> None:
        ctx = WorkflowContext({"asset_id": "P-201"})
        resolved = ctx.resolve("{trigger.asset_id}")
        assert resolved == "P-201"


# ---------------------------------------------------------------------------
# Tests — error policies
# ---------------------------------------------------------------------------


class TestErrorPolicies:
    """Test on_error behaviour: stop, skip, retry, notify."""

    @pytest.mark.asyncio
    async def test_stop_on_error(self, tracer: ActionTracer) -> None:
        """ErrorPolicy.STOP halts the workflow at the failed step."""
        registry = ConnectorRegistry()
        registry.register("flaky", _FakeErrorConnector())
        engine = WorkflowEngine(registry=registry, tracer=tracer)
        wf = Workflow(
            name="StopTest",
            steps=[
                Step("fail", action="flaky.flaky_operation", on_error=ErrorPolicy.STOP),
                Step("after", action=""),  # should NOT execute
            ],
        )
        result = await engine.execute(wf)
        assert result.success is False
        assert len(result.step_results) == 1
        assert result.step_results[0].success is False

    @pytest.mark.asyncio
    async def test_skip_on_error(self, tracer: ActionTracer) -> None:
        """ErrorPolicy.SKIP marks the step as skipped and continues."""
        registry = ConnectorRegistry()
        registry.register("flaky", _FakeErrorConnector())
        engine = WorkflowEngine(registry=registry, tracer=tracer)
        wf = Workflow(
            name="SkipTest",
            steps=[
                Step("fail", action="flaky.flaky_operation", on_error=ErrorPolicy.SKIP),
                Step("after", action=""),
            ],
        )
        result = await engine.execute(wf)
        assert result.success is True  # skip doesn't fail workflow
        assert len(result.step_results) == 2
        assert result.step_results[0].skipped is True

    @pytest.mark.asyncio
    async def test_notify_on_error(self, tracer: ActionTracer) -> None:
        """ErrorPolicy.NOTIFY records the failure but continues."""
        registry = ConnectorRegistry()
        registry.register("flaky", _FakeErrorConnector())
        engine = WorkflowEngine(registry=registry, tracer=tracer)
        wf = Workflow(
            name="NotifyTest",
            steps=[
                Step("fail", action="flaky.flaky_operation", on_error=ErrorPolicy.NOTIFY),
                Step("after", action=""),
            ],
        )
        result = await engine.execute(wf)
        assert result.success is False  # notify doesn't mark success
        assert len(result.step_results) == 2  # continues to next step
        assert result.step_results[0].success is False
        assert result.step_results[1].success is True

    @pytest.mark.asyncio
    async def test_retry_on_error(self, tracer: ActionTracer) -> None:
        """ErrorPolicy.RETRY retries up to `retries` times before failing."""
        call_count = 0

        class _FlakeThenOk:
            capabilities: ClassVar[list[str]] = ["flaky_then_ok"]

            async def connect(self) -> None:
                pass

            async def disconnect(self) -> None:
                pass

            async def health_check(self) -> bool:
                return True

            async def flaky_then_ok(self, **kwargs: Any) -> str:
                nonlocal call_count
                call_count += 1
                if call_count < 3:
                    raise RuntimeError("Transient error")
                return "success"

        registry = ConnectorRegistry()
        registry.register("svc", _FlakeThenOk())
        engine = WorkflowEngine(registry=registry, tracer=tracer)
        wf = Workflow(
            name="RetryTest",
            steps=[
                Step(
                    "flaky",
                    action="svc.flaky_then_ok",
                    on_error=ErrorPolicy.RETRY,
                    retries=3,
                ),
            ],
        )
        result = await engine.execute(wf)
        assert result.success is True
        assert call_count == 3  # failed twice, succeeded on third

    @pytest.mark.asyncio
    async def test_retry_exhausted(self, tracer: ActionTracer) -> None:
        """All retry attempts fail → step fails."""
        registry = ConnectorRegistry()
        registry.register("flaky", _FakeErrorConnector())
        engine = WorkflowEngine(registry=registry, tracer=tracer)
        wf = Workflow(
            name="RetryExhaust",
            steps=[
                Step(
                    "fail",
                    action="flaky.flaky_operation",
                    on_error=ErrorPolicy.RETRY,
                    retries=2,
                ),
            ],
        )
        result = await engine.execute(wf)
        assert result.success is False


# ---------------------------------------------------------------------------
# Tests — guard conditions
# ---------------------------------------------------------------------------


class TestGuardConditions:
    """Test step guard conditions."""

    @pytest.mark.asyncio
    async def test_guard_allows(self, tracer: ActionTracer) -> None:
        engine = WorkflowEngine(
            tracer=tracer,
            services={"failure_analyzer": _FakeDiagnoseService()},
        )
        wf = Workflow(
            name="GuardAllow",
            steps=[
                Step(
                    "diagnose",
                    action="failure_analyzer.diagnose",
                    guard=GuardCondition(check=lambda _: True),
                ),
            ],
        )
        result = await engine.execute(wf)
        assert result.success is True
        assert result.step_results[0].skipped is False

    @pytest.mark.asyncio
    async def test_guard_blocks(self, tracer: ActionTracer) -> None:
        engine = WorkflowEngine(
            tracer=tracer,
            services={"failure_analyzer": _FakeDiagnoseService()},
        )
        wf = Workflow(
            name="GuardBlock",
            steps=[
                Step(
                    "diagnose",
                    action="failure_analyzer.diagnose",
                    guard=GuardCondition(
                        check=lambda _: False,
                        description="Always skip",
                    ),
                ),
            ],
        )
        result = await engine.execute(wf)
        assert result.success is True
        assert result.step_results[0].skipped is True

    @pytest.mark.asyncio
    async def test_guard_exception_skips(self, tracer: ActionTracer) -> None:
        engine = WorkflowEngine(tracer=tracer)

        def _bad_guard(_ctx: dict[str, Any]) -> bool:
            raise ValueError("bad guard")

        wf = Workflow(
            name="GuardErr",
            steps=[
                Step(
                    "step",
                    guard=GuardCondition(check=_bad_guard),
                ),
            ],
        )
        result = await engine.execute(wf)
        assert result.step_results[0].skipped is True

    @pytest.mark.asyncio
    async def test_guard_uses_context(self, tracer: ActionTracer) -> None:
        """Guard can read prior step outputs from context."""
        engine = WorkflowEngine(
            tracer=tracer,
            services={"failure_analyzer": _FakeDiagnoseService()},
        )
        wf = Workflow(
            name="GuardCtx",
            steps=[
                Step("s1", action="failure_analyzer.diagnose"),
                Step(
                    "s2",
                    guard=GuardCondition(
                        check=lambda ctx: ctx.get("s1", {}).get("confidence") == "high",
                    ),
                ),
            ],
        )
        result = await engine.execute(wf)
        assert len(result.step_results) == 2
        assert result.step_results[1].skipped is False  # guard passes


# ---------------------------------------------------------------------------
# Tests — sandbox mode
# ---------------------------------------------------------------------------


class TestSandboxMode:
    """Test sandbox mode — write actions logged but not executed."""

    @pytest.mark.asyncio
    async def test_sandbox_llm(self, tracer: ActionTracer) -> None:
        llm = _FakeLLM()
        engine = WorkflowEngine(tracer=tracer, llm=llm, sandbox=True)
        wf = Workflow(
            name="SandboxLLM",
            steps=[Step("reason", action="agent.reason", prompt="test")],
        )
        result = await engine.execute(wf)
        assert result.success is True
        assert "[SANDBOX]" in result.step_results[0].output
        assert llm.call_count == 0  # LLM not actually called

    @pytest.mark.asyncio
    async def test_sandbox_notification(self, tracer: ActionTracer) -> None:
        comms = _FakeCommsConnector()
        registry = ConnectorRegistry()
        registry.register("comms", comms)
        engine = WorkflowEngine(
            registry=registry, tracer=tracer, sandbox=True,
        )
        wf = Workflow(
            name="SandboxNotify",
            steps=[
                Step("notify", action="channels.send_message", template="Alert!"),
            ],
        )
        result = await engine.execute(wf)
        assert result.step_results[0].output["sandbox"] is True
        assert result.step_results[0].output["sent"] is False
        assert not hasattr(comms, "last_message")

    @pytest.mark.asyncio
    async def test_sandbox_write_service(self, tracer: ActionTracer) -> None:
        engine = WorkflowEngine(
            tracer=tracer,
            services={"work_order_factory": _FakeWoFactory()},
            sandbox=True,
        )
        wf = Workflow(
            name="SandboxWO",
            steps=[Step("create", action="work_order_factory.create")],
        )
        result = await engine.execute(wf)
        assert result.step_results[0].output["sandbox"] is True

    @pytest.mark.asyncio
    async def test_sandbox_read_still_executes(self, tracer: ActionTracer) -> None:
        """Read actions should execute even in sandbox mode."""
        engine = WorkflowEngine(
            tracer=tracer,
            services={"failure_analyzer": _FakeDiagnoseService()},
            sandbox=True,
        )
        wf = Workflow(
            name="SandboxRead",
            steps=[Step("diagnose", action="failure_analyzer.diagnose")],
        )
        result = await engine.execute(wf)
        assert result.success is True
        # diagnose is a read action — should actually execute
        assert result.step_results[0].output["confidence"] == "high"


# ---------------------------------------------------------------------------
# Tests — timeout
# ---------------------------------------------------------------------------


class TestTimeout:
    """Test step timeout handling."""

    @pytest.mark.asyncio
    async def test_step_timeout(self, tracer: ActionTracer) -> None:
        class _SlowService:
            async def run(self, **kwargs: Any) -> str:
                await asyncio.sleep(10)
                return "done"

        engine = WorkflowEngine(
            tracer=tracer,
            services={"slow": _SlowService()},
        )
        wf = Workflow(
            name="TimeoutTest",
            steps=[
                Step(
                    "slow_step",
                    action="slow.run",
                    timeout_seconds=0.05,
                    on_error=ErrorPolicy.STOP,
                ),
            ],
        )
        result = await engine.execute(wf)
        assert result.success is False
        assert "timed out" in (result.step_results[0].error or "").lower()


# ---------------------------------------------------------------------------
# Tests — tracing
# ---------------------------------------------------------------------------


class TestTracing:
    """Test that workflow steps are traced via ActionTracer."""

    @pytest.mark.asyncio
    async def test_steps_are_traced(self) -> None:
        tracer = ActionTracer()
        engine = WorkflowEngine(
            tracer=tracer,
            services={"failure_analyzer": _FakeDiagnoseService()},
        )
        wf = Workflow(
            name="TraceTest",
            steps=[
                Step("s1", action="failure_analyzer.diagnose"),
                Step("s2", action=""),
            ],
        )
        await engine.execute(wf)
        step_traces = [e for e in tracer.entries if e.action == "workflow_step"]
        assert len(step_traces) == 2
        assert step_traces[0].operation == "s1"
        assert step_traces[1].operation == "s2"


# ---------------------------------------------------------------------------
# Tests — is_write_action helper
# ---------------------------------------------------------------------------


class TestIsWriteAction:
    """Test the _is_write_action classifier."""

    @pytest.mark.parametrize(
        ("action", "expected"),
        [
            ("cmms.create_work_order", True),
            ("channels.send_message", True),
            ("cmms.update_work_order", True),
            ("cmms.delete_work_order", True),
            ("work_order_factory.create", True),
            ("channels.notify_team", True),
            ("failure_analyzer.diagnose", False),
            ("cmms.read_work_orders", False),
            ("docs.search", False),
            ("maintenance_scheduler.find_window", False),
        ],
    )
    def test_classification(self, action: str, expected: bool) -> None:
        assert WorkflowEngine._is_write_action(action) is expected
