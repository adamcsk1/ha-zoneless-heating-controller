"""Backwards-compatible controller enable switch."""

from homeassistant.components.switch import SwitchEntity
from homeassistant.helpers.device_registry import DeviceEntryType
from homeassistant.helpers.entity import DeviceInfo, EntityCategory

from .const import DOMAIN


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([EnableSwitch(hass.data[DOMAIN][entry.entry_id], entry)])


class EnableSwitch(SwitchEntity):
    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_translation_key = "enabled"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, controller, entry):
        self.controller = controller
        self._attr_unique_id = f"{entry.entry_id}_enabled"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)}, name=entry.title,
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def is_on(self):
        return self.controller.enabled

    async def async_added_to_hass(self):
        self.async_on_remove(self.controller.listen(self.async_write_ha_state))

    async def async_turn_on(self, **kwargs):
        await self.controller.async_set_enabled(True)

    async def async_turn_off(self, **kwargs):
        await self.controller.async_set_enabled(False)
