"""The holiday flag, which is inferred and has to be inferred correctly.

PRONOTE publishes no holiday calendar, so ``binary_sensor.<eleve>_vacances``
reads the only evidence there is: the absence of lessons. That makes it the one
entity whose correctness depends on **how far the timetable was fetched**, and
it got two whole classes of day wrong at once.

A weekend read ``on`` because a seven-day question was asked of a two-day
window. And the last week of every long holiday read ``off`` because looking
seven days ahead found the return. Both matter for the same automation -- "no
school tomorrow, no alarm" -- and both fail it in the direction that wakes a
child at six in the morning, or fails to.
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.carnet_scolaire import binary_sensor
from custom_components.carnet_scolaire.binary_sensor import (
    _holiday_attributes,
    _is_holiday,
)
from custom_components.carnet_scolaire.connectors.protocol import (
    ConnectorCapabilities,
    Source,
)
from custom_components.carnet_scolaire.const import Tier
from custom_components.carnet_scolaire.coordinator import PronoteTierCoordinator
from custom_components.carnet_scolaire.models import (
    Absence,
    AttendanceFacts,
    Punishment,
    PunishmentSlot,
)

from .conftest import CHILDREN, REQUIRES_HASS
from .test_delta import a_lesson, timetable

if TYPE_CHECKING:
    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.carnet_scolaire.account import PronoteAccount
    from custom_components.carnet_scolaire.models import TimetableFacts

    from .fixtures.client import FakeClient

PARIS = ZoneInfo("Europe/Paris")


class _Today:
    """The only thing ``_is_holiday`` asks of an account."""

    def __init__(self, day: dt.date) -> None:
        self._day = day

    def today(self) -> dt.date:
        """The day the entity is being evaluated on."""
        return self._day


def _account(day: dt.date) -> PronoteAccount:
    """An account stand-in pinned to one day."""
    return _Today(day)  # type: ignore[return-value]


def _school_week(monday: dt.date) -> tuple[object, ...]:
    """Five ordinary days of lessons, Monday to Friday."""
    return tuple(
        a_lesson(
            id=f"LESSON-{monday:%Y%m%d}-{offset}",
            start=dt.datetime.combine(
                monday + dt.timedelta(days=offset), dt.time(8, 0), tzinfo=PARIS
            ),
            end=dt.datetime.combine(
                monday + dt.timedelta(days=offset), dt.time(9, 0), tzinfo=PARIS
            ),
        )
        for offset in range(5)
    )


def _facts(*mondays: dt.date) -> TimetableFacts:
    """A fetched window holding a full school week for each Monday given."""
    lessons: list[object] = []
    for monday in mondays:
        lessons.extend(_school_week(monday))
    return timetable(*lessons)  # type: ignore[arg-type]


#: The two weeks the timetable tier now always fetches, in the fixture's year.
THIS_MONDAY = dt.date(2026, 3, 9)
NEXT_MONDAY = dt.date(2026, 3, 16)


@pytest.mark.parametrize(
    "day",
    [dt.date(2026, 3, 14), dt.date(2026, 3, 15)],
    ids=["saturday", "sunday"],
)
def test_an_ordinary_weekend_is_not_a_holiday(day: dt.date) -> None:
    """The defect that started this: a weekend that read ``on``.

    With the old two-day horizon a Saturday held Monday to Friday and nothing
    else, so "no lesson in the next seven days" was true and the flag turned
    ``on`` at one second past midnight -- every weekend, for forty-eight hours.
    The fix is upstream, in what the tier fetches; this pins the answer the
    predicate must give once it has both weeks.
    """
    assert _is_holiday(_facts(THIS_MONDAY, NEXT_MONDAY), _account(day)) is False


def test_the_saturday_a_holiday_starts_is_a_holiday() -> None:
    """The week behind is full, the week ahead is empty: only looking ahead sees it."""
    assert _is_holiday(_facts(THIS_MONDAY), _account(dt.date(2026, 3, 14))) is True


def test_every_day_of_an_empty_week_is_a_holiday() -> None:
    """A fetched week with no lesson in it at all is a break, not a gap."""
    facts = _facts(NEXT_MONDAY)
    for offset in range(7):
        day = THIS_MONDAY + dt.timedelta(days=offset)
        assert _is_holiday(facts, _account(day)) is True, day


def test_the_last_week_of_a_long_holiday_is_still_a_holiday() -> None:
    """The tail of a two-week break, which the forward look alone gets wrong.

    From the Monday of the second week, "is there a lesson in the next seven
    days" finds the return and answers ``off`` while the child is still on
    holiday -- a wrong week at Toussaint, at Noël, in February, at Easter, and
    for the last week of the summer. Asking whether *this* week holds a lesson
    answers it correctly on every one of those days.
    """
    facts = _facts(NEXT_MONDAY)

    assert _is_holiday(facts, _account(dt.date(2026, 3, 9))) is True
    assert _is_holiday(facts, _account(dt.date(2026, 3, 13))) is True
    assert _is_holiday(facts, _account(NEXT_MONDAY)) is False


def test_a_free_monday_does_not_read_as_a_holiday() -> None:
    """The obvious weaker rule -- "no lesson today" -- would fire here.

    A pupil with no Monday lessons is not on holiday, and the week around them
    says so. This is why the clause is about the whole week rather than about
    today.
    """
    lessons = [
        lesson
        for lesson in _school_week(THIS_MONDAY)
        if lesson.start.date() != THIS_MONDAY  # type: ignore[attr-defined]
    ]
    facts = timetable(*lessons, *_school_week(NEXT_MONDAY))  # type: ignore[arg-type]

    assert _is_holiday(facts, _account(THIS_MONDAY)) is False


def test_the_summer_holidays_read_as_holidays_for_as_long_as_they_last() -> None:
    """Nothing fetched at all, because no week of the new year exists yet.

    Outside the school year the tier asks for nothing and publishes an empty
    snapshot. That is not "we have not looked" -- it is the correct answer for
    two months, and the entity has to give it rather than go unavailable.
    """
    empty = timetable()

    assert _is_holiday(empty, _account(dt.date(2026, 7, 20))) is True


def test_a_cancelled_lesson_is_not_evidence_of_school() -> None:
    """A week whose only entries are cancellations is still an empty week."""
    facts = timetable(
        *(
            a_lesson(
                id=f"CANCELED-{offset}",
                canceled=True,
                start=dt.datetime.combine(
                    THIS_MONDAY + dt.timedelta(days=offset),
                    dt.time(8, 0),
                    tzinfo=PARIS,
                ),
                end=dt.datetime.combine(
                    THIS_MONDAY + dt.timedelta(days=offset),
                    dt.time(9, 0),
                    tzinfo=PARIS,
                ),
            )
            for offset in range(5)
        )
    )

    assert _is_holiday(facts, _account(THIS_MONDAY)) is True


def test_the_attributes_say_which_clause_can_answer() -> None:
    """A card should not have to re-derive "no school this week" from the list."""
    attributes = _holiday_attributes(
        _facts(NEXT_MONDAY), _account(THIS_MONDAY + dt.timedelta(days=2))
    )

    assert attributes["inferred"] is True
    assert attributes["lookahead_days"] == 7
    assert attributes["current_week_without_lessons"] is True
    assert (
        attributes["next_lesson"]
        == dt.datetime.combine(NEXT_MONDAY, dt.time(8, 0), tzinfo=PARIS).isoformat()
    )


# ---------------------------------------------------------------------------
# What "in progress" publishes once something actually is
# ---------------------------------------------------------------------------


class _Clock:
    """The two things the attendance attribute functions ask of an account."""

    def __init__(self, moment: dt.datetime) -> None:
        self._moment = moment

    def now(self) -> dt.datetime:
        """The instant the entity is being evaluated at."""
        return self._moment

    def today(self) -> dt.date:
        """The day that instant falls on, in the establishment's zone."""
        return self._moment.date()


