"""Regression tests from the principal and independent safety reviews."""

import asyncio
import json
from datetime import timedelta

import pytest
from conftest import OUTDOOR, RELAY, ROOM, setup_controller
from homeassistant.config_entries import ConfigEntryError
from homeassistant.data_entry_flow import InvalidData
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.zoneless_heating_controller import async_setup_entry, async_unload_entry
from custom_components.zoneless_heating_controller.const import DOMAIN, MODE_MANUAL_OFF, MODE_MANUAL_ON
from custom_components.zoneless_heating_controller.controller import HeatingController


async def advance(hass, freezer, seconds=61):
    freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


def option_input(options, **changes):
    return {**options, **changes}


async def test_cold_start_discards_unobserved_cycle_history(hass, options, relay_calls):
    entry, controller = await setup_controller(hass, options)
    assert await controller.async_stop(final=True)

    restored = HeatingController(hass, entry)
    await restored.async_load("a-different-ha-process")
    assert restored._force_off is True
    assert restored._relay_state is None
    assert restored._on_since is None
    assert restored._off_since is None


async def test_failed_on_cooldown_is_persisted(hass, options, relay_calls, freezer, monkeypatch):
    monkeypatch.setattr("custom_components.zoneless_heating_controller.controller.COMMAND_TIMEOUT_SECONDS", 0.01)
    hass.services.async_remove("switch", "turn_on")

    async def no_confirmation(call):
        relay_calls.append("turn_on")

    hass.services.async_register("switch", "turn_on", no_confirmation)
    entry, controller = await setup_controller(hass, options)
    await advance(hass, freezer, 61)
    await advance(hass, freezer, 0.02)
    assert controller._on_blocked_until is not None

    saved = await controller._store.async_load()
    assert saved["on_blocked_until"] is not None
    restored = HeatingController(hass, entry)
    await restored.async_load(controller._runtime_id)
    assert restored._on_blocked_until == controller._on_blocked_until


async def test_fault_latches_until_off_before_recovery(hass, options, relay_calls, freezer):
    options["healthy_minutes"] = 2
    _, controller = await setup_controller(hass, options, relay_state="on")

    async def fail_off(call):
        relay_calls.append("turn_off")
        raise HomeAssistantError("offline")

    hass.services.async_remove("switch", "turn_off")
    hass.services.async_register("switch", "turn_off", fail_off)
    hass.states.async_set(ROOM, "unavailable")
    await hass.async_block_till_done()
    assert controller._shutdown_latched is True

    # Recovery must not cancel an unconfirmed fault shutdown.
    hass.states.async_set(ROOM, "18", {"unit_of_measurement": "°C"})
    await hass.async_block_till_done()
    assert controller._shutdown_latched is True
    assert controller.request_heat is False
    assert controller.reason == "OFF retry pending"

    async def restored_off(call):
        relay_calls.append("turn_off")
        hass.states.async_set(RELAY, "off")

    hass.services.async_remove("switch", "turn_off")
    hass.services.async_register("switch", "turn_off", restored_off)
    await advance(hass, freezer, 31)
    assert relay_calls == ["turn_off", "turn_off"]
    assert controller._shutdown_latched is False
    assert controller.reason == "Recovery wait"


async def test_normal_cancellation_retains_pending_on_for_unload(hass, options, relay_calls, freezer):
    hass.services.async_remove("switch", "turn_on")

    async def no_confirmation(call):
        relay_calls.append("turn_on")

    hass.services.async_register("switch", "turn_on", no_confirmation)
    entry, controller = await setup_controller(hass, options)
    await advance(hass, freezer, 61)
    assert controller._pending_state is True

    hass.states.async_set(ROOM, "24.5", {"unit_of_measurement": "°C"})
    await hass.async_block_till_done()
    assert controller._pending_state is True
    assert controller.reason == "Canceling pending ON"
    hass.states.async_set(ROOM, "18", {"unit_of_measurement": "°C"})
    await hass.async_block_till_done()
    assert relay_calls == ["turn_on"]  # No duplicate ON while confirmation is pending.

    assert await hass.config_entries.async_unload(entry.entry_id)
    assert relay_calls == ["turn_on", "turn_off"]


