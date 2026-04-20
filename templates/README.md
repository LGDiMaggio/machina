# Machina Starter-Kit Templates

Clone-and-go templates for common maintenance AI use cases.
Each template is a self-contained directory with its own Dockerfile,
docker-compose, config, and sample data.

## Available Templates

| Template | Description | Status |
|----------|-------------|--------|
| [`odl-generator-from-text`](odl-generator-from-text/) | Free-text message → Work Order creation (Italian, email + Telegram) | v0.3.0 |

## Templates vs Examples

- **Templates** (`templates/`) are production-ready starting points. Clone one, fill in
  `.env`, and `docker compose up`. They include Dockerfiles, sample data, and deployment configs.
- **Examples** (`examples/`) are learning tools. They demonstrate specific features with
  minimal code and run directly with `python agent.py`.

## Getting Started

```bash
# 1. Clone a template
cp -r templates/odl-generator-from-text my-agent
cd my-agent

# 2. Configure
cp .env.example .env
# Edit .env with your LLM key and CMMS credentials

# 3. Run in sandbox first
docker compose up

# 4. Test with a sample message
# (see the template's README for channel-specific instructions)
```

## Sandbox-First Rollout

Every template defaults to `MACHINA_SANDBOX_MODE=true`. This means:

1. The agent receives and processes messages normally
2. Work orders are synthesized and validated
3. **Writes are logged but not executed** — no data reaches the CMMS
4. The technician receives a reply prefixed with `[SANDBOX]`

Flip `MACHINA_SANDBOX_MODE=false` in `.env` when ready for live writes.

## Roadmap

Additional templates planned for v0.3.1 based on design-partner feedback:

- **technician-chatbot** — Interactive Q&A with RAG over manuals and maintenance history
- **predictive-workflow** — Sensor alarm → diagnosis → work order pipeline
