"""``event`` entities: what actually changed, as a trigger.

These exist because a state trigger cannot express "a *new* grade arrived". A
grade sensor counting 12 then 13 tells you the count moved; it does not tell
you which grade, in which subject, or with what coefficient. An ``event``
entity fires once per change and carries that change in its attributes, so the
automation reads ``trigger.event.data`` and needs no template (§2.3).

The central case is ``lesson_changed``. One entity declares four event types --
``lesson_canceled``, ``lesson_moved``, ``room_changed``, ``teacher_changed`` --
and fires **one event per changed aspect**, not one per changed lesson. A
lesson that is both moved and has its room changed produces two events, because
an automation asking "was a room changed?" must not have to inspect a payload
to find out (annexe A §4).

Delivery is deliberately indirect: the account fires a bus signal after the
snapshots are published, and these entities subscribe to it. That ordering is
the guarantee that an automation reacting to "a grade arrived" finds
``sensor.<student>_derniere_note`` already holding it (§5.1).
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from homeassistant.components.event import EventEntity, EventEntityDescription
from homeassistant.core import callback

from .account import SIGNAL_DELTA
from .const import (
    EVENT_ABSENCE_ADDED,
    EVENT_DELAY_ADDED,
    EVENT_EVALUATION_ADDED,
    EVENT_GRADE_ADDED,
    EVENT_HOMEWORK_ADDED,
    EVENT_INFORMATION_ADDED,
    EVENT_MESSAGE_RECEIVED,
    EVENT_PUNISHMENT_ADDED,
    LESSON_EVENT_TYPES,
    Tier,
)
from .entity import PronoteEntity, async_add_per_student

if TYPE_CHECKING:
    from collections.abc import Iterator

    from homeassistant.core import Event, HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from . import PronoteConfigEntry
    from .account import PronoteAccount
    from .coordinator import PronoteTierCoordinator
    from .models import Student

#: Nothing on this platform fetches. Entities read a snapshot the scheduler has
#: already published, so there is no update to serialise and no ceiling to set.
#: Zero states that, rather than leaving a reader to infer it from the absence
#: of an ``async_update``.
PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class PronoteEventDescription(EventEntityDescription):
    """An event entity, its tier and the event types it may fire."""

    tier: Tier


EVENTS: Final[tuple[PronoteEventDescription, ...]] = (
    PronoteEventDescription(
        key="new_grade",
        tier=Tier.MARKS,
        event_types=[EVENT_GRADE_ADDED],
    ),
    PronoteEventDescription(
        key="new_homework",
        tier=Tier.HOMEWORK,
        event_types=[EVENT_HOMEWORK_ADDED],
    ),
    # Four types on one entity, because they are four answers to the same
    # question -- "what happened to a lesson?" -- and an automation picks the
    # one it cares about through `trigger.event.data.event_type`.
    PronoteEventDescription(
        key="lesson_changed",
        tier=Tier.TIMETABLE,
        event_types=list(LESSON_EVENT_TYPES),
    ),
    PronoteEventDescription(
        key="new_information",
        tier=Tier.NEWS,
        event_types=[EVENT_INFORMATION_ADDED],
    ),
    PronoteEventDescription(
        key="new_absence",
        tier=Tier.ATTENDANCE,
        event_types=[EVENT_ABSENCE_ADDED],
    ),
    # Split out from `new_absence` in v2. An absence and a late arrival do not
    # carry the same payload -- `hours`/`days` against `minutes` -- and an
    # automation that wants one and not the other should not have to filter
    # (annexe A §4).
    PronoteEventDescription(
        key="new_delay",
        tier=Tier.ATTENDANCE,
        event_types=[EVENT_DELAY_ADDED],
    ),
    PronoteEventDescription(
        key="new_punishment",
        tier=Tier.ATTENDANCE,
        event_types=[EVENT_PUNISHMENT_ADDED],
    ),
    PronoteEventDescription(
        key="new_message",
        tier=Tier.DISCUSSIONS,
        event_types=[EVENT_MESSAGE_RECEIVED],
    ),
    PronoteEventDescription(
        key="new_evaluation",
        tier=Tier.EVALUATIONS,
        event_types=[EVENT_EVALUATION_ADDED],
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,  # noqa: ARG001 -- required by the platform contract
    entry: PronoteConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create the event entities for every child."""
    account = entry.runtime_data
    supported = account.connector.capabilities.tiers

    def _build(student: Student) -> Iterator[EventEntity]:
        for description in EVENTS:
            if description.tier not in supported:
                continue
            coordinator = account.coordinators.get(description.tier)
            if coordinator is None:
                continue
            yield PronoteEventEntity(account, coordinator, student, description)

    async_add_per_student(entry, account, async_add_entities, _build)


class PronoteEventEntity(PronoteEntity, EventEntity):
    """Fires once per detected change, carrying the change itself."""

    entity_description: PronoteEventDescription

    def __init__(
        self,
        account: PronoteAccount,
        coordinator: PronoteTierCoordinator,
        student: Student,
        description: PronoteEventDescription,
    ) -> None:
        super().__init__(account, coordinator, student, description.key)
        self.entity_description = description

    @property
    def available(self) -> bool:
        """Always available.

        An event entity holds no measurement to go stale, and letting it go
        unavailable between two collections would make its own trigger
        unreliable for no gain (§2.5).
        """
        return True

    async def async_added_to_hass(self) -> None:
        """Listen for the account's delta signal."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self.hass.bus.async_listen(SIGNAL_DELTA, self._async_handle_delta)
        )

    @callback
    def _async_handle_delta(self, event: Event) -> None:
        """Fire if this signal is for this entity, on this child.

        All three checks matter. The entry check keeps two accounts on one
        instance apart, the student check keeps two children of one parent
        account apart, and the key check keeps the nine entities of one child
        from all firing on every change.
        """
        data = event.data
        if data.get("entry_id") != self.account.entry.entry_id:
            return
        if data.get("student_id") != self.student.id:
            return
        if data.get("entity_key") != self.entity_description.key:
            return

        event_type = data.get("event_type")
        if not isinstance(event_type, str) or event_type not in (
            self.entity_description.event_types or ()
        ):
            # A detector emitting a type its entity never declared is an
            # integration bug, not a server quirk: dropping it silently would
            # hide a contract violation the tests are meant to catch.
            return

        attributes: dict[str, Any] = dict(data.get("attributes") or {})
        self._trigger_event(event_type, attributes)
        self.async_write_ha_state()
