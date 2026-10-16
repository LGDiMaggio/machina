"""Symptom diagnosis shared by the agent and MCP tool surfaces.

The agent's ``diagnose_failure`` tool and the MCP ``machina_diagnose_failure``
tool both harvest the catalog with :func:`collect_failure_modes` and rank it
with :func:`diagnose_symptoms`, so the two LLM-facing surfaces cannot drift on
ranking or on the honest-notes contract: an empty ``probable_failures`` list
always carries a ``note`` saying why. Each surface resolves the asset against
its own registry (the agent's ``Plant``, the MCP server's primary CMMS) and
passes the result in.
"""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING, Any

import structlog

from machina.agent.prompts import safe_text
from machina.exceptions import ConnectorError

if TYPE_CHECKING:
    from machina.connectors.base import BaseConnector
    from machina.domain.asset import Asset
    from machina.domain.failure_mode import FailureMode

logger = structlog.get_logger(__name__)

# Trivial English stopwords dropped when tokenizing LLM free-text symptoms at
# the diagnosis tool boundary. Deliberately tiny: only glue words that carry
# no diagnostic signal. Modifiers like "high"/"low" are kept — they simply
# never overlap an indicator token, so they cannot cause false hits.
_SYMPTOM_STOPWORDS: frozenset[str] = frozenset(
    {"a", "an", "and", "are", "at", "for", "in", "is", "of", "on", "or", "the", "to", "with"}
)

_SYMPTOM_TOKEN_SPLIT_RE = re.compile(r"[^a-z0-9]+")


def _symptom_tokens(text: str) -> set[str]:
    """Normalize free text into matchable tokens for symptom diagnosis.

    Lowercases, splits on non-alphanumerics, drops stopwords and
    single-character tokens (unit suffixes such as the ``s`` in ``mm_s``
    or the ``c`` in ``temperature_c`` would otherwise create spurious
    cross-mode matches).

    This tokenization lives at the LLM tool boundary ONLY: it lets
    "high vibration" match the canonical indicator
    ``vibration_velocity_mm_s`` via the shared token ``vibration``.
    Because indicators are tokenized with the same function, passing an
    exact canonical indicator name as a symptom still matches — the
    fuzzy matching is a strict superset of exact matching. The
    alarm/workflow path (:class:`FailureAnalyzer.diagnose`) keeps its
    exact-set-intersection semantics untouched.
    """
    return {
        tok
        for tok in _SYMPTOM_TOKEN_SPLIT_RE.split(text.lower())
        if len(tok) > 1 and tok not in _SYMPTOM_STOPWORDS
    }


async def collect_failure_modes(
    providers: list[tuple[str, BaseConnector]],
    **log_context: Any,
) -> list[FailureMode]:
    """Harvest failure modes from catalog providers, deduped by code.

    ``providers`` are the connectors declaring
    :attr:`~machina.connectors.capabilities.Capability.READ_FAILURE_MODES`,
    in ``find_by_capability`` order. Each connector's public
    ``read_failure_modes()`` is awaited at call time, so the harvest never
    serves a stale snapshot. A provider that raises :class:`ConnectorError`
    (e.g. not connected) contributes nothing instead of aborting the whole
    harvest — the empty-catalog honesty note downstream stays intact; any
    other exception is a connector bug and propagates. Duplicate codes
    across connectors keep the first occurrence (registration order).
    ``log_context`` is added to the skipped-provider warning.
    """
    # Fan out concurrently — one slow/flaky provider must not serialise
    # the whole harvest. gather() preserves argument order, so the
    # first-registration-wins dedup below is unchanged.
    results = await asyncio.gather(
        *(conn.read_failure_modes() for _name, conn in providers),  # type: ignore[attr-defined]
        return_exceptions=True,
    )
    by_code: dict[str, FailureMode] = {}
    for (name, _conn), result in zip(providers, results, strict=True):
        if isinstance(result, ConnectorError):
            logger.warning(
                "failure_mode_harvest_failed",
                connector=name,
                operation="collect_failure_modes",
                error=str(result),
                **log_context,
            )
            continue
        if isinstance(result, BaseException):
            raise result
        for fm in result:
            if fm.code not in by_code:
                by_code[fm.code] = fm
    return list(by_code.values())


