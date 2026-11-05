"""Mapping contract from EcoleDirecte payloads to shared DTOs."""

# The plan's tests are copied verbatim, including its intentionally long names.
# ruff: noqa: E501

import inspect
import json
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from custom_components.carnet_scolaire.connectors.ecoledirecte import ed_mapping
from custom_components.carnet_scolaire.connectors.ecoledirecte.ed_mapping import (
    attendance_facts,
    grades_from_notes,
    homework_facts,
    lesson_from_ed,
    marks_facts,
    timetable_facts,
)
from custom_components.carnet_scolaire.connectors.errors import (
    ConnectorUndecodableError,
)

FIXTURES = Path(__file__).parent / "fixtures" / "ecoledirecte"
ZONE = ZoneInfo("Europe/Paris")


def load_json(name: str) -> dict[str, Any]:
    """Load one hand-written EcoleDirecte fixture."""
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def minimal_ed_lesson() -> dict[str, Any]:
    """Return the smallest valid EcoleDirecte timetable entry."""
    return {
        "id": 1,
        "matiere": "Mathématiques",
        "start_date": "2026-09-14 08:00",
        "end_date": "2026-09-14 09:00",
    }


# fmt: off
def test_an_annulled_ed_lesson_maps_to_a_canceled_lesson() -> None:
    """binary_sensor.cours_annules must turn on without a Jinja walk of the JSON."""
    facts = timetable_facts(load_json("emploi_du_temps.json"), zone=ZONE)
    canceled = [lesson for lesson in facts.lessons if lesson.canceled]
    assert len(canceled) == 1
    assert canceled[0].end.tzinfo is not None
    assert canceled[0].num == 0
    assert facts.lessons == facts.all_lessons


def test_a_permanence_is_a_followed_slot_not_a_detention() -> None:
    """Study hall is not a detention; _in_class filters only canceled and exempted."""
    facts = timetable_facts(load_json("emploi_du_temps.json"), zone=ZONE)
    study = next(lesson for lesson in facts.lessons if lesson.status == "PERMANENCE")
    assert study.detention is False
    assert study.canceled is False
    assert study.duration == 0


def test_an_unknown_type_cours_does_not_become_a_calendar_status() -> None:
    """_lesson_events puts Lesson.status on the first line of the calendar description."""
    raw = {**minimal_ed_lesson(), "typeCours": "JETON-INCONNU"}
    lesson = lesson_from_ed(raw, zone=ZONE)
    assert lesson.status is None
    assert lesson.detention is False


def test_homework_v1_has_no_body_because_the_list_endpoint_does_not_pay_for_it() -> None:
    """Fetching each due date would explode the daily cap; empty prose is honest."""
    facts = homework_facts(load_json("cahier_de_texte.json"))
    item = facts.homework[0]
    assert item.description == ""
    assert item.done in {True, False}
    assert item.id  # from idDevoir


def test_a_french_decimal_grade_becomes_a_float_and_never_a_string_state() -> None:
    """A state of '14,5' cannot trip numeric_state."""
    grade = grades_from_notes(load_json("notes.json"))[0]
    assert isinstance(grade.value, float)
    assert grade.status is None


def test_a_missing_notes_key_is_undecodable_not_an_empty_term() -> None:
    """An empty tuple is a plausible term; a broken payload must fail the tier."""
    with pytest.raises(ConnectorUndecodableError):
        marks_facts({}, current_period_id="A001")


def test_an_ed_absence_does_not_invent_a_period_unique_id() -> None:
    """History sensors are Pronote-only; a sentinel period_id would mint _pN anyway."""
    facts = attendance_facts(load_json("vie_scolaire.json"), zone=ZONE)
    assert facts.period_id == ""
    assert facts.absences[0].days is None
    assert facts.delays[0].minutes is None
    assert all(p.schedule == () for p in facts.punishments)


