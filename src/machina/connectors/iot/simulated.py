"""Simulated sensor connector — reads pre-recorded sensor data from JSON files.

Used for demos and examples where real OPC-UA / MQTT connections are not
available. In production, replace with :class:`OpcUaConnector` or
:class:`MqttConnector`.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, ClassVar

import structlog

from machina.connectors.base import ConnectorHealth, ConnectorStatus
from machina.connectors.capabilities import Capability

logger = structlog.get_logger()

class SimulatedSensorConnector:
    """Connector that loads sensor readings from JSON files.

    Each JSON file in ``data_dir`` should have the structure::

        {
            "asset_id": "P-201",
            "asset_name": "Cooling Water Pump",
            "sensor_readings": [
                {"timestamp": "...", "sensors": {"vibration_velocity_mm_s": 3.2, ...}},
                ...
            ]
        }
    """

    capabilities: ClassVar[frozenset[Capability]] = frozenset(
        {Capability.GET_RELATED_READINGS, Capability.GET_LATEST_READING}
    )

    def __init__(self, *, data_dir: str | Path) -> None:
        self._data_dir = Path(data_dir)
        self._connected = False
        self._readings: dict[str, dict[str, Any]] = {}

    async def connect(self) -> None:
        """Load sensor data from JSON files."""
        if not self._data_dir.exists():
            logger.warning(
                "sensor_data_dir_missing",
                connector="SimulatedSensorConnector",
                path=str(self._data_dir),
            )
            self._connected = True
            return

        for json_file in self._data_dir.glob("*.json"):
            text = await asyncio.to_thread(json_file.read_text, encoding="utf-8")
            data = json.loads(text)
            asset_id = data.get("asset_id", json_file.stem)
            print(asset_id)
            self._readings[asset_id] = data

        self._connected = True
        logger.info(
            "connected",
            connector="SimulatedSensorConnector",
            assets=list(self._readings.keys()),
        )

    async def disconnect(self) -> None:
        """Clear loaded data."""
        self._readings.clear()
        self._connected = False

    async def health_check(self) -> ConnectorHealth:
        """Check connector status."""
        if not self._connected:
            return ConnectorHealth(status=ConnectorStatus.UNHEALTHY, message="Not connected")
        return ConnectorHealth(
            status=ConnectorStatus.HEALTHY,
            message=f"Loaded sensor data for {len(self._readings)} assets",
        )

    async def get_latest_reading(self, asset_id: str, **kwargs: Any) -> dict[str, Any]:
        """Return the most recent sensor reading for an asset."""
        # Utilize the newly updated method to ensure filtering applies to the 'latest' reading too
        related = await self.get_related_readings(asset_id=asset_id, **kwargs)
        
        if not related.get("readings"):
            return {"asset_id": asset_id, "error": "No sensor data available matching criteria"}
            
        latest_filtered = related["readings"][-1]
        
        return {
            "asset_id": asset_id,
            "asset_name": related.get("asset_name", ""),
            "timestamp": latest_filtered.get("timestamp", ""),
            "sensors": latest_filtered.get("sensors", {}),
        }

    async def get_related_readings(
        self,
        asset_id: str = "",
        time_initial: str | None = None,
        time_end: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Return sensor readings for an asset, filtered by time and exact sensor values."""
        asset_id = asset_id or kwargs.pop("asset_id", "")
        data = self._readings.get(asset_id)
        if not data or not data.get("sensor_readings"):
            return {"asset_id": asset_id, "readings": [], "note": "No sensor data"}

        filtered_readings = []
        for reading in data["sensor_readings"]:
            timestamp = reading.get("timestamp", "")
            
            # 1. Apply Time Boundaries
            if time_initial and timestamp < time_initial:
                continue
            if time_end and timestamp > time_end:
                continue

            # 2. Apply Dynamic Kwarg Filters to Sensors
            sensors = reading.get("sensors", {})
            match_all_filters = True
            
            for key, expected_value in kwargs.items():
                if key in sensors and sensors[key] != expected_value:
                    match_all_filters = False
                    break
                    
            if not match_all_filters:
                continue

            filtered_readings.append(reading)

        # 3. Fallback: Return last 5 if absolutely no filters were provided to maintain previous behavior
        if not time_initial and not time_end and not kwargs:
            filtered_readings = filtered_readings[-5:]

        return {
            "asset_id": asset_id,
            "asset_name": data.get("asset_name", ""),
            "reading_count": len(filtered_readings),
            "readings": filtered_readings,
            "latest": filtered_readings[-1].get("sensors", {}) if filtered_readings else {},
        }
