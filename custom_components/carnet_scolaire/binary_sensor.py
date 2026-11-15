"""Binary sensors: the yes/no facts, re-evaluated when the clock says so.

Every sensor here answers a question with two answers, so a state trigger works
directly. What makes them non-trivial is that most of them depend on the *time*
and not only on the data: "in class" stops being true when the lesson ends,
"school day" flips at midnight, "absence in progress" is a window. Bound to
their tier's cadence they would be up to fifteen minutes wrong, so each one
declares its next transition instant and is re-evaluated there with
``async_track_point_in_time`` (annexe A §3.1).

Three "twins" the first draft had -- ``devoirs_a_faire``,
``actualites_non_lues`` and ``messages_non_lus`` -- are deliberately absent.
Each one only restated ``> 0`` of a numeric sensor that already exists, and a
``numeric_state`` trigger above zero says the same thing without a second
entity to keep consistent (annexe A §3.1).
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import TYPE_CHECKING, Any, Final

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import callback

from .account import PronoteAccount
from .connectors.protocol import Source
from .const import Tier
from .entity import (
    ClockDrivenMixin,
    LocallyPolledMixin,
    PronoteAccountEntity,
    PronoteEntity,
    async_add_per_student,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from . import PronoteConfigEntry
    from .coordinator import PronoteTierCoordinator
    from .models import AttendanceFacts, HomeworkFacts, Student, TimetableFacts

#: Nothing on this platform fetches. Entities read a snapshot the scheduler has
#: already published, so there is no update to serialise and no ceiling to set.
#: Zero states that, rather than leaving a reader to infer it from the absence
#: of an ``async_update``.
PARALLEL_UPDATES = 0

#: 30 seconds, for the one polled entity here: the limiter's "throttled" flag,
#: read straight out of memory. Polling it places no request, which is why a
#: sub-minute cadence is affordable on this platform and nowhere else.
#:
#: This used to claim it was "the cadence these entities already ran at", and
#: that was wrong: nothing polled, because ``should_poll`` on a coordinator
#: entity is a property and the ``_attr_`` beside it was never consulted. See
#: :class:`.entity.LocallyPolledMixin`, which is what makes the interval mean
#: anything -- and note what it cost here: the flag whose whole purpose is to
#: explain a quiet integration could itself go quiet.
SCAN_INTERVAL = timedelta(seconds=30)

#: How far ahead ``holidays`` looks for a lesson. Seven days rather than "the
#: active period", because PRONOTE publishes no holiday calendar: the only
#: evidence available is the absence of lessons (annexe A §3).
#:
#: Seven is also exactly what the timetable tier now fetches ahead at its
#: narrowest -- a Sunday, whose window is this week plus the next. The question
#: used to be wider than the data it was asked of, which is how a Saturday with
#: two days of lessons behind it answered "holidays".
HOLIDAY_LOOKAHEAD_DAYS: Final = 7


@dataclass(frozen=True, kw_only=True)
class PronoteBinarySensorDescription(BinarySensorEntityDescription):
    """A binary sensor, its tier, its predicate and its next transition."""

    tier: Tier
    value_fn: Callable[[Any, PronoteAccount], bool]
    #: When this sensor's answer could next change on its own. ``None`` means
    #: "only when the data changes".
    transition_fn: Callable[[Any, PronoteAccount], datetime | None] | None = None
    attributes_fn: Callable[[Any, PronoteAccount], dict[str, Any]] | None = None


# ---------------------------------------------------------------------------
# Transition helpers
# ---------------------------------------------------------------------------


def _next_midnight(account: PronoteAccount) -> datetime:
    """The next local midnight, in the establishment's timezone.

    Day-scoped sensors flip there and nowhere else, and the timezone matters:
    an establishment abroad and a Home Assistant set to the family's timezone
    would otherwise disagree about which day it is (§4.2).
    """
    now = account.now()
    tomorrow = now.date() + timedelta(days=1)
    return datetime.combine(tomorrow, time.min, tzinfo=now.tzinfo)


def _next_boundary(
    moments: list[datetime], now: datetime, fallback: datetime
) -> datetime:
    """The earliest moment still in the future, else ``fallback``."""
    future = [moment for moment in moments if moment > now]
    return min(future) if future else fallback


# ---------------------------------------------------------------------------
# Timetable predicates
# ---------------------------------------------------------------------------


def _today_lessons(facts: TimetableFacts, account: PronoteAccount) -> list[Any]:
    """Today's lessons, cancellations included."""
    today = account.today()
    return [lesson for lesson in facts.lessons if lesson.start.date() == today]


