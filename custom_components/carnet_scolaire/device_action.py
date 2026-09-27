"""Device actions: what an automation can *do* to a child.

Only the refresh actions are unconditional. Everything that writes to PRONOTE
is offered only when the user turned write operations on *and* the source
advertises the service behind it, because a greyed-out action a user can pick
and that then fails is worse than an action that is simply not there (§8.3).

Each action is a thin call onto the domain service of the same name, so there
is exactly one implementation of "tick a homework item" and the automation
trace shows the service call that actually happened.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from homeassistant.const import (
    CONF_DEVICE_ID,
    CONF_DOMAIN,
    CONF_TYPE,
)
from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.helpers.service import async_call_from_config
import voluptuous as vol

from .const import (
    DOMAIN,
    SERVICE_MARK_HOMEWORK_DONE,
    SERVICE_MARK_INFORMATION_READ,
    SERVICE_REFRESH,
    Tier,
)

if TYPE_CHECKING:
    from homeassistant.core import Context, HomeAssistant
    from homeassistant.helpers.typing import ConfigType, TemplateVarsType

ACTION_REFRESH: Final = "refresh"
ACTION_REFRESH_MARKS: Final = "refresh_marks"
ACTION_MARK_HOMEWORK_DONE: Final = "mark_homework_done"
ACTION_MARK_INFORMATION_READ: Final = "mark_information_read"

#: Actions that reach PRONOTE with a write. Gated on the option.
WRITE_ACTIONS: Final = (ACTION_MARK_HOMEWORK_DONE, ACTION_MARK_INFORMATION_READ)

#: The domain service each write action calls. A write is offered only if the
#: source advertises that service: EcoleDirecte advertises none, and the
#: option alone left "mark information read" in its editor -- an action whose
#: every run the service then refused.
WRITE_ACTION_SERVICE: Final[dict[str, str]] = {
    ACTION_MARK_HOMEWORK_DONE: SERVICE_MARK_HOMEWORK_DONE,
    ACTION_MARK_INFORMATION_READ: SERVICE_MARK_INFORMATION_READ,
}

ACTION_TYPES: Final = (
    ACTION_REFRESH,
    ACTION_REFRESH_MARKS,
    *WRITE_ACTIONS,
)

ACTION_SCHEMA: Final = cv.DEVICE_ACTION_BASE_SCHEMA.extend(
    {
        vol.Required(CONF_TYPE): vol.In(ACTION_TYPES),
        vol.Optional("homework_id"): cv.string,
        vol.Optional("done", default=True): cv.boolean,
        vol.Optional("information_id"): cv.string,
    }
)


async def async_get_actions(
    hass: HomeAssistant, device_id: str
) -> list[dict[str, Any]]:
    """Offer the refresh actions, and the writes only if they can land."""
    account = _account(hass, device_id)
    if account is None:
        return []

    capabilities = account.connector.capabilities
    types = list(ACTION_TYPES)
    if not account.write_enabled:
        types = [name for name in types if name not in WRITE_ACTIONS]
    types = [
        name
        for name in types
        if name not in WRITE_ACTION_SERVICE
        or WRITE_ACTION_SERVICE[name] in capabilities.services
    ]
    if not account.can_tick_homework:
        # PRONOTE records nothing a parent session ticks, so the editor does
        # not offer an action whose every run would fail.
        types = [name for name in types if name != ACTION_MARK_HOMEWORK_DONE]

    return [
        {
            CONF_DOMAIN: DOMAIN,
            CONF_DEVICE_ID: device_id,
            CONF_TYPE: action_type,
        }
        for action_type in types
    ]


async def async_get_action_capabilities(
    hass: HomeAssistant,  # noqa: ARG001 -- required by the contract
    config: ConfigType,
) -> dict[str, vol.Schema]:
    """Ask for the identifier the write actions need, and nothing otherwise."""
    action_type = config[CONF_TYPE]
    if action_type == ACTION_MARK_HOMEWORK_DONE:
        return {
            "extra_fields": vol.Schema(
                {
                    vol.Required("homework_id"): cv.string,
                    vol.Optional("done", default=True): cv.boolean,
                }
            )
        }
    if action_type == ACTION_MARK_INFORMATION_READ:
        return {"extra_fields": vol.Schema({vol.Required("information_id"): cv.string})}
    return {"extra_fields": vol.Schema({})}


async def async_call_action_from_config(
    hass: HomeAssistant,
    config: ConfigType,
    variables: TemplateVarsType,
    context: Context | None,
) -> None:
    """Translate the action into the matching domain service call."""
    device_id = config[CONF_DEVICE_ID]
    action_type = config[CONF_TYPE]

    data: dict[str, Any] = {}
    if action_type == ACTION_REFRESH:
        service = SERVICE_REFRESH
    elif action_type == ACTION_REFRESH_MARKS:
        service = SERVICE_REFRESH
        data = {"tiers": [Tier.MARKS.value]}
    elif action_type == ACTION_MARK_HOMEWORK_DONE:
        service = SERVICE_MARK_HOMEWORK_DONE
        data = {
            "homework_id": config["homework_id"],
            "done": config.get("done", True),
        }
    else:
        service = SERVICE_MARK_INFORMATION_READ
        data = {"information_id": config["information_id"]}

    await async_call_from_config(
        hass,
        {
            "service": f"{DOMAIN}.{service}",
            "data": {"device_id": device_id, **data},
        },
        blocking=True,
        variables=variables,
        context=context,
    )


def _account(hass: HomeAssistant, device_id: str) -> Any | None:
    """The loaded account a device belongs to, or ``None``."""
    device = dr.async_get(hass).async_get(device_id)
    if device is None:
        return None
    accounts = hass.data.get(DOMAIN, {})
    for domain, identifier in device.identifiers:
        if domain != DOMAIN:
            continue
        if identifier in accounts:
            return accounts[identifier]
        entry_id, _, _student = identifier.partition("_")
        if entry_id in accounts:
            return accounts[entry_id]
    return None
