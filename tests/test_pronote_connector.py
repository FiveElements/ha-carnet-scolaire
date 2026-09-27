"""The Pronote connector: declared costs, and the period it may not have."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from custom_components.carnet_scolaire.connectors.errors import (
    ConnectorChildMissingError,
)
from custom_components.carnet_scolaire.const import Priority, Tier

from .conftest import REQUIRES_HASS

if TYPE_CHECKING:
    from custom_components.carnet_scolaire.account import PronoteAccount

    from .fixtures.client import FakeClient

pytestmark = REQUIRES_HASS


def test_pronote_connector_capabilities_list_every_collectable_tier() -> None:
    """Silently dropping history is a missing bulletin, not a setting."""
    from custom_components.carnet_scolaire.connectors.pronote import PronoteConnector
    from custom_components.carnet_scolaire.const import Tier

    collectable = frozenset(tier for tier in Tier if tier is not Tier.SESSION)
    assert collectable <= PronoteConnector.CAPABILITIES.tiers
    assert Tier.SESSION not in PronoteConnector.CAPABILITIES.tiers


async def test_asking_for_a_child_the_session_never_announced_is_a_caller_bug(
    account: PronoteAccount,
) -> None:
    """A typed error, not a ``KeyError`` from inside a dict lookup.

    The cache is filled from the roster the login announced, so a miss means
    the caller invented an identifier -- a selection that went stale, or a
    service call naming a child of the other entry. ``ConnectorChildMissingError``
    is what lets ``account.py`` tell that apart from a tier that failed, and a
    bare ``KeyError`` reaching a tier would be recorded as a collection
    failure and retried at the tier's cadence for ever.
    """
    with pytest.raises(ConnectorChildMissingError):
        account.connector.session_facts("STUDENT-NOBODY-ANNOUNCED")


@pytest.mark.parametrize(
    "tier",
    [
        pytest.param(Tier.MARKS, id="marks"),
        pytest.param(Tier.ATTENDANCE, id="attendance"),
        pytest.param(Tier.EVALUATIONS, id="evaluations"),
    ],
)
async def test_a_period_less_account_collects_the_three_period_tiers_for_free(
    account: PronoteAccount, parent_client: FakeClient, tier: Tier
) -> None:
    """Between two terms there is no current period, and that is not an error.

    An establishment publishes no current period over the summer, and for a
    few days between terms. Three tiers are keyed on it, and asking PRONOTE
    for "the marks of no period" is a request that cannot succeed -- so it
    must not be sent. The typed empty value keeps the entities fed and
    ``calls=0`` keeps the budget honest: this is the one collection that
    legitimately spends nothing.
    """
    from dataclasses import replace

    connector = account.connector
    student_id = account.students[0].id
    facts = connector.session_facts(student_id)
    connector._session_facts[student_id] = replace(facts, current_period=None)
    before = len(parent_client.posts)

    result = await connector.async_collect(tier, student_id, priority=Priority.NORMAL)

    assert result.calls == 0
    assert len(parent_client.posts) == before


def test_a_pupils_own_account_announces_itself_as_its_only_child(
    client: FakeClient,
) -> None:
    """A pupil account holds no ``children``, and must still name one child.

    Reading ``children`` alone would return an empty roster, and an account
    that announces nobody collects nothing -- which is how a pupil's own
    account would present: loaded, no error, no entities.
    """
    from custom_components.carnet_scolaire.connectors.pronote import _client_children

    assert not client.is_parent_account
    assert _client_children(client) == ((str(client.info.id), str(client.info.name)),)
