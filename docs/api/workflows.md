# Workflows

Workflows are declarative trigger-step pipelines for repeatable maintenance processes (alarm response, predictive routines, scheduled checks). They live alongside the agent's free-form LLM reasoning — use a workflow when the steps are well-defined and need to be deterministic; use the agent's tool-calling loop when the path is open-ended.

For end-to-end examples see the [`alarm_to_workorder` built-in](https://github.com/LGDiMaggio/machina/blob/main/src/machina/workflows/builtins/alarm_to_workorder.py) and [`examples/alarm_to_workorder`](https://github.com/LGDiMaggio/machina/tree/main/examples/alarm_to_workorder).

## Definition

### `Workflow`

::: machina.workflows.models.Workflow

### `Step`

::: machina.workflows.models.Step

### `Trigger`

::: machina.workflows.models.Trigger

### `TriggerType`

::: machina.workflows.models.TriggerType

### `GuardCondition`

::: machina.workflows.models.GuardCondition

### `ErrorPolicy`

::: machina.workflows.models.ErrorPolicy

## Execution

### `WorkflowEngine`

The engine that walks a [`Workflow`](#workflow), resolves template placeholders, dispatches each step's action, and applies the per-step error policy. Sandbox mode is configurable via the engine's `sandbox` attribute; when the engine is owned by an [`Agent`](agent.md), the agent's `sandbox` property propagates here automatically.

::: machina.workflows.engine.WorkflowEngine

### `WorkflowContext`

The mutable bag of data threaded through one workflow execution. Stores the trigger event and the output of every completed step, and exposes two template resolution methods — `resolve()` for free-text fields where the result must be a string, and `resolve_input_value()` for step inputs where complex outputs (dicts, objects) should flow through unchanged.

::: machina.workflows.models.WorkflowContext

## Results

### `WorkflowResult`

::: machina.workflows.models.WorkflowResult

### `StepResult`

::: machina.workflows.models.StepResult
