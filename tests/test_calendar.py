"""The three calendars, and the window query nothing exercised.

``async_get_events`` is what a dashboard calls every time a user pans the
calendar card, and it was covered by no test at all -- neither its window
arithmetic nor the promise its docstring makes, that it answers *from memory
only*. That promise is the load-bearing one: a calendar that fetched on pan
would be a second path to the network, outside the limiter's monopoly
(annexe B §6), driven by how often somebody drags a card.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from typing import TYPE_CHECKING

from custom_components.carnet_scolaire.calendar import (
    CALENDARS,
    PronoteCalendar,
    async_setup_entry,
)
from custom_components.carnet_scolaire.connectors.protocol import (
    ConnectorCapabilities,
    Source,
)
from custom_components.carnet_scolaire.const import Tier

from .conftest import REQUIRES_HASS

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant.common import MockConfigEntry

    from custom_components.carnet_scolaire.account import PronoteAccount

    from .fixtures.client import FakeClient

pytestmark = REQUIRES_HASS


def _timetable_calendar(account: PronoteAccount) -> PronoteCalendar:
    """The first child's timetable calendar, built as the platform builds it."""
    description = next(item for item in CALENDARS if item.key == "timetable")
    return PronoteCalendar(
        account,
        account.coordinators[description.tier],
        account.students[0],
        description,
    )


async def test_the_window_query_keeps_only_what_overlaps_it(
    hass: HomeAssistant,
    account: PronoteAccount,
    parent_client: FakeClient,
) -> None:
    """Overlap, not containment: a lesson straddling the edge must be kept.

    A card asking for "today" would otherwise drop the lesson in progress at
    midnight, and a filter written as "starts inside the window" is the
    natural way to get that wrong.
    """
    calendar = _timetable_calendar(account)
    everything = await calendar.async_get_events(
        hass,
        account.now() - timedelta(days=30),
        account.now() + timedelta(days=30),
    )
    assert everything, "the fixture must publish at least one lesson"

    first = everything[0]
    # A window that ends one second after the first event starts: it contains
    # neither the whole event nor its end, and the event must still be there.
    straddling = await calendar.async_get_events(
        hass, first.start - timedelta(hours=1), first.start + timedelta(seconds=1)
    )

    assert first in straddling


async def test_the_window_query_excludes_an_event_that_ends_on_the_boundary(
    hass: HomeAssistant,
    account: PronoteAccount,
) -> None:
    """The window is half-open, ``[start, end)``, as the docstring says.

    An event that ends exactly when the window begins is over, and counting it
    would make a "what is on now" card show the lesson that has just finished.
    """
    calendar = _timetable_calendar(account)
    everything = await calendar.async_get_events(
        hass,
        account.now() - timedelta(days=30),
        account.now() + timedelta(days=30),
    )
    first = everything[0]

    after = await calendar.async_get_events(
        hass, first.end, first.end + timedelta(days=1)
    )

    assert first not in after


async def test_the_window_query_answers_in_chronological_order(
    hass: HomeAssistant,
    account: PronoteAccount,
) -> None:
    """A calendar card renders the list as given, so the order is the contract."""
    calendar = _timetable_calendar(account)

    events = await calendar.async_get_events(
        hass,
        account.now() - timedelta(days=30),
        account.now() + timedelta(days=30),
    )

    assert [item.start for item in events] == sorted(item.start for item in events)


async def test_a_calendar_with_no_snapshot_yet_answers_an_empty_window(
    hass: HomeAssistant,
    account: PronoteAccount,
) -> None:
    """Empty, never an exception.

    A dashboard opened during the first batch calls this before any snapshot
    exists, and an exception there is rendered as a broken card rather than as
    an empty one.
    """
    description = next(item for item in CALENDARS if item.key == "timetable")
    coordinator = account.coordinators[description.tier]
    calendar = PronoteCalendar(
        account,
        coordinator,
        # A child the coordinator holds nothing for.
        replace(account.students[0], id="STUDENT-NEVER-COLLECTED"),
        description,
    )

    assert (
        await calendar.async_get_events(
            hass, account.now(), account.now() + timedelta(days=1)
        )
        == []
    )
    assert calendar.event is None


async def test_a_source_without_a_tier_gets_no_calendar_for_it(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    account: PronoteAccount,
) -> None:
    """§8.3: a platform must not build an entity its source cannot feed.

    A calendar for a tier the source does not have would sit ``unavailable``
    for ever, which is how a missing Ecoledirecte page looked before
    capabilities existed -- and an entity that is permanently unavailable
    breaks the automations pointing at it rather than merely looking empty.
    """
    built: list[object] = []

    def collect(entities: object, *_args: object, **_kwargs: object) -> None:
        built.extend(entities)  # type: ignore[arg-type]

    narrow = ConnectorCapabilities(
        source=Source.PRONOTE,
        tiers=frozenset({Tier.SESSION, Tier.TIMETABLE}),
        writes=frozenset(),
        services=frozenset(),
    )
    connector = account.connector
    original = type(connector).CAPABILITIES
    try:
        type(connector).CAPABILITIES = narrow  # type: ignore[misc]
        await async_setup_entry(
            hass,
            mock_entry,
            collect,  # type: ignore[arg-type]
        )
    finally:
        type(connector).CAPABILITIES = original  # type: ignore[misc]

    keys = {entity._description.key for entity in built}  # type: ignore[attr-defined]
    assert keys == {"timetable"}


async def test_a_tier_with_no_coordinator_gets_no_calendar_either(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    account: PronoteAccount,
) -> None:
    """Capabilities and coordinators are two separate questions.

    A tier can be announced by the source and still have no coordinator --
    disabled in the options, most simply. Reading the capability alone would
    hand the entity a ``None`` coordinator, and the failure would be an
    ``AttributeError`` inside the platform forward, which takes the whole
    entry down rather than one calendar.
    """
    built: list[object] = []

    def collect(entities: object, *_args: object, **_kwargs: object) -> None:
        built.extend(entities)  # type: ignore[arg-type]

    removed = account.coordinators.pop(Tier.HOMEWORK)
    try:
        await async_setup_entry(
            hass,
            mock_entry,
            collect,  # type: ignore[arg-type]
        )
    finally:
        account.coordinators[Tier.HOMEWORK] = removed

    keys = {entity._description.key for entity in built}  # type: ignore[attr-defined]
    assert "homework" not in keys
    assert "timetable" in keys
