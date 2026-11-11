"""Conservative heating control. This is not a certified safety device."""

from __future__ import annotations

import asyncio
import logging
import math
from datetime import timedelta

from homeassistant.const import ATTR_UNIT_OF_MEASUREMENT, EVENT_HOMEASSISTANT_STOP, UnitOfTemperature
from homeassistant.core import callback, valid_entity_id
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import TemperatureConverter

from .config import normalize_options, normalize_rooms
from .const import (
    COMMAND_TIMEOUT_SECONDS,
    CONFIRMATION_POLL_SECONDS,
    DEFAULT_OPTIONS,
    DOMAIN,
    MODE_AUTO,
    MODE_MANUAL_OFF,
    MODE_MANUAL_ON,
    MODES,
    OFF_RETRY_SECONDS,
)

_LOGGER = logging.getLogger(__name__)


class InputFault(ValueError):
    """A required input cannot safely be used."""


class HeatingController:
    """Calculate demand and own all commands to one demand relay."""

    def __init__(self, hass, entry):
        self.hass = hass
        self.entry = entry
        self.relay = entry.data.get("boiler_relay", "")
        self.outdoor_sensor = entry.data.get("outdoor_sensor", "")
        self.config_error = None
        try:
            self.settings = normalize_options(entry.options)
            self.rooms = normalize_rooms(self.settings["rooms_json"])
        except ValueError as err:
            self.config_error = str(err)
            self.settings = dict(DEFAULT_OPTIONS)
            self.settings["monitor_only"] = entry.options.get("monitor_only", True) is not False
            self.rooms = []
        if not valid_entity_id(self.relay) or not self.relay.startswith("switch."):
            self.config_error = "Invalid boiler demand switch"
        if not valid_entity_id(self.outdoor_sensor) or not self.outdoor_sensor.startswith("sensor."):
            self.config_error = "Invalid outdoor temperature sensor"

        self.mode = MODE_AUTO
        self.demand = None
        self.request_heat = False
        self.reason = "Not started"
        self.detail = "Controller has not started"
        self.active_fault = None
        self.sensor_fault = None
        self.overheated_rooms = []
        self.command_error = None
        self._listeners = []
        self._remove_shutdown_listener = None
        self._callbacks = []
        self._timer_cancel = None
        self._task = None
        self._evaluate_requested = False
        self._started = False
        self._stopped = True
        self._stopping = False
        self._stop_lock = asyncio.Lock()
        self._store = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}.state")
        self._state_revision = 0
        self._persisted_revision = 0
        self._save_task = None
        self._input_revision = 0
        self._runtime_id = None
        self.persistence_error = None
        self._relay_state = None
        self._on_since = None
        self._off_since = None
        self._force_off = True
        self._shutdown_latched = False
        self._on_uncertain = False
        self._monitor_transition = False
        self._recovery_required = False
        self._healthy_since = None
        self._pending_state = None
        self._confirmation_deadline = None
        self._next_off_attempt = None
        self._on_blocked_until = None
        self._minimum_on_until = None
        self._minimum_off_until = None
        self._recovery_until = None
        self._sensor_deadlines = []
        self.next_evaluation_at = None
        self.shutdown_failed = False

    @property
    def enabled(self):
        """Compatibility for the original enable switch."""
        return self.mode != MODE_MANUAL_OFF

    @property
    def monitor_only(self):
        return self.settings["monitor_only"]

    def listen(self, callback_fn):
        self._callbacks.append(callback_fn)
        return lambda: self._callbacks.remove(callback_fn) if callback_fn in self._callbacks else None

    def _notify(self):
        for fn in tuple(self._callbacks):
            try:
                fn()
            except Exception:  # Entity removal must not interrupt relay control.
                _LOGGER.debug("Entity update callback failed", exc_info=True)

    async def async_load(self, runtime_id):
        """Restore mode; only a same-process reload inherits cycle timestamps."""
        self._runtime_id = runtime_id
        try:
            data = await self._store.async_load()
        except Exception:
            _LOGGER.exception("Cannot restore controller state; using Manual OFF")
            self.mode = MODE_MANUAL_OFF
            self._force_off = True
            self._mark_state_dirty()
            return
        if not isinstance(data, dict):
            self._force_off = True
            self._mark_state_dirty()
            return
        self.mode = data.get("mode") if data.get("mode") in MODES else MODE_AUTO
        self._force_off = data.get("force_off") is True
        self._shutdown_latched = data.get("shutdown_latched") is True
        self._on_uncertain = data.get("on_uncertain") is True
        self._recovery_required = data.get("recovery_required") is True
        self._on_blocked_until = self._stored_future_time(data.get("on_blocked_until"))
        if self._on_blocked_until and "on_uncertain" not in data:
            self._on_uncertain = True  # Conservative migration from the previous storage format.
        if data.get("runtime_id") != runtime_id:
            # A cold start cannot prove what the relay did while HA was down.
            self._relay_state = None
            self._on_since = None
            self._off_since = None
            self._force_off = True
            self._mark_state_dirty()
            return
        self._relay_state = data.get("relay_state") if isinstance(data.get("relay_state"), bool) else None
        self._on_since = self._stored_time(data.get("on_since"))
        self._off_since = self._stored_time(data.get("off_since"))

    @staticmethod
    def _stored_time(value):
        if not isinstance(value, str) or not (parsed := dt_util.parse_datetime(value)):
            return None
        return min(dt_util.as_utc(parsed), dt_util.utcnow())

    @staticmethod
    def _stored_future_time(value):
        if not isinstance(value, str) or not (parsed := dt_util.parse_datetime(value)):
            return None
        parsed = dt_util.as_utc(parsed)
        return parsed if parsed > dt_util.utcnow() else None

    def _mark_state_dirty(self):
        self._state_revision += 1

    def _saved_data(self):
        return {
            "runtime_id": self._runtime_id,
            "mode": self.mode,
            "relay_state": self._relay_state,
            "on_since": self._on_since.isoformat() if self._on_since else None,
            "off_since": self._off_since.isoformat() if self._off_since else None,
            "on_blocked_until": self._on_blocked_until.isoformat() if self._on_blocked_until else None,
            "force_off": self._force_off,
            "shutdown_latched": self._shutdown_latched,
            "on_uncertain": self._on_uncertain,
            "recovery_required": self._recovery_required,
        }

    def _request_save(self):
        """Schedule persistence without delaying serialized relay control."""
        if self._persisted_revision == self._state_revision:
            return
        if self._save_task is None or self._save_task.done():
            self._save_task = self.hass.async_create_task(self._async_save_state())

    async def _async_save_state(self):
        """Write snapshots in revision order; never run concurrent store writes."""
        while self._persisted_revision != self._state_revision:
            revision = self._state_revision
            try:
                await self._store.async_save(self._saved_data())
            except Exception as err:
                _LOGGER.exception("Cannot persist controller state")
                self.persistence_error = f"State persistence failed: {type(err).__name__}"
                self._notify()
                return
            # A newer mode/relay update may have arrived while this save waited.
            self._persisted_revision = max(self._persisted_revision, revision)
            self.persistence_error = None
        self._notify()

    async def _flush_state(self):
        """Bound shutdown persistence without canceling the sole state writer."""
        self._request_save()
        if not self._save_task:
            return True
        try:
            await asyncio.wait_for(
                asyncio.shield(self._save_task), timeout=COMMAND_TIMEOUT_SECONDS
            )
        except TimeoutError:
            self.persistence_error = "State persistence timed out during shutdown"
            _LOGGER.error("%s; retaining the existing controller and writer", self.persistence_error)
            return False
        # A completed task may have caught a disk error without saving this revision.
        return self._persisted_revision == self._state_revision

    def _register_listeners(self):
        if self._listeners:
            return
        entities = [self.relay, self.outdoor_sensor, *[room["entity_id"] for room in self.rooms]]
        self._listeners.append(async_track_state_change_event(self.hass, entities, self._changed))
        self._listeners.append(async_track_time_interval(self.hass, self._changed, timedelta(minutes=1)))
        if self._remove_shutdown_listener is None:
            self._remove_shutdown_listener = self.hass.bus.async_listen_once(
                EVENT_HOMEASSISTANT_STOP, self._async_shutdown
            )

    async def async_start(self):
        self._stopped = False
        self._started = True
        self._register_listeners()
        await self.async_evaluate()

    async def _async_shutdown(self, _event):
        self._remove_shutdown_listener = None
        await self.async_stop(final=True)

    @callback
    def _changed(self, _event=None):
        if not self._stopped and not self._stopping:
            self._input_revision += 1
            self.hass.async_create_task(self.async_evaluate())

    async def async_set_mode(self, mode):
        if mode not in MODES:
            raise ValueError(f"Unsupported controller mode: {mode}")
        if self._monitor_transition and mode != MODE_MANUAL_OFF:
            raise HomeAssistantError("Monitor-only transition is waiting for controller reload")
        if mode == self.mode:
            return
        self.mode = mode
        self._input_revision += 1
        if mode == MODE_MANUAL_OFF:
            # A new urgent request gets one immediate attempt; later failures are throttled.
            self._next_off_attempt = None
        self._mark_state_dirty()
        await self.async_evaluate()

    async def async_set_enabled(self, enabled):
        await self.async_set_mode(MODE_AUTO if enabled else MODE_MANUAL_OFF)

    async def _wait_for_off(self, deadline):
        """Wait for an actual OFF report without polling the relay."""
        confirmed = asyncio.Event()

        @callback
        def _off_reported(event):
            if (state := event.data.get("new_state")) and state.state == "off":
                confirmed.set()

        remove = async_track_state_change_event(self.hass, [self.relay], _off_reported)
        try:
            while self._pending_state is False and dt_util.utcnow() < deadline:
                state = self.hass.states.get(self.relay)
                if state and state.state == "off":
                    self._track_relay(False, dt_util.utcnow())
                    return True
                remaining = (deadline - dt_util.utcnow()).total_seconds()
                try:
                    await asyncio.wait_for(confirmed.wait(), timeout=max(remaining, 0))
                except TimeoutError:
                    break
            state = self.hass.states.get(self.relay)
            return bool(state and state.state == "off" and self._pending_state is None)
        finally:
            remove()

    async def async_prepare_monitor_only(self):
        """Withdraw live demand before the options flow commits monitor-only."""
        self._monitor_transition = True
        succeeded = False
        try:
            if self.mode == MODE_MANUAL_OFF:
                self._next_off_attempt = None
                await self.async_evaluate()
            else:
                await self.async_set_mode(MODE_MANUAL_OFF)
            if self._pending_state is False and self._confirmation_deadline:
                await self._wait_for_off(self._confirmation_deadline)
            state = self.hass.states.get(self.relay)
            succeeded = bool(
                state and state.state == "off"
                and self._pending_state is None and not self._on_uncertain
            )
            return succeeded
        finally:
            if not succeeded:
                self._monitor_transition = False
            # On success the lock stays until this controller is discarded.

    async def async_resume(self):
        """Resume after a platform unload failure using the committed options."""
        if not self._stopped:
            return
        was_monitoring = self.monitor_only
        try:
            settings = normalize_options(self.entry.options)
            rooms = normalize_rooms(settings["rooms_json"])
            if not valid_entity_id(self.relay) or not self.relay.startswith("switch."):
                raise ValueError("Invalid boiler demand switch")
            if not valid_entity_id(self.outdoor_sensor) or not self.outdoor_sensor.startswith("sensor."):
                raise ValueError("Invalid outdoor temperature sensor")
        except ValueError as err:
            self.settings = dict(DEFAULT_OPTIONS)
            self.settings["monitor_only"] = True
            self.rooms = []
            self.config_error = str(err)
        else:
            self.settings = settings
            self.rooms = rooms
            self.config_error = None
        self._monitor_transition = False
        if was_monitoring and not self.monitor_only:
            self._force_off = True
            self._mark_state_dirty()
        self._stopped = False
        self._stopping = False
        self._register_listeners()
        await self.async_evaluate()

    async def async_evaluate(self, _now=None):
        """Serialize evaluations; events received during a command request another pass."""
        if self._stopped or self._stopping:
            return
        current = asyncio.current_task()
        if self._task is not None:
            self._evaluate_requested = True
            return
        self._task = current
        self._evaluate_requested = True
        try:
            while self._evaluate_requested and not self._stopped and not self._stopping:
                self._evaluate_requested = False
                try:
                    await self._evaluate(dt_util.utcnow())
                except Exception as err:
                    _LOGGER.exception("Heating evaluation failed; requesting relay OFF")
                    self.demand = None
                    self.request_heat = False
                    self.active_fault = "Controller error"
                    self.reason = "Controller error"
                    self.detail = str(err)
                    self._recovery_required = True
                    self._mark_state_dirty()
                    if not self.monitor_only:
                        await self._send_command(False)
                    self._request_save()
                    self._schedule_next(dt_util.utcnow())
        finally:
            self._task = None
            self._notify()

    def _track_relay(self, actual, now):
        if actual is True and self._on_uncertain:
            # An observed ON resolves delivery uncertainty; normal cycle rules resume.
            self._on_uncertain = False
            self._mark_state_dirty()
        if actual == self._relay_state:
            if actual is False and self._force_off:
                self._force_off = False
                self._mark_state_dirty()
            return
        self._relay_state = actual
        self._mark_state_dirty()
        if actual is True:
            self._on_since = now
            if self._pending_state is True:
                self._on_uncertain = False
                self._clear_pending()
            elif self._pending_state is False:
                self._clear_pending()
                self.command_error = "Relay reported ON while OFF confirmation was pending"
                self._next_off_attempt = now
        elif actual is False:
            self._off_since = now
            self._force_off = False
            if self._pending_state is False:
                self._on_uncertain = False
                self._clear_pending()
            elif self._pending_state is True:
                self._clear_pending()
                self.command_error = "Relay reported OFF while ON confirmation was pending"
                self._on_blocked_until = now + self._minimum_off
        if actual is not None and self._pending_state == actual:
            self._clear_pending()

    def _clear_pending(self):
        self._pending_state = None
        self._confirmation_deadline = None
        self.command_error = None

    @property
    def _minimum_off(self):
        return timedelta(minutes=self.settings["min_off_minutes"])

    @property
    def _minimum_on(self):
        return timedelta(minutes=self.settings["min_on_minutes"])

    def _temperature(self, entity_id, now):
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable"):
            raise InputFault(f"{entity_id} is unavailable")
        unit = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)
        if unit not in (UnitOfTemperature.CELSIUS, UnitOfTemperature.FAHRENHEIT, UnitOfTemperature.KELVIN):
            raise InputFault(f"{entity_id} has no supported temperature unit")
        try:
            value = float(state.state)
        except (TypeError, ValueError) as err:
            raise InputFault(f"{entity_id} is not numeric") from err
        if not math.isfinite(value):
            raise InputFault(f"{entity_id} is not finite")
        value = TemperatureConverter.convert(value, unit, UnitOfTemperature.CELSIUS)
        if not -50 < value < 80:
            raise InputFault(f"{entity_id} is outside the supported temperature range")
        reported = state.last_reported or state.last_updated
        if reported is None:
            raise InputFault(f"{entity_id} has no report timestamp")
        reported = dt_util.as_utc(reported)
        if reported > now:
            raise InputFault(f"{entity_id} report timestamp is in the future")
        deadline = reported + timedelta(minutes=self.settings["sensor_timeout_minutes"])
        if deadline <= now:
            raise InputFault(f"{entity_id} has not reported within the sensor timeout")
        self._sensor_deadlines.append(deadline)
        return value

    def _calculate_demand(self, now):
        if self.config_error:
            raise InputFault(self.config_error)
        if not self.rooms:
            raise InputFault("No rooms configured")
        outside = self._temperature(self.outdoor_sensor, now)
        scores = []
        weights = []
        overheated = []
        for room in self.rooms:
            temp = self._temperature(room["entity_id"], now)
            if temp - room["max"] >= self.settings["max_overshoot"]:
                overheated.append(room["name"])
            span = room["preferred"] - room["min"] if temp < room["preferred"] else room["max"] - room["preferred"]
            score = max(-1.0, min(1.0, (room["preferred"] - temp) / span))
            scores.append(score * room["weight"])
            weights.append(room["weight"])
        self.demand = sum(scores) / sum(weights)
        self.overheated_rooms = overheated
        threshold = self.settings["start_threshold"]
        threshold += 0.05 if outside >= 10 else (-0.05 if outside < 0 else 0)
        stop = self.settings["stop_threshold"]
        threshold = max(threshold, stop + 0.01)
        if self.demand >= threshold:
            return True, "Heating demand"
        if self.demand <= stop:
            return False, "No heating demand"
        return None, "Hysteresis"

    async def _evaluate(self, now):
        input_revision = self._input_revision
        self._minimum_on_until = None
        self._minimum_off_until = None
        self._recovery_until = None
        self._sensor_deadlines = []
        self.overheated_rooms = []
        state = self.hass.states.get(self.relay)
        actual = state.state == "on" if state and state.state in ("on", "off") else None
        self._track_relay(actual, now)

        try:
            automatic_request, automatic_reason = self._calculate_demand(now)
            self.sensor_fault = None
            input_fault = None
        except InputFault as err:
            self.demand = None
            self.sensor_fault = str(err)
            input_fault = self.sensor_fault
            automatic_request, automatic_reason = False, "Sensor fault"

        relay_fault = actual is None
        self.active_fault = input_fault
        if self.overheated_rooms and self.mode != MODE_MANUAL_OFF:
            self.active_fault = "Overheat: " + ", ".join(self.overheated_rooms)
        if relay_fault:
            self.active_fault = "Boiler demand relay is unavailable"
        if self._force_off:
            self.active_fault = "Safe startup requires relay OFF"

        if (
            self._shutdown_latched and actual is False
            and self._pending_state is not True and not self._on_uncertain
        ):
            self._shutdown_latched = False
            self._mark_state_dirty()
        elif (
            self.active_fault
            and (actual is not False or self._pending_state is True or self._on_uncertain)
            and not self.monitor_only and not self._shutdown_latched
        ):
            # OFF may only be apparent while a late ON is still possible.
            self._shutdown_latched = True
            self._next_off_attempt = None  # First shutdown attempt is immediate.
            self._mark_state_dirty()

        if input_fault or relay_fault or (self.overheated_rooms and self.mode != MODE_MANUAL_OFF):
            if not self._recovery_required:
                self._recovery_required = True
                self._mark_state_dirty()
            self._healthy_since = None
        elif self.mode != MODE_MANUAL_OFF and self._recovery_required and not self._force_off and not self._shutdown_latched:
            if self._healthy_since is None:
                self._healthy_since = now
            self._recovery_until = self._healthy_since + timedelta(minutes=self.settings["healthy_minutes"])
            if self._recovery_until <= now:
                self._recovery_required = False
                self._healthy_since = None
                self._recovery_until = None
                self._mark_state_dirty()
        else:
            self._healthy_since = None

        uncertain_off = self._on_uncertain and self._pending_state is not True
        urgent_off = (
            self.mode == MODE_MANUAL_OFF or self.active_fault is not None
            or self._shutdown_latched or uncertain_off or self._stopping
        )
        if not urgent_off:
            self._request_save()
            if input_revision != self._input_revision:
                self._evaluate_requested = True
                return

        if self.mode == MODE_MANUAL_OFF:
            desired, self.reason = False, "Manual OFF"
        elif self.active_fault is not None:
            desired = False
            self.reason = "Safe startup" if self._force_off else ("Overheat protection" if self.overheated_rooms else "Sensor fault")
            if relay_fault:
                self.reason = "Relay fault"
        elif self._shutdown_latched:
            desired, self.reason = False, "Shutdown confirmation"
        elif uncertain_off:
            desired, self.reason = False, "ON delivery uncertain"
        elif self._recovery_required:
            desired, self.reason = False, "Recovery wait"
        elif self.mode == MODE_MANUAL_ON:
            desired, self.reason = True, "Manual ON"
        else:
            desired = actual is True if automatic_request is None else automatic_request
            self.reason = automatic_reason

        if urgent_off:
            desired = False
        elif desired and actual is not True:
            off_since = self._off_since or now
            self._minimum_off_until = off_since + self._minimum_off
            if self._on_blocked_until:
                self._minimum_off_until = max(self._minimum_off_until, self._on_blocked_until)
            if self._minimum_off_until > now:
                desired = False
                self.reason = "Minimum OFF time"
        elif not desired and actual is True and self.mode == MODE_AUTO:
            on_since = self._on_since or now
            self._minimum_on_until = on_since + self._minimum_on
            if self._minimum_on_until > now:
                desired = True
                self.reason = "Minimum ON time"

        self.detail = self.active_fault or ("Waiting for confirmed relay OFF" if self._shutdown_latched else automatic_reason)
        if uncertain_off and not self.active_fault:
            self.detail = "A previous ON command is uncertain; a successful OFF is required"
        if self.persistence_error:
            self.detail = f"{self.detail}; {self.persistence_error}"
        if self._pending_state is False and desired and not self.monitor_only:
            desired = False
            self.reason = "Waiting for relay OFF"
        self.request_heat = desired
        if self.monitor_only:
            self.reason = "Monitor only"
            self.detail = f"{self.reason}: no relay commands; calculated state is {'ON' if desired else 'OFF'}"
        else:
            await self._apply_command(desired, actual, now)
        self._request_save()
        self._schedule_next(now)

    async def _apply_command(self, desired, actual, now):
        urgent_off = not desired and (
            self.active_fault is not None or self._shutdown_latched
            or (self._on_uncertain and self._pending_state is not True)
            or self.mode == MODE_MANUAL_OFF or self._stopping
        )
        pending = self._pending_state
        if pending is not None and actual == pending:
            self._clear_pending()
            pending = None

        if pending is not None and self._confirmation_deadline and self._confirmation_deadline <= now:
            self._pending_state = None
            self._confirmation_deadline = None
            self.command_error = f"Relay did not confirm {'ON' if pending else 'OFF'} in time"
            if pending:
                self._on_blocked_until = now + self._minimum_off
                self._minimum_off_until = self._on_blocked_until
                self.request_heat = False
                self.reason = "Minimum OFF time"
                self._mark_state_dirty()
                # A late ON is still possible; only a subsequent successful OFF clears it.
                await self._send_command(False)
                return
            self._next_off_attempt = now + timedelta(seconds=OFF_RETRY_SECONDS)
            if desired:
                self.request_heat = False
                self.reason = "OFF retry pending"
                return
            pending = None

        if pending is not None:
            if pending is False and desired:
                self.request_heat = False
                self.reason = "Waiting for relay OFF"
            elif pending is True and not desired:
                if urgent_off:
                    await self._send_command(False)
                else:
                    # Keep the outstanding ON until it confirms or times out.
                    self.reason = "Canceling pending ON"
            else:
                self.reason = "Confirming relay command"
            return

        if actual == desired:
            if actual is False and self._on_uncertain:
                if self._next_off_attempt and self._next_off_attempt > now:
                    self.reason = "OFF retry pending"
                else:
                    await self._send_command(False)
            return
        if desired:
            await self._send_command(True)
        elif self._next_off_attempt and self._next_off_attempt > now:
            self.reason = "OFF retry pending"
        else:
            await self._send_command(False)

    async def _send_command(self, desired):
        service = "turn_on" if desired else "turn_off"
        if desired or self._pending_state is True:
            self._on_uncertain = True
            self._mark_state_dirty()
        self._pending_state = None
        self._confirmation_deadline = None
        try:
            await asyncio.wait_for(
                self.hass.services.async_call("switch", service, {"entity_id": self.relay}, blocking=True),
                timeout=COMMAND_TIMEOUT_SECONDS,
            )
        except Exception as err:  # Service errors must not escape the control loop.
            now = dt_util.utcnow()
            self.command_error = f"Relay {service} failed: {type(err).__name__}"
            _LOGGER.warning("%s for %s: %s", self.command_error, self.relay, err)
            if desired:
                # A timed-out ON may still reach the device; keep tracking it.
                self._pending_state = True
                self._confirmation_deadline = now + timedelta(seconds=COMMAND_TIMEOUT_SECONDS)
                self._on_blocked_until = now + self._minimum_off
                self._mark_state_dirty()
                self._request_save()
            else:
                self._next_off_attempt = now + timedelta(seconds=OFF_RETRY_SECONDS)
                self._request_save()
            return False

        now = dt_util.utcnow()
        state = self.hass.states.get(self.relay)
        actual = state.state == "on" if state and state.state in ("on", "off") else None
        self._track_relay(actual, now)
        if actual == desired:
            if self._on_uncertain:
                self._on_uncertain = False
                if not desired:
                    self._off_since = now
                self._mark_state_dirty()
            if desired and self._on_blocked_until:
                self._on_blocked_until = None
                self._mark_state_dirty()
            self._clear_pending()
        self._request_save()
        if actual == desired:
            return True
        self._pending_state = desired
        self._confirmation_deadline = now + timedelta(seconds=COMMAND_TIMEOUT_SECONDS)
        self.reason = "Confirming relay command"
        return False

    def _schedule_next(self, now):
        if self._timer_cancel:
            self._timer_cancel()
            self._timer_cancel = None
        deadlines = [*self._sensor_deadlines]
        if self._pending_state is not None and self._confirmation_deadline:
            deadlines.append(min(self._confirmation_deadline, now + timedelta(seconds=CONFIRMATION_POLL_SECONDS)))
        if self._next_off_attempt and (self._relay_state is not False or self._on_uncertain):
            deadlines.append(self._next_off_attempt)
        for deadline in (self._minimum_on_until, self._minimum_off_until, self._recovery_until):
            if deadline:
                deadlines.append(deadline)
        future = [deadline for deadline in deadlines if deadline > now]
        self.next_evaluation_at = min(future) if future else None
        if self.next_evaluation_at and not self._stopped and not self._stopping:
            self._timer_cancel = async_call_later(
                self.hass, max((self.next_evaluation_at - now).total_seconds(), 0.1), self._changed
            )

    async def async_stop(self, *, final=False):
        """Stop control; only recoverable unload failures may keep retrying."""
        async with self._stop_lock:
            if self._stopped:
                return True
            self._stopping = True
            if self._timer_cancel:
                self._timer_cancel()
                self._timer_cancel = None
            for remove in self._listeners:
                remove()
            self._listeners.clear()
            if self._remove_shutdown_listener:
                self._remove_shutdown_listener()
                self._remove_shutdown_listener = None

            task = self._task
            if task and task is not asyncio.current_task():
                try:
                    await asyncio.wait_for(asyncio.shield(task), COMMAND_TIMEOUT_SECONDS + 1)
                except TimeoutError:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

            succeeded = True
            if self._started and not self.monitor_only:
                self._force_off = True
                self._mark_state_dirty()
                state = self.hass.states.get(self.relay)
                actual = state.state == "on" if state and state.state in ("on", "off") else None
                if actual is not False or self._pending_state is True or self._on_uncertain or self.request_heat:
                    succeeded = await self._send_command(False)
                    if not succeeded and self._pending_state is False and self._confirmation_deadline:
                        succeeded = await self._wait_for_off(self._confirmation_deadline)
                    if not succeeded and self._pending_state is False:
                        self._pending_state = None
                        self._confirmation_deadline = None
                        self.command_error = "Relay did not confirm OFF during shutdown"
                self._request_save()

            self.shutdown_failed = not succeeded
            self.request_heat = False
            if succeeded:
                state = self.hass.states.get(self.relay)
                if state and state.state == "off":
                    # Every reload gets a fresh, verifiable OFF observation.
                    self._off_since = dt_util.utcnow()
                    self._force_off = False
                    self._mark_state_dirty()
                    self._request_save()
                self._stopped = True
                self._stopping = False
                self.reason = "Controller stopped"
                self.detail = "Relay OFF confirmed or no live relay command was required"
            else:
                self.mode = MODE_MANUAL_OFF
                self._mark_state_dirty()
                self._request_save()
                self.reason = "Shutdown OFF failed"
                self.detail = self.command_error
                if final:
                    # HA cannot keep running retry jobs after its final stop.
                    self._stopped = True
                    self.next_evaluation_at = None
                else:
                    # Refusing unload keeps this controller and its entities alive.
                    self._next_off_attempt = dt_util.utcnow() + timedelta(seconds=OFF_RETRY_SECONDS)
                    self._stopped = False
                    self._register_listeners()
                    self._schedule_next(dt_util.utcnow())
                self._stopping = False
            self._notify()
            if not await self._flush_state():
                if succeeded:
                    self.reason = "Shutdown persistence failed"
                succeeded = False
                self.shutdown_failed = True
                self.mode = MODE_MANUAL_OFF
                self._mark_state_dirty()
                self.detail = self.persistence_error
                if final:
                    self._stopped = True
                    self.next_evaluation_at = None
                else:
                    # Refuse reload so no second controller/store writer is created.
                    self._stopped = False
                    self._register_listeners()
                    self._schedule_next(dt_util.utcnow())
                self._stopping = False
                self._notify()
            return succeeded
