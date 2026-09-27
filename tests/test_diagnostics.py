"""The download users are told to attach to a public issue.

``test_no_secret_in_state.py`` already holds the *config entry* half: no token,
no password, no PIN, no deep link. This file holds the half that was untested,
and it is the half that touches the school data itself -- the **per-device**
diagnostics, which walks every tier's latest snapshot.

The promise there is narrower and easier to break. A tier snapshot holds
lessons, grades, teachers' comments and message bodies; the file is supposed to
report only its *shape*: when it was fetched, what it cost, how many items it
holds. Nothing in the type system says so, `async_redact_data` cannot help --
there are no known key names to strip, the content *is* the payload -- and one
plausible, well-meant change ("include a sample so we can see the format")
turns a bug report into a disclosure of a child's timetable.

So the central test here does not check a list of forbidden keys. It reads the
strings the fixtures actually put in the live snapshots and asserts that none of
them comes back out: whatever a future payload happens to contain, that is what
must not be in the file.

The second thing asserted is the distinction between **absent** and **empty**,
which this integration is built on. An empty collection is a fact -- no homework
this week -- and it must be reported as `0`, not omitted; a *missing* attribute
must be omitted rather than reported as `0`, because that is a protocol change
and it has to be visible as one.
"""

from __future__ import annotations

from dataclasses import fields, is_dataclass
import json
from typing import TYPE_CHECKING, Any, ClassVar

import pytest

from custom_components.carnet_scolaire.const import CONF_PRONOTE_URL, DOMAIN
from custom_components.carnet_scolaire.diagnostics import (
    _counts,
    _short_hash,
    _student_id,
    async_get_config_entry_diagnostics,
    async_get_device_diagnostics,
)

from .conftest import CHILDREN, REQUIRES_HASS, child_key

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from custom_components.carnet_scolaire.account import PronoteAccount

ENTRY_ID = "0123456789abcdef0123456789abcdef"


class _Device:
    """A device registry entry, as far as ``diagnostics`` reads one."""

    def __init__(self, *identifiers: Any) -> None:
        self.identifiers = set(identifiers)


class _Facts:
    """A snapshot payload with whatever attributes a test needs."""

    def __init__(self, **attributes: Any) -> None:
        self.__dict__.update(attributes)


# ---------------------------------------------------------------------------
# Which child a device belongs to
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("device", "expected"),
    [
        pytest.param(
            _Device((DOMAIN, f"{ENTRY_ID}_STUDENT-1")),
            "STUDENT-1",
            id="a child device",
        ),
        pytest.param(
            _Device((DOMAIN, ENTRY_ID)),
            None,
            id="the account device, which has no child",
        ),
        pytest.param(
            _Device((DOMAIN, f"{ENTRY_ID}_STUDENT_WITH_UNDERSCORES")),
            "STUDENT_WITH_UNDERSCORES",
            id="only the first underscore is the separator",
        ),
        pytest.param(
            _Device((DOMAIN, "ffffffffffffffffffffffffffffffff_STUDENT-1")),
            None,
            id="another entry's child device",
        ),
        pytest.param(
            _Device((DOMAIN, f"{ENTRY_ID}-STUDENT-1")),
            None,
            id="the right prefix joined the wrong way",
        ),
        pytest.param(_Device(), None, id="a device with no identifier at all"),
        pytest.param(
            _Device((DOMAIN, ENTRY_ID), (DOMAIN, f"{ENTRY_ID}_STUDENT-2")),
            "STUDENT-2",
            id="a child identifier alongside the account's",
        ),
    ],
)
def test_a_device_is_resolved_to_the_child_it_belongs_to(
    device: Any, expected: str | None
) -> None:
    """The per-device file is per *child*, so this resolution is the whole key.

    Getting it wrong does not fail: it silently reports the wrong child's tier
    state, or reports the account device's as a child's, which is exactly the
    confusion a diagnostic file exists to remove.
    """
    assert _student_id(device, ENTRY_ID) == expected


@pytest.mark.parametrize(
    "identifiers",
    [
        pytest.param([42], id="not a pair at all"),
        pytest.param([(DOMAIN, ENTRY_ID, "extra")], id="a triple"),
        pytest.param([(DOMAIN,)], id="a single"),
        pytest.param([None], id="a null"),
    ],
)
def test_a_malformed_identifier_is_skipped_rather_than_fatal(
    identifiers: list[Any],
) -> None:
    """A registry this file does not own can hold shapes it did not write.

    Diagnostics is the code people run *because* something is already wrong.
    Raising here would replace "here is the state of every tier" with "unknown
    error", on the one screen whose purpose is to explain a failure.
    """
    device = _Device()
    device.identifiers = identifiers  # type: ignore[assignment]

    assert _student_id(device, ENTRY_ID) is None