def _absence(
    *, start: dt.datetime, end: dt.datetime, justified: bool = False
) -> Absence:
    """One absence, with the fields upstream really carries."""
    return Absence(
        id="ABSENCE-1",
        from_date=start,
        to_date=end,
        justified=justified,
        hours="2h00",
        days=0,
        reasons=("Maladie",),
    )


def test_an_absence_in_progress_publishes_its_own_dates_and_not_the_days() -> None:
    """The attribute block exists so a card can say *which* absence.

    The state answers "is my child absent right now", which is what an
    automation triggers on. A dashboard also has to name the absence, and the
    only honest source for that is the absence the clause actually matched --
    picking the first of the list would show yesterday's on a card while the
    state is about today's.
    """
    from custom_components.carnet_scolaire.binary_sensor import _absence_attributes

    now = dt.datetime(2026, 3, 12, 10, 0, tzinfo=PARIS)
    facts = AttendanceFacts(
        period_id="P1",
        period_index=1,
        absences=(
            _absence(
                start=dt.datetime(2026, 3, 2, 8, 0, tzinfo=PARIS),
                end=dt.datetime(2026, 3, 2, 17, 0, tzinfo=PARIS),
            ),
            _absence(
                start=dt.datetime(2026, 3, 12, 8, 0, tzinfo=PARIS),
                end=dt.datetime(2026, 3, 12, 17, 0, tzinfo=PARIS),
                justified=True,
            ),
        ),
        delays=(),
        punishments=(),
    )

    attributes = _absence_attributes(facts, _Clock(now))  # type: ignore[arg-type]

    assert attributes["from_date"] == "2026-03-12T08:00:00+01:00"
    assert attributes["justified"] is True
    assert attributes["reasons"] == ["Maladie"]


