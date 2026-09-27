"""Stable identities for the items PRONOTE publishes.

PRONOTE's ``N`` is **not an identifier, it is a session token**. A homework
item is ``147#<signature>``, and the signature is re-encrypted by every login:
measured on a live instance on 2026-09-26, the same assignment was
``147#<A>`` in the session opened before 22:57 and ``147#<B>`` in the one
opened after it, and the older value was gone from the list altogether. The
child's ``46#<signature>`` rotates the same way (see :mod:`.child_keys`); it
was simply the first one anybody noticed.

Anything that outlives one session and is indexed on ``N`` is therefore wrong
the moment the session changes, and two defects came from exactly that on the
same evening:

- **A tick that never held.** The to-do list published ``N`` as the item's
  ``uid``. A tick sent after a reconnection named an assignment the new session
  had never heard of; PRONOTE answered ``SaisieTAFFaitEleve`` normally and
  recorded nothing, so the next collection read it back unticked. ``E: 2``
  (PR #74) was a real divergence, and could not have fixed this.
- **Fifty-four "new homework" events in one millisecond.** The change detector
  compares identifiers, so the first collection in the new session announced
  the whole school year as new.

So the ``id`` the rest of the integration sees is **one we mint** from the
item's content, and PRONOTE's ``N`` is kept beside it as ``ref`` -- valid in
the session that produced it and nowhere else. A call that has to name an item
to the server (a tick, a document download, a reply) re-reads the list in its
own session and looks the key up there; it never sends a ``ref`` from a
snapshot.

**What goes into a key** is the content that identifies the item and does not
change when its *state* does: never ``done``, never ``read``, never a grade's
value, never another ``N`` (a subject's identifier rotates too). A teacher who
rewrites a homework statement produces a new key, and that is accepted: it is a
different assignment for every purpose a card or an automation has, and the
tick that races it fails visibly instead of being lost.

**Collisions are expected, not hypothetical.** "Apporter son workbook", set
twice for the same day in the same subject, is two items with the same content.
The first keeps the bare key and the next ones get ``-2``, ``-3``, in the
order PRONOTE lists them.

This module is deliberately free of Home Assistant and of ``pronotepy``.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

#: Hex digits kept from the digest. Sixty-four bits: collisions between
#: *different* contents are not a concern at a few hundred items per account.
_DIGEST_LENGTH = 16


class _Item(Protocol):
    """A frozen DTO with an identifier and room for PRONOTE's ``N``."""

    @property
    def id(self) -> str:
        """The minted key once stamped; PRONOTE's ``N`` before."""


def mint(kind: str, *parts: object) -> str:
    """Return ``<kind>-<16 hex>`` for one item's identifying content.

    ``parts`` are serialised as JSON with ``str`` as the fallback, so a date and
    its ISO form give the same key, and ``None`` is distinct from ``""``.
    """
    material = json.dumps([kind, *parts], default=str, ensure_ascii=False)
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return f"{kind}-{digest[:_DIGEST_LENGTH]}"


def disambiguate(keys: Iterable[str]) -> list[str]:
    """Make repeated keys distinct, first come first served."""
    seen: dict[str, int] = {}
    result: list[str] = []
    for key in keys:
        count = seen.get(key, 0) + 1
        seen[key] = count
        result.append(key if count == 1 else f"{key}-{count}")
    return result


def restamp[Keyed: _Item](
    items: Sequence[Keyed], key: Callable[[Keyed], str]
) -> tuple[Keyed, ...]:
    """Give every item its minted ``id``, keeping PRONOTE's ``N`` as ``ref``.

    ``items`` are frozen DTOs whose ``id`` is still the session's ``N``.
    """
    keys = disambiguate(key(item) for item in items)
    return tuple(
        dataclasses.replace(item, id=minted, ref=item.id)  # type: ignore[type-var]
        for item, minted in zip(items, keys, strict=True)
    )
