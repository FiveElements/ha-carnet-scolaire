"""Services: the on-demand actions, and the secrets that must not become states.

Two rules are load-bearing here, and both are tested by their failure mode
rather than by their happy path.

**A secret is returned, never stored (§8.2).** The iCal URL grants read access
to a child's whole timetable with no username and no password. As a state it
would land in the recorder database, in every backup, in screenshots and in
bug reports; as a service response it exists for the length of one script run.
``test_the_ical_url_never_reaches_a_state_or_a_diagnostic`` is what holds that
line -- it sweeps every state, every attribute and the diagnostics payload for
the URL the service just handed back.

**A write is off by default (§8.3).** Ticking a homework item, marking news read
and sending a message all change what the establishment sees, so each is
refused -- with an explanation, not silently -- until the user turns writes on.

The identity service gets a test of its own for a subtler reason: it must post
through ``communication.post`` with an explicit ``ressource``, because
``client.post`` stamps the *account holder* as ``membre`` and on a parent
account returns the **parent's** birth date, e-mail and INE number under the
child's device. That is a wrong-attribution disclosure of exactly the fields
§8.2 keeps out of the state machine, and it is invisible to any test that only
checks the response is well-formed.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
from pronotepy.exceptions import DataError, PronoteAPIError
import pytest
import requests
import voluptuous as vol

from custom_components.carnet_scolaire.const import (
    DOMAIN,
    OPT_WRITE_OPERATIONS_ENABLED,
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
from custom_components.carnet_scolaire.gateway import ProtocolChanged
from custom_components.carnet_scolaire.ratelimit import DeferReason, TierDeferred
from custom_components.carnet_scolaire.session import (
    AccountUnreadable,
    BootstrapFailed,
    IntegrationFault,
    InvalidCredentials,
    LoginRefused,
    MfaRequired,
)

from .conftest import CHILDREN, REQUIRES_HASS, child_key
from .fixtures import protocol
from .fixtures.client import FakeClient, FakeThread
from .keys import key_of, thread_key

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.carnet_scolaire.account import PronoteAccount

pytestmark = REQUIRES_HASS

STUDENT_ONE, STUDENT_TWO = (child_id for child_id, _name in CHILDREN)


def _device_id(hass: HomeAssistant, entry_id: str, identifier: str) -> str:
    """The registry id of one of our devices, by its PRONOTE identifier.

    Looked up *through the owning entry*: from HA 2026.9 an identifier is only
    unique within a config entry, and the entry-less lookup this used to do is
    deprecated for being ambiguous. Passing the entry is not a workaround, it
    is the correct question -- two accounts on one instance can legitimately
    follow children whose identifiers collide.
    """
    registry = dr.async_get(hass)
    device = registry.async_get_device_by_identifier((DOMAIN, identifier), entry_id)
    assert device is not None, f"no device for {identifier}"
    assert entry_id in device.config_entries
    return device.id


def _child_device(hass: HomeAssistant, entry: MockConfigEntry, student: str) -> str:
    """One child's device id."""
    return _device_id(
        hass, entry.entry_id, f"{entry.entry_id}_{child_key(entry, student)}"
    )


def _account_device(hass: HomeAssistant, entry: MockConfigEntry) -> str:
    """The account device, which carries the diagnostics."""
    return _device_id(hass, entry.entry_id, entry.entry_id)


@pytest.fixture(name="writes_on")
async def writes_on_fixture(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    account: PronoteAccount,
) -> PronoteAccount:
    """Turn write operations on, which reloads the entry (§7.3).

    Depends on `account` so the entry is set up *before* the options change:
    an update on an unloaded entry reloads nothing, and the test then reads
    `runtime_data` off an entry that was never started.

    Returns the **new** account. The reload builds one, and holding the old
    reference is how a test ends up asserting against a torn-down object --
    which is also exactly what a user's automation would do if the integration
    handed out long-lived references, so the fixture models the right thing.
    """
    hass.config_entries.async_update_entry(
        mock_entry,
        options={**mock_entry.options, OPT_WRITE_OPERATIONS_ENABLED: True},
    )
    await hass.async_block_till_done()
    reloaded: PronoteAccount = mock_entry.runtime_data
    assert reloaded.write_enabled
    return reloaded


#: The homework tick is only recorded from a student's own session, so every
#: test that expects it to land logs in as one.
on_a_student_account = pytest.mark.parametrize(
    "account_client", ["student"], indirect=True, ids=["student-account"]
)


# ---------------------------------------------------------------------------
# refresh
# ---------------------------------------------------------------------------


async def test_ten_refreshes_cost_one_collection(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    account: PronoteAccount,
    parent_client: FakeClient,
) -> None:
    """§5.1: the service raises a priority, it does not place a call.

    So it *does* lead to a collection -- a refresh that fetched nothing would
    be a refresh that does nothing -- but the scheduler still decides. Ten
    presses inside one interval therefore cost one batch, because the first
    collection moves the tier's deadline and the nine others find nothing due.

    Asserted on the requests actually placed rather than on the ``boosted``
    flag, which is cleared by the collection it triggers: a test watching the
    flag would pass just as happily if the collection never happened.
    """
    device_id = _child_device(hass, mock_entry, STUDENT_ONE)
    before = parent_client.posted_names.count("DernieresNotes")

    for _ in range(10):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_REFRESH,
            {"device_id": device_id, "tiers": ["marks"]},
            blocking=True,
        )
    await hass.async_block_till_done()

    # One request per child, once -- not ten times, and not zero.
    after = parent_client.posted_names.count("DernieresNotes")
    assert after - before == len(account.students)


async def test_refresh_will_not_force_a_login(
    hass: HomeAssistant, mock_entry: MockConfigEntry, account: PronoteAccount
) -> None:
    """``session`` is absent from the refreshable tiers on purpose.

    Forcing a re-login on demand is the one gesture that can get an address
    suspended, and it is never what somebody pressing "refresh" wants
    (annexe B §3).
    """
    with pytest.raises(vol.Invalid):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_REFRESH,
            {
                "device_id": _child_device(hass, mock_entry, STUDENT_ONE),
                "tiers": ["session"],
            },
            blocking=True,
        )


