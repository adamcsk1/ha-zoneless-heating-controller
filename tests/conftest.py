"""Test fixtures for the custom integration."""

import json

import pytest
from homeassistant.helpers.storage import Store
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.zoneless_heating_controller.const import DATA_RUNTIME, DOMAIN

RELAY = "switch.boiler_demand"
OUTDOOR = "sensor.outdoor_temperature"
ROOM = "sensor.room_temperature"


@pytest.fixture(autouse=True)
def custom_integrations(enable_custom_integrations):
    return enable_custom_integrations


@pytest.fixture(autouse=True)
def event_loop_socket(socket_enabled):
    """Windows needs a socketpair for its event loop self-pipe."""
    return socket_enabled


@pytest.fixture
def options():
    return {
        "monitor_only": False,
        "rooms_json": json.dumps([
            {"name": "Room", "entity_id": ROOM, "min": 18, "preferred": 22, "max": 24, "weight": 1}
        ]),
        "min_on_minutes": 1,
        "min_off_minutes": 1,
        "start_threshold": 0.2,
        "stop_threshold": -0.1,
        "max_overshoot": 1.0,
        "sensor_timeout_minutes": 5,
        "healthy_minutes": 1,
    }


@pytest.fixture
async def relay_calls(hass):
    await async_setup_component(hass, "switch", {})
    calls = []

    async def turn_on(call):
        calls.append(call.service)
        hass.states.async_set(RELAY, "on")

    async def turn_off(call):
        calls.append(call.service)
        hass.states.async_set(RELAY, "off")

    hass.services.async_register("switch", "turn_on", turn_on)
    hass.services.async_register("switch", "turn_off", turn_off)
    return calls


async def setup_controller(hass, options, relay_state="off", room_temperature="18", seed_state=True):
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Test heater",
        unique_id=RELAY,
        data={"name": "Test heater", "boiler_relay": RELAY, "outdoor_sensor": OUTDOOR},
        options=options,
    )
    entry.add_to_hass(hass)
    hass.data[DATA_RUNTIME] = "test-runtime"
    if seed_state:
        now = dt_util.utcnow()
        await Store(hass, 1, f"{DOMAIN}.{entry.entry_id}.state").async_save({
            "runtime_id": "test-runtime",
            "mode": "auto",
            "relay_state": relay_state == "on",
            "on_since": now.isoformat() if relay_state == "on" else None,
            "off_since": now.isoformat() if relay_state == "off" else None,
            "on_blocked_until": None,
            "force_off": False,
            "shutdown_latched": False,
            "on_uncertain": False,
            "recovery_required": False,
        })
    hass.states.async_set(RELAY, relay_state)
    hass.states.async_set(OUTDOOR, "5", {"unit_of_measurement": "°C"})
    hass.states.async_set(ROOM, room_temperature, {"unit_of_measurement": "°C"})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry, hass.data[DOMAIN][entry.entry_id]
