"""Map EcoleDirecte JSON payloads to the integration's frozen DTOs."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date, datetime, time
from typing import TYPE_CHECKING, Any

from ...const import GradeStatus  # noqa: TID252
from ...models import (  # noqa: TID252
    Absence,
    AttendanceFacts,
    Average,
    Delay,
    Grade,
    Homework,
    HomeworkFacts,
    Lesson,
    MarksFacts,
    Period,
    Punishment,
    SessionFacts,
    Student,
    TimetableFacts,
)
from ..errors import ConnectorUndecodableError  # noqa: TID252

if TYPE_CHECKING:
    from zoneinfo import ZoneInfo


def _parse_fr_decimal(text: str) -> float | None:
    """Parse either French or dotted decimal notation."""
    try:
        return float(text.strip().replace(",", "."))
    except TypeError, ValueError:
        return None


def _decimal(value: object) -> float | None:
    """Parse an API scalar as a decimal when possible."""
    if value is None:
        return None
    return _parse_fr_decimal(str(value))


def _data(payload: object) -> object:
    """Unwrap the business data from a complete API response."""
    if isinstance(payload, Mapping) and "data" in payload:
        return payload["data"]
    return payload


def _mapping(value: object) -> Mapping[str, Any]:
    """Return one mapping or fail the tier as undecodable."""
    if not isinstance(value, Mapping):
        raise ConnectorUndecodableError("EcoleDirecte returned an unexpected payload")
    return value


def _mappings(value: object) -> tuple[Mapping[str, Any], ...]:
    """Return a list of mappings, rejecting any corrupt entry."""
    if not isinstance(value, list):
        raise ConnectorUndecodableError("EcoleDirecte returned an unexpected list")
    mappings: list[Mapping[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise ConnectorUndecodableError(
                "EcoleDirecte returned a non-object list entry"
            )
        mappings.append(item)
    return tuple(mappings)


def _optional_text(value: object) -> str | None:
    """Normalize empty API strings to absent text."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _texts(value: object) -> tuple[str, ...]:
    """Normalize a scalar or list into non-empty strings."""
    values: Iterable[object]
    if isinstance(value, list):
        values = value
    elif value is None:
        values = ()
    else:
        values = (value,)
    return tuple(text for item in values if (text := _optional_text(item)) is not None)


def _student_sources(payload: object) -> tuple[Mapping[str, Any], ...]:
    """Return every pupil record from pupil and parent account shapes."""
    data = _mapping(_data(payload))
    if "accounts" not in data:
        raise ConnectorUndecodableError("EcoleDirecte accounts key is missing")
    sources: list[Mapping[str, Any]] = []
    for account in _mappings(data["accounts"]):
        if account.get("typeCompte") == "E":
            sources.append(account)
            continue
        profile = _mapping(account.get("profile"))
        sources.extend(_mappings(profile.get("eleves")))
    return tuple(sources)


def _optional_mapping(value: object) -> Mapping[str, Any] | None:
    """Return a mapping when the API supplied one, else nothing."""
    return value if isinstance(value, Mapping) else None


def _class_label(source: Mapping[str, Any]) -> str | None:
    """Read the class label from a pupil or parent-child login record."""
    profile = _optional_mapping(source.get("profile"))
    classe = _optional_mapping(profile.get("classe") if profile is not None else None)
    if classe is None:
        classe = _optional_mapping(source.get("classe"))
    if classe is None:
        return None
    return _optional_text(classe.get("libelle"))


def _student_from_source(source: Mapping[str, Any]) -> Student:
    """Map one login pupil without exposing its source login identifier."""
    if source.get("id") is None:
        raise ConnectorUndecodableError("EcoleDirecte pupil id is missing")
    first_name = _optional_text(source.get("prenom"))
    last_name = _optional_text(source.get("nom"))
    name = " ".join(part for part in (first_name, last_name) if part is not None)
    return Student(
        id=str(source["id"]),
        name=name,
        class_name=_class_label(source),
        establishment=None,
        has_photo=False,
    )


def students_from_accounts(payload: object) -> tuple[Student, ...]:
    """Map every pupil exposed by all login accounts."""
    return tuple(_student_from_source(source) for source in _student_sources(payload))


def student_login_ids_from_accounts(payload: object) -> dict[str, str]:
    """Build the connector-private pupil-to-idLogin routing table."""
    login_ids: dict[str, str] = {}
    for source in _student_sources(payload):
        if source.get("id") is None or source.get("idLogin") is None:
            raise ConnectorUndecodableError(
                "EcoleDirecte pupil routing identifiers are missing"
            )
        login_ids[str(source["id"])] = str(source["idLogin"])
    return login_ids


def session_facts_from_login(payload: object) -> tuple[SessionFacts, ...]:
    """Create zero-call session facts for every login pupil."""
    return tuple(
        SessionFacts(student=student, periods=(), current_period=None)
        for student in students_from_accounts(payload)
    )


