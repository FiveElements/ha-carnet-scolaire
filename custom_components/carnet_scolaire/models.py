"""Frozen data transfer objects -- the only thing that crosses the gateway.

Nothing pronotepy produces is allowed past this boundary (SPECIFICATION.md
§3.1). Three known defects disappear by construction:

* an object read after its session closed raises ``Erreur.G = 22``;
* a lazy attribute (``Information.content``, ``Attachment.data``,
  ``Lesson.content``) calls the network at the moment it is *read*, so
  potentially from the event loop;
* an entity holding a client reference keeps the whole object graph alive,
  session included.

The concrete case worth naming: ``ClientInfo._cache()`` calls
``self._client.communication.post(...)`` directly, bypassing ``ClientBase.post``
and -- on a parent account -- the ``membre`` signature that says *which child*.
Reading ``ClientInfo.address``, ``.email``, ``.phone`` or ``.ine_number`` from
anywhere therefore fires an unbudgeted and possibly misattributed call. This
boundary is what prevents it, and that is the best justification for its
existence.

Every ``datetime`` here is timezone-aware. The conversion happens once, in the
gateway, with the establishment's timezone (§4.2).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from .const import AttachmentKind, GradeDocumentRole, GradeStatus, Tier

# ---------------------------------------------------------------------------
# Timetable
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Lesson:
    """One timetable slot.

    Two fields need justifying.

    ``num`` is the protocol's ``P`` field, and it carries the de-duplication
    required by §2.2.1: ``PageEmploiDuTemps`` returns several entries for the
    same slot -- the original and its replacements -- and upstream's docstring
    says *"for the same lesson time, the biggest num is the one shown on
    pronote"*. Specification v1 declared ``num`` without saying what it was
    for, which amounted to not using it.

    ``end_inferred`` exists because ``end`` is not always transmitted. When
    ``DateDuCoursFin`` is absent, pronotepy computes it through
    ``Util.place2time``, whose upstream comment reads *"might be wrong... works
    with demo"*. Everything depends on ``end`` -- wake-up, "in class", the
    calendar, end of day -- so the flag lets an entity stay silent rather than
    assert a doubtful time.
    """

    id: str
    subject: str | None
    subject_id: str | None
    teachers: tuple[str, ...]
    classrooms: tuple[str, ...]
    groups: tuple[str, ...]
    start: datetime
    end: datetime
    canceled: bool
    status: str | None
    detention: bool
    outing: bool
    exempted: bool
    test: bool
    memo: str | None
    background_color: str | None
    virtual_classrooms: tuple[str, ...]
    num: int
    place: int
    duration: int
    end_inferred: bool
    #: PRONOTE's ``N`` for this item, valid **only in the session that read
    #: it** -- ``id`` is the key this integration mints (see
    #: :mod:`.item_keys`). ``None`` for a connector whose identifiers are
    #: already stable.
    ref: str | None = None

    @property
    def slot_key(self) -> tuple[str, int]:
        """Identity of the *slot*: which day, and which position in it.

        Used to de-duplicate -- keep the entry that supersedes the others --
        and deliberately not ``id``, because a replacement gets a new ``N``
        without the superseded entry being removed (§2.2.1).

        ``subject_id`` used to be part of this key, and including it defeated
        the de-duplication it exists for. A *substitution* -- the case the rule
        is written for -- is two entries on one slot with different subjects and
        different ``num``: keyed with the subject they landed in separate
        buckets, both survived, and the day was counted twice, drawn twice in
        the calendar, and "next lesson" could latch onto the superseded one.
        A slot is a coordinate on the grid; what is taught in it is content.

        Not used as the delta key: see :attr:`Lesson.id` and ``delta.py``,
        where the memo is keyed by ``id`` so that a moved lesson reads as one
        move rather than as a cancellation plus an addition.
        """
        return (self.start.date().isoformat(), self.place)

    @property
    def change_signature(self) -> tuple[object, ...]:
        """The whitelisted fields a change is detected on.

        ``memo``, ``background_color`` and lesson content are excluded on
        purpose: a teacher fixing a typo must not wake the house up. That is
        what the single "delta on ``N`` only" rule was really trying to buy.

        Classrooms and teachers are compared as **sets**, not as sequences.
        Nothing in the protocol orders ``ListeProfesseurs`` or
        ``ListeSalles``, and PRONOTE does reorder them between responses; as
        tuples, a co-taught lesson emitted ``lesson_changed`` on an arbitrary
        subset of collections, every night, for the rest of the year. An
        automation that pushes a notification on that event is worse than no
        automation.
        """
        return (
            self.canceled,
            self.status,
            frozenset(self.classrooms),
            frozenset(self.teachers),
            self.start,
            self.end,
        )

    @property
    def classroom(self) -> str | None:
        """Joined classrooms, for display."""
        return ", ".join(self.classrooms) if self.classrooms else None


# ---------------------------------------------------------------------------
# Homework
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HomeworkAttachment:
    """One document attached to a homework entry.

    PRONOTE attaches two different things under one payload key, and the
    difference decides whether an address can leave this process at all.

    A **link** (``G = 0``) carries an ordinary address a teacher pasted in. It
    is stable, it authenticates nobody, and it is safe to publish.

    A **file** (``G = 1``) has no address that exists independently of the
    session. ``pronotepy.Attachment`` builds one as
    ``FichiersExternes/<hex>/<name>?Session=<h>``, where the hex segment is
    ``{"N": id, "Actif": true}`` encrypted with the session's own AES key and
    IV -- ``aes_iv_temp`` is `secrets.token_bytes(16)`, drawn per session, and
    the key comes out of the authentication handshake. So two links to the same
    document from two sessions share no byte of that segment. Publishing one
    would put a credential-bearing address into the snapshot, into every
    recorder row and into the diagnostics download -- the hazard §8.2 removed
    the iCal URL from the state machine for -- and it would be dead within the
    hour anyway, since a session with no successful call for that long is
    abandoned. Hence ``url`` is ``None`` for a file, and the only honest way to
    open one is a service that mints an address at the instant of the click --
    ``carnet_scolaire.get_attachment_url``, see :mod:`.attachment`.

    ``kind`` says which of these this is, and it is the field to read. The
    presence of ``url`` does **not** tell a file from a link: an unusable link
    has no ``url`` either, and for a while it was treated as a file because of
    exactly that reading.
    """

    name: str
    #: The address, when there is one that survives leaving this process.
    #: ``None`` for a file, and for a link whose address is not `http` or
    #: `https` -- upstream falls back to the *name* when the payload carries no
    #: address, so this field would otherwise publish a label as though it were
    #: a URL, and a scheme like `javascript:` would reach an `href`.
    url: str | None = None
    #: The key this integration mints for the document (see
    #: :mod:`.item_keys`), stable across sessions. It is deliberately absent
    #: from every published attribute: what a dashboard receives is a
    #: fingerprint of it and of its homework's key (see :mod:`.attachment`).
    id: str = ""
    #: PRONOTE's own identifier for the document, which is what a file is
    #: *fetched* by -- the encrypted path segment is built from it. Valid only
    #: in the session that read it, so a download re-reads the homework in its
    #: own session rather than trusting a snapshot's value.
    ref: str = ""
    #: Decided in `gateway._attachment`. Defaults to the answer that opens
    #: nothing, so a DTO built without deciding fails closed.
    kind: AttachmentKind = AttachmentKind.OPAQUE


@dataclass(frozen=True, slots=True)
class Homework:
    """One homework assignment."""

    id: str
    subject: str | None
    #: As PRONOTE sends it, which is HTML: teachers type into a rich-text
    #: field, so this carries `<div>`, `<br>` and character entities.
    description: str
    #: The same prose with the markup removed and the entities decoded. It
    #: exists because a consumer can do neither of the two things raw HTML
    #: allows: injecting it would make every teacher's text field an XSS
    #: vector into the dashboard, and printing it makes the reader see the
    #: tags. The conversion belongs to the only module that knows the field is
    #: HTML, rather than to each card inventing its own stripper.
    description_text: str
    due: date
    done: bool
    background_color: str | None
    #: The attached documents, each with its name and -- only for a link --
    #: its address. See :class:`HomeworkAttachment` for why a file never
    #: carries one.
    attachments: tuple[HomeworkAttachment, ...] = ()
    #: PRONOTE's ``N`` for this item, valid **only in the session that read
    #: it** -- ``id`` is the key this integration mints (see
    #: :mod:`.item_keys`). ``None`` for a connector whose identifiers are
    #: already stable.
    ref: str | None = None


# ---------------------------------------------------------------------------
# Marks
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GradeDocument:
    """The paper or the answers of one graded test, as a teacher joined them.

    PRONOTE describes each in its own element of the ``listeDevoirs`` entry --
    ``elmSujet`` and ``elmCorrige`` -- carrying the document's **own** ``N``,
    which is what the file is fetched by, together with a file type that says
    which of the two it is (`PronoteGateway.grade_document`). The grade's
    ``N`` is not the paper's: fetched by it, the paper answered 404 on a live
    instance while the answers -- whose ``N`` happened to coincide -- opened.
    """

    name: str
    role: GradeDocumentRole
    #: The document's ``N``, valid **only in the session that read it**, so
    #: the relay re-reads the grades in its own session before using it, as it
    #: does for homework. Empty when the entry carried only the file name; the
    #: grade's ``N`` is then the fallback. Never published.
    ref: str = ""


@dataclass(frozen=True, slots=True)
class Grade:
    """One grade.

    ``value`` and ``status`` are mutually exclusive (§4.3). That separation is
    what lets ``sensor.<student>_derniere_note`` hold a *numeric* state -- so a
    ``numeric_state`` trigger works -- and fall back to ``unknown`` with the
    reason in an attribute when the grade is a sentinel. A state that were
    sometimes ``14.5`` and sometimes ``Absent`` would be usable neither by a
    threshold nor by a graph.
    """

    id: str
    subject: str | None
    subject_id: str | None
    value: float | None
    status: GradeStatus | None
    out_of: float | None
    default_out_of: float | None
    date: date
    coefficient: float | None
    class_average: float | None
    max_value: float | None
    min_value: float | None
    comment: str | None
    is_bonus: bool
    is_optional: bool
    is_out_of_20: bool
    #: PRONOTE's ``N`` for this item, valid **only in the session that read
    #: it** -- ``id`` is the key this integration mints (see
    #: :mod:`.item_keys`). ``None`` for a connector whose identifiers are
    #: already stable.
    ref: str | None = None
    #: The test's paper and answers, when a teacher joined them -- at most one
    #: of each, in that order. Not part of the grade's key: answers posted a
    #: week after the mark are the same grade, not a new one to announce.
    documents: tuple[GradeDocument, ...] = ()
    #: The teacher's remark on *this student's* mark (``commentaireSurNote``),
    #: distinct from ``comment``, which titles the test for the whole class.
    #: Not part of the key either: it is written after the mark, like the value.
    remark: str | None = None
    #: Whether the test was given to a group rather than the whole class
    #: (``estEnGroupe``), which is when ``class_average`` is the group's.
    subject_in_groups: bool = False
    #: The subject's colour as the establishment set it (``service.couleur``),
    #: the same value the per-subject averages carry.
    background_color: str | None = None

    def __post_init__(self) -> None:
        """Enforce the exclusivity the docstring promises.

        A promise the type system cannot make and that no caller checked. The
        gateway is the only producer, so this can only fire on a gateway bug --
        which is precisely when it is worth failing loudly: a ``Grade`` holding
        both ``14.5`` and ``ABSENT`` would put one of the two into
        ``sensor.<eleve>_derniere_note`` depending on which branch of the value
        function ran first, and the entity's contract with every
        ``numeric_state`` trigger downstream rests on that never happening.
        """
        if self.value is not None and self.status is not None:
            raise ValueError(
                f"grade {self.id} carries both a value ({self.value}) and a "
                f"status ({self.status}); §4.3 makes them exclusive"
            )


@dataclass(frozen=True, slots=True)
class Average:
    """A per-subject average.

    ``pronotepy.Average`` has no ``id`` field, so the stability rule of §2.4
    forbids using a rank in the list: items are keyed by ``subject_id``.
    """

    subject: str | None
    subject_id: str | None
    student: float | None
    class_average: float | None
    min_average: float | None
    max_average: float | None
    out_of: float | None
    background_color: str | None


@dataclass(frozen=True, slots=True)
class ReportSubject:
    """One subject line of a report card."""

    id: str
    name: str
    color: str | None
    comments: tuple[str, ...]
    student_average: float | None
    class_average: float | None
    min_average: float | None
    max_average: float | None
    coefficient: float | None
    teachers: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Report:
    """A report card for one period."""

    subjects: tuple[ReportSubject, ...]
    comments: tuple[str, ...]


# ---------------------------------------------------------------------------
# Attendance
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Absence:
    """An absence.

    Note the field set: upstream carries ``hours`` (a string) and ``days`` (an
    int). There is **no** ``minutes`` -- that belongs to :class:`Delay`.
    Specification v1 listed ``minutes`` on both, which is why they now have
    separate event entities (annexe A §4).
    """

    id: str
    from_date: datetime
    to_date: datetime
    justified: bool
    hours: str | None
    days: int | None
    reasons: tuple[str, ...]
    #: PRONOTE's ``N`` for this item, valid **only in the session that read
    #: it** -- ``id`` is the key this integration mints (see
    #: :mod:`.item_keys`). ``None`` for a connector whose identifiers are
    #: already stable.
    ref: str | None = None


@dataclass(frozen=True, slots=True)
class Delay:
    """A late arrival."""

    id: str
    at: datetime
    minutes: int | None
    justified: bool
    justification: str | None
    reasons: tuple[str, ...]
    #: PRONOTE's ``N`` for this item, valid **only in the session that read
    #: it** -- ``id`` is the key this integration mints (see
    #: :mod:`.item_keys`). ``None`` for a connector whose identifiers are
    #: already stable.
    ref: str | None = None


@dataclass(frozen=True, slots=True)
class PunishmentSlot:
    """One scheduled detention slot."""

    start: datetime
    duration_minutes: int


@dataclass(frozen=True, slots=True)
class Punishment:
    """A punishment, with its scheduled slots."""

    id: str
    nature: str | None
    reasons: tuple[str, ...]
    giver: str | None
    given_at: datetime | None
    exclusion: bool
    during_lesson: bool
    homework: str | None
    schedule: tuple[PunishmentSlot, ...]
    #: PRONOTE's ``N`` for this item, valid **only in the session that read
    #: it** -- ``id`` is the key this integration mints (see
    #: :mod:`.item_keys`). ``None`` for a connector whose identifiers are
    #: already stable.
    ref: str | None = None


# ---------------------------------------------------------------------------
# Evaluations
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Acquisition:
    """One competency line inside an evaluation."""

    id: str
    name: str | None
    level: str | None
    abbreviation: str | None
    coefficient: float | None
    domain: str | None
    pillar: str | None


@dataclass(frozen=True, slots=True)
class Evaluation:
    """A competency-based evaluation."""

    id: str
    name: str | None
    subject: str | None
    subject_id: str | None
    teacher: str | None
    description: str | None
    date: date
    acquisitions: tuple[Acquisition, ...]
    #: PRONOTE's ``N`` for this item, valid **only in the session that read
    #: it** -- ``id`` is the key this integration mints (see
    #: :mod:`.item_keys`). ``None`` for a connector whose identifiers are
    #: already stable.
    ref: str | None = None


# ---------------------------------------------------------------------------
# News and messaging
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Information:
    """A news item or a survey."""

    id: str
    title: str | None
    author: str | None
    category: str | None
    read: bool
    survey: bool
    anonymous_response: bool
    created: datetime
    start_date: datetime | None
    end_date: datetime | None
    #: PRONOTE's ``N`` for this item, valid **only in the session that read
    #: it** -- ``id`` is the key this integration mints (see
    #: :mod:`.item_keys`). ``None`` for a connector whose identifiers are
    #: already stable.
    ref: str | None = None


@dataclass(frozen=True, slots=True)
class Message:
    """One message inside a discussion."""

    id: str
    author: str | None
    created: datetime
    content: str | None
    #: PRONOTE's ``N`` for this item, valid **only in the session that read
    #: it** -- ``id`` is the key this integration mints (see
    #: :mod:`.item_keys`). ``None`` for a connector whose identifiers are
    #: already stable.
    ref: str | None = None


@dataclass(frozen=True, slots=True)
class Discussion:
    """A discussion thread.

    ``unread`` is an **integer** (``nbNonLus``), not a flag. The count sensor
    therefore sums it across discussions; counting threads that have at least
    one unread message would give a different number and the sensor would
    contradict its own ``items`` attribute (annexe A §1).

    ``messages`` is empty unless this thread was *newly active*, and that is a
    budget decision the specification did not anticipate.
    ``pronotepy.Discussion.messages`` is a property that posts ``ListeMessages``
    **every time it is read**, so populating it for every thread would cost one
    call per discussion -- with ten threads on an hourly tier that is ~176
    requests a day, roughly doubling the whole budget for data nobody asked
    for. The gateway therefore fetches messages only for threads whose
    ``unread`` count went up since the previous snapshot, which is what
    ``event.<student>_nouveau_message`` actually needs and normally costs zero
    or one extra call per cycle.
    """

    id: str
    subject: str | None
    creator: str | None
    unread: int
    closed: bool
    labels: tuple[str, ...]
    messages: tuple[Message, ...] = ()
    #: PRONOTE's ``N`` for this item, valid **only in the session that read
    #: it** -- ``id`` is the key this integration mints (see
    #: :mod:`.item_keys`). ``None`` for a connector whose identifiers are
    #: already stable.
    ref: str | None = None


# ---------------------------------------------------------------------------
# Canteen
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Menu:
    """One meal on one day."""

    id: str
    day: date
    name: str | None
    is_lunch: bool
    is_dinner: bool
    first_meal: tuple[str, ...]
    main_meal: tuple[str, ...]
    side_meal: tuple[str, ...]
    other_meal: tuple[str, ...]
    cheese: tuple[str, ...]
    dessert: tuple[str, ...]

    @property
    def dish_count(self) -> int:
        """Total number of dishes, which is the sensor's state."""
        return sum(
            len(part)
            for part in (
                self.first_meal,
                self.main_meal,
                self.side_meal,
                self.other_meal,
                self.cheese,
                self.dessert,
            )
        )


