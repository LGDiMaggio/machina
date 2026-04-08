"""Plant entity — top-level container for the asset hierarchy."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from machina.domain.asset import Asset
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

    async def load_assets_from(self, source: Any) -> int:
        """Load assets into the plant from a connector or a file path.

        Args:
            source: Either a connector exposing an async ``read_assets()``
                method, or a path (``str`` or ``Path``) to a JSON / YAML
                file containing a list of asset dicts.

        Returns:
            The number of assets loaded.

        Raises:
            TypeError: If ``source`` is neither a connector nor a path.
            FileNotFoundError: If the file path does not exist.
            ValueError: If the file extension is unsupported or the file
                does not contain a list of asset dicts.
        """
        if hasattr(source, "read_assets") and callable(source.read_assets):
            assets = await source.read_assets()
        elif isinstance(source, (str, Path)):
            assets = _load_assets_from_file(Path(source))
        else:
            raise TypeError(
                "source must be a connector with read_assets() or a file path, "
                f"got {type(source).__name__}"
            )

        for asset in assets:
            self.register_asset(asset)

        self._rebuild_hierarchy()
        return len(assets)

    def _rebuild_hierarchy(self) -> None:
        """Populate ``Asset.children`` from ``Asset.parent`` references.

        After loading a flat list of assets, walks every asset and
        ensures each asset whose ``parent`` exists in the registry
        appears in that parent's ``children`` list. Clears existing
        ``children`` first so reloads are idempotent.
        """
        for asset in self.assets.values():
            asset.children = []
        for asset in self.assets.values():
            if asset.parent and asset.parent in self.assets:
                parent = self.assets[asset.parent]
                if asset.id not in parent.children:
                    parent.children.append(asset.id)


def _load_assets_from_file(path: Path) -> list[Asset]:
    """Parse a JSON or YAML file containing a list of asset dicts."""
    if not path.exists():
        raise FileNotFoundError(f"Asset file not found: {path}")
    text = path.read_text(encoding="utf-8")
    suffix = path.suffix.lower()
    if suffix in (".yaml", ".yml"):
        import yaml

        data = yaml.safe_load(text)
    elif suffix == ".json":
        data = json.loads(text)
    else:
        raise ValueError(
            f"Unsupported asset file format: {suffix!r} (expected .json, .yaml, or .yml)"
        )
    if not isinstance(data, list):
        raise ValueError(f"Asset file must contain a list of assets, got {type(data).__name__}")
    return [Asset.model_validate(item) for item in data]