async def test_service_error_on_remains_outstanding(hass, options, relay_calls, freezer):
    hass.services.async_remove("switch", "turn_on")

    async def fail_on(call):
        relay_calls.append("turn_on")
        raise HomeAssistantError("unknown delivery")

    hass.services.async_register("switch", "turn_on", fail_on)
    entry, controller = await setup_controller(hass, options)
    await advance(hass, freezer, 61)
    assert controller._pending_state is True
    hass.states.async_set(ROOM, "24.5", {"unit_of_measurement": "°C"})
    await hass.async_block_till_done()
    assert controller._pending_state is True
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert relay_calls == ["turn_on", "turn_off"]


async def test_pending_off_has_confirmation_timer(hass, options, relay_calls, freezer, monkeypatch):
    monkeypatch.setattr("custom_components.zoneless_heating_controller.controller.COMMAND_TIMEOUT_SECONDS", 0.01)
    hass.services.async_remove("switch", "turn_off")

    async def no_confirmation(call):
        relay_calls.append("turn_off")

    hass.services.async_register("switch", "turn_off", no_confirmation)
    _, controller = await setup_controller(hass, options, relay_state="on")
    hass.states.async_set(ROOM, "24.5", {"unit_of_measurement": "°C"})
    await advance(hass, freezer, 61)
    assert controller._pending_state is False
    await advance(hass, freezer, 0.02)
    assert "did not confirm OFF" in controller.command_error


async def test_final_shutdown_failure_leaves_no_jobs(hass, options, relay_calls, monkeypatch):
    monkeypatch.setattr("custom_components.zoneless_heating_controller.controller.COMMAND_TIMEOUT_SECONDS", 0.01)
    hass.services.async_remove("switch", "turn_off")

    async def no_confirmation(call):
        relay_calls.append("turn_off")

    hass.services.async_register("switch", "turn_off", no_confirmation)
    _, controller = await setup_controller(hass, options, relay_state="on")
    assert await controller.async_stop(final=True) is False
    assert controller._stopped is True
    assert controller._timer_cancel is None
    assert controller._listeners == []
    assert controller._remove_shutdown_listener is None
    assert controller.next_evaluation_at is None


async def test_manual_off_gets_immediate_attempt_after_normal_failure(hass, options, relay_calls, freezer):
    _, controller = await setup_controller(hass, options, relay_state="on")

    async def fail_off(call):
        relay_calls.append("turn_off")
        raise HomeAssistantError("offline")

    hass.services.async_remove("switch", "turn_off")
    hass.services.async_register("switch", "turn_off", fail_off)
    hass.states.async_set(ROOM, "24.5", {"unit_of_measurement": "°C"})
    await advance(hass, freezer, 61)
    assert relay_calls == ["turn_off"]
    await controller.async_set_mode(MODE_MANUAL_OFF)
    assert relay_calls == ["turn_off", "turn_off"]

    async def restored_off(call):
        relay_calls.append("turn_off")
        hass.states.async_set(RELAY, "off")

    hass.services.async_remove("switch", "turn_off")
    hass.services.async_register("switch", "turn_off", restored_off)


async def test_relay_fault_requires_healthy_recovery(hass, options, relay_calls, freezer):
    options["healthy_minutes"] = 2
    _, controller = await setup_controller(hass, options, relay_state="on")
    hass.states.async_set(RELAY, "unavailable")
    await hass.async_block_till_done()
    assert relay_calls == ["turn_off"]
    assert controller._recovery_required is True
    await advance(hass, freezer, 61)
    assert relay_calls == ["turn_off"]
    assert controller.reason == "Recovery wait"
    await advance(hass, freezer, 61)
    assert relay_calls == ["turn_off", "turn_on"]


async def test_blocked_save_does_not_delay_manual_off(hass, options, relay_calls):
    _, controller = await setup_controller(hass, options, relay_state="on")
    started = asyncio.Event()
    release = asyncio.Event()
    original_save = controller._store.async_save

    async def slow_save(data):
        if data["mode"] == MODE_MANUAL_ON:
            started.set()
            await release.wait()
        await original_save(data)

    controller._store.async_save = slow_save
    task = hass.async_create_task(controller.async_set_mode(MODE_MANUAL_ON))
    await started.wait()
    try:
        await asyncio.wait_for(controller.async_set_mode(MODE_MANUAL_OFF), timeout=1)
        assert relay_calls == ["turn_off"]
        assert hass.states.get(RELAY).state == "off"
    finally:
        release.set()
    await task
    await hass.async_block_till_done()


