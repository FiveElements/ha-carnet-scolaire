"""Stable identities for the children of one account.

PRONOTE writes a child's resource identifier as ``46#<signature>``, and that
signature is **not stable between sessions**. Three distinct values were
observed for a single pupil on a single account in one day. Because an
entity's ``unique_id`` embedded that identifier, a rotation was
indistinguishable from the arrival of a new child: a second device appeared,
a full set of entities was created against it, and the previous set was
orphaned in the registry for ever -- every dashboard, automation and helper
pointing at it dead, with nothing logged anywhere.

So the identifier the rest of the integration uses is one **we mint**, and the
mapping from PRONOTE's rotating identifier to ours is stored in the config
entry. That mapping table is the durable artefact; the key is merely what it
contains. Re-pairing on every set-up is not a migration step that runs once,
it is the normal mode of operation -- which is why it lives here rather than
in a migration.

**Why not the name.** It is the obvious alternative and it is wrong for the
same reason: an establishment corrects a spelling, a family changes name, a
school switches from ``DUPONT Marie`` to ``Marie DUPONT``. That moves the
problem rather than removing it. The name is used only as the *second* way to
recognise a record whose identifier has rotated -- never as the key.

**Why not a position in the list.** §2.4 forbids it outright, and rightly: a
sibling added or removed would renumber everybody.

This module is deliberately free of Home Assistant and of ``pronotepy``: it is
a pure function over two lists, so it can be tested exhaustively and runs in
the HA-free half of the suite.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Final

from .const import CHILD_KEY, CHILD_NAME, CHILD_RESOURCE_ID

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

#: One stored record, as it sits in the config entry.
type Record = dict[str, str]

#: A minted key. Readable on purpose -- it appears in every ``unique_id`` of
#: the child's entities and in diagnostics, and "child-2" is far easier to
#: reason about in a bug report than an opaque hash. It carries nothing from
#: PRONOTE, so unlike the identifier it replaces it is safe to publish.
_KEY_FORMAT: Final = "child-{number}"

#: Reads the number back out of a minted key, to allocate the next one.
_KEY_NUMBER: Final = re.compile(r"^child-(\d+)$")


def _next_key(existing: Sequence[Record]) -> str:
    """Mint a key no record in this table has ever used.

    One above the highest ever allocated, rather than "length plus one":
    records are never deleted, but if one ever were, counting would re-issue a
    key that an orphaned registry row still carries -- and the registry would
    then re-point that row at the new child instead of rejecting it.
    """
    used = [
        int(match.group(1))
        for record in existing
        if (match := _KEY_NUMBER.match(record.get(CHILD_KEY, "")))
    ]
    return _KEY_FORMAT.format(number=max(used, default=0) + 1)


def is_minted(value: str) -> bool:
    """Whether ``value`` is one of our keys rather than PRONOTE's identifier.

    The registry repair needs it to tell a row it has already fixed from a row
    still carrying a rotated identifier -- including the key of a child who has
    since left the account, which is still ours and must never be re-pointed at
    somebody else.
    """
    return _KEY_NUMBER.match(value) is not None


def pair(
    stored: Sequence[Mapping[str, str]],
    announced: Sequence[tuple[str, str]],
) -> tuple[dict[str, str], list[Record], list[str]]:
    """Match the children the account announces against what we have seen.

    ``announced`` is ``(resource_id, name)`` in the order PRONOTE gave them.
    Returns the mapping the entities need, the table to store, and a list of
    human-readable notes about anything that moved -- so a rotation is logged
    once, plainly, instead of being absorbed in silence as it was before.

    Matching is by resource identifier first, because while it holds it is
    exact. Only children it does not place are then matched by name, and only
    against records that no announced child has already claimed: without that
    restriction two siblings sharing a name -- twins, which a school does not
    disambiguate -- could both claim the same record and collapse onto one key.
    Anything still unplaced is genuinely new and gets a fresh key.
    """
    records: list[Record] = [dict(record) for record in stored]
    by_resource = {
        record[CHILD_RESOURCE_ID]: record
        for record in records
        if record.get(CHILD_RESOURCE_ID)
    }

    keys: dict[str, str] = {}
    claimed: set[str] = set()
    notes: list[str] = []
    unplaced: list[tuple[str, str]] = []

    for resource_id, name in announced:
        record = by_resource.get(resource_id)
        if record is None:
            unplaced.append((resource_id, name))
            continue
        claimed.add(record[CHILD_KEY])
        keys[resource_id] = record[CHILD_KEY]
        if record.get(CHILD_NAME) != name:
            notes.append(
                f"{record[CHILD_KEY]} is now announced as a different name; "
                f"the stored one is updated and no entity changes"
            )
            record[CHILD_NAME] = name

    for resource_id, name in unplaced:
        match = next(
            (
                record
                for record in records
                if record.get(CHILD_NAME) == name and record[CHILD_KEY] not in claimed
            ),
            None,
        )
        if match is None:
            record = {
                CHILD_KEY: _next_key(records),
                CHILD_RESOURCE_ID: resource_id,
                CHILD_NAME: name,
            }
            records.append(record)
            claimed.add(record[CHILD_KEY])
            keys[resource_id] = record[CHILD_KEY]
            notes.append(f"{record[CHILD_KEY]} is a child never seen before")
            continue
        notes.append(
            f"{match[CHILD_KEY]} kept its identity across a changed PRONOTE "
            f"resource identifier, recognised by name; its entities are "
            f"untouched"
        )
        match[CHILD_RESOURCE_ID] = resource_id
        claimed.add(match[CHILD_KEY])
        keys[resource_id] = match[CHILD_KEY]

    return keys, records, notes


def is_legacy_selection(selection: Sequence[str]) -> bool:
    """Whether a stored selection still names PRONOTE identifiers.

    The followed children used to be stored as the resource identifiers
    PRONOTE announced at configuration time -- the very values that rotate. A
    selection holding any value that is not one of our keys is in that form,
    and :func:`followed` translates it once.
    """
    return any(not is_minted(value) for value in selection)


def followed(
    stored: Sequence[Mapping[str, str]],
    table: Sequence[Mapping[str, str]],
    selection: Sequence[str],
    keys: Mapping[str, str],
) -> list[str]:
    """The minted keys of the children to follow, in table order.

    ``stored`` is the key table before this pairing; ``table`` and ``keys``
    are what :func:`pair` returned for *every* announced child, followed or
    not -- a child the user chose not to follow must still be recognised after
    a rotation, or it would look new and be adopted. ``selection`` is the
    stored choice.

    The table records every child the account ever announced, so the choice
    reads in two parts: a key in ``selection`` is followed, a key in the table
    but not in ``selection`` was declined. A key this pairing has just minted
    is a child nobody was ever asked about, and *unknown is not refused*: it
    is followed. An empty selection follows everybody, as it always has.

    A selection still in the legacy form -- resource identifiers -- is
    translated through the identifier each record held *before* this pairing
    replaced it, or directly through the roster when nothing has rotated since
    it was written. In that form a child absent from the selection was
    declined in the flow, so a key minted now is *not* followed. The one
    exception is a selection of which nothing can be placed at all: the list
    had gone stale, the account was already following every child to recover,
    and it keeps doing so. That is an entry whose list grew by one identifier
    per login; its choice cannot be read back out of identifiers that no
    longer exist, and following what it already followed is the only answer
    that changes nothing the user can see.
    """
    every = [record[CHILD_KEY] for record in table]
    if not selection:
        return every
    if not is_legacy_selection(selection):
        known = {record[CHILD_KEY] for record in stored}
        chosen = set(selection) | {key for key in every if key not in known}
        return [key for key in every if key in chosen]

    by_resource = {
        record[CHILD_RESOURCE_ID]: record[CHILD_KEY]
        for record in stored
        if record.get(CHILD_RESOURCE_ID)
    }
    placed: set[str] = set()
    for value in selection:
        key = value if is_minted(value) else keys.get(value) or by_resource.get(value)
        if key is not None:
            placed.add(key)
    if not placed:
        return every
    return [key for key in every if key in placed]