# ---------------------------------------------------------------------------
# Target resolution
# ---------------------------------------------------------------------------


async def test_a_device_named_under_target_is_accepted_like_one_under_data(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    account: PronoteAccount,
    parent_client: Any,
) -> None:
    """The same gesture, in the shape a dashboard button actually sends it.

    ``ServiceRegistry.async_call`` ends with ``service_data.update(target)``,
    and ``target`` has already been through ``TargetSelector.CONFIG_SCHEMA``,
    which runs ``device_id`` through ``cv.ensure_list``. So a device written
    under ``data:`` arrives as a string and the *same* device written under
    ``target:`` arrives as a one-element list -- and every action picker in the
    dashboard editor writes ``target:``.

    Declaring ``cv.string`` therefore refused every such call with ``value
    should be a string at 'device_id'``, a message that names the caller and
    not the schema: it cost a peer session a debugging round on the wrong side
    of the boundary, and it would have refused any automation a user built with
    the visual editor. Asserted on the request placed rather than on the
    absence of an exception, so this cannot pass against a schema that accepts
    the shape and then resolves nothing.
    """
    device_id = _child_device(hass, mock_entry, STUDENT_ONE)
    before = parent_client.posted_names.count("DernieresNotes")

    await hass.services.async_call(
        DOMAIN,
        SERVICE_REFRESH,
        {"tiers": ["marks"]},
        target={"device_id": [device_id]},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert parent_client.posted_names.count("DernieresNotes") - before == len(
        account.students
    )


async def test_naming_several_devices_at_once_is_refused_rather_than_guessed(
    hass: HomeAssistant, mock_entry: MockConfigEntry, account: PronoteAccount
) -> None:
    """Accepting a list must not become accepting *any* list.

    Each of these services resolves exactly one account, so taking the first
    entry of a two-device target would act on a child the caller did not name
    -- the same wrong-attribution class as the identity service posting under
    the account holder. The narrow fix for the ``target:`` shape has to stay
    narrow, and this is the assertion that keeps it so.
    """
    device_id = _child_device(hass, mock_entry, STUDENT_ONE)

    with pytest.raises(vol.Invalid):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_REFRESH,
            {},
            target={"device_id": [device_id, device_id]},
            blocking=True,
        )


async def test_an_unknown_device_is_refused_by_name(
    hass: HomeAssistant, account: PronoteAccount
) -> None:
    """A validation error, so the automation editor shows it to the user."""
    with pytest.raises(ServiceValidationError) as raised:
        await hass.services.async_call(
            DOMAIN, SERVICE_REFRESH, {"device_id": "not-a-device"}, blocking=True
        )

    assert raised.value.translation_key == "unknown_device"


async def test_a_device_from_another_integration_is_refused(
    hass: HomeAssistant, mock_entry: MockConfigEntry, account: PronoteAccount
) -> None:
    """Targeting the wrong device must say so rather than act on a guess."""
    registry = dr.async_get(hass)
    stranger = registry.async_get_or_create(
        config_entry_id=mock_entry.entry_id,
        identifiers={("some_other_domain", "whatever")},
        name="Not ours",
    )

    with pytest.raises(ServiceValidationError) as raised:
        await hass.services.async_call(
            DOMAIN, SERVICE_REFRESH, {"device_id": stranger.id}, blocking=True
        )

    assert raised.value.translation_key == "device_not_pronote"


async def test_a_two_child_account_must_say_which_child(
    hass: HomeAssistant, mock_entry: MockConfigEntry, account: PronoteAccount
) -> None:
    """Guessing would send a message, or return an identity, from the wrong one.

    The account device is accepted as a shorthand only where it is unambiguous,
    which is an account with a single child.
    """
    with pytest.raises(ServiceValidationError) as raised:
        await hass.services.async_call(
            DOMAIN,
            SERVICE_GET_ICAL_URL,
            {"device_id": _account_device(hass, mock_entry)},
            blocking=True,
            return_response=True,
        )

    assert raised.value.translation_key == "student_required"
    assert raised.value.translation_placeholders is not None
    assert "Enfant Un" in raised.value.translation_placeholders["children"]


async def test_a_service_that_was_deferred_fails_visibly(
    hass: HomeAssistant, mock_entry: MockConfigEntry, account: PronoteAccount
) -> None:
    """The same reasoning as the homework checkbox, on the other write path.

    ``TierDeferred`` is a control-flow signal, and it is the right one for a
    *scheduled* collection: the tier keeps its snapshot and comes back at the
    next tick, so nothing needs to be said. It is the wrong one for a service
    call, which somebody or some automation is waiting on: returning normally
    would report success for an action that never reached the school.

    So it is translated into an error that names the reason and roughly how
    long to wait. `service_errors.async_run_gesture` does this conversion for
    the checkbox and for every service -- one chokepoint, two callers, and this
    is the test for the services.
    """

    async def _deferred(*_args: Any, **_kwargs: Any) -> Any:
        raise TierDeferred(DeferReason.DAILY_CAP, 42.7)

    with (
        patch.object(account.extras.session, "run", _deferred),
        pytest.raises(HomeAssistantError) as raised,
    ):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_GET_ICAL_URL,
            {"device_id": _child_device(hass, mock_entry, STUDENT_ONE)},
            blocking=True,
            return_response=True,
        )

    assert raised.value.translation_key == "service_deferred"
    assert raised.value.translation_placeholders == {
        "reason": "daily_cap",
        "seconds": "42",
    }
    # Truncated rather than rounded, and deliberately: "wait 42 seconds" and
    # then being refused at 43 is worse than waiting a second longer than told.


