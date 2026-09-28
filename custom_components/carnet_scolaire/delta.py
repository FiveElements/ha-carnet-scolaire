"""Change detection: two rules, because one cannot do the job.

Specification v1 had a single rule -- "delta on the ``N`` identifier, never on
content". It is right for most collections and **incapable of implementing
``cours_modifie``**, which is the most useful event of the set. Two reasons,
both verified:

* a room change keeps the same ``N``, so it produces no identifier delta at
  all, and ``room_changed`` / ``teacher_changed`` would never fire;
* a replacement arrives with a fresh ``N`` while the original entry is still
  present, so it reads as an *addition* rather than a modification -- and
  ``previous_start`` / ``previous_classroom`` have no source whatsoever.

So the rule splits (§2.2.1):

**Append-only collections** -- grades, homework, news, absences, delays,
punishments, messages, evaluations -- keep the identifier rule exactly as
written. A label a teacher corrects must not wake the house up.

**The timetable** compares a whitelisted field tuple **per lesson
identifier**, over the *un*-de-duplicated week. ``memo``,
``background_color`` and lesson content are excluded, so a corrected memo
still fires nothing -- which is what the single rule was really trying to buy.

Both halves of that sentence were wrong in the first implementation, and both
were wrong in a way that produced no error at all:

* it keyed on the **slot** (day plus position in the day). Since the day is
  part of a slot key, a lesson that moved could never be seen to have moved:
  ``lesson_moved`` was structurally unreachable, while a change to the
  establishment's slot *grid* -- the bell times shifting by five minutes --
  moved ``start`` for every lesson in every slot at once and fired a
  ``lesson_moved`` for the entire week;
* it ran over the **de-duplicated** week. On a substitution PRONOTE serves the
  original entry with ``estAnnule`` set *plus* a replacement with a higher
  ``num``, and de-duplication keeps the replacement -- so the entry carrying
  the cancellation had been discarded one layer down, and the cancellation was
  unobservable.

Keying on ``N`` fixes both: a cancellation, a room change, a teacher change and
a move are all *the same entry* changing, which is exactly what an identifier
is for.

Stated generally: **delta on identity where identity is stable, on a
whitelisted subset of fields where the protocol supersedes instead of
appending.**

And ``N`` turned out to be stable for **one session only**: PRONOTE
re-encrypts it at every login. Keyed on it, the first collection after a
reconnection announced every homework item of the year as new -- fifty-four
events in one millisecond, measured -- and compared no lesson with anything.
So the identity this module sees is the key :mod:`.item_keys` mints from the
content, and the lesson pairing still uses ``N`` where it is valid: inside the
session that produced the previous timetable, which is the only way to follow a
*move*, since a lesson's key is built from its slot.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING, Any, Final

from .const import (
    EVENT_ABSENCE_ADDED,
    EVENT_DELAY_ADDED,
    EVENT_EVALUATION_ADDED,
    EVENT_GRADE_ADDED,
    EVENT_HOMEWORK_ADDED,
    EVENT_INFORMATION_ADDED,
    EVENT_LESSON_CANCELED,
    EVENT_LESSON_MOVED,
    EVENT_LESSON_REPLACED,
    EVENT_LESSON_RESTORED,
    EVENT_LESSON_STATUS_CHANGED,
    EVENT_MESSAGE_RECEIVED,
    EVENT_PUNISHMENT_ADDED,
    EVENT_ROOM_CHANGED,
    EVENT_TEACHER_CHANGED,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from .models import (
        Absence,
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
        NewsFacts,
        Punishment,
        TimetableFacts,
    )

_LOGGER: Final = logging.getLogger(__name__)

#: Most ``message_received`` events a thread may fire on its **first** sighting.
#:
#: A thread becomes visible for the first time for reasons other than a message
#: arriving -- a label moved, a restore from Trash, an establishment
#: un-archiving an old conversation -- and treating its whole unread count as
#: arrivals replayed the thread. Three is enough for "somebody wrote to you"
#: without turning a restore into a notification storm.
_NEW_THREAD_CAP: Final = 3


@dataclass(frozen=True, slots=True)
class DeltaEvent:
    """One change, ready to be fired on an ``event`` entity.

    Eight new grades produce **eight** events, not one aggregated event: an
    automation that notifies per grade must be able to, and aggregating is
    trivial for the user to redo whereas separating is not (annexe A §4).
    """

    #: Functional key of the ``event`` entity, e.g. ``"new_grade"``.
    entity_key: str
    #: The ``event_type`` fired, which distinguishes the nature of the change.
    event_type: str
    #: Context, exposed as the event's attributes.
    attributes: dict[str, Any]


class DeltaDetector:
    """Compares consecutive snapshots per (student, tier) and yields events.

    The first snapshot of a tier after a start or a reload emits **nothing**.
    Without that, every Home Assistant restart would replay the whole term
    (§2.2.1). The detector holds only the identity material it needs, never the
    snapshots themselves, so a term's worth of grades does not sit in memory
    twice.
    """

    def __init__(self) -> None:
        self._seen_ids: dict[tuple[str, str], set[str]] = {}
        #: Per student, per lesson key: the last signature seen for it.
        self._lesson_signatures: dict[str, dict[str, _LessonMemo]] = {}
        #: Per student, the session ``N`` -> keys map of the last timetable --
        #: keys, plural, because one weekly course has one ``N`` for every
        #: sitting. A moved lesson keeps its ``N`` inside one session but not
        #: its key, which is built from the slot; see :meth:`timetable`.
        self._lesson_refs: dict[str, dict[str, list[str]]] = {}
        self._unread: dict[str, dict[str, int]] = {}

    # -- generic append-only handling --------------------------------------

    def _new_ids(
        self, student_id: str, collection: str, ids: Iterable[str]
    ) -> list[str] | None:
        """Return the identifiers not seen before, or ``None`` on the first pass.

        ``None`` rather than an empty list, because "nothing is new" and "this
        is the first snapshot" must lead to different behaviour and a caller
        that cannot tell them apart will replay the term.
        """
        key = (student_id, collection)
        incoming = list(dict.fromkeys(ids))
        previous = self._seen_ids.get(key)
        self._seen_ids[key] = set(incoming)
        if previous is None:
            _LOGGER.debug(
                "priming %s for %s with %d items; no events on a first snapshot",
                collection,
                student_id,
                len(incoming),
            )
            return None
        return [item for item in incoming if item not in previous]

    def rename(self, old: str, new: str) -> None:
        """Carry what is remembered about one student to a new identifier.

        PRONOTE renames a child at every login. Without this, the first
        snapshot under the new identifier was a "first pass" and primed the
        detector instead of comparing: a grade published in between was
        never announced.
        """
        for key in [k for k in self._seen_ids if k[0] == old]:
            self._seen_ids[(new, key[1])] = self._seen_ids.pop(key)
        if old in self._lesson_signatures:
            self._lesson_signatures[new] = self._lesson_signatures.pop(old)
        if old in self._lesson_refs:
            self._lesson_refs[new] = self._lesson_refs.pop(old)
        if old in self._unread:
            self._unread[new] = self._unread.pop(old)

    def forget(self, student_id: str) -> None:
        """Drop everything remembered about one student."""
        for key in [k for k in self._seen_ids if k[0] == student_id]:
            del self._seen_ids[key]
        self._lesson_signatures.pop(student_id, None)
        self._lesson_refs.pop(student_id, None)
        self._unread.pop(student_id, None)

    # -- marks -------------------------------------------------------------

    def marks(self, student_id: str, facts: MarksFacts) -> list[DeltaEvent]:
        """New grades, by identifier."""
        new = self._new_ids(
            student_id,
            # The period's position, not its `N`: that is re-encrypted by every
            # login, and a new collection name primes instead of comparing --
            # a grade published across a reconnection was never announced.
            f"grades:{facts.period_index}",
            (grade.id for grade in facts.grades),
        )
        if not new:
            return []
        index = {grade.id: grade for grade in facts.grades}
        return [
            DeltaEvent("new_grade", EVENT_GRADE_ADDED, _grade_context(index[gid]))
            for gid in new
            if gid in index
        ]

    # -- homework ----------------------------------------------------------

    def homework(self, student_id: str, facts: HomeworkFacts) -> list[DeltaEvent]:
        """Newly published homework, by identifier."""
        new = self._new_ids(
            student_id, "homework", (item.id for item in facts.homework)
        )
        if not new:
            return []
        index = {item.id: item for item in facts.homework}
        return [
            DeltaEvent(
                "new_homework", EVENT_HOMEWORK_ADDED, _homework_context(index[hid])
            )
            for hid in new
            if hid in index
        ]

    # -- news --------------------------------------------------------------

    def news(self, student_id: str, facts: NewsFacts) -> list[DeltaEvent]:
        """Newly visible news items, by identifier."""
        new = self._new_ids(student_id, "news", (item.id for item in facts.information))
        if not new:
            return []
        index = {item.id: item for item in facts.information}
        return [
            DeltaEvent(
                "new_information",
                EVENT_INFORMATION_ADDED,
                _information_context(index[nid]),
            )
            for nid in new
            if nid in index
        ]

    # -- attendance --------------------------------------------------------

    def attendance(self, student_id: str, facts: AttendanceFacts) -> list[DeltaEvent]:
        """New absences, delays and punishments -- three separate collections.

        Absences and delays get **separate** event entities, and that follows
        from their shapes rather than from taste: ``Absence`` carries ``hours``
        and ``days`` while ``Delay`` carries ``minutes`` and ``justification``.
        One entity with two differently-shaped event types would force every
        automation to test the type before reading an attribute -- that is, to
        write the condition this project exists to remove.
        """
        events: list[DeltaEvent] = []

        # Each collection is named by the period's position, as for grades,
        # and for the same reason: the period's `N` is re-encrypted by every
        # login, and a renamed collection primed instead of comparing -- an
        # absence recorded across a reconnection was never announced.
        new_absences = self._new_ids(
            student_id,
            f"absences:{facts.period_index}",
            (item.id for item in facts.absences),
        )
        if new_absences:
            index = {item.id: item for item in facts.absences}
            events.extend(
                DeltaEvent(
                    "new_absence", EVENT_ABSENCE_ADDED, _absence_context(index[aid])
                )
                for aid in new_absences
                if aid in index
            )

        new_delays = self._new_ids(
            student_id,
            f"delays:{facts.period_index}",
            (item.id for item in facts.delays),
        )
        if new_delays:
            delay_index = {item.id: item for item in facts.delays}
            events.extend(
                DeltaEvent(
                    "new_delay", EVENT_DELAY_ADDED, _delay_context(delay_index[did])
                )
                for did in new_delays
                if did in delay_index
            )

        new_punishments = self._new_ids(
            student_id,
            f"punishments:{facts.period_index}",
            (item.id for item in facts.punishments),
        )
        if new_punishments:
            punishment_index = {item.id: item for item in facts.punishments}
            events.extend(
                DeltaEvent(
                    "new_punishment",
                    EVENT_PUNISHMENT_ADDED,
                    _punishment_context(punishment_index[pid]),
                )
                for pid in new_punishments
                if pid in punishment_index
            )

        return events

    # -- evaluations -------------------------------------------------------

    def evaluations(self, student_id: str, facts: EvaluationsFacts) -> list[DeltaEvent]:
        """New competency evaluations, by identifier.

        The collection is named by the period's position, never its session
        ``N``; see :meth:`attendance`.
        """
        new = self._new_ids(
            student_id,
            f"evaluations:{facts.period_index}",
            (item.id for item in facts.evaluations),
        )
        if not new:
            return []
        index = {item.id: item for item in facts.evaluations}
        return [
            DeltaEvent(
                "new_evaluation",
                EVENT_EVALUATION_ADDED,
                _evaluation_context(index[eid]),
            )
            for eid in new
            if eid in index
        ]

    # -- discussions -------------------------------------------------------

    def discussions(self, student_id: str, facts: DiscussionsFacts) -> list[DeltaEvent]:
        """Messages received, detected on the unread count.

        Not on message identifiers, and that is a budget decision rather than a
        modelling one: ``pronotepy.Discussion.messages`` posts every time it is
        read, so identifying messages across every thread would cost one request
        per thread. The unread counter comes free with the thread list, and the
        gateway expands only the threads whose counter went up -- so an event
        normally carries real message context and always costs at most one extra
        request.
        """
        previous = self._unread.get(student_id)
        current = {thread.id: thread.unread for thread in facts.discussions}
        self._unread[student_id] = current

        if previous is None:
            _LOGGER.debug(
                "priming discussions for %s with %d threads; no events on a "
                "first snapshot",
                student_id,
                len(current),
            )
            return []

        events: list[DeltaEvent] = []
        for thread in facts.discussions:
            before = previous.get(thread.id)
            if before is None and thread.unread <= 0:
                # A thread appearing with nothing unread is not an arrival: a
                # conversation the parent started, or one already read on the
                # phone before the integration ever saw it. Diffing zero
                # against zero produced a context-less "somebody wrote to you"
                # for every such thread on the cycle it first appeared.
                continue
            if before is None:
                # A thread never seen before, which is not the same as a thread
                # that had no unread messages -- and the two used to be
                # collapsed by `previous.get(id, 0)`. A conversation becomes
                # visible for the first time for reasons other than a message
                # arriving: a label moved, a thread restored from Trash, an
                # establishment un-archiving a September discussion. Diffing
                # that against zero replayed the whole thread as arrivals,
                # which is the failure §2.2.1 exists to prevent.
                #
                # It is still worth notifying -- a genuinely new conversation
                # is the common case -- so the count is treated as a delta from
                # zero but capped, and the cap is what makes the difference
                # between "somebody wrote to you" and forty push notifications.
                events.extend(
                    _message_events(thread, max(0, thread.unread - _NEW_THREAD_CAP))
                )
                if thread.unread > _NEW_THREAD_CAP:
                    _LOGGER.debug(
                        "discussion %s appeared with %d unread messages; "
                        "reporting the %d most recent rather than replaying the "
                        "thread",
                        thread.id,
                        thread.unread,
                        _NEW_THREAD_CAP,
                    )
                continue
            if thread.unread <= before:
                continue
            events.extend(_message_events(thread, before))
        return events

    # -- the timetable: the two-step rule ---------------------------------

    def timetable(self, student_id: str, facts: TimetableFacts) -> list[DeltaEvent]:
        """Detect modified lessons on a whitelisted field tuple.

        Runs over ``facts.all_lessons`` -- the week *before* de-duplication --
        because de-duplication discards the entry that carries a
        substitution's cancellation.

        Each entry is paired with its previous self in two ways, and the order
        matters. By PRONOTE's ``N`` first, when the previous timetable came
        from the same session: that is the only pairing that survives a
        *move*, since the minted key is built from the slot and a moved lesson
        changes slot. By the key otherwise: the ``N`` is re-encrypted by every
        login, and pairing on it alone meant a cancellation published across a
        reconnection was compared with nothing and never announced.

        The ``N`` names the *course*, not one sitting of it: the same weekly
        lesson carries the same ``N`` in both weeks fetched. So the ``N`` only
        arbitrates when the key cannot -- an entry whose own key is among the
        entries sharing its ``N`` is paired with itself, and the ``N`` decides
        only between entries whose slot has gone. A map from ``N`` to one key
        kept next week's sitting, and every collection compared this week's
        lessons with their twins seven days on: a move for each of them, a
        cancellation for each lesson cancelled this week and not the next,
        re-announced every twenty minutes.

        Note what is *not* emitted: an entry appearing for the first time is not
        a change. A newly published timetable week would otherwise fire an
        event for every lesson in it.
        """
        memos = self._lesson_signatures.get(student_id)
        previous_refs = self._lesson_refs.get(student_id, {})
        replacements = {
            lesson.id: _replacement(lesson, facts) for lesson in facts.all_lessons
        }
        current = {
            lesson.id: _LessonMemo.of(
                lesson, replaced=replacements[lesson.id] is not None
            )
            for lesson in facts.all_lessons
        }
        self._lesson_signatures[student_id] = current
        refs: dict[str, list[str]] = {}
        for lesson in facts.all_lessons:
            if lesson.ref:
                refs.setdefault(lesson.ref, []).append(lesson.id)
        self._lesson_refs[student_id] = refs

        if memos is None:
            _LOGGER.debug(
                "priming the timetable for %s with %d entries; no events on a "
                "first snapshot",
                student_id,
                len(current),
            )
            return []

        found: list[tuple[Lesson, bool, list[DeltaEvent]]] = []
        for lesson in facts.all_lessons:
            before = memos.get(_previous_key(lesson, previous_refs, current))
            now = current[lesson.id]
            if before is None or (
                before.signature == now.signature and before.replaced == now.replaced
            ):
                continue
            found.append(
                (
                    lesson,
                    before.replaced,
                    _lesson_events(lesson, before, replacements[lesson.id]),
                )
            )

        # One slot, one cancellation. A replacement cancelled in turn empties
        # the slot twice over: once as its own cancellation, once as the
        # original losing its cover. The first is the lesson the child was
        # going to have, so it is the one reported.
        announced = [
            lesson
            for lesson, was_replaced, changes in found
            if not was_replaced
            and any(event.event_type == EVENT_LESSON_CANCELED for event in changes)
        ]
        events: list[DeltaEvent] = []
        for lesson, was_replaced, changes in found:
            twice = was_replaced and any(_overlap(lesson, other) for other in announced)
            events.extend(
                event
                for event in changes
                if not (twice and event.event_type == EVENT_LESSON_CANCELED)
            )
        return events


def _previous_key(
    lesson: Lesson, previous_refs: dict[str, list[str]], current: dict[str, Any]
) -> str:
    """The key this entry had in the previous timetable.

    Its own key, unless the ``N`` says it came from a slot that has since
    emptied -- that is a move, and only the ``N`` can see it. Any other answer
    pairs one sitting of a weekly course with another sitting of the same
    course, which shares the ``N``.
    """
    candidates = previous_refs.get(lesson.ref, []) if lesson.ref else []
    if not candidates or lesson.id in candidates:
        return lesson.id
    vacated = [key for key in candidates if key not in current]
    return vacated[0] if len(vacated) == 1 else lesson.id


def _replacement(lesson: Lesson, facts: TimetableFacts) -> Lesson | None:
    """The entry taught in place of this cancelled one, if any.

    That is how PRONOTE serves a substitution, a room change and a "cours
    maintenu": the original flagged ``estAnnule`` **plus** the entry that
    replaces it on the slot. The child still has a lesson then, and announcing
    the original as cancelled told a family the teacher was absent on a morning
    the child had an evaluation in another room. Among several, the one with
    the highest ``num`` -- the one PRONOTE displays.
    """
    if not lesson.canceled:
        return None
    covering = [
        other
        for other in facts.all_lessons
        if other is not lesson and not other.canceled and _overlap(lesson, other)
    ]
    return max(covering, key=lambda other: (other.num, other.start), default=None)


def _overlap(one: Lesson, other: Lesson) -> bool:
    """Whether two entries are taught, even partly, at the same time."""
    return other.start < one.end and one.start < other.end


@dataclass(frozen=True, slots=True)
class _LessonMemo:
    """The minimum kept between snapshots to describe a change.

    Only the whitelisted signature plus the few fields an event's
    ``previous_*`` attributes need -- specifically the fields specification v1
    promised and had no source for.
    """

    signature: tuple[object, ...]
    canceled: bool
    status: str | None
    classroom: str | None
    classrooms: frozenset[str]
    teachers: tuple[str, ...]
    start_iso: str
    end_iso: str
    #: Cancelled, with another entry taught on its slot -- see `_replacement`.
    replaced: bool = False

    @classmethod
    def of(cls, lesson: Lesson, *, replaced: bool = False) -> _LessonMemo:
        """Capture one lesson."""
        return cls(
            signature=lesson.change_signature,
            canceled=lesson.canceled,
            status=lesson.status,
            classroom=lesson.classroom,
            classrooms=frozenset(lesson.classrooms),
            teachers=lesson.teachers,
            start_iso=lesson.start.isoformat(),
            end_iso=lesson.end.isoformat(),
            replaced=replaced,
        )


def _slot_state(*, canceled: bool, replaced: bool) -> str:
    """What the child has on this entry's slot: the lesson, another, or none."""
    if replaced:
        return "replaced"
    return "canceled" if canceled else "held"


