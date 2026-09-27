"""ha-carnet-scolaire -- a Home Assistant PRONOTE integration built for automations.

The directing objective is in ``docs/SPECIFICATION.md`` §1: make automations on
PRONOTE data simple to write. Every structural choice here follows from it, and
the success criterion is precise -- **any ordinary school automation must be
writable without a Jinja template.**

Set-up order matters. The account logs in once, learns its own shape (children,
periods, current period), and only then are the platforms forwarded: an entity
that does not know which child it belongs to has no stable ``unique_id``, and
§2.4 makes identifier stability a guarantee rather than an aspiration.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Final, cast

from homeassistant.const import Platform
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryNotReady,
    HomeAssistantError,
)
from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from pronotepy.exceptions import PronoteAPIError

from .account import PronoteAccount
from .attachment import async_register_view
from .connectors.ecoledirecte.ed_client import EcoleDirecteClient
from .connectors.errors import (
    ConnectorChallengeRequired,
    ConnectorCredentialsError,
    ConnectorError,
    ConnectorSessionExpiredError,
    ConnectorTransportError,
    ConnectorUndecodableError,
)
from .connectors.factory import source_from_entry_data
from .connectors.protocol import Source
from .const import DEFAULT_READ_TIMEOUT, DOMAIN, OPT_READ_TIMEOUT
from .failures import describe_failure, is_transport_failure
from .options import bounded_option
from .ratelimit import LoginRefusedByLimiter
from .services import async_setup_services
from .session import (
    AccountUnreadable,
    BootstrapFailed,
    IntegrationFault,
    InvalidCredentials,
    LoginRefused,
    MfaRequired,
)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.typing import ConfigType

    from .connectors.ecoledirecte.ed_client import Transport

_LOGGER: Final = logging.getLogger(__name__)

PLATFORMS: Final[list[Platform]] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.CALENDAR,
    Platform.EVENT,
    Platform.IMAGE,
    Platform.SENSOR,
    Platform.TODO,
]

type PronoteConfigEntry = ConfigEntry[PronoteAccount]

#: There is no YAML configuration for this integration and there never will be:
#: a PRONOTE account needs a login before it can name its own children, and a
#: login cannot happen while YAML is being read. Declaring the schema is not a
#: formality either -- defining `async_setup` without it silently accepts a
#: `carnet_scolaire:` block in `configuration.yaml` and does nothing with it, which
#: is the most confusing possible answer to somebody who wrote one.
CONFIG_SCHEMA: Final = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:  # noqa: ARG001 -- required by the integration contract
    """Register the actions, once, independently of any account.

    Registered here and not from ``async_setup_entry`` for a reason that is
    about the automation editor rather than about tidiness: Home Assistant can
    only validate an automation against actions that *exist*, so an automation
    referencing ``carnet_scolaire.mark_homework_done`` was unvalidatable whenever the
    entry happened to be unloaded -- and being driven by automations is this
    integration's stated purpose (§1).

    The handlers already have the shape this requires: each resolves its account
    at call time from the target device and raises ``ServiceValidationError`` for
    a device it cannot place. So the action is always present and fails with a
    reason when no account is loaded, instead of being absent and failing with
    "action not found".

    Nothing is unregistered on unload, deliberately. Removing an action because
    the last entry went away is what made the editor's view of the world depend
    on whether a school's server happened to be reachable at start-up.
    """
    async_setup_services(hass)
    # Same argument, one line down: a route is per Home Assistant. It answers
    # for whichever account the address names, and says so with a status when
    # that account is not loaded -- which beats a 404 from aiohttp that a
    # reader cannot tell from a typo.
    async_register_view(hass)
    return True


def _chain_for_log(error: Exception) -> Exception | None:
    """The cause to chain a set-up refusal to, or ``None`` to hide it.

    Home Assistant writes the traceback of a ``ConfigEntryNotReady`` at DEBUG,
    cause included, and the message of a transport cause quotes the
    establishment's host and the path it failed on. Every other cause is kept:
    it is what a bug report about a real fault needs.
    """
    return None if is_transport_failure(error) else error


def _setup_error[E: HomeAssistantError](
    kind: type[E], key: str, error: BaseException
) -> E:
    """A set-up failure Home Assistant can show in the user's language.

    It replaces ``kind(str(error))``, which put the underlying exception's text
    on the integration card and in the log. That text is not ours: a
    ``pronotepy`` or transport error can quote a server page, or an address the
    user pasted with its session parameters still on it (`urls.py` exists for
    exactly that). The only placeholder is `describe_failure`: class names and
    a fixed category, never a message, which still tells a bug report which
    arm was taken and why. Whether the cause is chained is the caller's
    decision, through `_chain_for_log`.
    """
    return kind(
        translation_domain=DOMAIN,
        translation_key=key,
        translation_placeholders={"error_type": describe_failure(error)},
    )


async def async_setup_entry(hass: HomeAssistant, entry: PronoteConfigEntry) -> bool:
    """Set up one PRONOTE account."""
    connector_client = (
        EcoleDirecteClient(
            cast("Transport", async_create_clientsession(hass)),
            read_timeout=bounded_option(
                entry.options, OPT_READ_TIMEOUT, DEFAULT_READ_TIMEOUT
            ),
        )
        if source_from_entry_data(dict(entry.data)) is Source.ECOLEDIRECTE
        else None
    )
    account = PronoteAccount(hass, entry, connector_client=connector_client)

    try:
        await account.async_setup()
    except (
        InvalidCredentials,
        MfaRequired,
        ConnectorCredentialsError,
        ConnectorChallengeRequired,
    ) as error:
        # A re-authentication flow, never a silently broken entry (§7.2). For
        # MFA specifically the flow has to *ask for the PIN*: a secret we refuse
        # to store is a secret we must know how to request again (§8.1).
        await account.async_unload()
        raise _setup_error(ConfigEntryAuthFailed, "setup_auth_failed", error) from error
    except (BootstrapFailed, ConnectorTransportError) as error:
        # Deliberately does not claim a cause. pronotepy decides an address is
        # suspended with `if "IP" in html` -- two capitals anywhere in the page
        # -- and telling parents their home connection is banned because of a
        # school's footer would be worse than saying nothing (§6.3). A transport
        # failure is the same class of event on the other source.
        account.async_open_bootstrap_issue()
        await account.async_unload()
        # The message is ours and fixed; the chain is not. Home Assistant logs
        # the "Full exception" at DEBUG on this integration's logger, and the
        # traceback prints the `aiohttp` or `requests` cause, host and path.
        raise _setup_error(
            ConfigEntryNotReady, "setup_unreachable", error
        ) from _chain_for_log(error)
    except (
        LoginRefused,
        LoginRefusedByLimiter,
        IntegrationFault,
        ConnectorSessionExpiredError,
    ) as error:
        # Retry, and open nothing. These are ordinary ends of a session rather
        # than faults to report: the connector has already dropped the tokens
        # it held, so the retry logs in again. This arm sits *above* the
        # `ConnectorError` catch-all deliberately -- an expired token that fell
        # through to it opened a repair asking the user to file a bug report,
        # for a condition that fixed itself on the next attempt, and no code
        # path ever closes that card again.
        await account.async_unload()
        raise _setup_error(
            ConfigEntryNotReady, "setup_login_postponed", error
        ) from error
    except (AccountUnreadable, ConnectorUndecodableError, ConnectorError) as error:
        # The server answered and the credentials were fine; what came back is
        # outside the declared contract. Retrying is right -- establishments do
        # publish transient nonsense -- but the limiter holds it back, so this
        # does not become a loop. An unreadable payload raises
        # `ConnectorUndecodableError`; a bare `ConnectorError` is the last
        # resort, and catching the base class is what stops any connector
        # error from reaching set-up unclassified. Both must name the same
        # repair as `AccountUnreadable`, never `ConfigEntryAuthFailed`.
        account.async_open_unreadable_issue()
        await account.async_unload()
        raise _setup_error(ConfigEntryNotReady, "setup_unreadable", error) from error
    except PronoteAPIError as error:
        # A protocol-level refusal with no `Erreur.G` we recognise. It is the
        # server's answer, so "not ready" and retry -- never "auth failed",
        # which would send the user to re-enter credentials that are correct.
        await account.async_unload()
        raise _setup_error(
            ConfigEntryNotReady, "setup_server_refused", error
        ) from error
    except (TimeoutError, OSError) as error:
        # Classified, not quoted, and not chained. Home Assistant logs this
        # message and shows it as the entry's state, then logs the "Full
        # exception" at DEBUG -- and the text of a `requests` error names the
        # establishment's host and the path it failed on.
        await account.async_unload()
        raise _setup_error(ConfigEntryNotReady, "setup_network_error", error) from None

    entry.runtime_data = account
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = account

    # The account device, created here rather than left to whichever platform
    # happens to register a child first. Every child device declares
    # `via_device_id` on it, and the seven platforms are forwarded in parallel: on
    # a first start the child devices were created before their parent existed,
    # which Home Assistant reports as a non-existent parent device
    # and answers by flattening the device tree. It repaired itself on the next
    # restart, which is exactly why it survived review.
    account_device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
        manufacturer="PRONOTE",
        model="Account",
        name=account.establishment_name,
    )
    # Kept because the children need the *registry id* to point at their parent,
    # not the identifier tuple; see `PronoteAccount.account_device_id`.
    account.account_device_id = account_device.id

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Only now, and never before this line: the entities exist and have
    # subscribed to their coordinators, so the first batch's snapshots reach
    # them instead of being published to nobody. See
    # `PronoteAccount.async_start_first_collection`.
    account.async_start_first_collection()

    # No update listener, deliberately. An options save is reloaded by the
    # options flow itself (`PronoteOptionsFlow`, an `OptionsFlowWithReload`),
    # and a listener here would make Home Assistant report every
    # re-authentication and reconfiguration of a loaded entry as a mistake --
    # see `PronoteConfigFlow._async_finish_reauth`.
    return True


async def async_unload_entry(hass: HomeAssistant, entry: PronoteConfigEntry) -> bool:
    """Tear one account down without ever joining its worker thread."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    account: PronoteAccount | None = hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    if account is not None:
        await account.async_unload()

    # The actions are *not* removed. They belong to the integration, not to this
    # entry (see `async_setup`), and an automation referencing one must stay
    # validatable while the account is down -- which is the whole point of
    # registering them there.
    return unloaded


async def async_remove_config_entry_device(
    hass: HomeAssistant,  # noqa: ARG001 -- required by the integration contract
    entry: PronoteConfigEntry,
    device: dr.DeviceEntry,
) -> bool:
    """Whether the user may delete this device from the device page.

    Without this hook Home Assistant hides the delete button and answers the
    WebSocket call with "Config entry does not support device removal". That
    was the state when §10.5 of the user guide, and the release notes of
    v0.0.10, both told the reader to delete the stale child device: an
    instruction the code made impossible. The button is the whole remedy
    offered for a generation the repair deliberately leaves alone, so its
    absence turned a documented cleanup into a dead end.

    A **child** device may be removed. If the child is still followed the
    entry recreates it on the next reload, which is Home Assistant's normal
    behaviour for a device that is still there and costs nothing; if it is a
    superseded generation, this is the only way to be rid of it.

    The **account** device may not. Every child declares it as `via_device`,
    so removing it would leave the children pointing at a parent that no
    longer exists -- the exact state that once flattened the device tree until
    the next restart.
    """
    return (DOMAIN, entry.entry_id) not in device.identifiers
