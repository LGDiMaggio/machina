"""Pure-function mappers: vendor payload ↔ Machina domain entities.

Each vendor module (``sap_pm``, ``maximo``, ``upkeep``) exposes
module-level ``parse_*`` and ``reverse_*`` functions plus the mapping
constants used to translate dicts into domain entities.  The functions
are pure (no I/O, no connector state) so they can be unit-tested with
raw dict inputs and reused by any transport — the existing CMMS
connector, a future MCP-client transport adapter, or offline batch
tools.
"""
