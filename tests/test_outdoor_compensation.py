"""Continuous outdoor compensation and its diagnostic entity."""

import pytest
from conftest import OUTDOOR, ROOM, setup_controller
from homeassistant.helpers import entity_registry as er

from custom_components.zoneless_heating_controller.const import DOMAIN, MODE_MANUAL_OFF, MODE_MANUAL_ON


@pytest.mark.parametrize(
    ("outside", "expected"),
    [(-20, 0.15), (-0.01, 0.15), (0, 0.15), (0.01, 0.1501),
     (2.5, 0.175), (5, 0.20), (7.5, 0.225), (9.99, 0.2499),
     (10, 0.25), (10.01, 0.25), (30, 0.25)],
)
async def test_continuous_compensation(hass, options, outside, expected):
    options["monitor_only"] = True
    _, controller = await setup_controller(hass, options)
    hass.states.async_set(OUTDOOR, str(outside), {"unit_of_measurement": "°C"})
    await hass.async_block_till_done()
    assert controller.effective_start_threshold == pytest.approx(expected)


async def test_threshold_respects_stop_separation(hass, options):
    options.update(monitor_only=True, start_threshold=0.01, stop_threshold=0)
    _, controller = await setup_controller(hass, options)
    hass.states.async_set(OUTDOOR, "-10", {"unit_of_measurement": "°C"})
    await hass.async_block_till_done()
    assert controller.effective_start_threshold == pytest.approx(0.01)


async def test_start_uses_unrounded_compensated_threshold(hass, options):
    options["monitor_only"] = True
    # Demand 0.17505: above the displayed 0.175, below the actual 0.1751.
    _, controller = await setup_controller(hass, options, room_temperature="21.2998")
    hass.states.async_set(OUTDOOR, "2.51", {"unit_of_measurement": "°C"})
    await hass.async_block_till_done()
    assert controller.reason == "Hysteresis"
    hass.states.async_set(OUTDOOR, "2.5", {"unit_of_measurement": "°C"})
    await hass.async_block_till_done()
    assert controller.reason == "Minimum OFF time"


@pytest.mark.parametrize("failed_sensor", [OUTDOOR, ROOM])
async def test_threshold_sensor_fault_and_recovery(hass, options, failed_sensor):
    options["monitor_only"] = True
    entry, controller = await setup_controller(hass, options)
    entity_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}_effective_start_threshold"
    )
    assert entity_id is not None
    assert controller.reason == "Minimum OFF time"
    assert hass.states.get(entity_id).state == "0.2"

    hass.states.async_set(OUTDOOR, "2.51", {"unit_of_measurement": "°C"})
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == "0.175"
    healthy = hass.states.get(failed_sensor)
    hass.states.async_set(failed_sensor, "unavailable")
    await hass.async_block_till_done()
    assert controller.effective_start_threshold is None
    assert hass.states.get(entity_id).state == "unknown"

    hass.states.async_set(failed_sensor, healthy.state, dict(healthy.attributes))
    await hass.async_block_till_done()
    assert controller.reason == "Recovery wait"
    assert hass.states.get(entity_id).state == "0.175"

    for mode in (MODE_MANUAL_OFF, MODE_MANUAL_ON):
        await controller.async_set_mode(mode)
        await hass.async_block_till_done()
        assert hass.states.get(entity_id).state == "0.175"


async def test_controller_error_clears_threshold(hass, options, monkeypatch):
    options["monitor_only"] = True
    entry, controller = await setup_controller(hass, options)
    entity_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}_effective_start_threshold"
    )
    assert hass.states.get(entity_id).state == "0.2"

    def fail_calculation(now):
        raise RuntimeError("Calculation failed")

    monkeypatch.setattr(controller, "_calculate_demand", fail_calculation)
    await controller.async_evaluate()
    await hass.async_block_till_done()
    assert controller.reason == "Controller error"
    assert controller.effective_start_threshold is None
    assert hass.states.get(entity_id).state == "unknown"