# ---------------------------------------------------------------------------
# Static and session facts
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TeachingStaffMember:
    """A teacher or another member of staff.

    No e-mail field: ``pronotepy.TeachingStaff`` carries ``name``, ``type`` and
    the subjects taught, and nothing else. Contact details live behind
    ``ClientInfo._cache()``, which is a separate, unbudgeted call and is
    therefore only reachable through the ``get_identity`` response service
    (§8.2).
    """

    name: str | None
    role: str | None
    subjects: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Period:
    """A school period.

    ``index`` is a 1-based position used to build stable entity identifiers:
    "Trimestre 1" may be relabelled between years, so entities carry ``p1``,
    ``p2``, ``p3`` and only the *displayed* name follows the establishment
    (§2.4).
    """

    id: str
    name: str
    start: datetime
    end: datetime
    index: int

    def is_closed(self, now: datetime) -> bool:
        """True once the period can no longer change."""
        return now >= self.end


@dataclass(frozen=True, slots=True)
class Guardian:
    """A legal guardian. Never enters a state (§8.1)."""

    name: str | None
    relation: str | None
    email: str | None
    phone: str | None
    address: tuple[str, ...]
    is_legal: bool


@dataclass(frozen=True, slots=True)
class Identity:
    """Full identity. Returned only by a response service, never a state."""

    name: str | None
    birth_date: date | None
    birth_place: str | None
    email: str | None
    phone: str | None
    address: tuple[str, ...]
    ine_number: str | None
    guardians: tuple[Guardian, ...]


