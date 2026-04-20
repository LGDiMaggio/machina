# OdL Generator from Text

A technician sends a free-text message — via email or Telegram — describing a
maintenance issue in their language (Italian or English):

> *"pompa P-201 perde acqua, caldaia C-3 rumore anomalo, prego creare OdL"*

> **English:** *"pump P-201 leaking water, boiler C-3 abnormal noise, please create WO"*

> **Note:** *OdL* (*Ordine di Lavoro*) is Italian for Work Order. The template is named
> after its original Italian-market use case but works with any language.

The agent:

1. Parses the free-text message (tolerates typos, abbreviations, synonyms)
2. Resolves asset references against the plant registry
3. Creates structured Work Orders with inferred priority and failure mode
4. Writes them to the configured substrate (Excel or CMMS)
5. Replies to the technician with confirmation

**Time to first OdL: <15 minutes on a clean machine.**

## Quick Start

### 1. Clone and configure

```bash
cp -r templates/odl-generator-from-text my-odl-agent
cd my-odl-agent
cp .env.example .env
```

Edit `.env`:

- Set `OPENAI_API_KEY` (or `ANTHROPIC_API_KEY` and adjust `MACHINA_LLM_MODEL`)
- Leave `MACHINA_SANDBOX_MODE=true` for now
- Leave `MACHINA_SUBSTRATE=excel` (default)

### 2. Start

```bash
docker compose up
```

This starts:

- **Machina** agent on port 8000
- **MailHog** mock SMTP on port 1025 (web UI: http://localhost:8025)
- **ChromaDB** on port 8001

### 3. Send a test message

Using the sample email:

```bash
# Send via SMTP to the mock server
python -c "
import smtplib
from email.message import EmailMessage
msg = EmailMessage()
msg['From'] = 'mario.rossi@example.com'
msg['To'] = 'machina@example.com'
msg['Subject'] = 'Richiesta OdL'
msg.set_content('pompa P-201 perde acqua, caldaia C-3 rumore anomalo, prego creare OdL')
# English alternative:
# msg.set_content('pump P-201 leaking water, boiler C-3 abnormal noise, please create WO')
with smtplib.SMTP('localhost', 1025) as s:
    s.send_message(msg)
print('Sent!')
"
```

### 4. Observe the result

- **MailHog web UI** (http://localhost:8025): see the agent's reply
- **Traces**: `docker compose exec machina ls traces/` — JSONL action traces
- **Work orders**: check `data/workorders.xlsx` (sandbox mode: file unchanged)

### 5. Go live

When satisfied with sandbox behavior:

```bash
# In .env:
MACHINA_SANDBOX_MODE=false
```

```bash
docker compose restart machina
```

Send the same message again. Confirm rows appear in `data/workorders.xlsx`.

## Sandbox-First Rollout

The template defaults to `MACHINA_SANDBOX_MODE=true`:

| Step | Sandbox | Live |
|------|---------|------|
| Parse message | Yes | Yes |
| Resolve assets | Yes | Yes |
| Create WO objects | Yes | Yes |
| Write to substrate | **Logged, not executed** | **Executed** |
| Reply to technician | Prefixed `[SANDBOX]` | Normal confirmation |

Inspect traces with:

```bash
docker compose exec machina cat traces/*.jsonl | python -m json.tool
```

## Substrate Switch

### Excel (default)

Work orders are appended to `data/workorders.xlsx`. The asset registry is read
from `data/asset_registry.json`.

### GenericCmms YAML

Write work orders to a REST API using YAML schema mapping:

1. In `.env`, set `MACHINA_SUBSTRATE=generic_cmms_yaml`
2. In `config.yaml`, uncomment the `generic_cmms` connector block
3. Uncomment `mock-cmms` in `docker-compose.yml` (or point to your real CMMS)
4. Restart: `docker compose up -d`

The YAML mapping (`config/generic_cmms.yaml`) maps between Machina domain
entities and the CMMS REST API — no Python code required.

## Communication Channels

### Email (included)

The template uses MailHog as a mock SMTP/IMAP server. For production:

- Replace `MACHINA_SMTP_HOST` / `MACHINA_IMAP_HOST` with your mail server
- For Gmail: set `gmail_credentials_file` in config.yaml

### Telegram

1. Create a bot via [@BotFather](https://t.me/botfather)
2. Set `MACHINA_TELEGRAM_BOT_TOKEN` in `.env`
3. Set `MACHINA_TELEGRAM_CHAT_IDS` to your allowed chat IDs
4. Uncomment the Telegram channel in `config.yaml`

### WhatsApp (v0.3.1)

WhatsApp support lands in v0.3.1 once Meta approval completes.
Same `workflows/parse_message_to_wo.py` file — swap the comms connector,
no workflow changes.

## Asset Registry

The sample registry (`data/asset_registry.json`) contains 20 PMI-Italia-style
assets: pumps, boilers, motors, compressors, heat exchangers, etc.

Replace it with your own asset data. The entity resolver matches by:

1. Exact asset ID (e.g., "P-201")
2. Name keywords (e.g., "pompa centrifuga")
3. Location (e.g., "edificio A")
4. Fuzzy keyword match across all fields

The sample registry (`data/asset_registry.json`) uses Italian asset names (PMI-Italia style).
An English-named equivalent is available at `data/asset_registry_en.json`.

Two entity resolver prompts are available: `prompts/entity_resolver_it.txt` (Italian) and
`prompts/entity_resolver_en.txt` (English). Both add LLM-powered
fuzzy matching for typos, abbreviations, and synonyms.

## File Structure

```
odl-generator-from-text/
├── agent.py                     # Entry point (~20 lines)
├── config.yaml                  # Agent configuration
├── Dockerfile                   # Extends machina-ai:0.3.0
├── docker-compose.yml           # Machina + MailHog + ChromaDB
├── .env.example                 # All env vars with inline comments
├── .gitignore                   # Ignores .env, traces, workorder output
├── workflows/
│   └── parse_message_to_wo.py   # Message → Entity → WO → Reply
├── data/
│   ├── asset_registry.json      # 20 sample assets (PMI Italia style)
│   ├── workorders_blank.json    # Write target (empty)
│   └── asset_registry_en.json   # 20 sample assets (English names)
├── config/
│   └── generic_cmms.yaml        # YAML mapping for CMMS substrate
├── prompts/
│   ├── entity_resolver_it.txt   # Italian entity resolution prompt
│   └── entity_resolver_en.txt   # English entity resolution prompt
└── samples/messages/
    ├── sample_email_01.eml      # Sample email (2 assets, 2 problems)
    ├── sample_email_02.eml      # Sample email (1 asset, bearing issue)
    ├── sample_telegram_01.json  # Same as email 01, Telegram format
    ├── sample_telegram_02.json  # Typo test ("pompta P-201")
    ├── sample_email_01_en.eml      # English: pump leak + boiler noise
    ├── sample_email_02_en.eml      # English: conveyor motor overheating
    ├── sample_telegram_01_en.json  # English version of email 01
    └── sample_telegram_02_en.json  # English typo test ("pmp P-201")
```
