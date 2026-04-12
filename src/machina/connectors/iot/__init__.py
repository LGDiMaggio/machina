"""IoT / industrial protocol connectors for real-time sensor data."""

from machina.connectors.iot.mqtt import MqttConnector
from machina.connectors.iot.opcua import OpcUaConnector
from machina.connectors.iot.simulated import SimulatedSensorConnector

# Short public API aliases
MQTT = MqttConnector
OpcUA = OpcUaConnector

__all__ = [
    "MQTT",
    "MqttConnector",
    "OpcUA",
    "OpcUaConnector",
    "SimulatedSensorConnector",
]
