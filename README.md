# Zoneless Heating Controller

A [Home Assistant](https://www.home-assistant.io/) integration for **one ON/OFF boiler-demand relay**, an outdoor temperature sensor, and rooms with manually balanced heating circuits. Room temperatures produce a weighted heating demand with hysteresis, outdoor compensation, and anti-cycling protection.

The controller provides a **Control mode** selector with **Auto**, **Manual ON**, and **Manual OFF**, plus demand and decision diagnostics. It has no bundled dashboard card; use Home Assistant's standard cards.

> **Start in monitor-only mode.** This is not a certified safety controller. Never bypass boiler controls, pump overrun, overtemperature or frost protection, or mixing-valve safeguards. Have the relay wiring and settings checked by a qualified heating technician before enabling live control.

## Requirements

- Home Assistant **2026.10.0 or newer**
- A manufacturer-supported heating-demand input and compatible relay; **not boiler mains power**
- Independent boiler anti-cycling, pump-overrun, overtemperature, and frost protection
- Temperature sensors that report periodically, even when their values do not change
- No other controller or automation operating the same relay

The integration cannot guarantee OFF after a Home Assistant crash, power failure, network failure, or welded relay contact. Prefer a relay that defaults to OFF after power loss and has a device-side watchdog.

## Install with HACS

1. Add `https://github.com/adamcsk1/ha-zoneless-heating-controller` as a custom **Integration** repository in [HACS](https://www.hacs.xyz/), or use [this link](https://my.home-assistant.io/redirect/hacs_repository/?owner=adamcsk1&repository=ha-zoneless-heating-controller&category=integration).
2. Install **Zoneless Heating Controller** and restart Home Assistant.
3. Go to **Settings > Devices & services > Add integration**, then select **Zoneless Heating Controller**.
4. Select the boiler-demand switch and outdoor temperature sensor.

Restart Home Assistant after every update before opening the configuration dialog.

### Manual install

Copy `custom_components/zoneless_heating_controller/` into `/config/custom_components/zoneless_heating_controller/`, restart Home Assistant, then add the integration through **Settings > Devices & services**.

## Quick start

1. Leave **Monitor only** enabled while checking readings and diagnostics.
2. Open the integration's **Configure** dialog and add your rooms using the JSON below.
3. Add the **Control mode**, **Heating demand**, and **Decision reason** entities to a dashboard. The physical relay should also be visible.
4. Compare calculations with your existing heating behavior. Check that every sensor reports more often than the configured timeout.
5. Disable other automations controlling the relay before turning off monitor-only mode. Keep independent boiler safety controls enabled.

Changing from live control to monitor-only first withdraws active demand and requires confirmed OFF. If OFF confirmation fails, the option change is refused; Manual OFF retries remain active and Auto or Manual ON changes are rejected until the old controller is discarded.

### Room configuration

Paste a JSON array into **Rooms JSON**. Replace the entity IDs and temperatures with your requirements.

```json
[
  {
    "name": "Living room",
    "entity_id": "sensor.living_temperature",
    "min": 20,
    "preferred": 21,
    "max": 22,
    "weight": 2
  },
  {
    "name": "Bedroom",
    "entity_id": "sensor.bedroom_temperature",
    "min": 18,
    "preferred": 19,
    "max": 21,
    "weight": 1
  }
]
```

| Field | Meaning |
|---|---|
| `name` | Display name used in diagnostics. |
| `entity_id` | Unique temperature sensor entity ID. |
| `min` | Temperature mapped to full heating demand. |
| `preferred` | Temperature mapped to neutral demand. |
| `max` | Temperature mapped to full stop demand. |
| `weight` | Room priority, greater than 0 and at most 100. Higher values increase influence. |

Room values are in Celsius and must satisfy `min < preferred < max`. Sensor readings in Celsius, Fahrenheit, or Kelvin are converted automatically. Malformed JSON, invalid sensors, duplicate room sensors, and non-finite or boolean numbers are rejected.

### Dashboard and control modes

Add an **Entities** card and select the integration's **Control mode**, **Heating demand**, and **Decision reason** entities, or edit this example to match your entity IDs:

```yaml
type: entities
title: Heating controller
entities:
  - entity: select.zoneless_heating_control_mode
    name: Mode
  - entity: sensor.zoneless_heating_heating_demand
    name: Heating demand
  - entity: sensor.zoneless_heating_decision_reason
    name: Status
  - entity: switch.boiler_demand
    name: Physical relay
```

The **Control mode** selector is also available under the integration entry's device and entity list. No extra dashboard integration is needed.

| Mode | Behavior |
|---|---|
| **Auto** | Uses room demand, outdoor compensation, hysteresis, and cycle limits. |
| **Manual ON** | Requests continuous heating demand; safety checks, recovery, and cycle delays still apply. |
| **Manual OFF** | Immediately withdraws demand and suspends automatic control. |

The selected mode persists across restarts and reloads. **Monitor only** prevents relay commands in every mode. The legacy **Controller enabled** switch remains: ON selects Auto; OFF selects Manual OFF.

## How it works

Each room produces a score from `-1` to `+1`:

- `+1`: at or below its minimum temperature.
- `0`: at its preferred temperature.
- `-1`: at or above its maximum temperature.

Scores are weighted by room priority. Auto starts above the start threshold and stops below the stop threshold; hysteresis retains the current state between them. Outdoor temperature adjusts the start threshold by `-0.05` below 0°C and `+0.05` at or above 10°C.

A single shared relay cannot maintain an exact range in every room. Correct circuit balancing remains necessary.

| Setting | Default | Purpose |
|---|---:|---|
| Monitor only | `true` | Calculate diagnostics without sending relay commands. |
| Minimum normal ON time | 10 minutes | Prevents normal Auto stops from shortening a heating cycle. |
| Minimum OFF time | 10 minutes | Delays every new ON request after confirmed or uncertain OFF history. |
| Start threshold | `0.20` | Demand needed to start before outdoor adjustment. |
| Stop threshold | `-0.10` | Demand that requests a normal stop. |
| Maximum overshoot | 1.0°C | Excess above any room maximum that activates the overheat guard. |
| Sensor reporting timeout | 4 hours | Maximum time since every required sensor last reported; configurable from 1 minute to 24 hours. |
| Healthy-input recovery time | 5 minutes | Continuous valid-input period required after a fault. |

Use the timings required by the boiler manufacturer. The boiler may cycle its burner internally while demand remains ON.

## Fault behavior

| Situation | Live-control behavior |
|---|---|
| Invalid, unavailable, or stale required sensor | Demand becomes unknown; requests OFF immediately and waits for confirmed OFF before recovery. |
| Room overheat guard | Requests OFF immediately, including during minimum ON. |
| Manual OFF | Requests OFF immediately. |
| Relay unavailable or command unconfirmed | Shows a fault; retries OFF every 30 seconds. |
| Inputs recover after a fault | Waits for the complete healthy-input period and minimum OFF period. |
| Integration unload or graceful HA stop | Attempts OFF before releasing control. |
| Monitor only | Never sends relay commands, including during faults. |

Sensor faults override minimum ON and apply in Manual ON. After sensors recover, heating still waits for the recovery and minimum OFF periods. The **Decision reason** sensor and its attributes identify the active fault; `sensor_fault` gives the sensor-specific cause.

Cycle timers use confirmed relay state changes. Cold starts require a fresh OFF observation and full OFF period. Failed or uncertain ON attempts require a successful canceling OFF before another attempt. Unload failures retain Manual OFF retries rather than leaving an orphaned controller.

## Sensor reporting

Freshness uses Home Assistant's `last_reported` timestamp. An unchanged temperature stays healthy while its sensor continues reporting. A frozen value becomes a fault after the timeout. Sensors that report only when values change need a suitably large timeout or different hardware. Longer timeouts permit decisions based on older temperatures; use the shortest practical timeout for each installation.

This verifies reports inside Home Assistant, not the physical measurement path. A source integration republishing cached values must not be treated as proof of fresh measurements.

## Development and support

Run `python -m pip install -r requirements_test.txt`, then `pytest` and `ruff check .`. GitHub Actions also runs HACS validation and Hassfest.

[Report an issue](https://github.com/adamcsk1/ha-zoneless-heating-controller/issues).

When reporting an issue, include the integration version, Home Assistant version, Decision reason attributes, and relevant logs. Remove credentials and personal information.

To remove the integration, select **Manual OFF**, verify the physical relay is OFF, remove its entry, uninstall it, restart Home Assistant, and restore an approved alternative heating controller.

## License

[MIT](LICENSE)
