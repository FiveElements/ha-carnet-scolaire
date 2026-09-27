"""Device conditions: the questions an automation asks about a child.

"Only if it is a school day", "only if the child is not in class", "only if
homework is overdue". Each one maps onto a binary sensor that already exists,
so the condition is a thin, traceable wrapper over ``condition.state`` rather
than a second implementation of the predicate. Two implementations of "is it a
school day" would eventually disagree, and the disagreement would show up as an
automation that fires when its own condition card says it should not.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from homeassistant.const import (
    CONF_CONDITION,
    CONF_DEVICE_ID,
    CONF_DOMAIN,
    CONF_ENTITY_ID,
    CONF_STATE,
    CONF_TYPE,
    STATE_OFF,
    STATE_ON,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import (
    condition,
    config_validation as cv,
    entity_registry as er,
)
import voluptuous as vol

from .const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.condition import ConditionCheckerType
    from homeassistant.helpers.typing import ConfigType

#: ``condition type`` → the binary sensor key it reads, and the state it wants.
#: Both polarities are offered explicitly: an automation reading "if not in
#: class" is clearer than one negating a condition, and it is one fewer place
#: for a mistake.
CONDITION_MAP: Final[dict[str, tuple[str, str]]] = {
    "is_school_day": ("school_day", STATE_ON),
    "is_not_school_day": ("school_day", STATE_OFF),
    "is_in_class": ("in_class", STATE_ON),
    "is_not_in_class": ("in_class", STATE_OFF),
    "is_test_today": ("test_today", STATE_ON),
    "is_homework_overdue": ("homework_overdue", STATE_ON),
    "is_absence_in_progress": ("absence_in_progress", STATE_ON),
    "is_punishment_upcoming": ("punishment_upcoming", STATE_ON),
    "is_holidays": ("holidays", STATE_ON),
    "is_not_holidays": ("holidays", STATE_OFF),
}

CONDITION_SCHEMA: Final = cv.DEVICE_CONDITION_BASE_SCHEMA.extend(
    {
        vol.Required(CONF_TYPE): vol.In(CONDITION_MAP),
        vol.Optional(CONF_ENTITY_ID): vol.Any(str, None),
    }
)


async def async_get_conditions(
    hass: HomeAssistant, device_id: str
) -> list[dict[str, Any]]:
    """Offer only the conditions whose entity actually exists on this device.

    An establishment that publishes no attendance data has no
    ``absence_in_progress`` entity, and offering a condition that can never be
    true would be worse than offering nothing (§2.5).
    """
    # Matched on the *suffix*, not by splitting. A unique id is
    # `<entry>_<student>_<key>` and most keys contain an underscore
    # themselves, so taking the last segment answered `day` for `school_day`
    # and `class` for `in_class`: eight of the ten conditions silently never
    # appeared in the automation editor, and the two that did were the two
    # single-word keys. Suffix matching against the keys we actually want is
    # also immune to the unique-id scheme changing again.
    available: dict[str, str] = {}
    for key, _state in CONDITION_MAP.values():
        entity_id = _entity_for(hass, device_id, key)
        if entity_id is not None:
            available[key] = entity_id

    conditions: list[dict[str, Any]] = []
    for condition_type, (key, _state) in CONDITION_MAP.items():
        entity_id = available.get(key)
        if entity_id is None:
            continue
        conditions.append(
            {
                CONF_CONDITION: "device",
                CONF_DOMAIN: DOMAIN,
                CONF_DEVICE_ID: device_id,
                CONF_TYPE: condition_type,
                CONF_ENTITY_ID: entity_id,
            }
        )
    return conditions


def _entity_for(hass: HomeAssistant, device_id: str, key: str) -> str | None:
    """The binary sensor on ``device_id`` whose unique id ends in ``key``."""
    registry = er.async_get(hass)
    for entry in er.async_entries_for_device(registry, device_id):
        if entry.domain != "binary_sensor" or not entry.unique_id:
            continue
        if entry.unique_id.endswith(f"_{key}"):
            return entry.entity_id
    return None


def async_condition_from_config(
    hass: HomeAssistant,
    config: ConfigType,
) -> ConditionCheckerType:
    """Turn one device condition into a state check.

    Built as a real ``condition.state`` config so the automation trace shows
    which entity was consulted and what it held -- a hand-rolled closure would
    trace as an opaque "condition failed".

    ``entity_id`` is optional in the schema and is filled in from the device
    when it is absent. It has to be: the editor always supplies it, because
    `async_get_conditions` puts it there, but a condition written by hand or
    delivered in a blueprint carries only the device and the type -- and
    reading it unconditionally raised ``KeyError`` inside the automation, which
    surfaces as "this automation is broken" with no indication of which line.
    """
    key, wanted = CONDITION_MAP[config[CONF_TYPE]]
    entity_id = config.get(CONF_ENTITY_ID) or _entity_for(
        hass, config[CONF_DEVICE_ID], key
    )
    if entity_id is None:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="condition_entity_missing",
            translation_placeholders={
                "key": key,
                "device_id": config[CONF_DEVICE_ID],
            },
        )

    state_config = {
        CONF_CONDITION: "state",
        CONF_ENTITY_ID: entity_id,
        CONF_STATE: wanted,
    }
    validated = condition.state_validate_config(hass, state_config)
    return condition.state_from_config(validated)