async def test_a_deferred_service_is_not_a_validation_error(
    hass: HomeAssistant, mock_entry: MockConfigEntry, account: PronoteAccount
) -> None:
    """The class of the error is part of the message, not an implementation
    detail.

    ``ServiceValidationError`` means "what you asked for is wrong" and Home
    Assistant renders it as the caller's mistake. A deferral is the opposite:
    the call was correct and the integration chose not to send it yet. Raising
    the validation class would tell an automation's author to fix a call that
    has nothing wrong with it -- so this asserts the *plain*
    ``HomeAssistantError`` rather than accepting any subclass.
    """

    async def _deferred(*_args: Any, **_kwargs: Any) -> Any:
        raise TierDeferred(DeferReason.HOURLY_BUDGET, 900.0)

    with (
        patch.object(account.extras.session, "run", _deferred),
        pytest.raises(HomeAssistantError) as raised,
    ):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_GET_IDENTITY,
            {"device_id": _child_device(hass, mock_entry, STUDENT_ONE)},
            blocking=True,
            return_response=True,
        )

    assert not isinstance(raised.value, ServiceValidationError)


async def test_a_service_reaches_pronote_as_a_gesture(
    hass: HomeAssistant, mock_entry: MockConfigEntry, account: PronoteAccount
) -> None:
    """Observed at the session, not read off the default argument.

    A service is somebody waiting -- a card, a dashboard button, a voice
    request -- so it crosses quiet hours like a click on a document. Before
    this it ran at ``HIGH`` and was refused from 22:00 to 06:00 with
    ``quiet_hours``, which read as "the integration is disconnected".
    """
    seen: list[Priority] = []
    original = account.extras.session.run

    async def _spy(tier: str, priority: Priority, fn: Any, **kwargs: Any) -> Any:
        seen.append(priority)
        return await original(tier, priority, fn, **kwargs)

    with patch.object(account.extras.session, "run", _spy):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_GET_ICAL_URL,
            {"device_id": _child_device(hass, mock_entry, STUDENT_ONE)},
            blocking=True,
            return_response=True,
        )

    assert seen == [Priority.GESTURE]


# ---------------------------------------------------------------------------
# Failures on the way to PRONOTE -- Silver `action-exceptions`
# ---------------------------------------------------------------------------

#: Planted in every injected failure's text, to prove none of it reaches the
#: caller. Visibly fictional, as every value in this suite must be, and shaped
#: like what a real ``requests`` error quotes: the address it failed on.
_LEAKY_TEXT = (
    "https://demo.example.invalid/pronote/eleve.html?identifiant=NOT-A-REAL-TOKEN"
)


def _timed_out() -> IntegrationFault:
    """What the session raises for a call that outlived its deadline."""
    fault = IntegrationFault(_LEAKY_TEXT)
    fault.__cause__ = TimeoutError(_LEAKY_TEXT)
    return fault


@pytest.mark.parametrize(
    ("failure", "key", "placeholders"),
    [
        pytest.param(
            LoginRefused(str(DeferReason.LOGIN_CAP)),
            "service_login_refused",
            {"reason": "login_cap"},
            id="login-refused-by-the-limiter",
        ),
        pytest.param(
            InvalidCredentials(_LEAKY_TEXT),
            "service_reauth_required",
            None,
            id="invalid-credentials",
        ),
        pytest.param(
            MfaRequired(_LEAKY_TEXT), "service_reauth_required", None, id="mfa-required"
        ),
        pytest.param(
            BootstrapFailed(_LEAKY_TEXT),
            "service_unreachable",
            None,
            id="bootstrap-failed",
        ),
        pytest.param(
            requests.ConnectionError(_LEAKY_TEXT),
            "service_unreachable",
            None,
            id="transport-error",
        ),
        pytest.param(
            TimeoutError(_LEAKY_TEXT), "service_unreachable", None, id="timeout"
        ),
        pytest.param(
            _timed_out(),
            "service_unreachable",
            None,
            id="call-past-its-deadline",
        ),
        pytest.param(
            IntegrationFault(_LEAKY_TEXT),
            "service_internal_error",
            None,
            id="integration-fault",
        ),
        pytest.param(
            AccountUnreadable(_LEAKY_TEXT),
            "service_unreadable",
            None,
            id="account-unreadable",
        ),
        pytest.param(
            ProtocolChanged("identity", _LEAKY_TEXT),
            "service_unreadable",
            None,
            id="protocol-changed",
        ),
        pytest.param(
            DataError(_LEAKY_TEXT), "service_unreadable", None, id="undecodable"
        ),
        pytest.param(
            PronoteAPIError(_LEAKY_TEXT),
            "service_refused",
            None,
            id="protocol-refusal",
        ),
    ],
)
async def test_a_failure_on_the_way_to_pronote_reaches_the_caller_as_a_sentence(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    account: PronoteAccount,
    parent_client: FakeClient,
    failure: Exception,
    key: str,
    placeholders: dict[str, str] | None,
) -> None:
    """Every family the session raises becomes a translated error, and only that.

    Before this, ``_run`` translated ``TierDeferred`` and let everything else
    through raw: a school down for the evening reached a script as
    ``ConnectionError`` with the page address in it, and a refused login as a
    class name. Home Assistant shows either as an unexplained failure, and the
    raw text is the one piece of the error that must not travel -- a
    ``requests`` message quotes the URL, and an ENT bounce puts session
    parameters in it.

    So the assertion is threefold: the key names the family, the class is the
    plain ``HomeAssistantError`` (none of these says the *call* was wrong), and
    the planted text appears nowhere the caller can read it.
    """

    async def _fails(*_args: Any, **_kwargs: Any) -> Any:
        raise failure

    with (
        patch.object(account.extras.session, "run", _fails),
        pytest.raises(HomeAssistantError) as raised,
    ):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_GET_ICAL_URL,
            {"device_id": _child_device(hass, mock_entry, STUDENT_ONE)},
            blocking=True,
            return_response=True,
        )

    assert raised.value.translation_key == key
    assert raised.value.translation_placeholders == placeholders
    assert not isinstance(raised.value, ServiceValidationError)
    # Chained to the original, so a traceback keeps the real cause -- except
    # a decoding error, whose text is the payload it could not read.
    decoding = isinstance(failure, DataError)
    assert raised.value.__cause__ is (None if decoding else failure)
    assert "NOT-A-REAL-TOKEN" not in str(raised.value)
    assert "NOT-A-REAL-TOKEN" not in json.dumps(raised.value.translation_placeholders)
    assert parent_client.ical_calls == 0