def _is_school_day(facts: TimetableFacts, account: PronoteAccount) -> bool:
    """At least one lesson today that is not cancelled."""
    return any(
        not lesson.canceled and not lesson.exempted
        for lesson in _today_lessons(facts, account)
    )


def _in_class(facts: TimetableFacts, account: PronoteAccount) -> bool:
    """The current instant falls inside a lesson the child attends."""
    now = account.now()
    return any(
        lesson.start <= now < lesson.end
        for lesson in facts.lessons
        if not lesson.canceled and not lesson.exempted
    )


def _in_class_transition(
    facts: TimetableFacts, account: PronoteAccount
) -> datetime | None:
    """The next lesson boundary -- a start or an end, whichever comes first."""
    now = account.now()
    boundaries = [
        moment
        for lesson in facts.lessons
        if not lesson.canceled and not lesson.exempted
        for moment in (lesson.start, lesson.end)
    ]
    return _next_boundary(boundaries, now, _next_midnight(account))


def _in_class_attributes(
    facts: TimetableFacts, account: PronoteAccount
) -> dict[str, Any]:
    """Which lesson the child is in."""
    now = account.now()
    current = next(
        (
            lesson
            for lesson in facts.lessons
            if not lesson.canceled
            and not lesson.exempted
            and lesson.start <= now < lesson.end
        ),
        None,
    )
    if current is None:
        return {}
    return {
        "subject": current.subject,
        "classroom": current.classroom,
        "teachers": list(current.teachers),
        "ends_at": current.end.isoformat(),
    }


def _has_canceled_lesson(facts: TimetableFacts, account: PronoteAccount) -> bool:
    """At least one lesson cancelled today."""
    return any(lesson.canceled for lesson in _today_lessons(facts, account))


def _canceled_attributes(
    facts: TimetableFacts, account: PronoteAccount
) -> dict[str, Any]:
    """Which lessons were cancelled today."""
    canceled = [lesson for lesson in _today_lessons(facts, account) if lesson.canceled]
    return {
        "count": len(canceled),
        "items": [
            {
                "subject": lesson.subject,
                "start": lesson.start.isoformat(),
                "status": lesson.status,
            }
            for lesson in canceled
        ],
    }


def _has_outing(facts: TimetableFacts, account: PronoteAccount) -> bool:
    """An educational outing is scheduled today."""
    return any(lesson.outing for lesson in _today_lessons(facts, account))


def _has_test(facts: TimetableFacts, account: PronoteAccount) -> bool:
    """A test is scheduled today.

    Answers "today" and only "today", which is why
    ``sensor.<student>_prochain_controle`` exists alongside it: revising the
    evening before needs a timestamp, not a flag (annexe A §1).
    """
    return any(lesson.test for lesson in _today_lessons(facts, account))


def _test_attributes(facts: TimetableFacts, account: PronoteAccount) -> dict[str, Any]:
    """Which subjects are tested today."""
    tests = [lesson for lesson in _today_lessons(facts, account) if lesson.test]
    return {
        "count": len(tests),
        "subjects": [lesson.subject for lesson in tests if lesson.subject],
    }


