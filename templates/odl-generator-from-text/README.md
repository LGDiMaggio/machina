# OdL Generator from Text

A starter kit you copy, point at your own asset registry, and run. A
technician describes a problem in free text — Italian or English:

> *"pompa P-201 perde acqua, caldaia C-3 rumore anomalo, prego creare OdL"*
>
> *"pump P-201 leaking water, boiler C-3 abnormal noise, please create WO"*

> **Note:** *OdL* (*Ordine di Lavoro*) is Italian for Work Order. The template
> is named after its original use case but works in any language.

The agent:

1. Resolves the assets named in the message against the plant registry
   (a spreadsheet, `data/asset_registry.xlsx`)
2. Proposes one structured work order per asset (type, priority, description)
3. Asks you to confirm each one before it is written (live mode)
4. Appends the confirmed work orders to `data/workorders.xlsx`

Everything runs in **sandbox mode** by default: work orders are proposed and
logged, never written.

## Quick Start

### 1. Copy and install

```bash
cp -r templates/odl-generator-from-text my-odl-agent
cd my-odl-agent
pip install "machina-ai[excel,litellm,examples]"
cp .env.example .env
```

In `.env`, set the model and its key — for example `MACHINA_LLM_MODEL=openai/gpt-4o`
with `OPENAI_API_KEY`, or `MACHINA_LLM_MODEL=ollama/qwen3:8b` for a local model
served by [Ollama](https://ollama.com) (no key). Pick a model that supports tool
calling.

### 2. Run in sandbox

```bash
python agent.py --sandbox
```

Type a request at the prompt. The agent answers with the work orders it would
create; `data/workorders.xlsx` is not touched.

### 3. Go live

```bash
python agent.py --live
```

Before each work order is written, the agent shows it and asks for
confirmation (`[y/N]`). Confirmed work orders are appended to
`data/workorders.xlsx` (created on the first write).

## Sandbox and Live

| Step | Sandbox (default) | Live |
|------|-------------------|------|
| Resolve assets in the message | Yes | Yes |
| Propose work orders | Yes | Yes |
| Ask for confirmation | No — nothing is written | Yes, before every write |
| Write to `data/workorders.xlsx` | **No (logged only)** | **Yes, after confirmation** |

Mode precedence: `--live` / `--sandbox`, then `MACHINA_SANDBOX_MODE` in `.env`
(default `true`).

## How assets are matched

The resolver matches, in order:

1. The asset ID as a whole token (`P-201`; `P-2` does not match inside it)
2. The registered name, plus any curated aliases
3. The location
4. Plain keyword containment across the asset's fields

There is no typo tolerance or fuzzy matching: a word either occurs or it does
not. When a message could mean several assets, the agent asks which one; it
never writes a work order for an asset the message did not resolve to. To
teach the resolver the words your technicians use, add them to the asset's
`Alias` cell, separated by `;` (e.g. `pompa A;circuito A`).

## Your own data

`data/asset_registry.xlsx` is the sample registry (20 assets, Italian names;
`data/asset_registry_en.xlsx` has English names). It is generated from the
JSON files next to it by `python data/build_sample_registry.py`.

To use your registry, replace the spreadsheet — or point `config.yaml` at your
own `.xlsx` or `.csv` — and map its columns under
`connectors.registry.settings.asset_registry.columns`. Each entry maps a column
header to a Machina field; multi-valued cells (failure modes, aliases) are
`;`-separated. The work-order sheet is mapped the same way under `work_orders`.

## Substrates

| Substrate | Config | Notes |
|-----------|--------|-------|
| Spreadsheets (default) | `config.yaml` | Registry and work orders in `.xlsx`/`.csv` files |
| CMMS over REST | `config.cmms-rest.yaml` | `python agent.py --config config.cmms-rest.yaml`; set `MACHINA_CMMS_URL` and `MACHINA_CMMS_API_KEY` in `.env` |

`config.cmms-rest.yaml` speaks the Generic CMMS REST contract (`/health`,
`/assets`, `/work_orders`); the mock CMMS in `deploy/docker/mock-cmms` of the
Machina repository serves it for testing.

## Channels

The agent talks on the terminal by default (the CLI channel), which is also
where it asks for confirmation.

- **Telegram** — uncomment the block in `config.yaml`, set
  `MACHINA_TELEGRAM_BOT_TOKEN` in `.env`, and list the allowed chat IDs. On
  Telegram, a write is confirmed by replying to the agent's question.
- **Email** — supported by Machina, but the sender of an email is not
  authenticated: anyone can forge the `From` address. Use it for sandbox demos,
  or keep live writes on the CLI channel.

## Docker

```bash
cp .env.example .env    # then edit it
docker compose run --rm machina            # sandbox
docker compose run --rm machina --live     # live
```

The agent is interactive, so use `docker compose run` (not `up`). `./data` is
mounted, so the registry you edit and the work orders written live on the
host.

## File Structure

```
odl-generator-from-text/
├── agent.py                    # Entry point: python agent.py --sandbox | --live
├── config.yaml                 # Default config: spreadsheet substrate, CLI channel
├── config.cmms-rest.yaml       # Alternative: CMMS over REST
├── Dockerfile                  # Image for the interactive agent
├── docker-compose.yml          # docker compose run --rm machina
├── .env.example                # Environment variables, documented
├── data/
│   ├── asset_registry.xlsx     # Sample registry (Italian names)
│   ├── asset_registry_en.xlsx  # Sample registry (English names)
│   ├── asset_registry.json     # Source of the Italian registry
│   ├── asset_registry_en.json  # Source of the English registry
│   └── build_sample_registry.py
└── samples/messages/           # Sample requests (email and Telegram payloads)
```