def test_a_malformed_identifier_does_not_hide_the_good_one() -> None:
    """`continue`, not `return`: the loop must survive one bad entry."""
    device = _Device()
    device.identifiers = [42, (DOMAIN, f"{ENTRY_ID}_STUDENT-1")]  # type: ignore[assignment]

    assert _student_id(device, ENTRY_ID) == "STUDENT-1"


# ---------------------------------------------------------------------------
# The fingerprint, which is deliberately not a removal
# ---------------------------------------------------------------------------


def test_the_child_identifier_is_fingerprinted_and_not_merely_dropped() -> None:
    """A hash is more useful than a redaction, and that is the design.

    "Both children show the timetable failing" is answerable from two equal
    fingerprints and unanswerable from two `**REDACTED**`. What must not be
    possible is going the other way: the PRONOTE identifier `N` is what selects
    a child in a request, so the file must not carry it.
    """
    fingerprint = _short_hash("STUDENT-1")

    assert len(fingerprint) == 8
    assert int(fingerprint, 16) >= 0
    assert "STUDENT-1" not in fingerprint
    assert _short_hash("STUDENT-1") == fingerprint
    assert _short_hash("STUDENT-2") != fingerprint


# ---------------------------------------------------------------------------
# Counting, and the absent/empty distinction
# ---------------------------------------------------------------------------


def test_an_empty_collection_is_counted_as_zero_not_omitted() -> None:
    """Zero homework is a fact about the week, not a missing key."""
    assert _counts(_Facts(homework=[], lessons=[1, 2, 3])) == {
        "homework": 0,
        "lessons": 3,
    }


def test_an_attribute_the_payload_does_not_have_is_omitted() -> None:
    """Absent is not zero, and the file must not flatten the two together.

    A tier whose collection disappeared from the protocol shows up here as a
    *missing* key, which is the signal that distinguishes "the establishment
    published nothing" from "PRONOTE renamed the field" -- the whole reason the
    integration fails a tier instead of reporting an empty list.
    """
    counts = _counts(_Facts(lessons=[1]))

    assert counts == {"lessons": 1}
    assert "homework" not in counts


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("a string, which is iterable and must not be counted", id="str"),
        pytest.param(7, id="int"),
        pytest.param({"a": 1}, id="dict"),
        pytest.param(None, id="none"),
    ],
)
def test_only_a_list_or_a_tuple_is_counted(value: Any) -> None:
    """`len()` of a string is a character count, and would be nonsense here.

    Worse than nonsense: a scalar field that became a string would be reported
    as a plausible item count, so a protocol change would read as normal
    operation.
    """
    assert _counts(_Facts(grades=value)) == {}


def test_a_payload_that_is_not_an_object_at_all_counts_nothing() -> None:
    """`getattr` with a default is the whole tolerance, and it is enough."""
    assert _counts(None) == {}
    assert _counts("unexpected") == {}


# ---------------------------------------------------------------------------
# The per-device file, against a fully set-up integration
# ---------------------------------------------------------------------------


def _child_device(account: PronoteAccount, student_id: str) -> _Device:
    """A stand-in for the registry device of one child."""
    return _Device(
        (DOMAIN, f"{account.entry.entry_id}_{child_key(account.entry, student_id)}")
    )


def _descend(value: Any) -> list[Any]:
    """The values worth looking inside, whatever kind of container this is.

    Kept apart from the walk below so the recursion stays one loop: the
    snapshots are frozen slotted dataclasses holding lists of more of the same,
    and `fields()` is the only way in.
    """
    if isinstance(value, (list, tuple, set)):
        return list(value)
    if isinstance(value, dict):
        return list(value.values())
    if is_dataclass(value) and not isinstance(value, type):
        return [getattr(value, field.name, None) for field in fields(value)]
    return []


def _payload_strings(account: PronoteAccount) -> set[str]:
    """Every string the live snapshots actually hold.

    Read out of the snapshots rather than listed by hand, so the sweep below
    keeps testing the real payload as the fixtures grow. Short strings are
    dropped: a two-letter subject code would match half the JSON by accident.
    """
    found: set[str] = set()

    def walk(value: Any, depth: int = 0) -> None:
        if isinstance(value, str):
            if len(value) > 4:
                found.add(value)
            return
        if depth > 4:
            return
        for item in _descend(value):
            walk(item, depth + 1)

    for coordinator in account.coordinators.values():
        for snapshot in (coordinator.data or {}).values():
            walk(snapshot.data)
    return found