@dataclass(frozen=True, slots=True)
class Student:
    """A child on the account, and the facts that come free with the login."""

    id: str
    name: str
    class_name: str | None
    establishment: str | None
    has_photo: bool


# ---------------------------------------------------------------------------
# Per-tier snapshots
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SessionFacts:
    """What the login itself hands us -- zero extra calls.

    ``periods``, ``class_name`` and ``establishment`` are read out of
    ``func_options`` and ``parametres_utilisateur``, already in hand once
    authenticated. Specification v1 filed them under the ``static`` tier and
    budgeted calls for them; ``static`` is in fact a single call, the teaching
    staff (annexe A §2).

    ``current_period`` is ``None`` when it cannot be established.
    ``Client.current_period`` falls back to ``onglets[0]`` when tab 198 is
    absent, which silently names the wrong period in an establishment that does
    not publish grades -- so the gateway prefers ``None`` and the entity goes
    unavailable rather than wrong (§3.3.3).
    """

    student: Student
    periods: tuple[Period, ...]
    current_period: Period | None


@dataclass(frozen=True, slots=True)
class TimetableFacts:
    """Lessons for the current week, plus next week's at a boundary crossing.

    ``Client.lessons()`` loops ``for week in range(first_week, last_week + 1)``
    and posts once *per week*, then filters client-side. Asking for "today and
    tomorrow" therefore costs exactly the same call as asking for the whole
    week, which is why ``timetable_week`` was folded in here (§5.2).

    ``lessons`` is already de-duplicated by slot: without that,
    ``sensor.cours_du_jour`` overcounts every day a lesson was changed and the
    calendar draws overlapping events (§2.2.1).

    ``all_lessons`` is the same week **before** de-duplication, and the change
    detector is the only consumer. The two lists answer different questions,
    which is why both are here. De-duplication asks *what does PRONOTE
    display*, and a display must show one lesson per slot. Change detection
    asks *what moved since last time*, and the entry de-duplication throws away
    is often the answer: on a substitution PRONOTE serves the original with
    ``estAnnule`` set **plus** a replacement with a higher ``num``, so the
    winner is the replacement -- and running the delta on winners alone made
    the cancellation of the original literally unobservable, because the entry
    carrying it had been dropped one layer down. The detector now also reads
    it to tell such a *covered* slot, where the child still has a lesson, from
    an emptied one -- only the second is announced as cancelled.

    It costs a few extra objects per week and no extra request.
    """

    lessons: tuple[Lesson, ...]
    all_lessons: tuple[Lesson, ...]
    weeks_fetched: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class HomeworkFacts:
    """Homework for the whole school year, in one call.

    ``Client.homework()`` builds its domain as a *week range* and defaults
    ``date_to`` to the end of the year, so the span requested does not change
    the cost. ``homework_horizon`` is therefore a presentation filter, not a
    budget lever (§5.2).
    """

    homework: tuple[Homework, ...]


