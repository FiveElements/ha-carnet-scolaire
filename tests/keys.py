"""Find the key the integration minted for an item a test built by its ``N``.

Fixtures name items by PRONOTE's ``N`` (``HOMEWORK-1``) because that is what a
protocol payload carries. What the integration publishes -- a to-do ``uid``, an
``items[].id``, the identifier a service is given -- is the key minted from the
item's content (see ``item_keys``), and the ``N`` survives only as ``ref``.
Reading the key off the snapshot, rather than recomputing a digest here, keeps
these tests about behaviour: they would otherwise all fail together the day a
key's parts change, for a reason none of them is about.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from custom_components.carnet_scolaire.const import Tier
from custom_components.carnet_scolaire.gateway import _thread_keys, _visible_threads

if TYPE_CHECKING:
    from custom_components.carnet_scolaire.account import PronoteAccount

    from .fixtures.client import FakeClient

_ITEMS_BY_TIER = {
    Tier.HOMEWORK: "homework",
    Tier.NEWS: "information",
    Tier.DISCUSSIONS: "discussions",
}


def key_of(account: PronoteAccount, ref: str, tier: Tier = Tier.HOMEWORK) -> str:
    """The minted key of the item whose ``N`` is ``ref``, for any child."""
    for student in account.students:
        snapshot = account.snapshot(tier, student.id)
        if snapshot is None:
            continue
        items: tuple[Any, ...] = getattr(snapshot.data, _ITEMS_BY_TIER[tier])
        for item in items:
            if item.ref == ref:
                return str(item.id)
    raise AssertionError(f"no {tier} item with N {ref!r} in any snapshot")


def thread_key(client: FakeClient, ref: str) -> str:
    """The key of a thread a test added *after* the last collection.

    Computed the way a reply computes it -- from a fresh listing -- because no
    snapshot holds a thread appended to the fake client mid-test.
    """
    visible = _visible_threads(client.threads)  # type: ignore[arg-type]
    for key, thread in zip(_thread_keys(visible), visible, strict=True):
        if str(thread.id) == ref:
            return key
    raise AssertionError(f"no visible thread with N {ref!r}")
