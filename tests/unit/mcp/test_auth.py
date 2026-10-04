"""Tests for MCP auth — StaticBearerTokenVerifier and token loading."""

from __future__ import annotations

import json
import os
from unittest.mock import patch

import pytest

from machina.exceptions import ConnectorError
from machina.mcp.auth import StaticBearerTokenVerifier, load_tokens_from_env

# Environment-loaded tokens must be at least 32 characters.
TOKEN_A = "a" * 64
TOKEN_B = "b" * 64


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

    def test_a_config_object_is_not_a_token_map(self) -> None:
        """dict(config) would turn config field names into accepted tokens."""
        from machina.config.schema import MachinaConfig

        with pytest.raises(TypeError, match="token -> client_id"):
            StaticBearerTokenVerifier(MachinaConfig())  # type: ignore[arg-type]

    def test_non_string_entries_are_refused(self) -> None:
        with pytest.raises(TypeError):
            StaticBearerTokenVerifier({"tok": 1})  # type: ignore[dict-item]


class TestBuildVerifier:
    def test_static_verifier_as_token_verifier_class_is_refused(self) -> None:
        """Naming the built-in verifier must not start a server that accepts
        config field names (name, description, ...) as bearer tokens."""
        from machina.config.schema import MachinaConfig, McpConfig
        from machina.mcp.auth import build_verifier

        config = MachinaConfig(
            mcp=McpConfig(token_verifier_class="machina.mcp.auth.StaticBearerTokenVerifier")
        )
        env = {"MACHINA_MCP_TOKENS_JSON": "", "MACHINA_MCP_TOKENS": ""}
        with (
            patch.dict(os.environ, env, clear=False),
            pytest.raises(ConnectorError, match="built-in static verifier"),
        ):
            build_verifier(config)

    def test_verifier_outside_the_machina_namespace_is_refused(self) -> None:
        from machina.config.schema import MachinaConfig, McpConfig
        from machina.mcp.auth import build_verifier

        config = MachinaConfig(mcp=McpConfig(token_verifier_class="os.system"))
        with pytest.raises(ConnectorError, match="allowed namespace"):
            build_verifier(config)


class TestLoadTokensFromEnv:
    def test_json_env_var(self) -> None:
        env = {"MACHINA_MCP_TOKENS_JSON": json.dumps({TOKEN_A: "alice", TOKEN_B: "bob"})}
        with patch.dict(os.environ, env, clear=False):
            tokens = load_tokens_from_env()
        assert tokens == {TOKEN_A: "alice", TOKEN_B: "bob"}

    def test_legacy_csv_env_var(self) -> None:
        env = {"MACHINA_MCP_TOKENS": f"{TOKEN_A},{TOKEN_B}", "MACHINA_MCP_TOKENS_JSON": ""}
        with patch.dict(os.environ, env, clear=False):
            import warnings

            with warnings.catch_warnings(record=True) as w:
                warnings.simplefilter("always")
                tokens = load_tokens_from_env()
                assert any("deprecated" in str(warning.message).lower() for warning in w)
        assert tokens == {
            TOKEN_A: "machina-unattributed",
            TOKEN_B: "machina-unattributed",
        }

    def test_json_takes_precedence_over_legacy(self) -> None:
        env = {
            "MACHINA_MCP_TOKENS_JSON": json.dumps({TOKEN_A: "alice"}),
            "MACHINA_MCP_TOKENS": TOKEN_B,
        }
        with patch.dict(os.environ, env, clear=False):
            tokens = load_tokens_from_env()
        assert TOKEN_A in tokens
        assert TOKEN_B not in tokens

    @pytest.mark.parametrize("placeholder", ["your-token-here", "change-me-token", "x" * 31])
    def test_short_json_token_refused(self, placeholder: str) -> None:
        """A token shorter than 32 characters (e.g. a copied placeholder) is refused."""
        env = {"MACHINA_MCP_TOKENS_JSON": json.dumps({placeholder: "client"})}
        with (
            patch.dict(os.environ, env, clear=False),
            pytest.raises(ConnectorError, match="at least 32 characters") as excinfo,
        ):
            load_tokens_from_env()
        assert placeholder not in str(excinfo.value)  # never echo the secret

    def test_exactly_32_characters_is_accepted(self) -> None:
        token = "x" * 32
        env = {"MACHINA_MCP_TOKENS_JSON": json.dumps({token: "client"})}
        with patch.dict(os.environ, env, clear=False):
            assert load_tokens_from_env() == {token: "client"}

    def test_short_legacy_token_refused(self) -> None:
        env = {"MACHINA_MCP_TOKENS": f"{TOKEN_A},short", "MACHINA_MCP_TOKENS_JSON": ""}
        with (
            patch.dict(os.environ, env, clear=False),
            pytest.warns(DeprecationWarning),
            pytest.raises(ConnectorError, match="at least 32 characters"),
        ):
            load_tokens_from_env()

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

        env = {"MACHINA_MCP_TOKENS_JSON": json.dumps({TOKEN_A: "test-user"})}
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

    @pytest.mark.asyncio
    async def test_authenticated_health_reports_package_version(self) -> None:
        """The detailed payload carries the installed package version, not a literal."""
        from types import SimpleNamespace

        import machina
        from machina.mcp.server import health_app

        responses: list[dict] = []

        async def mock_send(msg: dict) -> None:
            responses.append(msg)

        app = SimpleNamespace(
            _runtime_ref=SimpleNamespace(connectors={"cmms": object()}, sandbox_mode=True),
            _token_verifier=StaticBearerTokenVerifier({TOKEN_A: "alice"}),
        )
        scope = {
            "type": "http",
            "path": "/health",
            "app": app,
            "headers": [(b"authorization", f"Bearer {TOKEN_A}".encode())],
        }
        await health_app(scope, None, mock_send)

        body = json.loads(responses[1]["body"])
        assert body["version"] == machina.__version__
        assert body["connectors"] == ["cmms"]
        assert body["sandbox_mode"] is True

    @pytest.mark.asyncio
    async def test_health_with_invalid_token_stays_minimal(self) -> None:
        from types import SimpleNamespace

        from machina.mcp.server import health_app

        responses: list[dict] = []

        async def mock_send(msg: dict) -> None:
            responses.append(msg)

        app = SimpleNamespace(
            _runtime_ref=SimpleNamespace(connectors={"cmms": object()}, sandbox_mode=True),
            _token_verifier=StaticBearerTokenVerifier({TOKEN_A: "alice"}),
        )
        scope = {
            "type": "http",
            "path": "/health",
            "app": app,
            "headers": [(b"authorization", b"Bearer not-the-token")],
        }
        await health_app(scope, None, mock_send)

        assert json.loads(responses[1]["body"]) == {"status": "healthy"}
