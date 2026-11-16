"""Catalog-based failure-mode matching shared by the agent and MCP surfaces.

The agent's ``diagnose_failure`` tool and the MCP ``machina_diagnose_failure``
tool both harvest the failure-mode catalog with :func:`harvest_failure_modes`
and rank it with :func:`diagnose_symptoms`, so the two LLM-facing surfaces
return the same ranking, the same notes and the same catalog report. Each
surface resolves the asset against its own registry (the agent's
:class:`~machina.domain.plant.Plant`, the MCP server's primary CMMS) and
passes the result in.

What the matching is, and is not:

* It compares free-text symptoms with each catalog mode's
  ``typical_indicators`` by shared tokens. It reads no sensor data and
  processes no signals, so a result lists catalog entries worth checking; it
  is not an evidence-based fault diagnosis.
* ``indicator_match_ratio`` is the share of a mode's indicators the symptoms
  matched. It is not a probability, a likelihood or a calibrated confidence.
* The alarm/workflow path
  (:class:`~machina.domain.services.failure_analyzer.FailureAnalyzer`) keeps
  its own exact matching and categorical confidence gate. It receives the
  harvested catalog but does not go through :func:`diagnose_symptoms`.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog

from machina.agent.prompts import safe_text
from machina.exceptions import ConnectorError

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from machina.connectors.base import BaseConnector
    from machina.domain.asset import Asset
    from machina.domain.failure_mode import FailureMode

logger = structlog.get_logger(__name__)

NO_CATALOG_NOTE = "No failure-mode data configured on any connector."
"""Note for a catalog that no connector provides, or that its providers serve empty."""

MAX_RESULTS = 5
"""How many matches a result lists; ``match_count`` gives the total before this cut."""

# Trivial English stopwords dropped when tokenizing LLM free-text symptoms at
# the diagnosis tool boundary. Deliberately tiny: only glue words that carry
# no diagnostic signal. Modifiers like "high"/"low" are kept — they simply
# never overlap an indicator token, so they cannot cause false hits.
_SYMPTOM_STOPWORDS: frozenset[str] = frozenset(
    {"a", "an", "and", "are", "at", "for", "in", "is", "of", "on", "or", "the", "to", "with"}
)

_SYMPTOM_TOKEN_SPLIT_RE = re.compile(r"[^a-z0-9]+")


def _symptom_tokens(text: str) -> set[str]:
    """Normalize free text into matchable tokens for symptom matching.

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


@dataclass(frozen=True)
class CatalogHarvest:
    """One call-time harvest of the failure-mode catalog.

    Attributes:
        failure_modes: What the providers that answered served, deduped by
            code (the first registered provider wins).
        providers: Registry names of every connector declaring
            ``READ_FAILURE_MODES``, in registration order.
        failed: Registry name of each provider whose ``read_failure_modes()``
            raised :class:`~machina.exceptions.ConnectorError`, mapped to that
            error. This is what tells a catalog whose backend is down from one
            that was never configured.
    """

    failure_modes: list[FailureMode]
    providers: tuple[str, ...] = ()
    failed: Mapping[str, ConnectorError] = field(default_factory=dict)

    @property
    def partial(self) -> bool:
        """Whether a provider failed, so the catalog may lack some of its modes."""
        return bool(self.failed)

    @property
    def unavailable(self) -> bool:
        """Whether providers failed and the others served no failure mode."""
        return bool(self.failed) and not self.failure_modes

    def report(self) -> dict[str, Any]:
        """Return the ``catalog`` block of a diagnosis result.

        A failed provider is named with its error type only: a connector's
        error message can carry a host, a path or a value from the backend,
        so the message stays in the ``failure_mode_harvest_failed`` log.
        """
        return {
            "providers": list(self.providers),
            "failed_providers": [
                {"name": name, "error_type": type(exc).__name__}
                for name, exc in self.failed.items()
            ],
            "partial": self.partial,
        }

    def describe_failed(self) -> str:
        """Name each failed provider and its error type, as text for the model."""
        return ", ".join(
            f"provider '{name}' failed ({type(exc).__name__})" for name, exc in self.failed.items()
        )


