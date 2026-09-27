"""Which children an account follows, across PRONOTE's identifier rotation.

PRONOTE re-encrypts every resource identifier at each login, a child's
``46#<signature>`` included. The children's *identity* already survived that
through the minted keys of ``child_keys.py``; their *selection* did not. It was
stored as resource identifiers, and each login that renamed a child appended
the new one as if a child had arrived: a one-child parent account was measured
holding eleven identifiers, none current, warning at every restart that its
selection had lapsed -- and any child its owner had declined was followed again.

The selection is now stored as minted keys, and these tests hold the four
promises that come with that: it does not grow, a declined child stays
declined, a child the account never announced before is still adopted, and an
ordinary rotation logs no warning and no name.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Final
from unittest.mock import patch

from custom_components.carnet_scolaire.child_keys import is_minted
from custom_components.carnet_scolaire.const import (
    CHILD_KEY,
    CHILD_NAME,
    CHILD_RESOURCE_ID,
    CONF_CHILD_KEYS,
    CONF_CHILDREN,
    Tier,
)

from .conftest import CHILDREN, REQUIRES_HASS
from .fixtures.client import FakeClient

if TYPE_CHECKING:
    from collections.abc import Iterator

    from homeassistant.core import HomeAssistant
    import pytest
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.carnet_scolaire.account import PronoteAccount

pytestmark = REQUIRES_HASS

#: How many logins each scenario goes through. Enough that growth by one per
#: login cannot hide, few enough to stay far below the daily login cap.
_ROTATIONS = 4

_OUR_LOGGER = "custom_components.carnet_scolaire"


def _patched(client: FakeClient) -> Any:
    """The one seam this suite doubles: the login."""
    return patch(
        "custom_components.carnet_scolaire.session.build_client",
        return_value=client,
    )


def _followed_names(account: PronoteAccount) -> set[str]:
    return {student.name for student in account.students}


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.levelno >= logging.WARNING and record.name.startswith(_OUR_LOGGER)
    ]


def _our_messages(caplog: pytest.LogCaptureFixture) -> Iterator[str]:
    return (
        record.getMessage()
        for record in caplog.records
        if record.name.startswith(_OUR_LOGGER)
    )


async def _reload_rotated(
    hass: HomeAssistant, entry: MockConfigEntry, client: FakeClient
) -> PronoteAccount:
    """A restart, as PRONOTE sees one: the next login renames every child."""
    client.rotate_identifiers()
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    account: PronoteAccount = entry.runtime_data
    return account


#: A different tier for each re-login: the scheduler grants one boost per tier
#: per interval, so asking twice for the same one would place no call -- and no
#: call means no login, and no rotation to observe.
_BOOSTABLE: Final = tuple(tier for tier in Tier if tier is not Tier.SESSION)


async def _reconnect_rotated(
    hass: HomeAssistant, account: PronoteAccount, client: FakeClient, turn: int
) -> None:
    """A session that expired mid-run: the next call logs in again, renamed."""
    client.rotate_identifiers()
    assert account.extras is not None
    await account.extras.session._reopen()
    account.scheduler.request([_BOOSTABLE[turn % len(_BOOSTABLE)]])
    await account.async_request_tick()
    await hass.async_block_till_done()
    announced = {str(child.id) for child in client.children}
    assert {student.id for student in account.students} <= announced, (
        "the re-login never happened, so this turn tested nothing"
    )


async def test_restarts_that_rename_the_child_leave_the_selection_at_one_key(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    school_day: Any,
    no_spacing: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The measured defect: a one-child account, one more identifier per login.

    After eleven logins the entry held eleven identifiers for one child and
    warned at every restart. Each restart here renames the child, and the
    stored selection must stay what it was after the first: one minted key.
    """
    client = FakeClient(children=(CHILDREN[0],))
    hass.config_entries.async_update_entry(
        mock_entry, data={**mock_entry.data, CONF_CHILDREN: [CHILDREN[0][0]]}
    )
    caplog.set_level(logging.INFO)

    with _patched(client):
        assert await hass.config_entries.async_setup(mock_entry.entry_id)
        await hass.async_block_till_done()
        first = list(mock_entry.data[CONF_CHILDREN])
        for _ in range(_ROTATIONS):
            account = await _reload_rotated(hass, mock_entry, client)
            assert mock_entry.data[CONF_CHILDREN] == first
            assert len(mock_entry.data[CONF_CHILD_KEYS]) == 1
        assert await hass.config_entries.async_unload(mock_entry.entry_id)
        await hass.async_block_till_done()

    assert len(first) == 1
    assert is_minted(first[0])
    assert _followed_names(account) == {CHILDREN[0][1]}
    assert _warnings(caplog) == []