async def test_an_unreachable_school_is_reported_through_the_real_session(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    account: PronoteAccount,
    parent_client: FakeClient,
) -> None:
    """The same translation, with nothing doubled between the service and the wire.

    The parametrised test above replaces ``session.run``; this one lets the
    real session, limiter and executor classify a ``requests`` failure raised
    by the client itself, which is the path a school down for maintenance
    actually takes. ``OSError`` is re-raised by the session after it has
    charged the backoff, and that re-raise is what used to reach the caller.
    """

    def _unreachable() -> str:
        raise requests.ConnectionError(_LEAKY_TEXT)

    with (
        patch.object(parent_client, "export_ical", _unreachable),
        pytest.raises(HomeAssistantError) as raised,
    ):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_GET_ICAL_URL,
            {"device_id": _child_device(hass, mock_entry, STUDENT_ONE)},
            blocking=True,
            return_response=True,
        )

    assert raised.value.translation_key == "service_unreachable"
    assert isinstance(raised.value.__cause__, requests.ConnectionError)
    assert "NOT-A-REAL-TOKEN" not in str(raised.value)


@on_a_student_account
async def test_a_tick_that_cannot_reach_pronote_sends_nothing_and_boosts_nothing(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    writes_on: PronoteAccount,
    client: FakeClient,
) -> None:
    """A failed write must not look, afterwards, like one that went through.

    The tick reads the homework list first, in the session that posts. When
    that read fails the post must not follow, and the tier must not be
    boosted: a boost is what a *successful* write asks for, and armed after a
    failure it would spend a collection announcing nothing new.
    """

    def _unreachable(_body: Any) -> dict[str, Any]:
        raise requests.ConnectionError(_LEAKY_TEXT)

    homework_id = key_of(writes_on, "HOMEWORK-1")
    client.responses["PageCahierDeTexte"] = _unreachable

    with pytest.raises(HomeAssistantError) as raised:
        await hass.services.async_call(
            DOMAIN,
            SERVICE_MARK_HOMEWORK_DONE,
            {
                "device_id": _child_device(hass, mock_entry, protocol.STUDENT_ID),
                "homework_id": homework_id,
                "done": True,
            },
            blocking=True,
        )

    assert raised.value.translation_key == "service_unreachable"
    assert "SaisieTAFFaitEleve" not in client.posted_names
    assert writes_on.scheduler.diagnostics()[str(Tier.HOMEWORK)]["boosted"] is False


# ---------------------------------------------------------------------------
# The secrets that are returned and not stored
# ---------------------------------------------------------------------------


async def test_the_ical_url_never_reaches_a_state_or_a_diagnostic(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    account: PronoteAccount,
    parent_client: FakeClient,
) -> None:
    """§8.2, checked by sweeping everything that gets persisted.

    Anyone holding this URL reads the child's timetable with no credential, and
    states go to the recorder, the backups, the screenshots and the bug
    reports. The service response is the only place it may appear.
    """
    response = await hass.services.async_call(
        DOMAIN,
        SERVICE_GET_ICAL_URL,
        {"device_id": _child_device(hass, mock_entry, STUDENT_ONE)},
        blocking=True,
        return_response=True,
    )

    assert response is not None
    url = response["url"]
    assert url
    assert parent_client.ical_calls == 1

    for state in hass.states.async_all():
        assert url not in state.state
        assert url not in json.dumps(dict(state.attributes), default=str)

    assert url not in json.dumps(account.diagnostics(), default=str)


async def test_the_identity_is_asked_for_as_the_child_and_not_as_the_member(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    account: PronoteAccount,
    parent_client: FakeClient,
) -> None:
    """The wrong-attribution disclosure this service exists to avoid.

    ``client.post`` stamps the account holder as ``membre``; on a parent account
    that returns the **parent's** birth date, e-mail, telephone number and INE
    number, which the service would then hand back attributed to the child.
    ``ClientInfo._cache()`` bypasses ``ClientBase.post`` for exactly this
    reason, and the gateway does the same -- explicitly, with the child's own
    ``ressource``.
    """
    response = await hass.services.async_call(
        DOMAIN,
        SERVICE_GET_IDENTITY,
        {"device_id": _child_device(hass, mock_entry, STUDENT_TWO)},
        blocking=True,
        return_response=True,
    )

    assert response is not None
    assert response["name"]

    assert [name for name, _body in parent_client.communication_posts] == [
        "PageInfosPerso"
    ]
    _name, body = parent_client.communication_posts[0]
    assert body["Signature"]["ressource"] == {"N": STUDENT_TWO, "G": 4}
    assert "PageInfosPerso" not in parent_client.posted_names

    # None of it may become an attribute. Swept on the INE number, the birth
    # date and the address rather than on the name: the child's *name* is the
    # device name and appears in every `friendly_name` by design, so asserting
    # on it would only prove that entities are named after children.
    identifying = [
        response["ine_number"],
        response["birth_date"],
        response["email"],
        response["phone"],
        *response["address"],
    ]
    assert all(identifying), "the fixture must supply every identity field"
    for state in hass.states.async_all():
        attributes = json.dumps(dict(state.attributes), default=str)
        for value in identifying:
            assert str(value) not in attributes, (
                f"{value!r} leaked into {state.entity_id}"
            )