def _is_holiday(facts: TimetableFacts, account: PronoteAccount) -> bool:
    """No lesson in the seven days ahead, or none in the whole current week.

    Inferred, and it says so. PRONOTE exposes no holiday calendar, so the only
    evidence is the absence of lessons -- which also reads as ``on`` when the
    timetable simply has not been published yet. The ``inferred`` attribute is
    there so an automation can decide how much to trust it.

    The two clauses answer two different shapes of break, and either one alone
    gets a week of the year wrong.

    **Forward** is the break that is about to start: on the Saturday before the
    Toussaint holidays the week just gone is full and the week ahead is empty,
    and only looking ahead can see it. It needs the seven days to be *fetched*,
    which is why the timetable tier now always reaches into next week.

    **The current week** is the break that is already running. A holiday of two
    weeks -- Toussaint, Noël, février, Pâques -- or the two months of summer
    has a tail: from the Monday of its last week, "is there a lesson in the
    next seven days" finds the return and answers ``off`` while the child is
    still on holiday. Asking whether *this* week holds a lesson at all answers
    that correctly for every day of it, and does not fire on a pupil whose
    Monday happens to be free, because the rest of the week still has lessons.
    """
    today = account.today()
    monday = today - timedelta(days=today.weekday())
    dates = [lesson.start.date() for lesson in facts.lessons if not lesson.canceled]
    nothing_ahead = not any(
        today <= day <= today + timedelta(days=HOLIDAY_LOOKAHEAD_DAYS) for day in dates
    )
    nothing_this_week = not any(
        monday <= day <= monday + timedelta(days=6) for day in dates
    )
    return nothing_ahead or nothing_this_week


def _holiday_attributes(
    facts: TimetableFacts, account: PronoteAccount
) -> dict[str, Any]:
    """How the holiday answer was reached, and what it rests on."""
    upcoming = [
        lesson.start
        for lesson in facts.lessons
        if not lesson.canceled and lesson.start.date() >= account.today()
    ]
    today = account.today()
    monday = today - timedelta(days=today.weekday())
    return {
        "inferred": True,
        "lookahead_days": HOLIDAY_LOOKAHEAD_DAYS,
        "next_lesson": min(upcoming).isoformat() if upcoming else None,
        # Which of the two clauses can fire, so a card can say "holidays" and
        # "no school this week" without re-deriving either from the lesson
        # list -- and so a bug report shows which one answered.
        "current_week_without_lessons": not any(
            monday <= lesson.start.date() <= monday + timedelta(days=6)
            for lesson in facts.lessons
            if not lesson.canceled
        ),
        "weeks_fetched": list(facts.weeks_fetched),
    }


# ---------------------------------------------------------------------------
# Homework and attendance predicates
# ---------------------------------------------------------------------------


def _homework_overdue(facts: HomeworkFacts, account: PronoteAccount) -> bool:
    """An unfinished homework item is already past its deadline."""
    today = account.today()
    return any(not item.done and item.due < today for item in facts.homework)


def _overdue_attributes(
    facts: HomeworkFacts, account: PronoteAccount
) -> dict[str, Any]:
    """Which homework items are late."""
    today = account.today()
    late = [item for item in facts.homework if not item.done and item.due < today]
    return {
        "count": len(late),
        "items": [
            {
                "subject": item.subject,
                "due": item.due.isoformat(),
                "description": item.description,
            }
            for item in late
        ],
    }


def _absence_in_progress(facts: AttendanceFacts, account: PronoteAccount) -> bool:
    """An absence window covers the current instant."""
    now = account.now()
    return any(absence.from_date <= now < absence.to_date for absence in facts.absences)


def _absence_transition(
    facts: AttendanceFacts, account: PronoteAccount
) -> datetime | None:
    """The next absence-window boundary."""
    now = account.now()
    boundaries = [
        moment
        for absence in facts.absences
        for moment in (absence.from_date, absence.to_date)
    ]
    return _next_boundary(boundaries, now, _next_midnight(account))


