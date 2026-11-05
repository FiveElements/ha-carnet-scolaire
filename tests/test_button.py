"""The refresh buttons: they ask for a collection, they never perform one.

This platform is the one place a user can make the integration talk to the
school's server on demand, which makes it the obvious place for a second path to
the network to appear. It must not be one. Pressing a button raises the priority
of some tiers and wakes the master tick; the scheduler still decides what is
due, the limiter still spaces the calls, and the batch lock still refuses a
second batch -- so ten presses while waiting for a grade cost one batch, not ten
(§5.1, annexe B §6). The sanction for getting that wrong applies to an IP
address and lands on the whole household, not on the press that caused it.

Three failure modes drive the tests below, and none of them shows up as an
error.

**A press that fetches.** A button calling the gateway directly would be
correct on a dashboard and invisible in the budget: the requests would not be
charged, not spaced and not shed under quiet hours. What is asserted here is
therefore negative -- pressing places no request of its own and only wakes the
heartbeat.

**A press that asks for a re-login.** ``session`` is the one tier no button may
request: forcing a re-login by hand is the gesture that can get an address
suspended (annexe B §3). The scheduler happens to hold no plan for it today, so
including it would be silently harmless -- and would stay harmless right up
until the day it was not. The exact tier list a press asks for is asserted.

**A button that greys out.** ``available`` is deliberately overridden to ignore
the base class's staleness rule. A stale integration is precisely when somebody
wants to press refresh, so inheriting that rule would hide the control at the
only moment it is useful.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

from homeassistant.components.button import DOMAIN as BUTTON_DOMAIN
from homeassistant.const import ATTR_ENTITY_ID, STATE_UNAVAILABLE
from homeassistant.helpers import entity_registry as er

from custom_components.carnet_scolaire import button as button_platform
from custom_components.carnet_scolaire.button import (
    BUTTONS,
    PronoteRefreshButton,
    async_setup_entry,
)
from custom_components.carnet_scolaire.const import DOMAIN, Tier
from custom_components.carnet_scolaire.entity import PronoteEntity

from .conftest import CHILDREN, REQUIRES_HASS, child_key

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity import Entity
    from pytest_homeassistant.common import MockConfigEntry

    from custom_components.carnet_scolaire.account import PronoteAccount

    from .fixtures.client import FakeClient

pytestmark = REQUIRES_HASS

STUDENT_ONE = CHILDREN[0][0]
STUDENT_TWO = CHILDREN[1][0]

#: Every tier a button is allowed to ask for: all of them except the session.
DATA_TIERS = [tier for tier in Tier if tier is not Tier.SESSION]


def button_id(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    student_id: str,
    key: str,
) -> str:
    """The entity id of one child's button, resolved through the registry.

    By unique id rather than by a slugified name: the name comes from
    ``strings.json`` and is translated, so building the entity id by hand would
    make these tests fail on a wording change instead of on a behaviour change.
    """
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        BUTTON_DOMAIN, DOMAIN, f"{entry.entry_id}_{child_key(entry, student_id)}_{key}"
    )
    assert entity_id is not None, f"no {key} button for {student_id}"
    return entity_id


async def press(hass: HomeAssistant, entity_id: str) -> None:
    """Press a button the way an automation does, through the service."""
    await hass.services.async_call(
        BUTTON_DOMAIN, "press", {ATTR_ENTITY_ID: entity_id}, blocking=True
    )


# ---------------------------------------------------------------------------
# What gets created
# ---------------------------------------------------------------------------


async def test_every_child_gets_its_own_pair_of_refresh_buttons(
    hass: HomeAssistant, mock_entry: MockConfigEntry, account: PronoteAccount
) -> None:
    """One press must refresh the child whose dashboard it is on.

    The setup loop nests students inside descriptions, and getting that nesting
    wrong does not raise: it creates the buttons for the first child only, and a
    parent with two children then finds a refresh control on one dashboard and
    none on the other -- or, worse, the second child's registry entry collides
    with the first's and silently re-points at it.
    """
    expected = {
        f"{mock_entry.entry_id}_{child_key(mock_entry, student)}_{description.key}"
        for student in (STUDENT_ONE, STUDENT_TWO)
        for description in BUTTONS
    }

    registry = er.async_get(hass)
    created = {
        entity.unique_id
        for entity in er.async_entries_for_config_entry(registry, mock_entry.entry_id)
        if entity.domain == BUTTON_DOMAIN
    }

    assert created == expected
    assert len(created) == len(account.students) * len(BUTTONS)


async def test_a_button_whose_tier_has_no_coordinator_is_skipped_not_fatal(
    hass: HomeAssistant, mock_entry: MockConfigEntry, account: PronoteAccount
) -> None:
    """One unsatisfiable button must not take the whole platform down with it.

    ``BUTTONS`` names its tier by hand, so a tier added there but not wired to a
    coordinator -- or a coordinator dropped from the account -- is a plain
    mistake in a constant. Subscripting the mapping would raise while the
    platform was being set up, which in Home Assistant means *no* button is
    created at all: the good ones disappear along with the broken one. Skipping
    leaves the rest working, and the missing button is visible on the dashboard.
    """
    account.coordinators.pop(Tier.MARKS)
    created: list[Entity] = []

    def capture(entities: Any, *_args: Any, **_kwargs: Any) -> None:
        created.extend(entities)

    await async_setup_entry(hass, mock_entry, capture)  # type: ignore[arg-type]

    assert created, "dropping one coordinator suppressed every button"
    assert {entity.entity_description.key for entity in created} == {"refresh"}  # type: ignore[attr-defined]
    assert len(created) == len(account.students)


def test_the_platform_serialises_presses_rather_than_fanning_them_out() -> None:
    """``PARALLEL_UPDATES`` is what Home Assistant itself honours.

    Left unset -- or set to zero, which means unlimited -- a script that presses
    the refresh button for six children at once produces six concurrent presses,
    six boosts and six wake-ups racing for the batch lock. The limiter serialises
    the wire under its own lock, but that is a second line of defence: this is
    the layer that keeps the fan-out from being created in the first place.
    """
    assert button_platform.PARALLEL_UPDATES == 1


# ---------------------------------------------------------------------------
# Availability
# ---------------------------------------------------------------------------


async def test_a_refresh_button_stays_pressable_when_its_tier_has_no_data(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    account: PronoteAccount,
) -> None:
    """The override matters exactly when the base class would hide the button.

    ``PronoteEntity.available`` reports unavailable with no snapshot or with a
    stale one -- correct for a sensor, wrong for the control a user reaches for
    *because* the data has stopped arriving. Deleting the override as
    duplication would grey out the refresh button during precisely the outage it
    exists to end, and a greyed-out button also stops answering
    ``button.press`` from an automation.
    """
    coordinator = account.coordinators[Tier.TIMETABLE]
    coordinator.async_set_updated_data({})
    await hass.async_block_till_done()

    entity_id = button_id(hass, mock_entry, STUDENT_ONE, "refresh")
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state != STATE_UNAVAILABLE

    # And the inherited rule really would have hidden it, so the override is
    # load-bearing rather than decorative.
    orphan = PronoteRefreshButton(account, coordinator, account.students[0], BUTTONS[0])
    assert PronoteEntity.available.fget(orphan) is False  # type: ignore[attr-defined]
    assert orphan.available is True

    await press(hass, entity_id)


# ---------------------------------------------------------------------------
# What a press asks for
# ---------------------------------------------------------------------------


async def test_the_general_refresh_asks_for_every_tier_but_never_the_session(
    hass: HomeAssistant, mock_entry: MockConfigEntry, account: PronoteAccount
) -> None:
    """No button may offer a hand-forced re-login (annexe B §3).

    Repeated logins are what gets a PRONOTE address suspended, and a suspension
    applies to an IP address -- so the cost of a "reconnect" button is the whole
    household losing access, not a failed press. The session tier is excluded
    here rather than relied upon to have no plan: it has none today, which makes
    the mistake silent instead of impossible.
    """
    entity_id = button_id(hass, mock_entry, STUDENT_ONE, "refresh")

    with patch.object(account.scheduler, "request") as request:
        await press(hass, entity_id)

    assert request.call_count == 1
    requested = request.call_args.args[0]
    assert Tier.SESSION not in requested
    assert list(requested) == DATA_TIERS


async def test_the_grades_button_asks_for_grades_and_nothing_else(
    hass: HomeAssistant, mock_entry: MockConfigEntry, account: PronoteAccount
) -> None:
    """A targeted button that boosts everything is a hidden tenfold cost.

    The point of a grades-only control is that a parent refreshing one number
    does not also re-collect the year's absences, the menus and the history --
    which is six requests per child on its own. If ``requests`` were ignored, or
    defaulted to "everything" for both descriptions, the two buttons would look
    identical and behave identically, and only the request counter would know.
    """
    entity_id = button_id(hass, mock_entry, STUDENT_ONE, "refresh_marks")

    with patch.object(account.scheduler, "request") as request:
        await press(hass, entity_id)

    assert list(request.call_args.args[0]) == [Tier.MARKS]


async def test_a_press_wakes_the_heartbeat_instead_of_calling_pronote_itself(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    account: PronoteAccount,
    parent_client: FakeClient,
) -> None:
    """A button that fetched would be a second path to the network.

    Every request this integration places has to be charged to the budget,
    spaced by the minimum interval and shed under quiet hours, and the only code
    that does all three is the collection loop the master tick drives. A press
    that called the gateway directly would work on a dashboard while quietly
    escaping all of it, so what is asserted is that the press itself posts
    nothing and hands the job to the tick.
    """
    entity_id = button_id(hass, mock_entry, STUDENT_ONE, "refresh")
    before = len(parent_client.posts)

    with patch.object(account, "async_request_tick", AsyncMock()) as request_tick:
        await press(hass, entity_id)

    assert request_tick.await_count == 1
    assert len(parent_client.posts) == before


async def test_a_press_still_refreshes_when_write_operations_are_disabled(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    account: PronoteAccount,
) -> None:
    """Reading is not writing, and the default install must keep its buttons.

    Write operations are off by default (§8.3), and gating this platform behind
    that switch -- an easy thing to do while hardening the ones that really do
    write -- would leave every default installation with two dead controls and
    no explanation. Nothing pressed here sends anything to the school.
    """
    assert account.write_enabled is False

    entity_id = button_id(hass, mock_entry, STUDENT_TWO, "refresh_marks")
    with patch.object(account.scheduler, "request") as request:
        await press(hass, entity_id)

    assert request.call_count == 1


# ---------------------------------------------------------------------------
# What a press costs
# ---------------------------------------------------------------------------


async def test_ten_presses_cost_one_batch_and_not_ten(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    account: PronoteAccount,
    parent_client: FakeClient,
) -> None:
    """§5.1, measured on the wire rather than argued from the design.

    A parent waiting for a grade presses again, and again. Each press used to
    re-arm the boost and buy another collection: ten presses on ``history``, six
    requests per child, were 120 requests against a server whose one sanction
    applies to an IP address. It only ever looked bounded because the
    collections overlapped and hit the batch lock -- a property of the event
    loop, which stopped holding between two Home Assistant releases with nothing
    changed in this repository.

    So both halves are asserted: the first press must actually collect
    something, or the button is decorative, and the nine that follow must cost
    nothing at all.
    """
    entity_id = button_id(hass, mock_entry, STUDENT_ONE, "refresh")
    before = len(parent_client.posts)

    await press(hass, entity_id)
    await hass.async_block_till_done()
    after_one = len(parent_client.posts)
    assert after_one > before, "the press collected nothing at all"

    for _ in range(9):
        await press(hass, entity_id)
    await hass.async_block_till_done()

    assert len(parent_client.posts) == after_one
