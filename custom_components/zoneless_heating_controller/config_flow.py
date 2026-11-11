"""UI configuration for Zoneless Heating Controller."""

import math

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.helpers import selector

from .config import normalize_options
from .const import DEFAULT_OPTIONS, DOMAIN


def _number(value, whole=False):
    """Reject booleans, fractions for timers, and non-finite form values."""
    if isinstance(value, bool):
        raise vol.Invalid("Must be a number, not a boolean")
    try:
        number = float(value)
    except (TypeError, ValueError) as err:
        raise vol.Invalid("Must be numeric") from err
    if not math.isfinite(number) or (whole and not number.is_integer()):
        raise vol.Invalid("Must be a finite whole number" if whole else "Must be finite")
    return int(number) if whole else number


def _whole_number(value):
    return _number(value, whole=True)


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
            vol.Optional("min_on_minutes", default=options.get("min_on_minutes", DEFAULT_OPTIONS["min_on_minutes"])):
                vol.All(_whole_number, vol.Range(min=1, max=180)),
            vol.Optional("min_off_minutes", default=options.get("min_off_minutes", DEFAULT_OPTIONS["min_off_minutes"])):
                vol.All(_whole_number, vol.Range(min=1, max=180)),
            vol.Optional("start_threshold", default=options.get("start_threshold", DEFAULT_OPTIONS["start_threshold"])):
                vol.All(_number, vol.Range(min=0.01, max=1)),
            vol.Optional("stop_threshold", default=options.get("stop_threshold", DEFAULT_OPTIONS["stop_threshold"])):
                vol.All(_number, vol.Range(min=-1, max=0)),
            vol.Optional("max_overshoot", default=options.get("max_overshoot", DEFAULT_OPTIONS["max_overshoot"])):
                vol.All(_number, vol.Range(min=0.2, max=4)),
            vol.Optional("sensor_timeout_minutes", default=options.get("sensor_timeout_minutes", DEFAULT_OPTIONS["sensor_timeout_minutes"])):
                vol.All(_whole_number, vol.Range(min=1, max=180)),
            vol.Optional("healthy_minutes", default=options.get("healthy_minutes", DEFAULT_OPTIONS["healthy_minutes"])):
                vol.All(_whole_number, vol.Range(min=1, max=60)),
        })
