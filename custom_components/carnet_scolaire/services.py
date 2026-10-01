"""Services: the on-demand actions, and the secrets that never become states.

Two rules shape this file.

**A secret is returned, never stored (§8.2).** The iCal URL, the identity and
the timetable PDF link are ``SupportsResponse.ONLY`` services. The iCal URL in
particular grants read access to a child's whole timetable with no username and
no password; as a state it would land in the recorder database, in every
backup, in screenshots and in bug reports. As a service response it exists for
the length of one script run. That is also why it is absent from the
diagnostics download -- there is nothing to redact, because nothing is kept.

**A write is off by default (§8.3).** Ticking a homework item, marking news read
and sending a message all change what the establishment sees. They are refused
unless the user turned write operations on, and refused with an explanation
rather than ignored.

Every call goes through the rate limiter and the serial executor like any
collection: a service is not a dispensation. ``refresh`` in particular does not
call anything itself -- it raises the priority of the requested tiers and lets
the next master tick serve them, so a button mashed ten times still costs one
batch (§5.1).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.util import dt as dt_util
import voluptuous as vol

from .attachment import FINGERPRINT_PATTERN, SIGNATURE_LIFETIME, resolve, signed_path
from .const import (
    AGNOSTIC_SERVICES,
    DOMAIN,
    SERVICE_GENERATE_TIMETABLE_PDF,
    SERVICE_GET_ATTACHMENT_URL,
    SERVICE_GET_ICAL_URL,
    SERVICE_GET_IDENTITY,
    SERVICE_GET_RATE_LIMIT_STATUS,
    SERVICE_MARK_HOMEWORK_DONE,
    SERVICE_MARK_INFORMATION_READ,
    SERVICE_REFRESH,
    SERVICE_SEND_MESSAGE,
    Priority,
    Tier,
)
from .gateway import (
    DiscussionIsClosed,
    DiscussionNotFound,
    ItemNotFound,
    RecipientNotFound,
    WriteNotApplied,
)
from .service_errors import async_run_gesture

if TYPE_CHECKING:
    from datetime import date

    from .account import PronoteAccount
    from .connectors.protocol import PronoteExtras
    from .models import Identity

ATTR_DEVICE_ID: Final = "device_id"
ATTR_TIERS: Final = "tiers"
ATTR_HOMEWORK_ID: Final = "homework_id"
ATTR_DONE: Final = "done"
ATTR_INFORMATION_ID: Final = "information_id"
ATTR_DISCUSSION_ID: Final = "discussion_id"
ATTR_SUBJECT: Final = "subject"
ATTR_MESSAGE: Final = "message"
ATTR_RECIPIENTS: Final = "recipients"
ATTR_DAY: Final = "day"
ATTR_ORIENTATION: Final = "orientation"
ATTR_KEY: Final = "key"

ORIENTATION_PORTRAIT: Final = "portrait"
ORIENTATION_LANDSCAPE: Final = "landscape"

#: Tiers a user may ask to refresh. ``session`` is absent on purpose: forcing a
#: re-login on demand is the one gesture that can get an address suspended, and
#: it is never what somebody pressing "refresh" wants (annexe B §3).
REFRESHABLE_TIERS: Final = tuple(
    tier.value for tier in Tier if tier is not Tier.SESSION
)


def _one_device(value: Any) -> str:
    """Accept the two shapes Home Assistant delivers one device in.

    ``ServiceRegistry.async_call`` ends with ``service_data.update(target)``
    (``core.py``), and ``target`` has already been validated by
    ``TargetSelector.CONFIG_SCHEMA``, which runs ``device_id`` through
    ``cv.ensure_list``. So one and the same gesture arrives here as a **string**
    when the caller wrote it under ``data:`` and as a **list** when the caller
    wrote it under ``target:`` -- and a dashboard's action picker writes
    ``target:``.

    Declaring ``cv.string`` therefore rejected every button on a card with
    ``value should be a string at 'device_id'``, which reads as the card being
    at fault rather than this schema, and cost a peer session a round of
    debugging on the wrong side of the boundary. Nothing in the failure names
    the real cause, which is why the mechanism is written out here rather than
    summarised.

    A list of *several* devices is a different matter and stays an error, with
    a message that says so: each of these services resolves exactly one
    account, so silently taking the first would run against a child the caller
    did not name.
    """
    values = cv.ensure_list(value)
    if len(values) != 1:
        message = (
            f"expected exactly one device, got {len(values)}; each carnet_scolaire "
            "service acts on one account"
        )
        raise vol.Invalid(message)
    return cv.string(values[0])


#: The one device every device-addressed service takes, in either shape.
_DEVICE_ID: Final = _one_device


_DEVICE_SCHEMA: Final = vol.Schema({vol.Required(ATTR_DEVICE_ID): _DEVICE_ID})

#: A malformed key is refused before any lookup, so a typo reads as "invalid
#: input" rather than as "document unknown" -- two different things to tell a
#: user, and the second is what the card shows for a rotated homework.
_ATTACHMENT_URL_SCHEMA: Final = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): _DEVICE_ID,
        vol.Required(ATTR_KEY): vol.All(str, vol.Match(FINGERPRINT_PATTERN)),
    }
)

_REFRESH_SCHEMA: Final = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): _DEVICE_ID,
        vol.Optional(ATTR_TIERS): vol.All(cv.ensure_list, [vol.In(REFRESHABLE_TIERS)]),
    }
)

_MARK_HOMEWORK_SCHEMA: Final = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): _DEVICE_ID,
        vol.Required(ATTR_HOMEWORK_ID): cv.string,
        vol.Optional(ATTR_DONE, default=True): cv.boolean,
    }
)

_MARK_INFORMATION_SCHEMA: Final = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): _DEVICE_ID,
        vol.Required(ATTR_INFORMATION_ID): cv.string,
    }
)

#: Either a reply to an existing thread, or a new one with named recipients --
#: never both, and never neither. Enforced in the schema rather than in the
#: handler so a malformed script fails before it spends a request.
_SEND_MESSAGE_SCHEMA: Final = vol.All(
    vol.Schema(
        {
            vol.Required(ATTR_DEVICE_ID): _DEVICE_ID,
            vol.Required(ATTR_MESSAGE): cv.string,
            vol.Optional(ATTR_DISCUSSION_ID): cv.string,
            vol.Optional(ATTR_SUBJECT): cv.string,
            vol.Optional(ATTR_RECIPIENTS): vol.All(cv.ensure_list, [cv.string]),
        }
    ),
    cv.has_at_least_one_key(ATTR_DISCUSSION_ID, ATTR_RECIPIENTS),
    cv.has_at_most_one_key(ATTR_DISCUSSION_ID, ATTR_RECIPIENTS),
)

_TIMETABLE_PDF_SCHEMA: Final = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): _DEVICE_ID,
        vol.Optional(ATTR_DAY): cv.date,
        vol.Optional(ATTR_ORIENTATION, default=ORIENTATION_PORTRAIT): vol.In(
            (ORIENTATION_PORTRAIT, ORIENTATION_LANDSCAPE)
        ),
    }
)


# ---------------------------------------------------------------------------
# Target resolution
# ---------------------------------------------------------------------------


def _accounts(hass: HomeAssistant) -> dict[str, PronoteAccount]:
    """Every loaded account, keyed by config entry id."""
    accounts: dict[str, PronoteAccount] = hass.data.get(DOMAIN, {})
    return accounts


def _resolve(hass: HomeAssistant, device_id: str) -> tuple[PronoteAccount, str | None]:
    """Map a device to its account and, for a child device, to the child.

    Devices are targeted rather than entities because a service acts on a
    *child*, not on one of their sensors -- and because the child device is what
    the automation editor already offers (§2.3).
    """
    registry = dr.async_get(hass)
    device = registry.async_get(device_id)
    if device is None:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="unknown_device",
            translation_placeholders={"device_id": device_id},
        )

    accounts = _accounts(hass)
    for domain, identifier in device.identifiers:
        if domain != DOMAIN:
            continue
        # `<entry_id>_<student_id>` is a child; a bare `<entry_id>` is the
        # account device that carries the diagnostics.
        if identifier in accounts:
            return accounts[identifier], None
        entry_id, _, child_key = identifier.partition("_")
        account = accounts.get(entry_id)
        if account is not None and child_key:
            # The suffix is the key this integration minted for the child; the
            # gateway needs the identifier PRONOTE announced this session.
            # `None` here means the device belongs to a child the account no
            # longer announces -- an orphan from before the keys existed --
            # which must not be silently served as some other child.
            student_id = account.student_id_for_key(child_key)
            if student_id is not None:
                return account, student_id

    raise ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key="device_not_pronote",
        translation_placeholders={"device_id": device_id},
    )


def _resolve_student(
    hass: HomeAssistant, call: ServiceCall
) -> tuple[PronoteAccount, str]:
    """Resolve a service that needs a specific child.

    An account with a single child accepts the account device as a shorthand;
    with several, the call has to say which one -- guessing would send a message
    from the wrong child.
    """
    account, student_id = _resolve(hass, call.data[ATTR_DEVICE_ID])
    if student_id is not None:
        return account, student_id
    if len(account.students) == 1:
        return account, account.students[0].id
    raise ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key="student_required",
        translation_placeholders={
            "children": ", ".join(student.name for student in account.students)
        },
    )


def _require_writes(account: PronoteAccount) -> None:
    """Refuse a write, with a reason, unless the user enabled writes (§8.3)."""
    if not account.write_enabled:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="writes_disabled",
        )


def _require_homework_ticks(account: PronoteAccount) -> None:
    """Refuse a tick from an account whose ticks PRONOTE records nowhere."""
    if not account.can_tick_homework:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="homework_tick_parent_account",
        )


def _require_service(account: PronoteAccount, service: str) -> None:
    """Refuse a service the connector does not advertise.

    ``refresh`` and ``get_rate_limit_status`` are source-agnostic: they only
    touch the scheduler and the limiter's in-memory counters.
    """
    if service in AGNOSTIC_SERVICES:
        return
    if service not in account.connector.capabilities.services:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="service_not_supported",
            translation_placeholders={
                "source": str(account.connector.capabilities.source),
                "service": service,
            },
        )


def _require_pronote_extras(account: PronoteAccount, service: str) -> PronoteExtras:
    """Refuse callers that need the PRONOTE session façade.

    Refused with the same sentence as :func:`_require_service`: from the
    caller's side both say that this source cannot do this, and which layer
    noticed is not something an automation author can act on.
    """
    extras = account.extras
    if extras is None:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="service_not_supported",
            translation_placeholders={
                "source": str(account.connector.capabilities.source),
                "service": service,
            },
        )
    return extras


async def _run(
    extras: PronoteExtras,
    tier: Tier,
    student_id: str | None,
    fn: Any,
    *,
    cost: int,
    priority: Priority = Priority.GESTURE,
) -> Any:
    """Run one gateway call, translating every session failure into a sentence.

    The translation itself lives in :func:`.service_errors.async_run_gesture`,
    shared with the to-do checkbox, so the two write paths cannot drift apart:
    a deferral, a refused login, an unreachable server all read the same from
    a script as from a tap.
    """
    return await async_run_gesture(
        extras, tier, student_id, fn, cost=cost, priority=priority
    )


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def _async_refresh(call: ServiceCall) -> None:
    """Raise the priority of some tiers for the next tick.

    Deliberately does not fetch. Ten presses inside one interval still cost one
    batch, because the request is a *boost* recorded in the scheduler and not a
    call placed on the wire (§5.1).
    """
    account, _ = _resolve(call.hass, call.data[ATTR_DEVICE_ID])
    _require_service(account, SERVICE_REFRESH)
    requested = call.data.get(ATTR_TIERS)
    tiers = (
        [
            tier
            for tier in (Tier(value) for value in requested)
            if tier in account.connector.capabilities.tiers
        ]
        if requested
        else [
            tier
            for tier in Tier
            if tier is not Tier.SESSION and tier in account.connector.capabilities.tiers
        ]
    )
    if not tiers:
        return
    account.scheduler.request(tiers)
    await account.async_request_tick()


async def _async_get_ical_url(call: ServiceCall) -> ServiceResponse:
    """Return the iCal URL, once, in the response.

    Never a state, never an attribute, never in diagnostics, and never logged.
    Anyone holding this URL reads the child's timetable without credentials
    (§8.2).
    """
    account, student_id = _resolve_student(call.hass, call)
    _require_service(account, SERVICE_GET_ICAL_URL)
    extras = _require_pronote_extras(account, SERVICE_GET_ICAL_URL)

    def work(client: Any) -> tuple[str, int]:
        return extras.gateway.ical_url(client)

    url, _cost = await _run(extras, Tier.TIMETABLE, student_id, work, cost=1)
    return {"url": url}


async def _async_get_identity(call: ServiceCall) -> ServiceResponse:
    """Return the identity block, once, in the response.

    Address, telephone number, e-mail, INE number and guardians are personal
    data with no business in a state machine that gets recorded and backed up.
    """
    account, student_id = _resolve_student(call.hass, call)
    _require_service(account, SERVICE_GET_IDENTITY)
    extras = _require_pronote_extras(account, SERVICE_GET_IDENTITY)

    def work(client: Any) -> tuple[Identity, int]:
        return extras.gateway.identity(client)

    identity, _cost = await _run(extras, Tier.STATIC, student_id, work, cost=1)
    return {
        "name": identity.name,
        "birth_date": identity.birth_date.isoformat() if identity.birth_date else None,
        "birth_place": identity.birth_place,
        "email": identity.email,
        "phone": identity.phone,
        "address": list(identity.address),
        "ine_number": identity.ine_number,
        "guardians": [
            {
                "name": guardian.name,
                "relation": guardian.relation,
                "email": guardian.email,
                "phone": guardian.phone,
                "address": list(guardian.address),
                "is_legal": guardian.is_legal,
            }
            for guardian in identity.guardians
        ],
    }


async def _run_on_item(
    extras: PronoteExtras, tier: Tier, student_id: str | None, fn: Any, *, cost: int
) -> None:
    """Run a write that names one item by its key, refusing a key now gone."""
    try:
        await _run(extras, tier, student_id, fn, cost=cost)
    except ItemNotFound as missing:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="item_not_found",
        ) from missing
    except WriteNotApplied as ignored:
        # Not a validation error: the call was well formed and PRONOTE said
        # yes. The re-read is what showed the answer to be empty.
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="write_not_applied",
        ) from ignored


async def _async_mark_homework_done(call: ServiceCall) -> None:
    """Tick or untick one homework item."""
    account, student_id = _resolve_student(call.hass, call)
    _require_service(account, SERVICE_MARK_HOMEWORK_DONE)
    _require_writes(account)
    extras = _require_pronote_extras(account, SERVICE_MARK_HOMEWORK_DONE)
    _require_homework_ticks(account)
    homework_id = call.data[ATTR_HOMEWORK_ID]
    done = call.data[ATTR_DONE]

    def work(client: Any) -> int:
        return extras.gateway.set_homework_done(client, homework_id, done=done)

    # Three: the list is read in the posting session, then the tick is posted,
    # then the list is read back to prove it landed (see `set_homework_done`).
    await _run_on_item(extras, Tier.HOMEWORK, student_id, work, cost=3)
    # The published snapshot is refreshed by the tier, not from the check read:
    # publishing from here would be a collection outside the scheduler.
    account.scheduler.request([Tier.HOMEWORK])


async def _async_mark_information_read(call: ServiceCall) -> None:
    """Mark one news item as read."""
    account, student_id = _resolve_student(call.hass, call)
    _require_service(account, SERVICE_MARK_INFORMATION_READ)
    _require_writes(account)
    extras = _require_pronote_extras(account, SERVICE_MARK_INFORMATION_READ)
    information_id = call.data[ATTR_INFORMATION_ID]

    def work(client: Any) -> int:
        return extras.gateway.mark_information_read(client, information_id)

    await _run_on_item(extras, Tier.NEWS, student_id, work, cost=2)
    account.scheduler.request([Tier.NEWS])


async def _async_send_message(call: ServiceCall) -> None:
    """Reply to a thread, or open a new one with named recipients.

    Billed at three requests either way, which is what it costs: a reply has to
    re-list the threads, then read the message being answered, then post. A
    write that under-reports its cost corrupts the budget protecting the
    account (annexe B §8).
    """
    account, student_id = _resolve_student(call.hass, call)
    _require_service(account, SERVICE_SEND_MESSAGE)
    _require_writes(account)
    extras = _require_pronote_extras(account, SERVICE_SEND_MESSAGE)
    message = call.data[ATTR_MESSAGE]
    discussion_id = call.data.get(ATTR_DISCUSSION_ID)
    recipients = call.data.get(ATTR_RECIPIENTS)
    subject = call.data.get(ATTR_SUBJECT)

    if discussion_id is None and not subject:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="subject_required",
        )

    def work(client: Any) -> int:
        if discussion_id is not None:
            return extras.gateway.reply_to_discussion(client, discussion_id, message)
        return extras.gateway.start_discussion(
            client, str(subject), message, recipients or []
        )

    try:
        await _run(extras, Tier.DISCUSSIONS, student_id, work, cost=3)
    except DiscussionNotFound as error:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="discussion_not_found",
            translation_placeholders={"discussion_id": error.discussion_id},
        ) from error
    except DiscussionIsClosed as error:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="discussion_closed",
            translation_placeholders={"discussion_id": error.discussion_id},
        ) from error
    except RecipientNotFound as error:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="recipient_not_found",
            translation_placeholders={
                "missing": ", ".join(error.missing),
                "available": ", ".join(error.available),
            },
        ) from error

    account.scheduler.request([Tier.DISCUSSIONS])


async def _async_generate_timetable_pdf(call: ServiceCall) -> ServiceResponse:
    """Ask PRONOTE to render a timetable PDF and return its URL.

    A response and not a state for the same reason as the iCal URL: the link
    carries its own authorisation.
    """
    account, student_id = _resolve_student(call.hass, call)
    _require_service(account, SERVICE_GENERATE_TIMETABLE_PDF)
    extras = _require_pronote_extras(account, SERVICE_GENERATE_TIMETABLE_PDF)
    day: date | None = call.data.get(ATTR_DAY)
    portrait = call.data[ATTR_ORIENTATION] == ORIENTATION_PORTRAIT

    def work(client: Any) -> tuple[str, int]:
        return extras.gateway.timetable_pdf_url(client, day, portrait=portrait)

    url, _cost = await _run(extras, Tier.TIMETABLE, student_id, work, cost=1)
    return {"url": url}


async def _async_get_rate_limit_status(call: ServiceCall) -> ServiceResponse:
    """Report the budget without spending any of it.

    Reads the limiter's counters in memory: zero requests, so this is safe to
    poll from a template or a dashboard while tuning the cadence (§6.6).
    """
    account, _ = _resolve(call.hass, call.data[ATTR_DEVICE_ID])
    _require_service(account, SERVICE_GET_RATE_LIMIT_STATUS)
    counters = account.limiter.snapshot_counters()
    extras = account.extras
    session = (
        extras.session.diagnostics()
        if extras is not None
        else account.connector.diagnostics().get("session", {})
    )
    return {
        **counters,
        "scheduler": account.scheduler.diagnostics(),
        "session": session,
    }


async def _async_get_attachment_url(call: ServiceCall) -> ServiceResponse:
    """Mint the address that opens one homework file, at the instant of the click.

    **Places no request to PRONOTE.** It resolves the key against the snapshot
    already in memory and signs a path with a key Home Assistant already holds;
    nothing leaves this process. The request happens later, when the returned
    address is opened and the relay fetches the bytes -- so a card calling this
    triggers no collection, and the relay's own admission decides the rest.

    Two refusals, and their ``translation_key`` values are a **contract**: a
    card reads them off the error to decide what to tell the user.

    ``attachment_not_collected`` -- the key names nothing yet, and a tier that
    could name it has no snapshot for this child: the homework, or the marks
    for a graded test's paper or answers. Typically the first seconds after a
    restart. Temporary.

    ``attachment_unknown`` -- the snapshots exist and the key is not a file in
    them: the homework left the horizon, the entry changed, or the key belongs
    to *another* child of the same account. The last case is deliberately the
    same answer as the others, so the error does not confirm to a caller that
    the key exists elsewhere.

    Looked up before the snapshots are checked, so a grade's document opens
    while the homework tier is still collecting, and the other way round. An
    establishment that publishes no grades never has a marks snapshot, so an
    unknown key there reads as not collected -- the one case where "temporary"
    overstates it, and the cheaper error to get wrong.
    """
    account, student_id = _resolve_student(call.hass, call)
    _require_service(account, SERVICE_GET_ATTACHMENT_URL)
    key = call.data[ATTR_KEY]
    if resolve(account, key, student_id=student_id) is None:
        pending = not all(
            account.has_data(tier, student_id) for tier in (Tier.HOMEWORK, Tier.MARKS)
        )
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key=(
                "attachment_not_collected" if pending else "attachment_unknown"
            ),
        )
    expires_at = dt_util.utcnow() + SIGNATURE_LIFETIME
    return {
        "url": signed_path(call.hass, account.entry.entry_id, key),
        "expires_at": expires_at.isoformat(),
    }


#: ``(name, handler, schema, response support)``. Kept as data so the test that
#: checks every service is documented in ``services.yaml`` and translated in
#: ``strings.json`` can iterate it (§10.5).
_SERVICES: Final[tuple[tuple[str, Any, Any, SupportsResponse], ...]] = (
    (SERVICE_REFRESH, _async_refresh, _REFRESH_SCHEMA, SupportsResponse.NONE),
    (
        SERVICE_GET_ICAL_URL,
        _async_get_ical_url,
        _DEVICE_SCHEMA,
        SupportsResponse.ONLY,
    ),
    (
        SERVICE_GET_IDENTITY,
        _async_get_identity,
        _DEVICE_SCHEMA,
        SupportsResponse.ONLY,
    ),
    (
        SERVICE_MARK_HOMEWORK_DONE,
        _async_mark_homework_done,
        _MARK_HOMEWORK_SCHEMA,
        SupportsResponse.NONE,
    ),
    (
        SERVICE_MARK_INFORMATION_READ,
        _async_mark_information_read,
        _MARK_INFORMATION_SCHEMA,
        SupportsResponse.NONE,
    ),
    (
        SERVICE_SEND_MESSAGE,
        _async_send_message,
        _SEND_MESSAGE_SCHEMA,
        SupportsResponse.NONE,
    ),
    (
        SERVICE_GENERATE_TIMETABLE_PDF,
        _async_generate_timetable_pdf,
        _TIMETABLE_PDF_SCHEMA,
        SupportsResponse.ONLY,
    ),
    (
        SERVICE_GET_RATE_LIMIT_STATUS,
        _async_get_rate_limit_status,
        _DEVICE_SCHEMA,
        SupportsResponse.ONLY,
    ),
    (
        SERVICE_GET_ATTACHMENT_URL,
        _async_get_attachment_url,
        _ATTACHMENT_URL_SCHEMA,
        SupportsResponse.ONLY,
    ),
)


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register the domain services, once, for the life of Home Assistant.

    Called from ``async_setup`` rather than per entry, so the actions exist
    before any account is loaded and survive one going away. The
    ``has_service`` guard is kept as a cheap belt: registering twice would
    replace the handler rather than fail, which is a silent way to end up with
    two of them.
    """
    for name, handler, schema, supports_response in _SERVICES:
        if hass.services.has_service(DOMAIN, name):
            continue
        hass.services.async_register(
            DOMAIN,
            name,
            handler,
            schema=schema,
            supports_response=supports_response,
        )