@dataclass(frozen=True, slots=True)
class MarksFacts:
    """One ``DernieresNotes`` response, four datasets, plus the current report.

    Upstream would spend four identical posts on this: ``Period.grades``,
    ``.averages``, ``.overall_average`` and ``.class_overall_average`` each
    re-post ``DernieresNotes`` with the same body
    (dataClasses.py:525/534/548/578).
    """

    period_id: str
    period_index: int
    grades: tuple[Grade, ...]
    averages: tuple[Average, ...]
    overall_average: float | None
    class_overall_average: float | None
    report: Report | None


@dataclass(frozen=True, slots=True)
class AttendanceFacts:
    """One ``PagePresence`` response, three datasets.

    ``Period.absences``, ``.delays`` and ``.punishments`` each re-post
    ``PagePresence`` and then read *the same* ``listeAbsences`` list, filtering
    on ``G`` being 13, 14 or 41 (dataClasses.py:606/621/636). One call, one
    filter, three tuples.

    ``period_index`` is what identifies the period *across* sessions:
    ``period_id`` is the session's ``N``, re-encrypted at every login, and
    anything keyed on it -- the change detector, a closed-period sensor --
    loses track of the period at the first reconnection.
    """

    period_id: str
    period_index: int
    absences: tuple[Absence, ...]
    delays: tuple[Delay, ...]
    punishments: tuple[Punishment, ...]