def _lesson_events(
    lesson: Lesson, before: _LessonMemo, replacement: Lesson | None = None
) -> list[DeltaEvent]:
    """Turn one entry's change into the specific event types it represents.

    A single change can legitimately be more than one thing -- a lesson moved
    *and* put in another room -- so this yields one event per aspect that
    actually changed rather than picking a winner. Each event gets its **own**
    attributes dict: they are handed to ``async_fire`` and end up on an entity's
    state, and four events sharing one mutable mapping is a bug waiting for the
    first consumer that annotates what it received.

    There is deliberately no "if nothing matched, call it a cancellation"
    fallback any more. Every branch of the whitelisted signature now has a name
    of its own, so the fallback could only ever fire for a transition it was
    wrong about -- and the transition it actually fired for was a cancellation
    being **lifted**, which it reported as ``lesson_canceled``.
    """
    context = _lesson_context(lesson, before)
    events: list[DeltaEvent] = []

    def emit(event_type: str) -> None:
        events.append(DeltaEvent("lesson_changed", event_type, dict(context)))

    was = _slot_state(canceled=before.canceled, replaced=before.replaced)
    now = _slot_state(canceled=lesson.canceled, replaced=replacement is not None)

    # A cancelled entry whose slot is taught by another is a replacement, not a
    # cancellation: the child still has a lesson. And once replaced, the
    # original's own time, room and status describe a lesson nobody attends, so
    # nothing else about it is reported.
    if replacement is not None:
        if was != "replaced":
            events.append(
                DeltaEvent(
                    "lesson_changed",
                    EVENT_LESSON_REPLACED,
                    {**context, **_replacement_context(replacement)},
                )
            )
        return events
    if now == "canceled" and was != "canceled":
        emit(EVENT_LESSON_CANCELED)
    elif now == "held" and was != "held":
        emit(EVENT_LESSON_RESTORED)

    # `end` counts as a move too: a lesson shortened or extended in place has
    # moved for every purpose an automation cares about, and reporting nothing
    # would leave the signature change unexplained.
    if (
        lesson.start.isoformat() != before.start_iso
        or lesson.end.isoformat() != before.end_iso
    ):
        emit(EVENT_LESSON_MOVED)

    # Sets, not sequences: nothing in the protocol orders `ListeSalles` or
    # `ListeProfesseurs`, and a reordered list is not a change.
    if frozenset(lesson.classrooms) != before.classrooms:
        emit(EVENT_ROOM_CHANGED)

    if frozenset(lesson.teachers) != frozenset(before.teachers):
        emit(EVENT_TEACHER_CHANGED)

    if not events and lesson.status != before.status:
        emit(EVENT_LESSON_STATUS_CHANGED)

    # Every component of `Lesson.change_signature` now maps to a branch above,
    # so this list cannot come back empty for a lesson whose signature moved --
    # a replaced entry, which returned early, is the one silence meant.
    # That invariant is *checked*, in `tests/test_delta.py`, rather than
    # guarded by a runtime log nobody reads: adding a field to the signature
    # without adding a branch here fails CI, which is the moment it can still
    # be fixed cheaply.
    return events


