"""Device triggers: the automation editor's half of §2.3.

Without this file, "when a lesson is cancelled for Camille" is something a user
has to build out of an event entity and a state condition. With it, they pick
the child's device and choose the trigger from a list. That is the difference
between an integration that *can* be automated and one that is designed to be.

Every trigger here is the same underlying mechanism as the ``event`` entities:
the account fires one bus signal per detected change, after the snapshots are
published. This module simply exposes that signal through the device-automation
contract, filtered on the entry, the child and the event type -- so the trigger
fires once, for the right child, and its ``trigger.event.data.attributes``
carries the change (annexe A §4).

What is offered depends on the source. A trigger fires only if some tier the
connector collects can produce its change, and an EcoleDirecte entry collects
no news, discussions or evaluations tier -- so "a message arrived" there is a
card that attaches cleanly and never fires. Such a trigger is neither listed
nor accepted (`_producible`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from homeassistant.components.device_automation import DEVICE_TRIGGER_BASE_SCHEMA
from homeassistant.components.device_automation.exceptions import (
    InvalidDeviceAutomationConfig,
)
from homeassistant.components.homeassistant.triggers import event as event_trigger
from homeassistant.const import (
    CONF_DEVICE_ID,
    CONF_DOMAIN,
    CONF_PLATFORM,
    CONF_TYPE,
)
from homeassistant.helpers import device_registry as dr
import voluptuous as vol

from .account import SIGNAL_DELTA
from .connectors.factory import capabilities_for_source, source_from_entry_data
from .const import (
    DOMAIN,
    EVENT_ABSENCE_ADDED,
    EVENT_DELAY_ADDED,
    EVENT_EVALUATION_ADDED,
    EVENT_GRADE_ADDED,
    EVENT_HOMEWORK_ADDED,
    EVENT_INFORMATION_ADDED,
    EVENT_LESSON_CANCELED,
    EVENT_LESSON_MOVED,
    EVENT_LESSON_RESTORED,
    EVENT_LESSON_STATUS_CHANGED,
    EVENT_MESSAGE_RECEIVED,
    EVENT_PUNISHMENT_ADDED,
    EVENT_ROOM_CHANGED,
    EVENT_TEACHER_CHANGED,
    Tier,
)
from .event import EVENTS

if TYPE_CHECKING:
    from homeassistant.core import CALLBACK_TYPE, HomeAssistant
    from homeassistant.helpers.trigger import TriggerActionType, TriggerInfo
    from homeassistant.helpers.typing import ConfigType

    from .connectors.protocol import ConnectorCapabilities

#: One trigger per event type, not per entity. A user thinking "a room
#: changed" should not have to know that six different changes share one
#: ``event`` entity (annexe A §4) -- and the count was itself wrong here,
#: which is the argument for the trigger list: `LESSON_EVENT_TYPES` is the
#: only place that knows how many there are.
TRIGGER_TYPES: Final = (
    EVENT_GRADE_ADDED,
    EVENT_HOMEWORK_ADDED,
    EVENT_LESSON_CANCELED,
    EVENT_LESSON_RESTORED,
    EVENT_LESSON_MOVED,
    EVENT_ROOM_CHANGED,
    EVENT_TEACHER_CHANGED,
    EVENT_LESSON_STATUS_CHANGED,
    EVENT_INFORMATION_ADDED,
    EVENT_ABSENCE_ADDED,
    EVENT_DELAY_ADDED,
    EVENT_PUNISHMENT_ADDED,
    EVENT_MESSAGE_RECEIVED,
    EVENT_EVALUATION_ADDED,
)

TRIGGER_SCHEMA: Final = DEVICE_TRIGGER_BASE_SCHEMA.extend(
    {vol.Required(CONF_TYPE): vol.In(TRIGGER_TYPES)}
)

#: The tier whose collection produces each change, read off the ``event``
#: entities rather than restated: an entity and a trigger for the same change
#: are the same bus signal, and the entity is already created only when the
#: connector collects its tier. Restating the mapping here is how the two
#: would one day disagree.
TRIGGER_TIER: Final[dict[str, Tier]] = {
    event_type: description.tier
    for description in EVENTS
    for event_type in description.event_types or ()
}


async def async_get_triggers(
    hass: HomeAssistant, device_id: str
) -> list[dict[str, Any]]:
    """Offer every change the source can produce, for a child's device only.

    The account device is deliberately excluded: it carries the budget
    diagnostics, and "a grade arrived" is not something that happens to an
    account -- it happens to a child (§2.3).
    """
    if _student_id(hass, device_id) is None:
        return []
    capabilities = _capabilities(hass, device_id)
    return [
        {
            CONF_PLATFORM: "device",
            CONF_DOMAIN: DOMAIN,
            CONF_DEVICE_ID: device_id,
            CONF_TYPE: trigger_type,
        }
        for trigger_type in TRIGGER_TYPES
        if _producible(capabilities, trigger_type)
    ]


async def async_validate_trigger_config(
    hass: HomeAssistant, config: ConfigType
) -> ConfigType:
    """Refuse a change the device's source can never produce.

    The editor no longer offers one, but an automation written by hand, pasted
    from a forum or imported in a blueprint is not built from that list. Home
    Assistant calls this once the integration is set up, which does not mean
    the entry is: one still retrying has no connector yet, which is why
    `_capabilities` falls back to the source named in the entry's data.
    """
    validated: ConfigType = TRIGGER_SCHEMA(config)
    _refuse_unproducible(hass, validated)
    return validated


async def async_attach_trigger(
    hass: HomeAssistant,
    config: ConfigType,
    action: TriggerActionType,
    trigger_info: TriggerInfo,
) -> CALLBACK_TYPE:
    """Attach by delegating to the core event trigger.

    Delegated rather than reimplemented so the trigger inherits everything the
    core one already gets right -- variable rendering, tracing, referenced-entity
    reporting -- and so this module holds no listener bookkeeping of its own.
    """
    device_id = config[CONF_DEVICE_ID]
    student_id = _student_id(hass, device_id)
    entry_id = _entry_id(hass, device_id)

    # Refusing beats attaching, and this is not defensive tidiness: a trigger
    # built with `student_id: None` is a *valid* event filter that no real
    # event can match. It attaches without error and fires nothing -- no
    # trace, no log entry, no `unavailable` entity -- so it is indistinguishable
    # from a quiet week. That happened: an automation on a child device whose
    # PRONOTE resource signature had rotated went silent, and the missed
    # announcement was noticed by the household, not by the integration.
    #
    # Minting our own child key (`child_keys.py`) stops the identifier moving,
    # but it cannot help a device left behind by an account that no longer
    # announces that child. For those, saying so is the only useful answer.
    if student_id is None or entry_id is None:
        raise InvalidDeviceAutomationConfig(
            translation_domain=DOMAIN,
            translation_key="trigger_device_not_followed",
            translation_placeholders={"device_id": device_id},
        )
    # Checked again at attach: Home Assistant skips the dynamic validation
    # when the integration could not be set up, and attaching is the last
    # point at which an impossible trigger can still be refused.
    _refuse_unproducible(hass, config)

    event_config = event_trigger.TRIGGER_SCHEMA(
        {
            event_trigger.CONF_PLATFORM: "event",
            event_trigger.CONF_EVENT_TYPE: SIGNAL_DELTA,
            # All three keys matter: the entry separates two accounts on one
            # instance, the student separates two children of one parent
            # account, and the type separates the twelve changes.
            event_trigger.CONF_EVENT_DATA: {
                "entry_id": entry_id,
                "student_id": student_id,
                "event_type": config[CONF_TYPE],
            },
        }
    )
    return await event_trigger.async_attach_trigger(
        hass, event_config, action, trigger_info, platform_type="device"
    )


async def async_get_trigger_capabilities(
    hass: HomeAssistant,  # noqa: ARG001 -- required by the device-automation contract
    config: ConfigType,  # noqa: ARG001 -- every trigger here takes no extra field
) -> dict[str, vol.Schema]:
    """No extra fields.

    A threshold on a grade or a subject filter belongs in the automation's
    condition, not here: baking it into the trigger would make the trigger
    fire-or-not invisible in a trace.
    """
    return {"extra_fields": vol.Schema({})}


def _capabilities(hass: HomeAssistant, device_id: str) -> ConnectorCapabilities | None:
    """What the device's source can produce, or ``None`` if nothing says.

    The loaded connector answers when there is one; otherwise the entry's own
    data names its source, so a disabled EcoleDirecte entry is not suddenly
    offered PRONOTE's triggers.
    """
    entry_id = _entry_id(hass, device_id)
    if entry_id is None:
        return None
    account = hass.data.get(DOMAIN, {}).get(entry_id)
    if account is not None:
        loaded: ConnectorCapabilities = account.connector.capabilities
        return loaded
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None:
        return None
    return capabilities_for_source(source_from_entry_data(dict(entry.data)))


def _producible(capabilities: ConnectorCapabilities | None, trigger_type: str) -> bool:
    """Whether some tier the source collects can produce this change.

    Unknown capabilities read as "yes": refusing then would hide triggers for
    a reason nobody could see, and nothing can fire on such a device anyway.
    """
    return capabilities is None or TRIGGER_TIER[trigger_type] in capabilities.tiers


def _refuse_unproducible(hass: HomeAssistant, config: ConfigType) -> None:
    """Raise the translated refusal for a change the source cannot produce."""
    device_id = config[CONF_DEVICE_ID]
    trigger_type = config[CONF_TYPE]
    capabilities = _capabilities(hass, device_id)
    if capabilities is None or _producible(capabilities, trigger_type):
        return
    raise InvalidDeviceAutomationConfig(
        translation_domain=DOMAIN,
        translation_key="trigger_type_unsupported",
        translation_placeholders={
            "type": trigger_type,
            "source": str(capabilities.source),
            "device_id": device_id,
        },
    )


def _identifier(hass: HomeAssistant, device_id: str) -> str | None:
    """This integration's identifier for a device, or ``None``."""
    device = dr.async_get(hass).async_get(device_id)
    if device is None:
        return None
    return next(
        (identifier for domain, identifier in device.identifiers if domain == DOMAIN),
        None,
    )