def _absence_attributes(
    facts: AttendanceFacts, account: PronoteAccount
) -> dict[str, Any]:
    """The absence currently in progress."""
    now = account.now()
    current = next(
        (
            absence
            for absence in facts.absences
            if absence.from_date <= now < absence.to_date
        ),
        None,
    )
    if current is None:
        return {}
    return {
        "from_date": current.from_date.isoformat(),
        "to_date": current.to_date.isoformat(),
        "justified": current.justified,
        "reasons": list(current.reasons),
        "hours": current.hours,
        "days": current.days,
    }


def _punishment_upcoming(facts: AttendanceFacts, account: PronoteAccount) -> bool:
    """A punishment slot is still in the future."""
    now = account.now()
    return any(
        slot.start > now
        for punishment in facts.punishments
        for slot in punishment.schedule
    )


def _punishment_transition(
    facts: AttendanceFacts, account: PronoteAccount
) -> datetime | None:
    """The next punishment slot, after which the answer may change."""
    now = account.now()
    starts = [
        slot.start for punishment in facts.punishments for slot in punishment.schedule
    ]
    return _next_boundary(starts, now, _next_midnight(account))


def _punishment_attributes(
    facts: AttendanceFacts, account: PronoteAccount
) -> dict[str, Any]:
    """The next punishment slot."""
    now = account.now()
    candidates = [
        (slot, punishment)
        for punishment in facts.punishments
        for slot in punishment.schedule
        if slot.start > now
    ]
    if not candidates:
        return {}
    slot, punishment = min(candidates, key=lambda pair: pair[0].start)
    return {
        "start": slot.start.isoformat(),
        "duration": slot.duration_minutes,
        "nature": punishment.nature,
        "exclusion": punishment.exclusion,
    }


def _midnight(_facts: Any, account: PronoteAccount) -> datetime | None:
    """Day-scoped sensors change at midnight and nowhere else."""
    return _next_midnight(account)


