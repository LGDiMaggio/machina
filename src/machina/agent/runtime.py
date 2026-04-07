"""Agent runtime — orchestrates LLM, connectors, and domain logic.

The :class:`Agent` is the central class of Machina.  It receives
messages (from Telegram, CLI, or programmatically), resolves entities,
gathers context from connectors, calls the LLM with domain-aware
prompts, and executes tool calls.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import structlog

from machina.agent.entity_resolver import EntityResolver
from machina.agent.prompts import build_context_message, build_system_prompt
from machina.connectors.base import ConnectorRegistry
from machina.domain.plant import Plant
from machina.exceptions import LLMError
from machina.llm.provider import LLMProvider
from machina.llm.tools import BUILTIN_TOOLS
from machina.observability.tracing import ActionTracer

logger = structlog.get_logger(__name__)


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
    ) -> None:
        self.name = name
        self.description = description
        self.plant = plant or Plant(name="Default")
        self._channels = channels or []
        self._max_history = max_history

        # LLM provider
        if isinstance(llm, str):
            self._llm = LLMProvider(model=llm, temperature=temperature)
        else:
            self._llm = llm

        # Connector registry
        self._registry = ConnectorRegistry()
        for i, conn in enumerate(connectors or []):
            cname = getattr(conn, "__class__", type(conn)).__name__
            self._registry.register(f"{cname}_{i}", conn)

        # Entity resolver
        self._resolver = EntityResolver(self.plant)

        # Action tracer
        self.tracer = ActionTracer()

        # Conversation history per chat
        self._histories: dict[str, list[dict[str, str]]] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Connect all connectors and load assets."""
        for name, conn in self._registry.all().items():
            with self.tracer.trace("connector_connect", connector=name):
                await conn.connect()
            logger.info("connector_ready", connector=name)

        # Auto-load assets from CMMS connectors
        cmms_connectors = self._registry.find_by_capability("read_assets")
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

        # Connect channels
        for channel in self._channels:
            await channel.connect()

        logger.info(
            "agent_started",
            agent=self.name,
            asset_count=len(self.plant.assets),
            connectors=list(self._registry.all().keys()),
        )

    async def stop(self) -> None:
        """Disconnect all connectors and channels."""
        for channel in self._channels:
            await channel.disconnect()
        for _name, conn in self._registry.all().items():
            await conn.disconnect()
        logger.info("agent_stopped", agent=self.name)

    async def handle_message(self, text: str, *, chat_id: str = "default") -> str:
        """Process a user message and return the agent's response.

        This is the main entry point for programmatic usage.

        Args:
            text: The user's message.
            chat_id: Identifier for the conversation.

        Returns:
            The agent's response text.

        Raises:
            LLMError: If the underlying LLM call fails.
        """
        logger.info(
            "message_received",
            agent=self.name,
            chat_id=chat_id,
            message_preview=text[:100],
        )

        # 1. Entity resolution
        resolved = self._resolver.resolve(text)

        # 2. Gather context from connectors
        context_data = await self._gather_context(text, resolved)

        # 3. Build messages
        messages = self._build_messages(text, chat_id, context_data)

        # 4. Call LLM (with tool-calling loop)
        try:
            response = await self._llm_loop(messages, chat_id)
        except Exception as exc:
            logger.error(
                "llm_error",
                agent=self.name,
                error=str(exc),
            )
            raise LLMError(f"LLM call failed: {exc}") from exc

        # 5. Update history
        self._add_to_history(chat_id, "user", text)
        self._add_to_history(chat_id, "assistant", response)

        logger.info(
            "response_generated",
            agent=self.name,
            chat_id=chat_id,
            response_length=len(response),
        )
        return response

    def run(self) -> None:
        """Start the agent with all channels (blocking, sync wrapper).

        Connects connectors, loads assets, and starts listening on
        all configured channels.
        """
        asyncio.run(self._run_async())

    async def _run_async(self) -> None:
        """Async main loop — start agent and listen on channels."""
        await self.start()

        if not self._channels:
            logger.warning("no_channels", agent=self.name)
            return

        # Use the first channel for listen (typically Telegram or CLI)
        channel = self._channels[0]

        async def _handler(msg: Any) -> str:
            return await self.handle_message(msg.text, chat_id=msg.chat_id)

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

        wo_connectors = self._registry.find_by_capability("read_work_orders")
        if wo_connectors:
            wo_cname, wo_conn = wo_connectors[0]

            async def _get_wos(_cname: str = wo_cname, _conn: Any = wo_conn) -> list[Any]:
                with self.tracer.trace(
                    "connector_query",
                    connector=_cname,
                    asset_id=asset.id,
                    operation="read_work_orders",
                ):
                    return await _conn.read_work_orders(asset_id=asset.id)  # type: ignore[attr-defined, no-any-return]

            tasks.append(_get_wos())
            task_names.append("work_orders")

        sp_connectors = self._registry.find_by_capability("read_spare_parts")
        if sp_connectors:
            sp_cname, sp_conn = sp_connectors[0]

            async def _get_parts(_cname: str = sp_cname, _conn: Any = sp_conn) -> list[Any]:
                with self.tracer.trace(
                    "connector_query",
                    connector=_cname,
                    asset_id=asset.id,
                    operation="read_spare_parts",
                ):
                    return await _conn.read_spare_parts(asset_id=asset.id)  # type: ignore[attr-defined, no-any-return]

            tasks.append(_get_parts())
            task_names.append("spare_parts")

        # Document search
        doc_connectors = self._registry.find_by_capability("search_documents")
        if doc_connectors:
            doc_cname, doc_conn = doc_connectors[0]

            async def _search_docs(_cname: str = doc_cname, _conn: Any = doc_conn) -> list[Any]:
                with self.tracer.trace(
                    "connector_query",
                    connector=_cname,
                    asset_id=asset.id,
                    operation="search_documents",
                ):
                    results = await _conn.search(text, asset_id=asset.id)  # type: ignore[attr-defined]
                    return [
                        {
                            "content": r.content,
                            "source": r.source,
                            "page": r.page,
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

        return context

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
    ) -> str:
        """Call the LLM, execute tool calls, and return final response."""
        tools = self._get_available_tools()

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

                with self.tracer.trace(
                    "tool_call",
                    operation=func_name,
                ) as tool_span:
                    tool_result = await self._execute_tool(func_name, args)
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

    async def _execute_tool(
        self,
        name: str,
        args: dict[str, Any],
    ) -> Any:
        """Execute a tool call by dispatching to the appropriate connector."""
        logger.debug("executing_tool", tool=name, args=args)

        if name == "search_assets":
            return self._tool_search_assets(args.get("query", ""))

        if name == "get_asset_details":
            return self._tool_get_asset_details(args.get("asset_id", ""))

        if name == "read_work_orders":
            connectors = self._registry.find_by_capability("read_work_orders")
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
            connectors = self._registry.find_by_capability("search_documents")
            if connectors:
                _, conn = connectors[0]
                results = await conn.search(  # type: ignore[attr-defined]
                    args.get("query", ""),
                    asset_id=args.get("asset_id", ""),
                )
                return [
                    {"content": r.content, "source": r.source, "page": r.page} for r in results
                ]
            return {"error": "No document connector available"}

        if name == "check_spare_parts":
            connectors = self._registry.find_by_capability("read_spare_parts")
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
        from machina.domain.work_order import Priority, WorkOrder, WorkOrderType

        connectors = self._registry.find_by_capability("create_work_order")
        if not connectors:
            return {"error": "No CMMS connector available for creating work orders"}

        _, conn = connectors[0]
        wo = WorkOrder(
            id=f"WO-AUTO-{id(args) % 10000:04d}",
            type=WorkOrderType(args.get("type", "corrective")),
            priority=Priority(args.get("priority", "medium")),
            asset_id=args.get("asset_id", ""),
            description=args.get("description", ""),
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

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_available_tools(self) -> list[dict[str, Any]]:
        """Return the tool definitions relevant to configured connectors."""
        all_caps = set()
        for _, conn in self._registry.all().items():
            all_caps.update(conn.capabilities)

        # Map capabilities to tools
        cap_to_tool = {
            "read_assets": ["search_assets", "get_asset_details"],
            "read_work_orders": ["read_work_orders"],
            "create_work_order": ["create_work_order"],
            "search_documents": ["search_documents"],
            "read_spare_parts": ["check_spare_parts"],
        }

        enabled_tool_names: set[str] = set()
        for cap in all_caps:
            for tool_name in cap_to_tool.get(cap, []):
                enabled_tool_names.add(tool_name)

        # Always include diagnosis and schedule tools
        enabled_tool_names.add("diagnose_failure")
        enabled_tool_names.add("get_maintenance_schedule")

        return [tool for tool in BUILTIN_TOOLS if tool["function"]["name"] in enabled_tool_names]

    def _add_to_history(self, chat_id: str, role: str, content: str) -> None:
        """Add a message to the conversation history."""
        if chat_id not in self._histories:
            self._histories[chat_id] = []
        self._histories[chat_id].append({"role": role, "content": content})
        # Trim to max length
        if len(self._histories[chat_id]) > self._max_history * 2:
            self._histories[chat_id] = self._histories[chat_id][-self._max_history * 2 :]