def _entry_id(hass: HomeAssistant, device_id: str) -> str | None:
    """The config entry a device belongs to."""
    identifier = _identifier(hass, device_id)
    if identifier is None:
        return None
    entry_id, _, _student = identifier.partition("_")
    return entry_id


def _student_id(hass: HomeAssistant, device_id: str) -> str | None:
    """The child a device belongs to, or ``None`` for the account device.

    Device identifiers are ``<entry_id>_<child_key>`` for a child and a bare
    ``<entry_id>`` for the account, so the presence of the suffix is the test.

    The suffix is the key this integration minted, while the bus events the
    trigger matches on carry the identifier PRONOTE announced this session --
    so it is translated here.

    Resolution happens at *attach* time and the stored trigger holds only a
    device and a type, never an identifier. That is necessary for an automation
    to survive a rotation but **not sufficient**, and the docstring here used
    to claim it was: both sides are re-derived, from the device identifier --
    which itself embedded the rotating `46#<signature>` until this integration
    began minting its own key. So the two were re-derived from a value that had
    also moved, and four automations out of four went silent on the live
    instance while this sentence said they would survive. The guarantee holds
    from the minted key onwards, and `async_attach_trigger` now refuses rather
    than attach a filter that cannot match.
    """
    identifier = _identifier(hass, device_id)
    if identifier is None:
        return None
    entry_id, separator, child_key = identifier.partition("_")
    if not separator or not child_key:
        return None
    account = hass.data.get(DOMAIN, {}).get(entry_id)
    if account is None:
        # The entry is not loaded, so nothing can be translated. Returning the
        # suffix keeps `async_get_triggers` able to answer "this is a child
        # device", which is all it asks; a trigger cannot fire meanwhile.
        return child_key
    resolved: str | None = account.student_id_for_key(child_key)
    return resolved
