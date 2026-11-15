"""Shared entity base: device, availability, staleness, translation.

Two behaviours are load-bearing here.

**Staleness rather than unavailability (§5.4, §2.5).** An entity becomes
``unavailable`` only if it never had data, or if the last snapshot has aged past
``stale_after`` times its tier's interval. In between it keeps its value and
flags its age. A ``numeric_state`` trigger on an entity that goes
``unavailable`` and comes back fires spuriously, so keeping the last known value
and marking it dated is safer than admitting ignorance every ten minutes.

**Clock-driven re-evaluation (annexe A §3.1).** Entities whose state depends on
the *time* and not only on the data -- "in class", "absent right now", "next
lesson", "wake-up" -- schedule a point-in-time callback at their next known
transition. Without it they change state at their tier's pace, up to fifteen
minutes late, which on a wake-up sensor removes the entire point of the sensor.
"""

from typing import TYPE_CHECKING, Any

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.event import async_track_point_in_time
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, UNRECORDED_LIST_ATTRIBUTES, Tier

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from datetime import datetime

    from homeassistant.core import CALLBACK_TYPE
    from homeassistant.helpers.entity import Entity
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from . import PronoteConfigEntry
    from .account import PronoteAccount
    from .coordinator import PronoteTierCoordinator, TierData
    from .models import Snapshot, Student