# ---------------------------------------------------------------------------
# Context builders -- the attributes an automation reads
# ---------------------------------------------------------------------------


def _lesson_context(lesson: Lesson, before: _LessonMemo) -> dict[str, Any]:
    """Attributes for ``event.<student>_cours_modifie``."""
    return {
        "subject": lesson.subject,
        "start": lesson.start.isoformat(),
        "end": lesson.end.isoformat(),
        "previous_start": before.start_iso,
        "previous_end": before.end_iso,
        "classroom": lesson.classroom,
        "previous_classroom": before.classroom,
        "teachers": list(lesson.teachers),
        "previous_teachers": list(before.teachers),
        "status": lesson.status,
        "canceled": lesson.canceled,
        "lesson_id": lesson.id,
    }


def _replacement_context(replacement: Lesson) -> dict[str, Any]:
    """What ``lesson_replaced`` adds: the lesson taught in the original's place."""
    return {
        "replacement_subject": replacement.subject,
        "replacement_classroom": replacement.classroom,
        "replacement_teachers": list(replacement.teachers),
        "replacement_status": replacement.status,
    }


def _grade_context(grade: Grade) -> dict[str, Any]:
    """Attributes for ``event.<student>_nouvelle_note``."""
    return {
        "subject": grade.subject,
        "grade": grade.value,
        "out_of": grade.out_of,
        "coefficient": grade.coefficient,
        "date": grade.date.isoformat(),
        "class_average": grade.class_average,
        "status": str(grade.status) if grade.status else None,
        "grade_id": grade.id,
    }


