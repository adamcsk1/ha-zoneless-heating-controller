"""Constants for Zoneless Heating Controller."""

from homeassistant.const import Platform

DOMAIN = "zoneless_heating_controller"
DATA_RUNTIME = f"{DOMAIN}_runtime"
PLATFORMS = [Platform.SELECT, Platform.SENSOR, Platform.SWITCH]

MODE_AUTO = "auto"
MODE_MANUAL_ON = "manual_on"
MODE_MANUAL_OFF = "manual_off"
MODES = [MODE_AUTO, MODE_MANUAL_ON, MODE_MANUAL_OFF]

CONF_ROOMS_JSON = "rooms_json"
DEFAULT_OPTIONS = {
    "monitor_only": True,
    CONF_ROOMS_JSON: "[]",
    "min_on_minutes": 10,
    "min_off_minutes": 10,
    "start_threshold": 0.20,
    "stop_threshold": -0.10,
    "max_overshoot": 1.0,
    "sensor_timeout_minutes": 15,
    "healthy_minutes": 5,
}

OPTION_RANGES = {
    "min_on_minutes": (1, 180, int),
    "min_off_minutes": (1, 180, int),
    "start_threshold": (0.01, 1.0, float),
    "stop_threshold": (-1.0, 0.0, float),
    "max_overshoot": (0.2, 4.0, float),
    "sensor_timeout_minutes": (1, 180, int),
    "healthy_minutes": (1, 60, int),
}

COMMAND_TIMEOUT_SECONDS = 15
OFF_RETRY_SECONDS = 30
CONFIRMATION_POLL_SECONDS = 1
