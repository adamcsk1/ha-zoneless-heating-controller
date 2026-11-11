"""UI configuration for Zoneless Heating Controller."""

import math

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.helpers import selector

from .config import normalize_options
from .const import DEFAULT_OPTIONS, DOMAIN, OPTION_RANGES


class StrictNumberSelector(selector.NumberSelector):
    """Keep numeric validation strict while exposing a standard number field."""

    def __call__(self, value):
        if isinstance(value, bool):
            raise vol.Invalid("Must be a number, not a boolean")
        number = super().__call__(value)
        if not math.isfinite(number):
            raise vol.Invalid("Must be finite")
        if self.config["step"] == 1 and not number.is_integer():
            raise vol.Invalid("Must be a whole number")
        return number


class ZonelessHeatingConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Create one controller for a boiler demand relay."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None:
            relay = user_input["boiler_relay"]
            if any(entry.data.get("boiler_relay") == relay for entry in self._async_current_entries()):
                errors["base"] = "relay_already_configured"
            else:
                await self.async_set_unique_id(relay)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title=user_input["name"], data=user_input)
        return self.async_show_form(
            step_id="user", errors=errors,
            data_schema=vol.Schema({
                vol.Required("name", default="Zoneless heating"): str,
                vol.Required("boiler_relay"): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="switch")
                ),
                vol.Required("outdoor_sensor"): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor", device_class="temperature")
                ),
            }),
        )

    @staticmethod
    def async_get_options_flow(config_entry):
        return ZonelessHeatingOptionsFlow(config_entry)


class ZonelessHeatingOptionsFlow(config_entries.OptionsFlow):
    """Edit heating settings."""

    def __init__(self, config_entry):
        self._entry = config_entry

    async def async_step_init(self, user_input=None):
        if user_input is not None:
            try:
                options = normalize_options(user_input)
            except ValueError:
                return self.async_show_form(
                    step_id="init", errors={"base": "invalid_rooms"},
                    data_schema=self._schema(user_input),
                )
            controller = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id)
            if options["monitor_only"] and controller and not controller.monitor_only:
                if not await controller.async_prepare_monitor_only():
                    return self.async_show_form(
                        step_id="init", errors={"base": "monitor_shutdown_failed"},
                        data_schema=self._schema(user_input),
                    )
            return self.async_create_entry(title="", data=options)
        return self.async_show_form(step_id="init", data_schema=self._schema())

    def _schema(self, values=None):
        options = self._entry.options if values is None else values
        return vol.Schema({
            vol.Optional("monitor_only", default=options.get("monitor_only", True)): bool,
            vol.Optional("rooms_json", default=options.get("rooms_json", "[]")): selector.TextSelector(
                selector.TextSelectorConfig(multiline=True)
            ),
            **{
                vol.Optional(key, default=options.get(key, DEFAULT_OPTIONS[key])): StrictNumberSelector(
                    selector.NumberSelectorConfig(
                        min=minimum, max=maximum,
                        step=1 if value_type is int else "any",
                        mode=selector.NumberSelectorMode.BOX,
                    )
                )
                for key, (minimum, maximum, value_type) in OPTION_RANGES.items()
            },
        })