@dataclass(frozen=True, slots=True)
class EvaluationsFacts:
    """Competency evaluations for a period.

    ``period_index`` identifies the period across sessions, as on
    :class:`AttendanceFacts`; ``period_id`` does not outlive the login.
    """

    period_id: str
    period_index: int
    evaluations: tuple[Evaluation, ...]


@dataclass(frozen=True, slots=True)
class NewsFacts:
    """News items and surveys."""

    information: tuple[Information, ...]


@dataclass(frozen=True, slots=True)
class DiscussionsFacts:
    """Discussion threads, and which of them were expanded.

    ``expanded`` holds the identifiers of the threads whose message bodies were
    actually fetched this cycle. It exists because expansion is *rationed*:
    reading ``pronotepy.Discussion.messages`` posts ``ListeMessages`` every
    time, so the gateway expands at most
    ``gateway.MAX_DISCUSSION_EXPANSIONS`` newly active threads per cycle.

    Without this field the caller could not tell "this thread has no messages"
    from "this thread was not read this time", and so recorded the new unread
    count for a thread it had never looked at -- permanently disqualifying it
    from ever being expanded again (§2.2.1).
    """

    discussions: tuple[Discussion, ...]
    expanded: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class MenusFacts:
    """Canteen menus for today and tomorrow."""

    menus: tuple[Menu, ...]