def _aware_datetime(value: object, zone: ZoneInfo) -> datetime:
    """Parse one ISO-like API timestamp in the entry's zone."""
    if not isinstance(value, str):
        raise ConnectorUndecodableError("EcoleDirecte returned an invalid date")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ConnectorUndecodableError(
            "EcoleDirecte returned an invalid date"
        ) from error
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=zone)
    return parsed.astimezone(zone)


def _midnight(value: object, zone: ZoneInfo) -> datetime:
    """Parse an API day as midnight in the entry's zone."""
    if not isinstance(value, str):
        raise ConnectorUndecodableError("EcoleDirecte returned an invalid day")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise ConnectorUndecodableError(
            "EcoleDirecte returned an invalid day"
        ) from error
    return datetime.combine(parsed, time.min, tzinfo=zone)


def lesson_from_ed(raw: Mapping[str, Any], *, zone: ZoneInfo) -> Lesson:
    """Map one EcoleDirecte timetable entry."""
    start = _aware_datetime(raw.get("start_date"), zone)
    end = _aware_datetime(raw.get("end_date"), zone)
    lesson_type = raw.get("typeCours")
    return Lesson(
        id=str(raw.get("id", "")),
        subject=_optional_text(raw.get("matiere") or raw.get("text")),
        subject_id=_optional_text(raw.get("codeMatiere")),
        teachers=_texts(raw.get("prof")),
        classrooms=_texts(raw.get("salle")),
        groups=_texts(raw.get("groupe")),
        start=start,
        end=end,
        canceled=bool(raw.get("isAnnule", False)),
        status="PERMANENCE" if lesson_type == "PERMANENCE" else None,
        detention=False,
        outing=False,
        exempted=bool(raw.get("dispensable") or raw.get("dispense")),
        test=False,
        memo=None,
        background_color=_optional_text(raw.get("color")),
        virtual_classrooms=(),
        num=0,
        place=0,
        duration=0,
        end_inferred=False,
    )


def timetable_facts(payload: object, *, zone: ZoneInfo) -> TimetableFacts:
    """Map a complete EcoleDirecte timetable response."""
    lessons = tuple(lesson_from_ed(raw, zone=zone) for raw in _mappings(_data(payload)))
    weeks = tuple(sorted({lesson.start.isocalendar().week for lesson in lessons}))
    return TimetableFacts(
        lessons=lessons,
        all_lessons=lessons,
        weeks_fetched=weeks,
    )


def homework_facts(payload: object) -> HomeworkFacts:
    """Map every dated group returned by the homework list endpoint."""
    data = _mapping(_data(payload))
    homework: list[Homework] = []
    for due_text, raw_items in data.items():
        try:
            due = date.fromisoformat(due_text)
        except (TypeError, ValueError) as error:
            raise ConnectorUndecodableError(
                "EcoleDirecte returned an invalid homework date"
            ) from error
        homework.extend(
            Homework(
                id=str(raw.get("idDevoir", "")),
                subject=_optional_text(raw.get("matiere")),
                description="",
                description_text="",
                due=due,
                done=bool(raw.get("effectue", False)),
                background_color=None,
                attachments=(),
            )
            for raw in _mappings(raw_items)
        )
    return HomeworkFacts(homework=tuple(homework))


def grades_from_notes(payload: object) -> tuple[Grade, ...]:
    """Map all grades, preserving unreadable values as UNKNOWN."""
    data = _mapping(_data(payload))
    if "notes" not in data:
        raise ConnectorUndecodableError("EcoleDirecte notes key is missing")
    grades: list[Grade] = []
    for raw in _mappings(data["notes"]):
        value = _decimal(raw.get("valeur"))
        out_of = _decimal(raw.get("noteSur"))
        raw_date = raw.get("date")
        if not isinstance(raw_date, str):
            raise ConnectorUndecodableError(
                "EcoleDirecte returned an invalid grade date"
            )
        try:
            grade_date = date.fromisoformat(raw_date)
        except ValueError as error:
            raise ConnectorUndecodableError(
                "EcoleDirecte returned an invalid grade date"
            ) from error
        grades.append(
            Grade(
                id=str(raw.get("id", "")),
                subject=_optional_text(raw.get("libelleMatiere")),
                subject_id=_optional_text(raw.get("codeMatiere")),
                value=value,
                status=None if value is not None else GradeStatus.UNKNOWN,
                out_of=out_of,
                default_out_of=20,
                date=grade_date,
                coefficient=_decimal(raw.get("coef")),
                class_average=_decimal(raw.get("moyenneClasse")),
                max_value=_decimal(raw.get("maxClasse")),
                min_value=_decimal(raw.get("minClasse")),
                comment=_optional_text(raw.get("commentaire")),
                is_bonus=False,
                is_optional=False,
                is_out_of_20=out_of == 20,
            )
        )
    return tuple(grades)


