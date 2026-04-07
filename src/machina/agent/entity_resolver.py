"""Entity resolver — map natural language references to domain entities.

When a technician says "the pump in building A" or "P-201", the entity
resolver finds the matching asset(s) in the plant registry.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from machina.domain.asset import Asset
    from machina.domain.plant import Plant

logger = structlog.get_logger(__name__)

_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)


def _tokenise(text: str) -> set[str]:
    """Tokenise text into lowercase words with punctuation stripped."""
    cleaned = _PUNCT_RE.sub(" ", text.lower())
    return {w for w in cleaned.split() if len(w) > 0}


class ResolvedEntity:
    """Result of entity resolution.

    Attributes:
        asset: The matched asset.
        confidence: Confidence score (0.0-1.0).
        match_reason: How the match was determined.
    """

    __slots__ = ("asset", "confidence", "match_reason")

    def __init__(
        self,
        asset: Asset,
        *,
        confidence: float = 1.0,
        match_reason: str = "",
    ) -> None:
        self.asset = asset
        self.confidence = confidence
        self.match_reason = match_reason

    def __repr__(self) -> str:
        return (
            f"ResolvedEntity(asset={self.asset.id!r}, "
            f"confidence={self.confidence:.2f}, reason={self.match_reason!r})"
        )


class EntityResolver:
    """Resolves natural language references to assets in a plant.

    Uses a cascading strategy:
    1. Exact ID match (e.g. ``"P-201"``)
    2. Name match (e.g. ``"cooling water pump"``)
    3. Location match (e.g. ``"building A"``)
    4. Fuzzy keyword match across all asset fields

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
            if asset.id.lower() in text.lower():
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

        # 4. Fuzzy keyword match across all fields
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

    def resolve_best(self, text: str) -> Asset | None:
        """Resolve to the single best-matching asset, or None.

        Args:
            text: User input that may reference an asset.

        Returns:
            The highest-confidence match, or ``None``.
        """
        matches = self.resolve(text)
        return matches[0].asset if matches else None
