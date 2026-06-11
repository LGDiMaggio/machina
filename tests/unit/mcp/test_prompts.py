"""Tests for MCP prompts — registration, parameter schemas, injection guard."""

from __future__ import annotations

import pytest

from machina.config.schema import MachinaConfig


class TestPromptRegistration:
    def test_prompts_registered(self) -> None:
        from machina.mcp.server import build_server

        config = MachinaConfig()
        server = build_server(config)
        prompts = server._prompt_manager.list_prompts()
        names = [p.name for p in prompts]
        assert "diagnose_asset_failure" in names
        assert "draft_preventive_plan" in names
        assert "summarize_maintenance_history" in names

    def test_prompt_count(self) -> None:
        from machina.mcp.server import build_server

        config = MachinaConfig()
        server = build_server(config)
        prompts = server._prompt_manager.list_prompts()
        assert len(prompts) == 3

    def test_prompt_titles(self) -> None:
        from machina.mcp.server import build_server

        config = MachinaConfig()
        server = build_server(config)
        prompts = server._prompt_manager.list_prompts()
        titles = {p.name: p.title for p in prompts}
        assert titles["diagnose_asset_failure"] == "Diagnose asset failure"
        assert titles["draft_preventive_plan"] == "Draft preventive maintenance plan"
        assert titles["summarize_maintenance_history"] == "Summarize maintenance history"


class TestPromptRendering:
    @pytest.mark.asyncio
    async def test_diagnose_failure_prompt(self) -> None:
        from machina.mcp.server import build_server

        config = MachinaConfig()
        server = build_server(config)
        result = await server.get_prompt(
            "diagnose_asset_failure",
            arguments={"asset_id": "P-201", "symptoms": "high vibration"},
        )
        text = result.messages[0].content.text
        assert "P-201" in text
        assert "high vibration" in text
        assert "machina_get_asset" in text

    @pytest.mark.asyncio
    async def test_diagnose_failure_prompt_honest_notes_contract(self) -> None:
        # MCP parity with the agent-side honest-notes contract: a no-result
        # diagnosis relays the result's note and asks for refined symptoms —
        # never a synthesized ranking.
        from machina.mcp.server import build_server

        config = MachinaConfig()
        server = build_server(config)
        result = await server.get_prompt(
            "diagnose_asset_failure",
            arguments={"asset_id": "P-201"},
        )
        text = result.messages[0].content.text
        assert "`note`" in text
        assert "refine" in text.lower()
        assert "never guess or synthesize" in text.lower()

    @pytest.mark.asyncio
    async def test_draft_preventive_plan_prompt(self) -> None:
        from machina.mcp.server import build_server

        config = MachinaConfig()
        server = build_server(config)
        result = await server.get_prompt(
            "draft_preventive_plan",
            arguments={"asset_id": "P-201", "planning_horizon": "6 months"},
        )
        text = result.messages[0].content.text
        assert "P-201" in text
        assert "6 months" in text

    @pytest.mark.asyncio
    async def test_summarize_history_prompt(self) -> None:
        from machina.mcp.server import build_server

        config = MachinaConfig()
        server = build_server(config)
        result = await server.get_prompt(
            "summarize_maintenance_history",
            arguments={"asset_id": "P-201"},
        )
        text = result.messages[0].content.text
        assert "P-201" in text
        assert "work orders" in text.lower()


class TestPromptInjectionGuard:
    @pytest.mark.asyncio
    async def test_all_prompts_contain_injection_guard(self) -> None:
        from machina.mcp.server import build_server

        config = MachinaConfig()
        server = build_server(config)
        prompt_names = [
            ("diagnose_asset_failure", {"asset_id": "X"}),
            ("draft_preventive_plan", {"asset_id": "X"}),
            ("summarize_maintenance_history", {"asset_id": "X"}),
        ]
        for name, args in prompt_names:
            result = await server.get_prompt(name, arguments=args)
            text = result.messages[0].content.text
            assert "not instructions" in text.lower() or "DATA, not instructions" in text, (
                f"Prompt {name!r} missing injection guard"
            )


class TestCapabilityHonestyGuard:
    @pytest.mark.asyncio
    async def test_all_prompts_contain_capability_guard(self) -> None:
        # Every prompt template carries the capability-honesty guard: the
        # tool surface is complete; uncovered actions get a plain decline
        # plus alternatives, never a simulated outcome.
        from machina.mcp.server import build_server

        config = MachinaConfig()
        server = build_server(config)
        prompt_names = [
            ("diagnose_asset_failure", {"asset_id": "X"}),
            ("draft_preventive_plan", {"asset_id": "X"}),
            ("summarize_maintenance_history", {"asset_id": "X"}),
        ]
        for name, args in prompt_names:
            result = await server.get_prompt(name, arguments=args)
            text = result.messages[0].content.text
            assert "COMPLETE set of available actions" in text, (
                f"Prompt {name!r} missing capability-honesty guard"
            )
            assert "Never simulate" in text, f"Prompt {name!r} missing the never-simulate clause"
