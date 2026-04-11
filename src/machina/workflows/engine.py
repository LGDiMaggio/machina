"""Workflow execution engine.

The :class:`WorkflowEngine` takes a :class:`~machina.workflows.models.Workflow`
definition and a trigger event, then executes each step sequentially —
resolving template variables, dispatching actions to connectors or
domain services, and handling errors according to per-step policies.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import structlog

from machina.connectors.base import ConnectorRegistry
from machina.exceptions import WorkflowError
from machina.observability.tracing import ActionTracer
from machina.workflows.models import (
    ErrorPolicy,
    Step,
    StepResult,
    Workflow,
    WorkflowContext,
    WorkflowResult,
)

logger = structlog.get_logger(__name__)


class WorkflowEngine:
    """Executes :class:`Workflow` definitions step by step.

    The engine resolves ``{step_name}`` and ``{trigger.*}`` template
    variables, dispatches each step's ``action`` to the appropriate
    handler, and records every step in the :class:`ActionTracer`.

    Args:
        registry: Connector registry for looking up connectors by
            capability.
        tracer: Action tracer for observability.
        llm: Optional LLM provider for ``agent.reason`` steps.
        services: Optional dict mapping service names to callables,
            e.g. ``{"failure_analyzer": analyzer_instance}``.
        sandbox: If ``True``, write actions are logged but not executed
            — read-only actions still run normally.

    Example:
        ```python
        from machina.workflows import WorkflowEngine, Workflow, Step

        engine = WorkflowEngine(registry=registry, tracer=tracer)
        result = await engine.execute(my_workflow, {"asset_id": "P-201"})
        ```
    """

    def __init__(
        self,
        *,
        registry: ConnectorRegistry | None = None,
        tracer: ActionTracer | None = None,
        llm: Any = None,
        services: dict[str, Any] | None = None,
        sandbox: bool = False,
    ) -> None:
        self._registry = registry or ConnectorRegistry()
        self._tracer = tracer or ActionTracer()
        self._llm = llm
        self._services = services or {}
        self.sandbox = sandbox

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def execute(
        self,
        workflow: Workflow,
        trigger_event: dict[str, Any] | None = None,
    ) -> WorkflowResult:
        """Execute a workflow from start to finish.

        Args:
            workflow: The workflow definition.
            trigger_event: The event data that triggered the workflow.

        Returns:
            A :class:`WorkflowResult` with per-step results.

        Raises:
            WorkflowError: If a step fails with :attr:`ErrorPolicy.STOP`.
        """
        trigger_event = trigger_event or {}
        context = WorkflowContext(trigger_event)
        step_results: list[StepResult] = []
        overall_success = True
        start = time.perf_counter()

        logger.info(
            "workflow_start",
            workflow=workflow.name,
            trigger=trigger_event,
            step_count=len(workflow.steps),
            sandbox=self.sandbox,
        )

        for step in workflow.steps:
            result = await self._execute_step(step, context)
            step_results.append(result)

            if result.success and not result.skipped:
                context.set_step_output(step.name, result.output)
            elif not result.success:
                overall_success = False
                if step.on_error == ErrorPolicy.STOP:
                    logger.error(
                        "workflow_stopped",
                        workflow=workflow.name,
                        failed_step=step.name,
                        error=result.error,
                    )
                    break

        elapsed = (time.perf_counter() - start) * 1000

        logger.info(
            "workflow_complete",
            workflow=workflow.name,
            success=overall_success,
            duration_ms=round(elapsed, 2),
            steps_executed=len(step_results),
        )

        return WorkflowResult(
            workflow_name=workflow.name,
            trigger_event=trigger_event,
            step_results=step_results,
            success=overall_success,
            duration_ms=round(elapsed, 2),
        )

    # ------------------------------------------------------------------
    # Step execution
    # ------------------------------------------------------------------

    async def _execute_step(
        self,
        step: Step,
        context: WorkflowContext,
    ) -> StepResult:
        """Execute a single step with guard check, error handling, and tracing."""
        # Guard condition check
        if step.guard is not None:
            try:
                should_run = step.guard.check(context.as_dict())
            except Exception:
                should_run = False
            if not should_run:
                logger.info(
                    "step_skipped_guard",
                    step=step.name,
                    guard=step.guard.description,
                )
                return StepResult(step_name=step.name, skipped=True)

        attempts = 1 + (step.retries if step.on_error == ErrorPolicy.RETRY else 0)

        for attempt in range(1, attempts + 1):
            start = time.perf_counter()
            try:
                with self._tracer.trace(
                    "workflow_step",
                    operation=step.name,
                ) as span:
                    if step.timeout_seconds is not None:
                        output = await asyncio.wait_for(
                            self._dispatch_action(step, context),
                            timeout=step.timeout_seconds,
                        )
                    else:
                        output = await self._dispatch_action(step, context)
                    span.output_summary = str(output)[:200]

                elapsed = (time.perf_counter() - start) * 1000
                logger.info(
                    "step_complete",
                    step=step.name,
                    attempt=attempt,
                    duration_ms=round(elapsed, 2),
                )
                return StepResult(
                    step_name=step.name,
                    output=output,
                    success=True,
                    duration_ms=round(elapsed, 2),
                )

            except TimeoutError:
                elapsed = (time.perf_counter() - start) * 1000
                error_msg = (
                    f"Step '{step.name}' timed out after {step.timeout_seconds}s"
                )
                logger.warning(
                    "step_timeout",
                    step=step.name,
                    attempt=attempt,
                    timeout=step.timeout_seconds,
                )
                if attempt < attempts:
                    continue
                return self._handle_step_failure(step, error_msg, elapsed)

            except Exception as exc:
                elapsed = (time.perf_counter() - start) * 1000
                error_msg = f"{type(exc).__name__}: {exc}"
                logger.warning(
                    "step_error",
                    step=step.name,
                    attempt=attempt,
                    error=error_msg,
                )
                if attempt < attempts:
                    continue
                return self._handle_step_failure(step, error_msg, elapsed)

        # Should not reach here, but be safe
        return StepResult(step_name=step.name, success=False, error="Unexpected")  # pragma: no cover

    def _handle_step_failure(
        self,
        step: Step,
        error_msg: str,
        elapsed_ms: float,
    ) -> StepResult:
        """Apply the step's error policy after all retries are exhausted."""
        if step.on_error == ErrorPolicy.SKIP:
            logger.info("step_skipped_error", step=step.name, error=error_msg)
            return StepResult(
                step_name=step.name,
                success=True,
                skipped=True,
                duration_ms=round(elapsed_ms, 2),
            )

        if step.on_error == ErrorPolicy.NOTIFY:
            logger.error("step_failed_notify", step=step.name, error=error_msg)
            return StepResult(
                step_name=step.name,
                success=False,
                error=error_msg,
                duration_ms=round(elapsed_ms, 2),
            )

        # ErrorPolicy.STOP or ErrorPolicy.RETRY (retries exhausted)
        return StepResult(
            step_name=step.name,
            success=False,
            error=error_msg,
            duration_ms=round(elapsed_ms, 2),
        )

    # ------------------------------------------------------------------
    # Action dispatch
    # ------------------------------------------------------------------

    async def _dispatch_action(
        self,
        step: Step,
        context: WorkflowContext,
    ) -> Any:
        """Route a step's action string to the appropriate handler.

        Action routing:

        * ``agent.reason`` — call the LLM with the step's resolved prompt.
        * ``channels.send_message`` — send the resolved template via a
          communication connector.
        * ``failure_analyzer.diagnose``, ``work_order_factory.create``,
          ``maintenance_scheduler.*`` — call registered domain services.
        * ``<category>.<method>`` — find a connector by capability and
          call the method.
        """
        action = step.action

        if not action:
            return None

        # -- LLM reasoning step ----------------------------------------
        if action == "agent.reason":
            return await self._dispatch_llm(step, context)

        # -- Notification step -----------------------------------------
        if action == "channels.send_message":
            return await self._dispatch_notification(step, context)

        # -- Domain services -------------------------------------------
        parts = action.split(".", 1)
        service_name = parts[0]

        if service_name in self._services:
            return await self._dispatch_service(step, context)

        # -- Connector action ------------------------------------------
        return await self._dispatch_connector(step, context)

    async def _dispatch_llm(self, step: Step, context: WorkflowContext) -> Any:
        """Call the LLM with the step's resolved prompt."""
        if self._llm is None:
            raise WorkflowError(
                f"Step '{step.name}' requires an LLM but none is configured"
            )

        resolved_prompt = context.resolve(step.prompt)

        if self.sandbox:
            logger.info(
                "sandbox_llm",
                step=step.name,
                prompt_preview=resolved_prompt[:200],
            )
            return f"[SANDBOX] LLM response for: {resolved_prompt[:100]}"

        messages = [
            {"role": "system", "content": "You are a maintenance domain expert."},
            {"role": "user", "content": resolved_prompt},
        ]
        return await self._llm.complete(messages)

    async def _dispatch_notification(
        self,
        step: Step,
        context: WorkflowContext,
    ) -> Any:
        """Send a notification via a communication connector."""
        resolved = context.resolve(step.template or step.prompt)

        if self.sandbox:
            logger.info(
                "sandbox_notification",
                step=step.name,
                message_preview=resolved[:200],
            )
            return {"sent": False, "sandbox": True, "message": resolved}

        connectors = self._registry.find_by_capability("send_message")
        if not connectors:
            logger.warning("no_comms_connector", step=step.name)
            return {"sent": False, "error": "No communication connector available"}

        _, conn = connectors[0]
        await conn.send_message(resolved)  # type: ignore[attr-defined]
        return {"sent": True, "message": resolved}

    async def _dispatch_service(
        self,
        step: Step,
        context: WorkflowContext,
    ) -> Any:
        """Call a registered domain service method."""
        parts = step.action.split(".", 1)
        service_name = parts[0]
        method_name = parts[1] if len(parts) > 1 else ""

        service = self._services.get(service_name)
        if service is None:
            raise WorkflowError(
                f"Step '{step.name}': service '{service_name}' not registered"
            )

        # Resolve inputs from template variables
        resolved_inputs = {
            k: context.resolve(v) for k, v in step.inputs.items()
        }

        if self.sandbox and self._is_write_action(step.action):
            logger.info(
                "sandbox_service",
                step=step.name,
                action=step.action,
                inputs=resolved_inputs,
            )
            return {"sandbox": True, "action": step.action, "inputs": resolved_inputs}

        method = getattr(service, method_name, None)
        if method is None:
            raise WorkflowError(
                f"Step '{step.name}': service '{service_name}' has no method '{method_name}'"
            )

        if asyncio.iscoroutinefunction(method):
            return await method(**resolved_inputs)
        return method(**resolved_inputs)

    async def _dispatch_connector(
        self,
        step: Step,
        context: WorkflowContext,
    ) -> Any:
        """Call a connector method looked up by capability."""
        parts = step.action.split(".", 1)
        capability = parts[1] if len(parts) > 1 else step.action

        # Resolve inputs
        resolved_inputs = {
            k: context.resolve(v) for k, v in step.inputs.items()
        }

        if self.sandbox and self._is_write_action(step.action):
            logger.info(
                "sandbox_connector",
                step=step.name,
                action=step.action,
                inputs=resolved_inputs,
            )
            return {"sandbox": True, "action": step.action, "inputs": resolved_inputs}

        connectors = self._registry.find_by_capability(capability)
        if not connectors:
            raise WorkflowError(
                f"Step '{step.name}': no connector with capability '{capability}'"
            )

        _, conn = connectors[0]
        method = getattr(conn, capability, None)
        if method is None:
            raise WorkflowError(
                f"Step '{step.name}': connector has no method '{capability}'"
            )

        if asyncio.iscoroutinefunction(method):
            return await method(**resolved_inputs)
        return method(**resolved_inputs)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _is_write_action(action: str) -> bool:
        """Return ``True`` if the action is a write/mutation operation."""
        write_keywords = {
            "create",
            "update",
            "delete",
            "send",
            "submit",
            "write",
            "notify",
        }
        return any(kw in action.lower() for kw in write_keywords)
