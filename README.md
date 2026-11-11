# Zoneless Heating Controller

A Home Assistant custom integration for **one ON/OFF boiler-demand relay**, one outdoor temperature sensor, and multiple rooms with manually balanced underfloor-heating circuits.

[![Validate](https://github.com/adamcsk1/ha-zoneless-heating-controller/actions/workflows/validate.yml/badge.svg)](https://github.com/adamcsk1/ha-zoneless-heating-controller/actions/workflows/validate.yml)
[![HACS](https://img.shields.io/badge/HACS-Custom_repository-41BDF5.svg)](https://www.hacs.xyz/docs/use/repositories/type/integration/)

> **Experimental — start in monitor-only mode.** This is not a certified safety controller. Never use it to bypass boiler controls, pump overrun, overtemperature protection, frost protection, or mixing-valve safeguards. Have relay wiring and settings reviewed by a qualified heating technician before enabling live control.

## Features

- Weighted room-temperature demand with hysteresis.
- Modest outdoor-temperature compensation.
- Separate minimum ON and OFF periods to prevent boiler-demand short cycling.
- Persistent **Auto**, **Manual ON**, and **Manual OFF** modes.
- Immediate demand withdrawal for sensor faults, overheating, Manual OFF, and graceful shutdown.
- Sensor reporting timeouts and delayed recovery after faults.
- Celsius, Fahrenheit, and Kelvin sensor support.
- Relay command timeouts, state confirmation, and bounded OFF retries.
- Monitor-only mode that never sends relay commands.
- Demand and decision diagnostics.
- UI configuration; no YAML or external binary helper required.

## How it works

Each room produces a normalized score from `-1` to `+1`:

- `+1`: room is at or below its minimum temperature.
- `0`: room is at its preferred temperature.
- `-1`: room is at or above its maximum temperature.

Scores are weighted by room importance. Auto mode requests heat above the start threshold and stops below the stop threshold. Between them, hysteresis preserves the current heating state.

Outdoor temperature adjusts the start threshold by `-0.05` below 0°C and by `+0.05` at or above 10°C. Room settings are always entered in Celsius; sensor readings are converted to Celsius.

## Safety and hardware requirements

**Required before live control:**

1. A manufacturer-supported heating-demand input and compatible relay. The relay must control demand, **not boiler mains power**.
2. Independent boiler anti-cycling, pump-overrun, overtemperature, and frost-protection systems.
3. Local temperature sensors that report periodically, even when their values do not change.
4. Preferably a relay that returns to OFF after power loss and has a device-side watchdog/auto-OFF if Home Assistant stops communicating.
5. Only one controller or automation operating the demand relay.

The integration can request OFF only while Home Assistant, the relay integration, and communication still work. A Home Assistant crash, network failure, power failure, or welded relay contact requires independent hardware protection. The selected mode and cycle timestamps cannot protect against those failures by themselves.

Exact relay compatibility and minimum cycle timings depend on the boiler manual. The boiler may cycle its burner internally while demand remains ON.

## Installation

### HACS custom repository

This repository follows the HACS integration layout. It is not automatically included in the HACS default catalog.

1. Ensure Home Assistant **2026.10.0 or newer** and HACS are installed.
2. Open [Add custom repository to HACS](https://my.home-assistant.io/redirect/hacs_repository/?owner=adamcsk1&repository=ha-zoneless-heating-controller&category=integration), or manually add `https://github.com/adamcsk1/ha-zoneless-heating-controller` as an **Integration** custom repository.
3. Download **Zoneless Heating Controller**.
4. Restart Home Assistant.
5. Go to **Settings → Devices & services → Add integration → Zoneless Heating Controller**.

### Manual installation

1. Copy `custom_components/zoneless_heating_controller` into `/config/custom_components/`.
2. Restart Home Assistant.
3. Add the integration from **Settings → Devices & services**.

## Initial setup

1. Select the **boiler-demand switch** and **outdoor temperature sensor**.
2. Open the integration's **Configure** dialog.
3. Add rooms using the JSON format below.
4. Leave **Monitor only** enabled initially.
5. Add the demand and decision-reason sensors to a dashboard and compare calculations with your existing heating behavior.
6. Check that all sensors report more frequently than the configured timeout.
7. Disable existing boiler-demand automations before turning off monitor-only mode. Do not disable independent boiler safety controls.

Changing from live control to monitor-only first withdraws active demand and requires confirmed OFF before the option is saved. Any uncertain ON delivery must also be canceled by a successful OFF. If confirmation fails, the option change is refused and the existing live controller remains visible in Manual OFF with bounded OFF retries. During this transition, Auto and Manual ON changes are rejected until the old controller is discarded.

## Room configuration

```json
[
  {
    "name": "Living room",
    "entity_id": "sensor.living_temperature",
    "min": 20.5,
    "preferred": 21.5,
    "max": 22.5,
    "weight": 1.5
  },
  {
    "name": "Bedroom",
    "entity_id": "sensor.bedroom_temperature",
    "min": 18.0,
    "preferred": 19.0,
    "max": 21.0,
    "weight": 1.0
  },
  {
    "name": "Bathroom",
    "entity_id": "sensor.bathroom_temperature",
    "min": 21.0,
    "preferred": 22.0,
    "max": 24.0,
    "weight": 1.2
  }
]
```

| Field | Meaning |
|---|---|
| `name` | Non-empty display name used in diagnostics. |
| `entity_id` | Temperature sensor. Each sensor may appear only once. |
| `min` | Temperature mapped to full heating demand. |
| `preferred` | Temperature mapped to neutral demand. |
| `max` | Temperature mapped to full stop demand. |
| `weight` | Relative room importance, greater than 0 and at most 100. |

Require `min < preferred < max`. Numeric JSON strings are accepted and normalized. Non-finite values, booleans used as numbers, malformed JSON, invalid entities, and duplicate room sensors are rejected.

## Settings

| Setting | Default | Purpose |
|---|---:|---|
| Monitor only | `true` | Calculate diagnostics without sending any relay commands. |
| Minimum normal ON time | 10 minutes | Prevents normal Auto stops from shortening a heating cycle. |
| Minimum OFF time | 10 minutes | Delays every new ON request after confirmed or uncertain OFF history. |
| Start threshold | `0.20` | Demand needed to start heating before outdoor adjustment. |
| Stop threshold | `-0.10` | Demand that requests a normal stop. |
| Maximum overshoot | 1.0°C | Excess above any room maximum that activates the overheat guard. |
| Sensor reporting timeout | 15 minutes | Maximum time since every required sensor last reported. |
| Healthy-input recovery time | 5 minutes | Continuous valid-input period required after a fault. |

Customized timings are retained when options are saved. Earlier MVP defaults were 20 minutes ON and 15 minutes OFF; new installations use 10 minutes for both. Increase these values if required by the boiler manufacturer.

### Freshness versus temperature changes

Freshness uses Home Assistant's `last_reported` timestamp. An unchanged temperature remains healthy if the sensor continues reporting. A frozen numeric value becomes a fault when no reports arrive before the timeout. Future timestamps caused by backward clock adjustments are also faults. Sensors that report only on temperature changes need a suitably large timeout or different hardware. This verifies freshness of reports inside Home Assistant; a source integration that republishes cached values must not be treated as proof of fresh physical measurements.

## Control modes

Use the **Control mode** selector on the integration device page or a dashboard.

| Mode | Behavior |
|---|---|
| **Auto** | Uses room demand, outdoor compensation, hysteresis, and cycle limits. |
| **Manual ON** | Requests continuous demand and suspends automatic comfort-demand decisions. Sensor faults, reporting timeouts, overheat protection, minimum OFF, and recovery delays still apply. |
| **Manual OFF** | Immediately withdraws demand and keeps automatic control suspended. |

The selected mode persists across restart and reload. Changing modes never resets confirmed cycle timers. Manual ON means heating **demand**, not a guarantee that the boiler burner is running.

The original **Controller enabled** switch remains for compatibility. ON selects Auto; OFF selects Manual OFF. The switch is shown as enabled in Manual ON as well.

## Anti-cycling and fault behavior

Cycle timing starts from confirmed relay state changes, not service requests. Attribute-only updates do not reset it. Timestamps are persisted, but a cold Home Assistant start never trusts stored relay history. Cold start requires a new OFF observation and fresh minimum OFF period, including when storage is missing or malformed. Every completed reload also starts a fresh OFF observation.

| Situation | Live-control behavior |
|---|---|
| Normal Auto stop | Waits for minimum ON time. |
| Any new ON request | Waits for minimum OFF time and recalculates current demand before acting. |
| Invalid, unavailable, or expired required sensor | Requests OFF immediately and latches shutdown until OFF is confirmed; then recovery and minimum OFF apply. |
| Room overheat guard | Requests OFF immediately, including during minimum ON. |
| Manual OFF | Requests OFF immediately. |
| Relay unavailable or command unconfirmed | Shows a fault; OFF requests are retried every 30 seconds. Failed or uncertain ON attempts require a successful canceling OFF and a full OFF period before another attempt. |
| Inputs recover after a fault | Waits for the complete healthy-input period and minimum OFF period. |
| Integration unload or graceful HA stop | Attempts OFF before releasing control. Failed unload stays active in Manual OFF and keeps retrying. |
| Monitor-only | Never sends relay commands, regardless of mode or fault. |

A fault can shorten an ON cycle, but it cannot cause an immediate restart. Normal stops during an unconfirmed ON command wait for confirmation or timeout; if the relay turns ON late, minimum ON still applies.

## Entities

| Entity | Purpose |
|---|---|
| **Control mode** | Select Auto, Manual ON, or Manual OFF. |
| **Controller enabled** | Backwards-compatible Auto/Manual OFF switch. |
| **Heating demand** | Weighted normalized demand, normally from `-1` to `+1`. |
| **Decision reason** | Short current state, such as `Heating demand`, `Minimum OFF time`, `Recovery wait`, or `Sensor fault`. |

Decision-reason attributes include the selected mode, requested and reported relay states, detailed fault, stale/invalid sensor, overheated rooms, pending command, command error, cycle timestamps, recovery deadline, and next scheduled evaluation.

Example dashboard card:

```yaml
type: entities
title: Heating controller
entities:
  - entity: select.zoneless_heating_control_mode
  - entity: sensor.zoneless_heating_heating_demand
  - entity: sensor.zoneless_heating_decision_reason
  - entity: switch.boiler_demand
```

Adjust entity IDs to your installation. Always show the physical relay state alongside the requested state.

## Troubleshooting

- **Minimum OFF time:** wait for the displayed deadline. Restarting or changing modes does not bypass it.
- **Recovery wait:** all required sensors and the relay must be healthy continuously for the configured recovery period.
- **Sensor fault:** inspect `sensor_fault` and `detail` attributes. Check units, availability, numeric value, and report freshness.
- **Relay fault or command error:** check the relay integration and physical relay. OFF retries continue, but independent hardware protection is still required.
- **Manual ON does not start immediately:** minimum OFF, recovery, and overheat/sensor protections still apply.
- **Unexpected heating:** use Manual OFF and verify the physical relay state. Check for another automation controlling the same relay. If necessary, use the manufacturer-approved emergency procedure—not this software alone.

## Removal

1. Select **Manual OFF** and verify that the physical relay is OFF.
2. Remove the integration entry.
3. Remove the integration from HACS, or delete its directory for a manual installation.
4. Restart Home Assistant and restore an approved alternative heating controller.

The integration attempts OFF during unload. If OFF cannot be confirmed, unload is refused and Manual OFF retries remain active so failure is visible rather than silently orphaned. A state-storage timeout also refuses normal unload, retaining the existing controller and writer instead of creating overlapping replacements. Final Home Assistant shutdown remains bounded.

## Development and validation

```bash
python -m pip install -r requirements_test.txt
pytest
```

Tests cover demand validation, unit conversion, anti-cycling, stale sensors, overheat shutdown, recovery, manual modes, persistence, failed commands, and command/disable races. GitHub Actions runs tests, HACS validation, and Hassfest.

The automated suite uses Home Assistant 2026.10.4. No physical boiler or relay has been tested. Validate on a test Home Assistant instance and have the complete heating installation checked before production use.

## Support

Report issues at [GitHub Issues](https://github.com/adamcsk1/ha-zoneless-heating-controller/issues). Include the integration version, Home Assistant version, decision-reason attributes, and relevant logs. Remove personal information and never include credentials.
