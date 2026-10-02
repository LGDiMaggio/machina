# MCP Authentication

The streamable-http transport requires bearer-token authentication on every
request, including `/mcp`. stdio mode has no authentication: the client that
launches the process owns it (single user, local process only).

## Static Bearer Tokens

The default verifier maps each token to a client identity, read from
environment variables when the server starts.

### Setup

Generate one token per MCP client:

```bash
openssl rand -hex 32
```

Set `MACHINA_MCP_TOKENS_JSON` to a JSON object of `{"<token>": "<client_id>"}`:

```bash
export MACHINA_MCP_TOKENS_JSON='{"<token-1>": "claude-desktop", "<token-2>": "cursor-ide"}'
```

Every token must be at least 32 characters; the server refuses to start
otherwise (the error counts the short tokens but never prints them), so a
placeholder copied from an example `.env` file cannot become a working
credential. The server also refuses to start on HTTP when no token source is
configured.

The `client_id` is attached to the verified access token of each request. It
is not yet written to traces, logs or CMMS records, so it does not give you a
per-client audit trail in v0.4.

### MCP Client Configuration

```json
{
  "mcpServers": {
    "machina": {
      "url": "http://localhost:8000/mcp",
      "headers": {
        "Authorization": "Bearer <token-1>"
      }
    }
  }
}
```

### Legacy Format

The older `MACHINA_MCP_TOKENS` variable accepts comma-separated tokens
without identities (it is read only when `MACHINA_MCP_TOKENS_JSON` is unset,
and it emits a deprecation warning):

```bash
export MACHINA_MCP_TOKENS=<token-1>,<token-2>
```

All of them get `client_id="machina-unattributed"`, and the same 32-character
minimum applies.

## Token Verification Flow

1. The client sends an `Authorization: Bearer <token>` header.
2. `StaticBearerTokenVerifier.verify_token()` looks the token up.
3. Found: it returns an `AccessToken` with the `client_id` and the
   `mcp:use` scope, which the server requires.
4. Not found: the request is rejected with `401 Unauthorized`.

Every valid token can call every registered tool, writes included; there are
no per-token permissions. Use `sandbox: true` in the config to make the whole
server read-only toward the CMMS.

## Allowed Hosts and Origins

The HTTP transport also checks the `Host` header (and the `Origin` header,
when present) to block DNS-rebinding attacks. The defaults accept only
loopback names (`localhost`, `127.0.0.1`, `[::1]`, with or without a port).
When clients reach the server under another name, list it in the config:

```yaml
mcp:
  allowed_hosts: ["mcp.plant.example", "mcp.plant.example:*"]
  allowed_origins: ["https://mcp.plant.example"]
```

An authenticated request to `/mcp` whose `Host` is not in the list gets
`421 Misdirected Request`; a disallowed `Origin` gets `403`. Behind a reverse
proxy, either forward the original `Host` header (and list it) or keep the
proxy's upstream host a loopback name.

## Custom Token Verifiers

`mcp.token_verifier_class` replaces the static verifier with a class of your
own:

```yaml
mcp:
  token_verifier_class: "machina.contrib.vault_auth.VaultTokenVerifier"
```

- The dotted path must start with `machina.`: the server refuses any other
  namespace, so a config file cannot load arbitrary code. In v0.4 the package
  ships only the static verifier, so a custom one means adding a module
  inside the `machina` package (for example in a fork or a vendored build).
- The class is constructed with the whole loaded configuration, `cls(config)`.
- It implements `async def verify_token(self, token: str) -> AccessToken | None`
  (the MCP SDK's `TokenVerifier` protocol) and must grant the `mcp:use`
  scope.

```python
from mcp.server.auth.provider import AccessToken


class VaultTokenVerifier:
    """Verify tokens against HashiCorp Vault (illustrative)."""

    def __init__(self, config):
        self._vault = make_vault_client(config)  # your client

    async def verify_token(self, token: str) -> AccessToken | None:
        identity = await self._vault.lookup(token)
        if identity is None:
            return None
        return AccessToken(token=token, client_id=identity["name"], scopes=["mcp:use"])
```

## Security Considerations

- **Rotate tokens** immediately if one is compromised; restart the server to
  pick up the new set.
- **Bind to localhost** (the default, `--host 127.0.0.1`) unless the server
  sits behind a TLS-terminating reverse proxy. Machina does not terminate TLS,
  and bearer tokens over plain HTTP are readable on the network.
- **One token per client**, so you can revoke a single client without
  rotating the others.
- See [Security](../deployment/security.md) for the full threat model.
