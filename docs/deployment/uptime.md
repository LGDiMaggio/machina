# Uptime and Resilience

How Machina handles transient failures, restarts, and degraded environments.

## Design Principle: Stateless by Design

Machina does not persist state across restarts. All durable state lives in your
CMMS (work orders, maintenance plans, asset records) and in your documents,
which the document store re-indexes when it connects.

A restart loses agent conversation context — including a write still waiting
for the user's confirmation — but **not** maintenance data. This is a
deliberate design choice: it keeps Machina simple to operate and eliminates an
entire class of state-corruption bugs.

## Transient Failure Handling

### Vendor CMMS Connectors (HTTP)

The SAP PM, Maximo and UpKeep connectors send their HTTP calls through
`request_with_retry` (`machina.connectors.cmms.retry`):

- **Retried responses:** HTTP 429 (Too Many Requests) for every method — the
  server refused the request without processing it — and HTTP 503 (Service
  Unavailable) for idempotent methods only, because a gateway can answer 503
  after the backend already processed a POST or PATCH.
- **Retried network errors:** `TimeoutException`, `ConnectError`, `ReadError`,
  for idempotent methods only (GET, HEAD, OPTIONS, PUT, DELETE). A POST or
  PATCH fails fast, because a timeout after a successful create would
  otherwise produce a duplicate.
- **Strategy:** exponential backoff — `min(0.5 s × 2^attempt, 8 s)`, up to 3
  retries. A numeric `Retry-After` header on a retried 429 or 503 replaces the
  computed delay when it is 8 s or less. A longer one ends the retries at once
  and the connector raises its error, since a retry inside the server's window
  would most likely be refused again.
- **Other errors:** 4xx (except 429) and 5xx (except 503) return immediately —
  the connector raises its own exception.

The Generic CMMS connector's REST mode makes single attempts, without retries.

The retry window is short and applies to each HTTP request: about 3.5 seconds
of backoff (0.5 + 1 + 2 s) plus each attempt's own timeout. A server's
`Retry-After` can stretch the backoff to at most 24 seconds (three waits of
8 s). An operation that sends several requests, such as a paged read, gets a
window for each. If the CMMS stays down past the window, the operation fails
and the error reaches the agent or MCP client. Machina does not queue failed
writes.

