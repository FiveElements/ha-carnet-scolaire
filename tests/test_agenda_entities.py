"""The agenda's three entities, wired end to end.

The calendar, the "next school event" sensor and the "new school event"
``event`` entity are a contract another repository's cards are built against,
so the shapes are asserted literally here: bare dates for an all-day entry,
full instants with their offset otherwise, and an **exclusive** end on both.
"""

from collections import Counter
from dataclasses import replace
import datetime as dt
from datetime import timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from homeassistant.helpers import entity_registry as er
from pytest_homeassistant.common import async_capture_events

from custom_components.carnet_scolaire.account import SIGNAL_DELTA
from custom_components.carnet_scolaire.calendar import CALENDARS, PronoteCalendar
from custom_components.carnet_scolaire.const import (
    DEFAULT_TIER_INTERVALS,
    DOMAIN,
    EVENT_SCHOOL_EVENT_ADDED,
    Tier,
)
from custom_components.carnet_scolaire.sensor import (
    AGENDA_ITEMS_CAP,
    PRIMITIVE_SENSORS,
    PronoteAgendaSensor,
)

from .conftest import REQUIRES_HASS
from .fixtures import protocol

if TYPE_CHECKING:
    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.core import Event, HomeAssistant

    from custom_components.carnet_scolaire.account import PronoteAccount
    from custom_components.carnet_scolaire.models import Student

    from .fixtures.client import FakeClient

pytestmark = REQUIRES_HASS

PARIS = ZoneInfo("Europe/Paris")


def _entity_id(
    hass: HomeAssistant, account: PronoteAccount, domain: str, key: str
) -> str:
    """The first child's entity, found by its unique id rather than its name."""
    student = account.students[0]
    unique_id = f"{account.entry.entry_id}_{account.stable_key(student.id)}_{key}"
    entity_id = er.async_get(hass).async_get_entity_id(domain, DOMAIN, unique_id)
    assert entity_id is not None, unique_id
    return entity_id


async def _collect_agenda(
    hass: HomeAssistant,
    account: PronoteAccount,
    clock: FrozenDateTimeFactory | None = None,
) -> None:
    """Collect the agenda tier now, past its interval when a clock is given."""
    if clock is not None:
        clock.tick(timedelta(minutes=DEFAULT_TIER_INTERVALS[Tier.AGENDA] + 1))
    account.scheduler.request([Tier.AGENDA])
    await account.async_request_tick()
    await hass.async_block_till_done()


def _agenda_sensor(account: PronoteAccount, student: Student) -> PronoteAgendaSensor:
    """The agenda sensor for one child, built as the platform builds it."""
    description = next(
        item for item in PRIMITIVE_SENSORS if item.key == "next_school_event"
    )
    return PronoteAgendaSensor(
        account, account.coordinators[Tier.AGENDA], student, description
    )


def _serve(client: FakeClient, *entries: dict[str, Any]) -> None:
    """What ``PageAgenda`` answers from now on."""
    client.responses["PageAgenda"] = protocol.agenda_response(list(entries))


#: Around Friday 13 March 2026 at 08:01: the fixtures' school day plus the
#: agenda's interval, which a test waits out before collecting again.
ONGOING = protocol.school_event(
    identifier="AGENDA-ONGOING",
    title="Semaine des langues",
    start=dt.datetime(2026, 3, 9, 0, 0),
    end=dt.datetime(2026, 3, 13, 0, 0),
)
PAST = protocol.school_event(
    identifier="AGENDA-PAST",
    title="Sortie au musée",
    start=dt.datetime(2026, 3, 10, 0, 0),
    end=dt.datetime(2026, 3, 10, 0, 0),
)
HOLIDAY = protocol.school_event(
    identifier="AGENDA-HOLIDAY",
    title="Pont",
    start=dt.datetime(2026, 3, 14, 0, 0),
    end=dt.datetime(2026, 3, 15, 0, 0),
    family=protocol.HOLIDAY_FAMILY,
    color="#0A0",
)
GRADES_CLOSE = protocol.school_event(identifier="AGENDA-GRADES")
COUNCIL = protocol.school_event(
    identifier="AGENDA-COUNCIL",
    title="Conseil de classe T2 5E1",
    start=dt.datetime(2026, 3, 24, 17, 15),
    end=dt.datetime(2026, 3, 24, 18, 30),
    all_day=False,
    council=True,
    family="Conseils de classe",
    color=None,
    place="Salle 101",
    comment="<p>Ordre du jour&nbsp;: bilan</p>",
    audience=("5E1",),
)


# ---------------------------------------------------------------------------
# The tier
# ---------------------------------------------------------------------------


