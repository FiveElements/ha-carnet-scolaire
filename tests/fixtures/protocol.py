"""Builders for raw PRONOTE-shaped responses.

Every function returns the plain nested ``dict`` the protocol sends, so the
gateway's decoders are exercised on the real shape rather than on mocks of
themselves. Each builder takes keyword overrides, and the *interesting* tests
are the ones that remove a field: ``average(class_average=None)`` is the
establishment that publishes no class statistics, and it is the case that used
to empty the whole marks tier.

The date format matters and is easy to get wrong. ``Util.datetime_parse``
accepts exactly three forms, of which this module uses ``%d/%m/%Y %H:%M:%S``
for instants and ``%d/%m/%Y`` for plain dates; anything else raises
``DateParsingError``, which would make a fixture bug look like a decoder bug.
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Sequence

#: The identifiers used throughout. Deliberately not numeric strings: a real
#: PRONOTE ``N`` is a number, so a test failure mentioning ``LESSON-1`` can
#: never be mistaken for something captured from a server.
STUDENT_ID = "STUDENT-1"
PERIOD_ID = "PERIOD-1"
CLOSED_PERIOD_ID = "PERIOD-0"


def _stamp(moment: dt.datetime) -> dict[str, Any]:
    """A ``{"_T": 7, "V": "dd/mm/yyyy HH:MM:SS"}`` instant."""
    return {"_T": 7, "V": moment.strftime("%d/%m/%Y %H:%M:%S")}


def _day(value: dt.date) -> dict[str, Any]:
    """A ``{"_T": 7, "V": "dd/mm/yyyy"}`` plain date."""
    return {"_T": 7, "V": value.strftime("%d/%m/%Y")}


def _label(name: str, identifier: str | None = None) -> dict[str, Any]:
    """A ``{"L": name, "N": id}`` label record."""
    record: dict[str, Any] = {"L": name}
    if identifier is not None:
        record["N"] = identifier
    return record


# ---------------------------------------------------------------------------
# Session material -- what the login already handed us
# ---------------------------------------------------------------------------

#: ``ListeHeuresFin``, which is what upstream reads when ``DateDuCoursFin`` is
#: absent. Nine slots, half-hourly places, as a real grid is shaped.
END_TIMES = [
    {"G": place, "L": f"{8 + place // 2}h{'30' if place % 2 else '00'}"}
    for place in range(9)
]

START_TIMES = [
    {"G": place, "L": f"{8 + place // 2}h{'30' if place % 2 else '00'}"}
    for place in range(9)
]


def func_options(
    *,
    first_monday: dt.date = dt.date(2025, 9, 1),
    last_date: dt.date | None = dt.date(2026, 7, 4),
    periods: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """``FonctionParametres``, as far as this integration reads it."""
    general: dict[str, Any] = {
        "PremierLundi": _day(first_monday),
        "ListeHeuresFin": {"V": END_TIMES},
        "ListeHeures": {"V": START_TIMES},
        "ListePeriodes": periods if periods is not None else default_periods(),
    }
    if last_date is not None:
        general["DerniereDate"] = _day(last_date)
    return {"dataSec": {"data": {"General": general}}}


def default_periods() -> list[dict[str, Any]]:
    """Two periods: one closed, one current."""
    return [
        {
            "N": CLOSED_PERIOD_ID,
            "L": "Trimestre 1",
            "dateDebut": _stamp(dt.datetime(2025, 9, 1, 0, 0)),
            "dateFin": _stamp(dt.datetime(2025, 12, 5, 23, 59, 59)),
        },
        {
            "N": PERIOD_ID,
            "L": "Trimestre 2",
            "dateDebut": _stamp(dt.datetime(2025, 12, 6, 0, 0)),
            "dateFin": _stamp(dt.datetime(2026, 3, 20, 23, 59, 59)),
        },
    ]


def parametres_utilisateur(
    *,
    student_id: str = STUDENT_ID,
    name: str = "Enfant Un",
    class_name: str = "4e A",
    establishment: str = "Collège d'Essai",
    has_photo: bool = True,
    marks_tab: bool = True,
) -> dict[str, Any]:
    """``ParametresUtilisateur``, as far as this integration reads it.

    ``marks_tab=False`` is the establishment that publishes no grades, where
    ``Client.current_period`` silently falls back to ``onglets[0]`` and names
    the wrong period. The gateway prefers ``None``; this switch is how that is
    proved.
    """
    tabs: list[dict[str, Any]] = []
    if marks_tab:
        tabs.append({"G": 198, "periodeParDefaut": {"V": {"N": PERIOD_ID}}})

    resource = {
        "N": student_id,
        "L": name,
        "G": 4,
        "avecPhoto": has_photo,
        "classeDEleve": {"L": class_name},
        "Etablissement": {"V": {"L": establishment}},
        "listeOngletsPourPeriodes": {"V": tabs},
    }
    return {"dataSec": {"data": {"ressource": resource}}}


# ---------------------------------------------------------------------------
# Timetable
# ---------------------------------------------------------------------------


def lesson(
    *,
    identifier: str = "LESSON-1",
    start: dt.datetime | None = None,
    end: dt.datetime | None = None,
    place: int = 0,
    duration: int = 2,
    num: int = 0,
    subject: str | None = "Mathématiques",
    subject_id: str = "SUBJECT-MATHS",
    teachers: tuple[str, ...] = ("Prof. Un",),
    classrooms: tuple[str, ...] = ("Salle 101",),
    groups: tuple[str, ...] = (),
    canceled: bool = False,
    status: str | None = None,
    detention: bool = False,
    outing: bool = False,
    exempted: bool = False,
    test: bool = False,
    memo: str | None = None,
    background_color: str | None = None,
    with_contents: bool = True,
) -> dict[str, Any]:
    """One ``ListeCours`` entry.

    ``background_color`` defaults to *absent*, which is what makes this fixture
    honest: upstream resolves ``CouleurFond`` on a lesson with
    ``strict=False``, and on the establishment this was measured against the
    key never arrives. A fixture that always supplied it would prove the
    publishing path and hide the only question that matters.

    ``with_contents=False`` removes ``ListeContenus`` entirely, which is how
    PRONOTE serves a slot with no published content -- and how a cancellation
    often arrives. Upstream raises ``ParsingError`` on it, so this switch is
    what exercises the raw fallback decoder.
    """
    begins = start if start is not None else dt.datetime(2026, 3, 12, 8, 0)
    entry: dict[str, Any] = {
        "N": identifier,
        "DateDuCours": _stamp(begins),
        "place": place,
        "duree": duration,
        "P": num,
        "estAnnule": canceled,
        "estRetenue": detention,
        "estSortiePedagogique": outing,
        "dispenseEleve": exempted,
        "cahierDeTextes": {"V": {"estDevoir": test}},
    }
    if end is not None:
        entry["DateDuCoursFin"] = _stamp(end)
    if status is not None:
        entry["Statut"] = status
    if memo is not None:
        entry["memo"] = memo
    if background_color is not None:
        entry["CouleurFond"] = background_color

    if with_contents:
        contents: list[dict[str, Any]] = []
        if subject is not None:
            contents.append({"G": 16, "L": subject, "N": subject_id})
        contents.extend({"G": 3, "L": teacher} for teacher in teachers)
        contents.extend({"G": 17, "L": room} for room in classrooms)
        contents.extend({"G": 2, "L": group} for group in groups)
        entry["ListeContenus"] = {"V": contents}
    return entry


def timetable_response(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """A ``PageEmploiDuTemps`` response."""
    return {"dataSec": {"data": {"ListeCours": entries}}}


# ---------------------------------------------------------------------------
# Homework
# ---------------------------------------------------------------------------


def homework(
    *,
    identifier: str = "HOMEWORK-1",
    due: dt.date | None = None,
    subject: str | None = "Histoire",
    description: str | None = "Lire le chapitre 4",
    done: bool | None = False,
    attachments: tuple[str | tuple[str, str | None], ...] = (),
) -> dict[str, Any]:
    """One ``ListeTravauxAFaire`` entry.

    ``done=None`` removes ``TAFFait`` and ``subject=None`` removes ``Matiere``:
    both are resolved strictly upstream, so either one used to fail the whole
    tier rather than one item.

    An attachment given as a bare string is a **file** (``G = 1``), which is
    what this establishment sends. Given as a pair it is a **link**
    (``G = 0``) carrying that address, and ``(name, None)`` is the link whose
    address is missing from the payload -- the case upstream answers by
    falling back to the name, which is exactly what must not be published.
    """
    entry: dict[str, Any] = {
        "N": identifier,
        "PourLe": _day(due if due is not None else dt.date(2026, 3, 16)),
        "CouleurFond": "#336699",
    }
    if subject is not None:
        entry["Matiere"] = {"V": _label(subject, "SUBJECT-HISTOIRE")}
    if description is not None:
        entry["descriptif"] = {"V": description}
    if done is not None:
        entry["TAFFait"] = done
    if attachments:
        pieces: list[dict[str, Any]] = []
        for index, attachment in enumerate(attachments, start=1):
            piece: dict[str, Any] = {"N": f"ATTACHMENT-{index}"}
            if isinstance(attachment, str):
                piece |= {"L": attachment, "G": 1}
            else:
                name, address = attachment
                piece |= {"L": name, "G": 0}
                if address is not None:
                    piece["url"] = address
            pieces.append(piece)
        entry["ListePieceJointe"] = {"V": pieces}
    return entry


def homework_response(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """A ``PageCahierDeTexte`` response."""
    return {"dataSec": {"data": {"ListeTravauxAFaire": {"V": entries}}}}


# ---------------------------------------------------------------------------
# Marks
# ---------------------------------------------------------------------------


def grade(
    *,
    identifier: str = "GRADE-1",
    value: str = "14,5",
    out_of: str = "20",
    date: dt.date | None = None,
    subject: str = "Mathématiques",
    subject_id: str = "SUBJECT-MATHS",
    coefficient: str = "1",
    class_average: str | None = "11,2",
    comment: str | None = "Contrôle sur les fractions",
    is_bonus: bool = False,
    is_optional: bool = False,
    out_of_20: bool = False,
    subject_file: str | None = None,
    correction_file: str | None = None,
    remark: str | None = None,
    in_groups: bool | None = None,
) -> dict[str, Any]:
    """One ``listeDevoirs`` entry.

    ``subject_file`` and ``correction_file`` are the file names PRONOTE puts in
    ``libelleSujet`` and ``libelleCorrige`` when a teacher joins the test's
    paper or answers; absent otherwise, as on most grades.

    ``value`` may be a sentinel: ``"|1"`` through ``"|8"`` are the documented
    ones, and ``"|9"`` is the future ninth that makes upstream's table raise
    ``IndexError`` and fail every grade in the batch.
    """
    entry: dict[str, Any] = {
        "N": identifier,
        "note": {"V": value},
        "bareme": {"V": out_of},
        "baremeParDefaut": {"V": "20"},
        "date": _day(date if date is not None else dt.date(2026, 3, 10)),
        "service": {"V": _label(subject, subject_id)},
        "coefficient": coefficient,
        "estBonus": is_bonus,
        "estFacultatif": is_optional,
        "estRamenerSur20": out_of_20,
    }
    if class_average is not None:
        entry["moyenne"] = {"V": class_average}
        entry["noteMax"] = {"V": "18"}
        entry["noteMin"] = {"V": "4"}
    if comment is not None:
        entry["commentaire"] = comment
    if subject_file is not None:
        entry["libelleSujet"] = subject_file
    if correction_file is not None:
        entry["libelleCorrige"] = correction_file
    if remark is not None:
        entry["commentaireSurNote"] = remark
    if in_groups is not None:
        entry["service"]["V"]["estServiceGroupe"] = in_groups
    return entry


def average(
    *,
    subject: str = "Mathématiques",
    subject_id: str = "SUBJECT-MATHS",
    student: str | None = "13,4",
    class_average: str | None = "11,1",
    minimum: str | None = "5,0",
    maximum: str | None = "18,2",
    out_of: str | None = "20",
) -> dict[str, Any]:
    """One ``listeServices`` entry.

    Passing ``class_average=None, minimum=None, maximum=None`` is the
    establishment that publishes no class statistics. Upstream resolves all
    three strictly, so that combination used to make ``MarksFacts.averages``
    come back **empty** -- every per-subject average entity gone, one DEBUG
    line per subject as the only trace.
    """
    entry: dict[str, Any] = {"N": subject_id, "L": subject, "couleur": "#AA3366"}
    if student is not None:
        entry["moyEleve"] = {"V": student}
    if class_average is not None:
        entry["moyClasse"] = {"V": class_average}
    if minimum is not None:
        entry["moyMin"] = {"V": minimum}
    if maximum is not None:
        entry["moyMax"] = {"V": maximum}
    if out_of is not None:
        entry["baremeMoyEleve"] = {"V": out_of}
    return entry


def marks_response(
    *,
    grades: list[dict[str, Any]] | None = None,
    averages: list[dict[str, Any]] | None = None,
    overall: str | None = "13,0",
    class_overall: str | None = "11,5",
) -> dict[str, Any]:
    """A ``DernieresNotes`` response."""
    data: dict[str, Any] = {
        "listeDevoirs": {"V": grades if grades is not None else [grade()]},
        "listeServices": {"V": averages if averages is not None else [average()]},
    }
    if overall is not None:
        data["moyGenerale"] = {"V": overall}
    if class_overall is not None:
        data["moyGeneraleClasse"] = {"V": class_overall}
    return {"dataSec": {"data": data}}


def report_subject(
    *,
    identifier: str = "SUBJECT-MATHS",
    name: str = "Mathématiques",
    student_average: str | None = "13,4",
    class_average: str | None = "11,1",
    minimum: str | None = "5,0",
    maximum: str | None = "18,2",
    coefficient: str | None = "1",
    teachers: tuple[str, ...] = ("Prof. Un",),
    comments: tuple[str, ...] = ("Bon trimestre.",),
) -> dict[str, Any]:
    """One ``ListeServices`` entry of a report card.

    Note the capitalisation. ``Report`` reads ``ListeServices``,
    ``MoyenneEleve`` and ``ListeAppreciations``; ``DernieresNotes`` reads
    ``listeServices`` and ``moyEleve`` for what is very nearly the same data.
    Getting that wrong is invisible until a report card is actually asked for,
    which is why this builder mirrors upstream field for field.
    """
    entry: dict[str, Any] = {
        "N": identifier,
        "L": name,
        "couleur": "#AA3366",
        "ListeAppreciations": {"V": [_label(comment) for comment in comments]},
        "ListeProfesseurs": {"V": [_label(teacher) for teacher in teachers]},
    }
    if student_average is not None:
        entry["MoyenneEleve"] = {"V": student_average}
    if class_average is not None:
        entry["MoyenneClasse"] = {"V": class_average}
    if minimum is not None:
        entry["MoyenneInf"] = {"V": minimum}
    if maximum is not None:
        entry["MoyenneSup"] = {"V": maximum}
    if coefficient is not None:
        entry["Coefficient"] = {"V": coefficient}
    return entry


def report_response(
    *,
    published: bool = True,
    with_services_key: bool = True,
    subjects: list[dict[str, Any]] | None = None,
    services: Any = None,
    comments: tuple[str, ...] = ("Ensemble correct.",),
) -> dict[str, Any]:
    """A ``PageBulletins`` response.

    Three distinguishable answers, and the integration used to collapse two of
    them: ``published=False`` is PRONOTE's own "not yet" ``Message``;
    ``with_services_key=False`` is a response whose shape moved, which decoded
    happily into an *empty* report; and the default is a real report.

    ``services`` replaces the whole ``ListeServices`` value, for the case where
    the key survived but its contents did not.
    """
    if not published:
        return {"dataSec": {"data": {"Message": "Bulletin non publié"}}}

    data: dict[str, Any] = {
        "ObjetListeAppreciations": {
            "V": {"ListeAppreciations": {"V": [_label(text) for text in comments]}}
        }
    }
    if with_services_key:
        data["ListeServices"] = (
            {"V": services}
            if services is not None
            else {"V": subjects if subjects is not None else [report_subject()]}
        )
    return {"dataSec": {"data": data}}


# ---------------------------------------------------------------------------
# Attendance
# ---------------------------------------------------------------------------


def absence(
    *,
    identifier: str = "ABSENCE-1",
    start: dt.datetime | None = None,
    end: dt.datetime | None = None,
    justified: bool = False,
    hours: str = "2h00",
    days: int = 0,
    reasons: tuple[str, ...] = ("Maladie",),
) -> dict[str, Any]:
    """One ``listeAbsences`` entry with ``G = 13``."""
    begins = start if start is not None else dt.datetime(2026, 3, 9, 8, 0)
    finishes = end if end is not None else dt.datetime(2026, 3, 9, 10, 0)
    return {
        "G": 13,
        "N": identifier,
        "dateDebut": _stamp(begins),
        "dateFin": _stamp(finishes),
        "justifie": justified,
        "NbrHeures": hours,
        "NbrJours": days,
        "listeMotifs": {"V": [_label(reason) for reason in reasons]},
    }


def delay(
    *,
    identifier: str = "DELAY-1",
    at: dt.datetime | None = None,
    minutes: int = 12,
    justified: bool = False,
    justification: str = "Transports",
    reasons: tuple[str, ...] = ("Retard bus",),
) -> dict[str, Any]:
    """One ``listeAbsences`` entry with ``G = 14``."""
    return {
        "G": 14,
        "N": identifier,
        "date": _stamp(at if at is not None else dt.datetime(2026, 3, 10, 8, 5)),
        "duree": minutes,
        "justifie": justified,
        "justification": justification,
        "listeMotifs": {"V": [_label(reason) for reason in reasons]},
    }


def punishment(
    *,
    identifier: str = "PUNISHMENT-1",
    nature: str = "Retenue",
    given_on: dt.date | None = None,
    exclusion: bool = False,
    during_lesson: bool = False,
    homework: str | None = "Copier le règlement",
    reasons: tuple[str, ...] = ("Bavardage",),
    schedulable: bool = True,
    schedule_place: int | None = 4,
    duration_minutes: int | None = 60,
) -> dict[str, Any]:
    """One ``listeAbsences`` entry with ``G = 41``.

    Two switches matter more than they look.

    ``schedule_place=None`` removes ``placeExecution``, which is what makes
    ``ScheduledPunishment.start`` a ``date`` rather than a ``datetime``. That
    asymmetry is real, it comes straight from upstream, and the gateway has to
    absorb it so the calendar can compare every event against every other one.

    ``during_lesson=True`` switches ``given`` from a ``date`` to a
    ``datetime``, through ``placeDemande`` and ``place2time`` -- and
    ``place2time`` raises ``ValueError`` on a place the grid does not contain,
    which upstream converts to ``DataError``. That is the second shape the
    decoder must tolerate rather than let fail the tier.

    Note ``horsCours`` is *inverted* upstream: ``during_lesson = not
    bool(horsCours)``.
    """
    day = given_on if given_on is not None else dt.date(2026, 3, 11)
    entry: dict[str, Any] = {
        "G": 41,
        "N": identifier,
        "dateDemande": _day(day),
        "horsCours": not during_lesson,
        "estUneExclusion": exclusion,
        "circonstances": "En cours de mathématiques" if during_lesson else "",
        "documentsCirconstances": {"V": []},
        "nature": {"V": _label(nature)},
        "listeMotifs": {"V": [_label(reason) for reason in reasons]},
        "demandeur": {"V": _label("CPE")},
        "estProgrammable": schedulable,
    }
    if during_lesson:
        entry["placeDemande"] = 2
    if homework is not None:
        entry["travailAFaire"] = homework
    if duration_minutes is not None:
        entry["duree"] = duration_minutes

    if schedulable:
        slot: dict[str, Any] = {
            "N": f"{identifier}-SLOT-1",
            "date": _day(dt.date(2026, 3, 13)),
            "duree": 1,
        }
        if schedule_place is not None:
            slot["placeExecution"] = schedule_place
        entry["programmation"] = {"V": [slot]}
    return entry


def attendance_response(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """A ``PagePresence`` response -- one list, three kinds, filtered on ``G``."""
    return {"dataSec": {"data": {"listeAbsences": {"V": entries}}}}


# ---------------------------------------------------------------------------
# Evaluations
# ---------------------------------------------------------------------------


def evaluation(
    *,
    identifier: str = "EVALUATION-1",
    name: str = "Résoudre un problème",
    subject: str = "Mathématiques",
    subject_id: str = "SUBJECT-MATHS",
    teacher: str = "Prof. Un",
    date: dt.date | None = None,
    coefficient: int | None = 1,
    levels: tuple[str, ...] = ("Très bonne maîtrise",),
) -> dict[str, Any]:
    """One ``listeEvaluations`` entry.

    ``coefficient=None`` removes a field upstream resolves strictly, which is
    the per-item tolerance case: one evaluation without a coefficient must cost
    that evaluation, never the whole tier.
    """
    entry: dict[str, Any] = {
        "N": identifier,
        "L": name,
        "matiere": {"V": _label(subject, subject_id)},
        "individu": {"V": _label(teacher)},
        "descriptif": "Évaluation de compétence",
        "date": _day(date if date is not None else dt.date(2026, 3, 6)),
        "domaine": {"V": _label("Nombres et calculs", "DOMAIN-1")},
        "listePaliers": {"V": [_label("Cycle 4")]},
        "listeNiveauxDAcquisitions": {
            "V": [
                acquisition(level, order=index)
                for index, level in enumerate(levels, start=1)
            ]
        },
    }
    if coefficient is not None:
        entry["coefficient"] = coefficient
    return entry


def acquisition(level: str, *, order: int = 1) -> dict[str, Any]:
    """One ``listeNiveauxDAcquisitions`` entry."""
    return {
        "N": f"ACQUISITION-{order}",
        "L": level,
        "abbreviation": level[:3],
        "coefficient": 1,
        "ordre": order,
        "domaine": {"V": _label("Nombres et calculs", "DOMAIN-1")},
        "item": {"V": _label("Calcul littéral", "ITEM-1")},
        "pilier": {
            "V": {
                "L": "Les langages pour penser et communiquer",
                "N": "PILLAR-1",
                "strPrefixes": "Domaine 1",
            }
        },
    }


def evaluations_response(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """A ``DernieresEvaluations`` response."""
    return {"dataSec": {"data": {"listeEvaluations": {"V": entries}}}}


# ---------------------------------------------------------------------------
# News
# ---------------------------------------------------------------------------


def information(
    *,
    identifier: str = "INFORMATION-1",
    title: str | None = "Sortie scolaire",
    author: str | None = "Direction",
    category: str | None = "Vie scolaire",
    read: bool = False,
    survey: bool = False,
    anonymous: bool = False,
    created: dt.datetime | None = None,
    start: dt.datetime | None = None,
    end: dt.datetime | None = None,
) -> dict[str, Any]:
    """One ``listeActualites`` entry.

    ``author=None`` and ``category=None`` remove fields upstream resolves
    strictly, which is what used to empty the entire news tier over one item.
    """
    entry: dict[str, Any] = {
        "N": identifier,
        "dateCreation": _stamp(
            created if created is not None else dt.datetime(2026, 3, 11, 9, 30)
        ),
        "lue": read,
        "estSondage": survey,
        "reponseAnonyme": anonymous,
    }
    if title is not None:
        entry["L"] = title
    if author is not None:
        entry["auteur"] = author
    if category is not None:
        entry["nature"] = {"V": _label(category)}
    if start is not None:
        entry["dateDebut"] = _stamp(start)
    if end is not None:
        entry["dateFin"] = _stamp(end)
    return entry


def news_response(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """A ``PageActualites`` response -- entries live under display modes."""
    return {
        "dataSec": {"data": {"listeModesAff": [{"listeActualites": {"V": entries}}]}}
    }


# ---------------------------------------------------------------------------
# Teaching staff -- the static tier
# ---------------------------------------------------------------------------


def teaching_staff(
    *,
    identifier: str = "STAFF-1",
    name: str = "Prof. Un",
    subjects: Sequence[tuple[str, str]] = (("SUBJECT-1", "Mathématiques"),),
    teacher: bool = True,
    order: int = 1,
) -> dict[str, Any]:
    """One ``PageEquipePedagogique`` entry, in the shape upstream decodes.

    Raw rather than an object, because ``dataClasses.TeachingStaff`` is what
    reads it: ``G`` is the discriminator it turns into ``"teacher"`` or
    ``"staff"`` (3 means teacher), and ``matieres.V`` carries the subjects with
    their weekly volume. A fake that returned finished objects would let this
    module's own decoding drift without a test noticing -- which is how a live
    server dropping the ``liste`` key reached a user as ``KeyError: 'liste'``.
    """
    return {
        "N": identifier,
        "L": name,
        "P": order,
        "G": 3 if teacher else 4,
        "matieres": {
            "V": [
                {"N": subject_id, "L": subject_name, "volumeHoraire": "4h30"}
                for subject_id, subject_name in subjects
            ]
        },
    }


def teaching_staff_response(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """A ``PageEquipePedagogique`` tab-37 response."""
    return {"dataSec": {"data": {"liste": {"V": entries}}}}


# ---------------------------------------------------------------------------
# Identity -- on demand, never stored
# ---------------------------------------------------------------------------


def personal_info_response(
    *,
    name: str = "Enfant Un",
    birth_date: dt.date | None = None,
    guardians: bool = True,
) -> dict[str, Any]:
    """A ``PageInfosPerso`` tab-49 response.

    Every value is invented, including the INE number, which is exactly the
    kind of field §8.2 keeps out of the state machine.
    """
    informations: dict[str, Any] = {
        "dateNaiss": {"V": (birth_date or dt.date(2011, 5, 4)).strftime("%d/%m/%Y")},
        "villeNaiss": "Villeneuve",
        "eMail": "enfant.un@example.invalid",
        "indicatifTel": "33",
        "telephonePortable": "600000001",
        "adresse1": "1 rue de l'Essai",
        "adresse2": "",
        "adresse3": "",
        "adresse4": "",
        "codePostal": "00000",
        "ville": "Villeneuve",
        "province": "",
        "pays": "France",
        "numeroINE": "0000000000A",
        "L": name,
    }
    data: dict[str, Any] = {"Informations": informations}
    if guardians:
        data["Responsables"] = {
            "V": [
                {
                    "L": "Parent Un",
                    "qualite": {"V": _label("Mère")},
                    "eMail": "parent.un@example.invalid",
                    "telephonePortable": "600000002",
                    "adresse1": "1 rue de l'Essai",
                    "estResponsableLegal": True,
                }
            ]
        }
    return {"dataSec": {"data": data}}
