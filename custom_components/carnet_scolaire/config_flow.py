"""The configuration and options flows.

Three ways in, because PRONOTE has three: a QR code from the mobile app,
a direct username and password, and an ENT federated login. They are not
variations of one form -- QR enrolment produces a rotating token and needs a
device UUID, credentials mode does not, and ENT needs a provider chosen from a
list -- so each gets its own step rather than one form with fields that are
sometimes ignored.

Two things here are load-bearing.

**The 2FA PIN is never stored (§8.1).** It is asked for, used for the one login
that needs it, and dropped. Everything else -- the token, the UUID, the client
identifier -- is persisted, because losing those means losing access. The
re-authentication flow exists precisely so a secret we refuse to keep can be
requested again.

**Every login here goes through the executor and the limiter.** A config flow
that logged in on the event loop would block Home Assistant for the length of a
PRONOTE handshake, and one that retried a wrong password freely would spend the
three-attempt guard rail that protects the address from suspension (annexe B
§3).
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, Final, cast
import uuid as uuid_module

from homeassistant.config_entries import (
    SOURCE_RECONFIGURE,
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
    TimeSelector,
)
from homeassistant.util import dt as dt_util
import voluptuous as vol

from .child_keys import pair
from .connectors.ecoledirecte.connector import EcoledirecteConnector
from .connectors.ecoledirecte.ed_client import EcoleDirecteClient
from .connectors.ecoledirecte.ed_limiter import EdRateLimiter
from .connectors.ecoledirecte.ed_mapping import students_from_accounts
from .connectors.errors import (
    ConnectorChallengeRequired,
    ConnectorCredentialsError,
    ConnectorTransportError,
)
from .connectors.factory import source_from_entry_data
from .connectors.protocol import Source
from .const import (
    BRAND_LOGO_URL,
    CONF_ACCOUNT_PIN,
    CONF_CHILD_KEYS,
    CONF_CHILDREN,
    CONF_CLIENT_IDENTIFIER,
    CONF_DEVICE_NAME,
    CONF_ENT,
    CONF_LOGIN_MODE,
    CONF_PRONOTE_URL,
    CONF_QR_PAYLOAD,
    CONF_QR_PIN,
    CONF_SOURCE,
    CONF_UUID,
    DEFAULT_HOMEWORK_HORIZON,
    DEFAULT_MASTER_TICK,
    DEFAULT_SESSION_STRATEGY,
    DEFAULT_STALE_AFTER,
    DEFAULT_TIER_INTERVALS,
    DEFAULT_WAKE_MARGIN,
    DEFAULT_WRITE_OPERATIONS_ENABLED,
    DOMAIN,
    LEGACY_WRITE_OPERATIONS_ENABLED,
    OPT_BACKOFF_BASE,
    OPT_BACKOFF_MAX,
    OPT_BOOTSTRAP_HOLD,
    OPT_BURST_SIZE,
    OPT_CONNECT_TIMEOUT,
    OPT_CREDENTIALS_HOLD,
    OPT_ESTABLISHMENT_TIMEZONE,
    OPT_HOMEWORK_HORIZON,
    OPT_MASTER_TICK,
    OPT_MAX_FAILED_LOGINS_PER_HOUR,
    OPT_MAX_LOGINS_PER_DAY,
    OPT_MAX_REQUESTS_PER_DAY,
    OPT_MAX_REQUESTS_PER_HOUR,
    OPT_MAX_WAIT,
    OPT_MIN_REQUEST_INTERVAL,
    OPT_QUIET_END,
    OPT_QUIET_HOURS_ENABLED,
    OPT_QUIET_START,
    OPT_READ_TIMEOUT,
    OPT_SESSION_STRATEGY,
    OPT_STALE_AFTER,
    OPT_TIER_ENABLED,
    OPT_TIER_INTERVAL,
    OPT_WAKE_MARGIN,
    OPT_WRITE_OPERATIONS_ENABLED,
    OPTION_RANGES,
    TIER_INTERVAL_RANGE,
    LoginMode,
    SessionStrategy,
)
from .ent_providers import ent_provider_names
from .failures import describe_failure, is_transport_failure
from .login_guard import clear_login_penalties, limiter_state_store, login_guard
from .options import (
    build_rate_limit_config,
    estimate_daily_requests,
    estimate_ecoledirecte_daily_requests,
    tier_enabled,
    tier_intervals,
)
from .ratelimit import REQUESTS_PER_LOGIN, LoginOutcome, LoginRefusedByLimiter
from .urls import public_url, url_host

if TYPE_CHECKING:
    from collections.abc import Mapping

    from .connectors.ecoledirecte.ed_client import Transport

_LOGGER: Final = logging.getLogger(__name__)

STEP_USER: Final = "user"
STEP_PRONOTE: Final = "pronote"
STEP_QR_CODE: Final = "qr_code"
STEP_CREDENTIALS: Final = "credentials"
STEP_ENT: Final = "ent"
STEP_ECOLEDIRECTE: Final = "ecoledirecte"
STEP_ECOLEDIRECTE_QCM: Final = "ecoledirecte_qcm"
STEP_CHILDREN: Final = "children"
STEP_REAUTH_CONFIRM: Final = "reauth_confirm"
STEP_REAUTH_QR: Final = "reauth_qr"
STEP_RECONFIGURE: Final = "reconfigure"


def _ent_options() -> list[SelectOptionDict]:
    """The ENT providers a login can actually use.

    Read from the installed library rather than hard-coded: a list copied into
    this file would silently rot at the next upstream release, and the failure
    mode is a user picking a provider that no longer exists. The list comes
    from :mod:`.ent_providers`, which also binds the portals ``pronotepy``
    writes but leaves disabled, so what the form offers and what a login
    resolves cannot drift apart.
    """
    return [SelectOptionDict(value=name, label=name) for name in ent_provider_names()]


_URL_SELECTOR: Final = TextSelector(TextSelectorConfig(type=TextSelectorType.URL))
_TEXT: Final = TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT))
_PASSWORD: Final = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))

QR_SCHEMA: Final = vol.Schema(
    {
        # The raw JSON from the app's QR code. Pasted rather than scanned
        # because Home Assistant has no camera in a config flow, and it is
        # single-use: PRONOTE invalidates it once enrolled.
        vol.Required(CONF_QR_PAYLOAD): TextSelector(
            TextSelectorConfig(type=TextSelectorType.TEXT, multiline=True)
        ),
        vol.Required(CONF_QR_PIN): _PASSWORD,
        vol.Optional(CONF_DEVICE_NAME, default="Home Assistant"): _TEXT,
        vol.Optional(CONF_ACCOUNT_PIN): _PASSWORD,
    }
)

CREDENTIALS_SCHEMA: Final = vol.Schema(
    {
        vol.Required(CONF_PRONOTE_URL): _URL_SELECTOR,
        vol.Required("username"): _TEXT,
        vol.Required("password"): _PASSWORD,
        vol.Optional(CONF_ACCOUNT_PIN): _PASSWORD,
    }
)

ECOLEDIRECTE_SCHEMA: Final = vol.Schema(
    {
        vol.Required("username"): _TEXT,
        vol.Required("password"): _PASSWORD,
    }
)


def _ecoledirecte_qcm_schema(propositions: tuple[str, ...]) -> vol.Schema:
    """One choice among the decoded propositions, not a JSON object to type."""
    return vol.Schema(
        {
            vol.Required("choice"): SelectSelector(
                SelectSelectorConfig(
                    options=list(propositions),
                    mode=SelectSelectorMode.LIST,
                )
            )
        }
    )


def _ent_schema() -> vol.Schema:
    """The ENT form, built at call time so the provider list is current."""
    return vol.Schema(
        {
            vol.Required(CONF_PRONOTE_URL): _URL_SELECTOR,
            vol.Required("username"): _TEXT,
            vol.Required("password"): _PASSWORD,
            vol.Required(CONF_ENT): SelectSelector(
                SelectSelectorConfig(
                    options=_ent_options(),
                    mode=SelectSelectorMode.DROPDOWN,
                    sort=True,
                    custom_value=True,
                )
            ),
        }
    )


REAUTH_SCHEMA: Final = vol.Schema(
    {
        vol.Optional("password"): _PASSWORD,
        # The reason this flow exists. §8.1 refuses to store the PIN, so it has
        # to be askable again -- otherwise "we do not keep your PIN" would mean
        # "the integration breaks the first time PRONOTE asks for it".
        vol.Optional(CONF_ACCOUNT_PIN): _PASSWORD,
    }
)


#: Keys that must never reach a config entry, wherever the entry comes from.
#: The QR payload is single-use and PRONOTE invalidates it on enrolment; its
#: four-digit code and the account's two-factor PIN are never persisted (§8.1);
#: `account_id` exists only long enough to set the unique id.
#:
#: One set rather than a literal per call site, because there were two call
#: sites and only one list: the re-authentication path stripped the PIN and
#: nothing else, so a QR re-enrolment would have persisted the payload it had
#: just promised not to keep.
_NEVER_PERSISTED: Final = frozenset(
    {CONF_QR_PAYLOAD, CONF_QR_PIN, CONF_ACCOUNT_PIN, "account_id"}
)
_ED_PERSISTED: Final = frozenset(
    {CONF_SOURCE, "username", "password", "qcm_json", CONF_CHILD_KEYS}
)


def _log_rejected_payload(error: Exception, raw: object) -> None:
    """Say *why* a pasted QR code was rejected, without echoing it.

    This rejection is the one flow failure that never reaches the network, and
    it logged nothing at all -- so a parent whose messaging app had quietly
    added characters to the JSON, and this happened, saw only "that does not
    look like the content of a PRONOTE QR code" while the log stayed silent.

    What is recorded is the *shape*: the exception's own message, the length of
    what was pasted, and whether it even starts like a JSON object. Never the
    content, because the content is a working credential -- the ``login`` and
    ``jeton`` of the QR code -- and this log is what a user is invited to
    attach to a public issue (§8.2). A length and a first character are enough
    to tell "pasted an image filename" from "pasted valid JSON of the wrong
    shape", which is the whole question here.
    """
    text = raw if isinstance(raw, str) else ""
    _LOGGER.debug(
        "the pasted QR code was rejected: %s(%s); %d characters, %s",
        type(error).__name__,
        error,
        len(text),
        "starts with an object brace"
        if text.strip().startswith("{")
        else "not an object",
    )


def _log_refusal(error: BaseException) -> None:
    """Say *why* a login was refused, on this integration's own logger.

    A classified failure used to log nothing at all -- only the unexpected ones
    wrote a traceback -- so "PRONOTE refused these credentials" was the whole of
    what a bug report could contain, and the four causes behind it were
    indistinguishable. Diagnosing a real one meant guessing, twice in one day.

    At DEBUG, because DEBUG is what a user turns on before reporting a bug, and
    strictly more conservative than the ERROR-level traceback the unexpected arm
    already writes unconditionally. The category is ours and the cause is
    upstream's: `pronotepy` raises static messages here ("challenge decryption
    failed", "invalid confirmation code"), which is exactly the distinction that
    was missing.

    What this must never become is `logging.getLogger("pronotepy")` at DEBUG:
    `pronoteAPI.py` writes the hexadecimal of every request body there,
    credentials included (§8.2).
    """
    cause = error.__cause__
    _LOGGER.debug(
        "the login was refused: %s, caused by %s",
        _refusal_shape(error),
        _refusal_shape(cause) if cause is not None else "nothing",
    )


def _refusal_shape(error: BaseException) -> str:
    """One link of a refusal, as ``_log_refusal`` may print it.

    A classified refusal keeps its message: upstream's are static, and they are
    the distinction the line exists for. A transport failure does not. The text
    of a ``requests`` error quotes the establishment's host and the path it
    failed on -- a page path that can carry session parameters -- and this is
    the log a user attaches to a public issue, so it is reduced to class names
    and a category (:func:`~.failures.describe_failure`).
    """
    if is_transport_failure(error):
        return describe_failure(error)
    return f"{type(error).__name__}({error})"


def _reset_ed_entry_penalties(hass: HomeAssistant, entry_id: str | None) -> None:
    """Clear this ED entry's hold so a human reauth can submit immediately."""
    if entry_id is not None:
        limiter_state_store(hass).pop(entry_id, None)
        account = hass.data.get(DOMAIN, {}).get(entry_id)
        limiter = getattr(account, "limiter", None)
        if limiter is not None:
            limiter.reset_after_reauth()
    flow_limiter = hass.data.get(f"{DOMAIN}_ecoledirecte_flow_limiter")
    if isinstance(flow_limiter, EdRateLimiter):
        flow_limiter.reset_after_reauth()