async def test_blocked_save_does_not_delay_sensor_fault_off(hass, options, relay_calls):
    _, controller = await setup_controller(hass, options, relay_state="on")
    started = asyncio.Event()
    release = asyncio.Event()
    off_called = asyncio.Event()
    original_save = controller._store.async_save

    async def slow_save(data):
        if data["mode"] == MODE_MANUAL_ON:
            started.set()
            await release.wait()
        await original_save(data)

    async def confirm_off(call):
        relay_calls.append("turn_off")
        hass.states.async_set(RELAY, "off")
        off_called.set()

    controller._store.async_save = slow_save
    hass.services.async_remove("switch", "turn_off")
    hass.services.async_register("switch", "turn_off", confirm_off)
    task = hass.async_create_task(controller.async_set_mode(MODE_MANUAL_ON))
    await started.wait()
    try:
        hass.states.async_set(ROOM, "unavailable")
        await asyncio.wait_for(off_called.wait(), timeout=1)
        assert relay_calls == ["turn_off"]
        assert controller.sensor_fault
    finally:
        release.set()
    await task
    await hass.async_block_till_done()


async def test_mode_change_during_save_is_not_lost(hass, options, relay_calls):
    _, controller = await setup_controller(hass, options, relay_state="on")
    started = asyncio.Event()
    release = asyncio.Event()
    original_save = controller._store.async_save

    async def slow_save(data):
        if data["mode"] == MODE_MANUAL_ON:
            started.set()
            await release.wait()
        await original_save(data)

    controller._store.async_save = slow_save
    task = hass.async_create_task(controller.async_set_mode(MODE_MANUAL_ON))
    await started.wait()
    await controller.async_set_mode(MODE_MANUAL_OFF)
    release.set()
    await task
    await hass.async_block_till_done()
    saved = await controller._store.async_load()
    assert saved["mode"] == MODE_MANUAL_OFF
    assert relay_calls == ["turn_off"]


async def test_future_sensor_timestamp_is_fault(hass, options, relay_calls, freezer):
    _, controller = await setup_controller(hass, options, relay_state="on")
    freezer.tick(timedelta(hours=1))
    hass.states.async_set(ROOM, "18", {"unit_of_measurement": "°C"}, force_update=True)
    freezer.tick(timedelta(hours=-1))
    await controller.async_evaluate()
    assert relay_calls == ["turn_off"]
    assert "future" in controller.sensor_fault


async def test_compensation_cannot_invert_thresholds(hass, options, relay_calls):
    options["monitor_only"] = True
    options["start_threshold"] = 0.01
    options["stop_threshold"] = 0
    _, controller = await setup_controller(hass, options, room_temperature="23")
    hass.states.async_set(OUTDOOR, "-1", {"unit_of_measurement": "°C"})
    await hass.async_block_till_done()
    assert controller.demand == -0.5
    assert controller.request_heat is False


async def test_duplicate_setup_reserves_relay_before_await(hass, options, relay_calls):
    entries = [
        MockConfigEntry(
            domain=DOMAIN, title=f"Heater {index}", unique_id=f"{RELAY}-{index}",
            data={"boiler_relay": RELAY, "outdoor_sensor": OUTDOOR}, options=options,
        )
        for index in range(2)
    ]
    for entry in entries:
        entry.add_to_hass(hass)
    hass.states.async_set(RELAY, "off")
    hass.states.async_set(OUTDOOR, "5", {"unit_of_measurement": "°C"})
    hass.states.async_set(ROOM, "18", {"unit_of_measurement": "°C"})
    started = asyncio.Event()
    release = asyncio.Event()
    original_load = HeatingController.async_load

    async def slow_load(self, runtime_id):
        started.set()
        await release.wait()
        await original_load(self, runtime_id)

    HeatingController.async_load = slow_load
    try:
        task = hass.async_create_task(hass.config_entries.async_setup(entries[0].entry_id))
        await started.wait()
        with pytest.raises(ConfigEntryError):
            await async_setup_entry(hass, entries[1])
        release.set()
        assert await task
        await hass.async_block_till_done()
    finally:
        HeatingController.async_load = original_load


