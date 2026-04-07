"""Plant entity — top-level container for the asset hierarchy."""

from __future__ import annotations

from pydantic import BaseModel, Field

from machina.domain.asset import Asset  # noqa: TC001
from machina.exceptions import AssetNotFoundError


class Plant(BaseModel):
    """Top-level container representing a physical plant or site.

    Acts as the root of the asset hierarchy and provides lookup
    methods for navigating the equipment tree.
    """

    name: str = Field(..., description="Plant / site name")
    location: str = Field(default="", description="Geographic location")
    assets: dict[str, Asset] = Field(
        default_factory=dict, description="Asset registry keyed by asset ID"
    )

    model_config = {"frozen": False, "str_strip_whitespace": True}

    def register_asset(self, asset: Asset) -> None:
        """Add an asset to the plant registry."""
        self.assets[asset.id] = asset

    def get_asset(self, asset_id: str) -> Asset:
        """Look up an asset by ID.

        Raises:
            AssetNotFoundError: If no asset with the given ID exists.
        """
        try:
            return self.assets[asset_id]
        except KeyError:
            raise AssetNotFoundError(f"Asset {asset_id!r} not found") from None

    def list_assets(self) -> list[Asset]:
        """Return all registered assets."""
        return list(self.assets.values())