def test_an_ed_lesson_serializes_to_the_card_contract_not_the_aplim_keys() -> None:
    """ha-carnet-scolaire-cards read subject/canceled; a matiere/is_annule payload would render empty."""
    from custom_components.carnet_scolaire.sensor import _lesson_dict

    facts = timetable_facts(load_json("emploi_du_temps.json"), zone=ZONE)
    payload = _lesson_dict(facts.lessons[0])
    assert "subject" in payload
    assert "canceled" in payload
    assert "classroom" in payload
    assert "matiere" not in payload
    assert "is_annule" not in payload
    assert payload["detention"] is False
    assert "salle" not in payload
# fmt: on


def test_login_mapping_produces_every_student_and_empty_session_facts() -> None:
    """Task 9 needs all pupil shapes plus zero-call session snapshots."""
    payload = {
        "data": {
            "accounts": [
                {
                    "id": 1,
                    "idLogin": 101,
                    "typeCompte": "E",
                    "prenom": "Enfant",
                    "nom": "Un",
                },
                {
                    "id": 9,
                    "typeCompte": "P",
                    "profile": {
                        "eleves": [
                            {
                                "id": 2,
                                "idLogin": 102,
                                "prenom": "Enfant",
                                "nom": "Deux",
                                "classe": {"libelle": "5e Demo"},
                            },
                            {
                                "id": 3,
                                "idLogin": 103,
                                "prenom": "Enfant",
                                "nom": "Trois",
                            },
                        ]
                    },
                },
            ]
        }
    }

    students = ed_mapping.students_from_accounts(payload)
    sessions = ed_mapping.session_facts_from_login(payload)
    login_ids = ed_mapping.student_login_ids_from_accounts(payload)

    assert tuple(student.id for student in students) == ("1", "2", "3")
    assert students[0].class_name is None
    assert students[1].class_name == "5e Demo"
    assert students[2].class_name is None
    assert tuple(facts.student for facts in sessions) == students
    assert all(
        facts.periods == () and facts.current_period is None for facts in sessions
    )
    assert login_ids == {"1": "101", "2": "102", "3": "103"}


def test_the_login_fixture_maps_the_class_label_onto_the_student() -> None:
    """The pupil card reads class_name; leaving it None blanks the ED student card."""
    students = ed_mapping.students_from_accounts(load_json("login_ok.json"))

    assert students[0].class_name == "4e Demo"


def test_mapping_helpers_require_a_zoneinfo_object() -> None:
    """A zone name must be resolved once by the connector, not by every mapper."""
    signature = inspect.signature(timetable_facts)
    assert signature.parameters["zone"].annotation == "ZoneInfo"
    lesson = lesson_from_ed(minimal_ed_lesson(), zone=ZONE)
    assert lesson.start.tzinfo is ZONE


def test_attendance_cannot_accept_a_diverging_period_id() -> None:
    """EcoleDirecte attendance has no period identity for history entities."""
    assert "period_id" not in inspect.signature(attendance_facts).parameters
    facts = attendance_facts(load_json("vie_scolaire.json"), zone=ZONE)
    assert facts.period_id == ""


def test_a_corrupted_list_entry_fails_instead_of_returning_partial_data() -> None:
    """Dropping one malformed item would make a broken response look complete."""
    payload: list[object] = [minimal_ed_lesson(), "not-a-dictionary"]
    with pytest.raises(ConnectorUndecodableError):
        timetable_facts(payload, zone=ZONE)


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {"accounts": "not-a-list"},
        {"accounts": [None]},
    ],
)
def test_invalid_account_shapes_fail_closed(payload: object) -> None:
    """A partial household would silently omit a child and all their entities."""
    with pytest.raises(ConnectorUndecodableError):
        ed_mapping.students_from_accounts(payload)


def test_accounts_require_public_and_routing_identifiers() -> None:
    """A child without either id cannot be collected or selected safely."""
    missing_public = {"accounts": [{"typeCompte": "E", "idLogin": 101}]}
    missing_login = {"accounts": [{"typeCompte": "E", "id": 1}]}
    with pytest.raises(ConnectorUndecodableError, match="pupil id"):
        ed_mapping.students_from_accounts(missing_public)
    with pytest.raises(ConnectorUndecodableError, match="routing"):
        ed_mapping.student_login_ids_from_accounts(missing_login)