async def test_reconnections_that_rename_the_child_leave_the_selection_alone(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    school_day: Any,
    no_spacing: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The other half of the growth: a re-login inside a running account.

    That is where the identifiers were appended -- each renamed child looked
    like a newcomer to the check that runs after every batch -- so it has to
    hold without any restart in between.
    """
    client = FakeClient(children=(CHILDREN[0],))
    hass.config_entries.async_update_entry(
        mock_entry, data={**mock_entry.data, CONF_CHILDREN: [CHILDREN[0][0]]}
    )
    caplog.set_level(logging.INFO)

    with _patched(client):
        assert await hass.config_entries.async_setup(mock_entry.entry_id)
        await hass.async_block_till_done()
        account: PronoteAccount = mock_entry.runtime_data
        first = list(mock_entry.data[CONF_CHILDREN])
        for turn in range(_ROTATIONS):
            await _reconnect_rotated(hass, account, client, turn)
            assert mock_entry.data[CONF_CHILDREN] == first
        # The account followed the rename rather than losing the child.
        assert [student.id for student in account.students] == [
            str(child.id) for child in client.children
        ]
        assert mock_entry.runtime_data is account
        assert await hass.config_entries.async_unload(mock_entry.entry_id)
        await hass.async_block_till_done()

    assert _warnings(caplog) == []


async def test_a_bloated_legacy_selection_is_repaired_and_follows_the_same_child(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    school_day: Any,
    no_spacing: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The live entry as it stands: eleven dead identifiers, one known child.

    Nothing in the list can be placed, so the account was following every
    child to recover. The repair must keep following exactly that child, under
    the key its entities already carry, and store the choice as that key --
    once, and without the warning the old list raised at every restart.
    """
    client = FakeClient(children=(CHILDREN[0],))
    client.rotate_identifiers()
    hass.config_entries.async_update_entry(
        mock_entry,
        data={
            **mock_entry.data,
            CONF_CHILDREN: [f"46#NOT-A-REAL-N-{index}" for index in range(11)],
            CONF_CHILD_KEYS: [
                {
                    CHILD_KEY: "child-1",
                    CHILD_RESOURCE_ID: "46#NOT-A-REAL-N-latest",
                    CHILD_NAME: CHILDREN[0][1],
                }
            ],
        },
    )
    caplog.set_level(logging.INFO)

    with _patched(client):
        assert await hass.config_entries.async_setup(mock_entry.entry_id)
        await hass.async_block_till_done()
        account: PronoteAccount = mock_entry.runtime_data
        assert mock_entry.data[CONF_CHILDREN] == ["child-1"]
        assert [account.stable_key(s.id) for s in account.students] == ["child-1"]

        # And it stays repaired: the next restart has nothing to rewrite.
        caplog.clear()
        account = await _reload_rotated(hass, mock_entry, client)
        assert mock_entry.data[CONF_CHILDREN] == ["child-1"]
        assert not any("minted key(s)" in m for m in _our_messages(caplog))
        assert await hass.config_entries.async_unload(mock_entry.entry_id)
        await hass.async_block_till_done()

    assert _warnings(caplog) == []


async def test_a_declined_child_stays_declined_across_rotations(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    parent_client: FakeClient,
    school_day: Any,
    no_spacing: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A parent who follows one child of two keeps following one.

    Stored as identifiers, the choice lapsed at the first rotation: nothing
    matched, both children were followed, and the declined one appeared with
    its entities and its whole request budget. The flow writes the choice as
    identifiers, so this starts exactly where a new entry does.
    """
    hass.config_entries.async_update_entry(
        mock_entry, data={**mock_entry.data, CONF_CHILDREN: [CHILDREN[0][0]]}
    )
    caplog.set_level(logging.INFO)

    with _patched(parent_client):
        assert await hass.config_entries.async_setup(mock_entry.entry_id)
        await hass.async_block_till_done()
        account: PronoteAccount = mock_entry.runtime_data
        selection = list(mock_entry.data[CONF_CHILDREN])
        # Both are recorded -- that is how the declined one is recognised
        # after a rotation -- and only one is chosen.
        assert len(mock_entry.data[CONF_CHILD_KEYS]) == 2
        assert selection == [account.stable_key(CHILDREN[0][0])]

        for turn in range(_ROTATIONS):
            account = await _reload_rotated(hass, mock_entry, parent_client)
            assert _followed_names(account) == {CHILDREN[0][1]}
            await _reconnect_rotated(hass, account, parent_client, turn)
            assert _followed_names(account) == {CHILDREN[0][1]}
            assert mock_entry.data[CONF_CHILDREN] == selection
            assert len(mock_entry.data[CONF_CHILD_KEYS]) == 2
        assert await hass.config_entries.async_unload(mock_entry.entry_id)
        await hass.async_block_till_done()

    assert _warnings(caplog) == []


async def test_a_child_enrolled_after_rotations_is_still_adopted(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    parent_client: FakeClient,
    school_day: Any,
    no_spacing: None,
) -> None:
    """Unknown is not refused, even next to a declined sibling.

    The declined child has a record, so a rotation cannot make it a newcomer;
    a child the table has never seen has none, and nobody was ever asked about
    it -- so it is followed, and joins the stored choice as its own key.
    """
    hass.config_entries.async_update_entry(
        mock_entry, data={**mock_entry.data, CONF_CHILDREN: [CHILDREN[0][0]]}
    )

    with _patched(parent_client):
        assert await hass.config_entries.async_setup(mock_entry.entry_id)
        await hass.async_block_till_done()
        account = await _reload_rotated(hass, mock_entry, parent_client)

        parent_client.enrol_child("STUDENT-3", "Enfant Trois")
        await _reconnect_rotated(hass, account, parent_client, 0)

        assert _followed_names(account) == {CHILDREN[0][1], "Enfant Trois"}
        assert mock_entry.data[CONF_CHILDREN] == [
            account.stable_key(student.id) for student in account.students
        ]
        assert len(mock_entry.data[CONF_CHILD_KEYS]) == 3
        assert await hass.config_entries.async_unload(mock_entry.entry_id)
        await hass.async_block_till_done()


async def test_a_selection_whose_children_all_left_warns_and_follows_the_rest(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    parent_client: FakeClient,
    school_day: Any,
    no_spacing: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The one case left for the warning: a choice that genuinely matches nobody.

    The followed child is gone and every child still announced was declined.
    Refusing to collect would take the integration down, so all of them are
    followed -- and that, unlike a rotation, is worth a WARNING, with counts
    and never a name. The stored choice is left alone, so the child is
    followed again if it comes back.
    """
    hass.config_entries.async_update_entry(
        mock_entry,
        data={
            **mock_entry.data,
            CONF_CHILDREN: ["child-9"],
            CONF_CHILD_KEYS: [
                {
                    CHILD_KEY: "child-9",
                    CHILD_RESOURCE_ID: "46#NOT-A-REAL-N-gone",
                    CHILD_NAME: "Enfant Parti",
                },
                *(
                    {
                        CHILD_KEY: f"child-{index}",
                        CHILD_RESOURCE_ID: child_id,
                        CHILD_NAME: name,
                    }
                    for index, (child_id, name) in enumerate(CHILDREN, 1)
                ),
            ],
        },
    )
    caplog.set_level(logging.INFO)

    with _patched(parent_client):
        assert await hass.config_entries.async_setup(mock_entry.entry_id)
        await hass.async_block_till_done()
        account: PronoteAccount = mock_entry.runtime_data
        assert _followed_names(account) == {name for _, name in CHILDREN}
        assert mock_entry.data[CONF_CHILDREN] == ["child-9"]
        assert await hass.config_entries.async_unload(mock_entry.entry_id)
        await hass.async_block_till_done()

    warnings = _warnings(caplog)
    assert len(warnings) == 1
    assert "none of the 1 children selected" in warnings[0]


async def test_no_child_name_reaches_the_log_through_pairing(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    parent_client: FakeClient,
    school_day: Any,
    no_spacing: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Keys and counts only: this log is what users attach to public issues.

    Exercised through every path that logs about children -- a legacy
    selection rewritten, a declined child minted, a rotation paired by name, a
    newcomer adopted -- at DEBUG for this integration's own logger.
    """
    hass.config_entries.async_update_entry(
        mock_entry, data={**mock_entry.data, CONF_CHILDREN: [CHILDREN[0][0]]}
    )
    caplog.set_level(logging.DEBUG, logger=_OUR_LOGGER)

    with _patched(parent_client):
        assert await hass.config_entries.async_setup(mock_entry.entry_id)
        await hass.async_block_till_done()
        account = await _reload_rotated(hass, mock_entry, parent_client)
        parent_client.enrol_child("STUDENT-3", "Enfant Trois")
        await _reconnect_rotated(hass, account, parent_client, 0)
        assert await hass.config_entries.async_unload(mock_entry.entry_id)
        await hass.async_block_till_done()

    logged = "\n".join(_our_messages(caplog))
    assert "child identity" in logged, "the pairing paths were not exercised"
    for name in (*(name for _, name in CHILDREN), "Enfant Trois"):
        assert name not in logged
