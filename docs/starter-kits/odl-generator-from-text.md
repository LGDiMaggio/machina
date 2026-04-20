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

The template includes an Italian-tuned prompt (`prompts/entity_resolver_it.txt`)
that handles:

- **Typos**: "pompta" → "pompa", "c.da" → "caldaia"
- **Abbreviations**: "mot." → "motore", "compres." → "compressore"
- **Synonyms**: "boiler" → "caldaia", "nastro" → "nastro trasportatore"
- **Informal references**: "pompa acqua reparto B" → search by location

The rule-based `EntityResolver` handles exact ID matching, name keywords,
and location overlap. The LLM prompt adds fuzzy matching for the cases
the rule-based resolver cannot handle.

## Sample Data

The template ships with 20 PMI-Italia-style assets:

- Pumps (P-201, P-202, P-203)
- Boilers (C-3, C-4)
- Motors (ME-15, ME-16)
- Compressors (CP-101, CP-102)
- Heat exchangers, fans, transformers, PLCs, UPS, cranes, chillers, filters, tanks, AGVs

Replace `data/asset_registry.json` with your own data to use with your plant.
