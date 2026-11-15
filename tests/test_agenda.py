"""The establishment's agenda: decoding, cost and change detection.

``PageAgenda`` is a tab pronotepy never reads, so every field here is decoded
by this integration from a shape described by the official client's
deserialiser, not captured. The tests therefore lean on the tolerant half:
an absent field becomes ``None``, an entry that cannot be dated is skipped,
and an all-day entry's last day is **inclusive** in PRONOTE and exclusive
everywhere this integration publishes it.

No network, no Home Assistant.
"""

import datetime as dt
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import pytest

from custom_components.carnet_scolaire.const import (
    DEFAULT_TIER_INTERVALS,
    EVENT_SCHOOL_EVENT_ADDED,
    TIER_PRIORITY,
    Priority,
    Tier,
)
from custom_components.carnet_scolaire.delta import DeltaDetector
from custom_components.carnet_scolaire.gateway import (
    PronoteGateway,
    ProtocolChanged,
    _hex_color,
    _is_holiday_family,
)
from custom_components.carnet_scolaire.options import REQUESTS_PER_BATCH

from .fixtures import protocol

if TYPE_CHECKING:
    from custom_components.carnet_scolaire.models import AgendaFacts

    from .fixtures.client import FakeClient

PARIS = ZoneInfo("Europe/Paris")


def _decode(
    gateway: PronoteGateway, client: FakeClient, *entries: dict[str, object]
) -> AgendaFacts:
    """Serve these entries and decode them."""
    client.responses["PageAgenda"] = protocol.agenda_response(list(entries))
    return gateway.agenda(client).facts


# ---------------------------------------------------------------------------
# The request
# ---------------------------------------------------------------------------


