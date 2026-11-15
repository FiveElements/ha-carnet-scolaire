"""Calendars: the timetable, the homework deadlines and the detentions.

The timetable calendar is where the de-duplication of §2.2.1 becomes visible.
PRONOTE returns the original lesson *and* its replacement when a slot changes,
so without the gateway keeping only the highest ``num`` per ``(date, place,
subject)`` slot the calendar shows the cancelled 10:00 maths lesson next to the
one that replaced it -- and every "am I in class" question answers twice.

``uid`` is the PRONOTE identifier, so a calendar card that re-reads a range
recognises the same event rather than duplicating it.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any, Final

from homeassistant.components.calendar import CalendarEntity, CalendarEvent

from .account import PronoteAccount
from .const import Tier
from .entity import PronoteEntity, async_add_per_student

if TYPE_CHECKING:
    from collections.abc import Iterator

    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from . import PronoteConfigEntry
    from .coordinator import PronoteTierCoordinator
    from .models import Student

#: Nothing on this platform fetches. Entities read a snapshot the scheduler has
#: already published, so there is no update to serialise and no ceiling to set.
#: Zero states that, rather than leaving a reader to infer it from the absence
#: of an ``async_update``.
PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class PronoteCalendarDescription:
    """A calendar, its tier, and how to turn facts into events."""

    key: str
    tier: Tier
    events_fn: Callable[[Any, PronoteAccount], list[CalendarEvent]]


def _lesson_events(facts: Any, _account: PronoteAccount) -> list[CalendarEvent]:
    """One event per de-duplicated lesson.

    Cancelled lessons are kept and marked, not dropped: a parent looking at
    Thursday wants to see that the 14:00 lesson is cancelled, which is exactly
    the information a filtered calendar would hide.
    """
    events: list[CalendarEvent] = []
    for lesson in facts.lessons:
        prefix = "❌ " if lesson.canceled else ""
        description_parts = [
            part
            for part in (
                lesson.status,
                ", ".join(lesson.teachers) or None,
                lesson.memo,
            )
            if part
        ]
        events.append(
            CalendarEvent(
                start=lesson.start,
                end=lesson.end,
                summary=f"{prefix}{lesson.subject or '?'}",
                description="\n".join(description_parts) or None,
                location=lesson.classroom,
                uid=lesson.id,
            )
        )
    return events


def _homework_events(facts: Any, _account: PronoteAccount) -> list[CalendarEvent]:
    """One all-day event per homework deadline.

    All-day because PRONOTE gives a due *date* and no time. Home Assistant's
    convention for an all-day event is an exclusive end, hence the extra day.
    """
    events: list[CalendarEvent] = []
    for item in facts.homework:
        mark = "✅ " if item.done else ""
        events.append(
            CalendarEvent(
                start=item.due,
                end=item.due + timedelta(days=1),
                summary=f"{mark}{item.subject or '?'}",
                description=item.description or None,
                uid=item.id,
            )
        )
    return events


def _punishment_events(facts: Any, _account: PronoteAccount) -> list[CalendarEvent]:
    """One event per scheduled detention slot.

    The *slots* and not the punishments: a punishment with three sessions is
    three appointments somebody has to drive the child to, and a single event
    spanning them would be wrong in both directions.
    """
    events: list[CalendarEvent] = []
    for punishment in facts.punishments:
        for index, slot in enumerate(punishment.schedule):
            events.append(
                CalendarEvent(
                    start=slot.start,
                    end=slot.start + timedelta(minutes=slot.duration_minutes),
                    summary=punishment.nature or "Punition",
                    description="\n".join(
                        part
                        for part in (
                            ", ".join(punishment.reasons) or None,
                            punishment.giver,
                            punishment.homework,
                        )
                        if part
                    )
                    or None,
                    uid=f"{punishment.id}_{index}",
                )
            )
    return events


CALENDARS: Final[tuple[PronoteCalendarDescription, ...]] = (
    PronoteCalendarDescription(
        key="timetable", tier=Tier.TIMETABLE, events_fn=_lesson_events
    ),
    PronoteCalendarDescription(
        key="homework", tier=Tier.HOMEWORK, events_fn=_homework_events
    ),
    PronoteCalendarDescription(
        key="punishments", tier=Tier.ATTENDANCE, events_fn=_punishment_events
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,  # noqa: ARG001 -- required by the platform contract
    entry: PronoteConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create the three calendars for every child."""
    account = entry.runtime_data
    supported = account.connector.capabilities.tiers

    def _build(student: Student) -> Iterator[CalendarEntity]:
        for description in CALENDARS:
            if description.tier not in supported:
                continue
            coordinator = account.coordinators.get(description.tier)
            if coordinator is None:
                continue
            yield PronoteCalendar(account, coordinator, student, description)

    async_add_per_student(entry, account, async_add_entities, _build)


class PronoteCalendar(PronoteEntity, CalendarEntity):
    """A calendar backed by one tier's snapshot.

    ``async_get_events`` filters what is already in memory and never reaches the
    network: a calendar card scrolling three months forward would otherwise
    place three months of requests, and the limiter would be right to refuse
    them (§5.1).
    """

    def __init__(
        self,
        account: PronoteAccount,
        coordinator: PronoteTierCoordinator,
        student: Student,
        description: PronoteCalendarDescription,
    ) -> None:
        super().__init__(account, coordinator, student, description.key)
        self._description = description

    def _events(self) -> list[CalendarEvent]:
        """Every event this calendar currently knows about."""
        facts = self.facts
        if facts is None:
            return []
        return self._description.events_fn(facts, self.account)

    @property
    def event(self) -> CalendarEvent | None:
        """The event in progress, else the next one to start."""
        now = self.account.now()
        events = self._events()

        current = [item for item in events if _starts_at(item) <= now < _ends_at(item)]
        if current:
            return min(current, key=_starts_at)

        upcoming = [item for item in events if _starts_at(item) > now]
        return min(upcoming, key=_starts_at) if upcoming else None

    async def async_get_events(
        self,
        hass: HomeAssistant,  # noqa: ARG002 -- required by the platform contract
        start_date: datetime,
        end_date: datetime,
    ) -> list[CalendarEvent]:
        """Events overlapping ``[start_date, end_date)``, from memory only."""
        return sorted(
            (
                item
                for item in self._events()
                if _starts_at(item) < end_date and _ends_at(item) > start_date
            ),
            key=_starts_at,
        )


def _starts_at(event: CalendarEvent) -> datetime:
    """An event's start as an aware datetime, all-day events included."""
    return _as_datetime(event.start)


def _ends_at(event: CalendarEvent) -> datetime:
    """An event's end as an aware datetime, all-day events included."""
    return _as_datetime(event.end)


def _as_datetime(value: datetime | date) -> datetime:
    """Widen an all-day ``date`` to midnight, keeping the comparison total.

    ``CalendarEvent`` accepts both, and comparing a ``date`` to a ``datetime``
    raises -- so a homework calendar next to a lesson calendar would crash the
    "next event" lookup without this.
    """
    if isinstance(value, datetime):
        return value
    from homeassistant.util import dt as dt_util  # noqa: PLC0415 -- local to the widen

    return datetime.combine(value, time.min, tzinfo=dt_util.get_default_time_zone())
