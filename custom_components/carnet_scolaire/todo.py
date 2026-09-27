"""A to-do list for the homework, with a real checkbox.

Ticking an item posts ``SaisieTAFFaitEleve`` to PRONOTE, so the establishment
sees it too. That is the only write in the whole integration reachable without
a service call, which is exactly why it is gated: with write operations off --
the default (§8.3) -- the list is read-only and says so through its supported
features rather than by failing when tapped.

It is read-only on a parent account too, whatever the option says. PRONOTE
answers a parent session's tick normally and records nothing (measured on
2026-09-27), so a checkbox offered there could only ever lie.

On a student account the tick is read back before it is reported: the server's
answer proves nothing, and a tick that did not land raises, so the card puts
the box back. The published snapshot is still refreshed by the tier rather
than from that check read.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.todo import (
    TodoItem,
    TodoItemStatus,
    TodoListEntity,
    TodoListEntityFeature,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from .const import DOMAIN, Priority, Tier
from .entity import PronoteEntity, async_add_per_student
from .gateway import ItemNotFound, WriteNotApplied
from .service_errors import async_run_gesture

if TYPE_CHECKING:
    from collections.abc import Iterator

    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from . import PronoteConfigEntry
    from .account import PronoteAccount
    from .coordinator import PronoteTierCoordinator
    from .models import Student

#: One at a time. Entities on this platform *act*: they place a real write
#: against the school's server. The limiter already serialises the wire under
#: its own lock, but declaring it at the platform level costs nothing and is
#: the layer Home Assistant itself honours -- zero here would let a script that
#: ticks off six homework items fire six writes at once.
PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,  # noqa: ARG001 -- required by the platform contract
    entry: PronoteConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create one homework list per child."""
    account = entry.runtime_data
    if Tier.HOMEWORK not in account.connector.capabilities.tiers:
        return
    coordinator = account.coordinators.get(Tier.HOMEWORK)
    if coordinator is None:
        return

    def _build(student: Student) -> Iterator[TodoListEntity]:
        yield PronoteHomeworkTodoList(account, coordinator, student)

    async_add_per_student(entry, account, async_add_entities, _build)


class PronoteHomeworkTodoList(PronoteEntity, TodoListEntity):
    """The child's homework, as a list that can be ticked."""

    def __init__(
        self,
        account: PronoteAccount,
        coordinator: PronoteTierCoordinator,
        student: Student,
    ) -> None:
        super().__init__(account, coordinator, student, "homework")
        # Declared from the option rather than always: a checkbox that appears
        # tappable and then refuses is worse than one that is visibly
        # read-only. Changing the option reloads the entry, so this is
        # re-evaluated (§7.3). The account's shape is known here too: the
        # platforms are forwarded only after the first login.
        #
        # The due date and the description are declared too, although neither
        # can be written. Home Assistant's own list card ticks an item by
        # sending it back whole, due date and description included, and
        # `todo.update_item` refuses a field whose feature is not declared --
        # so with the tick alone, every tick from that card failed with
        # `update_field_not_supported`. `async_update_todo_item` accepts them
        # unchanged and refuses them changed.
        self._attr_supported_features = (
            TodoListEntityFeature.UPDATE_TODO_ITEM
            | TodoListEntityFeature.SET_DUE_DATE_ON_ITEM
            | TodoListEntityFeature.SET_DESCRIPTION_ON_ITEM
            if account.write_enabled and account.can_tick_homework
            else TodoListEntityFeature(0)
        )

    @property
    def todo_items(self) -> list[TodoItem] | None:
        """The homework items, or ``None`` while nothing has been collected."""
        facts = self.facts
        if facts is None:
            return None
        return [
            TodoItem(
                uid=item.id,
                summary=item.subject or "?",
                # The plain form, not the HTML: the to-do list renders this
                # as text, so the markup PRONOTE wraps the description in
                # would be read out tag by tag.
                description=item.description_text or None,
                due=item.due,
                status=(
                    TodoItemStatus.COMPLETED
                    if item.done
                    else TodoItemStatus.NEEDS_ACTION
                ),
            )
            for item in facts.homework
        ]

    async def async_update_todo_item(self, item: TodoItem) -> None:
        """Apply a tick or an untick to PRONOTE.

        Only the status is sent. PRONOTE owns the subject, the wording and the
        deadline of a homework item -- a client that also pushed those would be
        overwriting a teacher.

        So an edit to any of them is refused rather than dropped: the list
        declares the due date and the description so that a whole item sent
        back with its tick is accepted, and the edit dialog that declaration
        opens must not save something that is then silently lost.
        """
        if not self.account.write_enabled:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="writes_disabled",
            )
        account = self.account
        extras = account.extras
        if extras is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="homework_tick_not_supported",
                translation_placeholders={
                    "source": str(account.connector.capabilities.source)
                },
            )
        if not account.can_tick_homework:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="homework_tick_parent_account",
            )
        if item.uid is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="todo_item_unknown",
            )

        current = next(
            (known for known in self.todo_items or () if known.uid == item.uid),
            None,
        )
        if current is not None and (
            item.summary,
            item.description,
            item.due,
        ) != (current.summary, current.description, current.due):
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="todo_item_owned_by_pronote",
            )

        done = item.status == TodoItemStatus.COMPLETED
        homework_id = item.uid

        def work(client: Any) -> int:
            return extras.gateway.set_homework_done(client, homework_id, done=done)

        try:
            await async_run_gesture(
                extras,
                Tier.HOMEWORK,
                self.student.id,
                work,
                # The list is read before the post, because the item's `N` is
                # only valid in the session that read it, and again after it,
                # because only that shows the tick landed (`set_homework_done`).
                cost=3,
                # A human just tapped a checkbox: a gesture, so it crosses quiet
                # hours -- but it still goes through the limiter, and it is
                # never CRITICAL: nothing done by hand may pre-empt the session
                # tier (annexe B §2.4). A deferral, a refused login or an
                # unreachable server is translated there, the same way as for
                # every service, so the card puts the box back with a sentence.
                priority=Priority.GESTURE,
            )
        except ItemNotFound as missing:
            # Raised, not swallowed: PRONOTE would have accepted a stale `N` and
            # recorded nothing, and the card puts the checkbox back on an error.
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="item_not_found",
            ) from missing
        except WriteNotApplied as ignored:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="write_not_applied",
            ) from ignored

        account.scheduler.request([Tier.HOMEWORK])
        self.async_write_ha_state()
