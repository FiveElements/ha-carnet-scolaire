"""The change detector, to 100 %.

Two rules, because one cannot do the job (§2.2.1). Append-only collections diff
on the identifier; the timetable diffs a whitelisted field tuple per lesson
``N``, over the *un*-de-duplicated week.

The property that has to hold above all others is that **a restart replays
nothing**. Everything else in this module is a refinement of that: an event
fired for a grade that arrived in September, on the morning somebody restarted
Home Assistant, is worse than no event at all -- it teaches the user to ignore
the notification.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from custom_components.carnet_scolaire.const import (
    EVENT_ABSENCE_ADDED,
    EVENT_DELAY_ADDED,
    EVENT_EVALUATION_ADDED,
    EVENT_GRADE_ADDED,
    EVENT_HOMEWORK_ADDED,
    EVENT_INFORMATION_ADDED,
    EVENT_LESSON_CANCELED,
    EVENT_LESSON_MOVED,
    EVENT_LESSON_RESTORED,
    EVENT_LESSON_STATUS_CHANGED,
    EVENT_MESSAGE_RECEIVED,
    EVENT_PUNISHMENT_ADDED,
    EVENT_ROOM_CHANGED,
    EVENT_TEACHER_CHANGED,
    LESSON_EVENT_TYPES,
    GradeStatus,
)
from custom_components.carnet_scolaire.delta import _NEW_THREAD_CAP, DeltaDetector
from custom_components.carnet_scolaire.models import (
    Absence,
    Acquisition,
    AttendanceFacts,
    Delay,
    Discussion,
    DiscussionsFacts,
    Evaluation,
    EvaluationsFacts,
    Grade,
    Homework,
    HomeworkFacts,
    Information,
    Lesson,
    MarksFacts,
    Message,
    NewsFacts,
    Punishment,
    TimetableFacts,
)

PARIS = ZoneInfo("Europe/Paris")
STUDENT = "STUDENT-1"


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


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


def timetable(*lessons: Lesson) -> TimetableFacts:
    """A timetable snapshot; the delta reads ``all_lessons``."""
    return TimetableFacts(lessons=lessons, all_lessons=lessons, weeks_fetched=(28,))


def a_grade(identifier: str = "GRADE-1", **overrides: object) -> Grade:
    """A decoded grade."""
    defaults: dict[str, object] = {
        "id": identifier,
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
        "comment": "Contrôle",
        "is_bonus": False,
        "is_optional": False,
        "is_out_of_20": False,
    }
    defaults.update(overrides)
    return Grade(**defaults)  # type: ignore[arg-type]


def marks(*grades: Grade) -> MarksFacts:
    """A marks snapshot."""
    return MarksFacts(
        period_id="PERIOD-1",
        period_index=2,
        grades=grades,
        averages=(),
        overall_average=None,
        class_overall_average=None,
        report=None,
    )


def a_homework(identifier: str = "HOMEWORK-1") -> Homework:
    """A decoded homework item."""
    return Homework(
        id=identifier,
        subject="Histoire",
        description="Lire le chapitre 4",
        description_text="Lire le chapitre 4",
        due=dt.date(2026, 3, 16),
        done=False,
        background_color=None,
    )


def an_information(identifier: str = "INFORMATION-1") -> Information:
    """A decoded news item."""
    return Information(
        id=identifier,
        title="Sortie scolaire",
        author="Direction",
        category="Vie scolaire",
        read=False,
        survey=False,
        anonymous_response=False,
        created=dt.datetime(2026, 3, 11, 9, 30, tzinfo=PARIS),
        start_date=None,
        end_date=None,
    )


def an_absence(identifier: str = "ABSENCE-1") -> Absence:
    """A decoded absence."""
    return Absence(
        id=identifier,
        from_date=dt.datetime(2026, 3, 9, 8, 0, tzinfo=PARIS),
        to_date=dt.datetime(2026, 3, 9, 10, 0, tzinfo=PARIS),
        justified=False,
        hours="2h00",
        days=0,
        reasons=("Maladie",),
    )


def a_delay(identifier: str = "DELAY-1") -> Delay:
    """A decoded late arrival."""
    return Delay(
        id=identifier,
        at=dt.datetime(2026, 3, 10, 8, 5, tzinfo=PARIS),
        minutes=12,
        justified=False,
        justification="Transports",
        reasons=("Retard bus",),
    )


def a_punishment(identifier: str = "PUNISHMENT-1") -> Punishment:
    """A decoded punishment."""
    return Punishment(
        id=identifier,
        nature="Retenue",
        reasons=("Bavardage",),
        giver="CPE",
        given_at=dt.datetime(2026, 3, 11, 16, 0, tzinfo=PARIS),
        exclusion=False,
        during_lesson=False,
        homework="Copier le règlement",
        schedule=(),
    )


def attendance(
    absences: tuple[Absence, ...] = (),
    delays: tuple[Delay, ...] = (),
    punishments: tuple[Punishment, ...] = (),
) -> AttendanceFacts:
    """An attendance snapshot."""
    return AttendanceFacts(
        period_id="PERIOD-1",
        period_index=2,
        absences=absences,
        delays=delays,
        punishments=punishments,
    )


def an_evaluation(identifier: str = "EVALUATION-1") -> Evaluation:
    """A decoded competency evaluation."""
    return Evaluation(
        id=identifier,
        name="Résoudre un problème",
        subject="Mathématiques",
        subject_id="SUBJECT-MATHS",
        teacher="Prof. Un",
        description="Évaluation de compétence",
        date=dt.date(2026, 3, 6),
        acquisitions=(
            Acquisition(
                id="ACQUISITION-1",
                name="Calcul littéral",
                level="Très bonne maîtrise",
                abbreviation="TBM",
                coefficient=1.0,
                domain="Nombres et calculs",
                pillar="Domaine 1",
            ),
        ),
    )


def a_message(identifier: str, minute: int = 0) -> Message:
    """A decoded message."""
    return Message(
        id=identifier,
        author="Direction",
        created=dt.datetime(2026, 3, 11, 10, minute, tzinfo=PARIS),
        content="Le départ est à 8h.",
    )


def a_thread(
    identifier: str = "THREAD-1",
    *,
    unread: int = 0,
    messages: tuple[Message, ...] = (),
) -> Discussion:
    """A decoded discussion thread."""
    return Discussion(
        id=identifier,
        subject="Sortie du 20 mars",
        creator="Direction",
        unread=unread,
        closed=False,
        labels=(),
        messages=messages,
    )


def discussions(*threads: Discussion) -> DiscussionsFacts:
    """A discussions snapshot."""
    return DiscussionsFacts(
        discussions=threads, expanded=frozenset(thread.id for thread in threads)
    )


# ---------------------------------------------------------------------------
# The rule that matters most
# ---------------------------------------------------------------------------


def test_a_first_snapshot_emits_nothing_at_all() -> None:
    """Without this, every restart replays the whole term (§2.2.1).

    Eight notifications for grades from September, at breakfast, because
    somebody restarted Home Assistant. That teaches the user to mute the
    notification, which costs every future event too.
    """
    detector = DeltaDetector()

    assert detector.marks(STUDENT, marks(a_grade())) == []
    assert detector.homework(STUDENT, HomeworkFacts(homework=(a_homework(),))) == []
    assert detector.news(STUDENT, NewsFacts(information=(an_information(),))) == []
    assert detector.attendance(STUDENT, attendance(absences=(an_absence(),))) == []
    assert (
        detector.evaluations(
            STUDENT,
            EvaluationsFacts(
                period_id="PERIOD-1", period_index=2, evaluations=(an_evaluation(),)
            ),
        )
        == []
    )
    assert detector.discussions(STUDENT, discussions(a_thread(unread=3))) == []
    assert detector.timetable(STUDENT, timetable(a_lesson())) == []


def test_forgetting_a_student_resets_every_collection() -> None:
    """A removed child must not leave memory behind that a re-add would diff.

    And the next snapshot after a re-add primes again rather than replaying.
    """
    detector = DeltaDetector()
    detector.marks(STUDENT, marks(a_grade()))
    detector.timetable(STUDENT, timetable(a_lesson()))
    detector.discussions(STUDENT, discussions(a_thread(unread=1)))

    detector.forget(STUDENT)

    assert detector.marks(STUDENT, marks(a_grade(), a_grade("GRADE-2"))) == []
    assert detector.timetable(STUDENT, timetable(a_lesson(canceled=True))) == []
    assert detector.discussions(STUDENT, discussions(a_thread(unread=5))) == []


def test_a_renamed_student_keeps_everything_the_detector_remembers() -> None:
    """PRONOTE renames a child at every login; its history must follow.

    Without the rename, the first snapshot under the new identifier primed
    the detector instead of comparing, so a grade published across a
    re-login, a cancellation and a new message were all swallowed.
    """
    detector = DeltaDetector()
    detector.marks("OLD", marks(a_grade()))
    detector.timetable("OLD", timetable(a_lesson()))
    detector.discussions("OLD", discussions(a_thread(unread=1)))

    detector.rename("OLD", "NEW")

    assert len(detector.marks("NEW", marks(a_grade(), a_grade("GRADE-2")))) == 1
    assert detector.timetable("NEW", timetable(a_lesson(canceled=True))) != []
    assert detector.discussions("NEW", discussions(a_thread(unread=3))) != []
    # And nothing is left under the old identifier to diff against.
    assert detector.marks("OLD", marks(a_grade("GRADE-3"))) == []


def test_renaming_a_student_nothing_remembers_is_harmless() -> None:
    """A child renamed before its first snapshot has nothing to carry."""
    detector = DeltaDetector()

    detector.rename("OLD", "NEW")

    assert detector.marks("NEW", marks(a_grade())) == []


def test_forgetting_one_student_leaves_the_other_alone() -> None:
    """Two children on one parent account are independent."""
    detector = DeltaDetector()
    detector.marks("A", marks(a_grade()))
    detector.marks("B", marks(a_grade()))

    detector.forget("A")

    assert detector.marks("B", marks(a_grade(), a_grade("GRADE-2"))) != []


# ---------------------------------------------------------------------------
# Append-only collections
# ---------------------------------------------------------------------------


def test_eight_new_grades_produce_eight_events() -> None:
    """Not one aggregated event (annexe A §4).

    An automation that notifies per grade must be able to, and aggregating is
    trivial for the user to redo whereas separating is not.
    """
    detector = DeltaDetector()
    detector.marks(STUDENT, marks(a_grade("GRADE-0")))

    grades = tuple(a_grade(f"GRADE-{index}") for index in range(1, 9))
    events = detector.marks(STUDENT, marks(a_grade("GRADE-0"), *grades))

    assert len(events) == 8
    assert {event.event_type for event in events} == {EVENT_GRADE_ADDED}
    assert {event.entity_key for event in events} == {"new_grade"}


def test_a_corrected_grade_fires_nothing() -> None:
    """The identifier rule: a teacher fixing a value must not wake the house.

    That is what "delta on ``N``, never on content" buys, and for append-only
    collections it is exactly right.
    """
    detector = DeltaDetector()
    detector.marks(STUDENT, marks(a_grade(value=14.5)))

    assert detector.marks(STUDENT, marks(a_grade(value=15.5))) == []


def test_a_grade_event_carries_what_an_automation_reads() -> None:
    """Subject, value, scale, coefficient, date, class average, identifier."""
    detector = DeltaDetector()
    detector.marks(STUDENT, marks())

    event = detector.marks(STUDENT, marks(a_grade()))[0]

    assert event.attributes["subject"] == "Mathématiques"
    assert event.attributes["grade"] == 14.5
    assert event.attributes["out_of"] == 20.0
    assert event.attributes["date"] == "2026-03-10"
    assert event.attributes["grade_id"] == "GRADE-1"
    assert event.attributes["status"] is None


def test_a_sentinel_grade_reports_its_status_rather_than_a_number() -> None:
    """``value`` and ``status`` are exclusive, and the event says which."""
    detector = DeltaDetector()
    detector.marks(STUDENT, marks())

    event = detector.marks(
        STUDENT, marks(a_grade(value=None, status=GradeStatus.ABSENT))
    )[0]

    assert event.attributes["grade"] is None
    assert event.attributes["status"] == str(GradeStatus.ABSENT)


def test_grades_are_scoped_to_their_period() -> None:
    """A closed period and the current one are separate collections.

    Otherwise collecting history would report every grade of the first term as
    new, once, on the day the feature was switched on.
    """
    detector = DeltaDetector()
    detector.marks(STUDENT, marks(a_grade()))

    other_period = MarksFacts(
        period_id="PERIOD-0",
        period_index=1,
        grades=(a_grade(),),
        averages=(),
        overall_average=None,
        class_overall_average=None,
        report=None,
    )
    # A first snapshot of a *different* collection, so still nothing.
    assert detector.marks(STUDENT, other_period) == []


def test_new_homework_is_reported_once() -> None:
    """And the event carries the due date, which is what a reminder needs."""
    detector = DeltaDetector()
    detector.homework(STUDENT, HomeworkFacts(homework=()))

    events = detector.homework(STUDENT, HomeworkFacts(homework=(a_homework(),)))

    assert [event.event_type for event in events] == [EVENT_HOMEWORK_ADDED]
    assert events[0].attributes["due"] == "2026-03-16"
    assert detector.homework(STUDENT, HomeworkFacts(homework=(a_homework(),))) == []


def test_new_news_is_reported_once() -> None:
    """With the author and whether it is a survey."""
    detector = DeltaDetector()
    detector.news(STUDENT, NewsFacts(information=()))

    events = detector.news(STUDENT, NewsFacts(information=(an_information(),)))

    assert [event.event_type for event in events] == [EVENT_INFORMATION_ADDED]
    assert events[0].attributes["author"] == "Direction"
    assert events[0].attributes["survey"] is False


def test_absences_delays_and_punishments_are_three_collections() -> None:
    """Three entities, and that follows from their shapes rather than taste.

    ``Absence`` carries ``hours`` and ``days``; ``Delay`` carries ``minutes``
    and ``justification``. One entity with two differently-shaped payloads
    would force every automation to test the type before reading an attribute
    -- that is, to write the condition this project exists to remove.
    """
    detector = DeltaDetector()
    detector.attendance(STUDENT, attendance())

    events = detector.attendance(
        STUDENT,
        attendance(
            absences=(an_absence(),),
            delays=(a_delay(),),
            punishments=(a_punishment(),),
        ),
    )

    by_type = {event.event_type: event for event in events}
    assert set(by_type) == {
        EVENT_ABSENCE_ADDED,
        EVENT_DELAY_ADDED,
        EVENT_PUNISHMENT_ADDED,
    }
    assert by_type[EVENT_ABSENCE_ADDED].attributes["hours"] == "2h00"
    assert "minutes" not in by_type[EVENT_ABSENCE_ADDED].attributes
    assert by_type[EVENT_DELAY_ADDED].attributes["minutes"] == 12
    assert by_type[EVENT_PUNISHMENT_ADDED].attributes["nature"] == "Retenue"


def test_absence_days_and_delay_minutes_can_be_missing_in_event_payloads() -> None:
    """The event payload must copy ``None`` through unchanged for ED."""
    detector = DeltaDetector()
    detector.attendance(STUDENT, attendance())

    events = detector.attendance(
        STUDENT,
        attendance(
            absences=(
                Absence(
                    id="A1",
                    from_date=dt.datetime(2026, 9, 11, 8, 0, tzinfo=dt.UTC),
                    to_date=dt.datetime(2026, 9, 11, 17, 0, tzinfo=dt.UTC),
                    justified=False,
                    hours=None,
                    days=None,
                    reasons=(),
                ),
            ),
            delays=(
                Delay(
                    id="D1",
                    at=dt.datetime(2026, 9, 11, 8, 0, tzinfo=dt.UTC),
                    minutes=None,
                    justified=False,
                    justification=None,
                    reasons=(),
                ),
            ),
        ),
    )

    by_type = {event.event_type: event for event in events}
    assert by_type[EVENT_ABSENCE_ADDED].attributes["days"] is None
    assert by_type[EVENT_DELAY_ADDED].attributes["minutes"] is None


def test_a_punishment_event_lists_its_scheduled_slots() -> None:
    """So an automation can put the detention in a calendar."""
    detector = DeltaDetector()
    detector.attendance(STUDENT, attendance())

    from custom_components.carnet_scolaire.models import PunishmentSlot

    punished = Punishment(
        id="PUNISHMENT-2",
        nature="Retenue",
        reasons=(),
        giver="CPE",
        given_at=dt.datetime(2026, 3, 11, 16, 0, tzinfo=PARIS),
        exclusion=False,
        during_lesson=False,
        homework=None,
        schedule=(
            PunishmentSlot(
                start=dt.datetime(2026, 3, 13, 10, 0, tzinfo=PARIS),
                duration_minutes=60,
            ),
        ),
    )
    event = detector.attendance(STUDENT, attendance(punishments=(punished,)))[0]

    assert event.attributes["schedule"] == ["2026-03-13T10:00:00+01:00"]


def test_new_evaluations_carry_their_acquisitions() -> None:
    """A competency event with no levels in it says nothing useful."""
    detector = DeltaDetector()
    detector.evaluations(
        STUDENT, EvaluationsFacts(period_id="PERIOD-1", period_index=2, evaluations=())
    )

    events = detector.evaluations(
        STUDENT,
        EvaluationsFacts(
            period_id="PERIOD-1", period_index=2, evaluations=(an_evaluation(),)
        ),
    )

    assert [event.event_type for event in events] == [EVENT_EVALUATION_ADDED]
    assert events[0].attributes["acquisitions"] == [
        {"name": "Calcul littéral", "level": "Très bonne maîtrise"}
    ]


def test_an_item_that_disappears_and_returns_is_reported_again() -> None:
    """The memo is replaced wholesale, which is the honest simple behaviour.

    A grade PRONOTE withdrew and republished *is* new information as far as the
    integration can tell, and pretending otherwise would need a permanent
    record of every identifier ever seen.
    """
    detector = DeltaDetector()
    detector.marks(STUDENT, marks(a_grade()))
    assert detector.marks(STUDENT, marks()) == []
    assert len(detector.marks(STUDENT, marks(a_grade()))) == 1


def test_a_duplicate_identifier_in_one_snapshot_fires_once() -> None:
    """Defensive: the protocol has been seen to repeat an entry."""
    detector = DeltaDetector()
    detector.marks(STUDENT, marks())
    events = detector.marks(STUDENT, marks(a_grade(), a_grade()))
    assert len(events) == 1


# ---------------------------------------------------------------------------
# The timetable
# ---------------------------------------------------------------------------


def test_a_cancellation_fires_a_cancellation() -> None:
    """The single most useful event this integration emits."""
    detector = DeltaDetector()
    detector.timetable(STUDENT, timetable(a_lesson()))

    events = detector.timetable(STUDENT, timetable(a_lesson(canceled=True)))

    assert [event.event_type for event in events] == [EVENT_LESSON_CANCELED]
    assert events[0].entity_key == "lesson_changed"
    assert events[0].attributes["canceled"] is True


def test_a_lifted_cancellation_is_not_reported_as_a_cancellation() -> None:
    """This is what the blind fallback got exactly backwards.

    "If nothing else matched, call it a cancellation" fired
    ``lesson_canceled`` when a cancellation was **lifted** -- so an automation
    that notifies "no school first period, sleep in" fired on the morning the
    lesson came back.
    """
    detector = DeltaDetector()
    detector.timetable(STUDENT, timetable(a_lesson(canceled=True)))

    events = detector.timetable(STUDENT, timetable(a_lesson()))

    assert [event.event_type for event in events] == [EVENT_LESSON_RESTORED]


def test_a_moved_lesson_fires_a_move_with_its_previous_time() -> None:
    """Keyed on the slot, ``lesson_moved`` was structurally unreachable.

    A slot key contains the day, so a lesson that moved could never be seen to
    have moved -- while a change to the establishment's *grid* (bell times
    shifting five minutes) moved ``start`` for every lesson at once and fired a
    move for the entire week. Keying on ``N`` is what makes the event mean what
    its name says.
    """
    detector = DeltaDetector()
    detector.timetable(STUDENT, timetable(a_lesson()))

    moved = a_lesson(
        start=dt.datetime(2026, 3, 12, 14, 0, tzinfo=PARIS),
        end=dt.datetime(2026, 3, 12, 15, 0, tzinfo=PARIS),
        place=6,
    )
    events = detector.timetable(STUDENT, timetable(moved))

    assert [event.event_type for event in events] == [EVENT_LESSON_MOVED]
    assert events[0].attributes["previous_start"] == "2026-03-12T08:00:00+01:00"
    assert events[0].attributes["start"] == "2026-03-12T14:00:00+01:00"


def test_a_shortened_lesson_counts_as_a_move() -> None:
    """Otherwise the signature moved with nothing to explain it."""
    detector = DeltaDetector()
    detector.timetable(STUDENT, timetable(a_lesson()))

    shortened = a_lesson(end=dt.datetime(2026, 3, 12, 8, 30, tzinfo=PARIS))
    events = detector.timetable(STUDENT, timetable(shortened))

    assert [event.event_type for event in events] == [EVENT_LESSON_MOVED]
    assert events[0].attributes["previous_end"] == "2026-03-12T09:00:00+01:00"


def test_a_room_change_fires_a_room_change() -> None:
    """A room change keeps the same ``N``, so an identifier diff sees nothing.

    Which is the whole reason the timetable needs its own rule.
    """
    detector = DeltaDetector()
    detector.timetable(STUDENT, timetable(a_lesson()))

    events = detector.timetable(STUDENT, timetable(a_lesson(classrooms=("Salle 202",))))

    assert [event.event_type for event in events] == [EVENT_ROOM_CHANGED]
    assert events[0].attributes["previous_classroom"] == "Salle 101"
    assert events[0].attributes["classroom"] == "Salle 202"


def test_a_teacher_change_fires_a_teacher_change() -> None:
    """With both lists, so an automation can name the replacement."""
    detector = DeltaDetector()
    detector.timetable(STUDENT, timetable(a_lesson()))

    events = detector.timetable(STUDENT, timetable(a_lesson(teachers=("Prof. Deux",))))

    assert [event.event_type for event in events] == [EVENT_TEACHER_CHANGED]
    assert events[0].attributes["previous_teachers"] == ["Prof. Un"]
    assert events[0].attributes["teachers"] == ["Prof. Deux"]


def test_a_reordered_teacher_list_is_not_a_change() -> None:
    """Nothing in the protocol orders ``ListeProfesseurs``.

    Compared as tuples, a co-taught lesson emitted ``lesson_changed`` on an
    arbitrary subset of collections, every night, for the rest of the year. An
    automation that pushes a notification on that is worse than no automation.
    """
    detector = DeltaDetector()
    detector.timetable(
        STUDENT, timetable(a_lesson(teachers=("Prof. Un", "Prof. Deux")))
    )

    events = detector.timetable(
        STUDENT, timetable(a_lesson(teachers=("Prof. Deux", "Prof. Un")))
    )

    assert events == []


def test_a_reordered_classroom_list_is_not_a_change() -> None:
    """Same for ``ListeSalles``, and for the same reason."""
    detector = DeltaDetector()
    detector.timetable(
        STUDENT, timetable(a_lesson(classrooms=("Salle 101", "Salle 102")))
    )

    events = detector.timetable(
        STUDENT, timetable(a_lesson(classrooms=("Salle 102", "Salle 101")))
    )

    assert events == []


def test_a_status_relabel_alone_is_reported_as_a_status_change() -> None:
    """PRONOTE uses ``Statut`` for things it does not set ``estAnnule`` for.

    "Prof. absent", "Cours dépl." -- the change is real and worth reporting,
    it is simply not a cancellation, which is what it used to be reported as.
    """
    detector = DeltaDetector()
    detector.timetable(STUDENT, timetable(a_lesson()))

    events = detector.timetable(STUDENT, timetable(a_lesson(status="Prof. absent")))

    assert [event.event_type for event in events] == [EVENT_LESSON_STATUS_CHANGED]
    assert events[0].attributes["status"] == "Prof. absent"


def test_a_status_change_alongside_a_cancellation_is_not_reported_twice() -> None:
    """The typed event already says what happened."""
    detector = DeltaDetector()
    detector.timetable(STUDENT, timetable(a_lesson()))

    events = detector.timetable(
        STUDENT, timetable(a_lesson(canceled=True, status="Cours annulé"))
    )

    assert [event.event_type for event in events] == [EVENT_LESSON_CANCELED]


def test_one_change_can_legitimately_be_several_things() -> None:
    """A lesson moved *and* put in another room is two events, not a winner."""
    detector = DeltaDetector()
    detector.timetable(STUDENT, timetable(a_lesson()))

    changed = a_lesson(
        start=dt.datetime(2026, 3, 12, 14, 0, tzinfo=PARIS),
        end=dt.datetime(2026, 3, 12, 15, 0, tzinfo=PARIS),
        classrooms=("Salle 202",),
        teachers=("Prof. Deux",),
        canceled=True,
    )
    events = detector.timetable(STUDENT, timetable(changed))

    assert [event.event_type for event in events] == [
        EVENT_LESSON_CANCELED,
        EVENT_LESSON_MOVED,
        EVENT_ROOM_CHANGED,
        EVENT_TEACHER_CHANGED,
    ]


def test_every_event_gets_its_own_attributes_mapping() -> None:
    """Four events sharing one mutable dict is a bug awaiting its first consumer.

    The mapping is handed to ``async_fire`` and ends up on an entity's state,
    so anything that annotates what it received would silently annotate the
    other three.
    """
    detector = DeltaDetector()
    detector.timetable(STUDENT, timetable(a_lesson()))

    changed = a_lesson(
        start=dt.datetime(2026, 3, 12, 14, 0, tzinfo=PARIS),
        end=dt.datetime(2026, 3, 12, 15, 0, tzinfo=PARIS),
        classrooms=("Salle 202",),
    )
    events = detector.timetable(STUDENT, timetable(changed))

    assert len(events) == 2
    events[0].attributes["annotated"] = True
    assert "annotated" not in events[1].attributes


def test_a_lesson_appearing_for_the_first_time_is_not_a_change() -> None:
    """A newly published week would otherwise fire an event per lesson."""
    detector = DeltaDetector()
    detector.timetable(STUDENT, timetable(a_lesson()))

    events = detector.timetable(
        STUDENT, timetable(a_lesson(), a_lesson(id="LESSON-2", place=2))
    )

    assert events == []


def test_a_memo_correction_fires_nothing() -> None:
    """A teacher fixing a typo must not wake the house up.

    ``memo``, ``background_color`` and lesson content are outside the
    whitelist, which is what the single "delta on ``N`` only" rule was really
    trying to buy.
    """
    detector = DeltaDetector()
    detector.timetable(STUDENT, timetable(a_lesson(memo="Aporter le manuel")))

    events = detector.timetable(
        STUDENT,
        timetable(a_lesson(memo="Apporter le manuel", background_color="#123456")),
    )

    assert events == []


def test_a_substitution_is_not_announced_as_a_cancellation() -> None:
    """The child still has a lesson on that slot.

    PRONOTE serves the original with ``estAnnule`` set **plus** a replacement
    with a higher ``num`` -- which is also how it serves a room change and a
    "cours maintenu". Reported as ``lesson_canceled``, a morning evaluation in
    another room was announced as an absent teacher.
    """
    detector = DeltaDetector()
    original = a_lesson(id="LESSON-1", num=0)
    detector.timetable(
        STUDENT,
        TimetableFacts(
            lessons=(original,), all_lessons=(original,), weeks_fetched=(28,)
        ),
    )

    cancelled = a_lesson(id="LESSON-1", num=0, canceled=True, status="Cours annulé")
    replacement = a_lesson(
        id="LESSON-2",
        num=1,
        subject="Évaluation",
        subject_id="SUBJECT-EVAL",
        status="Cours maintenu",
    )
    events = detector.timetable(
        STUDENT,
        TimetableFacts(
            lessons=(replacement,),
            all_lessons=(cancelled, replacement),
            weeks_fetched=(28,),
        ),
    )

    assert events == []


def test_a_cancellation_beside_a_cancelled_neighbour_is_still_announced() -> None:
    """Only a *taught* entry covers a slot; two cancelled ones leave it empty."""
    detector = DeltaDetector()
    other = a_lesson(id="LESSON-2", subject="Anglais", canceled=True)
    detector.timetable(STUDENT, timetable(a_lesson(), other))

    events = detector.timetable(STUDENT, timetable(a_lesson(canceled=True), other))

    assert [event.event_type for event in events] == [EVENT_LESSON_CANCELED]


def test_a_weekly_course_is_not_compared_with_its_sitting_next_week() -> None:
    """One weekly course carries one ``N`` in both weeks the timetable fetches.

    A map from ``N`` to a single key kept next week's sitting, so every
    collection compared this week's lesson with it: a move each time, and a
    cancellation re-announced every twenty minutes for a lesson cancelled this
    week and not the next.
    """
    next_week = a_lesson(
        id="lesson-next",
        ref="N-1",
        start=dt.datetime(2026, 3, 19, 8, 0, tzinfo=PARIS),
        end=dt.datetime(2026, 3, 19, 9, 0, tzinfo=PARIS),
    )
    detector = DeltaDetector()
    this_week = a_lesson(id="lesson-this", ref="N-1", canceled=True)
    detector.timetable(STUDENT, timetable(this_week, next_week))

    events = detector.timetable(STUDENT, timetable(this_week, next_week))

    assert events == []


def test_a_weekly_course_moved_this_week_is_followed_to_its_new_slot() -> None:
    """Its twin next week stays put, so the ``N`` still names one vacated slot."""
    next_week = a_lesson(
        id="lesson-next",
        ref="N-1",
        start=dt.datetime(2026, 3, 19, 8, 0, tzinfo=PARIS),
        end=dt.datetime(2026, 3, 19, 9, 0, tzinfo=PARIS),
    )
    detector = DeltaDetector()
    detector.timetable(
        STUDENT, timetable(a_lesson(id="lesson-this", ref="N-1"), next_week)
    )

    moved = a_lesson(
        id="lesson-moved",
        ref="N-1",
        start=dt.datetime(2026, 3, 12, 14, 0, tzinfo=PARIS),
        end=dt.datetime(2026, 3, 12, 15, 0, tzinfo=PARIS),
    )
    events = detector.timetable(STUDENT, timetable(moved, next_week))

    assert [event.event_type for event in events] == [EVENT_LESSON_MOVED]
    assert events[0].attributes["previous_start"] == "2026-03-12T08:00:00+01:00"


def test_two_vacated_sittings_of_one_course_are_not_guessed_between() -> None:
    """Both sittings moved at once: the ``N`` no longer says which is which."""
    detector = DeltaDetector()
    detector.timetable(
        STUDENT,
        timetable(
            a_lesson(id="lesson-a", ref="N-1"),
            a_lesson(
                id="lesson-b",
                ref="N-1",
                start=dt.datetime(2026, 3, 19, 8, 0, tzinfo=PARIS),
                end=dt.datetime(2026, 3, 19, 9, 0, tzinfo=PARIS),
            ),
        ),
    )

    moved = a_lesson(
        id="lesson-c",
        ref="N-1",
        start=dt.datetime(2026, 3, 12, 14, 0, tzinfo=PARIS),
        end=dt.datetime(2026, 3, 12, 15, 0, tzinfo=PARIS),
    )

    assert detector.timetable(STUDENT, timetable(moved)) == []


@pytest.mark.parametrize(
    "change",
    [
        pytest.param({"canceled": True}, id="canceled"),
        pytest.param({"status": "Prof. absent"}, id="status"),
        pytest.param({"classrooms": ("Salle 202",)}, id="classrooms"),
        pytest.param({"teachers": ("Prof. Deux",)}, id="teachers"),
        pytest.param(
            {"start": dt.datetime(2026, 3, 12, 14, 0, tzinfo=PARIS)}, id="start"
        ),
        pytest.param({"end": dt.datetime(2026, 3, 12, 10, 0, tzinfo=PARIS)}, id="end"),
    ],
)
def test_every_field_in_the_signature_produces_an_event(
    change: dict[str, object],
) -> None:
    """The drift canary, and it belongs here rather than in a DEBUG line.

    ``Lesson.change_signature`` is one list and ``_lesson_events`` is another,
    and they have to stay in step: a field added to the signature with no
    branch to report it means the detector notices a change and emits nothing,
    which is indistinguishable from no change at all. This test is the thing
    that fails when that happens -- at the moment it can still be fixed
    cheaply, rather than in a log nobody reads.

    One case per component. If a component is added to the signature and not
    to this list, the count assertion at the end of the module catches it.
    """
    detector = DeltaDetector()
    baseline = a_lesson()
    detector.timetable(STUDENT, timetable(baseline))

    events = detector.timetable(STUDENT, timetable(a_lesson(**change)))

    assert events, f"a change to {set(change)} produced no event"
    assert all(event.entity_key == "lesson_changed" for event in events)


def test_the_signature_has_exactly_the_components_the_events_cover() -> None:
    """Six components, six cases in the test above.

    Written as a count rather than by inspecting names, because the signature
    is a tuple by design -- it is compared, not read -- and giving it names
    just to satisfy a test would be the test dictating the design.
    """
    assert len(a_lesson().change_signature) == 6


def test_every_lesson_event_type_is_declared_on_the_entity() -> None:
    """A detector emitting an undeclared type is an integration bug.

    The entity drops it, silently, so nothing downstream would notice -- which
    is exactly why the two sets are compared here.
    """
    detector = DeltaDetector()
    detector.timetable(STUDENT, timetable(a_lesson()))

    seen: set[str] = set()
    for change in (
        a_lesson(canceled=True),
        a_lesson(status="Prof. absent"),
        a_lesson(classrooms=("Salle 202",)),
        a_lesson(teachers=("Prof. Deux",)),
        a_lesson(start=dt.datetime(2026, 3, 12, 14, 0, tzinfo=PARIS)),
    ):
        for event in detector.timetable(STUDENT, timetable(change)):
            seen.add(event.event_type)
        detector.forget(STUDENT)
        detector.timetable(STUDENT, timetable(a_lesson()))

    # `lesson_restored` needs the reverse transition, which the loop above
    # cannot produce from a live baseline.
    detector.forget(STUDENT)
    detector.timetable(STUDENT, timetable(a_lesson(canceled=True)))
    for event in detector.timetable(STUDENT, timetable(a_lesson())):
        seen.add(event.event_type)

    assert seen == set(LESSON_EVENT_TYPES)


# ---------------------------------------------------------------------------
# Discussions
# ---------------------------------------------------------------------------


def test_a_new_message_fires_once_per_unread_message() -> None:
    """Detected on the unread count, which is a budget decision.

    Identifying messages across every thread would cost one request per
    thread, because ``Discussion.messages`` posts every time it is read. The
    counter comes free with the thread list.
    """
    detector = DeltaDetector()
    detector.discussions(STUDENT, discussions(a_thread(unread=1)))

    events = detector.discussions(
        STUDENT,
        discussions(
            a_thread(
                unread=3,
                messages=(
                    a_message("M-1", 0),
                    a_message("M-2", 5),
                    a_message("M-3", 9),
                ),
            )
        ),
    )

    assert len(events) == 2
    assert {event.event_type for event in events} == {EVENT_MESSAGE_RECEIVED}
    # The two *newest* messages, because two is how many arrived.
    assert [event.attributes["created"] for event in events] == [
        "2026-03-11T10:05:00+01:00",
        "2026-03-11T10:09:00+01:00",
    ]


def test_reading_messages_elsewhere_fires_nothing() -> None:
    """A falling counter is not an arrival."""
    detector = DeltaDetector()
    detector.discussions(STUDENT, discussions(a_thread(unread=3)))

    assert detector.discussions(STUDENT, discussions(a_thread(unread=0))) == []


def test_a_message_after_the_counter_fell_is_still_detected() -> None:
    """Read on the phone, then a new message: the delta is from zero."""
    detector = DeltaDetector()
    detector.discussions(STUDENT, discussions(a_thread(unread=3)))
    detector.discussions(STUDENT, discussions(a_thread(unread=0)))

    events = detector.discussions(
        STUDENT, discussions(a_thread(unread=1, messages=(a_message("M-9"),)))
    )

    assert len(events) == 1


def test_an_unexpanded_thread_still_reports_that_something_arrived() -> None:
    """ "A message arrived" beats losing the event because of a budget cap.

    The automation's job is to say somebody wrote; losing that entirely would
    be worse than losing the author's name.
    """
    detector = DeltaDetector()
    detector.discussions(STUDENT, discussions(a_thread(unread=0)))

    events = detector.discussions(
        STUDENT,
        DiscussionsFacts(discussions=(a_thread(unread=2),), expanded=frozenset()),
    )

    assert len(events) == 1
    assert events[0].attributes["author"] is None
    assert events[0].attributes["created"] is None
    assert events[0].attributes["unread"] == 2


def test_a_thread_seen_for_the_first_time_is_capped_not_replayed() -> None:
    """A thread becomes visible for reasons other than a message arriving.

    A label moved, a restore from Trash, an establishment un-archiving a
    September conversation. Diffing that against zero -- which
    ``previous.get(id, 0)`` did -- replayed the whole thread as arrivals, which
    is the failure §2.2.1 exists to prevent. Notifying is still right; the cap
    is what keeps a restore from becoming a notification storm.
    """
    detector = DeltaDetector()
    detector.discussions(STUDENT, discussions())

    messages = tuple(a_message(f"M-{index}", index) for index in range(20))
    events = detector.discussions(
        STUDENT, discussions(a_thread("THREAD-NEW", unread=20, messages=messages))
    )

    assert len(events) == _NEW_THREAD_CAP


def test_a_small_new_thread_reports_everything_it_holds() -> None:
    """Below the cap, a genuinely new conversation notifies in full."""
    detector = DeltaDetector()
    detector.discussions(STUDENT, discussions())

    events = detector.discussions(
        STUDENT,
        discussions(
            a_thread(
                "THREAD-NEW", unread=2, messages=(a_message("M-1"), a_message("M-2", 5))
            )
        ),
    )

    assert len(events) == 2


def test_a_new_thread_with_nothing_unread_fires_nothing() -> None:
    """A thread the parent started, or one already read on the phone.

    Diffing zero against zero produced a context-less "somebody wrote to you"
    for every such thread on the cycle it first appeared -- which, after a
    reload, is every thread the account holds.
    """
    detector = DeltaDetector()
    detector.discussions(STUDENT, discussions())

    assert detector.discussions(STUDENT, discussions(a_thread("THREAD-NEW"))) == []


def test_a_message_event_names_its_thread() -> None:
    """So an automation can say "reply to the sports teacher"."""
    detector = DeltaDetector()
    detector.discussions(STUDENT, discussions(a_thread(unread=0)))

    event = detector.discussions(
        STUDENT, discussions(a_thread(unread=1, messages=(a_message("M-1"),)))
    )[0]

    assert event.entity_key == "new_message"
    assert event.attributes["discussion"] == "Sortie du 20 mars"
    assert event.attributes["discussion_id"] == "THREAD-1"
    assert event.attributes["author"] == "Direction"


@pytest.mark.parametrize(
    "event_type",
    [
        EVENT_GRADE_ADDED,
        EVENT_HOMEWORK_ADDED,
        EVENT_INFORMATION_ADDED,
        EVENT_ABSENCE_ADDED,
        EVENT_DELAY_ADDED,
        EVENT_PUNISHMENT_ADDED,
        EVENT_EVALUATION_ADDED,
        EVENT_MESSAGE_RECEIVED,
    ],
)
def test_every_event_type_has_an_identifier_in_its_attributes(
    event_type: str,
) -> None:
    """An event with no identifier cannot be de-duplicated by a consumer.

    Which matters because Home Assistant replays the last event on restart,
    and an automation that wants to avoid acting twice needs something to
    compare.
    """
    detector = DeltaDetector()
    detector.marks(STUDENT, marks())
    detector.homework(STUDENT, HomeworkFacts(homework=()))
    detector.news(STUDENT, NewsFacts(information=()))
    detector.attendance(STUDENT, attendance())
    detector.evaluations(
        STUDENT, EvaluationsFacts(period_id="PERIOD-1", period_index=2, evaluations=())
    )
    detector.discussions(STUDENT, discussions(a_thread(unread=0)))

    events = [
        *detector.marks(STUDENT, marks(a_grade())),
        *detector.homework(STUDENT, HomeworkFacts(homework=(a_homework(),))),
        *detector.news(STUDENT, NewsFacts(information=(an_information(),))),
        *detector.attendance(
            STUDENT,
            attendance(
                absences=(an_absence(),),
                delays=(a_delay(),),
                punishments=(a_punishment(),),
            ),
        ),
        *detector.evaluations(
            STUDENT,
            EvaluationsFacts(
                period_id="PERIOD-1", period_index=2, evaluations=(an_evaluation(),)
            ),
        ),
        *detector.discussions(
            STUDENT, discussions(a_thread(unread=1, messages=(a_message("M-1"),)))
        ),
    ]

    matching = [event for event in events if event.event_type == event_type]
    assert matching, f"no {event_type} event was produced"
    for event in matching:
        assert any(key.endswith("_id") or key == "id" for key in event.attributes)


# ---------------------------------------------------------------------------
# Across a reconnection: PRONOTE re-encrypts every `N` at each login
# ---------------------------------------------------------------------------


def test_a_lesson_that_moves_inside_one_session_is_followed_by_its_n() -> None:
    """The key is built from the slot, so a move changes it; the ``N`` does not.

    Inside the session that produced the previous timetable, the ``N`` is the
    only thing that can say "this is the same lesson, somewhere else".
    """
    detector = DeltaDetector()
    detector.timetable(STUDENT, timetable(a_lesson(id="lesson-slot-a", ref="N-1")))

    moved = a_lesson(
        id="lesson-slot-b",
        ref="N-1",
        start=dt.datetime(2026, 3, 12, 14, 0, tzinfo=PARIS),
        end=dt.datetime(2026, 3, 12, 15, 0, tzinfo=PARIS),
    )
    events = detector.timetable(STUDENT, timetable(moved))

    assert [event.event_type for event in events] == [EVENT_LESSON_MOVED]


def test_a_cancellation_published_across_a_reconnection_is_still_announced() -> None:
    """Paired on the ``N`` alone, it was compared with nothing and never fired.

    That is the event the one live automation on this integration listens to.
    """
    detector = DeltaDetector()
    detector.timetable(STUDENT, timetable(a_lesson(id="lesson-slot-a", ref="N-1")))

    canceled = a_lesson(id="lesson-slot-a", ref="N-2", canceled=True)
    events = detector.timetable(STUDENT, timetable(canceled))

    assert [event.event_type for event in events] == [EVENT_LESSON_CANCELED]


def test_across_a_reconnection_a_lesson_in_a_new_slot_is_left_unpaired() -> None:
    """New ``N`` and new key: nothing to compare with, so nothing is claimed.

    Pairing on anything looser -- the subject alone, the day -- is what fired a
    move for the whole week when the bell times shifted.
    """
    detector = DeltaDetector()
    detector.timetable(STUDENT, timetable(a_lesson(id="lesson-slot-a", ref="N-1")))

    elsewhere = a_lesson(
        id="lesson-slot-b",
        ref="N-2",
        start=dt.datetime(2026, 3, 12, 14, 0, tzinfo=PARIS),
        end=dt.datetime(2026, 3, 12, 15, 0, tzinfo=PARIS),
    )

    assert detector.timetable(STUDENT, timetable(elsewhere)) == []


def test_a_renamed_child_keeps_the_lesson_pairing_by_n() -> None:
    """``rename`` carries the ``N`` map along with the signatures."""
    detector = DeltaDetector()
    detector.timetable("OLD", timetable(a_lesson(id="lesson-slot-a", ref="N-1")))
    detector.rename("OLD", STUDENT)

    moved = a_lesson(
        id="lesson-slot-b",
        ref="N-1",
        start=dt.datetime(2026, 3, 12, 14, 0, tzinfo=PARIS),
        end=dt.datetime(2026, 3, 12, 15, 0, tzinfo=PARIS),
    )

    assert [e.event_type for e in detector.timetable(STUDENT, timetable(moved))] == [
        EVENT_LESSON_MOVED
    ]


def test_a_grade_published_across_a_reconnection_is_announced() -> None:
    """The period's ``N`` rotates too, and it used to name the collection.

    A new collection name primes instead of comparing, so a grade that arrived
    between two sessions was swallowed by the priming pass.
    """
    detector = DeltaDetector()
    detector.marks(STUDENT, marks(a_grade()))

    after_login = dataclasses.replace(
        marks(a_grade(), a_grade("GRADE-2")), period_id="PERIOD-1-AFTER-THE-LOGIN"
    )
    events = detector.marks(STUDENT, after_login)

    assert [event.event_type for event in events] == [EVENT_GRADE_ADDED]


def test_attendance_published_across_a_reconnection_is_announced() -> None:
    """Absences, delays and punishments used to be named by the period's ``N``.

    The same defect the grades had, left behind in three collections: the
    first collection after a login found ``absences:<fresh N>``, primed it and
    returned nothing, so the absence recorded between the two sessions -- the
    one a parent most wants to hear about -- never fired.
    """
    detector = DeltaDetector()
    detector.attendance(
        STUDENT,
        attendance(
            absences=(an_absence(),),
            delays=(a_delay(),),
            punishments=(a_punishment(),),
        ),
    )

    after_login = dataclasses.replace(
        attendance(
            absences=(an_absence(), an_absence("ABSENCE-2")),
            delays=(a_delay(), a_delay("DELAY-2")),
            punishments=(a_punishment(), a_punishment("PUNISHMENT-2")),
        ),
        period_id="PERIOD-1-AFTER-THE-LOGIN",
    )
    events = detector.attendance(STUDENT, after_login)

    assert [event.event_type for event in events] == [
        EVENT_ABSENCE_ADDED,
        EVENT_DELAY_ADDED,
        EVENT_PUNISHMENT_ADDED,
    ]


def test_an_evaluation_published_across_a_reconnection_is_announced() -> None:
    """The evaluations collection was named by the period's ``N`` as well."""
    detector = DeltaDetector()
    detector.evaluations(
        STUDENT,
        EvaluationsFacts(
            period_id="PERIOD-1", period_index=2, evaluations=(an_evaluation(),)
        ),
    )

    events = detector.evaluations(
        STUDENT,
        EvaluationsFacts(
            period_id="PERIOD-1-AFTER-THE-LOGIN",
            period_index=2,
            evaluations=(an_evaluation(), an_evaluation("EVALUATION-2")),
        ),
    )

    assert [event.event_type for event in events] == [EVENT_EVALUATION_ADDED]
    assert events[0].attributes["evaluation_id"] == "EVALUATION-2"


