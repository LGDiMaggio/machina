# Machina Starter-Kit Templates

Copy-and-adapt templates for common maintenance AI use cases. Each template
is a self-contained directory with its own config, sample data, Dockerfile and
README.

## Available Templates

| Template | Description |
|----------|-------------|
| [`odl-generator-from-text`](odl-generator-from-text/) | Free-text request → structured work orders, on a spreadsheet (or REST CMMS) substrate |

## Templates vs Examples

- **Templates** (`templates/`) are starter kits: copy one, point it at your own
  data, and run it. They include sample data, a Dockerfile and documented
  configuration.
- **Examples** (`examples/`) are learning tools. They demonstrate specific
  features with minimal code and run directly with `python agent.py`.

## Getting Started

```bash
# 1. Copy a template
cp -r templates/odl-generator-from-text my-agent
cd my-agent

# 2. Configure
pip install "machina-ai[excel,litellm,examples]"
cp .env.example .env      # set your LLM model and key

# 3. Run in sandbox first
python agent.py --sandbox
```

Each template's README covers its data, channels and Docker usage.

## Sandbox-First Rollout

Every template starts in sandbox mode (`MACHINA_SANDBOX_MODE=true` by default):

1. The agent receives and processes requests normally
2. Work orders are proposed and validated
3. **Writes are logged but not executed** — nothing reaches your files or CMMS

Run with `--live` (or set `MACHINA_SANDBOX_MODE=false`) when you are ready;
the agent then asks for confirmation before each write.
