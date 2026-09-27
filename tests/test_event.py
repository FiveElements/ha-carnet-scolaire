"""The event entities, and the three checks that keep them apart.

``event.py`` exists because a state trigger cannot say "a *new* grade
arrived". The entity therefore listens on a single bus signal shared by every
event entity of every account on the instance, and decides for itself whether
a given signal is its own. The entry check was the one nothing exercised --
and it is the check that keeps two accounts on one instance from firing each
other's automations.
"""

from __future__ import annotations

from collections import Counter
import datetime as dt
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.core import Event
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import async_capture_events

from custom_components.carnet_scolaire.account import SIGNAL_DELTA
from custom_components.carnet_scolaire.const import (
    DEFAULT_TIER_INTERVALS,
    EVENT_ABSENCE_ADDED,
    EVENT_EVALUATION_ADDED,
    EVENT_GRADE_ADDED,
    Tier,
)
from custom_components.carnet_scolaire.event import EVENTS, PronoteEventEntity

from .conftest import REQUIRES_HASS
from .fixtures import protocol

if TYPE_CHECKING:
    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.carnet_scolaire.account import PronoteAccount

    from .fixtures.client import FakeClient

pytestmark = REQUIRES_HASS


def _grade_event_entity(account: PronoteAccount) -> PronoteEventEntity:
    """The first child's new-grade event entity, as the platform builds it."""
    description = next(item for item in EVENTS if item.tier is Tier.MARKS)
    return PronoteEventEntity(
        account,
        account.coordinators[description.tier],
        account.students[0],
        description,
    )


def _signal(hass: HomeAssistant, **data: Any) -> Event:
    """One delta signal, as ``PronoteAccount`` fires it."""
    return Event(SIGNAL_DELTA, data)


async def test_a_signal_from_another_account_is_ignored(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    account: PronoteAccount,
) -> None:
    """Two accounts on one instance share the bus and must not share events.

    A second family's entry, a second school, or the same school under two
    logins all publish on ``SIGNAL_DELTA``. Without the entry check every
    "new grade" automation would fire for every child on the instance, and the
    payload it inspected would name a child it has no entity for.
    """
    entity = _grade_event_entity(account)
    description = entity.entity_description

    entity._async_handle_delta(
        _signal(
            hass,
            entry_id="an-entry-that-is-not-ours",
            student_id=account.students[0].id,
            entity_key=description.key,
            event_type=EVENT_GRADE_ADDED,
        )
    )

    assert entity.state is None


async def test_a_signal_for_another_child_of_the_same_account_is_ignored(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    account: PronoteAccount,
) -> None:
    """One parent account, two children, two sets of entities.

    The entry matches here, so only the student check can tell them apart --
    and getting it wrong would fire the elder's automation on the younger's
    grade, which reads as a correct integration doing the wrong thing.
    """
    entity = _grade_event_entity(account)
    description = entity.entity_description

    entity._async_handle_delta(
        _signal(
            hass,
            entry_id=mock_entry.entry_id,
            student_id=account.students[1].id,
            entity_key=description.key,
            event_type=EVENT_GRADE_ADDED,
        )
    )

    assert entity.state is None


async def _reconnect(account: PronoteAccount, client: FakeClient) -> None:
    """Drop the session so the next call logs in again, every ``N`` re-encrypted."""
    client.rotate_identifiers()
    assert account.extras is not None
    await account.extras.session._reopen()


async def _collect(
    hass: HomeAssistant,
    account: PronoteAccount,
    clock: FrozenDateTimeFactory | None = None,
) -> None:
    """Collect the attendance and evaluations tiers now.

    ``clock`` first moves past the longer of the two intervals: a tier takes
    one boost per interval, so a second request within it is refused.
    """
    if clock is not None:
        clock.tick(timedelta(minutes=DEFAULT_TIER_INTERVALS[Tier.EVALUATIONS] + 1))
    account.scheduler.request([Tier.ATTENDANCE, Tier.EVALUATIONS])
    await account.async_request_tick()
    await hass.async_block_till_done()