class PronoteEntity(CoordinatorEntity["PronoteTierCoordinator"]):
    """Base for every entity attached to a child's device."""

    _attr_has_entity_name = True
    # Every list attribute is unrecorded: PRONOTE lists blow past the
    # recorder's 16 KiB attribute limit, and the history worth keeping is the
    # count, not the payload (§9).
    _unrecorded_attributes = frozenset({*UNRECORDED_LIST_ATTRIBUTES, "fetched_at"})

    def __init__(
        self,
        account: PronoteAccount,
        coordinator: PronoteTierCoordinator,
        student: Student,
        key: str,
    ) -> None:
        super().__init__(coordinator)
        self.account = account
        self._student = student
        self._key = key

        # Config entry, then the child's minted key, then a stable functional
        # key -- never a label, a period name or a rank. An automation that
        # breaks in September because a sensor was renamed is a regression even
        # if no code failed (§2.4).
        #
        # The entry id is part of it because the entry's own `unique_id` is the
        # *account*, so two entries following the same child are legitimate: a
        # mother's account and a father's, or a parent account alongside the
        # child's own. Without that prefix the registry does not reject the
        # second entry -- it *re-points* the existing entity at it, moving the
        # entity to the other entry's device and silently breaking every
        # automation that referenced it.
        #
        # The middle part is `account.stable_key(...)` and **not** `student.id`.
        # That was the defect: `student.id` is PRONOTE's `46#<signature>`, and
        # the signature is not stable between sessions. When it rotated, every
        # `unique_id` here moved with it, so Home Assistant saw a new child --
        # a second device, a second full set of entities, and the previous set
        # orphaned in the registry for ever, with every dashboard and
        # automation still pointing at the dead one. See `child_keys.py`.
        self._child_key = account.stable_key(student.id)
        self._attr_unique_id = f"{account.entry.entry_id}_{self._child_key}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = _student_device(account, student)

    @property
    def student(self) -> Student:
        """The child as the account knows it *now*, found by its minted key.

        Not the object the entity was built with: PRONOTE renames a child at
        every login, and an entity that kept the identifier it was created
        under read its snapshots from a key nothing published to any more --
        its data froze at the last collection before the re-login -- and
        every write it placed asked for a child the session no longer had.
        On a live instance that was the first homework tick after the list
        became writable: "no child with id".
        """
        current = self.account.student_for_key(self._child_key)
        if current is not None:
            self._student = current
        return self._student

    @property
    def tier(self) -> Tier:
        """The tier feeding this entity."""
        return self.coordinator.tier

    @property
    def snapshot(self) -> Snapshot[Any] | None:
        """The latest successful snapshot for this student and tier."""
        return self.coordinator.snapshot_for(self.student.id)

    @property
    def facts(self) -> Any | None:
        """The payload of the latest snapshot, or ``None``."""
        snapshot = self.snapshot
        return snapshot.data if snapshot else None

    @property
    def available(self) -> bool:
        """Unavailable only when there is no data, or the data is too old.

        Note what is *not* consulted: ``coordinator.last_update_success``. A
        failed collection leaves the previous snapshot in place on purpose, and
        letting a transient failure flip availability is exactly the spurious
        trigger §2.5 exists to prevent.
        """
        if self.snapshot is None:
            return False
        return not self.account.is_stale(self.tier)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Age of the data, and whether it is considered stale."""
        snapshot = self.snapshot
        if snapshot is None:
            return {}
        return {
            "fetched_at": snapshot.fetched_at.isoformat(),
            "stale": self.account.is_stale(self.tier),
        }


class PronoteAccountEntity(CoordinatorEntity["PronoteTierCoordinator"]):
    """Base for diagnostic entities attached to the *account* device.

    The budget is shared between the children of one account, because the server
    sees one session and one IP address (§7.1), so these belong to the account
    and not to a child.
    """

    _attr_has_entity_name = True
    # A sibling of `PronoteEntity`, not a subclass, so this has to be repeated
    # rather than inherited -- and it was not. The diagnostic sensors carry
    # `by_tier` (a mapping) and `tiers_due` (a list), which the recorder was
    # storing on every state write despite both being named in
    # `UNRECORDED_LIST_ATTRIBUTES`.
    _unrecorded_attributes = frozenset({*UNRECORDED_LIST_ATTRIBUTES, "fetched_at"})

    def __init__(
        self,
        account: PronoteAccount,
        coordinator: PronoteTierCoordinator,
        key: str,
    ) -> None:
        super().__init__(coordinator)
        self.account = account
        self._attr_unique_id = f"{account.entry.entry_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = _account_device(account)

    @property
    def available(self) -> bool:
        """Diagnostics are always available: they describe the integration."""
        return True


class LocallyPolledMixin:
    """Makes a coordinator entity poll, for readings that cost nothing to take.

    ``_attr_should_poll = True`` on a :class:`CoordinatorEntity` does nothing.
    ``BaseCoordinatorEntity`` declares ``should_poll`` as a ``cached_property``
    returning ``False``, and a property beats the ``_attr_`` fallback it shadows
    -- so the attribute was inert, and the diagnostic entities carrying it never
    polled at all. They were refreshed only when the session tier happened to
    collect, which is once every session cadence and not at all while a tier is
    failing. That is how ``sensor.<account>_prochaine_collecte`` came to publish
    an instant forty minutes old, and how the "throttled" flag -- the entity
    whose whole job is to explain a quiet integration (§6.6) -- could stay ``on``
    after the limiter had recovered.

    Two overrides are needed together and neither works alone. ``should_poll``
    has to be a property, and ``async_update`` has to stop being
    ``CoordinatorEntity``'s: that one calls ``async_request_refresh``, so
    polling would have turned a free local reading into a server request every
    thirty seconds -- the exact thing the budget exists to prevent (§7.1).

    Only for entities whose value is computed from objects already in memory:
    the limiter's counters, the scheduler's deadlines, the session's age. Any
    entity whose value comes from a collection must stay coordinator-driven.
    """

    @property
    def should_poll(self) -> bool:
        """Yes -- and see the class docstring for why this is a property."""
        return True

    async def async_update(self) -> None:
        """Nothing to fetch.

        The poll exists to re-evaluate the properties, which read in-memory
        state. Home Assistant writes the new state after this returns, so an
        empty body is the whole update. Overriding it is load-bearing rather
        than tidy: the inherited implementation would place a request.
        """


class ClockDrivenMixin:
    """Re-evaluates an entity at a wall-clock instant rather than on a poll.

    ``async_track_point_in_time`` rather than a scan interval, because the
    transition is known in advance: a lesson ends at a specific minute, an
    absence covers a specific window. Polling would either be wasteful or late,
    and late is what makes a wake-up sensor useless.
    """

    hass: Any
    _clock_unsub: CALLBACK_TYPE | None = None

    def _schedule_next_transition(
        self,
        moment: datetime | None,
        callback_fn: Callable[[datetime], None],
    ) -> None:
        """Arm a single callback at ``moment``, replacing any pending one."""
        self._cancel_transition()
        if moment is None:
            return
        self._clock_unsub = async_track_point_in_time(self.hass, callback_fn, moment)

    def _cancel_transition(self) -> None:
        """Cancel a pending transition callback."""
        if self._clock_unsub is not None:
            self._clock_unsub()
            self._clock_unsub = None


def _student_device(account: PronoteAccount, student: Student) -> DeviceInfo:
    """The device carrying one child's entities.

    A device per child under a parent device carrying the establishment: that is
    what makes the device triggers of §2.3 readable, because "when a lesson is
    cancelled" has to be asked about *somebody*.

    The parent is named by ``via_device_id`` -- a registry id -- and not by the
    ``via_device`` identifier tuple this used to pass. That is not a style
    change: on Home Assistant 2026.9 the tuple form is rejected outright, and
    because the platforms build their devices from this mapping, the rejection
    surfaced as ``RuntimeError`` while *adding each entity*. Six of the seven
    platforms failed -- every entity attached to a child -- and only the account
    entities, which declare no parent, survived. A device link is therefore
    load-bearing for the whole integration, which is why it is spelled out here.
    """
    # The minted key, for the same reason as the entity `unique_id`: keyed on
    # `student.id` this device was re-created from scratch every time PRONOTE
    # rotated the child's resource signature, stranding the previous one and
    # everything attached to it.
    info = DeviceInfo(
        identifiers={
            (DOMAIN, f"{account.entry.entry_id}_{account.stable_key(student.id)}")
        },
        name=student.name,
        manufacturer="PRONOTE",
        model=student.class_name or None,
    )
    if account.account_device_id is not None:
        # Omitted rather than passed as `None` when the parent is not known:
        # a flat device tree is a cosmetic loss, whereas a rejected key costs
        # every entity on this device.
        info["via_device_id"] = account.account_device_id
    return info


def _account_device(account: PronoteAccount) -> DeviceInfo:
    """The parent device carrying the establishment and the diagnostics."""
    return DeviceInfo(
        identifiers={(DOMAIN, account.entry.entry_id)},
        name=account.establishment_name,
        manufacturer="PRONOTE",
        model="Account",
        entry_type=None,
    )


def student_data(coordinator: PronoteTierCoordinator) -> TierData:
    """The coordinator's data, or an empty mapping."""
    return coordinator.data or {}


