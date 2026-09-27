"""A period posted after a reconnection carries the new session's ``N``.

Every ``N`` PRONOTE hands out is re-encrypted per session, the periods'
included. The account reads its periods once, from the session facts, at
set-up -- and again only when a login announces a roster it has not paired,
after the batch. A login can happen inside any call (an expired session, or
``per_batch`` opening one per batch), so the period-scoped tiers of the batch
that logged in posted the previous session's ``N``: on a parent account for
that batch, on a student account -- whose roster never changes, so whose facts
were never read again -- at every collection until Home Assistant restarted.

These run the whole chain with only the login doubled, on both shapes of
account, and read the ``N`` off the bodies the fake client actually received.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any

import pytest

from custom_components.carnet_scolaire.const import (
    DEFAULT_TIER_INTERVALS,
    FUNC_ATTENDANCE,
    FUNC_EVALUATIONS,
    FUNC_MARKS,
    FUNC_REPORT,
    Tier,
)

from .conftest import REQUIRES_HASS

if TYPE_CHECKING:
    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.core import HomeAssistant

    from custom_components.carnet_scolaire.account import PronoteAccount

    from .fixtures.client import FakeClient

pytestmark = REQUIRES_HASS

_PERIOD_SCOPED = frozenset(
    name for name, _tab in (FUNC_MARKS, FUNC_REPORT, FUNC_ATTENDANCE, FUNC_EVALUATIONS)
)
_PERIOD_TIERS = [Tier.MARKS, Tier.ATTENDANCE, Tier.EVALUATIONS, Tier.HISTORY]


def _live_period_ids(client: FakeClient) -> set[str]:
    """The ``N`` of every period, as the session in hand encrypts it."""
    general = client.func_options["dataSec"]["data"]["General"]
    return {str(period["N"]) for period in general["ListePeriodes"]}


def _period_id(body: Any) -> str:
    """The period ``N`` a posted body names; the tabs disagree on the case."""
    named = body.get("Periode") or body.get("periode")
    return str(named["N"])


async def _reconnect(account: PronoteAccount, client: FakeClient) -> None:
    """Drop the session so the next call logs in again, every ``N`` re-encrypted."""
    client.rotate_identifiers()
    assert account.extras is not None
    await account.extras.session._reopen()


@pytest.mark.parametrize(
    "account_client",
    ["parent", "student"],
    indirect=True,
    ids=["parent-account", "student-account"],
)
async def test_the_first_collection_after_a_reconnection_posts_the_new_period_ids(
    hass: HomeAssistant,
    account: PronoteAccount,
    account_client: FakeClient,
) -> None:
    """No period-scoped request carries an ``N`` from the session before.

    The defect this stops: the connector bound each request to the period it
    cached at set-up, and a request with an earlier session's ``N`` is one the
    server either refuses or answers for nothing. The collection must also
    still succeed -- a fix that dropped the tier instead would pass the first
    assertion by posting nothing.
    """
    stale = {period.id for period in account.state.periods}
    await _reconnect(account, account_client)
    live = _live_period_ids(account_client)
    assert live.isdisjoint(stale)
    posted = len(account_client.posts)

    account.scheduler.request(_PERIOD_TIERS)
    await account.async_request_tick()
    await hass.async_block_till_done()

    scoped = [
        (name, body)
        for name, _tab, body in account_client.posts[posted:]
        if name in _PERIOD_SCOPED
    ]
    assert {name for name, _body in scoped} == _PERIOD_SCOPED
    assert {_period_id(body) for _name, body in scoped} <= live
    for tier in _PERIOD_TIERS:
        assert account.state.records[tier].consecutive_failures == 0
        for student in account.students:
            assert account.has_data(tier, student.id)


@pytest.mark.parametrize(
    "account_client",
    ["parent", "student"],
    indirect=True,
    ids=["parent-account", "student-account"],
)
async def test_a_second_reconnection_is_followed_as_well_as_the_first(
    hass: HomeAssistant,
    account: PronoteAccount,
    account_client: FakeClient,
    school_day: FrozenDateTimeFactory,
) -> None:
    """Resolution happens at every call, not once after the first login.

    A fix that refreshed the cached periods on one path -- the roster changing,
    say -- would follow the first reconnection of a parent account and none
    of a student's; two in a row is what tells a per-call resolution from a
    one-off refresh.
    """
    for _ in range(2):
        # A tier takes one boost per interval; step past it so the second
        # round is collected rather than refused.
        school_day.tick(timedelta(minutes=DEFAULT_TIER_INTERVALS[Tier.MARKS] + 1))
        await _reconnect(account, account_client)
        live = _live_period_ids(account_client)
        posted = len(account_client.posts)

        account.scheduler.request([Tier.MARKS])
        await account.async_request_tick()
        await hass.async_block_till_done()

        bodies = [
            body
            for name, _tab, body in account_client.posts[posted:]
            if name in _PERIOD_SCOPED
        ]
        assert bodies
        assert {_period_id(body) for body in bodies} <= live