def test_no_absence_in_progress_publishes_nothing_rather_than_the_next_one() -> None:
    """An empty block is the honest answer when the state is off.

    Publishing the *next* absence here would make a card read "absent" with a
    future date while the state says otherwise -- and the two are read
    together.
    """
    from custom_components.carnet_scolaire.binary_sensor import _absence_attributes

    now = dt.datetime(2026, 3, 12, 10, 0, tzinfo=PARIS)
    facts = AttendanceFacts(
        period_id="P1",
        period_index=1,
        absences=(
            _absence(
                start=dt.datetime(2026, 3, 20, 8, 0, tzinfo=PARIS),
                end=dt.datetime(2026, 3, 20, 17, 0, tzinfo=PARIS),
            ),
        ),
        delays=(),
        punishments=(),
    )

    assert _absence_attributes(facts, _Clock(now)) == {}  # type: ignore[arg-type]


def test_the_next_punishment_slot_is_the_earliest_still_ahead() -> None:
    """A punishment has several slots, and only the next one matters.

    ``min`` over the slots still ahead, not the first of the list: PRONOTE
    returns a punishment's schedule in its own order, and a card showing a
    detention that has already been served is worse than showing none.
    """
    from custom_components.carnet_scolaire.binary_sensor import _punishment_attributes

    now = dt.datetime(2026, 3, 12, 10, 0, tzinfo=PARIS)
    facts = AttendanceFacts(
        period_id="P1",
        period_index=1,
        absences=(),
        delays=(),
        punishments=(
            Punishment(
                id="PUNISHMENT-1",
                nature="Retenue",
                reasons=("Travail non fait",),
                giver=None,
                given_at=None,
                exclusion=False,
                during_lesson=False,
                homework=None,
                schedule=(
                    PunishmentSlot(
                        start=dt.datetime(2026, 3, 19, 13, 0, tzinfo=PARIS),
                        duration_minutes=60,
                    ),
                    PunishmentSlot(
                        start=dt.datetime(2026, 3, 5, 13, 0, tzinfo=PARIS),
                        duration_minutes=60,
                    ),
                    PunishmentSlot(
                        start=dt.datetime(2026, 3, 13, 13, 0, tzinfo=PARIS),
                        duration_minutes=30,
                    ),
                ),
            ),
        ),
    )

    attributes = _punishment_attributes(facts, _Clock(now))  # type: ignore[arg-type]

    assert attributes["start"] == "2026-03-13T13:00:00+01:00"
    assert attributes["duration"] == 30
    assert attributes["nature"] == "Retenue"
    assert attributes["exclusion"] is False