async def test_a_timetable_pdf_is_rendered_in_the_orientation_asked_for(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    account: PronoteAccount,
    parent_client: FakeClient,
) -> None:
    """And returned as a response, because the link carries its own authority."""
    response = await hass.services.async_call(
        DOMAIN,
        SERVICE_GENERATE_TIMETABLE_PDF,
        {
            "device_id": _child_device(hass, mock_entry, STUDENT_ONE),
            "orientation": "landscape",
        },
        blocking=True,
        return_response=True,
    )

    assert response is not None
    assert "portrait=False" in response["url"]
    assert parent_client.pdf_calls == 1


async def test_the_budget_can_be_read_without_spending_any_of_it(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    account: PronoteAccount,
    parent_client: FakeClient,
) -> None:
    """§6.6: safe to poll from a dashboard while tuning the cadence.

    And its response is JSON-serialisable with no secret in it, which is the
    part worth pinning: this is the one service whose payload a user is
    encouraged to paste into an issue.
    """
    before = len(parent_client.posts)

    response = await hass.services.async_call(
        DOMAIN,
        SERVICE_GET_RATE_LIMIT_STATUS,
        {"device_id": _account_device(hass, mock_entry)},
        blocking=True,
        return_response=True,
    )

    assert response is not None
    assert len(parent_client.posts) == before
    assert response["calls_today"] > 0
    assert "scheduler" in response
    assert "session" in response

    payload = json.dumps(response, default=str)
    for secret in (
        mock_entry.data["password"],
        mock_entry.data["username"],
        "icalsecurise",
    ):
        assert secret not in payload


# ---------------------------------------------------------------------------
# Writes -- off by default (§8.3)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("service", "payload"),
    [
        pytest.param(
            SERVICE_MARK_HOMEWORK_DONE,
            {"homework_id": "HOMEWORK-1"},
            id="mark-homework-done",
        ),
        pytest.param(
            SERVICE_MARK_INFORMATION_READ,
            {"information_id": "INFO-1"},
            id="mark-information-read",
        ),
        pytest.param(
            SERVICE_SEND_MESSAGE,
            {"message": "Bonjour", "subject": "Question", "recipients": ["Prof. Un"]},
            id="send-message",
        ),
    ],
)
async def test_a_write_is_refused_with_a_reason_by_default(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    account: PronoteAccount,
    parent_client: FakeClient,
    service: str,
    payload: dict[str, Any],
) -> None:
    """Refused, and refused *audibly*: silently ignoring it would be worse."""
    before = len(parent_client.posts)

    with pytest.raises(ServiceValidationError) as raised:
        await hass.services.async_call(
            DOMAIN,
            service,
            {"device_id": _child_device(hass, mock_entry, STUDENT_ONE), **payload},
            blocking=True,
        )

    assert raised.value.translation_key == "writes_disabled"
    assert len(parent_client.posts) == before


@on_a_student_account
async def test_ticking_a_homework_item_posts_it_and_asks_for_a_re_read(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    writes_on: PronoteAccount,
    client: FakeClient,
) -> None:
    """Checked on the wire, then refreshed by the tier rather than from here.

    The tick is read back inside the call, which is what shows it landed, but
    publishing that read would be a collection outside the scheduler: the
    homework tier is boosted instead.
    """

    await hass.services.async_call(
        DOMAIN,
        SERVICE_MARK_HOMEWORK_DONE,
        {
            "device_id": _child_device(hass, mock_entry, protocol.STUDENT_ID),
            "homework_id": key_of(writes_on, "HOMEWORK-1"),
            "done": True,
        },
        blocking=True,
    )

    assert client.body_for("SaisieTAFFaitEleve") == {
        "listeTAF": [{"N": "HOMEWORK-1", "E": 2, "TAFFait": True}]
    }
    assert writes_on.scheduler.diagnostics()[str(Tier.HOMEWORK)]["boosted"] is True


async def test_a_tick_on_a_parent_account_is_refused_before_anything_is_sent(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    writes_on: PronoteAccount,
    parent_client: FakeClient,
) -> None:
    """PRONOTE answers a parent's tick normally and records nothing.

    Measured on 2026-09-27. Sent anyway, the call would spend three requests
    to produce an error, and an automation written against a parent account
    would fail every time it ran; refused up front, it fails once, for free,
    with a message saying why.
    """
    before = len(parent_client.posts)

    with pytest.raises(ServiceValidationError) as raised:
        await hass.services.async_call(
            DOMAIN,
            SERVICE_MARK_HOMEWORK_DONE,
            {
                "device_id": _child_device(hass, mock_entry, STUDENT_ONE),
                "homework_id": key_of(writes_on, "HOMEWORK-1"),
                "done": True,
            },
            blocking=True,
        )

    assert raised.value.translation_key == "homework_tick_parent_account"
    assert len(parent_client.posts) == before


@on_a_student_account
async def test_a_tick_the_server_acknowledged_and_dropped_is_an_error(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    writes_on: PronoteAccount,
    client: FakeClient,
) -> None:
    """The server's answer proves nothing; the read-back does.

    Two releases in a row announced a tick that PRONOTE had not recorded,
    because a normal answer was taken for a success. Here the server answers
    normally and records nothing, and the automation's trace has to show a
    failure rather than a write that never happened.
    """
    client.responses["SaisieTAFFaitEleve"] = {"dataSec": {"data": {}}}

    with pytest.raises(HomeAssistantError) as raised:
        await hass.services.async_call(
            DOMAIN,
            SERVICE_MARK_HOMEWORK_DONE,
            {
                "device_id": _child_device(hass, mock_entry, protocol.STUDENT_ID),
                "homework_id": key_of(writes_on, "HOMEWORK-1"),
                "done": True,
            },
            blocking=True,
        )

    assert raised.value.translation_key == "write_not_applied"