def _homework_context(homework: Homework) -> dict[str, Any]:
    """Attributes for ``event.<student>_nouveau_devoir``."""
    return {
        "subject": homework.subject,
        "description": homework.description,
        "due": homework.due.isoformat(),
        "id": homework.id,
    }


def _information_context(information: Information) -> dict[str, Any]:
    """Attributes for ``event.<student>_nouvelle_actualite``."""
    return {
        "author": information.author,
        "title": information.title,
        "category": information.category,
        "survey": information.survey,
        "information_id": information.id,
    }


def _absence_context(absence: Absence) -> dict[str, Any]:
    """Attributes for ``event.<student>_nouvelle_absence``.

    ``hours`` and ``days`` -- there is no ``minutes`` on an absence.
    """
    return {
        "from_date": absence.from_date.isoformat(),
        "to_date": absence.to_date.isoformat(),
        "justified": absence.justified,
        "reasons": list(absence.reasons),
        "hours": absence.hours,
        "days": absence.days,
        "absence_id": absence.id,
    }


def _delay_context(delay: Delay) -> dict[str, Any]:
    """Attributes for ``event.<student>_nouveau_retard``."""
    return {
        "date": delay.at.isoformat(),
        "justified": delay.justified,
        "justification": delay.justification,
        "reasons": list(delay.reasons),
        "minutes": delay.minutes,
        "delay_id": delay.id,
    }


