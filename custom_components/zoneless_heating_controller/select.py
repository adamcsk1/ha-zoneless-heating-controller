"""Automatic and manual operating modes."""

from homeassistant.components.select import SelectEntity
from homeassistant.helpers.device_registry import DeviceEntryType
from homeassistant.helpers.entity import DeviceInfo

from .const import DOMAIN, MODES


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([ControllerModeSelect(hass.data[DOMAIN][entry.entry_id], entry)])


class ControllerModeSelect(SelectEntity):
    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_translation_key = "mode"
    _attr_options = MODES
    _attr_icon = "mdi:thermostat-auto"

    def __init__(self, controller, entry):
        self.controller = controller
        self._attr_unique_id = f"{entry.entry_id}_mode"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)}, name=entry.title,
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def current_option(self):
        return self.controller.mode

    async def async_added_to_hass(self):
        self.async_on_remove(self.controller.listen(self.async_write_ha_state))

    async def async_select_option(self, option):
        await self.controller.async_set_mode(option)