def periods_from_notes(payload: object, *, zone: ZoneInfo) -> tuple[Period, ...]:
    """Map non-annual periods in response order."""
    data = _mapping(_data(payload))
    raw_periods = _mappings(data.get("periodes", []))
    periods: list[Period] = []
    for raw in raw_periods:
        if raw.get("idPeriode") == "A999Z":
            continue
        periods.append(
            Period(
                id=str(raw.get("idPeriode", "")),
                name=str(raw.get("periode", "")),
                start=_midnight(raw.get("dateDebut"), zone),
                end=_midnight(raw.get("dateFin"), zone),
                index=len(periods) + 1,
            )
        )
    return tuple(periods)


def current_period_from_notes(
    payload: object,
    *,
    zone: ZoneInfo,
) -> Period | None:
    """Return the first open, non-annual EcoleDirecte period."""
    data = _mapping(_data(payload))
    periods = periods_from_notes(data, zone=zone)
    by_id = {period.id: period for period in periods}
    for raw in _mappings(data.get("periodes", [])):
        period_id = str(raw.get("idPeriode", ""))
        if period_id != "A999Z" and not raw.get("cloture", False):
            return by_id.get(period_id)
    return None


def _averages(raw_period: Mapping[str, Any]) -> tuple[Average, ...]:
    ensemble = raw_period.get("ensembleMatieres")
    if not isinstance(ensemble, Mapping):
        return ()
    disciplines = ensemble.get("disciplines", [])
    averages: list[Average] = []
    for raw in _mappings(disciplines):
        if raw.get("groupeMatiere") or _optional_text(raw.get("codeSousMatiere")):
            continue
        averages.append(
            Average(
                subject=_optional_text(raw.get("discipline")),
                subject_id=_optional_text(raw.get("codeMatiere")),
                student=_decimal(raw.get("moyenne")),
                class_average=_decimal(raw.get("moyenneClasse")),
                min_average=_decimal(raw.get("moyenneMin")),
                max_average=_decimal(raw.get("moyenneMax")),
                out_of=20,
                background_color=None,
            )
        )
    return tuple(averages)


def marks_facts(payload: object, *, current_period_id: str) -> MarksFacts:
    """Map grades and summary values for the selected period."""
    data = _mapping(_data(payload))
    grades = grades_from_notes(data)
    raw_periods = _mappings(data.get("periodes", []))
    selected = next(
        (
            raw
            for raw in raw_periods
            if str(raw.get("idPeriode", "")) == current_period_id
        ),
        None,
    )
    period_index = next(
        (
            index
            for index, raw in enumerate(
                (
                    period
                    for period in raw_periods
                    if period.get("idPeriode") != "A999Z"
                ),
                start=1,
            )
            if str(raw.get("idPeriode", "")) == current_period_id
        ),
        0,
    )
    ensemble = selected.get("ensembleMatieres") if selected is not None else None
    summary = ensemble if isinstance(ensemble, Mapping) else {}
    return MarksFacts(
        period_id=current_period_id,
        period_index=period_index,
        grades=grades,
        averages=_averages(selected) if selected is not None else (),
        overall_average=_decimal(summary.get("moyenneGenerale")),
        class_overall_average=_decimal(summary.get("moyenneClasse")),
        report=None,
    )


def attendance_facts(
    payload: object,
    *,
    zone: ZoneInfo,
) -> AttendanceFacts:
    """Map absences, delays, and punishments from one response."""
    data = _mapping(_data(payload))
    absences: list[Absence] = []
    delays: list[Delay] = []
    for raw in _mappings(data.get("absencesRetards", [])):
        at = _midnight(raw.get("date"), zone)
        reasons = _texts(raw.get("motif"))
        if raw.get("typeElement") == "Absence":
            absences.append(
                Absence(
                    id=str(raw.get("id", "")),
                    from_date=at,
                    to_date=at,
                    justified=bool(raw.get("justifie", False)),
                    hours=_optional_text(raw.get("libelle")),
                    days=None,
                    reasons=reasons,
                )
            )
        else:
            delays.append(
                Delay(
                    id=str(raw.get("id", "")),
                    at=at,
                    minutes=None,
                    justified=bool(raw.get("justifie", False)),
                    justification=_optional_text(raw.get("commentaire")),
                    reasons=reasons,
                )
            )
    punishments = tuple(
        Punishment(
            id=str(raw.get("id", "")),
            nature=_optional_text(raw.get("libelle")),
            reasons=_texts(raw.get("motif")),
            giver=_optional_text(raw.get("par")),
            given_at=(
                _midnight(raw.get("date"), zone)
                if raw.get("date") is not None
                else None
            ),
            exclusion=False,
            during_lesson=False,
            homework=_optional_text(raw.get("aFaire")),
            schedule=(),
        )
        for raw in _mappings(data.get("sanctionsEncouragements", []))
        if raw.get("typeElement") == "Punition"
    )
    return AttendanceFacts(
        period_id="",
        period_index=0,
        absences=tuple(absences),
        delays=tuple(delays),
        punishments=punishments,
    )
