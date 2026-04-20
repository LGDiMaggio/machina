"""MCP authentication — static bearer token verifier.

Implements the MCP SDK's ``TokenVerifier`` protocol for static bearer
tokens loaded from environment variables.  Each token maps to a
``client_id`` so CMMS audit logs can attribute writes to a named identity.

Token sources (checked in order):
1. ``MACHINA_MCP_TOKENS_JSON`` — JSON object ``{"<token>": "<client_id>"}``
2. ``MACHINA_MCP_TOKENS`` — comma-separated tokens (legacy; all get
   ``client_id="machina-unattributed"``; emits a deprecation warning)
"""

from __future__ import annotations

import json
import os
import warnings
from typing import Any

import structlog

from machina.exceptions import ConnectorError

logger = structlog.get_logger(__name__)


class StaticBearerTokenVerifier:
    """Verify bearer tokens against a static map.

    Implements the MCP SDK ``TokenVerifier`` protocol.

    Args:
        token_to_identity: Mapping of token strings to client identifiers.
    """

    def __init__(self, token_to_identity: dict[str, str]) -> None:
        self._tokens = dict(token_to_identity)

    async def verify_token(self, token: str) -> Any | None:
        """Return an ``AccessToken`` if the token is valid, else ``None``."""
        client_id = self._tokens.get(token)
        if client_id is None:
            return None

        from mcp.server.auth.provider import AccessToken

        return AccessToken(
            token=token,
            client_id=client_id,
            scopes=["mcp:use"],
        )


def load_tokens_from_env() -> dict[str, str]:
    """Load bearer tokens from environment variables.

    Returns:
        A ``{token: client_id}`` mapping.

    Raises:
        ConnectorError: If no tokens are configured.
    """
    json_raw = os.environ.get("MACHINA_MCP_TOKENS_JSON", "")
    if json_raw:
        try:
            tokens: dict[str, str] = json.loads(json_raw)
            if not isinstance(tokens, dict) or not tokens:
                raise ConnectorError(
                    "MACHINA_MCP_TOKENS_JSON must be a non-empty JSON object "
                    '{"<token>": "<client_id>"}'
                )
            logger.info("mcp_tokens_loaded", source="MACHINA_MCP_TOKENS_JSON", count=len(tokens))
            return tokens
        except json.JSONDecodeError as exc:
            raise ConnectorError(f"MACHINA_MCP_TOKENS_JSON is not valid JSON: {exc}") from exc

    legacy_raw = os.environ.get("MACHINA_MCP_TOKENS", "")
    if legacy_raw:
        warnings.warn(
            "MACHINA_MCP_TOKENS (comma-separated) is deprecated. "
            "Use MACHINA_MCP_TOKENS_JSON for per-token client_id attribution.",
            DeprecationWarning,
            stacklevel=2,
        )
        token_list = [t.strip() for t in legacy_raw.split(",") if t.strip()]
        tokens = {t: "machina-unattributed" for t in token_list}
        logger.info("mcp_tokens_loaded", source="MACHINA_MCP_TOKENS", count=len(tokens))
        return tokens

    raise ConnectorError(
        "streamable-http requires authentication configuration — "
        "set MACHINA_MCP_TOKENS_JSON or configure a custom token_verifier_class"
    )


def build_verifier(config: Any) -> StaticBearerTokenVerifier:
    """Build a token verifier from config or environment.

    If ``config.mcp.token_verifier_class`` is set, loads and instantiates
    that class instead of the default static verifier.
    """
    verifier_class_path = getattr(getattr(config, "mcp", None), "token_verifier_class", "")
    if verifier_class_path:
        from machina.runtime import _import_class

        cls = _import_class(verifier_class_path)
        return cls(config)  # type: ignore[return-value]

    tokens = load_tokens_from_env()
    return StaticBearerTokenVerifier(tokens)
