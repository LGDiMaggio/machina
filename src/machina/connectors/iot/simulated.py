"""Simulated sensor connector — reads pre-recorded sensor data from JSON files.

Used for demos and examples where real OPC-UA / MQTT connections are not
available. In production, replace with :class:`OpcUaConnector` or
:class:`MqttConnector`.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import structlog

from machina.connectors.base import ConnectorHealth, ConnectorStatus

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

    capabilities = [
        "read_sensor_data",
        "get_related_readings",
        "get_latest_reading",
    ]

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

    async def get_latest_reading(self, asset_id: str) -> dict[str, Any]:
        """Return the most recent sensor reading for an asset."""
        data = self._readings.get(asset_id)
        if not data or not data.get("sensor_readings"):
            return {"asset_id": asset_id, "error": "No sensor data available"}
        latest = data["sensor_readings"][-1]
        return {
            "asset_id": asset_id,
            "asset_name": data.get("asset_name", ""),
            "timestamp": latest.get("timestamp", ""),
            "sensors": latest.get("sensors", {}),
        }

    async def get_related_readings(
        self,
        asset_id: str = "",
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Return recent sensor readings for an asset (used by workflows).

        Returns the last 5 readings to show the trend.
        """
        # Accept asset_id from kwargs (workflow template resolution)
        asset_id = asset_id or kwargs.get("asset_id", "")
        data = self._readings.get(asset_id)
        if not data or not data.get("sensor_readings"):
            return {"asset_id": asset_id, "readings": [], "note": "No sensor data"}

        recent = data["sensor_readings"][-5:]
        return {
            "asset_id": asset_id,
            "asset_name": data.get("asset_name", ""),
            "reading_count": len(recent),
            "readings": recent,
            "latest": recent[-1].get("sensors", {}),
        }