async def test_the_agenda_is_collected_once_per_child_after_selecting_it(
    hass: HomeAssistant, account: PronoteAccount, parent_client: FakeClient
) -> None:
    """One request per child, each after that child's selection.

    A post placed before the selection answers for the previous child, and
    the calendar of one would be published under the other's device.
    """
    journal = parent_client.journal
    posts = [
        index for index, item in enumerate(journal) if item == ("post", "PageAgenda")
    ]

    selected_before = [
        next(item for item in reversed(journal[:index]) if item[0] == "select")[1]
        for index in posts
    ]
    assert len(posts) == len(account.students)
    assert set(selected_before) == {student.id for student in account.students}
    snapshot = account.coordinators[Tier.AGENDA].snapshot_for(account.students[0].id)
    assert snapshot is not None
    assert snapshot.calls == 1


# ---------------------------------------------------------------------------
# The sensor
# ---------------------------------------------------------------------------


async def test_the_next_school_event_skips_holidays_and_lists_everything_not_over(
    hass: HomeAssistant,
    account: PronoteAccount,
    parent_client: FakeClient,
    school_day: FrozenDateTimeFactory,
) -> None:
    """The state is the next non-holiday start; ``items`` keeps holidays.

    Tomorrow's long weekend is in the list and not in the state; the language
    week that began on Monday stays listed until it ends tonight; Tuesday's
    outing is gone.
    """
    _serve(parent_client, COUNCIL, HOLIDAY, PAST, GRADES_CLOSE, ONGOING)
    await _collect_agenda(hass, account, school_day)
    entity_id = _entity_id(hass, account, "sensor", "next_school_event")

    state = hass.states.get(entity_id)
    assert state is not None
    assert entity_id.endswith("_next_school_event")
    assert dt.datetime.fromisoformat(state.state) == dt.datetime(
        2026, 3, 20, tzinfo=PARIS
    )
    attributes = state.attributes
    assert attributes["title"] == "Arrêt des notes - trimestre 2"
    assert attributes["all_day"] is True
    assert attributes["end"] == "2026-03-21"
    assert attributes["category"] == "Vie de l'établissement"
    assert attributes["is_class_council"] is False
    assert "fetched_at" in attributes
    assert attributes["stale"] is False

    items = attributes["items"]
    assert [item["title"] for item in items] == [
        "Semaine des langues",
        "Pont",
        "Arrêt des notes - trimestre 2",
        "Conseil de classe T2 5E1",
    ]
    holiday, council = items[1], items[3]
    assert holiday["is_holiday"] is True
    assert (holiday["start"], holiday["end"]) == ("2026-03-14", "2026-03-16")
    assert holiday["color"] == "#00aa00"
    assert holiday["category"] == protocol.HOLIDAY_FAMILY
    assert "audience" not in holiday
    assert council == {
        "key": council["key"],
        "title": "Conseil de classe T2 5E1",
        "start": "2026-03-24T17:15:00+01:00",
        "end": "2026-03-24T18:30:00+01:00",
        "all_day": False,
        "category": "Conseils de classe",
        "color": "#999999",
        "is_class_council": True,
        "is_holiday": False,
        "location": "Salle 101",
        "description": "Ordre du jour : bilan",
        "audience": "5E1",
    }
    assert council["key"].startswith("agenda-")
    assert "AGENDA" not in council["key"]


async def test_only_holidays_ahead_leave_the_state_unknown_but_say_when_it_looked(
    hass: HomeAssistant,
    account: PronoteAccount,
    parent_client: FakeClient,
    school_day: FrozenDateTimeFactory,
) -> None:
    """``unknown`` with ``fetched_at``: "nothing planned", not "never read"."""
    _serve(parent_client, HOLIDAY)
    await _collect_agenda(hass, account, school_day)
    entity_id = _entity_id(hass, account, "sensor", "next_school_event")

    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state == "unknown"
    assert "fetched_at" in state.attributes
    assert "stale" in state.attributes
    assert state.attributes["title"] is None
    assert state.attributes["end"] is None
    assert [item["title"] for item in state.attributes["items"]] == ["Pont"]


async def test_the_list_stops_at_fifty_entries(
    hass: HomeAssistant,
    account: PronoteAccount,
    parent_client: FakeClient,
    school_day: FrozenDateTimeFactory,
) -> None:
    """A year of entries is not a payload any card needs; the first fifty are."""
    _serve(
        parent_client,
        *(
            protocol.school_event(
                identifier=f"AGENDA-{index}",
                title=f"Entrée {index:02d}",
                start=dt.datetime(2026, 4, 1, 0, 0) + timedelta(days=index),
                end=dt.datetime(2026, 4, 1, 0, 0) + timedelta(days=index),
            )
            for index in range(AGENDA_ITEMS_CAP + 10)
        ),
    )
    await _collect_agenda(hass, account, school_day)
    entity_id = _entity_id(hass, account, "sensor", "next_school_event")

    state = hass.states.get(entity_id)
    assert state is not None
    items = state.attributes["items"]
    assert len(items) == AGENDA_ITEMS_CAP
    assert items[0]["title"] == "Entrée 00"


