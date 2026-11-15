"""The frozen DTO boundary.

Nothing ``pronotepy`` produces is allowed past this line (§3.1), and the DTOs
carry the handful of derived properties the entity layer reads. Three of those
properties encode decisions that were wrong once, so they are pinned here
rather than only through the modules that use them.

The boundary itself is worth one test of its own: these objects are frozen and
slotted, which is what stops an entity holding a live ``pronotepy`` object --
and with it a session, a socket pool and an object read after its session
closed, which raises ``Erreur.G = 22``.
"""

import dataclasses
import datetime as dt
from typing import get_type_hints
from zoneinfo import ZoneInfo

import pytest

from custom_components.carnet_scolaire import models
from custom_components.carnet_scolaire.const import GradeStatus
from custom_components.carnet_scolaire.models import (
    Absence,
    Delay,
    Grade,
    Lesson,
    Menu,
    Period,
    SessionLifetime,
)

PARIS = ZoneInfo("Europe/Paris")


def a_lesson(**overrides: object) -> Lesson:
    """A decoded lesson."""
    defaults: dict[str, object] = {
        "id": "LESSON-1",
        "subject": "Mathématiques",
        "subject_id": "SUBJECT-MATHS",
        "teachers": ("Prof. Un",),
        "classrooms": ("Salle 101",),
        "groups": (),
        "start": dt.datetime(2026, 3, 12, 8, 0, tzinfo=PARIS),
        "end": dt.datetime(2026, 3, 12, 9, 0, tzinfo=PARIS),
        "canceled": False,
        "status": None,
        "detention": False,
        "outing": False,
        "exempted": False,
        "test": False,
        "memo": None,
        "background_color": None,
        "virtual_classrooms": (),
        "num": 0,
        "place": 0,
        "duration": 2,
        "end_inferred": False,
    }
    defaults.update(overrides)
    return Lesson(**defaults)  # type: ignore[arg-type]


