"""Tests for the Workflow and Step data models."""

from __future__ import annotations

from machina.workflows import Step, Workflow
from machina.workflows.models import (
    ErrorPolicy,
    GuardCondition,
    StepResult,
    Trigger,
    TriggerType,
    WorkflowContext,
    WorkflowResult,
)


class TestStep:
    """Test Step dataclass."""

    def test_minimal_step(self) -> None:
        step = Step("my_step")
        assert step.name == "my_step"
        assert step.action == ""
        assert step.description == ""
        assert step.prompt == ""
        assert step.template == ""
        assert step.depends_on == []

    def test_step_with_action(self) -> None:
        step = Step("diagnose", action="failure_analyzer.diagnose", description="Run diagnosis")
        assert step.action == "failure_analyzer.diagnose"
        assert step.description == "Run diagnosis"

    def test_step_with_prompt(self) -> None:
        step = Step(
            "llm_step",
            action="agent.reason",
            prompt="Given {diagnose}: summarise the root cause",
        )
        assert "{diagnose}" in step.prompt

    def test_step_with_template(self) -> None:
        step = Step("notify", action="channels.send", template="Alert: {alarm.value}")
        assert step.template == "Alert: {alarm.value}"

    def test_step_depends_on(self) -> None:
        step = Step("action", depends_on=["step_a", "step_b"])
        assert step.depends_on == ["step_a", "step_b"]

    def test_step_depends_on_isolation(self) -> None:
        """Each step gets its own depends_on list (no mutable default sharing)."""
        s1 = Step("a")
        s2 = Step("b")
        s1.depends_on.append("x")
        assert s2.depends_on == []


class TestWorkflow:
    """Test Workflow dataclass."""

    def test_minimal_workflow(self) -> None:
        wf = Workflow(name="Test")
        assert wf.name == "Test"
        assert wf.description == ""
        assert wf.trigger == ""
        assert wf.steps == []

    def test_workflow_with_steps(self) -> None:
        wf = Workflow(
            name="Predictive Maintenance",
            trigger="alarm",
            steps=[
                Step("detect", action="sensors.read"),
                Step("diagnose", action="failure_analyzer.diagnose"),
                Step("create_wo", action="work_order_factory.create"),
            ],
        )
        assert len(wf.steps) == 3
        assert wf.trigger == "alarm"

    def test_step_names_property(self) -> None:
        wf = Workflow(
            name="Test",
            steps=[Step("a"), Step("b"), Step("c")],
        )
        assert wf.step_names == ["a", "b", "c"]

    def test_step_names_empty(self) -> None:
        wf = Workflow(name="Empty")
        assert wf.step_names == []

    def test_get_step_found(self) -> None:
        step_b = Step("b", action="do_b")
        wf = Workflow(name="Test", steps=[Step("a"), step_b, Step("c")])
        result = wf.get_step("b")
        assert result is step_b

    def test_get_step_not_found(self) -> None:
        wf = Workflow(name="Test", steps=[Step("a")])
        assert wf.get_step("nonexistent") is None

    def test_steps_list_isolation(self) -> None:
        """Each workflow gets its own steps list (no mutable default sharing)."""
        w1 = Workflow(name="W1")
        w2 = Workflow(name="W2")
        w1.steps.append(Step("x"))
        assert w2.steps == []

    def test_full_predictive_pipeline(self) -> None:
        """Replicate the predictive_pipeline example structure to ensure it works."""
        wf = Workflow(
            name="Predictive Maintenance Pipeline",
            description="End-to-end predictive maintenance",
            trigger="alarm",
            steps=[
                Step("enrich_alarm", action="sensors.get_related_readings"),
                Step("diagnose_rules", action="failure_analyzer.diagnose"),
                Step("search_manuals", action="docs.search"),
                Step(
                    "diagnose_llm",
                    action="agent.reason",
                    prompt="Root cause analysis based on {diagnose_rules}",
                ),
                Step("check_parts", action="cmms.check_spare_parts"),
                Step("check_history", action="cmms.get_asset_history"),
                Step("draft_wo", action="agent.reason", prompt="Create a work order"),
                Step("submit_wo", action="work_order_factory.create"),
                Step("find_window", action="maintenance_scheduler.find_window"),
                Step("optimize_schedule", action="agent.reason", prompt="Optimize"),
                Step("notify_team", action="channels.send_message", template="Alert!"),
            ],
        )
        assert len(wf.steps) == 11
        assert wf.step_names[0] == "enrich_alarm"
        assert wf.step_names[-1] == "notify_team"
        assert wf.get_step("diagnose_llm") is not None
        assert wf.get_step("diagnose_llm").action == "agent.reason"


