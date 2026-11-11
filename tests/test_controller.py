"""Safety, timing, and mode tests for the heating controller."""

import asyncio
from datetime import timedelta

import pytest
from conftest import OUTDOOR, RELAY, ROOM, setup_controller
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.zoneless_heating_controller.const import DOMAIN, MODE_AUTO, MODE_MANUAL_OFF, MODE_MANUAL_ON


async def advance(hass, freezer, seconds=61):
    freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


async def test_auto_waits_for_minimum_off(hass, options, relay_calls, freezer):
    _, controller = await setup_controller(hass, options)
    assert relay_calls == []
    assert controller.reason == "Minimum OFF time"
    await advance(hass, freezer, 59)
    assert relay_calls == []
    await advance(hass, freezer, 2)
    assert relay_calls == ["turn_on"]
    assert hass.states.get(RELAY).state == "on"


async def test_normal_stop_respects_minimum_on(hass, options, relay_calls, freezer):
    _, controller = await setup_controller(hass, options, relay_state="on")
    hass.states.async_set(ROOM, "24.5", {"unit_of_measurement": "°C"})
    await hass.async_block_till_done()
    assert relay_calls == []
    assert controller.reason == "Minimum ON time"
    await advance(hass, freezer, 61)
    assert relay_calls == ["turn_off"]


async def test_attribute_update_does_not_reset_cycle_timer(hass, options, relay_calls, freezer):
    await setup_controller(hass, options, relay_state="on")
    await advance(hass, freezer, 30)
    hass.states.async_set(RELAY, "on", {"friendly_name": "Boiler"})
    hass.states.async_set(ROOM, "24.5", {"unit_of_measurement": "°C"})
    await hass.async_block_till_done()
    await advance(hass, freezer, 31)
    assert relay_calls == ["turn_off"]


async def test_sensor_fault_turns_off_and_delays_recovery(hass, options, relay_calls, freezer):
    options["healthy_minutes"] = 2
    _, controller = await setup_controller(hass, options, relay_state="on")
    hass.states.async_set(ROOM, "unavailable", {"unit_of_measurement": "°C"})
    await hass.async_block_till_done()
    assert relay_calls == ["turn_off"]
    assert controller.reason == "Sensor fault"

    hass.states.async_set(ROOM, "18", {"unit_of_measurement": "°C"})
    await advance(hass, freezer, 61)
    assert relay_calls == ["turn_off"]
    assert controller.reason == "Recovery wait"
    await advance(hass, freezer, 61)
    assert relay_calls == ["turn_off", "turn_on"]


async def test_monitor_only_retains_sensor_fault_diagnostics(hass, options):
    options["monitor_only"] = True
    _, controller = await setup_controller(hass, options)
    assert controller.reason == "Minimum OFF time"

    hass.states.async_set(ROOM, "unavailable", {"unit_of_measurement": "°C"})
    await hass.async_block_till_done()
    assert controller.demand is None
    assert controller.reason == "Sensor fault"
    assert ROOM in controller.sensor_fault
    assert "monitor only" in controller.detail

    hass.states.async_set(ROOM, "18", {"unit_of_measurement": "°C"})
    await hass.async_block_till_done()
    assert controller.demand == 1
    assert controller.reason == "Recovery wait"


async def test_stale_sensor_turns_off(hass, options, relay_calls, freezer):
    options["sensor_timeout_minutes"] = 1
    _, controller = await setup_controller(hass, options, relay_state="on")
    await advance(hass, freezer, 61)
    assert relay_calls == ["turn_off"]
    assert "has not reported" in controller.sensor_fault


async def test_unchanged_sensor_report_is_fresh(hass, options, relay_calls, freezer):
    options["sensor_timeout_minutes"] = 1
    _, controller = await setup_controller(hass, options, relay_state="on")
    await advance(hass, freezer, 30)
    for entity in (ROOM, OUTDOOR):
        state = hass.states.get(entity)
        hass.states.async_set(entity, state.state, dict(state.attributes), force_update=True)
    await hass.async_block_till_done()
    await advance(hass, freezer, 31)
    assert relay_calls == []
    assert controller.sensor_fault is None


async def test_overheat_bypasses_minimum_on_but_not_next_off_period(hass, options, relay_calls, freezer):
    options["healthy_minutes"] = 2
    _, controller = await setup_controller(hass, options, relay_state="on")
    hass.states.async_set(ROOM, "25", {"unit_of_measurement": "°C"})
    await hass.async_block_till_done()
    assert relay_calls == ["turn_off"]
    assert controller.reason == "Overheat protection"
    hass.states.async_set(ROOM, "18", {"unit_of_measurement": "°C"})
    await advance(hass, freezer, 59)
    assert relay_calls == ["turn_off"]
    await advance(hass, freezer, 3)
    assert relay_calls == ["turn_off"]  # Healthy-input recovery is still pending.


async def test_manual_modes_and_existing_switch(hass, options, relay_calls, freezer):
    _, controller = await setup_controller(hass, options, room_temperature="23.5")
    await controller.async_set_mode(MODE_MANUAL_ON)
    assert relay_calls == []
    assert controller.reason == "Minimum OFF time"
    await advance(hass, freezer, 61)
    assert relay_calls == ["turn_on"]

    await controller.async_set_mode(MODE_MANUAL_OFF)
    assert relay_calls == ["turn_on", "turn_off"]
    assert controller.mode == MODE_MANUAL_OFF
    assert not controller.enabled
    await controller.async_set_enabled(True)
    assert controller.mode == MODE_AUTO
    assert relay_calls == ["turn_on", "turn_off"]


