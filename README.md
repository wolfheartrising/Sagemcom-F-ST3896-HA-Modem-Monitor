# Modem Monitor — Home Assistant Add-on Repository

Custom Home Assistant add-on repository for monitoring a **Sagemcom F@ST3896** cable modem via DOCSIS telemetry, published over MQTT.

## Installation

1. In Home Assistant, go to **Settings → Add-ons → Add-on Store**
2. Click the three-dot menu (⋮) → **Repositories**
3. Add this repository URL:
   ```
   https://github.com/YOUR_USERNAME/modem-monitor
   ```
4. Find **Modem Monitor** in the store and install it

## Add-ons

### Modem Monitor (`modem_monitor`)

Polls the Sagemcom F@ST3896 modem JSON API on a configurable interval and publishes downstream/upstream DOCSIS channel telemetry to an MQTT broker.

**Configuration options:**

| Option | Default | Description |
|---|---|---|
| `modem_host` | `192.168.100.1` | Modem IP address |
| `modem_username` | _(empty)_ | Modem login username |
| `modem_password` | _(empty)_ | Modem login password |
| `mqtt_host` | `core-mosquitto` | MQTT broker hostname |
| `mqtt_topic` | `modem/telemetry` | Topic to publish telemetry |
| `mqtt_username` | _(empty)_ | MQTT username |
| `mqtt_password` | _(empty)_ | MQTT password |
| `interval` | `60` | Poll interval in seconds (10–300) |

## Requirements

- Home Assistant OS or Supervised
- Mosquitto broker add-on (or any MQTT broker reachable from the add-on)
- Modem accessible at the configured IP from the HA host network