@on_a_student_account
async def test_a_write_naming_an_item_no_longer_listed_is_refused(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    writes_on: PronoteAccount,
    client: FakeClient,
) -> None:
    """PRONOTE would accept a stale ``N`` and record nothing; the service refuses.

    The key an automation holds is minted from the content, so a withdrawn or
    rewritten item matches nothing in the session that posts -- and that has to
    read as a failure, or the automation's trace shows a write that never
    happened.
    """
    del writes_on
    with pytest.raises(ServiceValidationError) as raised:
        await hass.services.async_call(
            DOMAIN,
            SERVICE_MARK_HOMEWORK_DONE,
            {
                "device_id": _child_device(hass, mock_entry, protocol.STUDENT_ID),
                "homework_id": "hw-0000000000000000",
                "done": True,
            },
            blocking=True,
        )

    assert raised.value.translation_key == "item_not_found"
    assert "SaisieTAFFaitEleve" not in client.posted_names


async def test_marking_a_news_item_read_names_the_child_as_the_public(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    writes_on: PronoteAccount,
    parent_client: FakeClient,
) -> None:
    """``public`` has to be the child, not the account holder.

    Marking it read as the parent leaves the child's own copy unread, so the
    news sensor keeps reporting it and the automation fires again tomorrow.
    """

    await hass.services.async_call(
        DOMAIN,
        SERVICE_MARK_INFORMATION_READ,
        {
            "device_id": _child_device(hass, mock_entry, STUDENT_TWO),
            "information_id": key_of(writes_on, "INFORMATION-1", Tier.NEWS),
        },
        blocking=True,
    )

    body = parent_client.body_for("SaisieActualites")
    assert body["listeActualites"][0]["N"] == "INFORMATION-1"
    assert body["listeActualites"][0]["lue"] is True
    assert body["listeActualites"][0]["public"] == {"N": STUDENT_TWO}


async def test_a_new_thread_needs_a_subject(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    writes_on: PronoteAccount,
    parent_client: FakeClient,
) -> None:
    """Checked before a request is spent, not after."""
    before = len(parent_client.posts)

    with pytest.raises(ServiceValidationError) as raised:
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SEND_MESSAGE,
            {
                "device_id": _child_device(hass, mock_entry, STUDENT_ONE),
                "message": "Bonjour",
                "recipients": ["Prof. Un"],
            },
            blocking=True,
        )

    assert raised.value.translation_key == "subject_required"
    assert len(parent_client.posts) == before


async def test_a_message_may_reply_or_open_a_thread_but_not_both(
    hass: HomeAssistant, mock_entry: MockConfigEntry, writes_on: PronoteAccount
) -> None:
    """Enforced in the schema, so a malformed script fails before it costs."""
    device_id = _child_device(hass, mock_entry, STUDENT_ONE)

    with pytest.raises(vol.Invalid):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SEND_MESSAGE,
            {
                "device_id": device_id,
                "message": "Bonjour",
                "discussion_id": "THREAD-1",
                "recipients": ["Prof. Un"],
            },
            blocking=True,
        )

    with pytest.raises(vol.Invalid):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SEND_MESSAGE,
            {"device_id": device_id, "message": "Bonjour"},
            blocking=True,
        )


async def test_a_new_thread_reaches_the_named_recipients(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    writes_on: PronoteAccount,
    parent_client: FakeClient,
) -> None:
    """Matched on the published name, the only handle a user can read off."""

    await hass.services.async_call(
        DOMAIN,
        SERVICE_SEND_MESSAGE,
        {
            "device_id": _child_device(hass, mock_entry, STUDENT_ONE),
            "message": "Bonjour",
            "subject": "Sortie",
            "recipients": ["prof. un"],
        },
        blocking=True,
    )

    assert parent_client.new_discussions == [("Sortie", "Bonjour", ["Prof. Un"])]


async def test_an_unmatched_recipient_is_refused_with_the_available_names(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    writes_on: PronoteAccount,
    parent_client: FakeClient,
) -> None:
    """A message that quietly went to nobody is worse than an error."""

    with pytest.raises(ServiceValidationError) as raised:
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SEND_MESSAGE,
            {
                "device_id": _child_device(hass, mock_entry, STUDENT_ONE),
                "message": "Bonjour",
                "subject": "Sortie",
                "recipients": ["Personne"],
            },
            blocking=True,
        )

    assert raised.value.translation_key == "recipient_not_found"
    assert raised.value.translation_placeholders is not None
    assert "Prof. Un" in raised.value.translation_placeholders["available"]
    assert parent_client.new_discussions == []


async def test_replying_to_a_thread_that_is_gone_is_refused(
    hass: HomeAssistant, mock_entry: MockConfigEntry, writes_on: PronoteAccount
) -> None:
    """The thread list is re-read on every reply, so it can be missing."""

    with pytest.raises(ServiceValidationError) as raised:
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SEND_MESSAGE,
            {
                "device_id": _child_device(hass, mock_entry, STUDENT_ONE),
                "message": "Merci",
                "discussion_id": "THREAD-GONE",
            },
            blocking=True,
        )

    assert raised.value.translation_key == "discussion_not_found"


async def test_replying_to_a_closed_thread_is_refused(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    writes_on: PronoteAccount,
    parent_client: FakeClient,
) -> None:
    """PRONOTE closes threads; posting into one silently loses the reply."""
    parent_client.threads.append(FakeThread("THREAD-CLOSED", closed=True))

    with pytest.raises(ServiceValidationError) as raised:
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SEND_MESSAGE,
            {
                "device_id": _child_device(hass, mock_entry, STUDENT_ONE),
                "message": "Merci",
                "discussion_id": thread_key(parent_client, "THREAD-CLOSED"),
            },
            blocking=True,
        )

    assert raised.value.translation_key == "discussion_closed"