async def test_manual_on_retains_sensor_and_overheat_protection(hass, options, relay_calls, freezer):
    _, controller = await setup_controller(hass, options, relay_state="on")
    await controller.async_set_mode(MODE_MANUAL_ON)
    hass.states.async_set(ROOM, "25", {"unit_of_measurement": "°C"})
    await hass.async_block_till_done()
    assert relay_calls == ["turn_off"]
    assert controller.mode == MODE_MANUAL_ON
    assert controller.reason == "Overheat protection"


async def test_monitor_only_never_calls_relay(hass, options, relay_calls, freezer):
    options["monitor_only"] = True
    _, controller = await setup_controller(hass, options, relay_state="on")
    await controller.async_set_mode(MODE_MANUAL_ON)
    hass.states.async_set(ROOM, "unavailable")
    await advance(hass, freezer, 61)
    assert relay_calls == []
    assert controller.reason == "Sensor fault"
    assert controller.detail.endswith("; monitor only: no relay commands")


async def test_fahrenheit_sensor_is_converted(hass, options, relay_calls):
    options["monitor_only"] = True
    hass.states.async_set(ROOM, "68", {"unit_of_measurement": "°F"})
    _, controller = await setup_controller(hass, options)
    # setup_controller resets the room to Celsius; set Fahrenheit afterward.
    hass.states.async_set(ROOM, "68", {"unit_of_measurement": "°F"})
    await hass.async_block_till_done()
    assert controller.demand == pytest.approx(0.5)


async def test_mode_and_cooldown_survive_reload(hass, options, relay_calls, freezer):
    entry, controller = await setup_controller(hass, options, relay_state="on")
    await controller.async_set_mode(MODE_MANUAL_OFF)
    relay_calls.clear()
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    restored = hass.data[DOMAIN][entry.entry_id]
    assert restored.mode == MODE_MANUAL_OFF
    assert relay_calls == []
    await restored.async_set_mode(MODE_AUTO)
    assert relay_calls == []
    assert restored.reason == "Minimum OFF time"
    await advance(hass, freezer, 61)
    assert relay_calls == ["turn_on"]


async def test_unload_turns_off_live_relay(hass, options, relay_calls):
    entry, _ = await setup_controller(hass, options, relay_state="on")
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert relay_calls == ["turn_off"]


async def test_failed_off_is_retried(hass, options, relay_calls, freezer, monkeypatch):
    _, controller = await setup_controller(hass, options, relay_state="on")

    async def fail_off(call):
        relay_calls.append("turn_off")
        raise HomeAssistantError("offline")

    hass.services.async_remove("switch", "turn_off")
    hass.services.async_register("switch", "turn_off", fail_off)
    hass.states.async_set(ROOM, "unavailable")
    await hass.async_block_till_done()
    assert relay_calls == ["turn_off"]
    assert "turn_off failed" in controller.command_error
    await advance(hass, freezer, 29)
    assert relay_calls == ["turn_off"]
    await advance(hass, freezer, 2)
    assert relay_calls == ["turn_off", "turn_off"]

    async def restored_off(call):
        relay_calls.append("turn_off")
        hass.states.async_set(RELAY, "off")

    hass.services.async_remove("switch", "turn_off")
    hass.services.async_register("switch", "turn_off", restored_off)


async def test_unconfirmed_on_is_not_retried_immediately(hass, options, relay_calls, freezer, monkeypatch):
    monkeypatch.setattr("custom_components.zoneless_heating_controller.controller.COMMAND_TIMEOUT_SECONDS", 0.01)
    hass.services.async_remove("switch", "turn_on")

    async def no_confirmation(call):
        relay_calls.append("turn_on")

    hass.services.async_register("switch", "turn_on", no_confirmation)
    _, controller = await setup_controller(hass, options)
    await advance(hass, freezer, 61)
    await advance(hass, freezer, 0.02)
    assert relay_calls == ["turn_on", "turn_off"]
    assert controller._on_uncertain is False
    assert controller.reason == "Minimum OFF time"


async def test_manual_off_arriving_during_on_command_wins(hass, options, relay_calls, freezer):
    started = asyncio.Event()
    release = asyncio.Event()
    hass.services.async_remove("switch", "turn_on")

    async def delayed_on(call):
        relay_calls.append("turn_on")
        started.set()
        await release.wait()
        hass.states.async_set(RELAY, "on")

    hass.services.async_register("switch", "turn_on", delayed_on)
    _, controller = await setup_controller(hass, options)
    freezer.tick(timedelta(seconds=61))
    async_fire_time_changed(hass)
    await started.wait()
    await controller.async_set_mode(MODE_MANUAL_OFF)
    release.set()
    await hass.async_block_till_done()
    assert relay_calls == ["turn_on", "turn_off"]
    assert hass.states.get(RELAY).state == "off"
    assert controller.mode == MODE_MANUAL_OFF