def test_the_agenda_is_one_signed_post_on_tab_nine(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """The declared cost is the request count, and the body is the app's.

    One ``client.post`` -- the path that stamps ``membre`` on a parent account
    -- with the official client's body minus ``NumeroSemaine``.
    """
    result = gateway.agenda(client)

    assert result.calls == 1
    assert client.posts == [
        (
            "PageAgenda",
            9,
            {
                "avecEventsPasses": False,
                "listeFamillesFiltre": None,
                "uniquementMesEvenements": False,
                "avecRdvPartages": True,
                "AvecListeClasses": False,
            },
        )
    ]


def test_on_a_parent_account_the_agenda_is_read_for_the_selected_child(
    gateway: PronoteGateway, parent_client: FakeClient
) -> None:
    """The request follows the child selection, like every other tab.

    The gateway itself selects nothing; it must post *after* the session's
    selection, through the signed path, and never through
    ``communication.post`` -- which carries no ``membre`` at all.
    """
    second = parent_client.children[1].id
    parent_client.set_child(second)

    gateway.agenda(parent_client)

    assert parent_client.journal[-2:] == [("select", second), ("post", "PageAgenda")]
    assert parent_client.communication_posts == []


def test_the_agenda_tier_is_daily_low_priority_and_costs_one_request() -> None:
    """The contract the cards were built against: 24 h, ``low``, 1 per child."""
    assert DEFAULT_TIER_INTERVALS[Tier.AGENDA] == 1440
    assert TIER_PRIORITY[Tier.AGENDA] is Priority.LOW
    assert REQUESTS_PER_BATCH[Tier.AGENDA] == 1.0


def test_an_agenda_response_without_its_list_fails_the_tier(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """A renamed list must keep yesterday's agenda, not publish an empty one."""
    client.responses["PageAgenda"] = {"dataSec": {"data": {"listeFamilles": []}}}

    with pytest.raises(ProtocolChanged):
        gateway.agenda(client)


def test_an_empty_agenda_section_is_an_empty_agenda(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """An authorised tab answering an empty ``data`` publishes nothing.

    The teaching staff taught this one: a live server answers that way, and
    failing turned it into an entity that could never become available.
    """
    client.responses["PageAgenda"] = {"dataSec": {"data": {}}}

    result = gateway.agenda(client)

    assert result.facts.events == ()
    assert result.calls == 1


# ---------------------------------------------------------------------------
# Decoding one entry
# ---------------------------------------------------------------------------


def test_an_all_day_entry_ends_at_the_midnight_after_its_last_day(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """PRONOTE's "du 20 décembre au 3 janvier" includes the 3rd.

    Read as exclusive, the holidays would end a day early and the morning
    of the 3rd would look like a school day.
    """
    facts = _decode(
        gateway,
        client,
        protocol.school_event(
            title="Vacances de Noël",
            start=dt.datetime(2026, 12, 20, 0, 0),
            end=dt.datetime(2027, 1, 3, 0, 0),
            family=protocol.HOLIDAY_FAMILY,
        ),
    )

    (event,) = facts.events
    assert event.all_day
    assert event.start == dt.datetime(2026, 12, 20, tzinfo=PARIS)
    assert event.end == dt.datetime(2027, 1, 4, tzinfo=PARIS)
    assert event.iso_bounds() == ("2026-12-20", "2027-01-04")
    assert event.is_holiday
    assert event.category == protocol.HOLIDAY_FAMILY


def test_a_single_day_entry_lasts_exactly_that_day(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """ "Arrêt des notes" on the 20th: from that midnight to the next."""
    (event,) = _decode(gateway, client, protocol.school_event()).events

    assert event.title == "Arrêt des notes - trimestre 2"
    assert event.iso_bounds() == ("2026-03-20", "2026-03-21")
    assert not event.is_holiday
    assert not event.is_class_council


def test_a_timed_class_council_keeps_its_hours_in_the_establishment_s_zone(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """PRONOTE writes naive local time; the instant is Paris time, offset kept."""
    (event,) = _decode(
        gateway,
        client,
        protocol.school_event(
            title="Conseil de classe T2 5E1",
            start=dt.datetime(2026, 3, 24, 17, 15),
            end=dt.datetime(2026, 3, 24, 18, 30),
            all_day=False,
            council=True,
            family="Conseils de classe",
        ),
    ).events

    assert not event.all_day
    assert event.is_class_council
    assert event.start == dt.datetime(2026, 3, 24, 17, 15, tzinfo=PARIS)
    assert event.iso_bounds() == (
        "2026-03-24T17:15:00+01:00",
        "2026-03-24T18:30:00+01:00",
    )


def test_a_comment_written_in_html_is_published_as_text(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """A teacher's rich text, without the tags a card would print aloud."""
    (event,) = _decode(
        gateway,
        client,
        protocol.school_event(comment="<div>Salle&nbsp;polyvalente<br>Parents</div>"),
    ).events

    assert event.description == "Salle polyvalente\nParents"


def test_a_place_and_an_audience_are_read_whatever_their_shape(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """``lieu`` as a typed element, the audience as a list of labels."""
    (event,) = _decode(
        gateway,
        client,
        protocol.school_event(
            place={"_T": 24, "V": {"N": "PLACE-1", "L": "Gymnase"}},
            audience=("Enfant Un", "5E1", "Enfant Un"),
        ),
    ).events

    assert event.location == "Gymnase"
    assert event.audience == "Enfant Un, 5E1"


def test_a_place_written_as_plain_text_is_kept(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """The deserialiser copies ``lieu`` as it comes; a string stays a string."""
    (event,) = _decode(gateway, client, protocol.school_event(place="CDI")).events

    assert event.location == "CDI"


def test_every_optional_field_may_be_absent(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """No title, no end, no family, no colour, no comment: still an entry.

    A missing end is the start; nothing is invented for the rest.
    """
    (event,) = _decode(
        gateway,
        client,
        protocol.school_event(
            title=None,
            end=None,
            all_day=False,
            start=dt.datetime(2026, 3, 25, 10, 0),
            family=None,
            color=None,
        ),
    ).events

    assert event.title == ""
    assert event.end == event.start
    assert event.category is None
    assert event.color is None
    assert event.description is None
    assert event.location is None
    assert event.audience is None
    assert not event.is_holiday


def test_an_end_before_its_start_is_brought_back_to_the_start(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """Both kinds of entry: an inverted pair must not make a negative event."""
    timed, all_day = _decode(
        gateway,
        client,
        protocol.school_event(
            identifier="AGENDA-1",
            start=dt.datetime(2026, 3, 25, 10, 0),
            end=dt.datetime(2026, 3, 25, 9, 0),
            all_day=False,
        ),
        protocol.school_event(
            identifier="AGENDA-2",
            start=dt.datetime(2026, 3, 25, 0, 0),
            end=dt.datetime(2026, 3, 24, 0, 0),
        ),
    ).events

    assert timed.end == timed.start
    assert all_day.iso_bounds() == ("2026-03-25", "2026-03-26")


def test_an_entry_that_cannot_be_dated_is_skipped_and_the_rest_kept(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """One bad entry costs that entry, never the tier."""
    facts = _decode(
        gateway,
        client,
        protocol.school_event(identifier="AGENDA-1", start=None),
        {"N": "AGENDA-2", "L": "Date illisible", "DateDebut": "demain"},
        "not an entry",  # type: ignore[arg-type]
        protocol.school_event(identifier="AGENDA-3"),
    )

    assert [event.ref for event in facts.events] == ["AGENDA-3"]


def test_the_family_label_is_found_in_the_family_list_when_the_entry_omits_it(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """An entry may name its family by ``N`` only; the response lists the rest."""
    entry = protocol.school_event(family=None, color=None)
    entry["famille"] = {"N": "FAMILY-7"}
    client.responses["PageAgenda"] = protocol.agenda_response(
        [entry], families=[("FAMILY-7", "Jours fériés")]
    )

    (event,) = gateway.agenda(client).facts.events

    assert event.category == "Jours fériés"
    assert event.is_holiday
    assert event.color == "#aabbcc"


def test_the_entry_s_own_colour_wins_over_its_family_s(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """``CouleurCellule`` first, the family's ``couleur`` when it is absent."""
    own, inherited = _decode(
        gateway,
        client,
        protocol.school_event(identifier="AGENDA-1", color="#3A6EA5"),
        protocol.school_event(identifier="AGENDA-2", color=None),
    ).events

    assert own.color == "#3a6ea5"
    assert inherited.color == "#999999"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("#3A6EA5", "#3a6ea5"),
        ("3a6ea5", "#3a6ea5"),
        ("#AbC", "#aabbcc"),
        (" #abcdef ", "#abcdef"),
        ("#abcd", None),
        ("rgb(1,2,3)", None),
        ("", None),
        (None, None),
        (12, None),
    ],
)
def test_a_colour_is_published_as_six_lower_case_digits_or_not_at_all(
    raw: object, expected: str | None
) -> None:
    """A card puts it in a style unchecked, so only one spelling gets through."""
    assert _hex_color(raw) == expected


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("Vacances et jours fériés", True),
        ("VACANCES SCOLAIRES", True),
        ("Jour ferie", True),
        ("Vie de l'établissement", False),
        ("", False),
        (None, False),
    ],
)
def test_holidays_are_recognised_by_their_family_s_words(
    label: str | None, expected: bool
) -> None:
    """Case and accents ignored: the family carries no other marker."""
    assert _is_holiday_family(label) is expected


def test_the_key_survives_a_new_session_and_never_contains_the_n(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """``N`` is re-encrypted at every login; the key is title plus bounds."""
    before = _decode(gateway, client, protocol.school_event(identifier="AGENDA-1"))
    after = _decode(gateway, client, protocol.school_event(identifier="AGENDA-99"))

    assert before.events[0].id == after.events[0].id
    assert before.events[0].id.startswith("agenda-")
    assert "AGENDA" not in before.events[0].id
    assert after.events[0].ref == "AGENDA-99"


# ---------------------------------------------------------------------------
# Change detection
# ---------------------------------------------------------------------------


def _facts(gateway: PronoteGateway, client: FakeClient, *titles: str) -> AgendaFacts:
    """One all-day entry per title, on consecutive days, under rotating ``N``."""
    return _decode(
        gateway,
        client,
        *(
            protocol.school_event(
                identifier=f"AGENDA-{index}-{id(titles)}",
                title=title,
                start=dt.datetime(2026, 3, 20 + index, 0, 0),
                end=dt.datetime(2026, 3, 20 + index, 0, 0),
            )
            for index, title in enumerate(titles)
        ),
    )


def test_the_first_agenda_announces_nothing(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """A restart must not replay the year's agenda as news."""
    detector = DeltaDetector()

    assert detector.agenda("STUDENT-1", _facts(gateway, client, "Un", "Deux")) == []


def test_a_new_entry_is_announced_once_with_what_an_automation_reads(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """One event per new entry, bounds published like the sensor's."""
    detector = DeltaDetector()
    detector.agenda("STUDENT-1", _facts(gateway, client, "Un"))

    events = detector.agenda("STUDENT-1", _facts(gateway, client, "Un", "Deux"))

    (event,) = events
    assert event.entity_key == "new_school_event"
    assert event.event_type == EVENT_SCHOOL_EVENT_ADDED
    assert event.attributes == {
        "key": event.attributes["key"],
        "title": "Deux",
        "start": "2026-03-21",
        "end": "2026-03-22",
        "all_day": True,
        "category": "Vie de l'établissement",
        "is_class_council": False,
        "is_holiday": False,
    }
    assert event.attributes["key"].startswith("agenda-")
    assert detector.agenda("STUDENT-1", _facts(gateway, client, "Un", "Deux")) == []


def test_rotated_identifiers_announce_nothing(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """The same entries under fresh ``N`` after a reconnection are not new."""
    detector = DeltaDetector()
    detector.agenda("STUDENT-1", _facts(gateway, client, "Un", "Deux"))
    rotated = _decode(
        gateway,
        client,
        *(
            protocol.school_event(
                identifier=f"ROTATED-{index}",
                title=title,
                start=dt.datetime(2026, 3, 20 + index, 0, 0),
                end=dt.datetime(2026, 3, 20 + index, 0, 0),
            )
            for index, title in enumerate(("Un", "Deux"))
        ),
    )

    assert detector.agenda("STUDENT-1", rotated) == []


def test_an_entry_that_leaves_the_agenda_announces_nothing(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """Past entries drop out of the response; that is not a change to report."""
    detector = DeltaDetector()
    detector.agenda("STUDENT-1", _facts(gateway, client, "Un", "Deux"))

    assert detector.agenda("STUDENT-1", _facts(gateway, client, "Un")) == []


def test_the_debug_log_describes_the_response_without_any_of_its_data(
    gateway: PronoteGateway, client: FakeClient, caplog: pytest.LogCaptureFixture
) -> None:
    """The family of a real agenda entry could not be found from its decoder.

    The decoder was written from the official client, never from a capture,
    and on a live instance every entry came back without a category -- so
    holidays were not recognised. The debug line says what the response
    really looks like: key names, value types and type codes, and nothing a
    capture would leak -- no title, no date, no identifier.
    """
    caplog.set_level("DEBUG", logger="custom_components.carnet_scolaire.gateway")
    entry = protocol.school_event(
        title="Arret des notes - DEMO-TITLE", family="Vacances DEMO"
    )
    client.responses["PageAgenda"] = protocol.agenda_response(
        [entry], families=(("FAM-1", "Famille DEMO"),)
    )

    gateway.agenda(client)

    (record,) = [r for r in caplog.records if "agenda response keys" in r.getMessage()]
    message = record.getMessage()
    assert "ListeEvenements" in message
    assert "'DateDebut'" in message
    assert "'famille'" in message
    for leaked in ("DEMO-TITLE", "DEMO", "FAM-1", "2026"):
        assert leaked not in message
