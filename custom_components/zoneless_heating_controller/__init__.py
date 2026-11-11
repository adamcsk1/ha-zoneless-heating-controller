"""Zoneless Heating Controller integration."""

import logging
import uuid

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError

from .const import DATA_RUNTIME, DOMAIN, PLATFORMS
from .controller import HeatingController

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    controllers = hass.data.setdefault(DOMAIN, {})
    relay = entry.data.get("boiler_relay")
    if any(other.relay == relay for other in controllers.values()):
        raise ConfigEntryError(f"Boiler relay {relay} is already controlled by another entry")

    controller = HeatingController(hass, entry)
    # Reserve before the first await so concurrent setups cannot both claim the relay.
    controllers[entry.entry_id] = controller
    try:
        runtime_id = hass.data.setdefault(DATA_RUNTIME, uuid.uuid4().hex)
        await controller.async_load(runtime_id)
        # Restore mode before the controller can issue its first command.
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        await controller.async_start()
    except Exception:
        stopped = await controller.async_stop()
        if stopped:
            controllers.pop(entry.entry_id, None)
        raise
    entry.async_on_unload(entry.add_update_listener(_update_listener))
    return True


async def _update_listener(hass, entry):
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass, entry):
    controller = hass.data[DOMAIN][entry.entry_id]
    if not await controller.async_stop():
        _LOGGER.error(
            "Cannot unload %s: %s; existing controller remains active",
            entry.entry_id, controller.reason,
        )
        return False
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    else:
        await controller.async_resume()
    return ok
