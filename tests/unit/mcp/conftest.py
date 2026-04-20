"""Skip all MCP tests when the MCP SDK is not installed."""

import pytest

pytest.importorskip("mcp", reason="MCP SDK not installed (pip install machina-ai[mcp])")
