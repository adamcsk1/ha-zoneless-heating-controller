"""Configuration normalization and flow tests."""

import json

import pytest

from custom_components.zoneless_heating_controller.config import normalize_options, normalize_rooms
from custom_components.zoneless_heating_controller.const import DOMAIN

VALID_ROOM = {"name": "Room", "entity_id": "sensor.room", "min": 18, "preferred": 21, "max": 24, "weight": 1}


def test_numeric_room_strings_are_normalized():
    rooms = normalize_rooms(json.dumps([{**VALID_ROOM, "min": "18", "weight": "1.5"}]))
    assert rooms[0]["min"] == 18
    assert rooms[0]["weight"] == 1.5


@pytest.mark.parametrize("value", [True, "nan", "inf", "-inf", None])
def test_non_finite_or_boolean_weight_rejected(value):
    with pytest.raises(ValueError):
        normalize_rooms([{**VALID_ROOM, "weight": value}])


def test_duplicate_room_sensor_rejected():
    with pytest.raises(ValueError, match="more than once"):
        normalize_rooms([VALID_ROOM, VALID_ROOM])


def test_invalid_temperature_range_rejected():
    with pytest.raises(ValueError):
        normalize_rooms([{**VALID_ROOM, "preferred": 25}])


def test_default_options_are_fail_monitor_only():
    assert normalize_options({})["monitor_only"] is True
    assert normalize_options({})["min_on_minutes"] == 10
    assert normalize_options({})["min_off_minutes"] == 10


async def test_options_flow_normalizes_numeric_strings(hass):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(domain=DOMAIN, data={}, options={})
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == "form"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {
        "monitor_only": True,
        "rooms_json": json.dumps([{**VALID_ROOM, "min": "18"}]),
        "min_on_minutes": 10,
        "min_off_minutes": 10,
        "start_threshold": 0.2,
        "stop_threshold": -0.1,
        "max_overshoot": 1,
        "sensor_timeout_minutes": 15,
        "healthy_minutes": 5,
    })
    assert result["type"] == "create_entry"
    assert json.loads(result["data"]["rooms_json"])[0]["min"] == 18