async def test_the_sensor_wakes_when_an_entry_ends_not_only_when_one_starts(
    hass: HomeAssistant,
    account: PronoteAccount,
    parent_client: FakeClient,
    school_day: FrozenDateTimeFactory,
) -> None:
    """The language week ends before anything starts: that is the next wake-up.

    Armed on its own value alone, the sensor would keep a finished entry in
    ``items`` until the next daily collection.
    """
    _serve(parent_client, ONGOING, GRADES_CLOSE)
    await _collect_agenda(hass, account, school_day)
    sensor = _agenda_sensor(account, account.students[0])

    assert sensor._transition_at() == dt.datetime(2026, 3, 14, tzinfo=PARIS)


async def test_a_sensor_for_a_child_never_collected_has_no_transition(
    hass: HomeAssistant, account: PronoteAccount
) -> None:
    """No snapshot, no instant to wake at -- and no exception either."""
    sensor = _agenda_sensor(
        account, replace(account.students[0], id="STUDENT-NEVER-COLLECTED")
    )

    assert sensor._transition_at() is None


# ---------------------------------------------------------------------------
# The calendar
# ---------------------------------------------------------------------------


async def test_the_calendar_publishes_all_day_entries_as_dates_with_an_exclusive_end(
    hass: HomeAssistant,
    account: PronoteAccount,
    parent_client: FakeClient,
    school_day: FrozenDateTimeFactory,
) -> None:
    """The weekend runs to the 15th inclusive, so the calendar ends on the 16th."""
    _serve(parent_client, HOLIDAY, COUNCIL)
    await _collect_agenda(hass, account, school_day)
    entity_id = _entity_id(hass, account, "calendar", "school_agenda")
    description = next(item for item in CALENDARS if item.key == "school_agenda")
    calendar = PronoteCalendar(
        account, account.coordinators[Tier.AGENDA], account.students[0], description
    )

    events = await calendar.async_get_events(
        hass,
        dt.datetime(2026, 3, 1, tzinfo=PARIS),
        dt.datetime(2026, 4, 1, tzinfo=PARIS),
    )

    assert entity_id.endswith("_school_agenda")
    holiday, council = events
    assert (holiday.summary, holiday.start, holiday.end) == (
        "Pont",
        dt.date(2026, 3, 14),
        dt.date(2026, 3, 16),
    )
    assert council.start == dt.datetime(2026, 3, 24, 17, 15, tzinfo=PARIS)
    assert council.end == dt.datetime(2026, 3, 24, 18, 30, tzinfo=PARIS)
    assert council.location == "Salle 101"
    assert council.description == "Ordre du jour : bilan"
    assert council.uid is not None
    assert council.uid.startswith("agenda-")


# ---------------------------------------------------------------------------
# The event
# ---------------------------------------------------------------------------


def _ours(account: PronoteAccount, fired: list[Event]) -> list[dict[str, Any]]:
    """The agenda signals this account fired."""
    return [
        event.data
        for event in fired
        if event.data["entry_id"] == account.entry.entry_id
        and event.data["event_type"] == EVENT_SCHOOL_EVENT_ADDED
    ]


async def test_a_new_entry_fires_once_per_child_and_a_reconnection_fires_nothing(
    hass: HomeAssistant,
    account: PronoteAccount,
    parent_client: FakeClient,
    school_day: FrozenDateTimeFactory,
) -> None:
    """Not at the first fill, not after the ``N`` rotate, yes for a new entry.

    The fixture's first collection is the first fill and must have fired
    nothing. A reconnection then re-encrypts every ``N``; only the minted key
    recognises the entry already announced.
    """
    fired = async_capture_events(hass, SIGNAL_DELTA)
    assert _ours(account, fired) == []

    parent_client.rotate_identifiers()
    assert account.extras is not None
    await account.extras.session._reopen()
    _serve(parent_client, protocol.school_event(identifier="AGENDA-ROTATED"))
    await _collect_agenda(hass, account, school_day)
    assert _ours(account, fired) == []

    _serve(parent_client, protocol.school_event(identifier="AGENDA-ROTATED"), COUNCIL)
    await _collect_agenda(hass, account, school_day)

    signals = _ours(account, fired)
    assert Counter(signal["entity_key"] for signal in signals) == {
        "new_school_event": len(account.students)
    }
    assert signals[0]["attributes"] == {
        "key": signals[0]["attributes"]["key"],
        "title": "Conseil de classe T2 5E1",
        "start": "2026-03-24T17:15:00+01:00",
        "end": "2026-03-24T18:30:00+01:00",
        "all_day": False,
        "category": "Conseils de classe",
        "is_class_council": True,
        "is_holiday": False,
    }
    entity_id = _entity_id(hass, account, "event", "new_school_event")
    state = hass.states.get(entity_id)
    assert state is not None
    assert entity_id.endswith("_new_school_event")
    assert state.attributes["event_type"] == EVENT_SCHOOL_EVENT_ADDED
    assert state.attributes["title"] == "Conseil de classe T2 5E1"
