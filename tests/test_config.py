"""Configuration normalization and flow tests."""

import json

import probatio
import pytest
from homeassistant.data_entry_flow import InvalidData
from homeassistant.helpers import config_validation as cv

from custom_components.zoneless_heating_controller.config import normalize_options, normalize_rooms
from custom_components.zoneless_heating_controller.const import DOMAIN, OPTION_RANGES

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
    assert normalize_options({})["sensor_timeout_minutes"] == 240


def test_sensor_timeout_allows_up_to_24_hours():
    assert normalize_options({"sensor_timeout_minutes": 1440})["sensor_timeout_minutes"] == 1440
    with pytest.raises(ValueError, match="sensor_timeout_minutes must be between"):
        normalize_options({"sensor_timeout_minutes": 1441})


async def test_options_flow_normalizes_numeric_strings(hass):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(domain=DOMAIN, data={}, options={})
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == "form"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {
        "monitor_only": True,
        "rooms_json": [{**VALID_ROOM, "min": "18"}],
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


async def test_options_form_serializes_for_frontend(hass):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(domain=DOMAIN, data={}, options={"min_on_minutes": 25})
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    fields = probatio.to_field_list(result["data_schema"], custom_serializer=cv.custom_serializer)
    fields = {field["name"]: field for field in fields}
    assert fields["min_on_minutes"]["default"] == 25
    rooms = fields["rooms_json"]
    assert rooms["default"] == []
    room_selector = rooms["selector"]["object"]
    assert room_selector["multiple"] is True
    assert room_selector["label_field"] == "name"
    assert set(room_selector["fields"]) == set(VALID_ROOM)
    for key in ("min", "preferred", "max"):
        number = room_selector["fields"][key]["selector"]["number"]
        assert number["unit_of_measurement"] == "°C"
        assert number["step"] == "any"
    entity = room_selector["fields"]["entity_id"]["selector"]["entity"]
    assert entity["domain"] == ["sensor"]
    assert entity["device_class"] == ["temperature"]
    for key, (minimum, maximum, value_type) in OPTION_RANGES.items():
        number = fields[key]["selector"]["number"]
        assert number["min"] == minimum
        assert number["max"] == maximum
        assert number["step"] == (1 if value_type is int else "any")


@pytest.mark.parametrize("value", [True, 1.5, "nan", "inf", 0, 181])
def test_invalid_timer_options_rejected(value):
    with pytest.raises(ValueError):
        normalize_options({"min_on_minutes": value})


async def test_room_editor_load_edit_add_remove_and_reopen(hass):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(domain=DOMAIN, data={}, options={
        "rooms_json": json.dumps([VALID_ROOM]), "monitor_only": True,
    })
    entry.add_to_hass(hass)
    expected = [VALID_ROOM]
    edited = {**VALID_ROOM, "preferred": 22.25}
    added = {**VALID_ROOM, "name": "Bedroom", "entity_id": "sensor.bedroom"}
    for rooms in ([edited, added], [added], []):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        fields = probatio.to_field_list(result["data_schema"], custom_serializer=cv.custom_serializer)
        room_field = next(field for field in fields if field["name"] == "rooms_json")
        assert room_field["default"] == expected
        submitted = result["data_schema"]({"rooms_json": rooms, "monitor_only": True})
        result = await hass.config_entries.options.async_configure(result["flow_id"], submitted)
        assert result["type"] == "create_entry"
        assert json.loads(entry.options["rooms_json"]) == rooms
        expected = rooms
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["data_schema"]({})["rooms_json"] == []


@pytest.mark.parametrize(("rooms", "message"), [
    ([{**VALID_ROOM, "preferred": 25}], "Room 1 needs min < preferred < max"),
    ([VALID_ROOM, VALID_ROOM], "sensor.room is configured more than once"),
    ([{**VALID_ROOM, "weight": 0}], "Room 1 weight must be between"),
    ([{**VALID_ROOM, "name": " "}], "Room 1 needs a name"),
])
async def test_room_editor_errors_keep_edits(hass, rooms, message):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    saved = {"rooms_json": json.dumps([VALID_ROOM]), "monitor_only": True}
    entry = MockConfigEntry(domain=DOMAIN, data={}, options=saved)
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {
        "rooms_json": rooms, "monitor_only": True,
    })
    assert result["type"] == "form"
    assert result["errors"] == {"base": "invalid_rooms"}
    assert message in result["description_placeholders"]["error"]
    fields = probatio.to_field_list(result["data_schema"], custom_serializer=cv.custom_serializer)
    assert next(field for field in fields if field["name"] == "rooms_json")["default"] == rooms
    assert entry.options == saved


@pytest.mark.parametrize(("field", "value"), [("weight", True), ("min", "nan"), ("max", "inf")])
async def test_room_editor_rejects_invalid_numbers(hass, field, value):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(domain=DOMAIN, data={}, options={})
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    with pytest.raises(InvalidData):
        await hass.config_entries.options.async_configure(result["flow_id"], {
            "rooms_json": [{**VALID_ROOM, field: value}], "monitor_only": True,
        })
    assert entry.options == {}