BINARY_SENSORS: Final[tuple[PronoteBinarySensorDescription, ...]] = (
    PronoteBinarySensorDescription(
        key="school_day",
        tier=Tier.TIMETABLE,
        value_fn=_is_school_day,
        transition_fn=_midnight,
    ),
    PronoteBinarySensorDescription(
        key="in_class",
        tier=Tier.TIMETABLE,
        value_fn=_in_class,
        transition_fn=_in_class_transition,
        attributes_fn=_in_class_attributes,
    ),
    PronoteBinarySensorDescription(
        key="lessons_canceled",
        tier=Tier.TIMETABLE,
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=_has_canceled_lesson,
        transition_fn=_midnight,
        attributes_fn=_canceled_attributes,
    ),
    PronoteBinarySensorDescription(
        key="outing_today",
        tier=Tier.TIMETABLE,
        value_fn=_has_outing,
        transition_fn=_midnight,
    ),
    PronoteBinarySensorDescription(
        key="test_today",
        tier=Tier.TIMETABLE,
        value_fn=_has_test,
        transition_fn=_midnight,
        attributes_fn=_test_attributes,
    ),
    PronoteBinarySensorDescription(
        key="holidays",
        tier=Tier.TIMETABLE,
        value_fn=_is_holiday,
        transition_fn=_midnight,
        attributes_fn=_holiday_attributes,
    ),
    PronoteBinarySensorDescription(
        key="homework_overdue",
        tier=Tier.HOMEWORK,
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=_homework_overdue,
        transition_fn=_midnight,
        attributes_fn=_overdue_attributes,
    ),
    PronoteBinarySensorDescription(
        key="absence_in_progress",
        tier=Tier.ATTENDANCE,
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=_absence_in_progress,
        transition_fn=_absence_transition,
        attributes_fn=_absence_attributes,
    ),
    PronoteBinarySensorDescription(
        key="punishment_upcoming",
        tier=Tier.ATTENDANCE,
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=_punishment_upcoming,
        transition_fn=_punishment_transition,
        attributes_fn=_punishment_attributes,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,  # noqa: ARG001 -- required by the platform contract
    entry: PronoteConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create the per-child binary sensors, plus the account's throttle flag."""
    account = entry.runtime_data
    supported = account.connector.capabilities.tiers

    def _build(student: Student) -> Iterator[BinarySensorEntity]:
        for description in BINARY_SENSORS:
            if description.tier not in supported:
                continue
            if (
                account.connector.capabilities.source is Source.ECOLEDIRECTE
                and description.key in {"outing_today", "test_today", "holidays"}
            ):
                continue
            coordinator = account.coordinators.get(description.tier)
            if coordinator is None:
                continue
            yield PronoteBinarySensor(account, coordinator, student, description)

    # The throttle flag is the account's, not a child's: added once.
    async_add_entities(
        [
            PronoteThrottledBinarySensor(
                account, account.coordinators[Tier.SESSION], "throttled"
            )
        ]
    )
    async_add_per_student(entry, account, async_add_entities, _build)


class PronoteBinarySensor(ClockDrivenMixin, PronoteEntity, BinarySensorEntity):
    """A yes/no fact, re-evaluated at its own next transition."""

    entity_description: PronoteBinarySensorDescription

    def __init__(
        self,
        account: PronoteAccount,
        coordinator: PronoteTierCoordinator,
        student: Student,
        description: PronoteBinarySensorDescription,
    ) -> None:
        super().__init__(account, coordinator, student, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool | None:
        """The answer, or ``None`` while there is no data at all."""
        facts = self.facts
        if facts is None:
            return None
        return self.entity_description.value_fn(facts, self.account)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Context, plus the snapshot's age."""
        attributes = dict(super().extra_state_attributes)
        facts = self.facts
        if facts is not None and self.entity_description.attributes_fn is not None:
            attributes.update(
                self.entity_description.attributes_fn(facts, self.account)
            )
        return attributes

    async def async_added_to_hass(self) -> None:
        """Subscribe to the tier, then arm the first transition."""
        await super().async_added_to_hass()
        self._arm()

    async def async_will_remove_from_hass(self) -> None:
        """Cancel the pending transition."""
        self._cancel_transition()
        await super().async_will_remove_from_hass()

    @callback
    def _handle_coordinator_update(self) -> None:
        """New data can move the next transition."""
        super()._handle_coordinator_update()
        self._arm()

    @callback
    def _arm(self) -> None:
        """Schedule the next re-evaluation, if this sensor has one."""
        transition_fn = self.entity_description.transition_fn
        facts = self.facts
        if transition_fn is None or facts is None:
            self._cancel_transition()
            return
        moment = transition_fn(facts, self.account)
        if moment is None or moment <= self.account.now():
            self._cancel_transition()
            return
        # A second past the boundary, so the comparison that produced it has
        # actually flipped by the time it is re-read.
        self._schedule_next_transition(
            moment + timedelta(seconds=1), self._on_transition
        )

    @callback
    def _on_transition(self, _now: datetime) -> None:
        """Re-read, publish, re-arm."""
        self._clock_unsub = None
        self.async_write_ha_state()
        self._arm()


class PronoteThrottledBinarySensor(
    LocallyPolledMixin, PronoteAccountEntity, BinarySensorEntity
):
    """``on`` while the limiter is postponing collections.

    Attached to the account and not to a child: the budget is shared, because
    the server sees one session and one address (§7.1). This is the entity that
    makes a quiet integration explainable -- without it, "my sensors stopped
    updating" has no visible cause (§6.6).
    """

    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def is_on(self) -> bool:
        """Whether collections are currently being deferred."""
        return self.account.limiter.throttled

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Since when, and why."""
        limiter = self.account.limiter
        since = limiter.throttled_since
        return {
            "since": since.isoformat() if since else None,
            "state": str(limiter.state),
            "consecutive_failures": limiter.consecutive_failures,
        }