async def test_a_reply_is_billed_at_what_it_actually_costs(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    writes_on: PronoteAccount,
    parent_client: FakeClient,
) -> None:
    """Three requests, not one -- and the limiter has to be told so.

    A reply re-lists the threads (``ListeMessagerie``), reads the message being
    answered (``ListeMessages``) and posts (``SaisieMessage``). A write that
    under-reports its cost corrupts the very budget that protects the account
    (annexe B §8).
    """
    parent_client.threads.append(FakeThread("THREAD-1"))
    before = writes_on.limiter.snapshot_counters()["calls_today"]

    await hass.services.async_call(
        DOMAIN,
        SERVICE_SEND_MESSAGE,
        {
            "device_id": _child_device(hass, mock_entry, STUDENT_ONE),
            "message": "Merci",
            "discussion_id": thread_key(parent_client, "THREAD-1"),
        },
        blocking=True,
    )

    after = writes_on.limiter.snapshot_counters()["calls_today"]
    assert after - before == 3
    assert parent_client.threads[-1].replies == ["Merci"]


async def test_a_one_child_account_need_not_say_which_child(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    client: FakeClient,
    school_day: Any,
    no_spacing: None,
) -> None:
    """The counterpart of the two-child refusal, and the commonest account.

    A pupil's own account holds exactly one child, so naming it would be
    ceremony: the account device is unambiguous there. Without this the
    shorthand is only ever exercised on the ambiguous side, and a regression
    that refused *every* account device would look like correct strictness.
    """
    with patch(
        "custom_components.carnet_scolaire.session.build_client", return_value=client
    ):
        assert await hass.config_entries.async_setup(mock_entry.entry_id)
        await hass.async_block_till_done()

        response = await hass.services.async_call(
            DOMAIN,
            SERVICE_GET_ICAL_URL,
            {"device_id": _account_device(hass, mock_entry)},
            blocking=True,
            return_response=True,
        )

    assert response is not None


async def test_registering_the_services_twice_leaves_one_of_each(
    hass: HomeAssistant, mock_entry: MockConfigEntry, account: PronoteAccount
) -> None:
    """Two entries on one instance must not fight over the domain's actions.

    The actions belong to the integration, not to an entry, so the second
    entry's set-up calls the same registration. Re-registering would replace
    the live handler mid-flight; the guard is the ``has_service`` check, and
    nothing exercised it.
    """
    from custom_components.carnet_scolaire.services import async_setup_services

    async_setup_services(hass)
    async_setup_services(hass)

    assert hass.services.has_service(DOMAIN, SERVICE_REFRESH)
    assert hass.services.has_service(DOMAIN, SERVICE_GET_ICAL_URL)


# ---------------------------------------------------------------------------
# get_attachment_url -- the address is minted at the click
# ---------------------------------------------------------------------------