def test_a_punishment_wholly_in_the_past_publishes_nothing() -> None:
    """Served is not pending, and the block must not resurrect it."""
    from custom_components.carnet_scolaire.binary_sensor import _punishment_attributes

    now = dt.datetime(2026, 3, 12, 10, 0, tzinfo=PARIS)
    facts = AttendanceFacts(
        period_id="P1",
        period_index=1,
        absences=(),
        delays=(),
        punishments=(
            Punishment(
                id="PUNISHMENT-1",
                nature="Retenue",
                reasons=(),
                giver=None,
                given_at=None,
                exclusion=False,
                during_lesson=False,
                homework=None,
                schedule=(
                    PunishmentSlot(
                        start=dt.datetime(2026, 3, 5, 13, 0, tzinfo=PARIS),
                        duration_minutes=60,
                    ),
                ),
            ),
        ),
    )

    assert _punishment_attributes(facts, _Clock(now)) == {}  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# The platform, and the clock the yes/no facts are re-read on
# ---------------------------------------------------------------------------


@REQUIRES_HASS
class TestWhatThePlatformBuilds:
    """Two separate questions, asked in the right order and both asked."""

    @staticmethod
    def _keys(built: list[object]) -> set[str]:
        return {
            entity.entity_description.key  # type: ignore[attr-defined]
            for entity in built
            if hasattr(entity, "entity_description")
        }

    async def test_a_source_without_a_tier_gets_none_of_its_sensors(
        self,
        hass: HomeAssistant,
        mock_entry: MockConfigEntry,
        account: PronoteAccount,
    ) -> None:
        """§8.3: a flag its source cannot feed must not be created.

        Six of the nine binary sensors read the timetable and two read
        attendance. Built against a source that publishes neither, they would
        sit ``unavailable`` for ever -- and an unavailable entity breaks the
        automations pointing at it rather than merely looking empty (§2.5).
        """
        built: list[object] = []

        def collect(entities: object, *_args: object, **_kwargs: object) -> None:
            built.extend(entities)  # type: ignore[arg-type]

        narrow = ConnectorCapabilities(
            source=Source.PRONOTE,
            tiers=frozenset({Tier.SESSION, Tier.HOMEWORK}),
            writes=frozenset(),
            services=frozenset(),
        )
        connector = account.connector
        original = type(connector).CAPABILITIES
        try:
            type(connector).CAPABILITIES = narrow  # type: ignore[misc]
            await binary_sensor.async_setup_entry(
                hass,
                mock_entry,  # type: ignore[arg-type]
                collect,  # type: ignore[arg-type]
            )
        finally:
            type(connector).CAPABILITIES = original  # type: ignore[misc]

        assert self._keys(built) == {"homework_overdue"}

    async def test_a_tier_with_no_coordinator_gets_no_sensor_either(
        self,
        hass: HomeAssistant,
        mock_entry: MockConfigEntry,
        account: PronoteAccount,
    ) -> None:
        """Announced by the source is not the same as enabled on this entry.

        A tier can be a capability and still have no coordinator -- switched
        off in the options is the ordinary way. Reading the capability alone
        would hand the entity a ``None`` coordinator, and the ``AttributeError``
        would come out of the platform forward: the whole entry fails to load
        over one flag that should simply not exist.
        """
        built: list[object] = []

        def collect(entities: object, *_args: object, **_kwargs: object) -> None:
            built.extend(entities)  # type: ignore[arg-type]

        removed = account.coordinators.pop(Tier.ATTENDANCE)
        try:
            await binary_sensor.async_setup_entry(
                hass,
                mock_entry,  # type: ignore[arg-type]
                collect,  # type: ignore[arg-type]
            )
        finally:
            account.coordinators[Tier.ATTENDANCE] = removed

        keys = self._keys(built)
        assert "absence_in_progress" not in keys
        assert "punishment_upcoming" not in keys
        assert "school_day" in keys