async def harvest_failure_modes(
    providers: Sequence[tuple[str, BaseConnector]],
    **log_context: Any,
) -> CatalogHarvest:
    """Harvest failure modes from catalog providers, deduped by code.

    ``providers`` are the connectors declaring
    :attr:`~machina.connectors.capabilities.Capability.READ_FAILURE_MODES`,
    in ``find_by_capability`` order. Each connector's public
    ``read_failure_modes()`` is awaited at call time, so the harvest never
    serves a stale snapshot. A provider that raises
    :class:`~machina.exceptions.ConnectorError` (not connected, timed out,
    refused its credentials, served rows it cannot map) contributes nothing
    instead of aborting the harvest, and is recorded in
    :attr:`CatalogHarvest.failed`. Any other exception is a bug in the
    connector and propagates: it is never reported as an outage. Duplicate
    codes across connectors keep the first occurrence (registration order).

    Args:
        providers: ``(registry name, connector)`` pairs to read.
        **log_context: Added to the ``failure_mode_harvest_failed`` warning.

    Returns:
        The catalog, the providers read and the ones that failed.
    """
    # Fan out concurrently — one slow/flaky provider must not serialise
    # the whole harvest. gather() preserves argument order, so the
    # first-registration-wins dedup below is unchanged.
    results = await asyncio.gather(
        *(conn.read_failure_modes() for _name, conn in providers),  # type: ignore[attr-defined]
        return_exceptions=True,
    )
    by_code: dict[str, FailureMode] = {}
    failed: dict[str, ConnectorError] = {}
    for (name, _conn), result in zip(providers, results, strict=True):
        if isinstance(result, ConnectorError):
            logger.warning(
                "failure_mode_harvest_failed",
                connector=name,
                operation="collect_failure_modes",
                error_type=type(result).__name__,
                error=str(result),
                **log_context,
            )
            failed[name] = result
            continue
        if isinstance(result, BaseException):
            raise result
        for fm in result:
            if fm.code not in by_code:
                by_code[fm.code] = fm
    return CatalogHarvest(
        failure_modes=list(by_code.values()),
        providers=tuple(name for name, _conn in providers),
        failed=failed,
    )


