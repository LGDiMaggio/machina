# OPC-UA Connector

The `OpcUaConnector` reads real-time sensor data from OPC-UA-enabled PLCs and
SCADA systems.  It subscribes to node value changes and normalises them into
Machina [`Alarm`](../domain.md) entities when configured thresholds are
exceeded.

## Prerequisites

* An OPC-UA server accessible from the network (e.g. Siemens S7, Beckhoff,
  Prosys Simulation Server).
* Install the OPC-UA extra:

```bash
pip install machina-ai[opcua]
```

This installs [`asyncua`](https://github.com/FreeOpcUa/opcua-asyncio).

## Quick start

```python
from machina.connectors.iot import OpcUaConnector

opcua = OpcUaConnector(
    endpoint="opc.tcp://plc-line2:4840",
    subscriptions=[
        {
            "node_id": "ns=2;s=Pump.P201.Vibration.DE",
            "sampling_interval_ms": 1000,
            "asset_id": "P-201",
            "parameter": "vibration_velocity",
            "threshold": 6.0,
            "unit": "mm/s",
        },
    ],
)
await opcua.connect()

async def on_alarm(alarm):
    print(f"⚠ {alarm.asset_id}: {alarm.parameter}={alarm.value} {alarm.unit}")

sub = await opcua.subscribe(on_alarm)
```

## YAML configuration

```yaml
connectors:
  sensors:
    type: opcua
    settings:
      endpoint: opc.tcp://plc-line2:4840
      security_mode: SignAndEncrypt
      security_policy: Basic256Sha256
      certificate: /certs/client.der
      private_key: /certs/client_key.pem
      subscriptions:
        - node_id: "ns=2;s=Pump.P201.Vibration.DE"
          sampling_interval_ms: 1000
          asset_id: P-201
          parameter: vibration_velocity
          threshold: 6.0
          unit: mm/s
        - node_id: "ns=2;s=Pump.P201.Temperature"
          sampling_interval_ms: 5000
          asset_id: P-201
          parameter: bearing_temperature
          threshold: 80.0
          unit: "°C"
          severity: critical
```

## Capabilities

| Capability | Description |
|---|---|
| `subscribe_to_nodes` | Subscribe to OPC-UA node value changes with alarm generation. |
| `read_node_value` | Read the current value of a single node on demand. |
| `read_node_values` | Read multiple node values in one call. |
| `browse_nodes` | Browse the OPC-UA address space from a given root. |

### Reading values on demand

```python
value = await opcua.read_value("ns=2;s=Pump.P201.Vibration.DE")

values = await opcua.read_values([
    "ns=2;s=Pump.P201.Vibration.DE",
    "ns=2;s=Pump.P201.Temperature",
])
```

### Browsing the address space

```python
nodes = await opcua.browse_nodes()
for node in nodes:
    print(f"{node['browse_name']} ({node['node_class']}): {node['node_id']}")
```

### Unsubscribing

```python
await opcua.unsubscribe(sub)
await opcua.disconnect()
```

## Security modes

| Mode | Description |
|---|---|
| `None` | No security (suitable for isolated networks). |
| `Sign` | Messages are signed but not encrypted. |
| `SignAndEncrypt` | Messages are signed and encrypted (recommended for production). |

!!! warning "Certificate-based security"
    When using `Sign` or `SignAndEncrypt`, provide both `certificate` (DER or
    PEM) and `private_key` (PEM) paths.  The OPC-UA server must trust the
    client certificate.

## Alarm normalisation

When a subscribed node's value exceeds its configured `threshold`, an `Alarm`
entity is created:

| Alarm field | Source |
|---|---|
| `id` | Auto-generated (`ALM-{uuid}`) |
| `asset_id` | From subscription `asset_id` (falls back to node ID) |
| `severity` | From subscription `severity` (default `WARNING`) |
| `parameter` | From subscription `parameter` (falls back to node ID) |
| `value` | The new node value |
| `threshold` | From subscription config |
| `unit` | From subscription config |
| `source` | `opcua://{endpoint}/{node_id}` |

## API reference

::: machina.connectors.iot.opcua.OpcUaConnector