async def test_monitor_only_commit_requires_confirmed_off(hass, options, relay_calls):
    entry, controller = await setup_controller(hass, options, relay_state="on")
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], option_input(options, monitor_only=True)
    )
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()
    assert relay_calls == ["turn_off"]
    assert entry.options["monitor_only"] is True
    restored = hass.data[DOMAIN][entry.entry_id]
    await restored.async_set_mode(MODE_MANUAL_ON)
    assert relay_calls == ["turn_off"]


async def test_monitor_only_commit_refused_when_off_fails(hass, options, relay_calls):
    entry, controller = await setup_controller(hass, options, relay_state="on")

    async def fail_off(call):
        relay_calls.append("turn_off")
        raise HomeAssistantError("offline")

    hass.services.async_remove("switch", "turn_off")
    hass.services.async_register("switch", "turn_off", fail_off)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], option_input(options, monitor_only=True)
    )
    assert result["type"] == "form"
    assert result["errors"]["base"] == "monitor_shutdown_failed"
    assert entry.options["monitor_only"] is False
    assert controller.monitor_only is False
    assert controller.mode == MODE_MANUAL_OFF

    async def restored_off(call):
        relay_calls.append("turn_off")
        hass.states.async_set(RELAY, "off")

    hass.services.async_remove("switch", "turn_off")
    hass.services.async_register("switch", "turn_off", restored_off)


@pytest.mark.parametrize("value", [1.9, True])
async def test_options_flow_rejects_fractional_and_boolean_minutes(hass, options, value):
    entry, _ = await setup_controller(hass, options)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    with pytest.raises(InvalidData):
        await hass.config_entries.options.async_configure(
            result["flow_id"], option_input(options, min_on_minutes=value)
        )


async def test_platform_unload_failure_resumes_controller(hass, options, relay_calls, monkeypatch):
    entry, controller = await setup_controller(hass, options, relay_state="on")
    original_unload = hass.config_entries.async_unload_platforms

    async def fail_unload(entry, platforms):
        return False

    monkeypatch.setattr(hass.config_entries, "async_unload_platforms", fail_unload)
    assert await async_unload_entry(hass, entry) is False
    assert controller._stopped is False
    assert controller._listeners
    monkeypatch.setattr(hass.config_entries, "async_unload_platforms", original_unload)


async def test_missing_store_cold_start_requires_off(hass, options, relay_calls):
    _, controller = await setup_controller(hass, options, relay_state="on", seed_state=False)
    assert relay_calls == ["turn_off"]
    assert hass.states.get(RELAY).state == "off"
    assert controller._off_since is not None
    assert controller.reason == "Minimum OFF time"


async def test_failed_cancel_off_preserves_on_uncertainty_for_monitor(hass, options, relay_calls, freezer):
    hass.services.async_remove("switch", "turn_on")

    async def no_on_confirmation(call):
        relay_calls.append("turn_on")

    async def fail_off(call):
        relay_calls.append("turn_off")
        raise HomeAssistantError("offline")

    hass.services.async_register("switch", "turn_on", no_on_confirmation)
    hass.services.async_remove("switch", "turn_off")
    hass.services.async_register("switch", "turn_off", fail_off)
    _, controller = await setup_controller(hass, options)
    await advance(hass, freezer, 61)
    assert controller._pending_state is True
    assert controller._on_uncertain is True

    assert await controller.async_prepare_monitor_only() is False
    assert controller._on_uncertain is True
    assert controller._monitor_transition is False

    async def restored_off(call):
        relay_calls.append("turn_off")
        hass.states.async_set(RELAY, "off")

    hass.services.async_remove("switch", "turn_off")
    hass.services.async_register("switch", "turn_off", restored_off)


async def test_on_timeout_still_uncertain_requires_off_for_unload(hass, options, relay_calls, freezer, monkeypatch):
    monkeypatch.setattr("custom_components.zoneless_heating_controller.controller.COMMAND_TIMEOUT_SECONDS", 0.01)
    hass.services.async_remove("switch", "turn_on")

    async def no_on_confirmation(call):
        relay_calls.append("turn_on")

    async def fail_off(call):
        relay_calls.append("turn_off")
        raise HomeAssistantError("offline")

    hass.services.async_register("switch", "turn_on", no_on_confirmation)
    hass.services.async_remove("switch", "turn_off")
    hass.services.async_register("switch", "turn_off", fail_off)
    entry, controller = await setup_controller(hass, options)
    await advance(hass, freezer, 61)
    await advance(hass, freezer, 0.02)
    assert controller._pending_state is None
    assert controller._on_uncertain is True

    async def restored_off(call):
        relay_calls.append("turn_off")
        hass.states.async_set(RELAY, "off")

    hass.services.async_remove("switch", "turn_off")
    hass.services.async_register("switch", "turn_off", restored_off)
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert relay_calls == ["turn_on", "turn_off", "turn_off"]