def diagnose_symptoms(
    asset_id: str,
    asset: Asset | None,
    harvest: CatalogHarvest | None,
    symptoms: list[str],
    **log_context: Any,
) -> dict[str, Any]:
    """Rank catalog failure modes against free-text symptoms for one asset.

    The catalog is filtered to the asset's declared ``failure_modes`` when
    present, and the symptoms are matched by token overlap against each
    mode's ``typical_indicators`` (see :func:`_symptom_tokens`). Matches are
    ordered by matched-indicator count, then by ``indicator_match_ratio``,
    and the first :data:`MAX_RESULTS` are listed; ``match_count`` gives the
    total.

    The result always carries ``asset_id``, ``symptoms``,
    ``probable_failures`` and ``match_count``, plus ``asset_name`` and the
    ``catalog`` block (:meth:`CatalogHarvest.report`) once the asset
    resolved. The catalog states stay distinct:

    * no provider configured, or none serving a mode: ``note`` is
      :data:`NO_CATALOG_NOTE`;
    * at least one provider failed and the others served no mode: ``error``
      names the failed ones, since this is a connector failure, not a
      missing configuration;
    * some providers failed: the ranking runs on what the others served,
      ``catalog.partial`` is true, and the ``note`` ends by saying so;
    * a complete catalog with no match: the ``note`` lists the indicators
      the candidates know.

    An empty ``probable_failures`` list without an ``error`` ALWAYS carries
    a ``note`` saying why.

    Args:
        asset_id: The asset ID as the caller received it.
        asset: The asset ``asset_id`` resolved to, or ``None`` when unknown.
        harvest: The catalog harvest. An unknown asset needs none, so callers
            skip the harvest and pass ``None``; a resolved asset needs one.
        symptoms: Free-text symptoms, alarm parameters or observations.
        **log_context: Added to every warning logged here.

    Returns:
        The diagnosis result for the model or MCP client.

    Raises:
        TypeError: When ``asset`` resolved but ``harvest`` is ``None``.
    """
    result: dict[str, Any] = {
        "asset_id": asset_id,
        "symptoms": symptoms,
        "probable_failures": [],
        "match_count": 0,
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
    if harvest is None:
        # A caller bug, not a catalog state: reporting it as "no catalog"
        # would tell the model diagnosis is not set up.
        raise TypeError("diagnose_symptoms needs a catalog harvest for a resolved asset")
    result["asset_name"] = asset.name
    result["catalog"] = harvest.report()

    catalog = harvest.failure_modes
    if harvest.unavailable:
        # Providers ARE configured but could not serve: the catalog is
        # unavailable, not missing. An error rather than a note, so the
        # agent's turn loop handles it as the failed read it is.
        logger.warning(
            "diagnose_failure_catalog_unavailable",
            asset_id=asset_id,
            operation="diagnose_failure",
            failed_connectors=list(harvest.failed),
            **log_context,
        )
        result["error"] = safe_text(
            f"Failure-mode catalog unavailable: {harvest.describe_failed()}. "
            "Diagnosis could not run; this is a connector failure, not a "
            "missing configuration."
        )
        return result
    if not catalog:
        logger.warning(
            "diagnose_failure_no_catalog",
            asset_id=asset_id,
            operation="diagnose_failure",
            **log_context,
        )
        result["note"] = NO_CATALOG_NOTE
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
            # not a garbled "nothing matched" with an empty indicator list
            # (the no-match note below needs candidates).
            logger.warning(
                "diagnose_failure_declared_modes_not_in_catalog",
                asset_id=asset_id,
                operation="diagnose_failure",
                declared=sorted(declared),
                **log_context,
            )
            notes.append(
                safe_text(
                    f"Asset declares {len(declared)} failure mode(s) "
                    f"({', '.join(sorted(declared))}) but none are present in "
                    "the configured catalog (possible configuration mismatch)."
                )
            )
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
                # The fraction (0-1) of this mode's typical_indicators the
                # symptoms hit. Not a probability, likelihood or calibrated
                # confidence, and unrelated to FailureAnalyzer's CATEGORICAL
                # confidence on the alarm/workflow path.
                "indicator_match_ratio": round(len(matched) / len(fm.typical_indicators), 2),
                "matching_indicators": matched,
                "recommended_actions": fm.recommended_actions,
            }
        )
    # Rank by evidence first (matched-indicator count DESC), ratio second:
    # a mode matching 1 of 2 indicators must not outrank one matching 3 of 6.
    ranked.sort(
        key=lambda entry: (
            len(entry["matching_indicators"]),
            float(entry["indicator_match_ratio"]),
        ),
        reverse=True,
    )
    result["probable_failures"] = ranked[:MAX_RESULTS]
    result["match_count"] = len(ranked)

    if candidates and not ranked:
        # Tell the model WHAT it could have matched so it can re-ask the
        # user in the catalog's vocabulary instead of guessing.
        known = sorted({ind for fm in candidates for ind in fm.typical_indicators})
        notes.append(
            "No catalog entry matched these symptoms. Known indicators: "
            + safe_text(", ".join(known[:20]))
            + "."
        )
    if harvest.partial:
        # Last, as it qualifies every note before it: a declared mode missing
        # above may live on a failed provider, and a healthy match list would
        # otherwise hide the shrunken catalog entirely.
        notes.append(
            safe_text(
                f"Partial catalog: {harvest.describe_failed()}. Failure modes "
                "served by the failed provider(s) are missing from this result."
            )
        )
    if notes:
        result["note"] = " ".join(notes)
    return result
