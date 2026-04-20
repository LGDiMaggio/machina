# MCP Authentication

The streamable-http transport requires bearer token authentication.
stdio mode has no authentication (single-user, local process only).

## Static Bearer Tokens

The default auth method. Tokens are mapped to client identities via environment variables.

### Setup

Generate one token per MCP client:

```bash
openssl rand -hex 32
```

Set the `MACHINA_MCP_TOKENS_JSON` environment variable:

```bash
export MACHINA_MCP_TOKENS_JSON='{"abc123...": "claude-desktop", "def456...": "cursor-ide"}'
```

Each key is a token, each value is a client identity string used in audit logs
and trace files.

### MCP Client Configuration

```json
{
  "mcpServers": {
    "machina": {
      "url": "http://localhost:8000",
      "headers": {
        "Authorization": "Bearer abc123..."
      }
    }
  }
}
```

### Legacy Format

The older `MACHINA_MCP_TOKENS` variable accepts comma-separated tokens
without client identities:

```bash
export MACHINA_MCP_TOKENS=token1,token2,token3
```

All tokens get `client_id="machina-unattributed"`. Use `MACHINA_MCP_TOKENS_JSON`
instead for proper audit trails.

## Token Verification Flow

1. Client sends `Authorization: Bearer <token>` header
2. `StaticBearerTokenVerifier.verify_token()` looks up the token
3. If found: returns `AccessToken` with `client_id` and `scopes=["mcp:use"]`
4. If not found: returns `None` → 401 Unauthorized

## Custom Token Verifiers

For production environments using Vault, Azure Key Vault, or an OAuth provider,
implement a custom verifier:

```python
class VaultTokenVerifier:
    """Example: verify tokens against HashiCorp Vault."""

    async def verify_token(self, token: str) -> AccessToken | None:
        # Look up token in Vault
        identity = await vault_client.lookup(token)
        if identity:
            return AccessToken(
                token=token,
                client_id=identity["name"],
                scopes=["mcp:use"],
            )
        return None
```

Set the verifier class in your config:

```yaml
mcp:
  token_verifier_class: "my_package.auth.VaultTokenVerifier"
```

## Security Considerations

- **Rotate tokens** immediately if compromised. Restart the server to pick up new tokens.
- **Bind to localhost** unless behind a TLS-terminating reverse proxy.
  Machina does not terminate TLS.
- **One token per client** — use `MACHINA_MCP_TOKENS_JSON` to track which
  client performed which action in trace files.
- See [Security](../deployment/security.md) for the full threat model.
