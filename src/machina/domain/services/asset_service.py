"""AssetService — domain-level operations on assets.

Provides methods that the workflow engine can call via the
``domain`` service prefix, e.g. ``domain.check_asset_criticality``.
"""

from __future__ import annotations

from typing import Any

from machina.domain.plant import Plant  # noqa: TC001


class AssetService:
    """Domain service for asset-level queries.

    Args:
        plant: The plant instance whose asset registry is queried.
    """

    def __init__(self, *, plant: Plant) -> None:
        self._plant = plant

    def check_asset_criticality(
        self,
        *,
        asset_id: str = "",
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Return the criticality classification for an asset.

        Args:
            asset_id: The asset to look up.

        Returns:
            A dict with ``asset_id``, ``criticality``, and ``name``.
            If the asset is not found, ``criticality`` is ``"unknown"``.
        """
        asset = self._plant.get_asset(asset_id)
        if asset is None:
            return {
                "asset_id": asset_id,
                "criticality": "unknown",
                "name": "",
            }
        return {
            "asset_id": asset.id,
            "criticality": asset.criticality.value
            if hasattr(asset.criticality, "value")
            else str(asset.criticality),
            "name": asset.name,
        }
