"""Agent runtime — orchestrates LLM, connectors, and domain logic.

The :class:`Agent` is the central class of Machina.  It receives
messages (from Telegram, CLI, or programmatically), resolves entities,
gathers context from connectors, calls the LLM with domain-aware
prompts, and executes tool calls.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

import structlog

from machina.agent.citations import parse_response
from machina.agent.entity_resolver import EntityResolver
from machina.agent.prompts import (
    build_context_message,
    build_system_prompt,
    safe_source,
    safe_text,
)
from machina.connectors.base import ConnectorRegistry, set_sandbox_mode
from machina.connectors.capabilities import Capability
from machina.domain.citation import AgentResponse
from machina.domain.plant import Plant
from machina.exceptions import LLMError
from machina.llm.provider import LLMProvider
from machina.llm.tools import BUILTIN_TOOLS, MUTATING_TOOLS
from machina.observability.tracing import ActionTracer
from machina.workflows.engine import WorkflowEngine

if TYPE_CHECKING:
    from machina.workflows.models import Workflow, WorkflowResult

logger = structlog.get_logger(__name__)

# Tools whose execution mutates external state, memoised per turn in the LLM
# loop so a model that re-requests the same write does not trigger the side
# effect twice. Sourced from llm.tools.MUTATING_TOOLS (single source of truth,
# co-located with the tool definitions) to prevent drift between the dispatch
# table and this guard.
_SIDE_EFFECTING_TOOLS: frozenset[str] = MUTATING_TOOLS

# Deterministic affirmation / decline token sets for the two-turn confirmation
# degrade (U5). A weak local model must NOT be the component that interprets a
# "yes" — the parse here is mechanical, never delegated to the LLM. Tokens cover
# English + Italian (the example's two primary languages); matching is
# case/whitespace-insensitive and requires the WHOLE message to be a single
# token (a compound like "ok, but set priority high" is deliberately neither —
# treated as unrelated, which cancels the pending action). Unicode/homoglyph
# folding is intentionally deferred (see plan "Deferred to Implementation").
_AFFIRMATION_TOKENS: frozenset[str] = frozenset(
    {"y", "yes", "ok", "okay", "confirm", "confirmed", "sì", "si", "conferma", "procedi", "vai"}
)
_DECLINE_TOKENS: frozenset[str] = frozenset({"n", "no", "annulla", "cancel", "stop", "abort"})


def _format_response_for_channel(response: AgentResponse) -> str:
    """Render an :class:`AgentResponse` for delivery on a channel.

    Inline ``[source:page]`` markers are already in ``response.text``.
    When citations are present, a compact ``Sources`` footer is appended
    so the operator can trace the answer back to its origin in chat
    surfaces that don't expose the structured field.
    """
    if not response.citations:
        return response.text
    sources = "\n".join(f"  • {c.inline_marker()}" for c in response.citations)
    return f"{response.text}\n\n— Sources:\n{sources}"


class Agent:
    """Maintenance AI agent that orchestrates reasoning and actions.

    The agent receives user queries, resolves referenced assets,
    gathers context from configured connectors, and uses an LLM to
    produce grounded, domain-aware responses.

    Args:
        name: Human-readable agent name.
        description: What this agent specialises in.
        plant: The plant with its asset registry.
        connectors: List of connector instances to register.
        channels: Communication channels (Telegram, CLI, etc.).
        llm: LLM provider string (e.g. ``"openai:gpt-4o"``) or
             an :class:`LLMProvider` instance.
        temperature: LLM sampling temperature.
        max_history: Maximum conversation turns to keep in memory.
        workflows: List of workflow definitions to register.
        sandbox: If ``True``, write actions are logged but not executed.
        confirmations: If ``True`` (default), the agent requires human
            confirmation before executing write/mutation tool calls.

    Example:
        ```python
        from machina import Agent, Plant
        from machina.connectors.cmms import GenericCmmsConnector
        from machina.connectors.comms.telegram import CliChannel

        plant = Plant(name="Demo Plant")
        cmms = GenericCmmsConnector(data_dir="sample_data/cmms")

        agent = Agent(
            name="Maintenance Assistant",
            plant=plant,
            connectors=[cmms],
            channels=[CliChannel()],
            llm="openai:gpt-4o",
        )
        agent.run()
        ```
    """

    def __init__(
        self,
        *,
        name: str = "Machina Agent",
        description: str = "Maintenance AI assistant",
        plant: Plant | None = None,
        connectors: list[Any] | None = None,
        channels: list[Any] | None = None,
        llm: str | LLMProvider = "openai:gpt-4o",
        temperature: float = 0.1,
        max_history: int = 20,
        workflows: list[Workflow] | None = None,
        sandbox: bool = False,
        confirmations: bool = True,
    ) -> None:
        self.name = name
        self.description = description
        self.plant = plant or Plant(name="Default")
        self._channels = channels or []
        self._max_history = max_history
        self._max_message_length = 10_000

        # LLM provider
        if isinstance(llm, str):
            self._llm = LLMProvider(model=llm, temperature=temperature)
        else:
            self._llm = llm

        # Connector registry
        self._registry = ConnectorRegistry()
        _registered_ids: set[int] = set()
        for i, conn in enumerate(connectors or []):
            cname = getattr(conn, "__class__", type(conn)).__name__
            self._registry.register(f"{cname}_{i}", conn)
            _registered_ids.add(id(conn))

        # Channels are also registered so workflow steps that dispatch via
        # ``find_by_capability`` (e.g. ``channels.send_message``) can reach
        # comms connectors passed as ``channels=``. Dedup by identity: a
        # connector passed to BOTH ``connectors=`` and ``channels=`` is
        # registered once, not twice. See issue #31.
        for i, chan in enumerate(self._channels):
            if id(chan) in _registered_ids:
                continue
            cname = getattr(chan, "__class__", type(chan)).__name__
            self._registry.register(f"channel_{cname}_{i}", chan)
            _registered_ids.add(id(chan))

        # Entity resolver
        self._resolver = EntityResolver(self.plant)

        # Action tracer
        self.tracer = ActionTracer()

        # Sandbox mode — stored on the instance, propagated to the
        # workflow engine via the ``sandbox`` property setter below, and
        # mirrored into the ``connectors.base._sandbox_mode`` contextvar
        # so ``@sandbox_aware`` connector methods see the same value as
        # the engine's heuristic gate.
        self._sandbox = sandbox
        set_sandbox_mode(sandbox)

        # Confirmation gate — agent-loop-local switch (no contextvar, unlike
        # sandbox). Read directly by ``_llm_loop`` to decide whether a
        # write/mutation tool call must be confirmed by a human before it
        # executes. On by default. The gate logic itself is consumed
        # elsewhere; this just holds the value.
        self._confirmations = confirmations

        # Workflow engine
        self._workflows: dict[str, Workflow] = {}
        self._engine = WorkflowEngine(
            registry=self._registry,
            tracer=self.tracer,
            llm=self._llm,
            sandbox=sandbox,
        )
        for wf in workflows or []:
            self._workflows[wf.name] = wf

        # Conversation history per chat
        self._histories: dict[str, list[dict[str, str]]] = {}

        # Turn-surviving pending-action store for the two-turn confirmation
        # degrade (U5). Keyed (chat_id, user_id) → (func_name, args, prompt).
        # Follows the ``self._histories`` lifecycle (persists across turns) —
        # deliberately NOT ``self._turn_chunks``, which is reset/popped each
        # turn and would wipe a pending action before the confirming message
        # arrives. On a shared/group channel the key includes ``user_id`` so a
        # different participant cannot confirm another user's pending write.
        self._pending_actions: dict[tuple[str, str], tuple[str, dict[str, Any], str]] = {}

        # Per-turn chunk registry (chat_id -> chunk_id -> {source, page, content}).
        # Populated by _gather_context and the search_documents tool; consumed by
        # citation parsing at the end of each turn for the source/page fallback.
        self._turn_chunks: dict[str, dict[str, dict[str, Any]]] = {}

        # Per-turn ordered index map (chat_id -> [chunk_id by display position]).
        # Element ``i`` is the chunk the model saw as ``[i + 1]``; an empty
        # string marks a displayed-but-unregistered slot so the visible index
        # stays aligned with what the model saw. Built from the SAME
        # ``enumerate(results[:5], 1)`` the prompt rendering uses — never from
        # the filtered registry, which would drift off-by-k.
        self._turn_ordered: dict[str, list[str]] = {}

    # ------------------------------------------------------------------
    # Sandbox mode — single mutation point, propagates to the engine
    # ------------------------------------------------------------------

    @property
    def sandbox(self) -> bool:
        """Whether write actions are intercepted (``True``) or executed.

        Read this attribute through normal access — no behaviour change
        for existing call sites that branch on ``if self.sandbox``.
        """
        return self._sandbox

    @sandbox.setter
    def sandbox(self, value: bool) -> None:
        """Toggle sandbox mode atomically across every enforcement layer.

        Guarantees that the workflow engine's heuristic gate and the
        connector-level ``@sandbox_aware`` decorator both see the new
        value on the next call.  Three pieces of state are updated in
        one place so they cannot drift:

        * ``self._sandbox`` — the canonical value read by ``Agent``
          itself in the ``if self.sandbox`` branches.
        * ``self._engine.sandbox`` — the workflow engine's snapshot.
          Without this, a mutation after construction would leave the
          engine running in its construction-time mode (the original
          ``--live``-ignored bug).
        * ``connectors.base._sandbox_mode`` contextvar — the variable
          the ``@sandbox_aware`` decorator on connector methods checks.
          Without this update, a custom connector write action whose
          name does not match the engine's keyword heuristic (e.g.
          ``cmms.dispatch_field_team``) would bypass both engine and
          decorator and execute against the real system.
        """
        self._sandbox = value
        self._engine.sandbox = value
        set_sandbox_mode(value)

    # ------------------------------------------------------------------
    # Confirmation gate — agent-loop-local, no contextvar
    # ------------------------------------------------------------------

    @property
    def confirmations(self) -> bool:
        """Whether write/mutation tool calls require human confirmation.

        Read this attribute through normal access — the value is consumed
        inside the agent loop to gate side-effecting tool calls. Unlike
        :attr:`sandbox`, there is no contextvar or engine snapshot: the
        switch is purely agent-loop-local.
        """
        return self._confirmations

    @confirmations.setter
    def confirmations(self, value: bool) -> None:
        """Toggle the confirmation gate at runtime.

        Sets the agent-loop-local flag read by the loop. No contextvar or
        engine state is involved (in contrast to :attr:`sandbox`), so this
        setter only updates ``self._confirmations``.
        """
        self._confirmations = value

    # ------------------------------------------------------------------
    # Public API — factory
    # ------------------------------------------------------------------

    @classmethod
    def from_config(cls, path: str | Path) -> Agent:
        """Create an Agent from a ``machina.yaml`` configuration file.

        Connectors and channels are instantiated from their ``type``
        strings.  Workflows cannot be defined in YAML (they may
        contain Python callables); register them after construction
        with :meth:`register_workflow`.

        Args:
            path: Path to the YAML configuration file.

        Returns:
            A fully configured ``Agent`` instance.

        Example:
            ```python
            from machina import Agent

            agent = Agent.from_config("machina.yaml")
            agent.run()
            ```
        """
        from machina.config import load_config
        from machina.connectors.factory import create_channel, create_connector

        config = load_config(path)

        plant = Plant(name=config.plant.name, location=config.plant.location)

        connectors = [
            create_connector(cc.type, cc.settings)
            for cc in config.connectors.values()
            if cc.enabled
        ]

        if config.channels:
            channels = [create_channel(ch.type, ch.settings) for ch in config.channels]
        else:
            from machina.connectors.comms.telegram import CliChannel

            channels = [CliChannel()]

        return cls(
            name=config.name,
            description=config.description,
            plant=plant,
            connectors=connectors,
            channels=channels,
            llm=config.llm.provider,
            temperature=config.llm.temperature,
            sandbox=config.sandbox,
            confirmations=config.confirmations,
        )

    # ------------------------------------------------------------------
    # Public API — workflows
    # ------------------------------------------------------------------

    @property
    def workflows(self) -> dict[str, Workflow]:
        """Registered workflows (read-only copy)."""
        return dict(self._workflows)

    def register_workflow(self, workflow: Workflow) -> None:
        """Register a workflow for later execution.

        Args:
            workflow: The workflow definition to register.
        """
        self._workflows[workflow.name] = workflow
        logger.info("workflow_registered", workflow=workflow.name)

    async def trigger_workflow(
        self,
        workflow_name: str,
        event: dict[str, Any] | None = None,
    ) -> WorkflowResult:
        """Trigger a registered workflow by name.

        Args:
            workflow_name: Name of a previously registered workflow.
            event: Event data to pass to the workflow.

        Returns:
            A :class:`WorkflowResult` with per-step outcomes.

        Raises:
            WorkflowError: If the workflow is not found.
        """
        from machina.exceptions import WorkflowError

        workflow = self._workflows.get(workflow_name)
        if workflow is None:
            raise WorkflowError(f"Workflow '{workflow_name}' not registered")
        return await self._engine.execute(workflow, event)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Connect all connectors and load assets."""
        channel_ids = {id(ch) for ch in self._channels}
        for name, conn in self._registry.all().items():
            # Channels are connected below, with a sandbox guard. Skip them
            # here to avoid double-connect now that they share the registry.
            if id(conn) in channel_ids:
                continue
            with self.tracer.trace("connector_connect", connector=name):
                await conn.connect()
            logger.info("connector_ready", connector=name)

        # Auto-load assets from CMMS connectors
        cmms_connectors = self._registry.find_by_capability(Capability.READ_ASSETS)
        for cname, conn in cmms_connectors:
            with self.tracer.trace("load_assets", connector=cname) as span:
                assets = await conn.read_assets()  # type: ignore[attr-defined]
                for asset in assets:
                    self.plant.register_asset(asset)
                span.output_summary = f"Loaded {len(assets)} assets"
            logger.info(
                "assets_loaded",
                connector=cname,
                count=len(assets),
            )

        # Auto-load failure modes and build domain services
        self._build_domain_services()

        # Connect channels. In sandbox mode we skip outbound I/O so
        # channels like EmailConnector do not perform real SMTP logins.
        # See issue #31.
        for channel in self._channels:
            cname = getattr(channel, "__class__", type(channel)).__name__
            if self.sandbox:
                logger.info("sandbox_skip_channel_connect", channel=cname)
                continue
            await channel.connect()

        logger.info(
            "agent_started",
            agent=self.name,
            asset_count=len(self.plant.assets),
            connectors=list(self._registry.all().keys()),
        )

    def _build_domain_services(self) -> None:
        """Build domain services from loaded data and register them with the workflow engine."""
        from machina.domain.services.asset_service import AssetService
        from machina.domain.services.failure_analyzer import FailureAnalyzer
        from machina.domain.services.maintenance_scheduler import MaintenanceScheduler
        from machina.domain.services.work_order_factory import WorkOrderFactory

        # Collect failure modes from connectors that provide them
        all_failure_modes = []
        for _name, conn in self._registry.all().items():
            if hasattr(conn, "_failure_modes"):
                all_failure_modes.extend(conn._failure_modes)

        # Collect maintenance plans from connectors that provide them
        all_plans = []
        for _name, conn in self._registry.all().items():
            if hasattr(conn, "_maintenance_plans"):
                all_plans.extend(conn._maintenance_plans)

        analyzer = FailureAnalyzer(failure_modes=all_failure_modes)
        factory = WorkOrderFactory()
        scheduler = MaintenanceScheduler(plans=all_plans)
        asset_service = AssetService(plant=self.plant)

        self._engine._services = {
            "failure_analyzer": analyzer,
            "work_order_factory": factory,
            "maintenance_scheduler": scheduler,
            "domain": asset_service,
        }

        # Defence against ``_engine`` being replaced or rebuilt after
        # construction (e.g. by tests, subclasses, or future hot-reload
        # logic).  Re-apply the canonical sandbox value so the engine's
        # snapshot cannot drift from ``self._sandbox``.
        self._engine.sandbox = self._sandbox

        if all_failure_modes:
            logger.info(
                "domain_services_ready",
                agent=self.name,
                failure_modes=len(all_failure_modes),
            )

    async def stop(self) -> None:
        """Disconnect all connectors and channels."""
        channel_ids = {id(ch) for ch in self._channels}
        for channel in self._channels:
            cname = getattr(channel, "__class__", type(channel)).__name__
            if self.sandbox:
                logger.info("sandbox_skip_channel_disconnect", channel=cname)
                continue
            await channel.disconnect()
        for _name, conn in self._registry.all().items():
            if id(conn) in channel_ids:
                continue
            await conn.disconnect()
        logger.info("agent_stopped", agent=self.name)

    async def handle_message(
        self,
        text: str,
        *,
        chat_id: str = "default",
        confirmer: Callable[[str], Awaitable[bool]] | None = None,
        user_id: str = "",
    ) -> str:
        """Process a user message and return the agent's response text.

        This is the main entry point for programmatic usage. The returned
        string is the rendered answer with inline ``[source:page]``
        markers preserved but the trailing ``<citations>`` block stripped.
        Use :meth:`handle_message_full` to also access structured
        :class:`Citation` objects.

        Args:
            text: The user's message.
            chat_id: Identifier for the conversation.
            confirmer: Optional async callable that renders a confirmation
                prompt and returns the user's yes/no decision. Supplied by a
                channel that can confirm a write synchronously (e.g.
                ``CliChannel``). When ``None`` and :attr:`confirmations` is on,
                a mutating tool call is NOT executed (fail-safe).
            user_id: Identifier for the sender, forwarded for cross-user
                confirmation scoping. Note: ``confirmations`` only gates writes
                that flow through the agent LLM loop; ``trigger_workflow`` is a
                deliberate direct-execution path guarded by ``sandbox`` only.

        Returns:
            The agent's response text.

        Raises:
            LLMError: If the underlying LLM call fails.
        """
        response = await self.handle_message_full(
            text, chat_id=chat_id, confirmer=confirmer, user_id=user_id
        )
        return response.text

    async def handle_message_full(
        self,
        text: str,
        *,
        chat_id: str = "default",
        confirmer: Callable[[str], Awaitable[bool]] | None = None,
        user_id: str = "",
    ) -> AgentResponse:
        """Process a user message and return the structured agent response.

        Args:
            text: The user's message.
            chat_id: Identifier for the conversation.
            confirmer: Optional async callable that renders a confirmation
                prompt and returns the user's yes/no decision (see
                :meth:`handle_message`). When ``None`` and
                :attr:`confirmations` is on, a mutating tool call is NOT
                executed (fail-safe — a programmatic caller that wants
                autonomous writes sets ``confirmations=False`` or passes a
                ``confirmer``).
            user_id: Identifier for the sender, forwarded for cross-user
                confirmation scoping.

        Returns:
            An :class:`AgentResponse` with the rendered text and any
            citations the agent emitted.

        Raises:
            LLMError: If the underlying LLM call fails.
        """
        if len(text) > self._max_message_length:
            original_length = len(text)
            text = text[: self._max_message_length]
            logger.warning(
                "message_truncated",
                agent=self.name,
                chat_id=chat_id,
                original_length=original_length,
                max_length=self._max_message_length,
            )

        logger.info(
            "message_received",
            agent=self.name,
            chat_id=chat_id,
            message_preview=text[:100],
        )

        # Two-turn confirmation resume (U5). If a write is pending for this
        # (chat_id, user_id), interpret THIS message deterministically (never
        # via the LLM): a bare affirmation executes the pending write and
        # re-enters the loop in narration-only mode; anything else (a decline
        # OR an unrelated message) cancels the pending action and falls through
        # to normal processing — so an unrelated next message never silently
        # executes.
        pending = self._pending_actions.get((chat_id, user_id))
        if pending is not None:
            confirmed = await self._resume_pending_action(
                pending, text, chat_id=chat_id, user_id=user_id
            )
            if confirmed is not None:
                return confirmed

        # Reset the per-turn chunk registry and ordered index map.
        self._turn_chunks[chat_id] = {}
        self._turn_ordered[chat_id] = []

        try:
            # 1. Entity resolution
            resolved = self._resolver.resolve(text)

            # 2. Gather context from connectors
            context_data = await self._gather_context(text, resolved, chat_id=chat_id)

            # 3. Build messages
            messages = self._build_messages(text, chat_id, context_data)

            # 4. Call LLM (with tool-calling loop)
            try:
                raw_response = await self._llm_loop(
                    messages, chat_id, confirmer=confirmer, user_id=user_id
                )
            except Exception as exc:
                logger.error(
                    "llm_error",
                    agent=self.name,
                    error=str(exc),
                )
                raise LLMError(f"LLM call failed: {exc}") from exc

            # 5. Parse citations against the per-turn chunk registry. The
            #    ordered index map resolves visible ``[n]`` markers; the
            #    registry backs the source/page fallback.
            rendered, citations = parse_response(
                raw_response,
                self._turn_chunks.get(chat_id, {}),
                self._turn_ordered.get(chat_id, []),
            )

            # 6. Update history (use the rendered text without citation block).
            self._add_to_history(chat_id, "user", text)
            self._add_to_history(chat_id, "assistant", rendered)
        finally:
            # Always drop the per-turn registry — even on LLM errors — so a
            # long-lived agent does not accumulate orphan slots from failed
            # turns.
            self._turn_chunks.pop(chat_id, None)
            self._turn_ordered.pop(chat_id, None)

        logger.info(
            "response_generated",
            agent=self.name,
            chat_id=chat_id,
            response_length=len(rendered),
            citation_count=len(citations),
        )
        return AgentResponse(text=rendered, citations=citations)

    def run(self) -> None:
        """Start the agent with all channels (blocking, sync wrapper).

        Connects connectors, loads assets, and starts listening on
        all configured channels.  Automatically detects Jupyter
        notebooks and other environments with an already-running
        event loop.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop is not None:
            # Already inside an event loop (Jupyter, async REPL, etc.)
            # Schedule the coroutine on the existing loop.
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(asyncio.run, self._run_async()).result()
        else:
            asyncio.run(self._run_async())

    async def _run_async(self) -> None:
        """Async main loop — start agent and listen on channels."""
        await self.start()

        if not self._channels:
            logger.warning("no_channels", agent=self.name)
            return

        # Use the first channel for listen (typically Telegram or CLI)
        channel = self._channels[0]

        # Bind this channel's synchronous confirmation primitive (if any) into
        # the handler so the HITL gate can prompt in-turn. A channel without it
        # (async channels) leaves ``confirmer=None`` → the runtime fails safe
        # (U4) and U5 extends that into the two-turn propose→confirm flow.
        from machina.connectors.comms.types import supports_sync_confirmation

        sync_confirm = supports_sync_confirmation(channel)

        async def _handler(msg: Any) -> str:
            confirmer: Callable[[str], Awaitable[bool]] | None = None
            if sync_confirm:

                async def confirmer(prompt: str, _msg: Any = msg) -> bool:
                    return bool(await channel.request_confirmation(_msg.chat_id, prompt))

            response = await self.handle_message_full(
                msg.text,
                chat_id=msg.chat_id,
                confirmer=confirmer,
                user_id=getattr(msg, "user_id", ""),
            )
            return _format_response_for_channel(response)

        try:
            await channel.listen(_handler)
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            await self.stop()

    # ------------------------------------------------------------------
    # Internal: context gathering
    # ------------------------------------------------------------------

    async def _gather_context(
        self,
        text: str,
        resolved: list[Any],
        *,
        chat_id: str = "default",
    ) -> dict[str, Any]:
        """Gather context from connectors based on resolved entities."""
        context: dict[str, Any] = {
            "resolved_entities": resolved,
        }

        if not resolved:
            return context

        asset = resolved[0].asset
        context["asset"] = asset

        # Gather work orders, spare parts in parallel
        tasks: list[Any] = []
        task_names: list[str] = []

        wo_connectors = self._registry.find_by_capability(Capability.READ_WORK_ORDERS)
        if wo_connectors:
            wo_cname, wo_conn = wo_connectors[0]

            async def _get_wos(_cname: str = wo_cname, _conn: Any = wo_conn) -> list[Any]:
                with self.tracer.trace(
                    "connector_query",
                    connector=_cname,
                    asset_id=asset.id,
                    operation="read_work_orders",
                ):
                    return await _conn.read_work_orders(asset_id=asset.id)  # type: ignore[no-any-return]

            tasks.append(_get_wos())
            task_names.append("work_orders")

        sp_connectors = self._registry.find_by_capability(Capability.READ_SPARE_PARTS)
        if sp_connectors:
            sp_cname, sp_conn = sp_connectors[0]

            async def _get_parts(_cname: str = sp_cname, _conn: Any = sp_conn) -> list[Any]:
                with self.tracer.trace(
                    "connector_query",
                    connector=_cname,
                    asset_id=asset.id,
                    operation="read_spare_parts",
                ):
                    return await _conn.read_spare_parts(asset_id=asset.id)  # type: ignore[no-any-return]

            tasks.append(_get_parts())
            task_names.append("spare_parts")

        # Document search
        doc_connectors = self._registry.find_by_capability(Capability.SEARCH_DOCUMENTS)
        if doc_connectors:
            doc_cname, doc_conn = doc_connectors[0]

            async def _search_docs(_cname: str = doc_cname, _conn: Any = doc_conn) -> list[Any]:
                with self.tracer.trace(
                    "connector_query",
                    connector=_cname,
                    asset_id=asset.id,
                    operation="search_documents",
                ):
                    results = await _conn.search(text, asset_id=asset.id)
                    # Sanitise source and content at the LLM boundary so
                    # absolute file paths never reach the prompt context —
                    # safe_source for the metadata field, safe_text for paths
                    # embedded in the chunk body. See prompts.safe_source/safe_text.
                    return [
                        {
                            "content": safe_text(r.content),
                            "source": safe_source(r.source),
                            "page": r.page,
                            "chunk_id": getattr(r, "chunk_id", ""),
                            "section_title": getattr(r, "section_title", ""),
                            "is_table": getattr(r, "is_table", False),
                        }
                        for r in results
                    ]

            tasks.append(_search_docs())
            task_names.append("document_results")

        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for name, result in zip(task_names, results, strict=False):
                if isinstance(result, BaseException):
                    logger.warning(
                        "context_gather_error",
                        agent=self.name,
                        source=name,
                        error=str(result),
                    )
                else:
                    context[name] = result

        # Register any retrieved document chunks against the per-turn registry
        # so citation parsing can validate chunk_id references later.
        self._register_document_results(chat_id, context.get("document_results") or [])

        return context

    def _register_document_results(self, chat_id: str, results: list[dict[str, Any]]) -> None:
        """Register retrieved chunks for citation parsing.

        Builds two per-turn structures from the **same** ``results[:5]``
        enumeration the prompt renders:

        * ``self._turn_chunks[chat_id]`` — ``chunk_id`` → metadata, backing
          the source/page citation fallback (skips empty ``chunk_id`` rows).
        * ``self._turn_ordered[chat_id]`` — ``chunk_id`` by display position,
          so the visible ``[n]`` the model saw resolves directly. A
          displayed-but-unregistered row (empty ``chunk_id``) is appended as
          an empty string so later indices stay aligned with the prompt and
          do not drift off-by-k.

        Truncation to ``[:5]`` mirrors :func:`format_document_results`, which
        only renders the first five results.
        """
        registry = self._turn_chunks.setdefault(chat_id, {})
        ordered = self._turn_ordered.setdefault(chat_id, [])
        for r in results[:5]:
            chunk_id = r.get("chunk_id") or ""
            ordered.append(chunk_id)
            if not chunk_id:
                continue
            registry[chunk_id] = {
                "source": r.get("source", ""),
                "page": r.get("page", 0),
                "content": r.get("content", ""),
            }

    # ------------------------------------------------------------------
    # Internal: message building
    # ------------------------------------------------------------------

    def _build_messages(
        self,
        text: str,
        chat_id: str,
        context_data: dict[str, Any],
    ) -> list[dict[str, str]]:
        """Build the LLM message list with system prompt, context, and history."""
        # Gather all capabilities
        all_caps: list[str] = []
        for _, conn in self._registry.all().items():
            all_caps.extend(conn.capabilities)

        system = build_system_prompt(
            plant_name=self.plant.name,
            asset_count=len(self.plant.assets),
            capabilities=all_caps,
            workflows=list(self._workflows.keys()),
            sandbox=self._sandbox,
        )

        messages: list[dict[str, str]] = [{"role": "system", "content": system}]

        # Add domain context
        context_str = build_context_message(
            resolved_entities=context_data.get("resolved_entities"),
            asset=context_data.get("asset"),
            work_orders=context_data.get("work_orders"),
            alarms=context_data.get("alarms"),
            spare_parts=context_data.get("spare_parts"),
            document_results=context_data.get("document_results"),
        )
        if context_str:
            messages.append(
                {"role": "system", "content": f"## Retrieved Context\n\n{context_str}"}
            )

        # Add conversation history
        history = self._histories.get(chat_id, [])
        messages.extend(history)

        # Add current user message
        messages.append({"role": "user", "content": text})

        return messages

    # ------------------------------------------------------------------
    # Internal: LLM tool-calling loop
    # ------------------------------------------------------------------

    async def _llm_loop(
        self,
        messages: list[dict[str, str]],
        chat_id: str,
        *,
        max_iterations: int = 5,
        confirmer: Callable[[str], Awaitable[bool]] | None = None,
        user_id: str = "",
        narration_only: bool = False,
        seed_side_effects: dict[str, Any] | None = None,
    ) -> str:
        """Call the LLM, execute tool calls, and return final response.

        When :attr:`confirmations` is on, every mutating tool call
        (``func_name in _SIDE_EFFECTING_TOOLS``) is gated:

        * **sandbox on** → the gate is skipped (the write short-circuits to a
          no-op inside the tool; confirming a no-op would mislead).
        * **``confirmer`` available** (synchronous channels, e.g. CLI) → the
          decision is awaited; on yes the write executes, on no a structured
          ``{"declined": ...}`` result is returned without executing.
        * **no ``confirmer``** (programmatic callers / async channels) → the
          write is NOT executed; :meth:`_await_write_confirmation` stores the
          pending action and returns a fail-safe
          ``{"confirmation_required": ...}`` result (the two-turn flow).

        Args:
            narration_only: When ``True`` (the two-turn confirmation re-entry,
                U5) the offered toolset EXCLUDES :data:`_SIDE_EFFECTING_TOOLS`
                so the model cannot request another write while narrating the
                just-confirmed one — preventing a re-gate or a reworded
                duplicate. Combined with ``seed_side_effects``, even a verbatim
                re-issue collapses to the memoised result.
            seed_side_effects: Optional pre-populated side-effect memo (the
                ``(func_name, args) → result`` of the just-confirmed write) so
                a verbatim re-issue during narration collapses instead of
                executing again.
        """
        tools = self._get_available_tools()
        if narration_only:
            # Suppress mutating tools entirely for this pass so an eager model
            # cannot ratchet a second confirmation or write a reworded duplicate
            # while narrating the just-confirmed write.
            tools = [t for t in tools if t["function"]["name"] not in _SIDE_EFFECTING_TOOLS]

        # Per-turn memo of side-effecting tool calls (keyed by name + args).
        # If the model re-requests the same write across loop iterations — the
        # mechanism behind the duplicate-work-order report — we reuse the first
        # result instead of executing the side effect again. Read-only tools
        # are never memoised; they may legitimately be re-issued. Seeded with
        # the just-confirmed write on the two-turn narration re-entry.
        executed_side_effects: dict[str, Any] = dict(seed_side_effects or {})

        # Per-turn set of declined proposal keys (same canonical key as the
        # memo). A chatty model that re-proposes a write the user already
        # declined this turn is auto-declined WITHOUT re-prompting, so it
        # cannot ratchet repeated [y/N] prompts up to max_iterations. A
        # genuinely different proposal still prompts.
        declined_side_effects: set[str] = set()

        for _iteration in range(max_iterations):
            with self.tracer.trace(
                "llm_call",
                operation="complete_with_tools",
            ) as span:
                if tools:
                    result = await self._llm.complete_with_tools(messages, tools)
                else:
                    text = await self._llm.complete(messages)
                    return text

            content = result.get("content", "")
            tool_calls = result.get("tool_calls")

            if not tool_calls:
                return content or ""

            # Process tool calls
            span.output_summary = f"{len(tool_calls)} tool calls"
            messages.append(
                {
                    "role": "assistant",
                    "content": content or "",
                    "tool_calls": tool_calls,
                }
            )

            for tc in tool_calls:
                func_name = tc.function.name
                try:
                    args = json.loads(tc.function.arguments)
                except (json.JSONDecodeError, AttributeError):
                    args = {}

                memo_key: str | None = None
                if func_name in _SIDE_EFFECTING_TOOLS:
                    memo_key = f"{func_name}:{json.dumps(args, sort_keys=True, default=str)}"

                # The confirmation gate applies only to mutating tools and only
                # when confirmations are on AND we are not in sandbox (sandbox
                # already no-ops the write — confirming a no-op would mislead).
                gate_write = memo_key is not None and self._confirmations and not self.sandbox

                with self.tracer.trace(
                    "tool_call",
                    operation=func_name,
                ) as tool_span:
                    if memo_key is not None and memo_key in executed_side_effects:
                        tool_result = executed_side_effects[memo_key]
                        logger.info(
                            "duplicate_tool_call_suppressed",
                            agent=self.name,
                            tool=func_name,
                            operation=func_name,
                        )
                    elif gate_write and memo_key in declined_side_effects:
                        # Same proposal the user already declined this turn:
                        # auto-decline without re-prompting (anti-friction).
                        tool_result = {"declined": True, "tool": func_name}
                        logger.info(
                            "write_auto_declined",
                            agent=self.name,
                            tool=func_name,
                            operation=func_name,
                        )
                    elif gate_write and confirmer is not None:
                        # Synchronous path (e.g. CLI): ask, then act on yes.
                        approved = await confirmer(self._confirmation_prompt(func_name, args))
                        if approved:
                            tool_result = await self._execute_tool(
                                func_name, args, chat_id=chat_id
                            )
                            is_error = isinstance(tool_result, dict) and "error" in tool_result
                            if memo_key is not None and not is_error:
                                executed_side_effects[memo_key] = tool_result
                        else:
                            tool_result = {"declined": True, "tool": func_name}
                            if memo_key is not None:
                                declined_side_effects.add(memo_key)
                            logger.info(
                                "write_declined",
                                agent=self.name,
                                tool=func_name,
                                operation=func_name,
                            )
                    elif gate_write:
                        # No synchronous primitive available. Do NOT execute.
                        tool_result = await self._await_write_confirmation(
                            func_name, args, chat_id, user_id
                        )
                    else:
                        tool_result = await self._execute_tool(func_name, args, chat_id=chat_id)
                        # Only memoise successful results. A failed side effect
                        # (e.g. a transient workflow error returned as
                        # {"error": ...}) must not suppress a legitimate retry
                        # of the same call later in the turn.
                        is_error = isinstance(tool_result, dict) and "error" in tool_result
                        if memo_key is not None and not is_error:
                            executed_side_effects[memo_key] = tool_result
                    tool_span.output_summary = str(tool_result)[:200]

                messages.append(
                    {
                        "role": "tool",
                        "content": json.dumps(tool_result, default=str),
                        "tool_call_id": tc.id,
                    }
                )

        # Exhausted iterations — get final response without tools
        return await self._llm.complete(messages)

    # ------------------------------------------------------------------
    # Confirmation gate helpers
    # ------------------------------------------------------------------

    def _confirmation_prompt(self, func_name: str, args: dict[str, Any]) -> str:
        """Build a concrete, human-readable description of a pending write (R6).

        Pure function of the tool name and its arguments — no I/O. The channel
        renders this text verbatim, so it must state exactly what will happen.

        Args:
            func_name: The mutating tool the model requested.
            args: The arguments the model supplied for the call.

        Returns:
            A one-paragraph confirmation question naming the concrete action.
        """
        if func_name == "create_work_order":
            asset = args.get("asset_id") or "(unspecified asset)"
            wo_type = args.get("type") or "corrective"
            priority = args.get("priority") or "medium"
            description = args.get("description") or "(no description)"
            return (
                "Create a work order?\n"
                f"  • Asset: {asset}\n"
                f"  • Type: {wo_type}\n"
                f"  • Priority: {priority}\n"
                f"  • Description: {description}"
            )

        if func_name == "execute_workflow":
            workflow = args.get("workflow_name") or "(unnamed workflow)"
            event = args.get("event")
            summary = self._summarize_event(event)
            return f"Run workflow {workflow!r}?\n  • Event: {summary}"

        # Generic fallback for any other (future) mutating tool — better a
        # weaker description than no gate. New write tools should add a branch
        # above so R6 stays concrete.
        rendered_args = ", ".join(f"{k}={v!r}" for k, v in args.items()) or "(no arguments)"
        return f"Execute {func_name}?\n  • Arguments: {rendered_args}"

    @staticmethod
    def _summarize_event(event: Any) -> str:
        """Summarise a workflow ``event`` payload for the confirmation prompt.

        Highlights the target asset and a few key fields; falls back to a
        plain marker when the payload is empty or not a mapping.
        """
        if not isinstance(event, dict) or not event:
            return "(no event payload)"
        parts: list[str] = []
        asset = event.get("asset_id") or event.get("asset")
        if asset:
            parts.append(f"asset={asset}")
        for key in ("alarm_id", "failure_mode", "priority", "severity", "type"):
            if event.get(key):
                parts.append(f"{key}={event[key]}")
        if not parts:
            # Show up to three arbitrary keys so the user sees something.
            parts = [f"{k}={v}" for k, v in list(event.items())[:3]]
        return ", ".join(parts)

    @staticmethod
    def _is_affirmation(text: str) -> bool:
        """Deterministically recognise a bare affirmation (NOT via the LLM).

        Returns ``True`` only when the WHOLE message — after strip + lowercase
        — is a single recognised affirmation token (English or Italian). A
        compound message such as ``"ok, but set priority high"`` is NOT an
        affirmation (it is treated as unrelated → cancel + process normally),
        so the gate is never bypassed by an ambiguous "yes …" prefix.

        Args:
            text: The raw incoming message text.

        Returns:
            ``True`` if the message is exactly one affirmation token.
        """
        return text.strip().lower() in _AFFIRMATION_TOKENS

    @staticmethod
    def _is_decline(text: str) -> bool:
        """Deterministically recognise a bare decline (NOT via the LLM).

        Mirror of :meth:`_is_affirmation` for decline tokens. Both a decline
        and any unrelated message clear the pending action; this helper exists
        for symmetry and clearer logging, not because the two branches differ
        in effect (both cancel).

        Args:
            text: The raw incoming message text.

        Returns:
            ``True`` if the message is exactly one decline token.
        """
        return text.strip().lower() in _DECLINE_TOKENS

    async def _await_write_confirmation(
        self,
        func_name: str,
        args: dict[str, Any],
        chat_id: str,
        user_id: str,
    ) -> dict[str, Any]:
        """Handle a gated write when no synchronous confirmer is available.

        The write is NOT executed. The proposed action is stored in the
        turn-surviving :attr:`_pending_actions` store keyed
        ``(chat_id, user_id)`` so the next inbound message for the same
        ``(chat_id, user_id)`` can confirm it (the two-turn degrade — see
        :meth:`handle_message_full`). A structured ``confirmation_required``
        result is returned so the turn ends with the confirmation question as
        the response and a programmatic caller never writes unconfirmed.

        A new proposal for an existing key replaces the stale one (natural with
        dict assignment): only one pending action lives per ``(chat_id,
        user_id)`` at a time.

        Args:
            func_name: The mutating tool that was gated.
            args: The arguments the model supplied.
            chat_id: Conversation identifier (part of the pending key).
            user_id: Sender identifier (part of the pending key). When empty
                (CLI uses ``cli_user``; email derives a spoofable identity;
                anonymous channels emit ``""``), the per-user guarantee
                degrades to chat-level — this is logged, not silently treated
                as a per-user confirmation.

        Returns:
            A structured result the LLM loop feeds back as the tool result.
        """
        prompt = self._confirmation_prompt(func_name, args)
        if not user_id:
            # Explicit, documented degradation: the action is still stored and
            # confirmable, but under the (chat_id, "") key — anyone on the chat
            # can confirm it. Do NOT silently pretend it is per-user.
            logger.warning(
                "confirmation_chat_scoped",
                agent=self.name,
                tool=func_name,
                operation=func_name,
                chat_id=chat_id,
                reason="empty_user_id_degrades_to_chat_level",
            )
        self._pending_actions[(chat_id, user_id)] = (func_name, dict(args), prompt)
        logger.info(
            "write_confirmation_required",
            agent=self.name,
            tool=func_name,
            operation=func_name,
            chat_id=chat_id,
        )
        return {
            "confirmation_required": True,
            "tool": func_name,
            "prompt": prompt,
        }

    async def _resume_pending_action(
        self,
        pending: tuple[str, dict[str, Any], str],
        text: str,
        *,
        chat_id: str,
        user_id: str,
    ) -> AgentResponse | None:
        """Resume (or cancel) a pending two-turn write based on this message.

        Called at the top of :meth:`handle_message_full` when a pending action
        exists for ``(chat_id, user_id)``. The decision is deterministic — the
        LLM is never asked to interpret the confirmation.

        * **Affirmation** (the whole message is one yes-token): pop the pending
          action, execute it via :meth:`_execute_tool` (so the connector's own
          ``@sandbox_aware`` check still applies if state changed since the
          proposal), then re-enter :meth:`_llm_loop` in **narration-only** mode
          so the model narrates the outcome and emits citations instead of
          returning a raw payload. Returns the narrated :class:`AgentResponse`.
        * **Anything else** (a decline OR an unrelated message): pop/clear the
          pending action and return ``None`` so the caller falls through to
          normal processing of this message (an unrelated message never
          silently executes the pending write).

        Args:
            pending: The stored ``(func_name, args, prompt)`` tuple.
            text: The raw incoming message.
            chat_id: Conversation identifier.
            user_id: Sender identifier.

        Returns:
            The narrated response when the write was confirmed and executed;
            ``None`` when the pending action was cancelled (caller proceeds
            with normal processing).
        """
        func_name, args, _prompt = pending
        if not self._is_affirmation(text):
            # Decline or unrelated: cancel and let the caller process normally.
            self._pending_actions.pop((chat_id, user_id), None)
            logger.info(
                "pending_write_cancelled",
                agent=self.name,
                tool=func_name,
                operation=func_name,
                chat_id=chat_id,
                declined=self._is_decline(text),
            )
            return None

        # Affirmation: pop FIRST so a re-entrant failure cannot leave a
        # confirmable ghost, then execute the write through the normal tool
        # path (the connector's sandbox check still applies if state changed).
        self._pending_actions.pop((chat_id, user_id), None)
        logger.info(
            "pending_write_confirmed",
            agent=self.name,
            tool=func_name,
            operation=func_name,
            chat_id=chat_id,
        )

        # Fresh per-turn citation state for the narration pass.
        self._turn_chunks[chat_id] = {}
        self._turn_ordered[chat_id] = []
        try:
            tool_result = await self._execute_tool(func_name, args, chat_id=chat_id)

            # Re-enter the loop in narration-only mode: mutating tools are
            # suppressed AND the just-executed (func_name, args) is seeded into
            # the memo, so an eager model that re-issues the write — verbatim or
            # reworded — can neither ratchet a second confirmation nor write a
            # duplicate. The re-entry carries NO confirmer.
            memo_key = f"{func_name}:{json.dumps(args, sort_keys=True, default=str)}"
            messages: list[dict[str, Any]] = [
                {
                    "role": "system",
                    "content": self._build_messages(text, chat_id, {})[0]["content"],
                },
                {"role": "user", "content": text},
                {
                    "role": "assistant",
                    "content": (
                        f"The confirmed action {func_name!r} has been executed. "
                        "Summarise the outcome for the user and cite any sources."
                    ),
                },
                {"role": "tool", "content": json.dumps(tool_result, default=str)},
            ]
            raw_response = await self._llm_loop(
                messages,
                chat_id,
                max_iterations=2,
                narration_only=True,
                seed_side_effects={memo_key: tool_result},
            )
            rendered, citations = parse_response(
                raw_response,
                self._turn_chunks.get(chat_id, {}),
                self._turn_ordered.get(chat_id, []),
            )
            self._add_to_history(chat_id, "user", text)
            self._add_to_history(chat_id, "assistant", rendered)
        finally:
            self._turn_chunks.pop(chat_id, None)
            self._turn_ordered.pop(chat_id, None)

        logger.info(
            "response_generated",
            agent=self.name,
            chat_id=chat_id,
            response_length=len(rendered),
            citation_count=len(citations),
        )
        return AgentResponse(text=rendered, citations=citations)

    async def _execute_tool(
        self,
        name: str,
        args: dict[str, Any],
        *,
        chat_id: str = "default",
    ) -> Any:
        """Execute a tool call by dispatching to the appropriate connector.

        ``chat_id`` scopes any side effects that touch per-turn state
        (currently the citation chunk registry) so concurrent chats stay
        isolated.
        """
        logger.debug("executing_tool", tool=name, args=args)

        if name == "search_assets":
            return self._tool_search_assets(args.get("query", ""))

        if name == "get_asset_details":
            return self._tool_get_asset_details(args.get("asset_id", ""))

        if name == "read_work_orders":
            connectors = self._registry.find_by_capability(Capability.READ_WORK_ORDERS)
            if connectors:
                _, conn = connectors[0]
                wos = await conn.read_work_orders(  # type: ignore[attr-defined]
                    asset_id=args.get("asset_id", ""),
                    status=args.get("status", ""),
                )
                return [wo.model_dump(mode="json") for wo in wos]
            return {"error": "No CMMS connector available"}

        if name == "create_work_order":
            return await self._tool_create_work_order(args)

        if name == "search_documents":
            connectors = self._registry.find_by_capability(Capability.SEARCH_DOCUMENTS)
            if connectors:
                _, conn = connectors[0]
                raw_filters = args.get("filters")
                filters = raw_filters if isinstance(raw_filters, dict) else None
                results = await conn.search(  # type: ignore[attr-defined]
                    args.get("query", ""),
                    asset_id=args.get("asset_id", ""),
                    filters=filters,
                )
                # Sanitise source at the LLM boundary — the tool result is
                # serialised straight into the conversation history.  The
                # citation fields (chunk_id / section_title / is_table) come
                # from the v0.3 RAG upgrade and feed citation validation.
                # Surface a visible ``citation_index`` on the tool result so
                # the model can cite tool-retrieved chunks by ``[n]`` — the
                # same index contract the pre-fetch context uses. The index
                # is offset by any chunks already displayed this turn (e.g.
                # from pre-fetch context) so it matches the ordered map
                # _register_document_results builds. Only the first five are
                # indexed, mirroring format_document_results' ``[:5]``.
                offset = len(self._turn_ordered.get(chat_id, []))
                serialized = [
                    {
                        "citation_index": offset + i,
                        "content": safe_text(r.content),
                        "source": safe_source(r.source),
                        "page": r.page,
                        "chunk_id": getattr(r, "chunk_id", ""),
                        "section_title": getattr(r, "section_title", ""),
                        "is_table": getattr(r, "is_table", False),
                    }
                    for i, r in enumerate(results[:5], 1)
                ]
                # Register tool-retrieved chunks against the in-flight chat
                # only, so concurrent chats do not see each other's chunks
                # when citation parsing validates references later.
                self._register_document_results(chat_id, serialized)
                return serialized
            return {"error": "No document connector available"}

        if name == "check_spare_parts":
            connectors = self._registry.find_by_capability(Capability.READ_SPARE_PARTS)
            if connectors:
                _, conn = connectors[0]
                parts = await conn.read_spare_parts(  # type: ignore[attr-defined]
                    asset_id=args.get("asset_id", ""),
                    sku=args.get("sku", ""),
                )
                return [p.model_dump(mode="json") for p in parts]
            return {"error": "No spare parts connector available"}

        if name == "diagnose_failure":
            return self._tool_diagnose_failure(
                args.get("asset_id", ""),
                args.get("symptoms", []),
            )

        if name == "get_maintenance_schedule":
            return {"info": "Maintenance schedule lookup not yet connected to a data source."}

        if name == "execute_workflow":
            return await self._tool_execute_workflow(
                args.get("workflow_name", ""),
                args.get("event"),
            )

        return {"error": f"Unknown tool: {name}"}

    # ------------------------------------------------------------------
    # Tool implementations
    # ------------------------------------------------------------------

    def _tool_search_assets(self, query: str) -> list[dict[str, Any]]:
        """Search assets using the entity resolver."""
        resolved = self._resolver.resolve(query)
        return [
            {
                "id": r.asset.id,
                "name": r.asset.name,
                "type": r.asset.type.value,
                "location": r.asset.location,
                "criticality": r.asset.criticality.value,
                "confidence": r.confidence,
            }
            for r in resolved[:5]
        ]

    def _tool_get_asset_details(self, asset_id: str) -> dict[str, Any]:
        """Get full asset details."""
        try:
            asset = self.plant.get_asset(asset_id)
            return asset.model_dump(mode="json")
        except Exception:
            logger.warning(
                "asset_lookup_failed",
                agent=self.name,
                asset_id=asset_id,
                operation="get_asset_details",
            )
            return {"error": f"Asset {asset_id!r} not found"}

    async def _tool_create_work_order(self, args: dict[str, Any]) -> dict[str, Any]:
        """Create a work order via the CMMS connector."""
        if self.sandbox:
            logger.info(
                "sandbox_create_work_order",
                agent=self.name,
                args=args,
            )
            return {"sandbox": True, "action": "create_work_order", "args": args}

        from machina.domain.services.work_order_factory import auto_work_order_id
        from machina.domain.work_order import Priority, WorkOrder, WorkOrderType

        connectors = self._registry.find_by_capability(Capability.CREATE_WORK_ORDER)
        if not connectors:
            return {"error": "No CMMS connector available for creating work orders"}

        _, conn = connectors[0]
        wo_type = args.get("type", "corrective")
        priority = args.get("priority", "medium")
        asset_id = args.get("asset_id", "")
        description = args.get("description", "")
        # Deterministic, content-based ID (shared with WorkOrderFactory) so a
        # model that re-requests this tool inside the LLM loop collapses to a
        # single work order instead of creating one per call. The old
        # ``id(args) % 10000`` scheme used the memory address of a per-call
        # dict — non-deterministic, dedup-proof, prone to cross-turn collisions.
        wo = WorkOrder(
            id=auto_work_order_id(asset_id, wo_type, priority, description),
            type=WorkOrderType(wo_type),
            priority=Priority(priority),
            asset_id=asset_id,
            description=description,
        )
        created = await conn.create_work_order(wo)  # type: ignore[attr-defined]
        logger.info(
            "work_order_created",
            agent=self.name,
            work_order_id=created.id,
            asset_id=created.asset_id,
        )
        return created.model_dump(mode="json")  # type: ignore[no-any-return]

    def _tool_diagnose_failure(
        self,
        asset_id: str,
        symptoms: list[str],
    ) -> dict[str, Any]:
        """Diagnose failure using the domain service."""
        from machina.domain.alarm import Alarm, Severity

        # Convert symptom strings to pseudo-alarms for the analyzer
        alarms = [
            Alarm(
                id=f"SYMPTOM-{i}",
                asset_id=asset_id,
                severity=Severity.WARNING,
                parameter=symptom,
                value=0.0,
                threshold=0.0,
                unit="",
            )
            for i, symptom in enumerate(symptoms)
        ]

        from machina.domain.services.failure_analyzer import FailureAnalyzer

        # Collect failure modes from asset
        try:
            asset = self.plant.get_asset(asset_id)
            # In a real implementation, we'd load failure modes from a registry
            analyzer = FailureAnalyzer()
            results = analyzer.diagnose(alarms)
            return {
                "asset_id": asset_id,
                "asset_name": asset.name,
                "symptoms": symptoms,
                "probable_failures": [
                    {"code": fm.code, "name": fm.name, "category": fm.category} for fm in results
                ],
            }
        except Exception:
            logger.warning(
                "diagnose_failure_failed",
                agent=self.name,
                asset_id=asset_id,
                operation="diagnose_failure",
                symptoms=symptoms,
            )
            return {
                "asset_id": asset_id,
                "symptoms": symptoms,
                "probable_failures": [],
                "note": "No failure mode data available for this asset.",
            }

    async def _tool_execute_workflow(
        self,
        workflow_name: str,
        event: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Execute a registered workflow and return results."""
        try:
            result = await self.trigger_workflow(workflow_name, event or {})
            return {
                "workflow_name": result.workflow_name,
                "success": result.success,
                "duration_ms": result.duration_ms,
                "steps": [
                    {
                        "step": sr.step_name,
                        "success": sr.success,
                        # Scrub user-home / UNC paths from step output and error
                        # text before it enters the LLM message history.
                        "output_summary": safe_text(str(sr.output)[:500]) if sr.output else None,
                        "error": safe_text(str(sr.error)) if sr.error else None,
                    }
                    for sr in result.step_results
                ],
            }
        except Exception as exc:
            logger.warning(
                "workflow_execution_failed",
                agent=self.name,
                operation="execute_workflow",
                workflow=workflow_name,
                error=str(exc),
            )
            return {"error": safe_text(str(exc)), "workflow_name": workflow_name}

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_available_tools(self) -> list[dict[str, Any]]:
        """Return the tool definitions relevant to configured connectors."""
        from machina.connectors.capabilities import Capability

        all_caps: set[Capability] = set()
        for _, conn in self._registry.all().items():
            all_caps.update(conn.capabilities)

        cap_to_tool: dict[Capability, list[str]] = {
            Capability.READ_ASSETS: ["search_assets", "get_asset_details"],
            Capability.READ_WORK_ORDERS: ["read_work_orders"],
            Capability.CREATE_WORK_ORDER: ["create_work_order"],
            Capability.SEARCH_DOCUMENTS: ["search_documents"],
            Capability.READ_SPARE_PARTS: ["check_spare_parts"],
        }

        enabled_tool_names: set[str] = set()
        for cap in all_caps:
            for tool_name in cap_to_tool.get(cap, []):
                enabled_tool_names.add(tool_name)

        # Always include diagnosis and schedule tools
        enabled_tool_names.add("diagnose_failure")
        enabled_tool_names.add("get_maintenance_schedule")

        # Include workflow tool only when workflows are registered
        if self._workflows:
            enabled_tool_names.add("execute_workflow")

        return [tool for tool in BUILTIN_TOOLS if tool["function"]["name"] in enabled_tool_names]

    def _add_to_history(self, chat_id: str, role: str, content: str) -> None:
        """Add a message to the conversation history."""
        if chat_id not in self._histories:
            self._histories[chat_id] = []
        self._histories[chat_id].append({"role": role, "content": content})
        # Trim to max length
        if len(self._histories[chat_id]) > self._max_history * 2:
            self._histories[chat_id] = self._histories[chat_id][-self._max_history * 2 :]
