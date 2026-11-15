"""The child's profile photo.

Read through the gateway, under the session lock and after ``set_child``, and
never as a property. ``ClientInfo.profile_picture`` goes through
``ClientInfo._cache()``, which posts directly on ``communication`` and so
bypasses both ``ClientBase.post`` and the parent account's ``membre``
signature: read as a property from an entity, a parent account can render the
wrong child's face (annexe A §5.4).
"""

from typing import TYPE_CHECKING, Any

from homeassistant.components.image import ImageEntity

from .const import Priority, Tier
from .entity import PronoteEntity, async_add_per_student
from .ratelimit import TierDeferred

if TYPE_CHECKING:
    from collections.abc import Iterator

    from homeassistant.core import HomeAssistant
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


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PronoteConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create a photo entity for every child that has one."""
    account = entry.runtime_data
    if Tier.STATIC not in account.connector.capabilities.tiers:
        return
    coordinator = account.coordinators.get(Tier.STATIC)
    if coordinator is None:
        return

    def _build(student: Student) -> Iterator[ImageEntity]:
        # A child with no picture yields nothing and still counts as served.
        if student.has_photo:
            yield PronoteProfileImage(hass, account, coordinator, student)

    async_add_per_student(entry, account, async_add_entities, _build)


class PronoteProfileImage(PronoteEntity, ImageEntity):
    """The profile photo, fetched once and then cached.

    Fetched lazily on the first read rather than during set-up: a photo that
    changes once a year has no business costing a request at every start, and
    the ``static`` tier's daily cadence is already generous for it.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        account: PronoteAccount,
        coordinator: PronoteTierCoordinator,
        student: Student,
    ) -> None:
        PronoteEntity.__init__(self, account, coordinator, student, "photo")
        ImageEntity.__init__(self, hass)
        self._cached: bytes | None = None
        self._attempted = False

    @property
    def available(self) -> bool:
        """Available as soon as PRONOTE says the child has a photo."""
        return True

    async def async_image(self) -> bytes | None:
        """Return the photo, fetching it at most once per reload.

        A single attempt on purpose: an establishment that publishes no photo
        makes this return ``None`` for good, and retrying on every dashboard
        render would spend the daily budget on a picture that does not exist.
        """
        if self._cached is not None or self._attempted:
            return self._cached
        self._attempted = True

        extras = self.account.extras
        if extras is None:
            return None

        def work(client: Any) -> tuple[bytes | None, int]:
            return extras.gateway.profile_picture(client)

        try:
            data, _cost = await extras.session.run(
                str(Tier.STATIC),
                Priority.LOW,
                work,
                student_id=self.student.id,
                cost=1,
            )
        except TierDeferred:
            # Postponed, not failed: allow another attempt later rather than
            # caching "no photo" because the budget happened to be tight.
            self._attempted = False
            return None
        except Exception:  # noqa: BLE001 -- a missing photo must not break a card
            return None

        cached: bytes | None = data
        self._cached = cached
        return cached
