# Scaling

How to think about scaling Machina — and why the usual autoscaling playbook
does not apply to LLM-backed services.

## Why CPU-Based Autoscaling is Wrong for LLM Workloads

Traditional autoscaling (Kubernetes HPA, AWS Auto Scaling) watches CPU utilization:
when average CPU crosses a threshold, it adds replicas.

Machina's bottleneck is never CPU. It is:

1. **LLM API latency and rate limits.** A single conversation turn blocks on an
   LLM round-trip (1–30 seconds depending on model and prompt length). The Machina
   process uses near-zero CPU while waiting.
2. **LLM API cost.** Each conversation turn costs $0.01–$0.50+ depending on the model.
   Adding replicas does not reduce per-request cost — it increases aggregate spend.
3. **CMMS API rate limits.** Many CMMS platforms (SAP, Maximo) enforce per-user or
   per-tenant rate limits that are not relieved by horizontal scaling.

Scaling on CPU utilization for an LLM-backed service means:

- **Scaling too late:** CPU stays low during the expensive LLM call. By the time CPU
  spikes (during response parsing/processing), the bottleneck has already passed.
- **Scaling too aggressively:** A brief CPU spike during document embedding can trigger
  unnecessary replicas that sit idle waiting on LLM APIs.

## What to Scale On Instead

### Cost Budgets

For agents you run, track LLM spend from the usage records in the action
traces (`usd_cost` per LLM call; see [Cost Tracking](../observability/cost.md)).
The agent does not tag records with a conversation ID in v0.4, so
per-conversation figures need your own grouping. Set budgets and alerts:

| Metric | Healthy range | Alert threshold |
|--------|--------------|-----------------|
| Median cost per conversation | $0.02–$0.10 | >$0.50 |
| P95 cost per conversation | $0.05–$0.30 | >$1.00 |
| Daily total LLM spend | Depends on volume | >budget ceiling |

When cost-per-conversation climbs, the right response is usually to optimize
prompts, switch to a cheaper model, or add caching — not to add replicas.

### Queue Depth / Request Concurrency

If Machina serves multiple concurrent MCP clients, track:

- **Active conversations:** How many MCP clients are mid-conversation?
- **Request queue depth:** How many requests are waiting for a free worker?
- **P95 response latency:** Are clients waiting too long?

Scale horizontally when queue depth consistently exceeds your target SLO,
not when CPU exceeds a threshold.

### Practical Scaling Tiers

| Concurrent users | Deployment | Notes |
|-----------------|------------|-------|
| 1–5 | Single instance (systemd or Docker) | Default. Async runtime handles concurrency within one process. |
| 5–20 | 2–3 instances behind a load balancer | The MCP server handles requests statelessly — any instance can serve any request. An agent keeps conversation context in memory, so give its channel session affinity. |
| 20+ | Orchestrated deployment (K8s, ECS) with queue-depth scaling | Monitor LLM rate limits as the real ceiling. Coordinate CMMS credentials across replicas. |

## Horizontal Scaling Considerations

Machina is stateless by design, which makes horizontal scaling straightforward:

- **No shared state:** Each instance maintains its own connector sessions.
  The MCP server keeps no per-client state; an agent's conversation history
  and pending confirmations live in its own memory.
- **CMMS credential sharing:** All replicas use the same CMMS credentials.
  Ensure the CMMS can handle concurrent sessions from the same service account.
- **Document store:** `DocumentStoreConnector` runs its vector index in-process
  and builds it when it connects, so each replica indexes its own copy of the
  documents. Mount the same document directory on every replica.
- **Trace files:** If you attach a JSONL exporter, give each replica its own
  trace directory and centralize the files via log shipping.

## Load Balancing

Any L7 load balancer works (nginx, Caddy, Envoy, ALB). Machina's streamable-http
transport uses standard HTTP/1.1 with streaming responses. The server checks the
`Host` header against `mcp.allowed_hosts`, so forward the client's host name
and list it in the config (see [MCP Auth](../mcp/auth.md#allowed-hosts-and-origins)).

```nginx
upstream machina {
    server 127.0.0.1:8000;
    server 127.0.0.1:8001;
}

server {
    listen 443 ssl;
    location / {
        proxy_pass http://machina;
        proxy_http_version 1.1;
        proxy_set_header Host $host;   # must be in mcp.allowed_hosts
        proxy_set_header Connection "";
        proxy_buffering off;           # stream responses as they arrive
        proxy_read_timeout 120s;       # slow CMMS calls
    }
}
```

## What This Document Intentionally Omits

Kubernetes manifests, Helm charts, and HPA configurations are **not included**.
Shipping generic K8s YAML that works for a demo but breaks under real load creates
a false sense of production-readiness. The scaling characteristics of LLM-backed
services are different enough from typical web services that K8s configuration
must be tuned to your specific workload, provider rate limits, and cost constraints.

K8s deployment examples may follow once there is production feedback from
on-premise deployments.
