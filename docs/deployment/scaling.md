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

### Cost-per-Conversation Budgets

Track cumulative LLM spend per conversation using Machina's action traces
(`llm_cost_usd` field in JSONL traces). Set budgets and alerts:

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
| 5–20 | 2–3 instances behind a load balancer | Machina is stateless — any instance can serve any request. Use session affinity if conversation context matters. |
| 20+ | Orchestrated deployment (K8s, ECS) with queue-depth scaling | Monitor LLM rate limits as the real ceiling. Coordinate CMMS credentials across replicas. |

## Horizontal Scaling Considerations

Machina is stateless by design, which makes horizontal scaling straightforward:

- **No shared state:** Each instance maintains its own connector sessions.
  There is no in-memory state to synchronize between replicas.
- **CMMS credential sharing:** All replicas use the same CMMS credentials.
  Ensure the CMMS can handle concurrent sessions from the same service account.
- **ChromaDB:** If using RAG, all replicas should point to the same ChromaDB instance
  (not embedded mode). This is the default in the Docker Compose setup.
- **Trace files:** Each replica writes to its own trace directory. Centralize traces
  via log shipping or a shared volume.

## Load Balancing

Any L7 load balancer works (nginx, Caddy, Envoy, ALB). Machina's streamable-http
transport uses standard HTTP/1.1 with streaming responses.

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
        proxy_set_header Connection "";
        proxy_read_timeout 120s;  # LLM calls can be slow
    }
}
```

## What This Document Intentionally Omits

Kubernetes manifests, Helm charts, and HPA configurations are **not included**.
Shipping generic K8s YAML that works for a demo but breaks under real load creates
a false sense of production-readiness. The scaling characteristics of LLM-backed
services are different enough from typical web services that K8s configuration
must be tuned to your specific workload, provider rate limits, and cost constraints.

K8s deployment examples are planned for v0.3.1 after gathering production
feedback from initial on-premise deployments.