@REQUIRES_HASS
class TestTheClockTheseFlagsAreReReadOn:
    """A yes/no fact about *now* is wrong the moment the boundary passes."""

    @staticmethod
    def _sensor(
        account: PronoteAccount,
        key: str,
        coordinator: PronoteTierCoordinator | None = None,
    ) -> binary_sensor.PronoteBinarySensor:
        description = next(
            item for item in binary_sensor.BINARY_SENSORS if item.key == key
        )
        return binary_sensor.PronoteBinarySensor(
            account,
            coordinator
            if coordinator is not None
            else account.coordinators[description.tier],
            account.students[0],
            description,
        )

    async def test_before_any_collection_the_answer_is_unknown_and_not_no(
        self,
        hass: HomeAssistant,
        mock_entry: MockConfigEntry,
        account: PronoteAccount,
    ) -> None:
        """``None``, never ``False``: "no data" is not "no school".

        An automation reading "not in class" would fire during set-up, before
        the first collection, if the absence of data were answered with a
        ``False`` -- and the ``holidays`` flag answering ``False`` on an empty
        snapshot would say term time on the second of August. ``None`` renders
        as ``unknown``, which no state trigger on ``off`` matches.
        """
        empty = PronoteTierCoordinator(hass, mock_entry, Tier.TIMETABLE)  # type: ignore[arg-type]
        sensor = self._sensor(account, "in_class", empty)

        assert sensor.is_on is None
        assert "fetched_at" not in sensor.extra_state_attributes

    async def test_a_boundary_already_behind_us_is_not_scheduled(
        self,
        hass: HomeAssistant,
        account: PronoteAccount,
    ) -> None:
        """A point in the past fires at once, and this one re-arms itself.

        ``async_track_point_in_time`` called with a moment already gone runs
        its callback immediately; the callback ends by arming the next
        transition, which would be computed from the same unchanged snapshot
        and be in the past again. That is not a slow loop -- it is a spin
        inside the event loop. And the case is reachable: the boundary is
        computed from a snapshot, which outlives the day it was collected on.
        """
        del hass
        from dataclasses import replace

        sensor = self._sensor(account, "in_class")
        sensor.entity_description = replace(
            sensor.entity_description,
            transition_fn=lambda _facts, acc: acc.now() - dt.timedelta(minutes=1),
        )

        sensor._arm()

        assert sensor._clock_unsub is None, (
            "a transition in the past was armed, which fires immediately"
        )

    async def test_the_flag_is_rewritten_when_its_own_boundary_passes(
        self,
        hass: HomeAssistant,
        account: PronoteAccount,
        parent_client: FakeClient,
        school_day: FrozenDateTimeFactory,
    ) -> None:
        """The state changes with no new data, which is the whole point.

        "In class" depends on the time and not on the timetable: between the
        end of one lesson and the start of the next, nothing is collected and
        the answer still has to change. Without the armed transition the flag
        would be up to a full tier interval late -- fifteen minutes, which is
        most of a break.

        The master tick is unsubscribed before the clock moves, and that is
        not tidiness. A collection republishes the tier, which re-arms every
        clock-driven entity from scratch: the first draft of this test let the
        tick run, watched the state be rewritten by that collection, and
        passed -- while the transition it claimed to exercise had been
        cancelled before it could fire. Asserting that PRONOTE was not asked
        is what closes that hole: the rewrite can then only have come from the
        clock.
        """
        entity_id = "binary_sensor.enfant_un_in_class"
        before = hass.states.get(entity_id)
        assert before is not None, "no in-class flag"

        facts = account.snapshot(Tier.TIMETABLE, CHILDREN[0][0])
        assert facts is not None
        moment = binary_sensor._in_class_transition(facts.data, account)
        assert moment is not None, "the fixture day has no boundary left to cross"

        # Unsubscribed rather than patched: `async_track_time_interval` took a
        # bound method at set-up, so replacing the attribute afterwards leaves
        # the registered callback exactly where it was.
        unsub = account._unsub_tick
        assert unsub is not None
        unsub()
        account._unsub_tick = None

        posts = len(parent_client.posts)
        ahead = moment + dt.timedelta(seconds=2) - account.now()
        school_day.tick(ahead)
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()

        after = hass.states.get(entity_id)
        assert after is not None
        assert after.last_reported > before.last_reported, (
            "the boundary passed and the flag was never re-read"
        )
        assert len(parent_client.posts) == posts, (
            "the rewrite came from a collection, not from the armed transition"
        )