@callback
def async_add_per_student(
    entry: PronoteConfigEntry,
    account: PronoteAccount,
    async_add_entities: AddConfigEntryEntitiesCallback,
    build: Callable[[Student], Iterable[Entity]],
) -> None:
    """Create a platform's entities for every child, now and later.

    The seven platforms all used to iterate ``account.students`` once inside
    ``async_setup_entry`` and let ``async_add_entities`` go out of scope, so a
    pupil enrolled on a parent account mid-year had no entities until somebody
    reloaded the entry -- and reloading drops every snapshot the account
    holds. This keeps the callback alive and re-runs the difference whenever
    the ``SESSION`` coordinator publishes, which is what
    ``PronoteAccount._async_load_session_facts`` does for a newcomer.

    The difference is taken on the **minted key**, never on the PRONOTE
    resource identifier: that identifier rotates between sessions, so a diff
    against it would read a rotation as an arrival and build a second device
    for a child that already has one -- the defect ``child_keys`` exists to
    prevent.

    ``build`` may legitimately yield nothing for a child, as the photo
    platform does for one with no picture. The child still counts as served,
    or every publication would ask again.
    """
    known: set[str] = set()

    @callback
    def _add_missing() -> None:
        arrived = [
            student
            for student in account.students
            if account.stable_key(student.id) not in known
        ]
        if not arrived:
            return
        known.update(account.stable_key(student.id) for student in arrived)
        entities = [entity for student in arrived for entity in build(student)]
        if entities:
            async_add_entities(entities)

    _add_missing()
    session = account.coordinators.get(Tier.SESSION)
    if session is not None:
        entry.async_on_unload(session.async_add_listener(_add_missing))
