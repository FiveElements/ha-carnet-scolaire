"""The diagnostics download, and what is absent from it by construction.

Redaction is a safety net here, not the mechanism. The mechanism is that the
secrets never enter a snapshot in the first place: the iCal URL, the identity
block and the timetable PDF link are ``SupportsResponse.ONLY`` service results
(§8.2), so there is no state, no attribute and no runtime field holding them
for this file to find.

What *is* in the config entry -- the token, the username, the UUID, the client
identifier -- is redacted explicitly, and the child identifiers are replaced by
a truncated fingerprint rather than removed. A fingerprint is still useful in a
bug report ("both children show the same tier failing") while handing nobody
the material to replay a session (annexe A §7).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from homeassistant.components.diagnostics import async_redact_data

from .child_keys import is_minted
from .const import (
    CHILD_KEY,
    CHILD_NAME,
    CHILD_RESOURCE_ID,
    CONF_ACCOUNT_PIN,
    CONF_CHILD_KEYS,
    CONF_CHILDREN,
    CONF_CLIENT_IDENTIFIER,
    CONF_PRONOTE_URL,
    CONF_QR_PAYLOAD,
    CONF_QR_PIN,
    CONF_UUID,
)
from .urls import public_url, url_host

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from . import PronoteConfigEntry

#: Keys stripped from the config entry. ``pronote_url`` stays: it identifies the
#: establishment, which is exactly what a bug report needs, and it grants
#: nothing on its own -- unlike the iCal URL, which grants read access to a
#: child's timetable and is therefore never stored anywhere (§8.2, §8.4).
TO_REDACT: Final = {
    "username",
    "password",
    CONF_UUID,
    CONF_CLIENT_IDENTIFIER,
    CONF_ACCOUNT_PIN,
    CONF_QR_PAYLOAD,
    CONF_QR_PIN,
    # Belt and braces: the PIN is never persisted (§8.1), so this entry should
    # never match anything. If it ever does, that is the bug.
    "account_pin",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,  # noqa: ARG001 -- required by the diagnostics contract
    entry: PronoteConfigEntry,
) -> dict[str, Any]:
    """Everything useful for a bug report, and nothing that grants access."""
    account = entry.runtime_data

    # The address is re-trimmed here rather than trusted. It is stored trimmed
    # since the config flow started doing that, but an entry created before then
    # -- or restored from a backup, or edited by hand in `.storage` -- can still
    # hold the deep link a school mailed out, complete with its ticket. This
    # file exists so nobody has to notice that before attaching it to a public
    # issue (§8.2).
    data = dict(entry.data)
    if CONF_PRONOTE_URL in data:
        data[CONF_PRONOTE_URL] = public_url(str(data[CONF_PRONOTE_URL]))

    # The child selection, fingerprinted rather than redacted. It escaped both
    # treatments: `TO_REDACT` never listed it, so a dump carried each followed
    # child's PRONOTE resource identifier -- `46#<opaque blob>` -- in full,
    # while the `students` block a few lines below had been reducing the very
    # same identifiers to a fingerprint all along. One file, two policies, and
    # the laxer one won on the half nobody looked at.
    #
    # Fingerprinting rather than dropping, for the reason this module's own
    # docstring gives: "both children show the same tier failing" stays legible
    # if the ids are comparable, and the fingerprints match the `students`
    # block, so the two can be read against each other.
    #
    # Once paired, the selection holds minted keys instead, which stay in clear
    # for the reason the key table below gives; only a value still in the old
    # identifier form is fingerprinted.
    if CONF_CHILDREN in data:
        selection = data[CONF_CHILDREN]
        if isinstance(selection, list):
            data[CONF_CHILDREN] = [
                str(child) if is_minted(str(child)) else _short_hash(str(child))
                for child in selection
            ]

    # The child key table, reduced the same way and for a stronger reason: it
    # is the one structure in the entry that holds a child's **name**, which
    # §8.2 forbids in a file people attach to public issues -- and the name is
    # there precisely because it is how a record is recognised when PRONOTE
    # rotates the resource identifier.
    #
    # The minted key stays in clear. We own it, it grants nothing, and it is
    # what makes the dump legible: it is the value in every `unique_id`, so a
    # report saying "child-2 has no entities" can be acted on. The two
    # fingerprints beside it let a reader see *that* an identifier rotated
    # without being able to replay either.
    if CONF_CHILD_KEYS in data:
        table = data[CONF_CHILD_KEYS]
        if isinstance(table, list):
            data[CONF_CHILD_KEYS] = [
                {
                    CHILD_KEY: record.get(CHILD_KEY),
                    CHILD_RESOURCE_ID: _short_hash(
                        str(record.get(CHILD_RESOURCE_ID, ""))
                    ),
                    CHILD_NAME: _short_hash(str(record.get(CHILD_NAME, ""))),
                }
                for record in table
                if isinstance(record, dict)
            ]

    return {
        "entry": {
            "data": async_redact_data(data, TO_REDACT),
            "options": dict(entry.options),
            "version": entry.version,
            "minor_version": entry.minor_version,
            # Kept unredacted on purpose, see TO_REDACT.
            "url_host": url_host(str(entry.data.get(CONF_PRONOTE_URL, ""))) or None,
        },
        # Assembled by the account itself, which is the only object that knows
        # what is safe to expose -- and which builds it out of counters and
        # scheduler state, never out of PRONOTE payloads.
        "account": account.diagnostics(),
    }


async def async_get_device_diagnostics(
    hass: HomeAssistant,  # noqa: ARG001 -- required by the diagnostics contract
    entry: PronoteConfigEntry,
    device: Any,
) -> dict[str, Any]:
    """Per-device diagnostics: the tier state, not the data.

    Deliberately reports the *shape* of each snapshot -- when it was fetched,
    how many requests it cost, how many items it holds -- and not its content.
    A grade, a teacher's comment on a report card or a message body has no
    business in a file people paste into a public issue.
    """
    account = entry.runtime_data
    # The device carries the *minted* key; the coordinators are keyed by the
    # identifier PRONOTE announced this session. Translating here is what
    # keeps a device stored months ago matched to today's child.
    device_key = _student_id(device, entry.entry_id)
    student_id = (
        account.student_id_for_key(device_key) if device_key is not None else None
    )

    tiers: dict[str, Any] = {}
    for tier, coordinator in account.coordinators.items():
        snapshot = coordinator.snapshot_for(student_id) if student_id else None
        tiers[str(tier)] = {
            "has_data": snapshot is not None,
            "fetched_at": (snapshot.fetched_at.isoformat() if snapshot else None),
            "calls": snapshot.calls if snapshot else None,
            "stale": account.is_stale(tier),
            "last_update_success": coordinator.last_update_success,
            "item_counts": _counts(snapshot.data) if snapshot else None,
        }

    return {
        # The minted key in clear, because we own it and it grants nothing --
        # and the fingerprint of PRONOTE's identifier beside it, because the
        # *pairing* between the two is exactly what one debugs when a child's
        # entities go missing. Neither is replayable, and the fingerprint
        # still matches the `students` block so the two can be read together.
        "child_key": device_key,
        "student_id_hash": _short_hash(student_id) if student_id else None,
        "is_account_device": device_key is None,
        "tiers": tiers,
    }


def _counts(facts: Any) -> dict[str, int]:
    """How many items each collection of a snapshot holds.

    A count answers almost every real question -- "did the timetable come back
    empty?", "are there really no grades?" -- without exposing one word of the
    content.
    """
    counts: dict[str, int] = {}
    for name in (
        "lessons",
        "homework",
        "grades",
        "averages",
        "absences",
        "delays",
        "punishments",
        "evaluations",
        "information",
        "discussions",
        "menus",
        "teaching_staff",
        "periods",
    ):
        value = getattr(facts, name, None)
        if isinstance(value, (list, tuple)):
            counts[name] = len(value)
    return counts


def _student_id(device: Any, entry_id: str) -> str | None:
    """The child a device belongs to, or ``None`` for the account device."""
    for domain_identifier in getattr(device, "identifiers", ()):
        try:
            _domain, identifier = domain_identifier
        except TypeError, ValueError:
            continue
        if not identifier.startswith(entry_id):
            continue
        suffix = str(identifier)[len(entry_id) :]
        if suffix.startswith("_"):
            return suffix[1:]
    return None


def _short_hash(value: str) -> str:
    """A truncated fingerprint, never the identifier itself."""
    from hashlib import blake2s  # noqa: PLC0415 -- only needed here

    return blake2s(value.encode(), digest_size=4).hexdigest()
