"""The key minted for every item, and why PRONOTE's ``N`` cannot be one.

Measured on a live instance on 2026-09-26: the same homework item was
``147#<A>`` in the session opened before 22:57 and ``147#<B>`` in the one
opened after it. PRONOTE re-encrypts every ``N`` at each login. Two defects
followed on the same evening, and each test below exists to keep one of them
from coming back:

- a tick sent with a snapshot's ``N`` after a reconnection was accepted by
  PRONOTE and recorded nowhere;
- the first collection in the new session announced the whole school year as
  new homework -- fifty-four events in one millisecond.

The fake serves the same content under a different ``N`` to stand for the
reconnection, which is exactly what the server did.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import re
from typing import TYPE_CHECKING, Any

import pytest

from custom_components.carnet_scolaire import item_keys
from custom_components.carnet_scolaire.delta import DeltaDetector
from custom_components.carnet_scolaire.models import Homework

from .fixtures import protocol
from .fixtures.client import FakeThread

if TYPE_CHECKING:
    from custom_components.carnet_scolaire.gateway import PronoteGateway

    from .fixtures.client import FakeClient

STUDENT = "STUDENT-1"


# ---------------------------------------------------------------------------
# The three functions
# ---------------------------------------------------------------------------


def test_a_key_is_its_kind_and_sixteen_hex_digits() -> None:
    """The shape the cards were told, and nothing a URL or a `#` could break."""
    key = item_keys.mint("hw", "Histoire", "2026-03-16", "Lire le chapitre 4")

    assert re.fullmatch(r"hw-[0-9a-f]{16}", key)


def test_the_same_content_mints_the_same_key_and_different_content_another() -> None:
    """Deterministic, or it is no better than the ``N`` it replaces."""
    one = item_keys.mint("hw", "Histoire", "Lire le chapitre 4")

    assert item_keys.mint("hw", "Histoire", "Lire le chapitre 4") == one
    assert item_keys.mint("hw", "Histoire", "Lire le chapitre 5") != one
    assert item_keys.mint("grade", "Histoire", "Lire le chapitre 4") != one


def test_an_absent_field_is_not_the_same_as_an_empty_one() -> None:
    """``None`` and ``""`` are two different contents, so two different keys."""
    assert item_keys.mint("hw", None) != item_keys.mint("hw", "")


def test_repeated_keys_are_numbered_in_order_of_appearance() -> None:
    """The first keeps the bare key, so adding a twin never renames the first."""
    assert item_keys.disambiguate(["a", "a", "b", "a"]) == ["a", "a-2", "b", "a-3"]


def test_restamping_keeps_the_sessions_identifier_as_ref() -> None:
    """The ``N`` is still needed -- inside the session -- to name an item back."""
    item = Homework(
        id="HOMEWORK-1",
        subject="Histoire",
        description="",
        description_text="",
        due=dt.date(2026, 3, 16),
        done=False,
        background_color=None,
    )

    (stamped,) = item_keys.restamp([item], lambda _: "hw-0123456789abcdef")

    assert stamped.id == "hw-0123456789abcdef"
    assert stamped.ref == "HOMEWORK-1"
    assert dataclasses.replace(stamped, id=item.id, ref=None) == item


# ---------------------------------------------------------------------------
# Through the gateway: one content, two sessions
# ---------------------------------------------------------------------------


def _serve(client: FakeClient, function: str, payload: dict[str, Any]) -> None:
    client.responses[function] = payload


_COLLECTIONS = [
    pytest.param(
        "PageCahierDeTexte",
        lambda n: protocol.homework_response([protocol.homework(identifier=n)]),
        lambda gateway, client: gateway.homework(client).facts.homework,
        id="homework",
    ),
    pytest.param(
        "DernieresNotes",
        lambda n: protocol.marks_response(grades=[protocol.grade(identifier=n)]),
        lambda gateway, client: (
            gateway.marks(
                client,
                gateway.session_facts(client).facts.current_period,
                with_report=False,
            ).facts.grades
        ),
        id="grades",
    ),
    pytest.param(
        "PageActualites",
        lambda n: protocol.news_response([protocol.information(identifier=n)]),
        lambda gateway, client: gateway.news(client).facts.information,
        id="news",
    ),
    pytest.param(
        "PagePresence",
        lambda n: protocol.attendance_response([protocol.absence(identifier=n)]),
        lambda gateway, client: (
            gateway.attendance(
                client, gateway.session_facts(client).facts.current_period
            ).facts.absences
        ),
        id="absences",
    ),
    pytest.param(
        "PageEmploiDuTemps",
        lambda n: protocol.timetable_response([protocol.lesson(identifier=n)]),
        lambda gateway, client: gateway.timetable(client).facts.all_lessons,
        id="lessons",
    ),
]


@pytest.mark.parametrize(("function", "payload", "collect"), _COLLECTIONS)
def test_an_item_keeps_its_key_when_the_session_renames_it(
    gateway: PronoteGateway,
    client: FakeClient,
    function: str,
    payload: Any,
    collect: Any,
) -> None:
    """The whole point: one content, two ``N``, one key."""
    # The first item only: the fake serves one timetable page per week asked
    # for, so a Saturday collects the same entry twice.
    _serve(client, function, payload("N-BEFORE-THE-LOGIN"))
    before = collect(gateway, client)[0]
    _serve(client, function, payload("N-AFTER-THE-LOGIN"))
    after = collect(gateway, client)[0]

    assert after.id == before.id
    assert (before.ref, after.ref) == ("N-BEFORE-THE-LOGIN", "N-AFTER-THE-LOGIN")


def test_a_thread_keeps_its_key_when_the_session_renames_it(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """Threads are keyed before any DTO exists, so they get their own test."""
    client.threads = [FakeThread("T-BEFORE")]
    (before,) = gateway.discussions(client).facts.discussions
    client.threads = [FakeThread("T-AFTER")]
    (after,) = gateway.discussions(client).facts.discussions

    assert after.id == before.id
    assert after.ref == "T-AFTER"


def test_a_reconnection_does_not_make_every_unread_thread_newly_active(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """Keyed on the ``N``, each login re-read every thread with an unread message.

    That spends one request per thread up to the cap, and hands the detector
    messages it then reads as new.
    """
    client.threads = [FakeThread("T-BEFORE", unread=1)]
    first = gateway.discussions(client).facts
    previous = {thread.id: thread.unread for thread in first.discussions}
    client.threads = [FakeThread("T-AFTER", unread=1)]

    result = gateway.discussions(client, previous_unread=previous)

    assert result.facts.expanded == frozenset()
    assert result.calls == 1


def test_ticking_an_item_does_not_change_its_key(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """``done`` must stay out of the key, or a tick would rename what it ticked."""
    _serve(
        client, "PageCahierDeTexte", protocol.homework_response([protocol.homework()])
    )
    (open_item,) = gateway.homework(client).facts.homework
    _serve(
        client,
        "PageCahierDeTexte",
        protocol.homework_response([protocol.homework(done=True)]),
    )
    (ticked,) = gateway.homework(client).facts.homework

    assert ticked.done is True
    assert ticked.id == open_item.id


def test_two_identical_assignments_get_two_keys_in_a_stable_order(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """ "Apporter son workbook", set twice for the same day, is two items.

    One key for both would put two to-do items under one ``uid`` and make the
    tick ambiguous; the ordinal keeps them apart, the same way every session.
    """
    twins = [
        protocol.homework(identifier="N-1", description="Apporter son workbook"),
        protocol.homework(identifier="N-2", description="Apporter son workbook"),
    ]
    _serve(client, "PageCahierDeTexte", protocol.homework_response(twins))
    first, second = gateway.homework(client).facts.homework

    assert second.id == f"{first.id}-2"


def test_a_rewritten_statement_is_a_new_key(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """Accepted on purpose: for a card or an automation it is another assignment."""
    _serve(
        client, "PageCahierDeTexte", protocol.homework_response([protocol.homework()])
    )
    (before,) = gateway.homework(client).facts.homework
    _serve(
        client,
        "PageCahierDeTexte",
        protocol.homework_response(
            [protocol.homework(description="Lire le chapitre 4 et le 5")]
        ),
    )
    (after,) = gateway.homework(client).facts.homework

    assert after.id != before.id


def test_a_reconnection_announces_no_homework(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """Fifty-four ``new_homework`` events in one millisecond, measured on 0.1.4.

    The detector compared identifiers, and every identifier had changed.
    """
    detector = DeltaDetector()
    year = [
        protocol.homework(identifier=f"N-{index}", description=f"Exercice {index}")
        for index in range(54)
    ]
    _serve(client, "PageCahierDeTexte", protocol.homework_response(year))
    detector.homework(STUDENT, gateway.homework(client).facts)
    for entry in year:
        entry["N"] = f"{entry['N']}-AFTER-THE-LOGIN"
    _serve(client, "PageCahierDeTexte", protocol.homework_response(year))

    assert detector.homework(STUDENT, gateway.homework(client).facts) == []
