"""IoT / industrial protocol connectors for real-time sensor data."""

from machina.connectors.iot.mqtt import MqttConnector
from machina.connectors.iot.opcua import OpcUaConnector

# Short public API aliases
MQTT = MqttConnector
OpcUA = OpcUaConnector

__all__ = [
    "MQTT",
    "MqttConnector",
    "OpcUA",
    "OpcUaConnector",
]