A `Retry-After` longer than 8 s fails the request wherever it comes. A paged
read that runs into one fails as a whole. At startup, `Agent.start()` fails
when it meets one during a connection check or the initial asset load, and
the MCP server marks the connector as failed (see
[Health Endpoint](#health-endpoint)).

Work-order IDs are deterministic, but only some connectors use them to avoid
duplicates: the Excel/CSV and SQL connectors and the Generic CMMS connector's
local mode return the existing work order when its ID is already there, and
the Generic CMMS REST mode sends the ID to the backend (unless a
`reverse_fields` mapping leaves it out), which may or may not honor it. SAP PM, Maximo and UpKeep let the CMMS number new work orders, so a
create retried later can produce a second one — check for it before retrying.

### OPC-UA Connector

The OPC-UA connector (`machina.connectors.iot.opcua`) does **not**
auto-reconnect. If the OPC-UA server drops the connection:

1. `health_check()` reports it (it reads the `ServerStatus` node).
2. Active subscriptions are **not** re-established automatically.

To reconnect, the operator (or an orchestrator) must call `disconnect()` then
`connect()`, and re-create the subscriptions.

### MQTT Connector

The MQTT connector uses `aiomqtt`, which raises `MqttError` on disconnect.
Reconnection follows the same manual pattern as OPC-UA.

## Graceful Shutdown

On `SIGTERM` (systemd stop, Docker stop, Ctrl+C), the streamable-HTTP server:

1. stops accepting new connections;
2. lets in-flight requests finish (no timeout of its own — systemd and Docker
   kill the process after their stop timeouts, 90 s and 10 s by default);
3. runs `MachinaRuntime.disconnect_all()`, calling every connector's
   `disconnect()` in turn — an exception in one is suppressed so the others
   still disconnect;
4. exits.

```bash
# Graceful stop (sends SIGTERM)
sudo systemctl stop machina
```

## Behavior Matrix

What happens under every combination of degraded subsystems. "LLM" is the
model behind your agent, or behind the MCP client for the MCP server.

| CMMS | LLM | Sandbox | In-flight WF | Behavior |
|------|-----|---------|--------------|----------|
| :material-check: Up | :material-check: Up | Off | No | Normal operation. Reads and writes execute against live CMMS. |
| :material-check: Up | :material-check: Up | Off | Yes | Normal. Workflow steps execute, WOs created in CMMS. |
| :material-check: Up | :material-check: Up | **On** | No | Reads succeed. Writes are **logged but not executed**. |
| :material-check: Up | :material-check: Up | **On** | Yes | Workflow steps run. Write steps logged only. |
| :material-check: Up | :material-close: Down | Off | No | Agent cannot reason. MCP tools still callable directly by an MCP client. |
| :material-check: Up | :material-close: Down | Off | Yes | In-flight workflow halts at next LLM-dependent step. CMMS state unchanged for unexecuted steps. |
| :material-check: Up | :material-close: Down | **On** | No | Same as LLM-down + sandbox-off: tools callable, no agent reasoning. |
| :material-check: Up | :material-close: Down | **On** | Yes | Workflow halts at LLM step. No writes attempted (sandbox). |
| :material-close: Down | :material-check: Up | Off | No | CMMS reads/writes fail (after the retry window, where retries apply). Agent can still reason and report the outage. |
| :material-close: Down | :material-check: Up | Off | Yes | Workflow halts at CMMS-dependent step. Agent reports failure. |
| :material-close: Down | :material-check: Up | **On** | No | CMMS reads fail. Writes would be logged only anyway. Agent can reason about the outage. |
| :material-close: Down | :material-check: Up | **On** | Yes | Workflow halts at CMMS read step. Write steps would have been logged only. |
| :material-close: Down | :material-close: Down | Off | No | Fully degraded. MCP server still answers `/health`, but tool calls fail. |
| :material-close: Down | :material-close: Down | Off | Yes | Workflow halts. No state changes. |
| :material-close: Down | :material-close: Down | **On** | No | Fully degraded, sandbox active. Same as above — nothing to sandbox when nothing works. |
| :material-close: Down | :material-close: Down | **On** | Yes | Workflow halts. No state changes. Sandbox irrelevant. |

**Key takeaway:** Machina fails open for reads (returns errors to the caller)
and fails safe for writes in sandbox mode (logs but does not execute). Failures
are surfaced to the agent or MCP client, never swallowed.

## Health Endpoint

The MCP server exposes `GET /health` on the streamable-HTTP transport. It is a
liveness check: it answers while the process runs and does not probe the
connectors.

```bash
curl -f http://localhost:8000/health || echo "Machina is down"
# {"status":"healthy"}
```

With a valid bearer token it also reports the configured connector names, the
sandbox mode and the package version:

```json
{"status": "healthy", "connectors": ["cmms"], "sandbox_mode": true, "version": "0.4.0"}
```

A connector that failed to connect at startup is logged
(`runtime_connector_failed`, then `runtime_partial_startup`) and still listed;
the server keeps serving the healthy ones, and calls that reach the failed
connector return errors. Machina does not retry the connection: restart the
server once the backend is reachable again.

## Monitoring Recommendations

- **Log aggregation:** ship `/var/log/machina/machina.log` (or the container
  output) to your SIEM or log platform. Logs are structured (structlog) and
  machine-parseable.
- **Usage and cost:** for agents you run yourself, export action traces (token
  counts and `usd_cost` per LLM call) — see [Cost Tracking](../observability/cost.md).
- **Alerting:** alert on `systemctl is-active machina` returning `inactive` or
  on `/health` returning non-200 for more than 60 seconds.
