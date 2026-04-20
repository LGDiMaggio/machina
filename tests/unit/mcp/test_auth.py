"""Tests for MCP auth — StaticBearerTokenVerifier and token loading."""

from __future__ import annotations

import json
import os
from unittest.mock import patch

import pytest

from machina.exceptions import ConnectorError
from machina.mcp.auth import StaticBearerTokenVerifier, load_tokens_from_env


class TestStaticBearerTokenVerifier:
    @pytest.mark.asyncio
    async def test_valid_token_returns_access_token(self) -> None:
        verifier = StaticBearerTokenVerifier({"secret1": "alice", "secret2": "bob"})
        result = await verifier.verify_token("secret1")
        assert result is not None
        assert result.client_id == "alice"
        assert result.token == "secret1"
        assert "mcp:use" in result.scopes

    @pytest.mark.asyncio
    async def test_invalid_token_returns_none(self) -> None:
        verifier = StaticBearerTokenVerifier({"secret1": "alice"})
        result = await verifier.verify_token("wrongtoken")
        assert result is None

    @pytest.mark.asyncio
    async def test_empty_map(self) -> None:
        verifier = StaticBearerTokenVerifier({})
        result = await verifier.verify_token("anything")
        assert result is None

    @pytest.mark.asyncio
    async def test_multiple_tokens(self) -> None:
        verifier = StaticBearerTokenVerifier(
            {
                "tok-a": "service-a",
                "tok-b": "service-b",
            }
        )
        a = await verifier.verify_token("tok-a")
        b = await verifier.verify_token("tok-b")
        assert a is not None and a.client_id == "service-a"
        assert b is not None and b.client_id == "service-b"


class TestLoadTokensFromEnv:
    def test_json_env_var(self) -> None:
        env = {"MACHINA_MCP_TOKENS_JSON": '{"tok1": "alice", "tok2": "bob"}'}
        with patch.dict(os.environ, env, clear=False):
            tokens = load_tokens_from_env()
        assert tokens == {"tok1": "alice", "tok2": "bob"}

    def test_legacy_csv_env_var(self) -> None:
        env = {"MACHINA_MCP_TOKENS": "secret1,secret2", "MACHINA_MCP_TOKENS_JSON": ""}
        with patch.dict(os.environ, env, clear=False):
            import warnings

            with warnings.catch_warnings(record=True) as w:
                warnings.simplefilter("always")
                tokens = load_tokens_from_env()
                assert any("deprecated" in str(warning.message).lower() for warning in w)
        assert tokens == {
            "secret1": "machina-unattributed",
            "secret2": "machina-unattributed",
        }

    def test_json_takes_precedence_over_legacy(self) -> None:
        env = {
            "MACHINA_MCP_TOKENS_JSON": '{"tok1": "alice"}',
            "MACHINA_MCP_TOKENS": "legacy1,legacy2",
        }
        with patch.dict(os.environ, env, clear=False):
            tokens = load_tokens_from_env()
        assert "tok1" in tokens
        assert "legacy1" not in tokens

    def test_no_tokens_raises(self) -> None:
        env = {"MACHINA_MCP_TOKENS_JSON": "", "MACHINA_MCP_TOKENS": ""}
        with (
            patch.dict(os.environ, env, clear=False),
            pytest.raises(ConnectorError, match="streamable-http requires"),
        ):
            load_tokens_from_env()

    def test_invalid_json_raises(self) -> None:
        env = {"MACHINA_MCP_TOKENS_JSON": "not-json"}
        with (
            patch.dict(os.environ, env, clear=False),
            pytest.raises(ConnectorError, match="not valid JSON"),
        ):
            load_tokens_from_env()

    def test_empty_json_object_raises(self) -> None:
        env = {"MACHINA_MCP_TOKENS_JSON": "{}"}
        with (
            patch.dict(os.environ, env, clear=False),
            pytest.raises(ConnectorError, match="non-empty"),
        ):
            load_tokens_from_env()


class TestBuildServerWithAuth:
    def test_stdio_transport_no_auth_required(self) -> None:
        from machina.config.schema import MachinaConfig
        from machina.mcp.server import build_server

        config = MachinaConfig()
        server = build_server(config, transport="stdio")
        assert server.name == "machina"

    def test_http_transport_without_tokens_raises(self) -> None:
        from machina.config.schema import MachinaConfig
        from machina.mcp.server import build_server

        env = {"MACHINA_MCP_TOKENS_JSON": "", "MACHINA_MCP_TOKENS": ""}
        with patch.dict(os.environ, env, clear=False):
            config = MachinaConfig()
            with pytest.raises(ConnectorError, match="streamable-http requires"):
                build_server(config, transport="streamable-http")

    def test_http_transport_with_tokens_succeeds(self) -> None:
        from machina.config.schema import MachinaConfig
        from machina.mcp.server import build_server

        env = {"MACHINA_MCP_TOKENS_JSON": '{"test-token": "test-user"}'}
        with patch.dict(os.environ, env, clear=False):
            config = MachinaConfig()
            server = build_server(config, transport="streamable-http")
            assert server.name == "machina"


class TestHealthEndpoint:
    @pytest.mark.asyncio
    async def test_health_returns_200(self) -> None:
        from machina.mcp.server import health_app

        responses: list[dict] = []

        async def mock_receive() -> dict:
            return {"type": "http.request", "body": b""}

        async def mock_send(msg: dict) -> None:
            responses.append(msg)

        scope = {"type": "http", "path": "/health", "headers": []}
        await health_app(scope, mock_receive, mock_send)

        assert responses[0]["status"] == 200
        body = json.loads(responses[1]["body"])
        assert body["status"] == "healthy"

    @pytest.mark.asyncio
    async def test_health_404_for_other_paths(self) -> None:
        from machina.mcp.server import health_app

        responses: list[dict] = []

        async def mock_send(msg: dict) -> None:
            responses.append(msg)

        scope = {"type": "http", "path": "/other", "headers": []}
        await health_app(scope, None, mock_send)
        assert responses[0]["status"] == 404
