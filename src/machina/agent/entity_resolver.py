"""Entity resolver — map natural language references to domain entities.

When a technician says "the pump in building A" or "P-201", the entity
resolver finds the matching asset(s) in the plant registry.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from collections.abc import Sequence

    from machina.domain.asset import Asset
    from machina.domain.plant import Plant

logger = structlog.get_logger(__name__)

_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)

# Below this confidence, a resolved asset is treated as a weak guess rather than
# the definitive referent: the runtime does not commit to it (no prefetch, no
# ``context["asset"]``) and the agent is nudged to ask the user which asset is
# meant instead of acting on it (U5 — resolution-confidence gate). The bands the
# resolver emits: exact_id=1.0, name=0.9, name_keywords≤0.7, location≤0.6,
# fuzzy_keyword≤0.4 — so this floor admits id/name/strong matches and withholds
# the weakest keyword guesses (e.g. the 0.16 fuzzy match from dogfooding).
RESOLUTION_MIN_CONFIDENCE = 0.4

# Floor of the ``high`` band — a match the runtime may act on without hedging.
# Split out from :data:`RESOLUTION_MIN_CONFIDENCE` so the partition below is
# closed: ``[0.7, ∞)`` high, ``[0.4, 0.7)`` mid, ``(-∞, 0.4)`` low. The origin
# doc said "~0.7" and left ``(0.6, 0.7)`` unassigned; an if/elif/else would have
# swept that range into whichever branch happened to be ``else`` — permissive by
# accident. Naming the floor makes the gap impossible to reintroduce silently.
RESOLUTION_HIGH_CONFIDENCE = 0.7

# Band labels. Plain strings rather than an enum: ``entity_resolver`` is
# imported on every ``machina describe`` (Architecture Decision 8) and stays
# deliberately thin, and a public enum class would pull a docs-coverage
# obligation (``tests/unit/test_docs_coverage.py``) for zero reader benefit.
BAND_HIGH = "high"
BAND_MID = "mid"
BAND_LOW = "low"

# ``match_reason`` that marks a whole-token asset-ID hit. Several candidates at
# this reason means the user named several distinct assets ("compare P-201 and
# P-202") — multiplicity, not ambiguity.
_EXACT_ID_REASON = "exact_id"


def _id_occurs_in(asset_id: str, text: str) -> bool:
    """Whether ``asset_id`` occurs in ``text`` as a whole token.

    Raw substring containment makes a short ID match inside a longer one —
    ``P-2`` hits inside ``P-201`` — and stage 1 hands that back at confidence
    1.0, i.e. the wrong asset presented as the definitive referent. Anchoring
    with non-word lookarounds on both sides rejects that while staying
    permissive about the surrounding punctuation technicians actually type
    (``P-201,`` / ``(P-201)`` / end of message).

    ``Asset.id`` is free-form (only non-empty is enforced), so the ID is
    escaped and matched literally rather than parsed against any ID grammar —
    a format-constrained pattern would silently drop every asset whose ID does
    not fit the assumed shape.

    Lookarounds are used instead of ``\\b`` because ``\\b`` is defined relative
    to the adjacent character and misbehaves for IDs that start or end with a
    non-word character.

    Known limitation, accepted: ``/`` is a non-word character, so ``P-201``
    still matches inside ``P-201/A``.

    Args:
        asset_id: The registered asset identifier to look for.
        text: Free-form user input.

    Returns:
        ``True`` if the ID appears as a whole token, case-insensitively.
    """
    pattern = rf"(?<!\w){re.escape(asset_id)}(?!\w)"
    return re.search(pattern, text, re.IGNORECASE) is not None


def _tokenise(text: str) -> set[str]:
    """Tokenise text into lowercase words with punctuation stripped."""
    cleaned = _PUNCT_RE.sub(" ", text.lower())
    return {w for w in cleaned.split() if len(w) > 0}


@dataclass
class ResolvedEntity:
    """Result of entity resolution.

    ``confidence`` is required. It previously defaulted to ``1.0``, which made
    an entity built without a stated confidence *maximally* confident — the
    authority gate then acted on a match nobody had scored. Omitting it is now
    a ``TypeError`` at construction rather than silent full trust.

    Args:
        asset: The matched asset.
        confidence: Confidence score (0.0-1.0).
        match_reason: How the match was determined.
    """

    asset: Asset
    confidence: float
    match_reason: str = ""

    def __repr__(self) -> str:
        return (
            f"ResolvedEntity(asset={self.asset.id!r}, "
            f"confidence={self.confidence:.2f}, reason={self.match_reason!r})"
        )


@dataclass(frozen=True)
class _ResolutionVerdict:
    """How much authority a resolution result carries.

    Private by design: the verdict is an internal contract between the runtime
    gate and the prompt renderer, not something callers construct. Read it via
    :func:`resolution_verdict`.

    Attributes:
        band: :data:`BAND_HIGH`, :data:`BAND_MID`, :data:`BAND_LOW`, or ``None``
            when there were no candidates at all.
        ambiguous: The top two candidates have identical confidence and the top
            match is not a whole-token ID hit.
    """

    band: str | None
    ambiguous: bool

    @property
    def confident(self) -> bool:
        """Whether the top match may be treated as the definitive referent.

        The single predicate both consumers read, so the gate's decision to
        withhold and the prompt's decision to nudge cannot drift apart. ``None``
        (no candidates) is not confident — there is nothing to be confident in.
        """
        return self.band in (BAND_HIGH, BAND_MID)


def _band_for(confidence: object) -> str:
    """Classify a raw confidence value into a closed, exhaustive band.

    The partition is stated as three positive tests with no ``else`` branch, so
    no unassigned range can be swept into the permissive tier. Anything that
    fails all three — ``NaN``, ``None``, a string, a mock without a real score —
    is indeterminable, and an indeterminable confidence is *not* confidence:
    it lands in :data:`BAND_LOW`.

    Args:
        confidence: A confidence score, or any value where one was expected.

    Returns:
        One of :data:`BAND_HIGH`, :data:`BAND_MID`, :data:`BAND_LOW`.
    """
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        return BAND_LOW
    value = float(confidence)
    if value >= RESOLUTION_HIGH_CONFIDENCE:
        return BAND_HIGH
    if RESOLUTION_MIN_CONFIDENCE <= value < RESOLUTION_HIGH_CONFIDENCE:
        return BAND_MID
    if value < RESOLUTION_MIN_CONFIDENCE:
        return BAND_LOW
    return BAND_LOW  # NaN and other non-comparables — fail closed.


def resolution_verdict(entities: Sequence[ResolvedEntity]) -> _ResolutionVerdict:
    """Derive the authority verdict for a resolution result.

    The one place in ``src/`` that turns a candidate list into a band plus an
    ambiguity call. Both consumers — the runtime's commit gate and the prompt
    renderer's disambiguation nudge — read this, so they cannot disagree about
    what the same candidate list means.

    Ambiguity is an **exact confidence tie at the top**, not a shared band. The
    defect being fixed is ``resolved[0]`` picking *arbitrarily* among candidates
    of identical confidence — that is the only case where the runtime has no
    basis to choose. Deliberately not band-equality: 0.9 against 0.75 are both
    :data:`BAND_HIGH`, but the sort put a clear winner first, and asking the
    user there is noise where a correct answer already exists. Non-tie
    uncertainty is handled by stating the assumption, not by asking.
    Deliberately not an epsilon either: every confidence in the cascade is
    ``round(..., 2)``-quantised, so exact equality is meaningful and there is no
    tolerance to tune.

    Ambiguity is also not the same as multiplicity. A tie at
    :data:`_EXACT_ID_REASON` means the user typed several whole-token asset IDs
    that each matched a distinct asset ("compare P-201 and P-202") — a
    well-posed question about several assets, and classifying it ambiguous would
    turn it into an unanswerable refusal. Ambiguity therefore also requires that
    the top match arrived by something weaker than an exact ID hit.

    Args:
        entities: Resolution candidates, highest confidence first (the order
            :meth:`EntityResolver.resolve` returns).

    Returns:
        The :class:`_ResolutionVerdict` for this candidate list. An empty list
        yields ``band=None, ambiguous=False`` — the not-found path, unchanged.

    Example:
        ```python
        verdict = resolution_verdict(resolver.resolve(text))
        if not verdict.confident:
            ...  # ask which asset is meant instead of acting
        ```
    """
    if not entities:
        return _ResolutionVerdict(band=None, ambiguous=False)

    top = entities[0]
    top_confidence = getattr(top, "confidence", None)
    band = _band_for(top_confidence)

    tied_at_top = (
        len(entities) > 1
        and isinstance(top_confidence, (int, float))
        and not isinstance(top_confidence, bool)
        and getattr(entities[1], "confidence", None) == top_confidence
    )
    ambiguous = tied_at_top and getattr(top, "match_reason", "") != _EXACT_ID_REASON
    return _ResolutionVerdict(band=band, ambiguous=ambiguous)


class EntityResolver:
    """Resolves natural language references to assets in a plant.

    Uses a cascading strategy:
    1. Exact ID match (e.g. ``"P-201"``)
    2. Name match (e.g. ``"cooling water pump"``)
    3. Location match (e.g. ``"building A"``)
    4. Keyword match — verbatim token containment across all asset fields
       (no typo tolerance)

    Args:
        plant: The plant containing the asset registry.

    Example:
        ```python
        resolver = EntityResolver(plant)
        matches = resolver.resolve("the pump in building A")
        if matches:
            asset = matches[0].asset
        ```
    """

    def __init__(self, plant: Plant) -> None:
        self._plant = plant

    def resolve(self, text: str) -> list[ResolvedEntity]:
        """Resolve a natural language reference to zero or more assets.

        Args:
            text: User input that may reference an asset.

        Returns:
            List of :class:`ResolvedEntity` results, ordered by confidence
            (highest first). Empty if no matches found.
        """
        assets = self._plant.list_assets()
        if not assets:
            return []

        results: list[ResolvedEntity] = []

        # 1. Exact ID match — look for asset IDs embedded in the text
        for asset in assets:
            if _id_occurs_in(asset.id, text):
                results.append(ResolvedEntity(asset, confidence=1.0, match_reason="exact_id"))
                logger.debug(
                    "entity_resolved",
                    operation="resolve",
                    asset_id=asset.id,
                    match_reason="exact_id",
                )

        if results:
            return results

        # 2. Name match — check if the asset name appears in the text
        text_lower = text.lower()
        text_tokens = _tokenise(text)
        for asset in assets:
            name_lower = asset.name.lower()
            # Check full name match
            if name_lower in text_lower:
                results.append(ResolvedEntity(asset, confidence=0.9, match_reason="name_match"))
            else:
                # Check significant name words (skip short words)
                name_words = {w for w in name_lower.split() if len(w) > 2}
                overlap = name_words & text_tokens
                if overlap and len(overlap) >= len(name_words) * 0.5:
                    score = len(overlap) / len(name_words)
                    results.append(
                        ResolvedEntity(
                            asset,
                            confidence=round(0.7 * score, 2),
                            match_reason="name_keywords",
                        )
                    )

        if results:
            results.sort(key=lambda r: r.confidence, reverse=True)
            return results

        # 3. Location match
        for asset in assets:
            if asset.location:
                loc_lower = asset.location.lower()
                loc_parts = re.split(r"[/\-,\s]+", loc_lower)
                loc_parts = [p for p in loc_parts if len(p) > 1]
                overlap = set(loc_parts) & text_tokens
                if overlap:
                    score = len(overlap) / max(len(loc_parts), 1)
                    results.append(
                        ResolvedEntity(
                            asset,
                            confidence=round(0.6 * score, 2),
                            match_reason="location_match",
                        )
                    )

        if results:
            results.sort(key=lambda r: r.confidence, reverse=True)
            return results

        # 4. Keyword match — exact substring containment of each query token in
        # the asset's concatenated fields. Despite the "fuzzy" label this once
        # carried, there is no edit-distance or typo tolerance here: a token
        # either occurs verbatim or it does not.
        for asset in assets:
            searchable = " ".join(
                [
                    asset.id,
                    asset.name,
                    asset.location,
                    asset.manufacturer,
                    asset.model,
                    asset.type.value,
                ]
            ).lower()

            text_tokens_list = [t for t in text_tokens if len(t) > 2]
            if not text_tokens_list:
                continue

            matches = sum(1 for token in text_tokens_list if token in searchable)
            if matches > 0:
                score = matches / len(text_tokens_list)
                results.append(
                    ResolvedEntity(
                        asset,
                        confidence=round(0.4 * score, 2),
                        match_reason="keyword_match",
                    )
                )

        results.sort(key=lambda r: r.confidence, reverse=True)
        return results
