"""Configuration validation shared by the options flow and controller."""

import json
import math

from homeassistant.core import valid_entity_id

from .const import DEFAULT_OPTIONS, OPTION_RANGES

ROOM_FIELDS = ("name", "entity_id", "min", "preferred", "max", "weight")


def _number(value, label):
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a number")
    try:
        value = float(value)
    except (TypeError, ValueError) as err:
        raise ValueError(f"{label} must be a number") from err
    if not math.isfinite(value):
        raise ValueError(f"{label} must be finite")
    return value


def normalize_rooms(raw):
    """Return rooms with numeric values, or raise a user-facing ValueError."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as err:
            raise ValueError(f"Invalid rooms JSON: {err.msg}") from err
    if not isinstance(raw, list):
        raise ValueError("Rooms must be a JSON array")

    rooms = []
    entity_ids = set()
    for index, room in enumerate(raw, 1):
        if not isinstance(room, dict) or any(key not in room for key in ROOM_FIELDS):
            raise ValueError(f"Room {index} needs {', '.join(ROOM_FIELDS)}")
        name = room["name"]
        entity_id = room["entity_id"]
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"Room {index} needs a name")
        if not isinstance(entity_id, str) or not valid_entity_id(entity_id) or not entity_id.startswith("sensor."):
            raise ValueError(f"Room {index} needs a valid sensor entity")
        if entity_id in entity_ids:
            raise ValueError(f"Room sensor {entity_id} is configured more than once")
        entity_ids.add(entity_id)
        low = _number(room["min"], f"Room {index} minimum")
        preferred = _number(room["preferred"], f"Room {index} preferred")
        high = _number(room["max"], f"Room {index} maximum")
        weight = _number(room["weight"], f"Room {index} weight")
        if not -50 < low < preferred < high < 80:
            raise ValueError(f"Room {index} needs min < preferred < max within -50..80 °C")
        if not 0 < weight <= 100:
            raise ValueError(f"Room {index} weight must be between 0 and 100")
        rooms.append({
            "name": name.strip(), "entity_id": entity_id,
            "min": low, "preferred": preferred, "max": high, "weight": weight,
        })
    return rooms


def normalize_options(options):
    """Validate and normalize all stored options without mutating the input."""
    result = dict(DEFAULT_OPTIONS)
    monitor_only = options.get("monitor_only", True)
    if not isinstance(monitor_only, bool):
        raise ValueError("Monitor only must be true or false")
    result["monitor_only"] = monitor_only
    result["rooms_json"] = json.dumps(normalize_rooms(options.get("rooms_json", "[]")))
    for key, (minimum, maximum, value_type) in OPTION_RANGES.items():
        value = _number(options.get(key, DEFAULT_OPTIONS[key]), key)
        if value_type is int and not value.is_integer():
            raise ValueError(f"{key} must be a whole number")
        if not minimum <= value <= maximum:
            raise ValueError(f"{key} must be between {minimum} and {maximum}")
        result[key] = value_type(value)
    return result