def a_grade(**overrides: object) -> Grade:
    """A decoded grade."""
    defaults: dict[str, object] = {
        "id": "GRADE-1",
        "subject": "Mathématiques",
        "subject_id": "SUBJECT-MATHS",
        "value": 14.5,
        "status": None,
        "out_of": 20.0,
        "default_out_of": 20.0,
        "date": dt.date(2026, 3, 10),
        "coefficient": 1.0,
        "class_average": 11.2,
        "max_value": 18.0,
        "min_value": 4.0,
        "comment": None,
        "is_bonus": False,
        "is_optional": False,
        "is_out_of_20": False,
    }
    defaults.update(overrides)
    return Grade(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# The boundary itself
# ---------------------------------------------------------------------------


def _dto_classes() -> list[type]:
    """Every dataclass declared in ``models``."""
    return [
        value
        for value in vars(models).values()
        if isinstance(value, type)
        and dataclasses.is_dataclass(value)
        and value.__module__ == models.__name__
    ]


def test_every_dto_is_frozen_and_slotted() -> None:
    """Which is what keeps a live ``pronotepy`` object out of an entity.

    An entity holding a client reference keeps the whole object graph alive,
    session included; and an object read after its session closed raises
    ``Erreur.G = 22``. Frozen and slotted is how the boundary is enforced by
    construction rather than by review: nothing can be attached to one of these
    after the fact, and nothing mutable can be smuggled in later.
    """
    classes = _dto_classes()
    assert classes, "no DTOs found -- has the module been renamed?"

    for klass in classes:
        params = klass.__dataclass_params__  # type: ignore[attr-defined]
        assert params.frozen, f"{klass.__name__} is not frozen"
        assert not hasattr(klass, "__dict__") or "__slots__" in vars(klass), (
            f"{klass.__name__} is not slotted"
        )


def test_a_dto_refuses_mutation() -> None:
    """The property above, demonstrated rather than only asserted."""
    lesson = a_lesson()
    with pytest.raises(dataclasses.FrozenInstanceError):
        lesson.canceled = True  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Lesson
# ---------------------------------------------------------------------------


def test_a_slot_key_is_a_coordinate_on_the_grid() -> None:
    """The day and the position in it -- what is taught there is content.

    Including ``subject_id`` defeated the de-duplication the key exists for: a
    substitution is two entries on one slot with *different* subjects, so keyed
    with the subject they landed in separate buckets and both survived. The day
    was counted twice and the calendar drew both.
    """
    assert a_lesson().slot_key == ("2026-03-12", 0)
    assert a_lesson(subject_id="SOMETHING-ELSE").slot_key == ("2026-03-12", 0)
    assert a_lesson(place=3).slot_key == ("2026-03-12", 3)


def test_a_change_signature_ignores_the_fields_a_teacher_edits() -> None:
    """A corrected memo must fire nothing.

    That is what the single "delta on ``N`` only" rule was really trying to
    buy, and the whitelist is how it is bought without losing cancellations.
    """
    baseline = a_lesson().change_signature
    assert a_lesson(memo="Apporter le manuel").change_signature == baseline
    assert a_lesson(background_color="#123456").change_signature == baseline
    assert a_lesson(test=True).change_signature == baseline
    assert a_lesson(num=7).change_signature == baseline


def test_a_change_signature_compares_collections_as_sets() -> None:
    """Nothing in the protocol orders ``ListeProfesseurs`` or ``ListeSalles``.

    As tuples, a co-taught lesson emitted a change event on an arbitrary
    subset of collections, every night, for the rest of the year.
    """
    one = a_lesson(teachers=("A", "B"), classrooms=("X", "Y")).change_signature
    other = a_lesson(teachers=("B", "A"), classrooms=("Y", "X")).change_signature
    assert one == other


def test_a_change_signature_notices_what_matters() -> None:
    """Cancellation, status, rooms, teachers and both ends of the interval."""
    baseline = a_lesson().change_signature
    for change in (
        {"canceled": True},
        {"status": "Prof. absent"},
        {"classrooms": ("Salle 202",)},
        {"teachers": ("Prof. Deux",)},
        {"start": dt.datetime(2026, 3, 12, 14, 0, tzinfo=PARIS)},
        {"end": dt.datetime(2026, 3, 12, 10, 0, tzinfo=PARIS)},
    ):
        assert a_lesson(**change).change_signature != baseline, change


def test_classrooms_are_joined_for_display() -> None:
    """One entity attribute, several rooms -- and ``None`` when there are none."""
    assert a_lesson(classrooms=("A", "B")).classroom == "A, B"
    assert a_lesson(classrooms=()).classroom is None


# ---------------------------------------------------------------------------
# Grade
# ---------------------------------------------------------------------------


def test_a_grade_may_hold_a_value_or_a_status_but_not_both() -> None:
    """A promise the type system cannot make and no caller checked.

    The gateway is the only producer, so this can only fire on a gateway bug
    -- which is exactly when failing loudly is right: a ``Grade`` holding both
    ``14.5`` and ``ABSENT`` would put one of the two into
    ``sensor.<eleve>_derniere_note`` depending on which branch of the value
    function ran first, and the contract with every ``numeric_state`` trigger
    downstream rests on that never happening.
    """
    assert a_grade(value=14.5, status=None).value == 14.5
    assert a_grade(value=None, status=GradeStatus.ABSENT).status is GradeStatus.ABSENT
    assert a_grade(value=None, status=None).value is None

    with pytest.raises(ValueError, match="both a value"):
        a_grade(value=14.5, status=GradeStatus.ABSENT)


# ---------------------------------------------------------------------------
# Attendance
# ---------------------------------------------------------------------------


def test_a_delay_without_a_duration_is_none_not_zero() -> None:
    """An ED connector must be able to publish JSON ``null`` rather than ``0``.

    A ``numeric_state`` on ``minutes=0`` would stay valid forever and never
    fire, which is the same trap ``Grade.value`` already avoids by publishing
    ``None`` for sentinels.
    """
    delay = Delay(
        id="D1",
        at=dt.datetime(2026, 9, 11, 8, 0, tzinfo=dt.UTC),
        minutes=None,
        justified=False,
        justification=None,
        reasons=(),
    )

    assert get_type_hints(Delay)["minutes"] == int | None
    assert delay.minutes is None


def test_an_absence_without_a_day_count_is_none_not_zero() -> None:
    """The absence DTO carries the ED contract too, not a made-up zero."""
    absence = Absence(
        id="A1",
        from_date=dt.datetime(2026, 9, 11, 8, 0, tzinfo=dt.UTC),
        to_date=dt.datetime(2026, 9, 11, 17, 0, tzinfo=dt.UTC),
        justified=False,
        hours=None,
        days=None,
        reasons=(),
    )

    assert get_type_hints(Absence)["days"] == int | None
    assert absence.days is None


# ---------------------------------------------------------------------------
# Menu
# ---------------------------------------------------------------------------


def test_a_menu_counts_its_dishes() -> None:
    """Which is the sensor's state, so the sum has to cover every course."""
    menu = Menu(
        id="MENU-1",
        day=dt.date(2026, 3, 12),
        name="Menu du jour",
        is_lunch=True,
        is_dinner=False,
        first_meal=("Carottes",),
        main_meal=("Poisson", "Riz"),
        side_meal=(),
        other_meal=(),
        cheese=("Yaourt",),
        dessert=("Pomme",),
    )
    assert menu.dish_count == 5


# ---------------------------------------------------------------------------
# Period
# ---------------------------------------------------------------------------


def test_a_period_is_closed_once_it_has_ended() -> None:
    """Which is what decides whether the history tier collects it at all."""
    period = Period(
        id="PERIOD-1",
        name="Trimestre 1",
        start=dt.datetime(2025, 9, 1, tzinfo=PARIS),
        end=dt.datetime(2025, 12, 5, 23, 59, 59, tzinfo=PARIS),
        index=1,
    )
    assert period.is_closed(dt.datetime(2026, 3, 12, tzinfo=PARIS))
    assert not period.is_closed(dt.datetime(2025, 10, 1, tzinfo=PARIS))


def test_a_period_index_is_what_makes_an_entity_id_stable() -> None:
    """ "Trimestre 1" may be relabelled between years (§2.4).

    So the history entities carry ``p1``, ``p2``, ``p3``, and only the
    *displayed* name follows the establishment.
    """
    period = Period(
        id="PERIOD-1",
        name="Semestre 1",
        start=dt.datetime(2025, 9, 1, tzinfo=PARIS),
        end=dt.datetime(2026, 1, 31, tzinfo=PARIS),
        index=1,
    )
    assert period.index == 1


# ---------------------------------------------------------------------------
# SessionLifetime
# ---------------------------------------------------------------------------


def test_an_unmeasured_lifetime_reports_nothing() -> None:
    """Before the first measurement the pessimistic default has to apply.

    The dominance argument for lazy reconnection depends on that ordering: the
    measurement *relaxes* the constraint, it never poses it.
    """
    assert SessionLifetime().observed_minutes is None


def test_the_shortest_observation_wins() -> None:
    """The minimum, not the mean.

    One short expiry is proof the timeout can be that short; a long one only
    proves it was not exercised.
    """
    lifetime = SessionLifetime()
    at = dt.datetime(2026, 3, 12, 8, 0, tzinfo=PARIS)
    lifetime = lifetime.with_sample(3600.0, at)
    lifetime = lifetime.with_sample(600.0, at)
    lifetime = lifetime.with_sample(1800.0, at)

    assert lifetime.observed_minutes == pytest.approx(10.0)


def test_the_sample_window_is_bounded() -> None:
    """A long-running instance must not accumulate a year of observations."""
    lifetime = SessionLifetime()
    at = dt.datetime(2026, 3, 12, 8, 0, tzinfo=PARIS)
    for index in range(50):
        lifetime = lifetime.with_sample(float(600 + index), at)

    assert len(lifetime.samples) == 20
    assert lifetime.last_expiry == at