def _punishment_context(punishment: Punishment) -> dict[str, Any]:
    """Attributes for ``event.<student>_nouvelle_punition``."""
    return {
        "nature": punishment.nature,
        "reasons": list(punishment.reasons),
        "giver": punishment.giver,
        "exclusion": punishment.exclusion,
        "schedule": [slot.start.isoformat() for slot in punishment.schedule],
        "punishment_id": punishment.id,
    }


def _evaluation_context(evaluation: Evaluation) -> dict[str, Any]:
    """Attributes for ``event.<student>_nouvelle_evaluation``."""
    return {
        "subject": evaluation.subject,
        "name": evaluation.name,
        "acquisitions": [
            {"name": item.name, "level": item.level} for item in evaluation.acquisitions
        ],
        "date": evaluation.date.isoformat(),
        "evaluation_id": evaluation.id,
    }


def _message_events(thread: Discussion, before: int) -> list[DeltaEvent]:
    """One event per newly unread message in a thread.

    When the gateway expanded the thread there is real per-message context;
    when it did not -- because more threads went active than the expansion cap
    allows -- one event still fires with the thread's identity, because the
    automation's job is to say "a message arrived", and losing that entirely
    would be worse than losing the author's name.
    """
    delta = thread.unread - before
    if thread.messages:
        newest: Sequence[Any] = sorted(
            thread.messages, key=lambda message: message.created
        )[-delta:]
        return [
            DeltaEvent(
                "new_message",
                EVENT_MESSAGE_RECEIVED,
                {
                    "discussion": thread.subject,
                    "author": message.author,
                    "created": message.created.isoformat(),
                    "discussion_id": thread.id,
                    "unread": thread.unread,
                },
            )
            for message in newest
        ]

    return [
        DeltaEvent(
            "new_message",
            EVENT_MESSAGE_RECEIVED,
            {
                "discussion": thread.subject,
                "author": None,
                "created": None,
                "discussion_id": thread.id,
                "unread": thread.unread,
            },
        )
    ]