def _account_identity(account_id: str | None) -> tuple[str, str]:
    """The parts of an account id that identify it *stably*.

    ``flow_login._account_id`` builds ``<establishment url>::<PRONOTE resource
    N>``, and the ``N`` of a parent resource is regularly written
    ``46#<signature>`` -- an establishment-local number followed by an opaque
    blob. That blob is undocumented and is not reliably stable across
    enrolments, so comparing the whole string would answer "is this the same
    *session*" where the question is "is this the same *account*" -- and a
    parent re-enrolling their own account from a fresh QR code would be told it
    belongs to somebody else, with no way out but deleting the entry.

    So the address and the number are compared and the signature is dropped.
    ``<url>::46`` is still a real identity -- resource 46 at that establishment
    -- and it cannot collide with a sibling, who carries a different number.

    The stored ``unique_id`` is deliberately left alone: every existing entry
    holds the full form, and narrowing it would orphan them.
    """
    url, _, resource = (account_id or "").rpartition("::")
    return url, resource.partition("#")[0]


def _same_account(existing_unique_id: str | None, account_id: Any) -> bool:
    """Whether a fresh login landed on the account an entry already follows."""
    return _account_identity(existing_unique_id) == _account_identity(str(account_id))


def _same_account_on_host(existing_unique_id: str | None, account_id: Any) -> bool:
    """Whether a reconfigured login landed on the account the entry follows.

    Looser than :func:`_same_account` in exactly one respect, and only because
    reconfiguring exists to allow it: the *path* of the address may change.
    Correcting ``eleve.html`` into ``parent.html``, or pasting the page an ENT
    opens instead of a copied deep link, is the ordinary reason somebody opens
    this form -- and ``flow_login._account_id`` writes the whole address into
    the identity, so comparing it would refuse the very correction the form is
    for.

    The host and the resource number are still both required. The number is
    local to one establishment: resource 46 at another school is another
    family. An identity this function cannot read, with no host, is refused
    rather than waved through, because the failure this guards against is an
    entry silently repointed at somebody else's child, keeping this child's
    name, devices and history.
    """
    old_url, old_resource = _account_identity(existing_unique_id)
    new_url, new_resource = _account_identity(str(account_id))
    old_host = url_host(old_url)
    return (
        bool(old_host)
        and bool(old_resource)
        and old_host == url_host(new_url)
        and old_resource == new_resource
    )


#: What a PRONOTE reconfiguration replaces in the entry's data: the keys that
#: say *how* it connects. Everything else -- the followed children, the minted
#: child keys, anything the running account persisted -- is kept.
#:
#: Dropped from the old data rather than overwritten by the new, because a mode
#: change does not re-supply every key: an entry moved from ENT to direct
#: credentials would otherwise keep its `ent` and go on logging in through the
#: portal it had just been moved off, and one moved off a QR code would keep
#: the device identity of an enrolment it no longer uses.
_CONNECTION_KEYS: Final = frozenset(
    {
        CONF_LOGIN_MODE,
        CONF_PRONOTE_URL,
        "username",
        "password",
        CONF_ENT,
        CONF_UUID,
        CONF_CLIENT_IDENTIFIER,
        CONF_DEVICE_NAME,
    }
)


def _probe(data: dict[str, Any]) -> dict[str, Any]:
    """Import ``flow_login`` and run its probe, both on the worker thread.

    The import is deliberately here rather than at module scope, for the reason
    ``flow_login``'s own docstring gives: the flow must not import
    ``pronotepy`` at import time, or a broken dependency turns "cannot log in"
    into "cannot even be added". But it must not be inside a coroutine either
    -- Home Assistant instruments ``importlib`` and reports an import from the
    event loop as a blocking call -- so it happens here, in the executor,
    immediately before the login it belongs to.
    """
    from .flow_login import probe_account  # noqa: PLC0415 -- see the docstring

    return probe_account(data)