@REQUIRES_HASS
async def test_the_per_device_file_reports_the_shape_of_every_tier(
    hass: HomeAssistant, account: PronoteAccount
) -> None:
    """One entry per coordinator, whether or not that tier has run.

    Reported even when empty, because "this tier has never produced anything"
    is the single most useful line in the file -- and a dictionary that only
    listed the tiers that worked could not say it.
    """
    diagnostics = await async_get_device_diagnostics(
        hass, account.entry, _child_device(account, CHILDREN[0][0])
    )

    assert set(diagnostics["tiers"]) == {str(tier) for tier in account.coordinators}
    for state in diagnostics["tiers"].values():
        assert set(state) == {
            "has_data",
            "fetched_at",
            "calls",
            "stale",
            "last_update_success",
            "item_counts",
        }

    # At least one tier really ran, or this test would pass vacuously against a
    # file full of `None`.
    assert any(state["has_data"] for state in diagnostics["tiers"].values())


@REQUIRES_HASS
async def test_the_per_device_file_carries_a_fingerprint_and_not_the_child(
    hass: HomeAssistant, account: PronoteAccount
) -> None:
    """The identifier that selects a child in a request must not be in the file."""
    student_id = CHILDREN[0][0]

    diagnostics = await async_get_device_diagnostics(
        hass, account.entry, _child_device(account, student_id)
    )

    assert diagnostics["student_id_hash"] == _short_hash(student_id)
    assert diagnostics["is_account_device"] is False
    assert student_id not in json.dumps(diagnostics, default=str)


@REQUIRES_HASS
async def test_the_account_device_reports_no_child_and_no_snapshot(
    hass: HomeAssistant, account: PronoteAccount
) -> None:
    """The account device is not a child, and must not borrow one's data.

    `snapshot_for` is skipped entirely when there is no student id, so every
    tier reports `has_data: false` here even though the same tiers are full for
    the children -- which is correct: the account device has no timetable.
    """
    diagnostics = await async_get_device_diagnostics(
        hass, account.entry, _Device((DOMAIN, account.entry.entry_id))
    )

    assert diagnostics["student_id_hash"] is None
    assert diagnostics["is_account_device"] is True
    assert all(not state["has_data"] for state in diagnostics["tiers"].values())
    assert all(state["item_counts"] is None for state in diagnostics["tiers"].values())


@REQUIRES_HASS
async def test_not_one_word_of_the_school_data_reaches_the_file(
    hass: HomeAssistant, account: PronoteAccount
) -> None:
    """The test this file exists for.

    The strings are taken from the snapshots themselves rather than listed, so
    this keeps holding as the fixtures grow: whatever the payload says --
    a subject, a teacher, the text of a piece of homework, a message body -- it
    is not in the download.
    """
    strings = _payload_strings(account)
    assert len(strings) > 10, "the snapshots hold nothing, so this proves nothing"

    for student_id, _name in CHILDREN:
        diagnostics = await async_get_device_diagnostics(
            hass, account.entry, _child_device(account, student_id)
        )
        rendered = json.dumps(diagnostics, default=str)
        leaked = sorted(value for value in strings if value in rendered)
        assert not leaked, f"school data reached the diagnostics: {leaked}"


@REQUIRES_HASS
async def test_the_counts_are_the_real_item_counts(
    hass: HomeAssistant, account: PronoteAccount
) -> None:
    """A count that did not match its snapshot would be worse than no count.

    The number is what a maintainer reasons from -- "there really are no
    grades" -- so it is checked against the snapshot it describes rather than
    merely checked for being an integer.
    """
    student_id = CHILDREN[0][0]
    diagnostics = await async_get_device_diagnostics(
        hass, account.entry, _child_device(account, student_id)
    )

    checked = 0
    for tier, coordinator in account.coordinators.items():
        snapshot = coordinator.snapshot_for(student_id)
        if snapshot is None:
            continue
        state = diagnostics["tiers"][str(tier)]
        assert state["fetched_at"] == snapshot.fetched_at.isoformat()
        assert state["calls"] == snapshot.calls
        for name, count in state["item_counts"].items():
            assert count == len(getattr(snapshot.data, name))
            checked += 1

    assert checked, "no tier produced a countable collection"


@REQUIRES_HASS
async def test_a_tier_that_failed_says_so_while_keeping_its_last_snapshot(
    hass: HomeAssistant, account: PronoteAccount
) -> None:
    """`last_update_success` and `has_data` answer two different questions.

    A failed collection deliberately does not discard the previous snapshot
    (§5.4): entities keep their last known value and report its age. The
    diagnostic file has to show both halves, or "the timetable is stale" and
    "the timetable never worked" look identical.
    """
    student_id = CHILDREN[0][0]
    tier, coordinator = next(
        (tier, coordinator)
        for tier, coordinator in account.coordinators.items()
        if coordinator.snapshot_for(student_id) is not None
    )
    coordinator.note_failure(RuntimeError("the server answered something else"))
    await hass.async_block_till_done()

    diagnostics = await async_get_device_diagnostics(
        hass, account.entry, _child_device(account, student_id)
    )

    state = diagnostics["tiers"][str(tier)]
    assert state["last_update_success"] is False
    assert state["has_data"] is True
    assert "the server answered something else" not in json.dumps(
        diagnostics, default=str
    )