def diagnose_symptoms(
    asset_id: str,
    asset: Asset | None,
    catalog: list[FailureMode],
    symptoms: list[str],
    **log_context: Any,
) -> dict[str, Any]:
    """Rank catalog failure modes against free-text symptoms for one asset.

    ``asset`` is ``asset_id`` as the caller resolved it (``None`` when
    unknown) and ``catalog`` the harvested failure modes; an unknown asset
    needs no catalog, so callers skip the harvest and pass ``[]``. The
    catalog is filtered to the asset's declared ``failure_modes`` when
    present, and the symptoms are matched by token overlap against each
    mode's ``typical_indicators`` (see :func:`_symptom_tokens`). The
    alarm/workflow path through
    :class:`~machina.domain.services.failure_analyzer.FailureAnalyzer`
    keeps its exact-match semantics — fuzzy matching lives at the LLM tool
    boundary only.

    An empty ``probable_failures`` list ALWAYS carries an explanatory
    ``note`` so the model can distinguish "unknown asset" from "no
    catalog configured" from "catalog present but nothing matched".
    ``log_context`` is added to every warning logged here.
    """
    result: dict[str, Any] = {
        "asset_id": asset_id,
        "symptoms": symptoms,
        "probable_failures": [],
    }

    # An unknown asset gets a distinct, honest note instead of a
    # full-catalog guess for equipment we know nothing about.
    if asset is None:
        logger.warning(
            "diagnose_failure_asset_not_found",
            asset_id=asset_id,
            operation="diagnose_failure",
            **log_context,
        )
        result["note"] = safe_text(f"Asset '{asset_id}' not found in the asset registry.")
        return result
    result["asset_name"] = asset.name

    if not catalog:
        logger.warning(
            "diagnose_failure_no_catalog",
            asset_id=asset_id,
            operation="diagnose_failure",
            **log_context,
        )
        result["note"] = "No failure-mode data configured on any connector."
        return result

    notes: list[str] = []

    # Per-asset applicability filter: when the asset declares its own
    # failure modes, match ONLY against those — a pump must never get the
    # conveyor's belt-wear diagnosis just because both list a vibration
    # indicator. Assets that declare nothing fall back to the full
    # catalog, and the result says so.
    declared = set(asset.failure_modes)
    if declared:
        candidates = [fm for fm in catalog if fm.code in declared]
        if not candidates:
            # The asset names failure modes, but NONE of them exist in the
            # harvested catalog — an honest configuration-mismatch note,
            # not a garbled "nothing matched" with an empty indicator list.
            logger.warning(
                "diagnose_failure_declared_modes_not_in_catalog",
                asset_id=asset_id,
                operation="diagnose_failure",
                declared=sorted(declared),
                **log_context,
            )
            result["note"] = safe_text(
                f"Asset declares {len(declared)} failure mode(s) "
                f"({', '.join(sorted(declared))}) but none are present in "
                "the configured catalog (possible configuration mismatch)."
            )
            return result
    else:
        candidates = catalog
        notes.append(
            "Asset declares no failure modes; diagnosis ran against the full failure-mode catalog."
        )

    symptom_tokens: set[str] = set()
    for symptom in symptoms:
        symptom_tokens |= _symptom_tokens(symptom)

    ranked: list[dict[str, Any]] = []
    for fm in candidates:
        if not fm.typical_indicators:
            continue
        matched = [ind for ind in fm.typical_indicators if _symptom_tokens(ind) & symptom_tokens]
        if not matched:
            continue
        ranked.append(
            {
                "code": fm.code,
                "name": fm.name,
                "category": fm.category,
                # Numeric indicator-match ratio 0-1: the fraction of this
                # mode's typical_indicators the symptoms hit. Distinct from
                # FailureAnalyzer's CATEGORICAL confidence on the
                # alarm/workflow path — do not conflate the two.
                "confidence": round(len(matched) / len(fm.typical_indicators), 2),
                "matching_indicators": matched,
                "recommended_actions": fm.recommended_actions,
            }
        )
    # Rank by evidence first (matched-indicator count DESC), ratio second:
    # a mode matching 1 of 2 indicators must not outrank one matching 3 of 6.
    ranked.sort(
        key=lambda entry: (len(entry["matching_indicators"]), float(entry["confidence"])),
        reverse=True,
    )
    result["probable_failures"] = ranked[:5]

    if not ranked:
        # Tell the model WHAT it could have matched so it can re-ask the
        # user in the catalog's vocabulary instead of guessing.
        known = sorted({ind for fm in candidates for ind in fm.typical_indicators})
        notes.append(
            "No catalog entry matched these symptoms. Known indicators: "
            + safe_text(", ".join(known[:20]))
            + "."
        )
    if notes:
        result["note"] = " ".join(notes)
    return result
