"""MCP authentication — static bearer token verifier.

Implements the MCP SDK's ``TokenVerifier`` protocol for static bearer
tokens loaded from environment variables.  Each token maps to a
``client_id`` that is attached to the verified access token.

Token sources (checked in order):
1. ``MACHINA_MCP_TOKENS_JSON`` — JSON object ``{"<token>": "<client_id>"}``
2. ``MACHINA_MCP_TOKENS`` — comma-separated tokens (legacy; all get
   ``client_id="machina-unattributed"``; emits a deprecation warning)

Every token loaded from the environment must be at least
:data:`MIN_TOKEN_LENGTH` characters, so a copied placeholder from an example
``.env`` file cannot become a working credential. The verified ``client_id``
is attached to the request's access token; it is not yet written to traces,
logs or CMMS records.
"""

from __future__ import annotations

import json
import os
import warnings
from typing import Any

import structlog

from machina.exceptions import ConnectorError

logger = structlog.get_logger(__name__)

#: Minimum length of a bearer token loaded from the environment
#: (``openssl rand -hex 16`` yields exactly 32 characters).
MIN_TOKEN_LENGTH = 32


class StaticBearerTokenVerifier:
    """Verify bearer tokens against a static map.

    Implements the MCP SDK ``TokenVerifier`` protocol.

    Args:
        token_to_identity: Mapping of token strings to client identifiers.

    Raises:
        TypeError: If ``token_to_identity`` is not a ``dict`` of strings to
            strings — anything else (a config object, for instance) would
            turn arbitrary keys into accepted tokens.
    """

    def __init__(self, token_to_identity: dict[str, str]) -> None:
        if not isinstance(token_to_identity, dict) or not all(
            isinstance(token, str) and isinstance(client, str)
            for token, client in token_to_identity.items()
        ):
            raise TypeError("StaticBearerTokenVerifier needs a dict of token -> client_id strings")
        self._tokens = dict(token_to_identity)

    async def verify_token(self, token: str) -> Any | None:
        """Return an ``AccessToken`` if the token is valid, else ``None``."""
        client_id = self._tokens.get(token)
        if client_id is None:
            return None

        from mcp.server.auth.provider import (  # type: ignore[import-not-found,unused-ignore]
            AccessToken,
        )

        return AccessToken(
            token=token,
            client_id=client_id,
            scopes=["mcp:use"],
        )


def _require_strong_tokens(tokens: dict[str, str], source: str) -> None:
    """Refuse tokens shorter than :data:`MIN_TOKEN_LENGTH` (never echoing them)."""
    weak = sum(1 for token in tokens if len(token) < MIN_TOKEN_LENGTH)
    if weak:
        raise ConnectorError(
            f"{source}: {weak} token(s) shorter than the minimum — MCP bearer "
            f"tokens must be at least {MIN_TOKEN_LENGTH} characters "
            "(generate one with `openssl rand -hex 32`)"
        )


def load_tokens_from_env() -> dict[str, str]:
    """Load bearer tokens from environment variables.

    Returns:
        A ``{token: client_id}`` mapping.

    Raises:
        ConnectorError: If no tokens are configured, the JSON is invalid, or
            any token is shorter than :data:`MIN_TOKEN_LENGTH` characters.
    """
    json_raw = os.environ.get("MACHINA_MCP_TOKENS_JSON", "")
    if json_raw:
        try:
            tokens: dict[str, str] = json.loads(json_raw)
        except json.JSONDecodeError as exc:
            raise ConnectorError(f"MACHINA_MCP_TOKENS_JSON is not valid JSON: {exc}") from exc
        if not isinstance(tokens, dict) or not tokens:
            raise ConnectorError(
                "MACHINA_MCP_TOKENS_JSON must be a non-empty JSON object "
                '{"<token>": "<client_id>"}'
            )
        _require_strong_tokens(tokens, "MACHINA_MCP_TOKENS_JSON")
        logger.info("mcp_tokens_loaded", source="MACHINA_MCP_TOKENS_JSON", count=len(tokens))
        return tokens

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
        _require_strong_tokens(tokens, "MACHINA_MCP_TOKENS")
        logger.info("mcp_tokens_loaded", source="MACHINA_MCP_TOKENS", count=len(tokens))
        return tokens

    raise ConnectorError(
        "streamable-http requires authentication configuration — "
        "set MACHINA_MCP_TOKENS_JSON or configure a custom token_verifier_class"
    )


_VERIFIER_ALLOWED_PREFIXES = ("machina.",)


def build_verifier(config: Any) -> Any:
    """Build a token verifier from config or environment.

    If ``config.mcp.token_verifier_class`` is set, loads and instantiates
    that class instead of the default static verifier.  Only classes under
    the ``machina.`` namespace are permitted to prevent arbitrary code load.
    """
    verifier_class_path = getattr(getattr(config, "mcp", None), "token_verifier_class", "")
    if verifier_class_path:
        if not any(verifier_class_path.startswith(p) for p in _VERIFIER_ALLOWED_PREFIXES):
            raise ConnectorError(
                f"token_verifier_class must be under an allowed namespace "
                f"({_VERIFIER_ALLOWED_PREFIXES}), got: {verifier_class_path!r}"
            )
        from machina.runtime import _import_class

        cls = _import_class(verifier_class_path)
        if isinstance(cls, type) and issubclass(cls, StaticBearerTokenVerifier):
            # The static verifier takes its tokens from the environment; built
            # from the config it would accept config field names as tokens.
            raise ConnectorError(
                "token_verifier_class names the built-in static verifier — leave "
                "token_verifier_class empty and set MACHINA_MCP_TOKENS_JSON instead"
            )
        return cls(config)

    tokens = load_tokens_from_env()
    return StaticBearerTokenVerifier(tokens)