async def test_monitor_transition_lock_rejects_manual_on(hass, options, relay_calls):
    _, controller = await setup_controller(hass, options, relay_state="on")
    assert await controller.async_prepare_monitor_only() is True
    assert controller._monitor_transition is True
    with pytest.raises(HomeAssistantError, match="Monitor-only transition"):
        await controller.async_set_mode(MODE_MANUAL_ON)
    assert relay_calls == ["turn_off"]
    assert controller.mode == MODE_MANUAL_OFF


async def test_late_on_after_failed_cancel_uses_minimum_on(hass, options, relay_calls, freezer, monkeypatch):
    monkeypatch.setattr("custom_components.zoneless_heating_controller.controller.COMMAND_TIMEOUT_SECONDS", 0.01)
    hass.services.async_remove("switch", "turn_on")

    async def no_on_confirmation(call):
        relay_calls.append("turn_on")

    async def fail_off(call):
        relay_calls.append("turn_off")
        raise HomeAssistantError("offline")

    hass.services.async_register("switch", "turn_on", no_on_confirmation)
    hass.services.async_remove("switch", "turn_off")
    hass.services.async_register("switch", "turn_off", fail_off)
    _, controller = await setup_controller(hass, options)
    await advance(hass, freezer, 61)
    hass.states.async_set(ROOM, "24.5", {"unit_of_measurement": "°C"})
    await advance(hass, freezer, 0.02)
    assert relay_calls == ["turn_on", "turn_off"]
    assert controller._on_uncertain is True

    hass.states.async_set(RELAY, "on")
    await hass.async_block_till_done()
    assert controller._on_uncertain is False
    assert controller.reason == "Minimum ON time"
    await advance(hass, freezer, 59)
    assert relay_calls == ["turn_on", "turn_off"]

    async def restored_off(call):
        relay_calls.append("turn_off")
        hass.states.async_set(RELAY, "off")

    hass.services.async_remove("switch", "turn_off")
    hass.services.async_register("switch", "turn_off", restored_off)
    await advance(hass, freezer, 2)
    assert relay_calls == ["turn_on", "turn_off", "turn_off"]


async def test_cold_persisted_uncertainty_sends_off_even_when_reported_off(hass, options, relay_calls):
    entry, controller = await setup_controller(hass, options)
    runtime_id = controller._runtime_id
    assert await controller.async_stop(final=True)
    saved = await controller._store.async_load()
    saved["on_uncertain"] = True
    await controller._store.async_save(saved)

    restored = HeatingController(hass, entry)
    await restored.async_load(runtime_id)
    hass.data[DOMAIN][entry.entry_id] = restored
    await restored.async_start()
    assert relay_calls == ["turn_off"]
    assert restored._on_uncertain is False
    await restored.async_stop(final=True)


async def test_failed_monitor_reload_resumes_read_only(hass, options, relay_calls, monkeypatch):
    entry, controller = await setup_controller(hass, options, relay_state="on")
    original_unload = hass.config_entries.async_unload_platforms

    async def fail_unload(entry, platforms):
        return False

    monkeypatch.setattr(hass.config_entries, "async_unload_platforms", fail_unload)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], option_input(options, monitor_only=True)
    )
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()
    assert entry.options["monitor_only"] is True
    assert controller.monitor_only is True
    assert controller._monitor_transition is False
    assert controller._stopped is False
    await controller.async_set_mode(MODE_MANUAL_ON)
    assert relay_calls == ["turn_off"]
    monkeypatch.setattr(hass.config_entries, "async_unload_platforms", original_unload)