# -----------------------------------------------------------------------
# New model tests (v0.2)
# -----------------------------------------------------------------------


class TestTriggerType:
    """Test TriggerType enum."""

    def test_values(self) -> None:
        assert TriggerType.ALARM == "alarm"
        assert TriggerType.SCHEDULE == "schedule"
        assert TriggerType.MANUAL == "manual"
        assert TriggerType.CONDITION == "condition"

    def test_from_string(self) -> None:
        assert TriggerType("alarm") is TriggerType.ALARM


class TestTrigger:
    """Test Trigger dataclass."""

    def test_default(self) -> None:
        t = Trigger()
        assert t.type == TriggerType.MANUAL
        assert t.filter == {}

    def test_with_filter(self) -> None:
        t = Trigger(type=TriggerType.ALARM, filter={"severity": ["critical"]})
        assert t.type == TriggerType.ALARM
        assert "severity" in t.filter

    def test_matches_no_filter(self) -> None:
        t = Trigger()
        assert t.matches({"any": "event"}) is True

    def test_matches_with_matching_filter(self) -> None:
        t = Trigger(
            type=TriggerType.ALARM,
            filter={"severity": ["warning", "critical"]},
        )
        assert t.matches({"severity": "critical"}) is True

    def test_matches_with_non_matching_filter(self) -> None:
        t = Trigger(
            type=TriggerType.ALARM,
            filter={"severity": ["critical"]},
        )
        assert t.matches({"severity": "info"}) is False

    def test_matches_missing_key(self) -> None:
        t = Trigger(filter={"severity": ["critical"]})
        assert t.matches({}) is False

    def test_matches_scalar_filter(self) -> None:
        t = Trigger(filter={"asset_id": "P-201"})
        assert t.matches({"asset_id": "P-201"}) is True
        assert t.matches({"asset_id": "P-999"}) is False


class TestErrorPolicy:
    """Test ErrorPolicy enum."""

    def test_values(self) -> None:
        assert ErrorPolicy.RETRY == "retry"
        assert ErrorPolicy.SKIP == "skip"
        assert ErrorPolicy.STOP == "stop"
        assert ErrorPolicy.NOTIFY == "notify"


class TestGuardCondition:
    """Test GuardCondition dataclass."""

    def test_default_passes(self) -> None:
        g = GuardCondition()
        assert g.check({}) is True

    def test_custom_check(self) -> None:
        g = GuardCondition(
            check=lambda ctx: ctx.get("confidence") == "high",
            description="Require high confidence",
        )
        assert g.check({"confidence": "high"}) is True
        assert g.check({"confidence": "low"}) is False
        assert g.description == "Require high confidence"


class TestStepExtended:
    """Test extended Step fields (v0.2)."""

    def test_default_on_error(self) -> None:
        s = Step("x")
        assert s.on_error == ErrorPolicy.STOP
        assert s.retries == 0
        assert s.guard is None
        assert s.timeout_seconds is None
        assert s.inputs == {}

    def test_with_retry(self) -> None:
        s = Step("x", on_error=ErrorPolicy.RETRY, retries=3)
        assert s.on_error == ErrorPolicy.RETRY
        assert s.retries == 3

    def test_with_guard(self) -> None:
        guard = GuardCondition(check=lambda _: False)
        s = Step("x", guard=guard)
        assert s.guard is guard

    def test_with_inputs(self) -> None:
        s = Step("x", inputs={"asset_id": "{trigger.asset_id}"})
        assert s.inputs["asset_id"] == "{trigger.asset_id}"

    def test_inputs_isolation(self) -> None:
        s1 = Step("a")
        s2 = Step("b")
        s1.inputs["key"] = "val"
        assert s2.inputs == {}