# ---------------------------------------------------------------------------
# The config entry file: the one branch its own suite could not reach
# ---------------------------------------------------------------------------


@REQUIRES_HASS
async def test_an_entry_with_no_address_still_produces_a_diagnostic(
    account: PronoteAccount,
) -> None:
    """`public_url` is only applied when there is something to trim.

    An entry can genuinely lack the key -- one written by an older version, or
    hand-edited in `.storage` -- and the download is the *first* thing asked
    for when an entry misbehaves. Raising `KeyError` here would make the
    malformed entry undiagnosable, which is the only case where it matters.

    The entry is a stand-in rather than the real one put through
    `async_update_entry`: updating a loaded entry reloads it, which would make
    this a test of the reload path and leave the assertion below measuring a
    freshly built account instead of the branch it names. `runtime_data` is the
    genuine account, so the `account` half of the file is still real.
    """

    class _Entry:
        data: ClassVar[dict[str, Any]] = {"username": "parent-under-test"}
        options: ClassVar[dict[str, Any]] = {}
        version = 1
        minor_version = 1
        runtime_data = account

    diagnostics = await async_get_config_entry_diagnostics(
        account.hass,  # type: ignore[arg-type]
        _Entry(),  # type: ignore[arg-type]
    )

    assert CONF_PRONOTE_URL not in diagnostics["entry"]["data"]
    assert diagnostics["entry"]["url_host"] is None
    assert diagnostics["account"]["students"]


@REQUIRES_HASS
async def test_the_followed_child_selection_is_fingerprinted_like_the_rest(
    hass: HomeAssistant, account: PronoteAccount
) -> None:
    """One file, and it had two policies -- the laxer one on the unread half.

    ``entry.data["children"]`` holds each followed child's PRONOTE resource
    identifier, written ``46#<opaque blob>``. It appeared in a real download in
    full: ``TO_REDACT`` never listed it, while the ``students`` block a few
    lines below had been reducing the very same identifiers to a fingerprint
    all along.

    That identifier is what selects a child in a request, and this file exists
    to be attached to a public issue (§8.2). Fingerprinted rather than dropped,
    so it still matches the ``students`` block and the two can be read against
    each other -- which is the whole argument this module makes for hashing
    instead of redacting.
    """
    entry = account.entry
    # A selection still in the identifier form, as an entry holds it until its
    # first pairing -- once paired it holds minted keys, which stay in clear.
    hass.config_entries.async_update_entry(
        entry,
        data={**entry.data, "children": [child_id for child_id, _ in CHILDREN]},
    )
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    selection = diagnostics["entry"]["data"]["children"]
    fingerprints = [_short_hash(child_id) for child_id, _ in CHILDREN]

    assert selection == fingerprints
    dumped = json.dumps(diagnostics)
    for child_id, _name in CHILDREN:
        assert child_id not in dumped


@REQUIRES_HASS
async def test_a_selection_of_minted_keys_is_shown_in_clear(
    hass: HomeAssistant, account: PronoteAccount
) -> None:
    """The key is ours and grants nothing, so hashing it only hides the answer.

    Once paired, the selection is stored as the keys every ``unique_id``
    carries; a report reading ``children: ["child-1"]`` says which device is
    followed, where a fingerprint would say nothing a reader could act on.
    """
    diagnostics = await async_get_config_entry_diagnostics(hass, account.entry)

    assert diagnostics["entry"]["data"]["children"] == [
        child_key(account.entry, child_id) for child_id, _ in CHILDREN
    ]


@REQUIRES_HASS
async def test_whether_a_child_has_a_photo_is_reported(
    hass: HomeAssistant, account: PronoteAccount
) -> None:
    """ "No photo entity" and "the flag is misread" looked identical.

    ``image.async_setup_entry`` creates an entity only for a child whose
    ``has_photo`` is true, and that flag is read from one key of
    ``parametres_utilisateur``. On a live account no photo entity appeared, and
    there was no way to tell a school that publishes no photos -- a legitimate
    setting -- from a renamed key, which is a bug. The boolean is a boolean:
    reporting it costs nothing and settles the question from the download.
    """
    diagnostics = await async_get_config_entry_diagnostics(hass, entry=account.entry)

    students = diagnostics["account"]["students"]

    assert students
    for student in students:
        assert isinstance(student["has_photo"], bool)