def _serve_session(client: FakeClient, session: int, *, recorded: bool) -> None:
    """What the server answers in one session.

    Every item under the ``N`` this session encrypts it to, because the server
    re-encrypts items as well as periods. ``recorded`` adds one absence and one
    evaluation to the fixture's defaults: what the school entered since.
    """

    def serial(item: str) -> str:
        return f"{item}#session-{session}"

    absences = [protocol.absence(identifier=serial("ABSENCE-1"))]
    evaluations = [protocol.evaluation(identifier=serial("EVALUATION-1"))]
    if recorded:
        absences.append(
            protocol.absence(
                identifier=serial("ABSENCE-2"),
                start=dt.datetime(2026, 3, 11, 13, 0),  # noqa: DTZ001 -- protocol times are naive
                end=dt.datetime(2026, 3, 11, 17, 0),  # noqa: DTZ001
            )
        )
        evaluations.append(
            protocol.evaluation(
                identifier=serial("EVALUATION-2"), name="Rédiger un texte argumenté"
            )
        )
    client.responses["PagePresence"] = protocol.attendance_response(
        [
            *absences,
            protocol.delay(identifier=serial("DELAY-1")),
            protocol.punishment(identifier=serial("PUNISHMENT-1")),
        ]
    )
    client.responses["DernieresEvaluations"] = protocol.evaluations_response(
        evaluations
    )


def _ours(account: PronoteAccount, fired: list[Event]) -> list[tuple[str, str]]:
    """``(student_id, event_type)`` for every delta signal this account fired."""
    return [
        (event.data["student_id"], event.data["event_type"])
        for event in fired
        if event.data["entry_id"] == account.entry.entry_id
    ]


async def test_an_absence_and_an_evaluation_recorded_after_a_reconnection_fire_once(
    hass: HomeAssistant,
    account: PronoteAccount,
    parent_client: FakeClient,
    school_day: FrozenDateTimeFactory,
) -> None:
    """After a login the detector compares; it does not prime all over again.

    The absences, delays, punishments and evaluations collections were named
    after the period's ``N``, which PRONOTE re-encrypts at every login. Once
    the account had re-read the periods of the new session, the next
    collection opened a fresh collection, the detector treated it as a first
    pass by design, and the absence or evaluation that arrived with it never
    reached an automation -- while the memory of the old name stayed behind.

    The fake re-encrypts the periods as the server does, and the items
    already known come back under fresh ``N`` too, so only the stable keys
    can recognise them. The first collection after the login and the last one
    are the controls: nothing new, nothing fired.
    """
    periods_before = {period.index: period.id for period in account.state.periods}
    fired = async_capture_events(hass, SIGNAL_DELTA)

    _serve_session(parent_client, 1, recorded=False)
    await _reconnect(account, parent_client)
    await _collect(hass, account)

    periods_after = {period.index: period.id for period in account.state.periods}
    assert periods_after.keys() == periods_before.keys()
    assert all(periods_after[i] != periods_before[i] for i in periods_before)
    assert _ours(account, fired) == []

    _serve_session(parent_client, 1, recorded=True)
    await _collect(hass, account, school_day)

    pairs = _ours(account, fired)
    assert len(set(pairs)) == len(pairs), "an event fired twice"
    assert Counter(event_type for _, event_type in pairs) == {
        EVENT_ABSENCE_ADDED: len(account.students),
        EVENT_EVALUATION_ADDED: len(account.students),
    }
    # And it reached the entity an automation triggers on, for each child:
    # a signal addressed to an identifier no entity answers to any more
    # would be the same missed event, one hop later.
    expected = {
        "new_absence": EVENT_ABSENCE_ADDED,
        "new_evaluation": EVENT_EVALUATION_ADDED,
    }
    entities = [
        entry
        for entry in er.async_entries_for_config_entry(
            er.async_get(hass), account.entry.entry_id
        )
        if entry.domain == "event" and entry.translation_key in expected
    ]
    assert len(entities) == len(expected) * len(account.students)
    for entry in entities:
        state = hass.states.get(entry.entity_id)
        assert state is not None, entry.entity_id
        assert state.state not in ("unknown", "unavailable"), entry.entity_id
        assert state.attributes["event_type"] == expected[entry.translation_key]

    fired.clear()
    await _collect(hass, account, school_day)

    assert _ours(account, fired) == []