async def _probe_ecoledirecte(
    hass: HomeAssistant,
    username: str,
    password: str,
    qcm_json: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Authenticate against EcoleDirecte with its own limiter and HTTP session."""
    answers = qcm_json if qcm_json is not None else {}
    limiter = _ecoledirecte_flow_limiter(hass)
    client = EcoleDirecteClient(
        cast("Transport", async_create_clientsession(hass)),
    )
    try:
        payload = await limiter.login(
            lambda: client.login(
                username,
                password,
                answers,
                charge_qcm=limiter.charge_qcm,
            ),
            cost=2,
        )
    except ConnectorCredentialsError:
        limiter.note_bad_credentials()
        raise
    except ConnectorChallengeRequired:
        limiter.note_qcm()
        raise
    except ConnectorTransportError:
        limiter.note_transport_failure(bootstrap=True)
        raise
    limiter.note_success()
    return {
        "students": tuple(
            (student.id, student.name) for student in students_from_accounts(payload)
        )
    }


def _ecoledirecte_flow_limiter(hass: HomeAssistant) -> EdRateLimiter:
    """Return the instance-wide enrolment guard dedicated to EcoleDirecte."""
    key = f"{DOMAIN}_ecoledirecte_flow_limiter"
    limiter = hass.data.get(key)
    if isinstance(limiter, EdRateLimiter):
        return limiter
    limiter = EdRateLimiter(build_rate_limit_config({}))
    hass.data[key] = limiter
    return limiter


class PronoteConfigFlow(ConfigFlow, domain=DOMAIN):
    """Walk the user through one PRONOTE account."""

    VERSION = 1
    MINOR_VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._children: list[tuple[str, str]] = []
        self._reauth_entry: ConfigEntry | None = None
        self._ecoledirecte_qcm: dict[str, Any] = {}
        self._ecoledirecte_question: str = ""
        self._ecoledirecte_propositions: tuple[str, ...] = ()

    @staticmethod
    @callback
    def async_get_options_flow(
        entry: ConfigEntry,  # noqa: ARG004 -- required by the flow contract
    ) -> PronoteOptionsFlow:
        """Return the options flow."""
        return PronoteOptionsFlow()

    # -- entry point -------------------------------------------------------

    async def async_step_user(
        self,
        user_input: dict[str, Any] | None = None,  # noqa: ARG002 -- a menu, not a form
    ) -> ConfigFlowResult:
        """Ask which school platform the account lives on.

        The platform comes first because it decides everything after it:
        PRONOTE has three ways in, EcoleDirecte one, and a parent looking for
        "username and password" among four options could not tell whose they
        were. Asking the platform first keeps each later list about one
        product.
        """
        return self.async_show_menu(
            step_id=STEP_USER,
            menu_options=[STEP_PRONOTE, STEP_ECOLEDIRECTE],
            # See BRAND_LOGO_URL for why this is a URL and for the version gate
            # that keeps it: the mark reaches this screen as a markdown image
            # because hassfest refuses a URL written into the translation
            # string itself, and names a placeholder as the way to pass one.
            description_placeholders={"logo": BRAND_LOGO_URL},
        )

    async def async_step_pronote(
        self,
        user_input: dict[str, Any] | None = None,  # noqa: ARG002 -- a menu, not a form
    ) -> ConfigFlowResult:
        """Ask which of PRONOTE's three login methods to use."""
        return self.async_show_menu(
            step_id=STEP_PRONOTE,
            menu_options=[STEP_QR_CODE, STEP_CREDENTIALS, STEP_ENT],
        )

    # -- QR enrolment ------------------------------------------------------

    async def async_step_qr_code(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Enrol from the mobile app's QR code.

        Costs **two** complete logins by construction: ``qrcode_login`` builds
        a client -- whose constructor logs in -- posts ``PageInfosPerso``, then
        the integration logs in again with the exported token. That deliberate
        doubling is exempted from the three-attempt guard rail rather than
        eating two thirds of it (annexe B §3.3).
        """
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                payload = _parse_qr_payload(user_input[CONF_QR_PAYLOAD])
            except (TypeError, ValueError) as error:
                _log_rejected_payload(error, user_input[CONF_QR_PAYLOAD])
                errors[CONF_QR_PAYLOAD] = "invalid_qr_payload"
            else:
                self._data = {
                    CONF_LOGIN_MODE: LoginMode.QR_CODE.value,
                    CONF_PRONOTE_URL: public_url(payload["url"]),
                    CONF_QR_PAYLOAD: payload,
                    CONF_QR_PIN: user_input[CONF_QR_PIN],
                    # Must not change between logins, so it is generated once
                    # here and persisted with the entry -- and reused when an
                    # existing entry is reconfigured, for the reason
                    # `async_step_reauth_qr` gives.
                    CONF_UUID: self._reconfigured_uuid() or uuid_module.uuid4().hex,
                    CONF_DEVICE_NAME: user_input.get(CONF_DEVICE_NAME),
                    CONF_ACCOUNT_PIN: user_input.get(CONF_ACCOUNT_PIN),
                }
                return await self._async_try_login(errors)

        return self.async_show_form(
            step_id=STEP_QR_CODE, data_schema=QR_SCHEMA, errors=errors
        )

    # -- credentials -------------------------------------------------------

    async def async_step_credentials(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Log in with a username and a password."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._data = {
                CONF_LOGIN_MODE: LoginMode.CREDENTIALS.value,
                # Trimmed here, once, and only the trimmed form is ever stored.
                # What parents paste is regularly a deep link from a school
                # mailing or an ENT bounce carrying a ticket, and the query
                # string went on to appear in a repair issue's placeholders and
                # in the diagnostics download (§8.2).
                CONF_PRONOTE_URL: public_url(user_input[CONF_PRONOTE_URL]),
                "username": user_input["username"],
                "password": user_input["password"],
                CONF_ACCOUNT_PIN: user_input.get(CONF_ACCOUNT_PIN),
            }
            return await self._async_try_login(errors)

        return self.async_show_form(
            step_id=STEP_CREDENTIALS,
            data_schema=self._suggest(CREDENTIALS_SCHEMA),
            errors=errors,
        )

    # -- ENT ---------------------------------------------------------------

    async def async_step_ent(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Log in through a federated ENT provider."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._data = {
                CONF_LOGIN_MODE: LoginMode.ENT.value,
                CONF_PRONOTE_URL: public_url(user_input[CONF_PRONOTE_URL]),
                "username": user_input["username"],
                "password": user_input["password"],
                CONF_ENT: user_input[CONF_ENT],
            }
            return await self._async_try_login(errors)

        return self.async_show_form(
            step_id=STEP_ENT, data_schema=self._suggest(_ent_schema()), errors=errors
        )

    # -- EcoleDirecte ------------------------------------------------------

    async def async_step_ecoledirecte(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Log in to EcoleDirecte with an identifier and password."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._data = {
                CONF_SOURCE: Source.ECOLEDIRECTE.value,
                "username": user_input["username"],
                "password": user_input["password"],
                "qcm_json": self._ecoledirecte_qcm,
                "title": "EcoleDirecte",
            }
            return await self._async_try_ecoledirecte(errors)
        return self.async_show_form(
            step_id=STEP_ECOLEDIRECTE,
            data_schema=self._suggest(ECOLEDIRECTE_SCHEMA),
            errors=errors,
        )

    async def async_step_ecoledirecte_qcm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect remembered QCM answers without creating a half-born entry."""
        errors: dict[str, str] = {}
        if user_input is not None:
            choice = str(user_input["choice"])
            if choice not in self._ecoledirecte_propositions:
                errors["choice"] = "invalid_qcm"
            else:
                answers = {self._ecoledirecte_question: choice}
                self._ecoledirecte_qcm = answers
                self._data["qcm_json"] = answers
                return await self._async_try_ecoledirecte(errors)
        return self.async_show_form(
            step_id=STEP_ECOLEDIRECTE_QCM,
            data_schema=_ecoledirecte_qcm_schema(self._ecoledirecte_propositions),
            errors=errors,
            description_placeholders={
                "question": self._ecoledirecte_question,
                "propositions": ", ".join(self._ecoledirecte_propositions),
            },
        )

    async def _async_try_ecoledirecte(self, errors: dict[str, str]) -> ConfigFlowResult:
        if self.source == SOURCE_RECONFIGURE:
            # Checked *before* the login, where Home Assistant's own pattern
            # checks after it: an EcoleDirecte entry's identity is the
            # identifier typed in this very form, so a different one is known
            # to be refused before anything is sent, and spending a login on
            # learning it would draw on a budget for nothing.
            await self.async_set_unique_id(
                f"{Source.ECOLEDIRECTE.value}:{self._data['username']}"
            )
            self._abort_if_unique_id_mismatch(reason="reconfigure_wrong_account")
        try:
            outcome = await _probe_ecoledirecte(
                self.hass,
                str(self._data["username"]),
                str(self._data["password"]),
                self._ecoledirecte_qcm,
            )
        except ConnectorChallengeRequired as challenge:
            self._ecoledirecte_question = challenge.question or ""
            self._ecoledirecte_propositions = challenge.propositions
            return await self.async_step_ecoledirecte_qcm()
        except LoginRefusedByLimiter:
            errors["base"] = "rate_limited"
        except ConnectorCredentialsError:
            errors["base"] = "ed_invalid_auth"
        except ConnectorTransportError:
            errors["base"] = "cannot_connect"
        except Exception:
            _LOGGER.exception("unexpected failure while validating EcoleDirecte")
            errors["base"] = "unknown"
        else:
            students = list(outcome["students"])
            if self._reauth_entry is not None:
                return self._async_finish_ed_reauth(self._reauth_entry)
            if self.source == SOURCE_RECONFIGURE:
                entry = self._get_reconfigure_entry()
                # The proven credentials replace the ones whose failures put
                # the entry on hold; left in place, that hold would refuse the
                # very reload that applies the correction.
                _reset_ed_entry_penalties(self.hass, entry.entry_id)
                return self._async_finish_ed_reauth(
                    entry, reason="reconfigure_successful"
                )
            await self.async_set_unique_id(
                f"{Source.ECOLEDIRECTE.value}:{self._data['username']}"
            )
            self._abort_if_unique_id_configured()
            self._children = students
            self._data[CONF_CHILD_KEYS] = pair([], students)[1]
            if len(students) > 1:
                return await self.async_step_children()
            return self._async_create()
        return self.async_show_form(
            step_id=STEP_ECOLEDIRECTE,
            data_schema=self._suggest(ECOLEDIRECTE_SCHEMA),
            errors=errors,
        )

    # -- child selection ---------------------------------------------------

    async def async_step_children(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Let a parent account choose which children to follow.

        Offered rather than assumed: every followed child multiplies the daily
        request count, and a parent with four children who only wants one
        should not pay for four (annexe B §5.3).
        """
        if user_input is not None:
            selected = set(user_input[CONF_CHILDREN])
            if self._data.get(CONF_SOURCE) == Source.ECOLEDIRECTE.value:
                chosen = [
                    (student_id, name)
                    for student_id, name in self._children
                    if student_id in selected
                ]
                self._data[CONF_CHILD_KEYS] = pair([], chosen)[1]
            else:
                self._data[CONF_CHILDREN] = user_input[CONF_CHILDREN]
            return self._async_create()

        return self.async_show_form(
            step_id=STEP_CHILDREN,
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_CHILDREN,
                        default=[child_id for child_id, _ in self._children],
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                SelectOptionDict(value=child_id, label=name)
                                for child_id, name in self._children
                            ],
                            multiple=True,
                            mode=SelectSelectorMode.LIST,
                        )
                    )
                }
            ),
        )

    # -- re-authentication -------------------------------------------------

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Start a re-authentication, keeping the existing entry."""
        self._reauth_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        self._data = dict(entry_data)
        if source_from_entry_data(self._data) is Source.ECOLEDIRECTE:
            _reset_ed_entry_penalties(
                self.hass,
                None if self._reauth_entry is None else self._reauth_entry.entry_id,
            )
            return await self.async_step_ecoledirecte()
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for a corrected password and/or the 2FA PIN.

        A deliberate human gesture, so the limiter's failure counters are
        cleared before the attempt: a person retyping a password with the
        correction in hand is not an automatic retry and earns a clean slate
        (annexe B §3.2).

        A QR-enrolled account never reaches the form below. It has no password
        to correct -- see :meth:`async_step_reauth_qr`.
        """
        if self._data.get(CONF_LOGIN_MODE) == LoginMode.QR_CODE.value:
            return await self.async_step_reauth_qr()

        errors: dict[str, str] = {}
        entry = self._reauth_entry

        if user_input is not None and entry is not None:
            # Before the attempt, as the docstring says -- and it was not
            # happening at all: `reset_after_reauth` had no caller outside the
            # limiter's own tests, so the MFA hold, whose only exit is a human
            # supplying the PIN, could not be exited by a human supplying the
            # PIN.
            clear_login_penalties(self.hass, entry.entry_id)
            data = dict(entry.data)
            if user_input.get("password"):
                data["password"] = user_input["password"]
            if user_input.get(CONF_ACCOUNT_PIN):
                data[CONF_ACCOUNT_PIN] = user_input[CONF_ACCOUNT_PIN]
            self._data = data

            outcome = await self._async_probe(self._data, errors)
            if outcome is not None:
                return self._async_finish_reauth(entry, outcome)

        return self.async_show_form(
            step_id=STEP_REAUTH_CONFIRM,
            data_schema=REAUTH_SCHEMA,
            errors=errors,
            description_placeholders={
                "url": public_url(self._data.get(CONF_PRONOTE_URL))
            },
        )

    async def async_step_reauth_qr(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Re-enrol a QR account from a freshly generated QR code.

        A token account cannot be repaired with a password, and it has none:
        in token mode PRONOTE rotates ``jetonConnexionAppliMobile`` at every
        login, so a token that went stale -- an interrupted login, a session
        opened twice, a device revoked in the app -- is dead for good, and the
        only thing that mints a new one is a new QR code. Showing such an
        account the password form was offering a repair that could not work,
        and the account's own two-factor PIN is asked for here too, so nothing
        the old form could fix is lost by not showing it.

        The device UUID is deliberately **reused** rather than regenerated, and
        that is a requirement rather than a preference: upstream documents the
        parameter as "Unique ID for your application. Must not change between
        logins" (``pronotepy.ClientBase.qrcode_login``). A fresh one would also
        fill the account's enrolled-device list with copies of this Home
        Assistant, but the contract is the reason.
        """
        errors: dict[str, str] = {}
        entry = self._reauth_entry

        if user_input is not None and entry is not None:
            try:
                payload = _parse_qr_payload(user_input[CONF_QR_PAYLOAD])
            except (TypeError, ValueError) as error:
                _log_rejected_payload(error, user_input[CONF_QR_PAYLOAD])
                errors[CONF_QR_PAYLOAD] = "invalid_qr_payload"
            else:
                # Before the attempt, for the reason `async_step_reauth_confirm`
                # gives: a human pasting a QR code is not an automatic retry.
                clear_login_penalties(self.hass, entry.entry_id)
                self._data = {
                    **dict(entry.data),
                    CONF_LOGIN_MODE: LoginMode.QR_CODE.value,
                    CONF_PRONOTE_URL: public_url(payload["url"]),
                    CONF_QR_PAYLOAD: payload,
                    CONF_QR_PIN: user_input[CONF_QR_PIN],
                    CONF_UUID: entry.data.get(CONF_UUID) or uuid_module.uuid4().hex,
                    CONF_DEVICE_NAME: user_input.get(CONF_DEVICE_NAME),
                    CONF_ACCOUNT_PIN: user_input.get(CONF_ACCOUNT_PIN),
                }

                outcome = await self._async_probe(self._data, errors)
                if outcome is not None:
                    # A QR code is the one reconnection input that can name a
                    # *different* account: a parent with two children in two
                    # establishments has two apps to generate one from. Pasting
                    # the wrong one would silently repoint this entry -- keeping
                    # its devices, its entity ids and its history -- at somebody
                    # else's child, so the identity is checked rather than
                    # assumed.
                    if not _same_account(entry.unique_id, outcome["account_id"]):
                        return self.async_abort(reason="wrong_account")
                    return self._async_finish_reauth(entry, outcome)

        return self.async_show_form(
            step_id=STEP_REAUTH_QR,
            data_schema=QR_SCHEMA,
            errors=errors,
            description_placeholders={
                "url": public_url(self._data.get(CONF_PRONOTE_URL))
            },
        )

    def _async_finish_reauth(
        self, entry: ConfigEntry, outcome: dict[str, Any]
    ) -> ConfigFlowResult:
        """Persist a successful reconnection and reload the entry.

        ``children`` comes from the entry and never from the probe, for the
        reason ``_async_try_login`` gives at length: the probe returns
        ``(id, name)`` label pairs under the very key that holds the user's
        *selection*, so merging them in replaced "follow this one child" with
        "follow every child the account can see" -- silently, on every single
        reconnection, along with the second child's whole request budget.

        The PIN, and a QR payload if one was used, are dropped on the way out:
        they exist in ``self._data`` only long enough to log in once.
        """
        merged = dict(self._data)
        merged.update(
            {key: value for key, value in outcome.items() if key != CONF_CHILDREN}
        )
        persisted = {
            key: value
            for key, value in merged.items()
            if key not in _NEVER_PERSISTED and value is not None
        }
        # `reload_even_if_entry_is_unchanged=False` because this entry *has* an
        # update listener (`__init__.py`, `add_update_listener`), and HA 2026.9
        # reports leaving the default on such an entry as a mistake:
        # `report_usage("has an update listener and should use it for scheduling
        # a reload", breaks_in_ha_version="2026.12.0")`. The listener does the
        # reload, so asking for one here as well is either redundant or a second
        # reload.
        return self.async_update_reload_and_abort(
            entry,
            data=persisted,
            reason="reauth_successful",
            reload_even_if_entry_is_unchanged=False,
        )

    def _async_finish_ed_reauth(
        self, entry: ConfigEntry, *, reason: str = "reauth_successful"
    ) -> ConfigFlowResult:
        """Persist ED credentials without importing a Pronote probe payload.

        Shared by re-authentication and reconfiguration, which for EcoleDirecte
        replace the same three keys. The minted child keys come from the entry
        and the options are not passed, so both survive either path.
        """
        merged = {
            **dict(entry.data),
            "username": self._data["username"],
            "password": self._data["password"],
            "qcm_json": self._data.get("qcm_json") or {},
        }
        persisted = {key: merged[key] for key in _ED_PERSISTED if key in merged}
        return self.async_update_reload_and_abort(
            entry,
            data=persisted,
            reason=reason,
            reload_even_if_entry_is_unchanged=False,
        )

    # -- reconfiguration ---------------------------------------------------

    async def async_step_reconfigure(
        self,
        user_input: dict[str, Any] | None = None,  # noqa: ARG002 -- a menu, not a form
    ) -> ConfigFlowResult:
        """Change how an existing entry connects, without removing it.

        Removing and re-adding an entry was the only way to correct an address,
        switch to the ENT or move to a QR code, and it cost everything attached
        to the entry: its devices, its entity ids, the automations and the
        history written against them, the options, the children deliberately
        left unticked.

        No form of its own. PRONOTE gets the add flow's three-way menu and then
        the add flow's own steps -- the same schemas, the same probe, the same
        limiter, the same error messages -- which branch at the very end, in
        :meth:`_async_try_login`, to :meth:`_async_finish_reconfigure`.
        EcoleDirecte has one way in and goes straight to its form. A second copy
        of each form would be a second place for the rules about what is
        trimmed, what is charged and what is never stored to drift apart.
        """
        entry = self._get_reconfigure_entry()
        if source_from_entry_data(dict(entry.data)) is Source.ECOLEDIRECTE:
            # The remembered QCM answers go with the attempt, as they do at
            # every collection: the identifier cannot change (see
            # `_async_try_ecoledirecte`), so they still belong to this account,
            # and dropping them would ask a question it has already answered.
            self._ecoledirecte_qcm = dict(entry.data.get("qcm_json") or {})
            return await self.async_step_ecoledirecte()
        return self.async_show_menu(
            step_id=STEP_RECONFIGURE,
            menu_options=[STEP_QR_CODE, STEP_CREDENTIALS, STEP_ENT],
            description_placeholders={
                "url": public_url(entry.data.get(CONF_PRONOTE_URL))
            },
        )

    def _reconfigured_uuid(self) -> str | None:
        """The device UUID of the entry being reconfigured, if it has one."""
        if self.source != SOURCE_RECONFIGURE:
            return None
        value = self._get_reconfigure_entry().data.get(CONF_UUID)
        return str(value) if value else None

    def _suggest(self, schema: vol.Schema) -> vol.Schema:
        """Pre-fill a reused add-flow form with what the entry already holds.

        Only while reconfiguring, and only the address, the username and the
        portal -- never the password. Suggested values travel to the browser,
        and on a QR-enrolled entry the stored "password" is the live access
        token. So a reconfiguration asks for the password again, which is also
        what proves the person changing the connection knows it.

        The username is not suggested from a QR-enrolled entry either: there it
        is the enrolment's own login, meaningless on a password form.

        Suggested rather than defaulted: a default is submitted as displayed,
        and a field emptied by the user would come back filled.
        """
        if self.source != SOURCE_RECONFIGURE:
            return schema
        stored = self._get_reconfigure_entry().data
        suggested: dict[str, Any] = {}
        if stored.get(CONF_PRONOTE_URL):
            suggested[CONF_PRONOTE_URL] = public_url(stored[CONF_PRONOTE_URL])
        if stored.get(CONF_LOGIN_MODE) != LoginMode.QR_CODE.value and stored.get(
            "username"
        ):
            suggested["username"] = stored["username"]
        if stored.get(CONF_ENT):
            suggested[CONF_ENT] = stored[CONF_ENT]
        return self.add_suggested_values_to_schema(schema, suggested)

    def _async_finish_reconfigure(self, outcome: dict[str, Any]) -> ConfigFlowResult:
        """Check the login landed on this entry's account, then apply it.

        The identity check is the one thing reconfiguration adds to the add
        flow, and the reason it is not ``_abort_if_unique_id_mismatch``: that
        compares the whole ``unique_id``, which holds the full address and the
        rotating signature of the PRONOTE resource. It would refuse a corrected
        path and, on a re-enrolment, the right account with a fresh signature --
        see :func:`_same_account_on_host`. The stored ``unique_id`` is
        left as it is, for the reason :func:`_account_identity` gives.

        What is written: the entry's data minus its connection keys, then the
        new connection, then what the login exported (the fresh token, the
        client identifier). ``children`` is the entry's, for the reason
        :meth:`_async_finish_reauth` gives; the options are not passed, so the
        children selection's budget, the write switch and every tier setting
        survive untouched. ``_NEVER_PERSISTED`` is applied last.
        """
        entry = self._get_reconfigure_entry()
        if not _same_account_on_host(entry.unique_id, outcome["account_id"]):
            return self.async_abort(reason="reconfigure_wrong_account")

        merged = {
            key: value
            for key, value in entry.data.items()
            if key not in _CONNECTION_KEYS
        }
        merged.update(self._data)
        merged.update(
            {key: value for key, value in outcome.items() if key != CONF_CHILDREN}
        )
        # Trimmed again after the merge: the probe exports the address the
        # library logged in with, and only the trimmed form may be stored.
        merged[CONF_PRONOTE_URL] = public_url(merged.get(CONF_PRONOTE_URL))
        persisted = {
            key: value
            for key, value in merged.items()
            if key not in _NEVER_PERSISTED and value is not None
        }
        # The holds a failing entry carries were earned by the parameters this
        # login has just replaced. Left in place, they would refuse the reload
        # that applies the correction -- the MFA hold has no other exit at all.
        clear_login_penalties(self.hass, entry.entry_id)
        # `reload_even_if_entry_is_unchanged=False` for the reason
        # `_async_finish_reauth` gives.
        return self.async_update_reload_and_abort(
            entry,
            data=persisted,
            reason="reconfigure_successful",
            reload_even_if_entry_is_unchanged=False,
        )

    # -- shared login plumbing ---------------------------------------------

    async def _async_try_login(self, errors: dict[str, str]) -> ConfigFlowResult:
        """Attempt the login, then branch on the account's shape."""
        outcome = await self._async_probe(self._data, errors)
        if outcome is None:
            return self._async_reshow(errors)
        if self.source == SOURCE_RECONFIGURE:
            # Before `async_set_unique_id` below, which belongs to the add
            # flow: it would abort `already_configured` on the very entry being
            # reconfigured.
            return self._async_finish_reconfigure(outcome)

        # Everything except `children`, which is deliberately *not* merged.
        #
        # The probe returns `children` as `(id, name)` label pairs, and
        # `CONF_CHILDREN` -- the very same string -- is where the user's
        # *selection* is stored a few lines below. Merging the pairs in and then
        # dropping the key on the way out (because the labels have no business
        # being persisted) threw the selection away with them: a parent who
        # deliberately followed one of two children got both anyway, silently,
        # along with the second child's whole request budget. The labels live in
        # `self._children`, which is the only place that needs them.
        self._data.update(
            {key: value for key, value in outcome.items() if key != CONF_CHILDREN}
        )
        await self.async_set_unique_id(str(outcome["account_id"]))
        self._abort_if_unique_id_configured()

        self._children = list(outcome["children"])
        if len(self._children) > 1:
            return await self.async_step_children()

        self._data[CONF_CHILDREN] = [child_id for child_id, _ in self._children]
        return self._async_create()

    def _async_reshow(self, errors: dict[str, str]) -> ConfigFlowResult:
        """Re-display the form the user came from, with its errors."""
        mode = self._data.get(CONF_LOGIN_MODE)
        if mode == LoginMode.QR_CODE.value:
            return self.async_show_form(
                step_id=STEP_QR_CODE, data_schema=QR_SCHEMA, errors=errors
            )
        if mode == LoginMode.ENT.value:
            return self.async_show_form(
                step_id=STEP_ENT,
                data_schema=self._suggest(_ent_schema()),
                errors=errors,
            )
        return self.async_show_form(
            step_id=STEP_CREDENTIALS,
            data_schema=self._suggest(CREDENTIALS_SCHEMA),
            errors=errors,
        )

    async def _async_probe(
        self, data: dict[str, Any], errors: dict[str, str]
    ) -> dict[str, Any] | None:
        """Log in once in a worker thread and learn the account's shape.

        Returns the credentials to persist plus the children, or ``None`` after
        filling ``errors``. Runs in the executor because constructing a
        ``pronotepy`` client *is* a network login (§3.2), and it runs **through
        the limiter**, which it did not.

        That omission mattered more than it looks. This is the one login path a
        human can repeat at will: the re-authentication form re-displays itself
        on every failure, so a parent convinced the password is right could
        submit it twenty times in a minute. None of those attempts reached
        `may_login`, none was counted by `note_login`, and the three-attempt
        guard rail -- the defence against the one sanction PRONOTE applies to an
        address rather than to an account -- never saw them (annexe B §3.1).
        """
        guard = login_guard(self.hass)
        # QR enrolment performs two complete logins by construction, so it is
        # declared as two. Under-declaring here would spend the budget without
        # recording it, which is the defect this whole file just stopped having.
        # The payload is part of the test, not just the mode: a QR-enrolled
        # entry keeps `qr_code` for ever, but once the payload is gone it logs
        # in through pronotepy's token mode, which is a single login. Charging
        # two for it would spend a budget nothing used.
        cost = (
            REQUESTS_PER_LOGIN * 2
            if data.get(CONF_LOGIN_MODE) == LoginMode.QR_CODE.value
            and data.get(CONF_QR_PAYLOAD)
            else REQUESTS_PER_LOGIN
        )

        try:
            outcome: dict[str, Any] = await guard.login(
                lambda: self.hass.async_add_executor_job(_probe, data),
                cost=cost,
            )
        except LoginRefusedByLimiter:
            # Nothing was sent, so this is not an attempt and must not be
            # counted as one.
            errors["base"] = "rate_limited"
        except ProbeInvalidCredentials as error:
            _log_refusal(error)
            guard.note_login(LoginOutcome.BAD_CREDENTIALS, requests_used=cost)
            errors["base"] = "invalid_auth"
        except ProbeMfaRequired as error:
            _log_refusal(error)
            guard.note_login(LoginOutcome.MFA_REQUIRED, requests_used=cost)
            errors["base"] = "mfa_required"
        except ProbeEntRequired as error:
            # A bootstrap failure to the limiter, which is what it is: the
            # password was never submitted, so it must not reach the credentials
            # guard. Only the message differs from the arm below -- it names
            # the one field that fixes it, the login mode.
            _log_refusal(error)
            guard.note_login(LoginOutcome.BOOTSTRAP, requests_used=cost)
            errors["base"] = "ent_required"
        except ProbeBootstrapFailed as error:
            _log_refusal(error)
            # Says the bootstrap failed and stops there. pronotepy infers an IP
            # suspension from `if "IP" in html` -- two capitals anywhere in the
            # page -- and telling somebody their home connection is banned
            # because a school wrote "Espace IP" in a footer would be worse
            # than saying nothing (§6.3).
            guard.note_login(LoginOutcome.BOOTSTRAP, requests_used=cost)
            errors["base"] = "bootstrap_failed"
        except ProbeQrInvalid as error:
            # A four-digit code that cannot decrypt the payload. Purely local:
            # `qrcode_login` does that AES pass before it opens a socket, so
            # nothing reached the school.
            #
            # It is charged all the same, and saying otherwise would be wrong.
            # `RateLimiter.login` commits at admission -- inside the lock,
            # before the call, deliberately and with no refund path -- so by the
            # time this arm runs, `_commit_login` has already recorded the ten
            # requests and incremented `logins_today`. What this arm can decide
            # is where the failure is *attributed*, and there the answer is
            # clear: it must not eat one of the three slots on the IP guard,
            # because that rail exists for credentials PRONOTE actually refused
            # and a typo never left the house.
            #
            # `note_login` is still called, for a reason that is not cosmetic.
            # It is what consumes `_login_charged`; skipping it leaves the flag
            # armed, and the next caller to report an outcome without having
            # gone through `login()` reads a charge that was already settled and
            # bills itself nothing. `counts_against_guard=False` is the same
            # exemption QR enrolment's deliberate double login uses
            # (annexe B §3.3).
            #
            # What remains unfixed, and is not hidden: twenty-four wrong
            # four-digit codes exhaust `max_logins_per_day` until midnight. The
            # cure is a refund path in the limiter, which is a decision about
            # `ratelimit.py`, not a line to slip into an except arm.
            _log_refusal(error)
            guard.note_login(
                LoginOutcome.BAD_CREDENTIALS,
                requests_used=cost,
                counts_against_guard=False,
            )
            errors["base"] = "invalid_qr"
        except ProbeQrRefused as error:
            # The payload decrypted and the server then refused the challenge.
            # Charged to the credentials guard, because from PRONOTE's point of
            # view it is a refused authentication like any other.
            _log_refusal(error)
            guard.note_login(LoginOutcome.BAD_CREDENTIALS, requests_used=cost)
            errors["base"] = "qr_refused"
        except ProbeEntUnknown as error:
            # Nothing was attempted: the resolver refuses before `build_client`
            # is constructed, so no handshake happened. The charge stands for
            # the reason the `ProbeQrInvalid` arm gives at length -- admission
            # already committed it -- but the IP guard must not see it, because
            # the credentials were never submitted to anyone.
            #
            # The error goes on the provider field rather than on `base`, since
            # that is the box to correct and the other three fields are fine.
            _log_refusal(error)
            guard.note_login(
                LoginOutcome.BAD_CREDENTIALS,
                requests_used=cost,
                counts_against_guard=False,
            )
            errors[CONF_ENT] = "unknown_ent"
        except (TimeoutError, OSError) as error:
            _log_refusal(error)
            guard.note_login(LoginOutcome.TRANSPORT, requests_used=cost)
            errors["base"] = "cannot_connect"
        except Exception:
            _LOGGER.exception("unexpected failure while validating the account")
            guard.note_login(LoginOutcome.TRANSPORT, requests_used=cost)
            errors["base"] = "unknown"
        else:
            guard.note_login(LoginOutcome.SUCCESS, requests_used=cost)
            return outcome
        return None

    def _async_create(self) -> ConfigFlowResult:
        """Create the entry, dropping the single-use and never-stored fields."""
        # `children` is *not* in `_NEVER_PERSISTED`, and must not be: it holds
        # the user's selection, which `PronoteAccount` reads to decide whose
        # data to collect. It used to be excluded here, which is how the
        # selection came to be discarded on every single install.
        data = {
            key: value
            for key, value in self._data.items()
            if key not in _NEVER_PERSISTED and value is not None
        }
        if data.get(CONF_SOURCE) == Source.ECOLEDIRECTE.value:
            data = {key: data[key] for key in _ED_PERSISTED if key in data}
            return self.async_create_entry(title=str(self._data["title"]), data=data)
        # Seeded rather than left to a fallback: the fallback has to keep
        # answering "off" for entries created before writes were on by
        # default, so a new entry must say "on" in its own options (§8.3).
        return self.async_create_entry(
            title=str(self._data["title"]),
            data=data,
            options={OPT_WRITE_OPERATIONS_ENABLED: DEFAULT_WRITE_OPERATIONS_ENABLED},
        )


class PronoteOptionsFlow(OptionsFlow):
    """Three sections, and a live budget estimate.

    The estimate is not decoration. Annexe B §7 requires the page to show what
    the entered values will cost, computed by the *same* function that produced
    the numbers in the annexe -- a setting whose consequence you cannot see gets
    set at random, and the consequence here is measured in requests against
    somebody's school account.
    """

    async def async_step_init(
        self,
        user_input: dict[str, Any] | None = None,  # noqa: ARG002 -- a menu, not a form
    ) -> ConfigFlowResult:
        """Offer the three sections."""
        return self.async_show_menu(
            step_id="init",
            menu_options=["general", "tiers", "rate_limit"],
            description_placeholders=self._estimate_placeholders(
                self.config_entry.options
            ),
        )

    async def async_step_general(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Cadence, horizons, timezone and the write switch."""
        options = self.config_entry.options
        errors: dict[str, str] = {}
        # What to show in the timezone box: the stored pin, or -- after a
        # refusal -- what the user just typed, so a single wrong character
        # does not cost them the whole string.
        shown = options.get(OPT_ESTABLISHMENT_TIMEZONE)

        if user_input is not None:
            typed = str(user_input.get(OPT_ESTABLISHMENT_TIMEZONE) or "").strip()
            if typed and await dt_util.async_get_time_zone(typed) is None:
                # Refused here rather than at set-up. The string reaches
                # `ZoneInfo` in `PronoteGateway.__init__`, which raises
                # `ZoneInfoNotFoundError` on the set-up path -- so "Paris"
                # instead of "Europe/Paris" used to take every entity of the
                # account down and explain itself as `unavailable`.
                errors[OPT_ESTABLISHMENT_TIMEZONE] = "invalid_timezone"
                shown = typed
            else:
                # Trimmed, not refused: a pasted zone carries whitespace, and
                # that is somebody saying the right thing with an invisible
                # character attached. An empty string reaches `_save` as the
                # instruction to un-pin.
                return self._save(
                    {**user_input, OPT_ESTABLISHMENT_TIMEZONE: typed},
                    clearable=(OPT_ESTABLISHMENT_TIMEZONE,),
                )

        schema = vol.Schema(
            {
                vol.Required(
                    OPT_MASTER_TICK,
                    default=options.get(OPT_MASTER_TICK, DEFAULT_MASTER_TICK),
                ): _number(OPT_MASTER_TICK, "min"),
                vol.Required(
                    OPT_HOMEWORK_HORIZON,
                    default=options.get(OPT_HOMEWORK_HORIZON, DEFAULT_HOMEWORK_HORIZON),
                ): _number(OPT_HOMEWORK_HORIZON, "d"),
                vol.Required(
                    OPT_WAKE_MARGIN,
                    default=options.get(OPT_WAKE_MARGIN, DEFAULT_WAKE_MARGIN),
                ): _number(OPT_WAKE_MARGIN, "min"),
                vol.Required(
                    OPT_STALE_AFTER,
                    default=options.get(OPT_STALE_AFTER, DEFAULT_STALE_AFTER),
                ): _number(OPT_STALE_AFTER, "x"),
                # The only field on these three pages whose default is not
                # a constant but a live reading of another system's mutable
                # setting -- Home Assistant's own timezone (§4.2). So it is
                # the only one where storing the displayed value is not a
                # no-op, and it is deliberately *not* prefilled with that
                # reading: Home Assistant submits a form back as displayed, so
                # a `default` here turned merely visiting this page into a
                # decision, pinning a snapshot of `hass.config.time_zone` that
                # the account then never consulted again. An empty box is not
                # an omission, it is how the value "follow Home Assistant" is
                # shown -- and the only way back once a pin exists.
                _optional_text(OPT_ESTABLISHMENT_TIMEZONE, shown): _TEXT,
            }
        )
        source = source_from_entry_data(dict(self.config_entry.data))
        if source is not Source.ECOLEDIRECTE:
            # Both strategies are offered because the server's inactivity
            # timeout is not published and cannot be assumed. `lazy` starts
            # pessimistic and degrades to `per_batch` on measured evidence,
            # so the default is never worse than the alternative (§6.5).
            schema = schema.extend(
                {
                    vol.Required(
                        OPT_SESSION_STRATEGY,
                        default=options.get(
                            OPT_SESSION_STRATEGY, DEFAULT_SESSION_STRATEGY.value
                        ),
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                SelectOptionDict(
                                    value=strategy.value, label=strategy.value
                                )
                                for strategy in SessionStrategy
                            ],
                            mode=SelectSelectorMode.DROPDOWN,
                            translation_key="session_strategy",
                        )
                    ),
                    vol.Required(
                        OPT_WRITE_OPERATIONS_ENABLED,
                        # The legacy default, like `write_enabled`: the form is
                        # submitted as displayed, so prefilling an old entry
                        # with "on" would turn writes on for whoever saved
                        # this page to change something else.
                        default=options.get(
                            OPT_WRITE_OPERATIONS_ENABLED,
                            LEGACY_WRITE_OPERATIONS_ENABLED,
                        ),
                    ): BooleanSelector(),
                }
            )
        return self.async_show_form(
            step_id="general",
            data_schema=schema,
            errors=errors,
            description_placeholders=self._estimate_placeholders(options),
        )

    async def async_step_tiers(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Per-tier interval and on/off switch."""
        if user_input is not None:
            return self._save(user_input)

        options = self.config_entry.options
        intervals = tier_intervals(options)
        enabled = tier_enabled(options)
        capable = _capable_tiers(self.config_entry)

        fields: dict[Any, Any] = {}
        for tier, default in DEFAULT_TIER_INTERVALS.items():
            if tier not in capable:
                continue
            fields[
                vol.Required(
                    OPT_TIER_INTERVAL.format(tier=tier.value),
                    default=intervals.get(tier, default),
                )
            ] = NumberSelector(
                NumberSelectorConfig(
                    # From the same table `options.tier_intervals` clamps
                    # against, so the field and the reader cannot drift apart.
                    min=TIER_INTERVAL_RANGE[0],
                    max=TIER_INTERVAL_RANGE[1],
                    step=1,
                    mode=NumberSelectorMode.BOX,
                    unit_of_measurement="min",
                )
            )
            fields[
                vol.Required(
                    OPT_TIER_ENABLED.format(tier=tier.value),
                    default=enabled.get(tier, True),
                )
            ] = BooleanSelector()

        return self.async_show_form(
            step_id="tiers",
            data_schema=vol.Schema(fields),
            description_placeholders=self._estimate_placeholders(options),
        )

    async def async_step_rate_limit(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Every limiter tunable of annexe B §7."""
        if user_input is not None:
            return self._save(user_input)

        options = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Required(
                    OPT_MIN_REQUEST_INTERVAL,
                    default=_default(options, OPT_MIN_REQUEST_INTERVAL),
                ): _number(OPT_MIN_REQUEST_INTERVAL, "s", step=0.1),
                vol.Required(
                    OPT_MAX_REQUESTS_PER_HOUR,
                    default=_default(options, OPT_MAX_REQUESTS_PER_HOUR),
                ): _number(OPT_MAX_REQUESTS_PER_HOUR, "req/h"),
                vol.Required(
                    OPT_BURST_SIZE, default=_default(options, OPT_BURST_SIZE)
                ): _number(OPT_BURST_SIZE, "req"),
                vol.Required(
                    OPT_MAX_REQUESTS_PER_DAY,
                    default=_default(options, OPT_MAX_REQUESTS_PER_DAY),
                ): _number(OPT_MAX_REQUESTS_PER_DAY, "req/d"),
                vol.Required(
                    OPT_MAX_WAIT, default=_default(options, OPT_MAX_WAIT)
                ): _number(OPT_MAX_WAIT, "s"),
                vol.Required(
                    OPT_MAX_LOGINS_PER_DAY,
                    default=_default(options, OPT_MAX_LOGINS_PER_DAY),
                ): _number(OPT_MAX_LOGINS_PER_DAY, "login/d"),
                # The one setting that guards against address suspension. Its
                # range starts at 1 and stops at 10 on purpose: annexe B §3.1
                # is clear that the sanction targets failed logins, and a large
                # value here is the mistake that costs an account.
                vol.Required(
                    OPT_MAX_FAILED_LOGINS_PER_HOUR,
                    default=_default(options, OPT_MAX_FAILED_LOGINS_PER_HOUR),
                ): _number(OPT_MAX_FAILED_LOGINS_PER_HOUR, "attempt/h"),
                vol.Required(
                    OPT_CREDENTIALS_HOLD,
                    default=_default(options, OPT_CREDENTIALS_HOLD),
                ): _number(OPT_CREDENTIALS_HOLD, "s"),
                vol.Required(
                    OPT_BOOTSTRAP_HOLD,
                    default=_default(options, OPT_BOOTSTRAP_HOLD),
                ): _number(OPT_BOOTSTRAP_HOLD, "s"),
                vol.Required(
                    OPT_BACKOFF_BASE, default=_default(options, OPT_BACKOFF_BASE)
                ): _number(OPT_BACKOFF_BASE, "s"),
                vol.Required(
                    OPT_BACKOFF_MAX, default=_default(options, OPT_BACKOFF_MAX)
                ): _number(OPT_BACKOFF_MAX, "s"),
                vol.Required(
                    OPT_CONNECT_TIMEOUT,
                    default=_default(options, OPT_CONNECT_TIMEOUT),
                ): _number(OPT_CONNECT_TIMEOUT, "s"),
                vol.Required(
                    OPT_READ_TIMEOUT, default=_default(options, OPT_READ_TIMEOUT)
                ): _number(OPT_READ_TIMEOUT, "s"),
                vol.Required(
                    OPT_QUIET_HOURS_ENABLED,
                    default=options.get(OPT_QUIET_HOURS_ENABLED, True),
                ): BooleanSelector(),
                vol.Required(
                    OPT_QUIET_START,
                    default=options.get(OPT_QUIET_START, "22:00:00"),
                ): TimeSelector(),
                vol.Required(
                    OPT_QUIET_END,
                    default=options.get(OPT_QUIET_END, "06:00:00"),
                ): TimeSelector(),
            }
        )
        return self.async_show_form(
            step_id="rate_limit",
            data_schema=schema,
            description_placeholders=self._estimate_placeholders(options),
        )

    def _save(
        self, user_input: dict[str, Any], *, clearable: tuple[str, ...] = ()
    ) -> ConfigFlowResult:
        """Merge one section's answers into the stored options.

        Merged rather than replaced: each section shows a subset, and replacing
        would silently reset everything the user did not have on screen.

        `clearable` names the keys of *this* section for which an empty answer
        is itself an answer -- remove the stored value, so the option goes back
        to deriving itself. A merge alone can never express that, because a
        merge can only add: without this, an option pinned once could not be
        un-pinned from the interface at all.

        Passed per step rather than held as a module constant, and that is the
        load-bearing part. "Absent from the answers means cleared" is only true
        of the section on screen; applied to the whole merge it would wipe
        every limiter tunable each time somebody saved the "general" page,
        which is the exact failure this method was written to prevent.
        """
        merged = {**self.config_entry.options, **user_input}
        for key in clearable:
            if not merged.get(key):
                merged.pop(key, None)
        return self.async_create_entry(title="", data=merged)

    def _estimate_placeholders(self, options: Mapping[str, Any]) -> dict[str, str]:
        """The daily request estimate shown on every page.

        Uses the measured session lifetime when one exists, and the pessimistic
        assumption when it does not -- an estimate a user can be disappointed by
        is worse than no estimate (§6.5).
        """
        students = _entry_children(self.config_entry)
        if source_from_entry_data(dict(self.config_entry.data)) is Source.ECOLEDIRECTE:
            account = getattr(self.config_entry, "runtime_data", None)
            login_ids = getattr(getattr(account, "connector", None), "_login_ids", {})
            estimate = estimate_ecoledirecte_daily_requests(
                options,
                students=max(1, students),
                include_qcm=bool(self.config_entry.data.get("qcm_json")),
                multi_establishment=len(set(login_ids.values())) > 1,
            )
            measured = None
        else:
            students = len(options.get(CONF_CHILDREN) or ()) or students
            measured = _measured_lifetime(self.config_entry)
            estimate = estimate_daily_requests(
                options,
                students=max(1, students),
                session_lifetime_minutes=measured,
            )
        return {
            "estimate": str(estimate),
            "students": str(max(1, students)),
            "measured": (f"{measured:.0f}" if measured is not None else "-"),
        }


def _capable_tiers(entry: ConfigEntry) -> frozenset[Any]:
    """Tiers this entry can actually collect."""
    account = getattr(entry, "runtime_data", None)
    if account is not None:
        return frozenset(account.connector.capabilities.tiers)
    if source_from_entry_data(dict(entry.data)) is Source.ECOLEDIRECTE:
        return EcoledirecteConnector.CAPABILITIES.tiers
    return frozenset(DEFAULT_TIER_INTERVALS)


def _entry_children(entry: ConfigEntry) -> int:
    """How many children the entry follows."""
    stored = entry.data.get(CONF_CHILDREN)
    if stored:
        return len(stored)
    keys = entry.data.get(CONF_CHILD_KEYS) or ()
    return len(keys) or 1


def _measured_lifetime(entry: ConfigEntry) -> float | None:
    """The measured session lifetime, if the account is a Pronote extras owner."""
    account = getattr(entry, "runtime_data", None)
    extras = getattr(account, "extras", None)
    if extras is None:
        return None
    lifetime = extras.session.lifetime.observed_minutes
    return float(lifetime) if lifetime is not None else None


def _default(options: Mapping[str, Any], key: str) -> Any:
    """The stored value for an option, else its documented default."""
    from . import const  # noqa: PLC0415 -- one lookup, keeps the table in const

    return options.get(key, getattr(const, f"DEFAULT_{key.upper()}"))


def _optional_text(key: str, current: str | None) -> vol.Optional:
    """A text field that may be left empty, prefilled only if something is set.

    `vol.Optional` with neither a `default` nor a `suggested_value` is what
    puts an empty box on screen, and emptiness has to be reachable for a field
    whose absence is a value. The distinction is not cosmetic: Home Assistant
    sends a form back as displayed, so anything prefilled here -- default or
    suggestion -- is submitted and stored by a user who only came to change
    another field on the same page.

    A stored value is still prefilled, and through `suggested_value` rather
    than `default` on purpose. That the frontend submits a suggestion back is
    the contract of `FlowHandler.add_suggested_values_to_schema`, which core
    options flows use precisely to open a form on its current values and save
    them unchanged -- so a deliberate pin survives a visit. The difference from
    `default` is what happens when the box is emptied: voluptuous re-applies a
    `default` to a field the frontend then omits, which would make the pin
    impossible to remove.
    """
    if not current:
        return vol.Optional(key)
    return vol.Optional(key, description={"suggested_value": current})


def _number(key: str, unit: str, *, step: float = 1) -> NumberSelector:
    """A bounded number field, using the range table of ``const.OPTION_RANGES``.

    Bounded in the UI and not only in the code: annexe B §7 gives every tunable
    a range, and a field that accepts 100000 requests an hour is a field that
    will eventually be set to 100000.
    """
    low, high = OPTION_RANGES[key]
    return NumberSelector(
        NumberSelectorConfig(
            min=float(low),
            max=float(high),
            step=step,
            mode=NumberSelectorMode.BOX,
            unit_of_measurement=unit,
        )
    )


def _parse_qr_payload(raw: str) -> dict[str, Any]:
    """Parse the QR code's JSON and check the three keys ``pronotepy`` needs."""
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError("not JSON") from error
    if not isinstance(payload, dict):
        # A ValueError, deliberately, and not the TypeError this used to be.
        # Every failure of this function means the same thing to its one
        # caller -- "this payload is unusable" -- and that caller catches
        # ValueError. A TypeError therefore escaped the flow entirely, so a
        # payload of `[1, 2]` or `"nope"` -- valid JSON, wrong shape, and
        # exactly what a badly cropped QR code decodes to -- produced an
        # unhandled exception and Home Assistant's generic "unknown error"
        # instead of the field error beside the box to re-scan into.
        raise ValueError(  # noqa: TRY004 -- see the comment above
            "not a JSON object"
        )
    missing = {"login", "jeton", "url"} - set(payload)
    if missing:
        raise ValueError(f"missing keys: {', '.join(sorted(missing))}")

    # Presence is not enough, and the gap was expensive. `qrcode_login` does
    # `bytes.fromhex(qr_code["login"])` and the same for `jeton` **before** the
    # `try` that turns a decryption failure into `QRCodeDecryptError`
    # (pronotepy 2.15.7, `clients.py:184-191`). A truncated or mangled hex
    # string therefore raises a bare `ValueError` from inside upstream, which is
    # not a `PronoteAPIError`, escapes every classified arm of the probe, and
    # lands in the last-resort `except Exception`.
    #
    # Two consequences, neither of them the user's fault. The screen said
    # "unexpected error" for what is a copy-paste problem, and the limiter was
    # told `TRANSPORT` -- which calls `note_failure()` and opens an exponential
    # backoff hold on the *instance-wide* guard, blocking every other config
    # flow, and growing on each retry. All of it for an attempt that never
    # opened a socket. A messaging app inserting a timestamp into the pasted
    # JSON is enough to produce it.
    for key in ("login", "jeton"):
        value = payload[key]
        if not isinstance(value, str):
            # A `ValueError`, like every other refusal in this function,
            # for the reason the comment above `not a JSON object` gives:
            # the one caller catches `ValueError`, and a `TypeError` here
            # would escape the flow entirely.
            raise ValueError(  # noqa: TRY004 -- see the comment above
                f"{key} is not a string"
            )
        try:
            bytes.fromhex(value)
        except ValueError as error:
            raise ValueError(f"{key} is not hexadecimal") from error
    if not isinstance(payload["url"], str) or not payload["url"]:
        # `urlparse` is called on it a few lines further up in upstream, and it
        # is what the entry's address is rebuilt from.
        raise ValueError("url is not a usable string")
    return payload


class ProbeError(Exception):
    """Base class for the failures the flow knows how to explain."""


class ProbeInvalidCredentials(ProbeError):  # noqa: N818 -- a flow outcome, not an error class
    """The credentials were refused."""


class ProbeMfaRequired(ProbeError):  # noqa: N818 -- a flow outcome, not an error class
    """PRONOTE asked for the 2FA PIN."""


class ProbeBootstrapFailed(ProbeError):  # noqa: N818 -- a flow outcome, not an error class
    """The bootstrap page carried no session block. Cause deliberately unnamed."""


class ProbeEntRequired(ProbeError):  # noqa: N818 -- a flow outcome, not an error class
    """Direct login was tried on a space that delegates it to the ENT portal.

    The address is right and nothing was refused: the establishment has not
    enabled PRONOTE's own login, so every visit is sent to the portal's form.
    The credentials never left the house -- `pronotepy` stops at the bootstrap,
    before the challenge -- which is why this is not an invalid password.
    """


class ProbeQrInvalid(ProbeError):  # noqa: N818 -- a flow outcome, not an error class
    """The QR payload or its PIN was rejected."""


class ProbeEntUnknown(ProbeError):  # noqa: N818 -- a flow outcome, not an error class
    """The named ENT provider does not exist in the installed ``pronotepy``.

    Its own category because the alternative was silence: the resolver returned
    ``None`` and the login proceeded in direct mode, sending the ENT portal's
    credentials to the PRONOTE server. See :func:`.flow_login._ent_provider`.
    """


class ProbeQrRefused(ProbeError):  # noqa: N818 -- a flow outcome, not an error class
    """The QR code decrypted, and then PRONOTE would not honour it.

    Distinct from :class:`ProbeQrInvalid`, which is the *local* failure: a
    four-digit code that cannot decrypt the payload at all. This one is the
    server refusing the challenge afterwards, and separating them is the whole
    point -- the first is a typo the user can fix, the second is about the QR
    code's own validity and cannot be fixed by re-reading any field on the
    form.
    """