@pytest.mark.parametrize(
    "raw",
    [
        {**minimal_ed_lesson(), "start_date": None},
        {**minimal_ed_lesson(), "start_date": "not-a-date"},
        {
            **minimal_ed_lesson(),
            "start_date": "2026-09-14T06:00:00+00:00",
        },
    ],
)
def test_lesson_dates_are_valid_and_normalized(raw: dict[str, Any]) -> None:
    """Invalid dates fail the tier; aware dates adopt the entry timezone."""
    if raw["start_date"] == "2026-09-14T06:00:00+00:00":
        assert lesson_from_ed(raw, zone=ZONE).start.tzinfo is ZONE
    else:
        with pytest.raises(ConnectorUndecodableError):
            lesson_from_ed(raw, zone=ZONE)


@pytest.mark.parametrize("date_value", [None, "not-a-date"])
def test_period_dates_must_be_iso_days(date_value: object) -> None:
    """A malformed period must not mint a plausible history boundary."""
    payload = load_json("notes.json")
    payload["periodes"][0]["dateDebut"] = date_value
    with pytest.raises(ConnectorUndecodableError):
        ed_mapping.periods_from_notes(payload, zone=ZONE)


def test_invalid_homework_and_grade_dates_fail_the_tier() -> None:
    """Bad calendar keys must not become empty, apparently valid collections."""
    with pytest.raises(ConnectorUndecodableError, match="homework date"):
        homework_facts({"not-a-date": []})
    notes = load_json("notes.json")
    notes["notes"][0]["date"] = None
    with pytest.raises(ConnectorUndecodableError, match="grade date"):
        grades_from_notes(notes)
    notes["notes"][0]["date"] = "not-a-date"
    with pytest.raises(ConnectorUndecodableError, match="grade date"):
        grades_from_notes(notes)


def test_unreadable_decimals_and_optional_collections_remain_absent() -> None:
    """Missing numeric evidence is None, never an invented zero."""
    notes = load_json("notes.json")
    notes["notes"][0]["valeur"] = "absent"
    notes["notes"][0]["noteSur"] = None
    grade = grades_from_notes(notes)[0]
    assert grade.value is None
    assert grade.out_of is None
    assert ed_mapping._texts(None) == ()
    assert ed_mapping._texts(" seul ") == ("seul",)


def test_closed_or_incomplete_periods_produce_no_current_or_averages() -> None:
    """No open term is different from selecting a fabricated fallback."""
    notes = load_json("notes.json")
    notes["periodes"][0]["cloture"] = True
    notes["periodes"][0]["ensembleMatieres"] = None
    notes["periodes"].append(
        {
            "idPeriode": "A002",
            "periode": "Trimestre 2",
            "dateDebut": "2027-01-01",
            "dateFin": "2027-03-31",
            "cloture": True,
            "ensembleMatieres": {
                "disciplines": [
                    {
                        "discipline": "Option",
                        "groupeMatiere": True,
                    }
                ]
            },
        }
    )
    assert ed_mapping.current_period_from_notes(notes, zone=ZONE) is None
    facts = marks_facts(notes, current_period_id="A001")
    assert facts.averages == ()


def test_missing_accounts_lists_and_grouped_averages_fail_or_skip() -> None:
    """Missing identity fails, while aggregate subject rows are intentionally skipped."""
    with pytest.raises(ConnectorUndecodableError, match="accounts key"):
        ed_mapping.students_from_accounts({})
    assert ed_mapping._texts(["", " professeur "]) == ("professeur",)
    grouped = {
        "ensembleMatieres": {
            "disciplines": [
                {"discipline": "Groupe", "groupeMatiere": True},
                {"discipline": "Sous-matière", "codeSousMatiere": "SUB"},
            ]
        }
    }
    assert ed_mapping._averages(grouped) == ()
