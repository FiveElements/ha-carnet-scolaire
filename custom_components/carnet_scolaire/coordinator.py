"""One coordinator per tier, driven by the scheduler rather than by a timer.

``update_interval`` is ``None`` on purpose: the scheduler decides what is due
and calls ``async_set_updated_data()``. An entity subscribes only to its own
tier's coordinator, so collecting the canteen menus does not rewrite the
timetable's state (§5.3).

Data is keyed by student because one config entry is one *account*, and a parent
account holds several children who share a session, a budget and an IP address
(§7.1).
"""

import logging
from typing import TYPE_CHECKING, Any, Final

from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import DOMAIN, Tier

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from . import PronoteConfigEntry
    from .models import Snapshot

_LOGGER: Final = logging.getLogger(__name__)

#: A tier's payload for every student on the account.
TierData = dict[str, "Snapshot[Any]"]


class PronoteTierCoordinator(DataUpdateCoordinator[TierData]):
    """Holds one tier's latest successful snapshot, per student.

    There is no ``_async_update_data``: this coordinator is never allowed to
    fetch on its own. A coordinator that could would be a second path to the
    network, and annexe B §6 gives the limiter a monopoly on that.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: PronoteConfigEntry,
        tier: Tier,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}:{entry.entry_id[:8]}:{tier}",
            update_interval=None,
            config_entry=entry,
            always_update=False,
        )
        self.tier = tier
        self.data = {}

    def snapshot_for(self, student_id: str) -> Snapshot[Any] | None:
        """The latest snapshot for one student, or ``None``."""
        if not self.data:
            return None
        return self.data.get(student_id)

    def publish(self, student_id: str, snapshot: Snapshot[Any]) -> None:
        """Publish one student's snapshot, leaving the others untouched.

        A failed collection must never overwrite a good snapshot: entities keep
        their last known value and report its age instead (§4.4, §5.4). That is
        enforced simply by this being the only writer and only ever being called
        after a success.

        The identity check is the one runtime defence against the worst silent
        failure this integration has (§7.1). ``pronotepy``'s parent client sets
        ``_selected_child = self.children[0]`` in its constructor, so a code
        path that forgets ``set_child`` raises nothing at all -- it returns the
        *first* child's data. With one child that is undetectable; with two it
        publishes one child's timetable under the other child's entities, and
        the only detector left is a parent recognising the wrong lessons on
        their dashboard. Every snapshot is stamped with the child it was
        collected for, so the mismatch can be caught here instead.

        Raised rather than dropped: the collection loop turns an exception into
        a failed tier with a repair, which keeps the previous good snapshot and
        makes the fault visible. Silently skipping it would leave the entities
        stale with no explanation, which is the failure this check exists to
        remove.
        """
        if snapshot.student_id is not None and snapshot.student_id != student_id:
            message = (
                f"refusing a {snapshot.tier} snapshot collected for another "
                f"child: the collection loop asked for one child and the "
                f"client answered for another"
            )
            raise ValueError(message)

        merged: TierData = dict(self.data or {})
        merged[student_id] = snapshot
        self.async_set_updated_data(merged)

    def note_failure(self, error: Exception) -> None:
        """Record a failure without discarding the data or flooding the log.

        ``last_update_success`` flips so diagnostics can tell a stale snapshot
        from one that never arrived. Logging is not done here: one failed tick
        would otherwise emit one error per coordinator. The Silver rule
        ``log-when-unavailable`` lives on the session, once for the service,
        at INFO.
        """
        self.last_exception = error
        if self.last_update_success:
            self.last_update_success = False
            self.async_update_listeners()