@dataclass(frozen=True, slots=True)
class StaticFacts:
    """The teaching staff, and nothing else.

    The iCal URL used to live here. It does not any more: keeping it would drop
    an autonomous authentication bearer into the snapshot store -- a long-lived
    in-memory structure whose *purpose* is to be dumped into a diagnostic
    report. That is the very hazard §8.2 exists to prevent, moved from the
    state machine to the store. ``get_ical_url`` fetches on demand instead.
    """

    teaching_staff: tuple[TeachingStaffMember, ...]


@dataclass(frozen=True, slots=True)
class HistoryFacts:
    """Closed periods: marks, attendance and report, read once a day.

    A closed period cannot change. Re-reading it every three hours, as the
    current integration does, spends calls on a constant result (§5.2).
    """

    marks: tuple[MarksFacts, ...]
    attendance: tuple[AttendanceFacts, ...]
    evaluations: tuple[EvaluationsFacts, ...]


class GatewayResult[T]:
    """A tier's facts plus what they actually cost on the wire.

    Returning the cost rather than inferring it is what makes the per-tier
    contract test of §11.1 possible: an accidental property access doubles the
    cost without anything breaking, and that is the regression this whole
    project exists to prevent.
    """

    __slots__ = ("calls", "facts")

    def __init__(self, facts: T, calls: int) -> None:
        self.facts = facts
        self.calls = calls