async def test_fault_latch_survives_failed_cancel_and_late_on(hass, options, relay_calls, freezer):
    hass.services.async_remove("switch", "turn_on")

    async def no_on_confirmation(call):
        relay_calls.append("turn_on")

    async def fail_off(call):
        relay_calls.append("turn_off")
        raise HomeAssistantError("offline")

    hass.services.async_register("switch", "turn_on", no_on_confirmation)
    hass.services.async_remove("switch", "turn_off")
    hass.services.async_register("switch", "turn_off", fail_off)
    _, controller = await setup_controller(hass, options)
    await advance(hass, freezer, 61)
    assert controller._pending_state is True

    hass.states.async_set(ROOM, "unavailable")
    await hass.async_block_till_done()
    assert relay_calls == ["turn_on", "turn_off"]
    assert controller._shutdown_latched is True
    assert controller._on_uncertain is True

    hass.states.async_set(ROOM, "18", {"unit_of_measurement": "°C"})
    await hass.async_block_till_done()
    hass.states.async_set(RELAY, "on")
    await hass.async_block_till_done()
    assert controller._shutdown_latched is True
    assert controller.request_heat is False
    assert controller.reason == "OFF retry pending"

    async def restored_off(call):
        relay_calls.append("turn_off")
        hass.states.async_set(RELAY, "off")

    hass.services.async_remove("switch", "turn_off")
    hass.services.async_register("switch", "turn_off", restored_off)
    await advance(hass, freezer, 31)
    assert relay_calls == ["turn_on", "turn_off", "turn_off"]
    assert controller._shutdown_latched is False
    assert controller.reason == "Recovery wait"


async def test_storage_wait_does_not_delay_fault_off(hass, options, relay_calls):
    _, controller = await setup_controller(hass, options, relay_state="on")
    started = asyncio.Event()
    release = asyncio.Event()
    original_save = controller._store.async_save

    async def slow_save(data):
        started.set()
        await release.wait()
        await original_save(data)

    controller._store.async_save = slow_save
    hass.states.async_set(ROOM, "unavailable")
    await started.wait()
    assert relay_calls == ["turn_off"]
    assert hass.states.get(RELAY).state == "off"
    release.set()
    await hass.async_block_till_done()


async def test_storage_wait_does_not_delay_shutdown_off(hass, options, relay_calls):
    _, controller = await setup_controller(hass, options, relay_state="on")
    started = asyncio.Event()
    release = asyncio.Event()
    original_save = controller._store.async_save

    async def slow_save(data):
        started.set()
        await release.wait()
        await original_save(data)

    controller._store.async_save = slow_save
    stop_task = hass.async_create_task(controller.async_stop(final=True))
    await started.wait()
    assert relay_calls == ["turn_off"]
    assert hass.states.get(RELAY).state == "off"
    release.set()
    assert await stop_task


@pytest.mark.parametrize("final", [False, True], ids=["unload", "ha-stop"])
async def test_storage_timeout_is_bounded_and_retains_single_writer(
    hass, options, relay_calls, monkeypatch, final
):
    entry, controller = await setup_controller(hass, options, relay_state="on")
    monkeypatch.setattr("custom_components.zoneless_heating_controller.controller.COMMAND_TIMEOUT_SECONDS", 0.01)
    started = asyncio.Event()
    release = asyncio.Event()
    original_save = controller._store.async_save

    async def blocked_save(data):
        started.set()
        await release.wait()
        await original_save(data)

    controller._store.async_save = blocked_save
    mode_task = hass.async_create_task(controller.async_set_mode(MODE_MANUAL_ON))
    await started.wait()
    save_task = controller._save_task
    try:
        if final:
            stopped = await asyncio.wait_for(controller.async_stop(final=True), timeout=1)
        else:
            stopped = await asyncio.wait_for(async_unload_entry(hass, entry), timeout=1)
        assert stopped is False
        assert relay_calls == ["turn_off"]
        assert controller._save_task is save_task
        assert not save_task.done()
        assert controller.reason == "Shutdown persistence failed"
        assert controller.mode == MODE_MANUAL_OFF
        if final:
            assert controller._stopped is True
            assert controller._listeners == []
            assert controller._timer_cancel is None
        else:
            assert controller._stopped is False
            assert controller._listeners
            assert hass.data[DOMAIN][entry.entry_id] is controller
    finally:
        release.set()
    await mode_task
    await save_task
    await hass.async_block_till_done()


