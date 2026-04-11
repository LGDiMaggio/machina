"""Tests for the Workflow and Step data models."""

from __future__ import annotations

from machina.workflows import Step, Workflow


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
