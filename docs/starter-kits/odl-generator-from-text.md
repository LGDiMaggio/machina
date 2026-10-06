# Template: OdL Generator from Text

The `odl-generator-from-text` template turns a technician's free-text request
into structured work orders: the agent resolves the assets the message names
against the plant registry, proposes one work order per asset, and — in live
mode — writes each one after a human confirms it.

## Use Case

Maintenance requests often arrive as a message rather than as a CMMS entry:

> *"pompa P-201 perde acqua, caldaia C-3 rumore anomalo, prego creare OdL"*

1. The technician types (or sends) the request
2. The agent resolves `P-201` and `C-3` against the registry
3. It proposes a work order per asset — type, priority, description
4. In live mode it asks for confirmation, then appends the work orders to the
   substrate (a spreadsheet by default)

## Flow

```
Technician                 Machina agent                         Substrate
  │                             │                                    │
  │── request (CLI/Telegram) ──▶│                                    │
  │                             │── resolve assets in the registry ─▶│
  │                             │── propose work orders (LLM tools)  │
  │◀── "confirm? [y/N]" ────────│   (live mode only)                 │
  │── y ───────────────────────▶│── append confirmed work orders ───▶│
  │◀── summary ─────────────────│                                    │
```

In sandbox mode (the default) the proposal is logged and nothing is written.
A work order can only target an asset the message actually resolved to; when
a message could mean several assets, the agent asks which one first.

## Substrate Options

| Substrate | Connector | Config | Best for |
|-----------|-----------|--------|----------|
| **Spreadsheets** (default) | `ExcelCsvConnector` — reads `asset_registry.xlsx`, appends to `workorders.xlsx` | `config.yaml` | Small teams without a CMMS, demos |
| **CMMS over REST** | `GenericCmmsConnector` — the Generic CMMS REST contract | `config.cmms-rest.yaml` | Teams with an existing CMMS API |

Switch with `python agent.py --config config.cmms-rest.yaml`.

## Communication Channels

| Channel | How |
|---------|-----|
| Terminal (CLI) | Default; also where confirmations are asked |
| Telegram | Opt-in block in `config.yaml`; confirmation by replying to the agent |
| Email | Supported by Machina, but senders are not authenticated — keep it for sandbox demos |

## Getting Started

See the [template README](https://github.com/LGDiMaggio/machina/tree/main/templates/odl-generator-from-text)
for setup: install, copy `.env.example` to `.env`, then
`python agent.py --sandbox`.

## Entity Resolution

Resolution is rule-based. `EntityResolver` matches, in order:

1. **Exact asset ID** as a whole token — "pompa P-201 perde" → `P-201`
2. **Registered name, and any curated `Asset.aliases`** — the plant's own word
   for the machine, at the same authority as its registered name
3. **Location overlap** — "pompa acqua reparto B" narrows by location
4. **Verbatim keyword containment** across the asset's fields

**Synonyms** are what aliases are for. Add the words your technicians actually
use to the registry's `Alias` column (`;`-separated) and they resolve like the
registered name:

| Codice | Nome | … | Alias |
|--------|------|---|-------|
| C-3 | Caldaia a Vapore | … | `boiler;caldaia vapore;la grande` |

Aliases are language-neutral strings — an Italian and an English alias are
both just entries in the list — and they name the asset, never describe its
condition. The same field is available on every substrate (`aliases` in JSON,
a `;`-delimited column in Excel/SQL/CMMS sources).

**Typos and abbreviations are not handled.** "pompta" does not resolve to
"pompa": there is no edit-distance or fuzzy matching anywhere in the cascade,
only verbatim containment. If a spelling recurs on your site, add it as an
alias.

When several assets match equally well, the runtime does not guess: it
withholds the asset for that turn and asks which one you mean. Answering by
ID, by name, or by position ("la seconda") resolves it.

## Sample Data

The template ships with 20 sample assets in `data/asset_registry.xlsx`
(Italian names; `asset_registry_en.xlsx` has English names):

- Pumps (P-201, P-202, P-203)
- Boilers (C-3, C-4)
- Motors (ME-15, ME-16)
- Compressors (CP-101, CP-102)
- Heat exchangers, fans, transformers, PLCs, UPS, cranes, chillers, filters, tanks, AGVs

The spreadsheets are generated from the JSON files next to them by
`python data/build_sample_registry.py`. Replace them with your own registry
and adjust the column mapping in `config.yaml`.