async def test_storage_error_refuses_unload_and_keeps_manual_off(hass, options, relay_calls):
    entry, controller = await setup_controller(hass, options, relay_state="on")

    async def fail_save(data):
        raise OSError("disk full")

    controller._store.async_save = fail_save
    await controller.async_set_mode(MODE_MANUAL_OFF)
    await controller._save_task
    assert relay_calls == ["turn_off"]

    assert await async_unload_entry(hass, entry) is False
    assert hass.data[DOMAIN][entry.entry_id] is controller
    assert controller.mode == MODE_MANUAL_OFF
    assert controller._stopped is False
    assert controller._listeners
    assert "OSError" in controller.persistence_error
    assert controller.reason == "Shutdown persistence failed"


async def test_failed_live_reload_applies_all_committed_options(hass, options, relay_calls, freezer, monkeypatch):
    options["monitor_only"] = True
    entry, controller = await setup_controller(hass, options)
    rooms = json.loads(options["rooms_json"])
    rooms[0]["preferred"] = 21
    changed = option_input(
        options, monitor_only=False, min_on_minutes=7,
        max_overshoot=3, rooms_json=json.dumps(rooms),
    )
    original_unload = hass.config_entries.async_unload_platforms

    async def fail_unload(entry, platforms):
        return False

    monkeypatch.setattr(hass.config_entries, "async_unload_platforms", fail_unload)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], changed)
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()
    assert controller.monitor_only is False
    assert controller.settings["min_on_minutes"] == 7
    assert controller.settings["max_overshoot"] == 3
    assert controller.rooms[0]["preferred"] == 21

    await advance(hass, freezer, 61)
    assert relay_calls == ["turn_on"]
    hass.states.async_set(ROOM, "24.5", {"unit_of_measurement": "°C"})
    await advance(hass, freezer, 61)
    assert relay_calls == ["turn_on"]
    assert controller.reason == "Minimum ON time"
    monkeypatch.setattr(hass.config_entries, "async_unload_platforms", original_unload)


async def test_uncertain_off_retry_is_scheduled_while_relay_reports_off(hass, options, relay_calls, freezer):
    hass.services.async_remove("switch", "turn_on")

    async def no_on_confirmation(call):
        relay_calls.append("turn_on")

    async def fail_off(call):
        relay_calls.append("turn_off")
        raise HomeAssistantError("offline")

    hass.services.async_register("switch", "turn_on", no_on_confirmation)
    hass.services.async_remove("switch", "turn_off")
    hass.services.async_register("switch", "turn_off", fail_off)
    _, controller = await setup_controller(hass, options)
    await advance(hass, freezer, 61)
    await controller.async_set_mode(MODE_MANUAL_OFF)
    assert relay_calls == ["turn_on", "turn_off"]
    assert controller._on_uncertain is True
    assert hass.states.get(RELAY).state == "off"

    await advance(hass, freezer, 31)
    assert relay_calls == ["turn_on", "turn_off", "turn_off"]

    async def restored_off(call):
        relay_calls.append("turn_off")
        hass.states.async_set(RELAY, "off")

    hass.services.async_remove("switch", "turn_off")
    hass.services.async_register("switch", "turn_off", restored_off)


async def test_on_timeout_cancels_before_blocked_persistence(hass, options, relay_calls, freezer, monkeypatch):
    monkeypatch.setattr("custom_components.zoneless_heating_controller.controller.COMMAND_TIMEOUT_SECONDS", 0.01)
    hass.services.async_remove("switch", "turn_on")

    async def no_on_confirmation(call):
        relay_calls.append("turn_on")

    hass.services.async_register("switch", "turn_on", no_on_confirmation)
    _, controller = await setup_controller(hass, options)
    await advance(hass, freezer, 61)
    assert controller._pending_state is True

    started = asyncio.Event()
    release = asyncio.Event()
    original_save = controller._store.async_save

    async def slow_save(data):
        if data["on_blocked_until"] is not None:
            started.set()
            await release.wait()
        await original_save(data)

    controller._store.async_save = slow_save
    try:
        freezer.tick(timedelta(seconds=0.02))
        async_fire_time_changed(hass)
        await asyncio.wait_for(started.wait(), timeout=1)
        assert relay_calls == ["turn_on", "turn_off"]
        assert hass.states.get(RELAY).state == "off"
    finally:
        release.set()
    await hass.async_block_till_done()