def test_an_unchanged_list_under_a_rotated_period_announces_nothing() -> None:
    """The control: a new name for the period is not a new item.

    Keyed by position, the collection is the same one after the login, and a
    list that did not change must stay silent -- the fix must not trade a
    missed event for a replayed term.
    """
    detector = DeltaDetector()
    before = attendance(absences=(an_absence(),), delays=(a_delay(),))
    detector.attendance(STUDENT, before)
    detector.evaluations(
        STUDENT,
        EvaluationsFacts(
            period_id="PERIOD-1", period_index=2, evaluations=(an_evaluation(),)
        ),
    )

    assert (
        detector.attendance(
            STUDENT, dataclasses.replace(before, period_id="PERIOD-1-ROTATED")
        )
        == []
    )
    assert (
        detector.evaluations(
            STUDENT,
            EvaluationsFacts(
                period_id="PERIOD-1-ROTATED",
                period_index=2,
                evaluations=(an_evaluation(),),
            ),
        )
        == []
    )


def test_attendance_is_scoped_to_its_period() -> None:
    """Another period is still another collection, primed rather than diffed.

    Keying by position must not merge two periods: the first term's absences
    read against the second term's would all be "new" on the day the term
    changes.
    """
    detector = DeltaDetector()
    detector.attendance(STUDENT, attendance(absences=(an_absence(),)))

    next_term = dataclasses.replace(
        attendance(absences=(an_absence(), an_absence("ABSENCE-2"))),
        period_id="PERIOD-2",
        period_index=3,
    )

    assert detector.attendance(STUDENT, next_term) == []
