"""The adapter: one function per protocol call, frozen DTOs out.

This is the only module that imports ``pronotepy``, so it is the only file to
re-read on a version bump and the only point to double in tests (§3.4).

Two rules govern the decoding, and they are not the same rule.

**Deduplicate without reimplementing.** Specification v1 concluded the raw
responses had to be decoded by hand, which contradicted its own §3.4: it
refused to take on upstream's decoding debt and then took it on. pronotepy's
data classes accept the bare dictionary -- ``Average(json)``, ``Absence(json)``,
``Delay(json)``, ``Evaluation(json)``, ``Report(data)``; ``Lesson(client,
json)`` and ``Punishment(client, json)`` take a client and nothing else. So the
shape is one raw ``post()`` per tab, the sub-lists handed to upstream's
classes, then mapping to frozen DTOs. Deduplication *and* upstream's fixes.

**One exception: `Grade`.** ``Grade.__init__`` resolves ``self.period`` through
``Util.get(Period.instances, id=p)[0]``, and ``Period.instances`` is a
never-cleared class attribute -- the only reader of that registry in all of
``dataClasses.py``. Using it would tie us to a global we can neither keep (it
leaks a dead client per period) nor clear (``[0]`` on an empty list raises
``IndexError``, wrapped as ``ParsingError``, failing the whole marks batch).
And independently: ``Util.grade_parse`` has already replaced ``|1``…``|8`` with
``"Absent"``, ``"Dispense"``… so ``Grade.grade`` is *already* lossy, and
decoding ``note.V`` ourselves is the only route to the raw sentinel that
:class:`~.const.GradeStatus` needs. Both reasons are recorded here on purpose;
this is a bounded exception, not a policy.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from html import unescape
import json
import logging
import re
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import ParseResult, parse_qs, quote, urlparse
from zoneinfo import ZoneInfo

from Crypto.Util import Padding
from pronotepy import dataClasses
from pronotepy.exceptions import DataError, ParsingError
import requests

from . import item_keys
from .const import (
    FUNC_ATTENDANCE,
    FUNC_EVALUATIONS,
    FUNC_HOMEWORK,
    FUNC_MARKS,
    FUNC_NEWS,
    FUNC_NEWS_WRITE,
    FUNC_PERSONAL_INFO,
    FUNC_REPORT,
    FUNC_TIMETABLE,
    GRADE_SENTINELS,
    PRESENCE_KIND_ABSENCE,
    PRESENCE_KIND_DELAY,
    PRESENCE_KIND_PUNISHMENT,
    AttachmentKind,
    GradeDocumentRole,
    GradeStatus,
)
from .failures import describe_failure
from .models import (
    Absence,
    Acquisition,
    AttendanceFacts,
    Average,
    Delay,
    Discussion,
    DiscussionsFacts,
    Evaluation,
    EvaluationsFacts,
    GatewayResult,
    Grade,
    GradeDocument,
    Guardian,
    Homework,
    HomeworkAttachment,
    HomeworkFacts,
    Identity,
    Information,
    Lesson,
    MarksFacts,
    Menu,
    MenusFacts,
    Message,
    NewsFacts,
    Period,
    Punishment,
    PunishmentSlot,
    Report,
    ReportSubject,
    SessionFacts,
    StaticFacts,
    Student,
    TeachingStaffMember,
    TimetableFacts,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

    from .hardened_client import HardenedClient

_LOGGER: Final = logging.getLogger(__name__)

#: What decoding one entry can raise, and why each member is in the list.
#:
#: * ``ParsingError`` -- upstream's own wrapper, raised by ``Object._resolver``
#:   when a *strict* field is missing (dataClasses.py:220).
#: * ``DataError`` -- its parent, and **not** a ``PronoteAPIError``, so it never
#:   reaches the protocol-error handling in :mod:`.session`.
#: * ``ValueError`` -- ``strptime`` on a date the establishment wrote its own
#:   way, and ``float()`` on a number that is not one.
#: * ``KeyError`` -- a converter lambda indexing a key that moved; upstream's
#:   resolver only guards the *path*, never the converter's own body.
#: * ``IndexError`` -- ``Util.grade_parse`` does ``grade_translate[int(s[1]) -
#:   1]`` against a table of eight (dataClasses.py:118), so a future ``|9``
#:   sentinel indexes past the end. This one is why the tuple is shared: it was
#:   present on two decoders and missing from five.
#: * ``TypeError`` -- a path segment that is ``null`` rather than absent, which
#:   upstream's ``KeyError`` guard does not cover.
#: * ``ZeroDivisionError`` -- ``Lesson.__init__`` computes ``place %
#:   (len(end_times) - 1)`` (dataClasses.py:905), so an establishment
#:   publishing a single ``ListeHeuresFin`` entry divides by zero.
_ENTRY_ERRORS: Final = (
    ParsingError,
    DataError,
    ValueError,
    KeyError,
    IndexError,
    TypeError,
    ZeroDivisionError,
)

#: Cap on how many discussion threads may be expanded in one cycle. Reading
#: ``Discussion.messages`` costs a request each; without a cap, a parent coming
#: back from holiday to twenty new threads would spend twenty requests in one
#: batch and trip the token bucket.
MAX_DISCUSSION_EXPANSIONS: Final = 3


class ProtocolChanged(Exception):  # noqa: N818 -- fails a tier, never a service
    """A collection the protocol should provide was absent from the response.

    Raised rather than returning an empty list, so the tier *fails* -- keeping
    its previous snapshot and going stale in due course -- instead of
    publishing a successful, empty one. See :func:`_required_list`.
    """

    def __init__(self, what: str, path: str) -> None:
        super().__init__(f"PRONOTE returned no {what} (no {path} in the response)")
        self.what = what
        self.path = path


class PeriodNotInSession(Exception):  # noqa: N818 -- fails a tier, never a service
    """The session in hand lists no period at the position a caller asked for.

    A period is identified across sessions by its position, never by its
    ``N``: every ``N`` is re-encrypted at each login. Raised rather than
    falling back on the ``N`` the caller holds, because that ``N`` belongs to
    an earlier session and posting it is the very defect
    :meth:`PronoteGateway.in_this_session` prevents; and rather than returning
    an empty collection, which would publish "no grades this term" as a
    success. The tier fails and keeps its previous snapshot.
    """

    def __init__(self, index: int) -> None:
        super().__init__(f"this session lists no period at position {index}")
        self.index = index


class DiscussionNotFound(Exception):  # noqa: N818 -- surfaced as a ServiceValidationError
    """No thread with that identifier is visible to this account."""

    def __init__(self, discussion_id: str) -> None:
        super().__init__(f"no discussion with id {discussion_id}")
        self.discussion_id = discussion_id


class ItemNotFound(Exception):  # noqa: N818 -- surfaced as a ServiceValidationError
    """No item with that key is on PRONOTE in the current session.

    The key is minted from the item's content (see :mod:`.item_keys`), so this
    is what a write meets when the item was withdrawn or rewritten between the
    collection that published the key and the gesture that sent it back. It is
    raised rather than answered with success: PRONOTE itself accepts an unknown
    ``N`` without complaint and records nothing, and a tick lost that way is
    exactly the defect the key exists to end.
    """

    def __init__(self, item_id: str) -> None:
        super().__init__(f"no item with key {item_id} in this session")


class WriteNotApplied(Exception):  # noqa: N818 -- surfaced as a HomeAssistantError
    """PRONOTE accepted a write and the re-read shows it was not recorded.

    The server answers a write it ignores exactly like one it records, so the
    answer proves nothing: a tick from a parent session, or one addressed to a
    stale ``N``, is acknowledged and dropped. Only reading the item back tells
    the two apart, and a write that did not land is raised rather than
    reported as a success.
    """

    def __init__(self, item_id: str) -> None:
        super().__init__(f"the write to {item_id} was acknowledged and not recorded")
        self.item_id = item_id


class DiscussionIsClosed(Exception):  # noqa: N818 -- surfaced as a ServiceValidationError
    """PRONOTE closed the thread; ``pronotepy`` would refuse the reply."""

    def __init__(self, discussion_id: str) -> None:
        super().__init__(f"discussion {discussion_id} is closed")
        self.discussion_id = discussion_id


class RecipientNotFound(Exception):  # noqa: N818 -- surfaced as a ServiceValidationError
    """One or more named recipients are not reachable from this account."""

    def __init__(self, missing: list[str], available: list[str]) -> None:
        super().__init__(f"unknown recipients: {', '.join(missing)}")
        self.missing = missing
        self.available = available


class AttachmentUnavailable(Exception):  # noqa: N818 -- surfaced as an HTTP status
    """PRONOTE would not serve a homework document.

    Its own class because the caller is an HTTP view and has to answer with a
    status rather than a traceback -- and because the *reason* matters: the
    address is signed by the session, so a refusal here usually means the
    session died between the collection that listed the document and the click
    that asked for it, which is worth saying differently from "no such file".
    """

    def __init__(self, name: str, status: int) -> None:
        super().__init__(f"PRONOTE answered {status} for the document {name!r}")
        self.name = name
        self.status = status


# ---------------------------------------------------------------------------
# Non-strict primitives (§3.3.3)
# ---------------------------------------------------------------------------


#: Tags that end a line of prose. Substituted before the rest are dropped, so
#: a description written as paragraphs does not arrive as one run-on sentence.
_LINE_BREAK_TAG: Final = re.compile(
    r"(?i)<\s*(?:br\s*/?|/\s*(?:p|div|li|tr|h[1-6]|blockquote))\s*>"
)
_ANY_TAG: Final = re.compile(r"<[^>]*>")


def _homework_key(item: Homework) -> str:
    """Subject, due date and statement -- never ``done``, which a tick flips.

    The plain text rather than the HTML: a statement can embed an address on
    the establishment's server, and such an address carries session material.
    """
    return item_keys.mint("hw", item.subject, item.due, item.description_text)


def _attachment_key(attachment: HomeworkAttachment) -> str:
    """Name and kind, disambiguated within one homework item."""
    return item_keys.mint("att", attachment.name, str(attachment.kind))


def _stamp_homework(items: list[Homework]) -> tuple[Homework, ...]:
    """Key the homework and, inside each item, its documents."""
    with_documents = [
        dataclasses.replace(
            item, attachments=item_keys.restamp(item.attachments, _attachment_key)
        )
        for item in items
    ]
    return item_keys.restamp(with_documents, _homework_key)


def _lesson_key(lesson: Lesson) -> str:
    """Subject and slot. A cancellation or a room change keeps the key."""
    return item_keys.mint("lesson", lesson.subject, lesson.start, lesson.end)


def _download(client: HardenedClient, url: str, name: str) -> tuple[bytes, str | None]:
    """GET one ``FichiersExternes`` address, and say only *that* it failed.

    Two refusals, and neither may carry the address. A status other than 200
    is checked here rather than trusted, because ``Attachment.data`` returns
    the body whatever the status -- an expired session's login page would
    reach a card as though it were the document. And a transport error is
    re-raised bare: ``requests`` writes the full URL into its message --
    ``Max retries exceeded with url: .../FichiersExternes/<hex>/<name>?Session=``
    -- and an exception escaping the view is logged with that message, which
    puts an address that opens the document into ``home-assistant.log``.
    """
    try:
        response = client.communication.session.get(url)
    except requests.RequestException:
        raise AttachmentUnavailable(name, 502) from None
    if response.status_code != 200:
        raise AttachmentUnavailable(name, response.status_code)
    content: bytes = response.content
    declared: str | None = response.headers.get("content-type")
    return content, declared


def _external_file_url(
    client: HardenedClient, *, ref: str, name: str, genre: str
) -> str:
    """A ``FichiersExternes`` address whose segment also names a file type.

    The construction of ``dataClasses.Attachment.__init__``, line for line,
    with one key added: upstream encrypts ``{"N", "Actif"}`` and nothing else,
    which is enough for a homework file and not for a graded test's, where one
    ``N`` -- the grade's -- names two files and ``G`` says which. The cipher is
    still upstream's (``communication.encryption``), so only the plaintext and
    the formatting are repeated here; a pin bump must re-read that constructor.
    """
    plaintext = json.dumps({"N": ref, "Actif": True, "G": genre}).replace(" ", "")
    segment = client.communication.encryption.aes_encrypt(
        Padding.pad(plaintext.encode(), 16)
    ).hex()
    return (
        f"{client.communication.root_site}/FichiersExternes/{segment}/"
        + quote(name, safe="~()*!.'")
        + f"?Session={client.attributes['h']}"
    )


def _grade_key(grade: Grade) -> str:
    """Everything a teacher sets when creating the grade -- not its value.

    A corrected value is the same grade and must not be announced again.
    """
    return item_keys.mint(
        "grade",
        grade.subject,
        grade.date,
        grade.out_of,
        grade.coefficient,
        grade.comment,
        grade.is_bonus,
        grade.is_optional,
    )


def _absence_key(absence: Absence) -> str:
    """When it started and ended; justification comes later and must not count."""
    return item_keys.mint("absence", absence.from_date, absence.to_date)


def _delay_key(delay: Delay) -> str:
    """When it happened."""
    return item_keys.mint("delay", delay.at)


def _punishment_key(punishment: Punishment) -> str:
    """What, by whom and when it was given; its schedule is filled in later."""
    return item_keys.mint(
        "punishment", punishment.nature, punishment.giver, punishment.given_at
    )


def _evaluation_key(evaluation: Evaluation) -> str:
    """Name, subject, teacher and date; the levels are what changes."""
    return item_keys.mint(
        "evaluation",
        evaluation.name,
        evaluation.subject,
        evaluation.teacher,
        evaluation.date,
    )


def _information_key(information: Information) -> str:
    """Title, author and creation date; never ``read``."""
    return item_keys.mint(
        "news", information.title, information.author, information.created
    )


def _message_key(message: Message) -> str:
    """Author and time."""
    return item_keys.mint("message", message.author, message.created)


def _visible_threads(
    threads: Iterable[dataClasses.Discussion],
) -> list[dataClasses.Discussion]:
    """Drop Drafts and Trash: nobody wants an automation on those."""
    return [
        thread
        for thread in threads
        if not ({"Drafts", "Trash"} & set(thread.labels or []))
    ]


def _thread_keys(threads: list[dataClasses.Discussion]) -> list[str]:
    """Subject and creator, in listing order -- the unread count is what moves.

    Minted from the upstream threads rather than restamped on DTOs, because the
    comparison with the previous unread counts happens *before* any DTO exists,
    and a reply has to find the same thread again from a fresh listing.
    """
    return item_keys.disambiguate(
        item_keys.mint("discussion", thread.subject or None, thread.creator)
        for thread in threads
    )


def _attachment(raw: Any, establishment_host: str | None) -> HomeworkAttachment:
    """One attached document, classified once and for every consumer.

    ``G`` says which of two different things this is: ``0`` a link, ``1`` a
    file. Only the first has an address that means anything outside the session
    that fetched it -- :class:`~.models.HomeworkAttachment` gives the reason at
    length. The answer is written into ``kind`` rather than left to be inferred
    from whether ``url`` is set, because that inference is wrong on the one case
    that matters: an unusable link has no ``url`` either.

    Every refusal below happens here rather than in a card, and each is one a
    card is not placed to make.

    Upstream falls back to the *name* when a link carries no ``url``
    (``dataClasses.Attachment``: ``self.url = self.name if url is None else
    url``), so an unchecked read publishes a human label in a field a consumer
    will put in an `href`. A scheme is not a detail: an address is only
    published if it is `http` or `https`, so a `javascript:` payload typed into
    a homework entry cannot reach a dashboard's `href` through us. A relative
    address is refused by the same test -- the consumer does not know which host
    it would belong to.

    And a link is only a *third party's* link if PRONOTE would not authenticate
    it. A teacher can paste an address on the establishment's own server, and
    one carrying a ``Session`` parameter or a ``FichiersExternes`` segment is a
    session-bearing address whatever host it names: published, it would open a
    document with no credentials, which is the exact hazard this module keeps
    files out of attributes for. None was measured on a live instance -- eleven
    links, all third parties -- and that is a fact about one fortnight of
    homework, not a guarantee. The card cannot make this check itself: it does
    not know the server's host, and it should not.
    """
    name = str(_get(raw, "L"))
    identifier = str(_get(raw, "N") or "")
    if _get(raw, "G") != _ATTACHMENT_LINK:
        if not identifier:
            # A file is fetched *by* its identifier, so without one there is
            # nothing to relay, and a service asked for it would mint an
            # address that 404s.
            return HomeworkAttachment(name=name, kind=AttachmentKind.OPAQUE)
        return HomeworkAttachment(name=name, id=identifier, kind=AttachmentKind.FILE)
    address = _get(raw, "url")
    if not isinstance(address, str):
        return HomeworkAttachment(name=name, id=identifier)
    parsed = urlparse(address)
    if parsed.scheme not in _PUBLISHABLE_SCHEMES or not parsed.netloc:
        _LOGGER.debug(
            "an attachment on %r is declared a link but carries no usable "
            "address, so it is published as a name only",
            name,
        )
        return HomeworkAttachment(name=name, id=identifier)
    if _is_session_bearing(parsed, establishment_host):
        _LOGGER.debug(
            "an attachment on %r is a link PRONOTE itself would authenticate, "
            "so its address is not published",
            name,
        )
        return HomeworkAttachment(name=name, id=identifier)
    return HomeworkAttachment(
        name=name, url=address, id=identifier, kind=AttachmentKind.LINK
    )


def _is_session_bearing(parsed: ParseResult, establishment_host: str | None) -> bool:
    """Whether an address would open something with PRONOTE's authority.

    Three signatures, any one of which is enough. The establishment's own host,
    because anything served there is served in the context of a session. A
    ``Session`` query parameter, compared without regard to case, because that
    is how PRONOTE numbers one. And a ``FichiersExternes`` path segment, because
    that is PRONOTE's route for a file whatever host a proxy puts in front of
    it.
    """
    host = parsed.hostname
    if (
        host is not None
        and establishment_host is not None
        and host == establishment_host
    ):
        return True
    if any(key.lower() == "session" for key in parse_qs(parsed.query)):
        return True
    return _PRONOTE_FILE_SEGMENT in parsed.path.lower().split("/")


def _establishment_host(root_site: object) -> str | None:
    """The host PRONOTE is served from, lower-cased, or ``None`` if unknowable.

    Read off ``communication.root_site``, a plain attribute set at login: no
    request. ``None`` rather than a guess when it is missing or not a URL, which
    only disables the host test -- the other two still run.
    """
    if not isinstance(root_site, str):
        return None
    return urlparse(root_site).hostname


#: ``G`` on a ``ListePieceJointe`` entry: a link, as opposed to a file.
_ATTACHMENT_LINK: Final = 0

#: PRONOTE's route for a file, compared lower-cased against path segments.
_PRONOTE_FILE_SEGMENT: Final = "fichiersexternes"

#: The other value of ``G``. Named because it is *written* into the payload
#: handed to ``pronotepy`` when re-deriving a file's address, and a magic ``1``
#: there would be unreadable.
_ATTACHMENT_FILE: Final = 1

#: Where a ``listeDevoirs`` entry names each of its two documents, and the file
#: types PRONOTE may serve it under, in the order they are tried -- its own
#: client's ``TypeFichierExterneHttpSco`` values, which are strings, not
#: numbers. The type goes in the ``G`` of the encrypted segment; without it the
#: server does not know which of the grade's two files the grade's ``N`` names.
#:
#: The answers open under ``DevoirCorrige`` (measured on a live instance, two
#: tests out of two). The paper did **not** open under ``DevoirSujet`` on the
#: same tests -- the server answered 404 both times -- so ``EvaluationSujet``,
#: the enumeration's other paper type, is tried next. A fallback that served
#: it, or a refusal under every type, is logged as a warning by
#: `PronoteGateway.grade_document`, so the order can be settled from evidence
#: and the losing type dropped.
_GRADE_DOCUMENTS: Final[tuple[tuple[GradeDocumentRole, str, tuple[str, ...]], ...]] = (
    (GradeDocumentRole.SUBJECT, "libelleSujet", ("DevoirSujet", "EvaluationSujet")),
    (GradeDocumentRole.CORRECTION, "libelleCorrige", ("DevoirCorrige",)),
)


def grade_document_cost(role: GradeDocumentRole) -> int:
    """The most requests opening one graded test's document can place.

    One ``DernieresNotes``, then one GET per file type tried. Declared as the
    worst case because the limiter charges at admission and never refunds: an
    under-declared fallback would spend budget the limiter never saw.
    """
    return 1 + next(
        len(genres) for known, _key, genres in _GRADE_DOCUMENTS if known is role
    )


#: ``E`` on a line of a ``Saisie*`` list: the entity state, ``2`` meaning
#: "modified" (``1`` created, ``3`` deleted). A line without it is not applied.
_ENTITY_MODIFIED: Final = 2

#: The only schemes an attachment address is published under. A consumer puts
#: this value in an `href`, so the list is a whitelist and not a filter.
_PUBLISHABLE_SCHEMES: Final = frozenset({"http", "https"})


def _plain_text(html: str) -> str:
    """The same prose, without the markup PRONOTE writes into it.

    ``descriptif`` is HTML: teachers type into a rich-text field, so a homework
    description arrives as ``<div>…</div>``, ``<br>`` and character entities
    (``&#039;``, ``&quot;``, ``&nbsp;``). That is unusable at both ends of a
    Home Assistant install. A card cannot inject it -- doing so would make
    every teacher's text field an XSS vector into the dashboard -- and cannot
    print it either, because the parent then reads the tags out loud.

    So the conversion belongs here, once, and not in each consumer: this module
    is the only place that *knows* the field is HTML, and three cards each
    inventing their own stripper is three subtly different answers to
    "&amp;amp;".

    The HTML is kept alongside in ``description``. It carries emphasis and the
    occasional link, and discarding what upstream sent in favour of our reading
    of it is the one thing §3.1 refuses to do.

    Order matters. Breaks become newlines first, then tags go, then entities are
    decoded -- decoding earlier would turn a literal ``&lt;b&gt;`` the teacher
    typed into a tag and delete it.
    """
    if not html:
        return ""
    text = _LINE_BREAK_TAG.sub("\n", html)
    text = _ANY_TAG.sub("", text)
    text = unescape(text).replace("\xa0", " ")
    lines = [line.strip() for line in text.split("\n")]
    return "\n".join(line for line in lines if line)


def _get(source: Any, *path: str) -> Any:
    """Walk a path through nested dicts, yielding ``None`` on any miss.

    An absent field gives ``None``, never an exception: class averages, minima,
    maxima and coefficients are legitimately absent when the establishment does
    not publish them.
    """
    value: Any = source
    for key in path:
        if not isinstance(value, dict) or key not in value:
            return None
        value = value[key]
    return value


def _list(source: Any, *path: str) -> list[Any]:
    """Read a ``{"V": [...]}`` list, or an empty list."""
    value = _get(source, *path)
    if isinstance(value, dict):
        value = value.get("V")
    return value if isinstance(value, list) else []


def _shape(value: Any) -> str:
    """What a value *is*, named without quoting anything it contains.

    Key names and types only, never a value: this ends up in the warning a
    user is invited to attach to a public issue (§8.2).

    The distinction between an empty mapping and a missing one is the whole
    point, and the first version of this warning lost it: both printed ``[]``,
    because a caller passing ``_get(...) or {}`` turns "absent" into "empty"
    before the reader ever sees it. A live establishment then reported "what it
    does carry is ``[]``" and nobody could tell whether the section was empty
    or the path was wrong -- which is exactly the question the warning exists
    to answer.
    """
    if value is None:
        return "absent"
    if isinstance(value, dict):
        return "an empty mapping" if not value else f"a mapping of {sorted(value)}"
    if isinstance(value, list):
        return f"a list of {len(value)}"
    return f"a {type(value).__name__}"


def _required_list(source: Any, *path: str, what: str) -> list[Any]:
    """Read a collection the protocol is expected to provide.

    Distinguishes "the key is absent" from "the list is empty", and refuses the
    first. That distinction is the difference between a broken integration and
    a quiet school week, and collapsing them is the worst failure mode this
    gateway has: if PRONOTE renamed ``ListeCours``, then ``_get(...) or []``
    returned an empty timetable, the tier reported **success**, the snapshot was
    replaced, ``binary_sensor.<eleve>_jour_de_classe`` read ``off`` and the
    wake-up automation simply stopped firing -- with nothing in the log, and
    staleness never triggering either, because the collection had succeeded.

    Raising instead makes the tier fail, which keeps the previous snapshot,
    marks it dated and eventually raises a repair (§5.4). "I know, but it is
    old" is a recoverable answer; "there are no lessons this week" when a field
    was renamed is not.
    """
    cursor: Any = source
    for depth, key in enumerate(path):
        if not isinstance(cursor, dict) or key not in cursor:
            # *Which* key of the path is missing, not just the path. Reporting
            # the whole path and the shape at the failure point still cannot
            # tell `{"dataSec": {}}` from `{"dataSec": {"data": {}}}`: both
            # stop at an empty mapping, and both responses are "a mapping of
            # ['dataSec']" from the outside. Naming the level reached and the
            # key it lacks is the only version of this warning that answers
            # the question it is for.
            reached = ".".join(path[:depth]) if depth else "the response"
            # The key names actually present, which is the single piece of
            # information that turns "the protocol changed" into "the protocol
            # changed *to this*". Names only, never a value: this warning lands
            # in the file users are invited to attach to a public issue (§8.2).
            #
            # It is here because its absence was expensive. A live server
            # dropped one key, the log said `KeyError: 'liste'`, and there was
            # no way to tell a renamed field from a section the establishment
            # does not publish without shipping a build just to look.
            _LOGGER.warning(
                "PRONOTE returned nothing usable for %s: %s carries no %r, so "
                "%s could not be read. What %s does carry is %s. This usually "
                "means the protocol changed and the integration needs an "
                "update; treating the collection as failed rather than as "
                "empty so the previous data is kept",
                what,
                reached,
                key,
                ".".join(path),
                reached,
                _shape(cursor),
            )
            raise ProtocolChanged(what, ".".join(path))
        cursor = cursor[key]
    if isinstance(cursor, dict) and "V" in cursor:
        cursor = cursor["V"]
    if cursor is None:
        return []
    if not isinstance(cursor, list):
        _LOGGER.warning(
            "PRONOTE returned a %s that is not a list but a %s; treating the "
            "collection as failed rather than as empty",
            what,
            type(cursor).__name__,
        )
        raise ProtocolChanged(what, ".".join(path))
    return cursor


def _structure(value: Any, depth: int = 0) -> Any:
    """A payload's structure -- its keys and value types -- with no value in it.

    For diagnosing a protocol field this module does not read yet, without
    putting a single identifier, name or mark into a log: every leaf becomes
    the name of its type, a list is described by its first element, and the
    walk stops four levels down.
    """
    if depth >= 4:
        return "..."
    if isinstance(value, dict):
        return {key: _structure(item, depth + 1) for key, item in sorted(value.items())}
    if isinstance(value, list):
        return [_structure(value[0], depth + 1)] if value else []
    return type(value).__name__


def _color_census(kind: str, colors: Iterable[str | None]) -> str:
    """Count how many entries of a tier carry a subject colour, in THREE buckets.

    Three and not two, because the two questions "did the server send nothing"
    and "did the server send an empty string" have different answers and the
    same symptom. ``pronotepy`` resolves ``CouleurFond`` with ``strict=False``,
    which yields ``None`` for an absent key and ``""`` for a present but empty
    one, so a two-bucket count would report "no colour" for both and the next
    person would look in the wrong place.

    One honest limit, written here so the reading is not over-trusted: the raw
    decode paths spell ``_get(entry, "CouleurFond") or None``, which folds the
    empty string into ``None`` before the value reaches the DTO. ``empty`` can
    therefore only ever be non-zero for lessons pronotepy decoded itself. When
    it reads zero, that is not evidence the server sends no empty strings.

    Returns the sentence rather than logging it, so the caller decides the
    level and so a test can assert on the counting without a log fixture.
    """
    absent = empty = present = 0
    for color in colors:
        if color is None:
            absent += 1
        elif color.strip() == "":
            empty += 1
        else:
            present += 1
    return f"{kind}: {absent} absent, {empty} empty, {present} present"


def _field_names(entry: Any) -> str:
    """The KEY names of one raw entry, sorted, and nothing else.

    This is the instrument that separates the two hypotheses the census cannot:
    "the server does not send a colour" and "the server sends one and we drop
    it". A census over decoded values reads zero in both cases.

    Names only, never values. A key name is protocol vocabulary -- the same
    words appear in upstream's own source -- whereas the values on a timetable
    entry are the subject, the room and the teacher of a named child. This
    distinction is what makes the line safe to paste into a bug report, and it
    is the reason the function returns keys instead of a payload excerpt.
    """
    if not isinstance(entry, dict):
        return _shape(entry)
    return ",".join(sorted(str(key) for key in entry))


def _number(raw: Any) -> float | None:
    """Parse a PRONOTE numeric string.

    Values arrive with a **comma** decimal separator, and a sentinel like
    ``|1`` is not a number at all.
    """
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    text = str(raw).strip()
    if not text or text.startswith("|"):
        return None
    try:
        return float(text.replace(",", "."))
    except ValueError:
        return None


def _grade_sentinel(raw: Any) -> GradeStatus | None:
    """Map ``|1``…``|8`` to the enum, and anything else unrecognised to UNKNOWN.

    Upstream's table has exactly eight entries and is indexed by
    ``int(string[1]) - 1``, so a future ``|9`` raises ``IndexError`` there and
    fails *every* grade in the batch. This is how we decline to inherit that
    (§3.3.3).
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if not text.startswith("|"):
        return None
    return GRADE_SENTINELS.get(text[1:2], GradeStatus.UNKNOWN)


def _strings(values: Iterable[Any], key: str = "L") -> tuple[str, ...]:
    """Pull the label out of a list of ``{"L": …}`` records."""
    out: list[str] = []
    for value in values:
        if isinstance(value, dict):
            label = value.get(key)
            if isinstance(label, str) and label:
                out.append(label)
        elif isinstance(value, str) and value:
            out.append(value)
    return tuple(out)


class PronoteGateway:
    """Turns protocol responses into frozen DTOs, in the establishment's time.

    All datetime conversion happens here, once, with the establishment's
    timezone. PRONOTE dates are naive local text with no offset, and two of
    ``Util.date_parse``'s six forms complete the missing part with *today* --
    comparing those against the clock of a machine set to UTC, which is the
    default for a container, produces silent shifts and therefore automations
    that fire at the wrong hour (§4.2).
    """

    def __init__(
        self,
        timezone: str,
        clock: Callable[[], dt.datetime] | None = None,
    ) -> None:
        self._tz = ZoneInfo(timezone)
        # `None` means "read the real clock". Passing one is how a test fixes
        # the date, which several decisions here depend on -- see `now`.
        self._clock = clock

    @property
    def timezone(self) -> ZoneInfo:
        """The establishment's timezone."""
        return self._tz

    def now(self) -> dt.datetime:
        """Current instant, in the establishment's timezone.

        Injectable, like the limiter's and the scheduler's clocks, and for the
        same reason: "does tomorrow fall in the next week?" and "which week
        does the school year start in?" are decided here, and a test that
        cannot fix the date cannot ask either question -- it can only pass on
        most days of the year.
        """
        if self._clock is not None:
            return self._clock().astimezone(self._tz)
        return dt.datetime.now(tz=self._tz)

    def today(self) -> dt.date:
        """Current date, in the establishment's timezone.

        Never ``date.today()``: that reads the host's timezone, which on a
        default container is UTC (§4.2).
        """
        return self.now().date()

    def _aware(self, value: dt.datetime | dt.date | None) -> dt.datetime | None:
        """Attach the establishment's timezone to a naive PRONOTE value.

        For fields that are legitimately absent. Where the protocol guarantees
        a value -- because upstream resolved it strictly and would have raised
        otherwise -- use :meth:`_instant`, which is total.
        """
        if value is None:
            return None
        return self._instant(value)

    def _instant(self, value: dt.datetime | dt.date) -> dt.datetime:
        """Attach the establishment's timezone to a value that is always there.

        Separate from :meth:`_aware` so the callers that cannot receive
        ``None`` do not have to write a branch that cannot be taken.
        ``Absence.from_date``, ``Delay.date``, ``Lesson.start`` and
        ``ScheduledPunishment.start`` are all resolved strictly upstream: they
        raise rather than return ``None``, and that exception is already caught
        one level up. An impossible ``if ... is None: return None`` in each of
        those four decoders was four branches no test could ever reach.

        A ``date`` becomes local midnight, so a calendar can compare an all-day
        event against a timed one -- which ``date`` and ``datetime`` mixed
        cannot.
        """
        if isinstance(value, dt.datetime):
            if value.tzinfo is None:
                return value.replace(tzinfo=self._tz)
            return value.astimezone(self._tz)
        return dt.datetime.combine(value, dt.time.min, tzinfo=self._tz)

    # -- session facts: zero calls ----------------------------------------

    def session_facts(self, client: HardenedClient) -> GatewayResult[SessionFacts]:
        """Everything the login already handed us.

        ``periods``, ``class_name``, ``establishment`` and ``name`` are read out
        of ``func_options`` and ``parametres_utilisateur``, already in memory
        once authenticated -- **no call at all**. Specification v1 filed these
        under the ``static`` tier and budgeted requests for them (annexe A §2).
        """
        info = client.info
        student = Student(
            id=str(info.id),
            name=str(info.name),
            class_name=info.class_name or None,
            establishment=info.establishment or None,
            has_photo=bool(info.raw_resource.get("avecPhoto")),
        )

        periods = self._periods(client)
        return GatewayResult(
            SessionFacts(
                student=student,
                periods=periods,
                current_period=self._current_period(client, periods),
            ),
            calls=0,
        )

    def _periods(self, client: HardenedClient) -> tuple[Period, ...]:
        """The periods as *this* session names them. Zero calls.

        ``ListePeriodes`` belongs to ``FonctionParametres``, which the login
        already delivered, so reading it again is free -- and it has to be
        read on the client in hand, because each period's ``N`` is encrypted
        for one session only.
        """
        periods: list[Period] = []
        raw_periods = _get(
            client.func_options, "dataSec", "data", "General", "ListePeriodes"
        )
        for index, raw in enumerate(raw_periods or [], start=1):
            start = self._aware(_parse_datetime(_get(raw, "dateDebut", "V")))
            end = self._aware(_parse_datetime(_get(raw, "dateFin", "V")))
            if start is None or end is None:
                continue
            periods.append(
                Period(
                    id=str(raw.get("N")),
                    name=str(raw.get("L", "")),
                    start=start,
                    end=end,
                    index=index,
                )
            )
        return tuple(periods)

    def in_this_session(self, client: HardenedClient, period: Period) -> Period:
        """The same period, under the ``N`` the client in hand knows it by.

        A caller holds a :class:`Period` taken from the session facts, and the
        session facts are read at set-up and when the roster changes -- not at
        every login. A login can happen inside any call (the session expired,
        or ``per_batch`` opened a new one), and that call then carried the
        previous session's ``N`` onto the wire: on a parent account until the
        end of the batch, on a student account -- whose roster never changes,
        so whose facts were never read again -- for as long as Home Assistant
        ran. Resolved here, inside the call and on the client that places it,
        so no login anywhere upstream can come between the two.

        By position, the one handle on a period that survives a login
        (:attr:`Period.index`). Free: no request is placed.
        """
        for live in self._periods(client):
            if live.index == period.index:
                return live
        raise PeriodNotInSession(period.index)

    def _current_period(
        self, client: HardenedClient, periods: Sequence[Period]
    ) -> Period | None:
        """The period PRONOTE considers current, or ``None``.

        ``Client.current_period`` falls back to ``onglets[0]`` when tab 198 is
        absent, which silently names the wrong period in an establishment that
        does not publish grades. We prefer ``None``, and the entity goes
        unavailable rather than wrong (§3.3.3).
        """
        tabs = _list(
            client.parametres_utilisateur,
            "dataSec",
            "data",
            "ressource",
            "listeOngletsPourPeriodes",
        )
        marks_tab = next((tab for tab in tabs if _get(tab, "G") == 198), None)
        if marks_tab is None:
            _LOGGER.debug(
                "no marks tab (198) in listeOngletsPourPeriodes; "
                "current period left undetermined rather than guessed"
            )
            return None
        period_id = _get(marks_tab, "periodeParDefaut", "V", "N")
        if period_id is None:
            return None
        return next((p for p in periods if p.id == str(period_id)), None)

    # -- timetable ---------------------------------------------------------

    def timetable(self, client: HardenedClient) -> GatewayResult[TimetableFacts]:
        """The current week and the next one, always, inside the school year.

        ``Client.lessons()`` loops ``for week in range(first_week, last_week +
        1)`` and posts ``PageEmploiDuTemps`` once *per week*, then filters
        client-side. Asking for "today and tomorrow" therefore costs exactly the
        same request as asking for the whole week, which is why the separate
        week tier was folded in here (§5.2).

        The horizon used to be those two days, with next week's request placed
        only when tomorrow fell in another week -- that is, **on a Sunday and
        on no other day**. So from Saturday's first collection until Monday's,
        the timetable held nothing at all about Monday: the card drew an empty
        day, ``sensor.<eleve>_prochain_cours`` had nothing to point at, and
        ``binary_sensor.<eleve>_vacances``, which asks a seven-day question,
        was answering it from two days of data and read ``on`` every weekend --
        an automation that skips the alarm on holidays skipped it for Monday.
        Reaching one week ahead instead costs one extra request per collection
        and makes the fetched window (14 days from Monday, 8 from Sunday) at
        least as wide as the widest question asked of it.

        The weeks are clamped to ``[first_week, last_week]``: outside the
        school year nothing is asked for at all. The long holidays are the
        reason -- from the last day of one year to the first Monday of the
        next, PRONOTE publishes no timetable, and a week number beyond
        ``DerniereDate`` is not an empty week but a question the server was
        never asked before. A tier that fails on it fails every tick, for two
        months, and takes its entities down with it.

        Raw posts rather than ``client.lessons()`` for one reason: the DTO needs
        ``place``, ``duree`` and whether ``end`` was supplied or inferred, and
        none of those survive ``pronotepy.Lesson``.
        """
        weeks = self._timetable_weeks(client)

        fetched: list[Lesson] = []
        calls = 0
        for week in weeks:
            raw_lessons, used = self._fetch_week(client, week)
            calls += used
            fetched.extend(raw_lessons)
        # Keyed in a fixed order, so two entries sharing a slot and a subject --
        # a cancelled original and its replacement -- get the same ordinals
        # from one session to the next.
        lessons = list(
            item_keys.restamp(
                sorted(
                    fetched,
                    key=lambda lesson: (lesson.start, lesson.place, lesson.num),
                ),
                _lesson_key,
            )
        )

        # A measurement, not an error trace: this path has decoded the colour
        # field since day one and published it nowhere, so nobody knew whether
        # it arrives empty. One line per collection, under
        # `custom_components.carnet_scolaire` -- never under `pronotepy`, whose DEBUG
        # writes the reversible hex of every request body.
        _LOGGER.debug(
            "%s",
            _color_census(
                "lesson colours", (lesson.background_color for lesson in lessons)
            ),
        )

        return GatewayResult(
            TimetableFacts(
                lessons=deduplicate_lessons(lessons),
                # Sorted, but not de-duplicated: the change detector needs the
                # entries de-duplication discards. See `TimetableFacts`.
                all_lessons=tuple(
                    sorted(lessons, key=lambda lesson: (lesson.start, lesson.place))
                ),
                weeks_fetched=tuple(weeks),
            ),
            calls=calls,
        )

    def _timetable_weeks(self, client: HardenedClient) -> tuple[int, ...]:
        """This week and the next, minus any that falls outside the year.

        Both bounds are pure arithmetic on values read once at login
        (``PremierLundi``, ``DerniereDate``), so the clamp costs no request --
        see :meth:`_first_week` and :meth:`_last_week`, which the homework span
        already uses. Two dates can land in the same week (the days before the
        first Monday all do), hence the set.
        """
        today = self.today()
        first = self._first_week(client)
        last = self._last_week(client)
        wanted = {
            int(client.get_week(today)),
            int(client.get_week(today + dt.timedelta(days=7))),
        }
        weeks = tuple(sorted(week for week in wanted if first <= week <= last))
        if not weeks:
            _LOGGER.debug(
                "no timetable week to ask for: %s is outside the school year "
                "[%d..%d], which is the ordinary state of the summer holidays",
                today.isoformat(),
                first,
                last,
            )
        return weeks

    def _fetch_week(
        self, client: HardenedClient, week: int
    ) -> tuple[list[Lesson], int]:
        """One ``PageEmploiDuTemps`` request, decoded."""
        resource = client.parametres_utilisateur["dataSec"]["data"]["ressource"]
        payload = {
            "ressource": resource,
            "Ressource": resource,
            "avecAbsencesEleve": False,
            "avecConseilDeClasse": True,
            "estEDTPermanence": False,
            "avecAbsencesRessource": True,
            "avecDisponibilites": True,
            "avecInfosPrefsGrille": True,
            "NumeroSemaine": week,
            "numeroSemaine": week,
        }
        raw = client.post(FUNC_TIMETABLE[0], FUNC_TIMETABLE[1], payload)
        entries = _required_list(raw, "dataSec", "data", "ListeCours", what="timetable")
        if entries:
            # One entry is enough: entries of the same response share their key
            # set. Looking for `CouleurFond` in this line answers the question
            # the census cannot -- whether the server sends the field at all.
            _LOGGER.debug("timetable entry fields: %s", _field_names(entries[0]))

        lessons = [
            lesson
            for lesson in (self._lesson(client, entry) for entry in entries)
            if lesson is not None
        ]
        return lessons, 1

    def _lesson(self, client: HardenedClient, entry: dict[str, Any]) -> Lesson | None:
        """Decode one timetable entry, keeping the raw slot coordinates.

        ``TypeError`` is in the catch list for a concrete reason: upstream's
        ``_resolver`` walks the path inside ``try: ... except KeyError``, so a
        segment holding ``null`` -- ``{"cahierDeTextes": {"V": null}}`` is a
        real response -- raises ``TypeError``, which is neither converted to
        ``ParsingError`` nor caught by the obvious tuple. One such entry used to
        fail the whole tier, which is exactly what §3.3.3 forbids.

        And when upstream does refuse the entry, the raw JSON is decoded
        directly rather than the slot being dropped: see :meth:`_lesson_raw`.
        """
        try:
            upstream = dataClasses.Lesson(client, entry)
        except _ENTRY_ERRORS as error:
            # Described, never traced: the traceback of a decoding error
            # prints what it could not decode, and so would the entry's `N`.
            fallback = self._lesson_raw(entry)
            if fallback is None:
                _LOGGER.debug(
                    "skipping an undecodable timetable entry: %s",
                    describe_failure(error),
                )
            else:
                _LOGGER.debug(
                    "decoding a timetable entry from raw JSON: pronotepy "
                    "refused it (%s)",
                    describe_failure(error),
                )
            return fallback

        start = self._instant(upstream.start)
        end, made_up = self._sane_end(start, self._instant(upstream.end), upstream.id)

        subject = upstream.subject
        return Lesson(
            id=str(upstream.id),
            subject=subject.name if subject else None,
            subject_id=str(subject.id) if subject else None,
            teachers=tuple(upstream.teacher_names or ()),
            classrooms=tuple(upstream.classrooms or ()),
            groups=tuple(upstream.group_names or ()),
            start=start,
            end=end,
            canceled=bool(upstream.canceled),
            status=upstream.status,
            detention=bool(upstream.detention),
            outing=bool(upstream.outing),
            exempted=bool(upstream.exempted),
            test=bool(upstream.test),
            memo=upstream.memo,
            background_color=upstream.background_color,
            virtual_classrooms=tuple(upstream.virtual_classrooms or ()),
            num=int(upstream.num or 0),
            place=int(entry.get("place", 0) or 0),
            duration=int(entry.get("duree", 1) or 1),
            # `Util.place2time` carries the upstream comment "might be wrong...
            # works with demo", and everything downstream depends on `end`.
            # `or made_up`: an end the guard had to fabricate is inferred
            # too, even when the field was present. See `_sane_end`.
            end_inferred=_get(entry, "DateDuCoursFin", "V") is None or made_up,
        )

    def _sane_end(
        self, start: dt.datetime, end: dt.datetime, identifier: object
    ) -> tuple[dt.datetime, bool]:
        """Guarantee ``end > start``, and say whether the end had to be made up.

        When ``DateDuCoursFin`` is absent, upstream infers the end as
        ``place % (len(end_times) - 1) + duree - 1`` and then feeds *that*
        through ``place2time`` a second time, under its own comment "might be
        wrong... works with demo". At the last slots of the day the modulo
        wraps, so a 17:00 lesson came back ending at 09:00 -- an inverted
        interval that a calendar draws as a broken event, that
        ``binary_sensor.<eleve>_en_cours`` can never match, and that makes
        ``sensor.<eleve>_fin_des_cours`` wrong for the whole day.

        §4.1 makes this gateway the single place ``end`` is established, so it
        is also the only place that can refuse an impossible one.

        **Returning the substitution rather than hiding it** is the point of
        the second element. This used to claim that ``end_inferred`` was
        already ``True`` on every entry it could touch -- but that flag is
        exactly ``DateDuCoursFin is None``, while this guard fires on
        ``end <= start`` whatever the field said. A server publishing a
        present-but-impossible end therefore got a fabricated ``start + 1h``
        with the flag still ``False``: an invented hour declared reliable,
        which is the opposite of what the flag is for, and worst precisely
        where the data is least trustworthy. Whether a server ever does that
        is unknown and does not matter -- the caller ORs this into
        ``end_inferred``, so the invariant now holds by construction instead
        of by circumstance.
        """
        if end > start:
            return end, False
        _LOGGER.debug(
            "lesson %s came back ending before it starts (%s -> %s); using a "
            "one-hour slot instead and marking the end as inferred",
            identifier,
            start.isoformat(),
            end.isoformat(),
        )
        return start + dt.timedelta(hours=1), True

    def _lesson_raw(self, entry: dict[str, Any]) -> Lesson | None:
        """Decode the minimum a timetable slot needs, from raw JSON only.

        Used when upstream refuses the entry. The most common cause is the most
        awkward one: ``Lesson.__init__`` resolves ``ListeContenus`` with no
        ``strict=False``, and a slot with no published content -- which is
        precisely how a **cancellation** often arrives -- has no
        ``ListeContenus`` at all. Dropping those entries meant the one event
        this integration exists to emit, ``event.<eleve>_cours_modifie`` with
        ``lesson_canceled``, could not fire for them.

        Everything derived from the content record (subject, teachers, rooms)
        is legitimately absent here. The slot's coordinates, its timing and its
        flags all live on the entry itself, and that is enough for
        de-duplication, for the delta and for the calendar.
        """
        identifier = entry.get("N")
        start = self._aware(_parse_datetime(_get(entry, "DateDuCours", "V")))
        if identifier is None or start is None:
            return None

        duration = int(entry.get("duree", 1) or 1)
        raw_end = self._aware(_parse_datetime(_get(entry, "DateDuCoursFin", "V")))
        end = raw_end if raw_end is not None else start + dt.timedelta(hours=duration)

        end, made_up = self._sane_end(start, end, identifier)

        return Lesson(
            id=str(identifier),
            subject=None,
            subject_id=None,
            teachers=(),
            classrooms=(),
            groups=(),
            start=start,
            end=end,
            canceled=bool(entry.get("estAnnule", False)),
            status=_get(entry, "Statut") or None,
            detention=bool(entry.get("estRetenue", False)),
            outing=bool(entry.get("estSortiePedagogique", False)),
            exempted=bool(entry.get("dispenseEleve", False)),
            test=False,
            memo=None,
            background_color=_get(entry, "CouleurFond") or None,
            virtual_classrooms=(),
            num=int(entry.get("P", 0) or 0),
            place=int(entry.get("place", 0) or 0),
            duration=duration,
            end_inferred=raw_end is None or made_up,
        )

    # -- homework ----------------------------------------------------------

    def homework(self, client: HardenedClient) -> GatewayResult[HomeworkFacts]:
        """Homework for the whole school year, in one request.

        Two things here were wrong and both were silent, so they are worth
        spelling out.

        **The span starts at the first day of the school year, not today.**
        Upstream filters its own response with ``if date_from <= hw.date <=
        date_to`` and builds the week domain from ``get_week(date_from)``, so
        passing ``self.today()`` discarded every *overdue* assignment. That made
        ``binary_sensor.<eleve>_devoirs_en_retard`` structurally incapable of
        ever being ``on``, and its ``count`` permanently ``0`` -- while the
        docstring claimed the opposite. Fixing it costs nothing: the request is
        a week *range*, so one post covers the year either way, and
        ``homework_horizon`` stays what §5.2 says it is, a presentation filter.

        **The decoding is by hand**, for the reason §3.3.3 gives.
        ``Homework.__init__`` resolves ``TAFFait``, ``descriptif.V`` and
        ``Matiere.V`` with no default, so a single entry served without one of
        them raised ``ParsingError`` from inside upstream's list comprehension
        and failed the **entire** tier -- taking the to-do list, the calendar
        and both homework sensors down with it.
        """
        payload = {
            "domaine": {
                "_T": 8,
                "V": f"[{self._first_week(client)}..{self._last_week(client)}]",
            }
        }
        raw = client.post(FUNC_HOMEWORK[0], FUNC_HOMEWORK[1], payload)
        entries = _required_list(
            raw, "dataSec", "data", "ListeTravauxAFaire", what="homework"
        )

        establishment_host = _establishment_host(
            getattr(client.communication, "root_site", None)
        )
        items = [
            item
            for item in (self._homework(entry, establishment_host) for entry in entries)
            if item is not None
        ]
        _LOGGER.debug(
            "%s",
            _color_census(
                "homework colours", (item.background_color for item in items)
            ),
        )
        if entries:
            _LOGGER.debug("homework entry fields: %s", _field_names(entries[0]))
        return GatewayResult(HomeworkFacts(homework=_stamp_homework(items)), calls=1)

    @staticmethod
    def _first_week(client: HardenedClient) -> int:
        """Week number of the first day of the school year.

        ``ClientBase.start_day`` comes from ``General.PremierLundi``, read once
        at login, and ``get_week`` is pure arithmetic on it -- no request.
        """
        return int(client.get_week(client.start_day))

    @staticmethod
    def _last_week(client: HardenedClient) -> int:
        """Week number of the last day of the school year.

        Mirrors what ``Client.homework`` does when ``date_to`` is omitted, but
        without inheriting its bare ``strptime``: a ``DerniereDate`` in an
        unexpected form would raise ``ValueError`` from the middle of the tier,
        and a bounded guess is a far better answer than no homework at all.
        """
        last = _parse_date(
            _get(client.func_options, "dataSec", "data", "General", "DerniereDate", "V")
        )
        if last is None:
            _LOGGER.debug("no usable DerniereDate; asking for a full 62-week year")
            return 62
        return int(client.get_week(last))

    def _homework(
        self, entry: dict[str, Any], establishment_host: str | None = None
    ) -> Homework | None:
        """Decode one homework item, tolerating any absent optional field."""
        identifier = entry.get("N")
        due = _parse_date(_get(entry, "PourLe", "V"))
        if identifier is None or due is None:
            return None

        description = _get(entry, "descriptif", "V")
        return Homework(
            id=str(identifier),
            subject=_get(entry, "Matiere", "V", "L") or None,
            description=str(description) if description else "",
            description_text=_plain_text(str(description) if description else ""),
            due=due,
            done=bool(entry.get("TAFFait", False)),
            background_color=_get(entry, "CouleurFond") or None,
            # Read off the raw payload rather than through
            # `pronotepy.Attachment`, and that is deliberate: constructing one
            # of those *builds* the session-signed URL, which is the single
            # thing that must not end up in a snapshot. Here nothing is
            # encrypted and no session value is touched.
            attachments=tuple(
                _attachment(raw_attachment, establishment_host)
                for raw_attachment in _list(entry, "ListePieceJointe")
                if _get(raw_attachment, "L")
            ),
        )

    # -- marks -------------------------------------------------------------

    def marks(
        self,
        client: HardenedClient,
        period: Period,
        *,
        with_report: bool,
    ) -> GatewayResult[MarksFacts]:
        """One ``DernieresNotes`` request, four datasets, plus the report.

        Upstream would spend four identical posts here: ``Period.grades``,
        ``.averages``, ``.overall_average`` and ``.class_overall_average`` each
        re-post ``DernieresNotes`` with the same body
        (dataClasses.py:525/534/548/578).
        """
        period = self.in_this_session(client, period)
        payload = {"Periode": {"N": period.id, "L": period.name}}
        raw = client.post(FUNC_MARKS[0], FUNC_MARKS[1], payload)
        data = _get(raw, "dataSec", "data") or {}
        calls = 1

        # Required, not optional. Upstream resolves both of these *strictly*
        # (dataClasses.py:526 and :535), which is upstream saying they are
        # always present -- so an absent key is a protocol change, and reporting
        # it as "no grades this term" would replace a good snapshot with a
        # believable lie: every subject average sensor would disappear, "last
        # grade" would go to None, and staleness would never fire because the
        # collection *succeeded*.
        entries = _required_list(data, "listeDevoirs", what="grades")
        grades = tuple(self._grade(entry) for entry in entries)
        # A graded test's paper is listed and refused under every file type
        # tried, while its answers open (measured on a live instance): the
        # paper is probably fetched by something other than the grade's `N`.
        # The structure of one such entry -- keys and types, never a value --
        # says where to look.
        documented = next(
            (
                entry
                for entry in entries
                if isinstance(entry, dict) and entry.get("libelleSujet")
            ),
            None,
        )
        if documented is not None:
            _LOGGER.debug(
                "A graded test with a paper has this shape: %s", _structure(documented)
            )
        averages = tuple(
            self._average(entry)
            for entry in _required_list(data, "listeServices", what="subject averages")
        )

        _LOGGER.debug(
            "%s",
            _color_census(
                "subject average colours",
                (
                    average.background_color
                    for average in averages
                    if average is not None
                ),
            ),
        )

        report: Report | None = None
        if with_report:
            report, used = self._report(client, period)
            calls += used

        return GatewayResult(
            MarksFacts(
                period_id=period.id,
                period_index=period.index,
                grades=item_keys.restamp(
                    [g for g in grades if g is not None], _grade_key
                ),
                averages=tuple(a for a in averages if a is not None),
                overall_average=_number(_get(data, "moyGenerale", "V")),
                class_overall_average=_number(_get(data, "moyGeneraleClasse", "V")),
                report=report,
            ),
            calls=calls,
        )

    def _grade(self, entry: dict[str, Any]) -> Grade | None:
        """Decode one grade by hand -- the single documented exception (§3.3.2).

        Not for the sake of saving a call: for the two reasons in this module's
        docstring. ``value`` and ``status`` are mutually exclusive, which is
        what lets the "last grade" sensor hold a numeric state usable by a
        threshold trigger.
        """
        identifier = entry.get("N")
        if identifier is None:
            return None

        raw_value = _get(entry, "note", "V")
        status = _grade_sentinel(raw_value)
        value = None if status is not None else _number(raw_value)

        parsed_date = _parse_date(_get(entry, "date", "V"))
        if parsed_date is None:
            return None

        subject_name = _get(entry, "service", "V", "L")
        subject_id = _get(entry, "service", "V", "N")

        return Grade(
            id=str(identifier),
            subject=str(subject_name) if subject_name else None,
            subject_id=str(subject_id) if subject_id else None,
            value=value,
            status=status,
            out_of=_number(_get(entry, "bareme", "V")),
            default_out_of=_number(_get(entry, "baremeParDefaut", "V")),
            date=parsed_date,
            coefficient=_number(entry.get("coefficient")),
            class_average=_number(_get(entry, "moyenne", "V")),
            max_value=_number(_get(entry, "noteMax", "V")),
            min_value=_number(_get(entry, "noteMin", "V")),
            comment=entry.get("commentaire") or None,
            is_bonus=bool(entry.get("estBonus", False)),
            is_optional=bool(entry.get("estFacultatif", False))
            and not bool(entry.get("estBonus", False)),
            is_out_of_20=bool(entry.get("estRamenerSur20", False)),
            documents=tuple(
                GradeDocument(name=str(name), role=role)
                for role, key, _genres in _GRADE_DOCUMENTS
                if (name := entry.get(key))
            ),
            remark=entry.get("commentaireSurNote") or None,
            subject_in_groups=bool(_get(entry, "service", "V", "estServiceGroupe")),
        )

    def _average(self, entry: dict[str, Any]) -> Average | None:
        """Decode one per-subject average by hand.

        The second documented exception to "reuse upstream's decoder", and it
        is forced by a head-on conflict with §3.3.3. ``dataClasses.Average``
        resolves ``moyClasse``, ``moyMin`` and ``moyMax`` with neither a
        ``default`` nor ``strict=False``, so an establishment that does not
        publish class statistics -- which §3.3.3 names explicitly as a
        legitimate case -- made the constructor raise for **every single
        subject**. The result was not a degraded reading but no averages at
        all: ``MarksFacts.averages`` came back empty, every
        ``sensor.<eleve>_moyenne_<matiere>`` disappeared, and the only trace was
        one DEBUG line per subject.

        Keyed on the subject, because ``Average`` carries no identifier of its
        own and §2.4 rules out using a position in a list.
        """
        subject_name = _get(entry, "L")
        subject_id = entry.get("N")
        if not subject_name and subject_id is None:
            return None

        return Average(
            subject=str(subject_name) if subject_name else None,
            subject_id=str(subject_id) if subject_id is not None else None,
            student=_number(_get(entry, "moyEleve", "V")),
            class_average=_number(_get(entry, "moyClasse", "V")),
            min_average=_number(_get(entry, "moyMin", "V")),
            max_average=_number(_get(entry, "moyMax", "V")),
            out_of=_number(_get(entry, "baremeMoyEleve", "V")),
            background_color=_get(entry, "couleur") or None,
        )

    def _report(
        self, client: HardenedClient, period: Period
    ) -> tuple[Report | None, int]:
        """Fetch a report card, or ``None`` when it is not published."""
        payload = {"periode": {"G": 2, "N": period.id, "L": period.name}}
        raw = client.post(FUNC_REPORT[0], FUNC_REPORT[1], payload)
        data = _get(raw, "dataSec", "data") or {}
        if "Message" in data:
            # PRONOTE says so itself: not published yet, or unavailable.
            return None, 1

        if "ListeServices" not in data:
            # Both of `Report`'s resolvers carry `default=[]`, so a response
            # whose shape moved decoded happily into an *empty* report -- and
            # `sensor.<eleve>_bulletin` then said "published, zero subjects"
            # rather than "not published". Requiring the key keeps those two
            # very different answers apart.
            #
            # The capital L is upstream's, not a typo: `Report` resolves
            # `ListeServices` while `DernieresNotes` uses `listeServices`.
            # Checking the lower-case spelling here reported every published
            # report card as unpublished, which is what the test that asks for
            # one is now there to catch.
            _LOGGER.debug("report card response carries no ListeServices")
            return None, 1

        try:
            upstream = dataClasses.Report(data)
        except _ENTRY_ERRORS as error:
            _LOGGER.debug(
                "skipping an undecodable report card: %s", describe_failure(error)
            )
            return None, 1

        subjects = tuple(
            ReportSubject(
                id=str(subject.id),
                name=subject.name,
                color=subject.color,
                comments=tuple(subject.comments or ()),
                student_average=_number(subject.student_average),
                class_average=_number(subject.class_average),
                min_average=_number(subject.min_average),
                max_average=_number(subject.max_average),
                coefficient=_number(subject.coefficient),
                teachers=tuple(subject.teachers or ()),
            )
            for subject in upstream.subjects
        )
        return Report(subjects=subjects, comments=tuple(upstream.comments or ())), 1

    # -- attendance --------------------------------------------------------

    def attendance(
        self, client: HardenedClient, period: Period
    ) -> GatewayResult[AttendanceFacts]:
        """One ``PagePresence`` request, three datasets.

        ``Period.absences``, ``.delays`` and ``.punishments`` each re-post
        ``PagePresence`` and then read *the same* ``listeAbsences`` list,
        filtering on ``G`` being 13, 14 or 41 (dataClasses.py:606/621/636). One
        request, one filter, three tuples.
        """
        period = self.in_this_session(client, period)
        payload = {
            "periode": {"N": period.id, "L": period.name, "G": 2},
            "DateDebut": {
                "_T": 7,
                "V": period.start.strftime("%d/%m/%Y %H:%M:%S"),
            },
            "DateFin": {"_T": 7, "V": period.end.strftime("%d/%m/%Y %H:%M:%S")},
        }
        raw = client.post(FUNC_ATTENDANCE[0], FUNC_ATTENDANCE[1], payload)
        # Required for the same reason as the grades: "no absences" is the
        # answer a parent acts on, and it must mean the school said so.
        entries = _required_list(
            _get(raw, "dataSec", "data") or {}, "listeAbsences", what="attendance"
        )

        absences: list[Absence] = []
        delays: list[Delay] = []
        punishments: list[Punishment] = []

        for entry in entries:
            kind = _get(entry, "G")
            if kind == PRESENCE_KIND_ABSENCE:
                absence = self._absence(entry)
                if absence is not None:
                    absences.append(absence)
            elif kind == PRESENCE_KIND_DELAY:
                delay = self._delay(entry)
                if delay is not None:
                    delays.append(delay)
            elif kind == PRESENCE_KIND_PUNISHMENT:
                punishment = self._punishment(client, entry)
                if punishment is not None:
                    punishments.append(punishment)

        return GatewayResult(
            AttendanceFacts(
                period_id=period.id,
                period_index=period.index,
                absences=item_keys.restamp(absences, _absence_key),
                delays=item_keys.restamp(delays, _delay_key),
                punishments=item_keys.restamp(punishments, _punishment_key),
            ),
            calls=1,
        )

    def _absence(self, entry: dict[str, Any]) -> Absence | None:
        """Decode an absence. Note: ``hours``/``days``, never ``minutes``."""
        try:
            upstream = dataClasses.Absence(entry)
        except _ENTRY_ERRORS as error:
            _LOGGER.debug(
                "skipping an undecodable absence: %s", describe_failure(error)
            )
            return None

        return Absence(
            id=str(upstream.id),
            from_date=self._instant(upstream.from_date),
            to_date=self._instant(upstream.to_date),
            justified=bool(upstream.justified),
            hours=upstream.hours,
            days=int(upstream.days or 0),
            reasons=tuple(upstream.reasons or ()),
        )

    def _delay(self, entry: dict[str, Any]) -> Delay | None:
        """Decode a late arrival. ``minutes`` lives here, not on the absence."""
        try:
            upstream = dataClasses.Delay(entry)
        except _ENTRY_ERRORS as error:
            _LOGGER.debug("skipping an undecodable delay: %s", describe_failure(error))
            return None

        return Delay(
            id=str(upstream.id),
            at=self._instant(upstream.date),
            minutes=int(upstream.minutes or 0),
            justified=bool(upstream.justified),
            justification=upstream.justification,
            reasons=tuple(upstream.reasons or ()),
        )

    def _punishment(
        self, client: HardenedClient, entry: dict[str, Any]
    ) -> Punishment | None:
        """Decode a punishment and its scheduled slots.

        ``ScheduledPunishment.start`` is a ``date`` when the slot has no
        ``placeExecution`` and a ``datetime`` when it does, and ``place2time``
        raises ``DataError`` on an out-of-range slot -- so both are handled
        rather than assumed.
        """
        try:
            upstream = dataClasses.Punishment(client, entry)
        except _ENTRY_ERRORS as error:
            _LOGGER.debug(
                "skipping an undecodable punishment: %s", describe_failure(error)
            )
            return None

        slots: list[PunishmentSlot] = []
        for slot in upstream.schedule:
            start = self._instant(slot.start)
            duration = slot.duration
            slots.append(
                PunishmentSlot(
                    start=start,
                    duration_minutes=(
                        int(duration.total_seconds() // 60) if duration else 0
                    ),
                )
            )

        return Punishment(
            id=str(upstream.id),
            nature=upstream.nature,
            reasons=tuple(upstream.reasons or ()),
            giver=upstream.giver,
            given_at=self._aware(upstream.given),
            exclusion=bool(upstream.exclusion),
            during_lesson=bool(upstream.during_lesson),
            homework=upstream.homework,
            schedule=tuple(sorted(slots, key=lambda slot: slot.start)),
        )

    # -- evaluations -------------------------------------------------------

    def evaluations(
        self, client: HardenedClient, period: Period
    ) -> GatewayResult[EvaluationsFacts]:
        """Competency evaluations for one period, in one request."""
        period = self.in_this_session(client, period)
        payload = {"periode": {"N": period.id, "L": period.name, "G": 2}}
        raw = client.post(FUNC_EVALUATIONS[0], FUNC_EVALUATIONS[1], payload)
        entries = _required_list(
            _get(raw, "dataSec", "data") or {}, "listeEvaluations", what="assessments"
        )

        items: list[Evaluation] = []
        for entry in entries:
            evaluation = self._evaluation(entry)
            if evaluation is not None:
                items.append(evaluation)

        return GatewayResult(
            EvaluationsFacts(
                period_id=period.id,
                period_index=period.index,
                evaluations=item_keys.restamp(items, _evaluation_key),
            ),
            calls=1,
        )

    def _evaluation(self, entry: dict[str, Any]) -> Evaluation | None:
        """Decode one evaluation, reusing upstream's class."""
        try:
            upstream = dataClasses.Evaluation(entry)
        except _ENTRY_ERRORS as error:
            _LOGGER.debug(
                "skipping an undecodable evaluation: %s", describe_failure(error)
            )
            return None

        subject = upstream.subject
        return Evaluation(
            id=str(upstream.id),
            name=upstream.name,
            subject=subject.name if subject else None,
            subject_id=str(subject.id) if subject else None,
            teacher=upstream.teacher,
            description=upstream.description,
            date=upstream.date,
            acquisitions=tuple(
                Acquisition(
                    id=str(acquisition.id),
                    name=acquisition.name,
                    level=acquisition.level,
                    abbreviation=acquisition.abbreviation,
                    coefficient=_number(acquisition.coefficient),
                    domain=acquisition.domain,
                    pillar=acquisition.pillar,
                )
                for acquisition in upstream.acquisitions
            ),
        )

    # -- news --------------------------------------------------------------

    def news(self, client: HardenedClient) -> GatewayResult[NewsFacts]:
        """News items and surveys, in one request.

        ``PageActualites`` tab 8 -- the *read*. ``SaisieActualites`` on the same
        tab is the write used by ``mark_as_read``, and specification v1 had the
        two the wrong way round (§3.4).

        ``Information.content()`` is deliberately never touched: it is a lazy
        attribute that posts when read.

        Decoded by hand for the third time, and for the same reason as homework
        and averages: ``Information.__init__`` resolves ``auteur``, ``lue``,
        ``nature.V.L``, ``estSondage`` and ``reponseAnonyme`` strictly, and
        ``client.information_and_surveys()`` builds the whole list in a single
        comprehension. One item missing a category therefore emptied the news
        tier, took ``sensor.<eleve>_informations_non_lues`` with it, and left
        the school's actual announcement invisible.
        """
        payload = {"modesAffActus": {"_T": 26, "V": "[0..3]"}}
        raw = client.post(FUNC_NEWS[0], FUNC_NEWS[1], payload)
        groups = _required_list(raw, "dataSec", "data", "listeModesAff", what="news")

        items: list[Information] = []
        for group in groups:
            for entry in _list(group, "listeActualites"):
                item = self._information(entry)
                if item is not None:
                    items.append(item)
        return GatewayResult(
            NewsFacts(information=item_keys.restamp(items, _information_key)),
            calls=1,
        )

    def _information(self, entry: dict[str, Any]) -> Information | None:
        """Decode one news item or survey, tolerating absent optional fields."""
        identifier = entry.get("N")
        created = self._aware(_parse_datetime(_get(entry, "dateCreation", "V")))
        if identifier is None or created is None:
            return None

        return Information(
            id=str(identifier),
            title=_get(entry, "L") or None,
            author=_get(entry, "auteur") or None,
            category=_get(entry, "nature", "V", "L") or None,
            read=bool(entry.get("lue", False)),
            survey=bool(entry.get("estSondage", False)),
            anonymous_response=bool(entry.get("reponseAnonyme", False)),
            created=created,
            start_date=self._aware(_parse_datetime(_get(entry, "dateDebut", "V"))),
            end_date=self._aware(_parse_datetime(_get(entry, "dateFin", "V"))),
        )

    # -- discussions -------------------------------------------------------

    def discussions(
        self,
        client: HardenedClient,
        *,
        previous_unread: dict[str, int] | None = None,
    ) -> GatewayResult[DiscussionsFacts]:
        """Discussion threads, expanding only the newly active ones.

        The list itself is one request. Message bodies are not: reading
        ``pronotepy.Discussion.messages`` posts ``ListeMessages`` **every time**,
        so expanding every thread would cost one request each -- ten threads on
        an hourly tier is ~176 requests a day, which roughly doubles the whole
        budget. The specification budgeted this tier at one request and did not
        anticipate that.

        So only threads whose unread count *went up* are expanded, capped at
        :data:`MAX_DISCUSSION_EXPANSIONS` per cycle. That is exactly what
        ``event.<student>_nouveau_message`` needs, and it normally costs zero or
        one extra request.
        """
        previous = previous_unread or {}
        threads = client.discussions()
        calls = 1

        # Drafts and Trash are filtered at the door: they are not conversations
        # anybody wants an automation on.
        visible = _visible_threads(threads)
        # `previous` is keyed by what the last snapshot published, which is the
        # minted key: compared with the session's `N` instead, every thread with
        # an unread message looked newly active after each reconnection.
        keyed = list(zip(_thread_keys(visible), visible, strict=True))

        newly_active = [
            key
            for key, thread in keyed
            if int(thread.unread or 0) > previous.get(key, 0)
        ]
        expandable = set(newly_active[:MAX_DISCUSSION_EXPANSIONS])
        if len(newly_active) > MAX_DISCUSSION_EXPANSIONS:
            _LOGGER.debug(
                "%d newly active discussions; expanding %d this cycle to stay "
                "inside the token bucket",
                len(newly_active),
                MAX_DISCUSSION_EXPANSIONS,
            )

        items: list[Discussion] = []
        opened: set[str] = set()
        for key, thread in keyed:
            messages: tuple[Message, ...] = ()
            if key in expandable:
                messages, used = self._messages(thread)
                calls += used
                opened.add(key)
            items.append(
                Discussion(
                    id=key,
                    subject=thread.subject or None,
                    creator=thread.creator,
                    unread=int(thread.unread or 0),
                    closed=bool(thread.closed),
                    labels=tuple(thread.labels or ()),
                    messages=messages,
                    ref=str(thread.id),
                )
            )

        return GatewayResult(
            DiscussionsFacts(discussions=tuple(items), expanded=frozenset(opened)),
            calls=calls,
        )

    def _messages(
        self, thread: dataClasses.Discussion
    ) -> tuple[tuple[Message, ...], int]:
        """Expand one thread. Costs exactly one request."""
        try:
            upstream_messages = thread.messages
        except _ENTRY_ERRORS as error:
            _LOGGER.debug(
                "could not expand a discussion thread: %s", describe_failure(error)
            )
            return (), 1

        messages: list[Message] = []
        for message in upstream_messages:
            created = self._aware(message.created)
            if created is None:
                continue
            messages.append(
                Message(
                    id=str(message.id),
                    author=message.author,
                    created=created,
                    content=message.content or None,
                )
            )
        return item_keys.restamp(messages, _message_key), 1

    # -- menus -------------------------------------------------------------

    def menus(self, client: HardenedClient) -> GatewayResult[MenusFacts]:
        """Today's and tomorrow's menus.

        ``Client.menus()`` walks whole weeks, so this is one request except when
        tomorrow falls in the next week -- which is why annexe B budgets 1.14
        rather than 1.
        """
        today = self.today()
        tomorrow = today + dt.timedelta(days=1)
        calls = 1 if tomorrow.isocalendar()[1] == today.isocalendar()[1] else 2

        items = [
            Menu(
                id=str(upstream.id),
                day=upstream.date,
                name=upstream.name,
                is_lunch=bool(upstream.is_lunch),
                is_dinner=bool(upstream.is_dinner),
                first_meal=_food_names(upstream.first_meal),
                main_meal=_food_names(upstream.main_meal),
                side_meal=_food_names(upstream.side_meal),
                other_meal=_food_names(upstream.other_meal),
                cheese=_food_names(upstream.cheese),
                dessert=_food_names(upstream.dessert),
            )
            for upstream in client.menus(today, tomorrow)
        ]
        return GatewayResult(MenusFacts(menus=tuple(items)), calls=calls)

    # -- static ------------------------------------------------------------

    def static(self, client: HardenedClient) -> GatewayResult[StaticFacts]:
        """The teaching staff, and nothing else.

        One request. The iCal URL used to be collected here and is not any
        more: keeping it would drop an autonomous authentication bearer into the
        snapshot store, a long-lived structure whose purpose is to be dumped
        into a diagnostic report (§8.2).

        Decoded here rather than through ``client.get_teaching_staff()``, and
        the difference is not stylistic. Upstream reads
        ``post("PageEquipePedagogique", 37)["dataSec"]["data"]["liste"]["V"]``
        with bare subscription, so a server whose response omits ``liste`` --
        which is what a live PRONOTE 26.2 does -- reaches the user as
        ``KeyError: 'liste'``. That names nothing anyone can act on, and it
        breaks §3.3's own rule: one raw ``post`` per tab, the entries handed to
        upstream's data classes, the *reading* done by this module's helpers.
        Through ``_required_list`` the same failure names the tab, the key and
        the keys the response did carry -- which is the difference between a
        diagnosable outage and a guess.
        """
        # The whole response and the whole path, not a pre-walked fragment.
        # This first read `_get(..., "dataSec", "data") or {}` and asked for
        # `liste` alone, which meant the warning could not tell an empty
        # `data` from a missing one -- the `or {}` had already turned the
        # second into the first. A live establishment then reported exactly
        # that ambiguity, the instrument was built to answer it, and it did:
        # `data` is present and **empty**. Hence the branch below.
        response = client.post("PageEquipePedagogique", 37)
        section = _get(response, "dataSec", "data")
        if isinstance(section, dict) and not section:
            # An authorised tab that answers with an empty `data` is this
            # establishment saying it publishes no teaching team -- not a
            # protocol change. Upstream's own guard proves the tab is
            # authorised: `_Communication.post` refuses an unauthorised
            # `onglet` with "Action not permitted" before anything is sent
            # (`pronoteAPI.py:126`), so a response at all means 37 is granted.
            #
            # Failing here instead cost a real instance its
            # `equipe_pedagogique` entity permanently: the tier could never
            # hold data, so it could never stop being retried, and it was the
            # one collection that never came back. And an empty staff list is
            # **safe** to publish, which is the whole reason this differs from
            # `_required_list`'s refusal. That refusal exists for collections
            # whose emptiness silently breaks an automation -- a renamed
            # `ListeCours` reads as "no lessons today" and the wake-up
            # automation stops firing. Nobody triggers on the size of the
            # teaching team, so "zero members" is an honest answer and
            # "unavailable for ever" is not.
            #
            # `info`, not `warning`: this is a property of the establishment,
            # not a fault, and it is logged once a day at most.
            _LOGGER.info(
                "this establishment publishes no teaching team: the tab is "
                "authorised and answered with an empty 'data'. Reporting an "
                "empty list rather than failing the collection, so the entity "
                "exists and says zero instead of staying unavailable"
            )
            return GatewayResult(StaticFacts(teaching_staff=()), calls=1)

        entries = _required_list(
            response,
            "dataSec",
            "data",
            "liste",
            what="the teaching staff",
        )
        members = tuple(
            TeachingStaffMember(
                name=member.name,
                role=member.type,
                subjects=tuple(subject.name for subject in member.subjects),
            )
            for member in (dataClasses.TeachingStaff(entry) for entry in entries)
        )
        return GatewayResult(StaticFacts(teaching_staff=members), calls=1)

    # -- on-demand reads, never stored ------------------------------------

    def ical_url(self, client: HardenedClient) -> tuple[str, int]:
        """Build the iCal URL on demand. One request, nothing stored.

        The caller must return this straight to the service response and drop
        it. Anyone holding this URL reads the student's timetable with no
        username and no password, and states go to the recorder, the backups,
        the screenshots and the bug reports (§8.2).
        """
        return client.export_ical(), 1

    def identity(self, client: HardenedClient) -> tuple[Identity, int]:
        """Read the full identity on demand. Never enters a state.

        ``ClientInfo._cache()`` posts through ``communication.post`` directly,
        bypassing ``ClientBase.post`` *and* the parent ``membre`` signature --
        which is why this has to be a gateway function called under the lock and
        after ``set_child``, and never a property read from an entity (§8.2).
        """
        info = client.info
        # Posted through `communication` with an explicit `ressource`, not
        # through `client.post`, which stamps the *account holder* as `membre`.
        # `ClientInfo._cache()` bypasses `ClientBase.post` for exactly this
        # reason, and says so: "we need to manually add the resource id". On a
        # parent account, `membre` returns the **parent's** birth date, e-mail,
        # telephone number and INE number -- which the service then handed back
        # attributed to the child. A wrong-attribution disclosure of precisely
        # the fields §8.2 keeps out of the state machine.
        raw = client.communication.post(
            FUNC_PERSONAL_INFO[0],
            {"Signature": {"onglet": 49, "ressource": {"N": info.id, "G": 4}}},
        )
        data = _get(raw, "dataSec", "data", "Informations") or {}

        guardians = tuple(
            Guardian(
                name=_get(entry, "L"),
                relation=_get(entry, "qualite", "V", "L"),
                email=_get(entry, "eMail"),
                phone=_get(entry, "telephonePortable"),
                address=_strings(
                    [
                        {"L": _get(entry, f"adresse{index}")}
                        for index in range(1, 5)
                        if _get(entry, f"adresse{index}")
                    ]
                ),
                is_legal=bool(_get(entry, "estResponsableLegal")),
            )
            for entry in _list(_get(raw, "dataSec", "data") or {}, "Responsables")
        )

        return (
            Identity(
                name=str(info.name),
                birth_date=_parse_date(_get(data, "dateNaiss", "V")),
                birth_place=_get(data, "villeNaiss"),
                email=_get(data, "eMail"),
                phone=_join_phone(
                    _get(data, "indicatifTel"), _get(data, "telephonePortable")
                ),
                address=_strings(
                    [
                        {"L": _get(data, f"adresse{index}")}
                        for index in range(1, 5)
                        if _get(data, f"adresse{index}")
                    ]
                ),
                ine_number=_get(data, "numeroINE"),
                guardians=guardians,
            ),
            1,
        )

    def profile_picture(self, client: HardenedClient) -> tuple[bytes | None, int]:
        """Fetch the profile photo under the lock, after ``set_child``.

        ``ClientInfo.profile_picture`` goes through ``ClientInfo._cache()``,
        which short-circuits ``ClientBase.post`` and the parent ``membre``
        signature -- so a parent account reading it as a property can show the
        wrong child's face (annexe A §5.4).
        """
        attachment = client.info.profile_picture
        if attachment is None:
            return None, 0
        data: bytes = attachment.data
        return data, 1

    def homework_attachment(
        self,
        client: HardenedClient,
        *,
        homework_id: str,
        attachment_id: str,
        name: str,
    ) -> tuple[bytes, str | None, int]:
        """Download one homework document, under the lock, after ``set_child``.

        The address is **derived by upstream and not re-implemented here**, on
        purpose. It is
        ``FichiersExternes/<hex>/<name>?Session=<h>`` where the hex segment is
        ``{"N": id, "Actif": true}`` encrypted with the session's own AES key
        and IV, and reproducing that arithmetic would give this module a second
        copy of a cipher construction that only ``pronotepy`` is versioned
        against -- a pin bump could then diverge silently, which is the class of
        failure ``hardened_client`` exists to document rather than repeat. So a
        minimal payload is handed to ``dataClasses.Attachment`` and its ``url``
        is used.

        What is *not* delegated is the status check. ``Attachment.data`` returns
        ``response.content`` whatever the status, so an expired session yields
        an HTML error page with a 200-looking shape, and a card would render a
        few kilobytes of PRONOTE's login screen as though it were the exercise.
        ``Attachment.save`` checks the status; the property does not.

        Two requests: the homework is read again in this session first, because
        the document's ``N`` -- what the encrypted segment is built from -- is
        re-encrypted by every login, and a snapshot's value would sign an
        address for a document this session does not know.
        """
        try:
            item = self._current_homework(client, homework_id)
        except ItemNotFound as error:
            raise AttachmentUnavailable(name, 404) from error
        document = next(
            (
                candidate
                for candidate in item.attachments
                if candidate.id == attachment_id
                and candidate.kind is AttachmentKind.FILE
            ),
            None,
        )
        if document is None:
            raise AttachmentUnavailable(name, 404)
        attachment = dataClasses.Attachment(
            client, {"L": name, "N": document.ref, "G": _ATTACHMENT_FILE}
        )
        content, declared = _download(client, attachment.url, name)
        return content, declared, 2

    def grade_document(
        self,
        client: HardenedClient,
        *,
        period_index: int,
        grade_id: str,
        role: GradeDocumentRole,
        name: str,
    ) -> tuple[bytes, str | None, int]:
        """Download a graded test's paper or answers, under the lock.

        One request more than the file types tried (`grade_document_cost`):
        the file is fetched by the grade's ``N``, which every login
        re-encrypts, so the grades of its period are read again in this
        session first -- one ``DernieresNotes``, without the report card --
        and the key looked up in them. Then one GET per type until one serves
        the file; only a 404 moves on to the next type, any other refusal is
        the answer.

        The address is the one place this module builds what
        ``dataClasses.Attachment`` would (see :func:`_external_file_url`):
        upstream's constructor cannot carry the file type, and without it the
        server cannot tell the paper from the answers.
        """
        period = next(
            (live for live in self._periods(client) if live.index == period_index),
            None,
        )
        if period is None:
            raise AttachmentUnavailable(name, 404)
        grades = self.marks(client, period, with_report=False).facts.grades
        grade = next(
            (candidate for candidate in grades if candidate.id == grade_id), None
        )
        # The role *and* the name, as this session reads them: a teacher who
        # replaces the answers replaces the file, and the key a card holds
        # names the file it was shown -- so that key now names nothing, rather
        # than opening a document the card never listed.
        if (
            grade is None
            or grade.ref is None
            or GradeDocument(name=name, role=role) not in grade.documents
        ):
            raise AttachmentUnavailable(name, 404)
        genres = next(
            genres for known, _key, genres in _GRADE_DOCUMENTS if known is role
        )
        calls = 1
        for genre in genres:
            calls += 1
            try:
                content, declared = _download(
                    client,
                    _external_file_url(client, ref=grade.ref, name=name, genre=genre),
                    name,
                )
            except AttachmentUnavailable as refusal:
                if refusal.status != 404:
                    raise
                continue
            if genre != genres[0]:
                _LOGGER.warning(
                    "A graded test's %s was served under the file type %s, not %s",
                    role,
                    genre,
                    genres[0],
                )
            return content, declared, calls
        _LOGGER.warning(
            "A graded test's %s was refused under every file type tried (%s)",
            role,
            ", ".join(genres),
        )
        raise AttachmentUnavailable(name, 404)

    def timetable_pdf_url(
        self,
        client: HardenedClient,
        day: dt.date | None,
        *,
        portrait: bool,
    ) -> tuple[str, int]:
        """Ask PRONOTE to render a timetable PDF and return its URL."""
        return client.generate_timetable_pdf(day=day, portrait=portrait), 1

    # -- writes ------------------------------------------------------------

    def set_homework_done(
        self, client: HardenedClient, homework_id: str, *, done: bool
    ) -> int:
        """Tick or untick one homework item, named by its minted key.

        The list is read before the post. PRONOTE's ``N`` is re-encrypted by
        every login, so the one a snapshot holds names nothing in a later
        session -- and the server answers an unknown ``N`` normally and records
        nothing. Measured: a tick sent at 22:59:58 with the ``N`` of a list
        read at 21:55, in a session opened at 22:57, was accepted, billed and
        read back unticked.
        So the list is read again here, in the session that posts, and the key
        is looked up in it; an item that is no longer there raises
        :class:`ItemNotFound` instead of reporting a success.

        Posted directly rather than through ``Homework.set_done`` so no
        ``pronotepy.Homework`` has to be kept alive across the DTO boundary --
        an object read after its session closed raises ``Erreur.G = 22``
        (§3.1).

        The line carries ``E`` (the entity state, ``2`` for a modification),
        which ``Homework.set_done`` omits. Without it the server answers
        normally and records nothing: measured on a live instance, two ticks
        were accepted, billed, and read back unticked by the next collection,
        with no error anywhere. Maintained clients of the same protocol send
        ``E: 2`` on this request; ``pronotepy`` 2.15.7 predates that.

        Three requests, then: the list is read a second time after the post,
        because the server's answer proves nothing. A parent session's tick is
        answered normally and recorded nowhere (measured on 2026-09-27), and
        the only evidence that a tick landed is the item read back with the
        requested state -- otherwise :class:`WriteNotApplied` is raised.
        """
        item = self._current_homework(client, homework_id)
        client.post(
            "SaisieTAFFaitEleve",
            88,
            {"listeTAF": [{"N": item.ref, "E": _ENTITY_MODIFIED, "TAFFait": done}]},
        )
        after = next(
            (
                candidate
                for candidate in self.homework(client).facts.homework
                if candidate.id == homework_id
            ),
            None,
        )
        if after is None or after.done != done:
            raise WriteNotApplied(homework_id)
        return 3

    def _current_homework(self, client: HardenedClient, homework_id: str) -> Homework:
        """Read the homework in this session and find one item by its key."""
        item = next(
            (
                candidate
                for candidate in self.homework(client).facts.homework
                if candidate.id == homework_id
            ),
            None,
        )
        if item is None:
            raise ItemNotFound(homework_id)
        return item

    def reply_to_discussion(
        self, client: HardenedClient, discussion_id: str, content: str
    ) -> int:
        """Reply to one thread, identified by its ``N``.

        Three requests, not one. The thread has to be re-listed
        (``ListeMessagerie``) because a ``pronotepy.Discussion`` carries the
        ``listePossessionsMessages`` the reply needs and must **not** be kept
        alive across the DTO boundary -- read after its session closed it
        raises ``Erreur.G = 22`` (§3.1). ``Discussion.reply`` then posts
        ``ListeMessages`` to find the message being answered, and
        ``SaisieMessage`` to send.

        Billed honestly at three, because a write that under-reports its cost
        corrupts the very budget that protects the account (annexe B §8).
        """
        visible = _visible_threads(client.discussions())
        thread = next(
            (
                candidate
                for key, candidate in zip(_thread_keys(visible), visible, strict=True)
                if key == discussion_id
            ),
            None,
        )
        if thread is None:
            raise DiscussionNotFound(discussion_id)
        if thread.closed:
            raise DiscussionIsClosed(discussion_id)
        thread.reply(content)
        return 3

    def start_discussion(
        self,
        client: HardenedClient,
        subject: str,
        content: str,
        recipient_names: Sequence[str],
    ) -> int:
        """Open a new thread with the named recipients.

        Recipients are matched on the name PRONOTE publishes, because that is
        the only handle a user can read off the interface -- the ``N``
        identifier is not shown anywhere. An unmatched name is refused rather
        than silently dropped: a message that quietly went to nobody is worse
        than an error.
        """
        available = client.get_recipients()
        wanted = [name.strip().casefold() for name in recipient_names]
        chosen = [
            recipient
            for recipient in available
            if (recipient.name or "").strip().casefold() in wanted
        ]
        matched = {(recipient.name or "").strip().casefold() for recipient in chosen}
        missing = sorted(set(wanted) - matched)
        if missing:
            raise RecipientNotFound(
                missing, sorted((recipient.name or "") for recipient in available)
            )
        client.new_discussion(subject, content, chosen)
        return 3

    def mark_information_read(self, client: HardenedClient, information_id: str) -> int:
        """Mark one news item as read, named by its minted key.

        Two requests: the news is read again in this session to find the item's
        current ``N``, for the reason :meth:`set_homework_done` gives.
        """
        item = next(
            (
                candidate
                for candidate in self.news(client).facts.information
                if candidate.id == information_id
            ),
            None,
        )
        if item is None:
            raise ItemNotFound(information_id)
        client.post(
            FUNC_NEWS_WRITE[0],
            FUNC_NEWS_WRITE[1],
            {
                "listeActualites": [
                    {
                        "N": item.ref,
                        "validationDirecte": True,
                        "genrePublic": 4,
                        "public": {"N": client.info.id},
                        "lue": True,
                        "estUnSondage": False,
                    }
                ]
            },
        )
        return 2


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def deduplicate_lessons(lessons: Iterable[Lesson]) -> tuple[Lesson, ...]:
    """Keep one entry per slot: the one with the largest ``num``.

    This is step one of the two-step timetable rule, and it does **not** belong
    to the delta detector -- without it ``sensor.<student>_cours_du_jour``
    overcounts every day a lesson was changed, the calendar draws overlapping
    events, and "next lesson" can latch onto a superseded entry (§2.2.1).

    Upstream says it plainly: *"for the same lesson time, the biggest num is the
    one shown on pronote"*. ``Client.lessons()`` does not filter.
    """
    best: dict[tuple[str, int], Lesson] = {}
    for lesson in lessons:
        key = lesson.slot_key
        current = best.get(key)
        if current is None or _supersedes(lesson, current):
            best[key] = lesson
    return tuple(sorted(best.values(), key=lambda lesson: (lesson.start, lesson.place)))


def _supersedes(candidate: Lesson, current: Lesson) -> bool:
    """Whether ``candidate`` should replace ``current`` for the same slot.

    ``num`` decides, per upstream's rule. A **tie** needs an answer of its own,
    and response order is not one: ``num`` is the ``P`` field, which defaults to
    0 when absent, so two content-less entries on the same slot -- an outing and
    a detention, say -- both scored 0 and the second was dropped in silence,
    taking ``binary_sensor.<eleve>_sortie_pedagogique`` and one calendar event
    with it.

    On a tie the entry that is *not* cancelled wins, because a replacement is
    what PRONOTE displays; failing that, the one that names a subject, which is
    the more informative of two otherwise indistinguishable entries.
    """
    if candidate.num != current.num:
        return candidate.num > current.num
    if candidate.canceled != current.canceled:
        return current.canceled
    return current.subject is None and candidate.subject is not None


def _food_names(foods: Sequence[Any] | None) -> tuple[str, ...]:
    """Names of the dishes in one course, or an empty tuple."""
    if not foods:
        return ()
    return tuple(str(food.name) for food in foods if getattr(food, "name", None))


def _join_phone(prefix: Any, number: Any) -> str | None:
    """Assemble ``+<country><number>``, tolerating either part missing.

    Upstream returns ``"+" + indicatifTel + telephonePortable`` unguarded, so a
    guardian record with no country code -- which is common -- came out as
    ``"+600000000"``: a number that looks international and is not, and that no
    dialler will accept. With no prefix the number is handed back as it stands.
    """
    if not number:
        return None
    if not prefix:
        return str(number)
    return f"+{prefix}{number}"


def _parse_datetime(raw: Any) -> dt.datetime | None:
    """Parse a PRONOTE datetime string without raising.

    ``Util.datetime_parse`` raises ``DateParsingError`` on an unknown form, and
    a single unparseable field must not fail a whole tier (§3.3.3).
    """
    if raw is None:
        return None
    try:
        return dataClasses.Util.datetime_parse(str(raw))
    except Exception:  # noqa: BLE001 -- upstream raises several unrelated types
        _LOGGER.debug("could not parse datetime %r", raw)
        return None


def _parse_date(raw: Any) -> dt.date | None:
    """Parse a PRONOTE date string without raising.

    Worth knowing what is being tolerated: two of ``Util.date_parse``'s six
    accepted forms complete the missing part with ``date.today()``, read in the
    *host's* timezone. Those forms appear on short fields, and the values they
    produce are only ever used as calendar dates, never compared to an instant.
    """
    if raw is None:
        return None
    try:
        return dataClasses.Util.date_parse(str(raw))
    except Exception:  # noqa: BLE001 -- upstream raises several unrelated types
        _LOGGER.debug("could not parse date %r", raw)
        return None