@dataclass(frozen=True, slots=True)
class Snapshot[T]:
    """A tier's payload, with the instant it was obtained.

    A failed collection does **not** replace the previous snapshot: the entity
    goes on showing the last known value and reports its age (§5.4). That is
    the difference between "I no longer know" and "I know, but it is old" -- on
    school data the second answer is almost always the right one.
    """

    data: T
    fetched_at: datetime
    tier: Tier
    calls: int = 0
    student_id: str | None = None


@dataclass(frozen=True, slots=True)
class SessionLifetime:
    """What the session manager measured, rather than assumed (§6.5).

    This is what makes the lazy strategy defensible instead of a bet: if the
    measured inactivity timeout turns out to be shorter than the fastest tier's
    interval, the lazy strategy degenerates into exactly one login per batch --
    specification v1's design. It is therefore never worse, for any value of
    the unknown parameter.

    Before the first measurement the behaviour is the pessimistic one. The
    measurement only ever *relaxes* the constraint; it never imposes it. Without
    that, the dominance property above stops holding at startup -- which is the
    moment the user judges the integration.
    """

    samples: tuple[float, ...] = ()
    last_expiry: datetime | None = None

    @property
    def observed_minutes(self) -> float | None:
        """The shortest lifetime ever observed, in minutes.

        The minimum, not the mean: one short expiry is proof the timeout can be
        that short, whereas a long one only proves it was not exercised.
        """
        if not self.samples:
            return None
        return min(self.samples) / 60.0

    def with_sample(self, seconds: float, at: datetime) -> SessionLifetime:
        """Return a new record including one more observation."""
        return SessionLifetime(
            samples=(*self.samples, seconds)[-20:],
            last_expiry=at,
        )
