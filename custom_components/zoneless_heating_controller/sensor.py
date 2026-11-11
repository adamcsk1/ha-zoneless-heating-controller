"""Demand and controller diagnostics."""

from homeassistant.components.sensor import SensorEntity
from homeassistant.helpers.device_registry import DeviceEntryType
from homeassistant.helpers.entity import DeviceInfo, EntityCategory

from .const import DOMAIN


async def async_setup_entry(hass, entry, async_add_entities):
    controller = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([DemandSensor(controller, entry), ReasonSensor(controller, entry)])


class BaseSensor(SensorEntity):
    _attr_should_poll = False
    _attr_has_entity_name = True

    def __init__(self, controller, entry):
        self.controller = controller
        self._attr_unique_id = f"{entry.entry_id}_{self.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)}, name=entry.title,
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_added_to_hass(self):
        self.async_on_remove(self.controller.listen(self.async_write_ha_state))


class DemandSensor(BaseSensor):
    key = "demand"
    _attr_translation_key = "demand"
    _attr_icon = "mdi:gauge"

    @property
    def native_value(self):
        return round(self.controller.demand, 3) if self.controller.demand is not None else None


class ReasonSensor(BaseSensor):
    key = "reason"
    _attr_translation_key = "reason"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:text-search"

    @property
    def native_value(self):
        return self.controller.reason

    @staticmethod
    def _iso(value):
        return value.isoformat() if value else None

    @property
    def extra_state_attributes(self):
        controller = self.controller
        relay_state = {True: "on", False: "off", None: "unavailable"}[controller._relay_state]
        return {
            "detail": controller.detail,
            "mode": controller.mode,
            "monitor_only": controller.monitor_only,
            "requested_heat": controller.request_heat,
            "reported_relay_state": relay_state,
            "active_fault": controller.active_fault,
            "sensor_fault": controller.sensor_fault,
            "overheated_rooms": controller.overheated_rooms,
            "command_error": controller.command_error,
            "persistence_error": controller.persistence_error,
            "shutdown_latched": controller._shutdown_latched,
            "on_delivery_uncertain": controller._on_uncertain,
            "monitor_transition_pending": controller._monitor_transition,
            "pending_command": controller._pending_state,
            "last_on_at": self._iso(controller._on_since),
            "last_off_at": self._iso(controller._off_since),
            "minimum_on_until": self._iso(controller._minimum_on_until),
            "minimum_off_until": self._iso(controller._minimum_off_until),
            "on_blocked_until": self._iso(controller._on_blocked_until),
            "recovery_until": self._iso(controller._recovery_until),
            "next_evaluation_at": self._iso(controller.next_evaluation_at),
        }
