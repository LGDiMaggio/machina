# Uptime and Resilience

How Machina handles transient failures, restarts, and degraded environments.

## Design Principle: Stateless by Design

Machina does not persist state across restarts. All durable state lives in your CMMS
(work orders, maintenance plans, asset records) and your vector store (document embeddings).

A restart loses agent conversation context but **not** maintenance data.
This is a deliberate design choice — it keeps Machina simple to operate and
eliminates an entire class of state-corruption bugs.

## Transient Failure Handling

### CMMS Connector (HTTP)

All CMMS HTTP calls go through `request_with_retry` (`machina.connectors.cmms.retry`):

- **Retryable conditions:** HTTP 429 (Too Many Requests), HTTP 503 (Service Unavailable),
  `TimeoutException`, `ConnectError`, `ReadError`
- **Strategy:** Exponential backoff — `min(0.5s × 2^attempt, 8s)`, up to 3 retries
- **Retry-After:** Honored for 429 responses (numeric seconds)
- **Non-retryable errors:** 4xx (except 429) return immediately — the caller decides how to handle

If the CMMS is down for longer than the retry window (~15 seconds), the operation fails
and the agent reports the error to the user. Machina does not queue failed writes.

### OPC-UA Connector

The OPC-UA connector (`machina.connectors.iot.opcua`) does **not** auto-reconnect.
If the OPC-UA server drops the connection:

1. `health_check()` returns `False` (reads the `ServerStatus` node).
2. Active subscriptions are **not** re-established automatically.
3. The MCP server's `/health` endpoint reflects the degraded state.

To reconnect, the operator (or an orchestrator) must call `disconnect()` then `connect()`.
Subscriptions must be re-created after reconnection.

### MQTT Connector

The MQTT connector uses `aiomqtt`, which raises `MqttError` on disconnect.
Reconnection follows the same manual pattern as OPC-UA.

## Graceful Shutdown

On `SIGTERM` (systemd stop, Docker stop, Ctrl+C):

1. The MCP server stops accepting new requests.
2. In-flight MCP requests are given up to **30 seconds** to complete.
3. `MachinaRuntime.disconnect_all()` runs, closing every connector.
   Each connector's `disconnect()` is called independently — a failure in one
   does not block the others.
4. The process exits.

```bash
# Graceful stop (sends SIGTERM, waits 90s by default)
sudo systemctl stop machina

# Check for clean shutdown in logs
grep "disconnect_all" /var/log/machina/machina.log
```

## Behavior Matrix

What happens under every combination of degraded subsystems:

| CMMS | LLM | Sandbox | In-flight WF | Behavior |
|------|-----|---------|--------------|----------|
| :material-check: Up | :material-check: Up | Off | No | Normal operation. Reads and writes execute against live CMMS. |
| :material-check: Up | :material-check: Up | Off | Yes | Normal. Workflow steps execute, WOs created in CMMS. |
| :material-check: Up | :material-check: Up | **On** | No | Reads succeed. Writes are **logged but not executed**. |
| :material-check: Up | :material-check: Up | **On** | Yes | Workflow steps run. Write steps logged only. |
| :material-check: Up | :material-close: Down | Off | No | Agent cannot reason. MCP tools still callable directly by the MCP client. |
| :material-check: Up | :material-close: Down | Off | Yes | In-flight workflow halts at next LLM-dependent step. CMMS state unchanged for unexecuted steps. |
| :material-check: Up | :material-close: Down | **On** | No | Same as LLM-down + sandbox-off: tools callable, no agent reasoning. |
| :material-check: Up | :material-close: Down | **On** | Yes | Workflow halts at LLM step. No writes attempted (sandbox). |
| :material-close: Down | :material-check: Up | Off | No | CMMS reads/writes fail after retry window (~15s). Agent can still reason and report the outage. |
| :material-close: Down | :material-check: Up | Off | Yes | Workflow halts at CMMS-dependent step. Agent reports failure. |
| :material-close: Down | :material-check: Up | **On** | No | CMMS reads fail. Writes would be logged only anyway. Agent can reason about the outage. |
| :material-close: Down | :material-check: Up | **On** | Yes | Workflow halts at CMMS read step. Write steps would have been logged only. |
| :material-close: Down | :material-close: Down | Off | No | Fully degraded. MCP server responds to `/health` but all tool calls fail. |
| :material-close: Down | :material-close: Down | Off | Yes | Workflow halts. No state changes. |
| :material-close: Down | :material-close: Down | **On** | No | Fully degraded, sandbox active. Same as above — nothing to sandbox when nothing works. |
| :material-close: Down | :material-close: Down | **On** | Yes | Workflow halts. No state changes. Sandbox irrelevant. |

**Key takeaway:** Machina fails open for reads (returns errors to the caller) and
fails safe for writes in sandbox mode (logs but does not execute). There is no
silent data loss — every failure is surfaced to the MCP client.

## Health Endpoint

The MCP server exposes `GET /health` on the streamable-http transport:

```json
{
  "status": "healthy",
  "server": "machina",
  "transport": "streamable-http"
}
```

Use this for load-balancer health checks and monitoring:

```bash
curl -f http://localhost:8000/health || echo "Machina is down"
```

## Monitoring Recommendations

- **Log aggregation:** Ship `/var/log/machina/machina.log` to your SIEM or log platform.
  Logs are structured (structlog) and machine-parseable.
- **Trace analysis:** Action trace JSONL files include cost tracking (`llm_cost_usd`)
  and timing. Use them for cost dashboards and latency monitoring.
- **Alerting:** Alert on `systemctl is-active machina` returning `inactive` or
  on `/health` returning non-200 for >60 seconds.
