"""Tests for LLM tool definitions."""

from machina.llm.tools import BUILTIN_TOOLS, make_tool


class TestMakeTool:
    """Test the make_tool helper."""

    def test_basic_tool(self) -> None:
        tool = make_tool("test_tool", "A test tool")
        assert tool["type"] == "function"
        assert tool["function"]["name"] == "test_tool"
        assert tool["function"]["description"] == "A test tool"
        assert tool["function"]["parameters"]["type"] == "object"

    def test_tool_with_parameters(self) -> None:
        params = {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
            },
            "required": ["query"],
        }
        tool = make_tool("search", "Search for things", parameters=params)
        assert tool["function"]["parameters"] == params


class TestBuiltinTools:
    """Test that built-in tools are well-formed."""

    def test_all_tools_have_required_fields(self) -> None:
        for tool in BUILTIN_TOOLS:
            assert tool["type"] == "function"
            func = tool["function"]
            assert "name" in func
            assert "description" in func
            assert "parameters" in func
            assert isinstance(func["name"], str)
            assert len(func["name"]) > 0

    def test_expected_tools_present(self) -> None:
        names = {t["function"]["name"] for t in BUILTIN_TOOLS}
        expected = {
            "search_assets",
            "get_asset_details",
            "read_work_orders",
            "create_work_order",
            "search_documents",
            "check_spare_parts",
            "diagnose_failure",
            "get_maintenance_schedule",
        }
        assert expected == names

    def test_tool_count(self) -> None:
        assert len(BUILTIN_TOOLS) == 8