class TestTheAttachmentAddressIsMintedOnClick:
    """A file's address exists only in a service response, for five minutes.

    Each child is served *different* homework here, and that is what makes the
    cross-child test mean anything: with the shared fixture both children hold
    the same homework, so one child's key would be equally valid for the other
    and a lookup that ignored the device would still pass.
    """

    @pytest.fixture(name="parent_client")
    def parent_client_fixture(self) -> FakeClient:
        """One file and one link for the first child, one file for the second.

        And a grade whose answers were joined, for the one test that opens a
        graded test's document.
        """
        client = FakeClient(children=CHILDREN)

        def per_child(_body: Any) -> dict[str, Any]:
            if client.selected_child_id == STUDENT_ONE:
                return protocol.homework_response(
                    [
                        protocol.homework(
                            identifier="HOMEWORK-A",
                            attachments=(
                                "enonce.pdf",
                                ("Le sujet", "https://exemple.invalid/sujet"),
                            ),
                        )
                    ]
                )
            return protocol.homework_response(
                [protocol.homework(identifier="HOMEWORK-B", attachments=("autre.pdf",))]
            )

        client.responses["PageCahierDeTexte"] = per_child
        client.responses["DernieresNotes"] = protocol.marks_response(
            grades=[protocol.grade(correction_file="corrige.pdf")]
        )
        return client

    @staticmethod
    def _key(hass: HomeAssistant, entity_id: str, kind: str) -> str:
        """The key a card would read off the first homework item."""
        state = hass.states.get(entity_id)
        assert state is not None
        refs = state.attributes["items"][0]["attachment_refs"]
        if kind == "external":
            # A link carries no key; the fingerprint a card could compute from
            # it is what a misbehaving caller would send.
            from custom_components.carnet_scolaire.attachment import fingerprint

            item_id = state.attributes["items"][0]["id"]
            return fingerprint(item_id, "ATTACHMENT-2")
        key: str = next(ref["key"] for ref in refs if ref["kind"] == kind)
        return key

    async def _call(
        self, hass: HomeAssistant, mock_entry: MockConfigEntry, student: str, key: str
    ) -> dict[str, Any]:
        response = await hass.services.async_call(
            DOMAIN,
            SERVICE_GET_ATTACHMENT_URL,
            {"device_id": _child_device(hass, mock_entry, student), "key": key},
            blocking=True,
            return_response=True,
        )
        assert response is not None
        return dict(response)

    async def test_a_file_key_is_answered_with_a_signed_address(
        self,
        hass: HomeAssistant,
        mock_entry: MockConfigEntry,
        account: PronoteAccount,
    ) -> None:
        """The address names the relay, the account and the document, and is signed.

        Rooted rather than absolute, because it is Home Assistant's own route
        and the card resolves it against the instance -- never against the page
        it happens to be embedded in.
        """
        del account
        key = self._key(hass, "sensor.enfant_un_homework_to_do", "local")

        response = await self._call(hass, mock_entry, STUDENT_ONE, key)

        assert response["url"].startswith(
            f"/api/{DOMAIN}/attachment/{mock_entry.entry_id}/{key}?authSig="
        )
        assert "expires_at" in response

    async def test_the_address_expires_in_minutes(
        self,
        hass: HomeAssistant,
        mock_entry: MockConfigEntry,
        account: PronoteAccount,
    ) -> None:
        """What the response says about expiry is what the signature enforces.

        Read back from the JWT itself rather than trusted from `expires_at`, so
        a lifetime changed in one place and not the other is caught.
        """
        del account
        import base64

        from homeassistant.util import dt as dt_util

        key = self._key(hass, "sensor.enfant_un_homework_to_do", "local")
        before = dt_util.utcnow()
        response = await self._call(hass, mock_entry, STUDENT_ONE, key)

        token = response["url"].split("authSig=", 1)[1]
        body = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        lifetime = claims["exp"] - claims["iat"]

        assert 30 <= lifetime <= 600
        expires_at = dt_util.parse_datetime(response["expires_at"])
        assert expires_at is not None
        assert 0 < (expires_at - before).total_seconds() <= 600

    async def test_minting_an_address_places_no_request_to_pronote(
        self,
        hass: HomeAssistant,
        mock_entry: MockConfigEntry,
        account: PronoteAccount,
        parent_client: FakeClient,
    ) -> None:
        """The service signs locally; only *opening* the address fetches anything.

        This is what lets a card promise it triggers no collection: pressing
        the button costs the school's server nothing: the one request happens
        when the relay opens the address, and nowhere before it.
        """
        key = self._key(hass, "sensor.enfant_un_homework_to_do", "local")
        posts = len(parent_client.posted_names)
        downloads = len(parent_client.communication.session.gets)
        charged = account.limiter.calls_today

        await self._call(hass, mock_entry, STUDENT_ONE, key)

        assert len(parent_client.posted_names) == posts
        assert len(parent_client.communication.session.gets) == downloads
        assert account.limiter.calls_today == charged

    async def test_one_childs_key_with_the_other_childs_device_is_unknown(
        self,
        hass: HomeAssistant,
        mock_entry: MockConfigEntry,
        account: PronoteAccount,
    ) -> None:
        """A key is only good for the child it was published under.

        On a parent account a card holds both children's keys, and a bug there
        -- or a hand-written call -- must not turn one child's key into a
        working address under the sibling's device. The answer is the same
        ``attachment_unknown`` as for a stale key, so the refusal does not even
        confirm that the key exists elsewhere.
        """
        del account
        first_childs = self._key(hass, "sensor.enfant_un_homework_to_do", "local")

        with pytest.raises(ServiceValidationError) as raised:
            await self._call(hass, mock_entry, STUDENT_TWO, first_childs)

        assert raised.value.translation_key == "attachment_unknown"

    async def test_a_link_is_not_minted_an_address(
        self,
        hass: HomeAssistant,
        mock_entry: MockConfigEntry,
        account: PronoteAccount,
    ) -> None:
        """A link already carries its own address, and the relay would refuse it.

        Minting one anyway would hand a card an address guaranteed to 404.
        """
        del account
        link_key = self._key(hass, "sensor.enfant_un_homework_to_do", "external")

        with pytest.raises(ServiceValidationError) as raised:
            await self._call(hass, mock_entry, STUDENT_ONE, link_key)

        assert raised.value.translation_key == "attachment_unknown"

    async def test_before_the_first_collection_it_says_so_rather_than_unknown(
        self,
        hass: HomeAssistant,
        mock_entry: MockConfigEntry,
        account: PronoteAccount,
    ) -> None:
        """ "Not collected yet" and "no such document" are different things to say.

        The first is temporary -- the seconds after a restart -- and the card
        tells the user to wait. The second means the homework changed, and the
        card tells them to refresh. Merged into one, the card would give the
        wrong advice half the time.
        """
        key = self._key(hass, "sensor.enfant_un_homework_to_do", "local")

        with (
            patch.object(account, "snapshot", return_value=None),
            pytest.raises(ServiceValidationError) as raised,
        ):
            await self._call(hass, mock_entry, STUDENT_ONE, key)

        assert raised.value.translation_key == "attachment_not_collected"

    async def test_a_graded_tests_answers_are_minted_an_address_like_a_file(
        self,
        hass: HomeAssistant,
        mock_entry: MockConfigEntry,
        account: PronoteAccount,
        parent_client: FakeClient,
    ) -> None:
        """One service for every document, so a card has one way to open one.

        A grade's key resolves against the marks snapshot, and is signed the
        same way, for the same five minutes, with no request placed.
        """
        del account
        state = hass.states.get("sensor.enfant_un_grades")
        assert state is not None
        (ref,) = state.attributes["items"][0]["attachment_refs"]
        posts = len(parent_client.posted_names)

        response = await self._call(hass, mock_entry, STUDENT_ONE, ref["key"])

        assert response["url"].startswith(
            f"/api/{DOMAIN}/attachment/{mock_entry.entry_id}/{ref['key']}?authSig="
        )
        assert len(parent_client.posted_names) == posts

    @pytest.mark.parametrize(
        "key",
        [
            pytest.param("0123456789ABCDEF", id="upper case"),
            pytest.param("0123456789abcde", id="one character short"),
            pytest.param("0123456789abcdef0", id="one character long"),
            pytest.param("../../auth/token", id="a path"),
        ],
    )
    async def test_a_malformed_key_is_refused_before_any_lookup(
        self,
        hass: HomeAssistant,
        mock_entry: MockConfigEntry,
        account: PronoteAccount,
        key: str,
    ) -> None:
        """Invalid input, not "unknown document" -- a typo is not a rotation.

        And the value never reaches the path it would otherwise be signed into.
        """
        del account
        with pytest.raises(vol.Invalid):
            await self._call(hass, mock_entry, STUDENT_ONE, key)

    async def test_the_translation_keys_are_a_published_contract(self) -> None:
        """The card chooses its sentence by these two values.

        Renaming one would not fail anything here -- the error would still be
        raised, just with a key the card no longer recognises -- so it would
        fall back to a generic message, silently. Pinned against the catalogue
        so a rename has to be made on purpose.
        """
        strings = json.loads(
            (
                __import__("pathlib").Path(__file__).parent.parent
                / "custom_components"
                / DOMAIN
                / "strings.json"
            ).read_text(encoding="utf-8")
        )

        assert {"attachment_not_collected", "attachment_unknown"} <= set(
            strings["exceptions"]
        )
