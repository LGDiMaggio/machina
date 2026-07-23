# Template: OdL Generator from Text

The `odl-generator-from-text` template demonstrates Machina's core value proposition:
a technician sends a free-text message in Italian, and the agent creates structured
Work Orders automatically.

## Use Case

Italian PMI (small/medium enterprise) maintenance teams don't open the CMMS — they text.
This template turns that behavior into a feature:

1. Technician sends an email or Telegram message:
   *"pompa P-201 perde acqua, caldaia C-3 rumore anomalo, prego creare OdL"*
2. The agent parses Italian informal text (tolerates typos, abbreviations, synonyms)
3. Resolves asset references against the plant registry
4. Creates structured Work Orders with inferred priority and failure mode
5. Writes them to the configured substrate (Excel or REST CMMS)
6. Replies with confirmation on the same channel

## Architecture

```
Technician                    Machina Agent                    Substrate
  │                              │                               │
  │─── email / Telegram ────────▶│                               │
  │                              │── parse Italian text (LLM) ──▶│
  │                              │── resolve assets ────────────▶│
  │                              │── create Work Orders ────────▶│
  │                              │── write to Excel / CMMS ─────▶│
  │◀── reply with confirmation ──│                               │
```

## Substrate Options

| Substrate | Backend | Config key | Best for |
|-----------|---------|------------|----------|
| **Excel** (default) | `ExcelCsvConnector` — appends rows to `workorders.xlsx` | `MACHINA_SUBSTRATE=excel` | Quick demos, small teams without a CMMS |
| **GenericCmms YAML** | `GenericCmmsConnector` — maps to any REST API via YAML | `MACHINA_SUBSTRATE=generic_cmms_yaml` | Teams with an existing CMMS |

Both substrates use the same workflow. Switching requires only an env var change
and a config.yaml comment toggle.

## Communication Channels

| Channel | Status | Notes |
|---------|--------|-------|
| Email (SMTP/IMAP) | v0.3.0 | Included. MailHog mock for dev. |
| Telegram | v0.3.0 | Included. Requires bot token. |
| WhatsApp | v0.3.1 | Deferred pending Meta approval. Same workflow file. |

## Getting Started

See the [template README](https://github.com/LGDiMaggio/machina/tree/main/templates/odl-generator-from-text)
for step-by-step setup instructions. Time to first OdL: <15 minutes.

## Italian Entity Resolution

Resolution is rule-based. `EntityResolver` matches, in order:

1. **Exact asset ID** as a whole token — "pompa P-201 perde" → `P-201`
2. **Registered name, and any curated `Asset.aliases`** — the plant's own word
   for the machine, at the same authority as its registered name
3. **Location overlap** — "pompa acqua reparto B" narrows by location
4. **Verbatim keyword containment** across the asset's fields

**Synonyms** are what `aliases` is for. Add the words your technicians actually
use to your asset data and they resolve like the registered name:

```json
{
  "id": "C-3",
  "name": "Caldaia a Vapore",
  "type": "static_equipment",
  "aliases": ["boiler", "caldaia vapore", "la grande"]
}
```

The key works in `data/asset_registry.json`, in a `;`-delimited `aliases`
column on the Excel/SQL/CMMS substrates, and anywhere `Asset` is constructed.
Aliases are language-neutral strings — an Italian and an English alias are
both just entries in the list — and they name the asset, never describe its
condition.

**Typos and abbreviations are not handled.** "pompta" does not resolve to
"pompa": there is no edit-distance or fuzzy matching anywhere in the cascade,
only verbatim containment. `prompts/entity_resolver_{it,en}.txt` ship with the
template and describe an LLM-assisted resolution layer, but **no code loads
them** — they are a design sketch, not a wired component. Treat them as such
until that changes. If a spelling recurs on your site, the durable fix is to
add it as an alias.

When several assets match equally well, the runtime does not guess: it
withholds the asset for that turn and asks which one you mean. Answering by ID,
by name, or by position ("la seconda") resolves it.

## Sample Data

The template ships with 20 PMI-Italia-style assets:

- Pumps (P-201, P-202, P-203)
- Boilers (C-3, C-4)
- Motors (ME-15, ME-16)
- Compressors (CP-101, CP-102)
- Heat exchangers, fans, transformers, PLCs, UPS, cranes, chillers, filters, tanks, AGVs

Replace `data/asset_registry.json` with your own data to use with your plant.