class TestWorkflowWithTrigger:
    """Test Workflow with typed Trigger."""

    def test_trigger_object(self) -> None:
        wf = Workflow(
            name="Test",
            trigger=Trigger(type=TriggerType.ALARM),
        )
        assert isinstance(wf.trigger, Trigger)
        assert wf.trigger.type == TriggerType.ALARM

    def test_trigger_string_backward_compat(self) -> None:
        wf = Workflow(name="Test", trigger="alarm")
        assert wf.trigger == "alarm"


class TestStepResult:
    """Test StepResult dataclass."""

    def test_success(self) -> None:
        r = StepResult(step_name="test", output=42)
        assert r.success is True
        assert r.output == 42
        assert r.error is None
        assert r.skipped is False

    def test_failure(self) -> None:
        r = StepResult(step_name="test", success=False, error="boom")
        assert r.success is False
        assert r.error == "boom"

    def test_skipped(self) -> None:
        r = StepResult(step_name="test", skipped=True)
        assert r.skipped is True


class TestWorkflowResult:
    """Test WorkflowResult dataclass."""

    def test_success(self) -> None:
        r = WorkflowResult(
            workflow_name="Test",
            trigger_event={"asset_id": "P-201"},
            step_results=[StepResult(step_name="s1", output="ok")],
            success=True,
            duration_ms=100.0,
        )
        assert r.success is True
        assert len(r.step_results) == 1
        assert r.workflow_name == "Test"

    def test_defaults(self) -> None:
        r = WorkflowResult(workflow_name="Test")
        assert r.trigger_event == {}
        assert r.step_results == []
        assert r.success is True
        assert r.duration_ms == 0.0


class TestWorkflowContext:
    """Test WorkflowContext template resolution."""

    def test_empty_context(self) -> None:
        ctx = WorkflowContext()
        assert ctx.trigger == {}
        assert ctx.steps == {}

    def test_set_and_get(self) -> None:
        ctx = WorkflowContext({"asset_id": "P-201"})
        ctx.set_step_output("diagnose", {"confidence": "high"})
        assert ctx.get("diagnose") == {"confidence": "high"}
        assert ctx.get("missing") is None

    def test_as_dict(self) -> None:
        ctx = WorkflowContext({"x": 1})
        ctx.set_step_output("s1", "result1")
        d = ctx.as_dict()
        assert d["trigger"] == {"x": 1}
        assert d["s1"] == "result1"

    def test_resolve_trigger_field(self) -> None:
        ctx = WorkflowContext({"asset_id": "P-201"})
        assert ctx.resolve("Asset: {trigger.asset_id}") == "Asset: P-201"

    def test_resolve_step_output(self) -> None:
        ctx = WorkflowContext()
        ctx.set_step_output("diagnose", "bearing wear")
        assert ctx.resolve("Result: {diagnose}") == "Result: bearing wear"

    def test_resolve_step_nested_dict(self) -> None:
        ctx = WorkflowContext()
        ctx.set_step_output("check_parts", {"available": True})
        assert ctx.resolve("Parts: {check_parts.available}") == "Parts: True"

    def test_resolve_unknown_placeholder(self) -> None:
        ctx = WorkflowContext()
        assert ctx.resolve("{unknown}") == "{unknown}"

    def test_resolve_trigger_no_field(self) -> None:
        ctx = WorkflowContext({"a": 1})
        result = ctx.resolve("{trigger}")
        assert "a" in result

    def test_resolve_multiple_placeholders(self) -> None:
        ctx = WorkflowContext({"asset_id": "P-201"})
        ctx.set_step_output("s1", "val1")
        result = ctx.resolve("{trigger.asset_id} -> {s1}")
        assert result == "P-201 -> val1"


class TestBuiltinAlarmToWorkorder:
    """Test the built-in alarm_to_workorder template loads correctly."""

    def test_import_and_structure(self) -> None:
        from machina.workflows.builtins import alarm_to_workorder

        assert alarm_to_workorder.name == "Alarm to Work Order"
        assert isinstance(alarm_to_workorder.trigger, Trigger)
        assert alarm_to_workorder.trigger.type == TriggerType.ALARM
        assert len(alarm_to_workorder.steps) == 7
        assert alarm_to_workorder.step_names[0] == "analyze_alarm"
        assert alarm_to_workorder.step_names[-1] == "submit_work_order"
