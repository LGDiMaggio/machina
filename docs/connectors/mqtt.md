# MQTT Connector

The `MqttConnector` ingests IoT sensor data from an MQTT broker and normalises
incoming messages into Machina [`Alarm`](../domain.md) entities.  It supports
JSON payloads, Sparkplug B (JSON representation), and raw numeric values.

## Prerequisites

* An MQTT broker (e.g. Mosquitto, HiveMQ, EMQX).
* Install the MQTT extra:

```bash
pip install machina-ai[mqtt]
```

This installs [`aiomqtt`](https://github.com/sbtinstruments/aiomqtt).

## Quick start

```python
from machina.connectors.iot import MqttConnector

mqtt = MqttConnector(
    broker="mqtt.example.com",
    port=1883,
    topics=[
        {
            "topic": "plant/sensors/pump-201/vibration",
            "asset_id": "P-201",
            "parameter": "vibration_velocity",
            "threshold": 6.0,
            "unit": "mm/s",
            "payload_format": "json",
            "value_path": "data.value",
        },
    ],
)
await mqtt.connect()

async def on_alarm(alarm):
    print(f"⚠ {alarm.asset_id}: {alarm.parameter}={alarm.value} {alarm.unit}")

sub = await mqtt.subscribe(on_alarm)
```

## YAML configuration

```yaml
connectors:
  iot:
    type: mqtt
    settings:
      broker: mqtt.example.com
      port: 1883
      username: ${MQTT_USER}
      password: ${MQTT_PASS}
      tls: false
      topics:
        - topic: "plant/sensors/pump-201/vibration"
          asset_id: P-201
          parameter: vibration_velocity
          threshold: 6.0
          unit: mm/s
          payload_format: json
          value_path: data.value
        - topic: "spBv1.0/Plant/DDATA/+/+"
          payload_format: sparkplug_b
          parameter: temperature
          threshold: 80.0
          unit: "°C"
          severity: critical
```

## Capabilities

| Capability | Description |
|---|---|
| `subscribe_to_topics` | Subscribe to MQTT topics with alarm generation. |
| `publish_message` | Publish a message to any MQTT topic. |

### Publishing messages

```python
await mqtt.publish("plant/commands/pump-201", '{"action": "stop"}', qos=1)
```

### Unsubscribing

```python
await mqtt.unsubscribe(sub)
await mqtt.disconnect()
```

## Payload formats

### JSON (default)

The most common format.  Use `value_path` to specify where the numeric
value lives in the JSON object (dot notation).

```json
{"data": {"value": 7.8, "unit": "mm/s", "ts": "2026-04-11T10:00:00Z"}}
```

```yaml
payload_format: json
value_path: data.value
```

### Sparkplug B

Sparkplug B is a standard for industrial MQTT.  This connector supports the
JSON representation used by MQTT-to-JSON bridges (e.g. Ignition, HiveMQ
Sparkplug extension):

```json
{
  "metrics": [
    {"name": "temperature", "value": 72.5, "type": "Float"},
    {"name": "vibration", "value": 4.2, "type": "Float"}
  ]
}
```

The `parameter` field in the topic config is matched against
`metric.name`.

```yaml
payload_format: sparkplug_b
parameter: temperature
```

!!! note "Protobuf payloads"
    Native Sparkplug B protobuf payloads are not yet supported.
    Use an MQTT-to-JSON bridge or configure your gateway to publish
    JSON-encoded Sparkplug messages.

### Raw

For simple sensors that publish a plain numeric string:

```
7.8
```

```yaml
payload_format: raw
```

## MQTT wildcards

Topic filters support standard MQTT wildcards:

| Wildcard | Description | Example |
|---|---|---|
| `+` | Matches exactly one topic level. | `plant/+/vibration` |
| `#` | Matches any number of levels (must be last). | `plant/sensors/#` |

## TLS / SSL

```python
mqtt = MqttConnector(
    broker="mqtt.example.com",
    port=8883,
    tls=True,
    ca_certs="/path/to/ca.crt",
    username="device01",
    password="secret",
)
```

## Alarm normalisation

When a message's extracted value exceeds the configured `threshold`, an
`Alarm` entity is created:

| Alarm field | Source |
|---|---|
| `id` | Auto-generated (`ALM-{uuid}`) |
| `asset_id` | From topic config `asset_id` (falls back to topic string) |
| `severity` | From topic config `severity` (default `WARNING`) |
| `parameter` | From topic config `parameter` (falls back to topic string) |
| `value` | Parsed from payload |
| `threshold` | From topic config |
| `unit` | From topic config |
| `source` | `mqtt://{broker}/{topic}` |

## API reference

::: machina.connectors.iot.mqtt.MqttConnector
