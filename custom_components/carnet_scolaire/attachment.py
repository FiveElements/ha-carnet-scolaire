"""Serving a school document without ever publishing PRONOTE's address.

Two kinds of document go through here: a file joined to a homework entry, and
the paper or the answers joined to a graded test (`GradeDocument`). Everything
below is written about the first and holds for the second; where they differ
-- how one is found in a snapshot, how it is fetched -- the function says so.

A homework attachment comes in two kinds and only one of them has an address
that means anything outside the session that fetched it (see
:class:`.models.HomeworkAttachment`). A **link** is published as-is. A **file**
is the reason this module exists.

PRONOTE's address for a file is
``FichiersExternes/<hex>/<name>?Session=<h>``, where the hex segment is the
document's identifier encrypted with the session's own AES key and IV. Two
consequences decide the whole design: it stops working when the session does,
and while it works it opens the document with no credentials at all. So it must
never reach a state, an attribute, the recorder or a diagnostics download --
the rule §8.2 established for the iCal URL.

What a dashboard gets instead is an address **Home Assistant** serves: a
signed, expiring path to the view below, which fetches the bytes through the
ordinary chokepoint -- one session, one lock, charged to the limiter -- and
relays them. That address is **not** in any attribute. The state carries a
``key`` naming the document, and ``carnet_scolaire.get_attachment_url`` mints the
signed path at the instant somebody clicks. A signed path is a bearer token,
and an attribute is the most widely readable surface Home Assistant has: every
account reads `/api/states`, automation traces keep the triggering state in
`.storage`, and the more-info dialog displays it.

Relayed and not redirected, deliberately. A 302 would put PRONOTE's address in
the browser's address bar, its history, the network tab and the log of any
proxy on the way, which is precisely the credential this module exists not to
publish. Nothing in the response body or headers carries it.

Three properties of the path are load-bearing:

**It is rooted, not absolute.** That is what :func:`async_sign_path` produces,
and it is also the only correct form: an absolute URL would make the
integration guess which host the browser arrived through, and ``get_url`` hands
the internal address to somebody connected from outside.

**It names a fingerprint, not an identifier.** A real PRONOTE identifier
carries a ``#`` -- a fragment delimiter, which would truncate the path -- and
writing one into an attribute puts it in the recorder. The fingerprint is the
same idiom ``diagnostics.py`` uses for child identifiers. It also makes the
address name a *document* rather than a position, so re-ordering between two
collections cannot serve the wrong file.

**It expires, in minutes.** Minted on click and opened at once, it has no
reason to outlive the click, so a copied address -- a link shared by mistake,
a screenshot of the browser bar -- is dead before it can be reused.
"""

from __future__ import annotations

from datetime import timedelta
import hashlib
import hmac
from typing import TYPE_CHECKING, Any, Final

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.components.http.auth import async_sign_path
from homeassistant.helpers.http import KEY_HASS

from .const import DOMAIN, AttachmentKind, Priority, Tier
from .gateway import AttachmentUnavailable
from .ratelimit import TierDeferred

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from homeassistant.core import HomeAssistant

    from .account import PronoteAccount
    from .models import (
        GradeDocument,
        HistoryFacts,
        HomeworkAttachment,
        HomeworkFacts,
        MarksFacts,
    )

#: How long a minted address stays usable.
#:
#: Five minutes. It used to be twelve hours, and that figure was derived from a
#: constraint that no longer exists: the address sat in an attribute, so it had
#: to outlive the homework tier's interval or a parent would meet a 401 on a
#: link the page was still showing. Minted by the service at the click and
#: opened immediately, it only has to survive the round trip and a slow PDF
#: viewer asking for a second range -- and every minute beyond that is a minute
#: a copied address still opens a child's document.
SIGNATURE_LIFETIME: Final = timedelta(minutes=5)

#: Characters of the digest kept in the path.
#:
#: Sixteen hex characters, 64 bits. This is not a secret -- the signature is
#: what authorises the request -- so its only job is to name one document
#: without colliding with another on the same account. At a few dozen
#: attachments a collision is not a rounding error away from impossible, it is
#: impossible in practice, and a longer digest would only make the address
#: harder to read in a log.
_FINGERPRINT_LENGTH: Final = 16

#: What a well-formed fingerprint looks like, for the service schema. Derived
#: from the length rather than written twice.
FINGERPRINT_PATTERN: Final = rf"^[0-9a-f]{{{_FINGERPRINT_LENGTH}}}$"

#: How many documents' bytes are kept in memory, per account.
#:
#: The cache is what makes the cost of this feature bounded rather than
#: per-click: a browser prefetcher, a link scanner on the page, or a parent
#: opening the same exercise twice costs one request per document per Home
#: Assistant run, not one per fetch. Sixteen is above the number of documents
#: a fortnight of homework carries -- twelve, measured -- so in practice
#: nothing is evicted before a reload clears it anyway.
_CACHE_ENTRIES: Final = 16

#: Refused rather than guessed. The browser is told what PRONOTE declared, and
#: PRONOTE declares nothing useful for some documents, so a fallback is needed
#: -- but it must not be a type the browser will *execute* or render as a
#: document in our own origin. ``application/octet-stream`` makes an unknown
#: document download instead of running.
_FALLBACK_CONTENT_TYPE: Final = "application/octet-stream"

#: Content types the browser is allowed to be told, when PRONOTE declares one.
#:
#: A whitelist, because the response is served from Home Assistant's **own
#: origin**: a document echoed back as ``text/html`` would run its own script
#: with the user's session, which is a stored cross-site scripting route
#: through a teacher's file upload. PDFs and images are what exercise sheets
#: actually are, and everything else downloads.
_SAFE_CONTENT_TYPES: Final = frozenset(
    {
        "application/pdf",
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
        "text/plain",
    }
)

#: Where the per-entry byte caches live.
_CACHE_KEY: Final = f"{DOMAIN}_attachment_cache"


def fingerprint(homework_id: str, attachment_id: str) -> str:
    """Name one document in a URL, without naming it in the recorder.

    A digest of the pair rather than of the attachment alone: PRONOTE's
    identifiers are unique per account in practice, but the pair is what the
    lookup actually resolves, and a digest that matched two homework entries
    would serve one child's document from the other's row.

    A graded test's document is named by its grade's key and its role
    (`grade_fingerprint`): it has no identifier of its own.
    """
    digest = hashlib.sha256(f"{homework_id}\x00{attachment_id}".encode())
    return digest.hexdigest()[:_FINGERPRINT_LENGTH]


def grade_fingerprint(grade_id: str, role: str) -> str:
    """Name a graded test's paper or answers: the grade's key and the role."""
    return fingerprint(grade_id, role)


def signed_path(hass: HomeAssistant, entry_id: str, print_: str) -> str:
    """The address the service returns for one document.

    Local: `async_sign_path` builds a JWT from a key Home Assistant already
    holds. No request leaves this process.
    """
    return async_sign_path(
        hass,
        f"/api/{DOMAIN}/attachment/{entry_id}/{print_}",
        SIGNATURE_LIFETIME,
    )


def async_register_view(hass: HomeAssistant) -> None:
    """Register the route, once, independently of any account.

    Called from ``async_setup`` and not from ``async_setup_entry``, for the same
    reason the actions are registered there: a route is per Home Assistant, not
    per account, and registering it once per entry would re-register the same
    path -- at best redundantly, at worst replacing a live handler mid-request.
    The view resolves its account from the entry id in the path at call time, so
    it needs nothing from set-up.
    """
    hass.http.register_view(PronoteAttachmentView())


class _ByteCache:
    """The fetched documents, newest last, oldest evicted.

    Deliberately not an ``lru_cache``: the entries are megabytes and their
    lifetime is a config entry's, so eviction has to be observable and the
    whole thing has to be droppable on unload.
    """

    def __init__(self) -> None:
        self._entries: dict[str, tuple[bytes, str]] = {}

    def get(self, key: str) -> tuple[bytes, str] | None:
        return self._entries.get(key)

    def put(self, key: str, value: tuple[bytes, str]) -> None:
        self._entries[key] = value
        while len(self._entries) > _CACHE_ENTRIES:
            self._entries.pop(next(iter(self._entries)))


def cache_for(hass: HomeAssistant, entry_id: str) -> _ByteCache:
    """The byte cache of one account, created on first use.

    Held in ``hass.data`` under the entry rather than on the account object,
    for the reason `login_guard` holds the login counters there: an account is
    rebuilt on every reload and a retried set-up would otherwise drop a cache
    that is still perfectly valid -- the documents have not changed because
    Home Assistant reloaded.
    """
    caches: dict[str, _ByteCache] = hass.data.setdefault(_CACHE_KEY, {})
    if entry_id not in caches:
        caches[entry_id] = _ByteCache()
    return caches[entry_id]


def _locate(
    account: PronoteAccount, print_: str, *, student_id: str | None = None
) -> tuple[str, str, HomeworkAttachment] | None:
    """Find the *file* one fingerprint names: its child, its homework, itself.

    Scanned rather than addressed: the snapshot is the authority on what this
    account may fetch, so a fingerprint that matches nothing in it is refused
    without ever reaching PRONOTE. An address for a document that has since
    left the horizon therefore stops working, which is the correct answer --
    the integration cannot vouch for what it no longer holds.

    Only ``FILE`` matches, and that is the whole defence of the relay rather
    than a filter: the view and the service both trust what this returns, so a
    link -- whose bytes are a third party's page -- and an ``OPAQUE``
    attachment are unreachable here by construction, not by a check somebody
    could forget downstream.

    ``student_id`` scopes the scan to one child, and the service always passes
    it. Without it, a parent's card could present one child's key with the
    other child's device and be handed a working address. The view does not
    pass it: its path carries no student, and it does not need one -- it only
    ever serves an address the service already scoped when it minted it.
    """
    students = (
        [student for student in account.students if student.id == student_id]
        if student_id is not None
        else account.students
    )
    for student in students:
        snapshot = account.snapshot(Tier.HOMEWORK, student.id)
        if snapshot is None:
            continue
        facts: HomeworkFacts = snapshot.data
        for item in facts.homework:
            for attachment in item.attachments:
                if attachment.kind is not AttachmentKind.FILE:
                    continue
                if hmac.compare_digest(fingerprint(item.id, attachment.id), print_):
                    return student.id, item.id, attachment
    return None


def _locate_grade_document(
    account: PronoteAccount, print_: str, *, student_id: str | None = None
) -> tuple[str, int, str, GradeDocument] | None:
    """Find the graded-test document one fingerprint names.

    Its child, the period its grade belongs to -- by position, the handle on a
    period that survives a login -- its grade and itself. Scanned in the
    current period's marks and in the closed periods', since a card shows
    both. ``student_id`` scopes the scan exactly as it does in `_locate`.
    """
    students = (
        [student for student in account.students if student.id == student_id]
        if student_id is not None
        else account.students
    )
    for student in students:
        periods: list[MarksFacts] = []
        if (current := account.snapshot(Tier.MARKS, student.id)) is not None:
            periods.append(current.data)
        if (closed := account.snapshot(Tier.HISTORY, student.id)) is not None:
            history: HistoryFacts = closed.data
            periods.extend(history.marks)
        for marks in periods:
            for grade in marks.grades:
                for document in grade.documents:
                    if hmac.compare_digest(
                        grade_fingerprint(grade.id, document.role), print_
                    ):
                        return student.id, marks.period_index, grade.id, document
    return None


def resolve(
    account: PronoteAccount, print_: str, *, student_id: str | None = None
) -> tuple[str, Callable[[], Awaitable[tuple[bytes, str]]]] | None:
    """The document one fingerprint names, and how to fetch it -- or ``None``.

    The single answer the view and the service both trust: a name to show and
    a download that has not run yet. Homework first, then grades; the two
    fingerprints are digests of different pairs, so one cannot shadow the
    other.
    """
    if (located := _locate(account, print_, student_id=student_id)) is not None:
        owner, homework_id, attachment = located
        if attachment.kind is not AttachmentKind.FILE:
            # `_locate` returns files only, so this is unreachable today -- and
            # it stays, because it is the relay's own refusal and not a
            # property of whatever hands it an attachment. A link is a third
            # party's page; relaying it would fetch an unrelated site with the
            # school's session and make Home Assistant an open proxy for
            # anything a teacher pastes.
            return None
        return attachment.name, lambda: _fetch(account, attachment, homework_id, owner)
    found = _locate_grade_document(account, print_, student_id=student_id)
    if found is not None:
        owner, period_index, grade_id, document = found
        return document.name, lambda: _fetch_grade_document(
            account, document, period_index, grade_id, owner
        )
    return None


class PronoteAttachmentView(HomeAssistantView):
    """Relay one homework or graded-test document.

    ``requires_auth`` stays at its default -- ``True`` -- and that is what makes
    a plain ``<a href>`` work from a dashboard: a request carrying a valid
    ``authSig`` satisfies Home Assistant's auth middleware without an
    ``Authorization`` header, which is the mechanism ``entity_picture`` uses for
    camera and image entities. An unsigned guess at this URL gets 401 before
    any of the code below runs, so nothing here can be made to spend a request
    by somebody who cannot already see the dashboard.
    """

    url = f"/api/{DOMAIN}/attachment/{{entry_id}}/{{print_}}"
    name = f"api:{DOMAIN}:attachment"

    async def get(
        self, request: web.Request, entry_id: str, print_: str
    ) -> web.StreamResponse:
        """Answer with the document's bytes, or with a status that says why not."""
        hass = request.app[KEY_HASS]
        account = _account(hass, entry_id)
        if account is None:
            return web.Response(status=404, text="unknown or unloaded account")

        resolved = resolve(account, print_)
        if resolved is None:
            return web.Response(status=404, text="no such document on this account")
        name, download = resolved

        cache = cache_for(hass, entry_id)
        if (cached := cache.get(print_)) is None:
            try:
                cached = await download()
            except TierDeferred as deferred:
                # The limiter said "later", not "never", so 503 with a
                # `Retry-After` a browser understands -- and with the limiter's
                # own figure rather than a guess. The distinction is not
                # cosmetic: a refusal during quiet hours is hours away, not a
                # minute, and a hard-coded sixty seconds would invite a client
                # to retry all night against a school server.
                return web.Response(
                    status=503,
                    headers={"Retry-After": str(max(1, int(deferred.retry_after)))},
                    text=f"deferred by the rate limiter: {deferred.reason}",
                )
            except AttachmentUnavailable as error:
                return web.Response(status=502, text=str(error))
            cache.put(print_, cached)

        content, content_type = cached
        return web.Response(
            body=content,
            content_type=content_type,
            headers={
                # `inline` so an exercise sheet opens in the browser's own
                # viewer rather than landing in the downloads folder, but with
                # the real filename so saving it keeps a usable name.
                "Content-Disposition": f'inline; filename="{_ascii(name)}"',
                # The bytes never change for a given fingerprint -- it names a
                # document, not a position -- and the signature is what bounds
                # access, so letting the browser keep them saves re-serving the
                # same megabytes on every render.
                "Cache-Control": "private, max-age=3600",
                # The whitelist above is the defence; these stop a browser from
                # second-guessing it. Without `nosniff` a document declared
                # `text/plain` can be sniffed as HTML and run in Home
                # Assistant's origin. And the relay's own address -- signed --
                # must not travel to a third party as a `Referer` when a
                # document links out.
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
            },
        )


async def _fetch(
    account: PronoteAccount,
    attachment: HomeworkAttachment,
    homework_id: str,
    student_id: str,
) -> tuple[bytes, str]:
    """Download one document through the one path to the network.

    Two requests: the gateway reads the homework again in this session to find
    the document's current ``N`` (see ``PronoteGateway.homework_attachment``).
    """
    extras = account.extras
    if extras is None:
        raise AttachmentUnavailable(attachment.name, 501)

    def work(client: Any) -> tuple[bytes, str | None, int]:
        return extras.gateway.homework_attachment(
            client,
            homework_id=homework_id,
            attachment_id=attachment.id,
            name=attachment.name,
        )

    content, declared, _cost = await extras.session.run(
        str(Tier.HOMEWORK),
        # The priority every other human-initiated call in this integration
        # uses (`services.py`, the to-do tick, a refresh button): somebody is
        # looking at the screen. It crosses quiet hours -- a parent checking
        # their child's homework at 22:30 is not the automatic collection those
        # hours protect the address against -- and nothing else: the daily
        # cap, the hold, the spacing and the bucket still apply, and the cache
        # above means a document already opened does not go back out.
        Priority.GESTURE,
        work,
        student_id=student_id,
        cost=2,
    )
    return content, _content_type(declared)


async def _fetch_grade_document(
    account: PronoteAccount,
    document: GradeDocument,
    period_index: int,
    grade_id: str,
    student_id: str,
) -> tuple[bytes, str]:
    """Download a graded test's document through the one path to the network.

    Two requests, charged to the marks tier: the grades of its period are read
    again in this session (see ``PronoteGateway.grade_document``). The same
    ``GESTURE`` priority as a homework document, for the same reason.
    """
    extras = account.extras
    if extras is None:
        raise AttachmentUnavailable(document.name, 501)

    def work(client: Any) -> tuple[bytes, str | None, int]:
        return extras.gateway.grade_document(
            client,
            period_index=period_index,
            grade_id=grade_id,
            role=document.role,
            name=document.name,
        )

    content, declared, _cost = await extras.session.run(
        str(Tier.MARKS),
        Priority.GESTURE,
        work,
        student_id=student_id,
        cost=2,
    )
    return content, _content_type(declared)


def _content_type(declared: str | None) -> str:
    """What the browser is told, which is not always what PRONOTE said."""
    if declared is None:
        return _FALLBACK_CONTENT_TYPE
    # Parameters dropped before the comparison: PRONOTE sends
    # `text/plain; charset=...`, and a whitelist that matched the whole header
    # would fall through to the fallback for a document it should have allowed.
    base = declared.split(";", 1)[0].strip().lower()
    if base in _SAFE_CONTENT_TYPES:
        return base
    return _FALLBACK_CONTENT_TYPE


def _ascii(name: str) -> str:
    """A filename safe to put in a header.

    Header values are latin-1 on the wire and a quote would end the parameter
    early, so an accented or quoted document name is reduced rather than
    trusted. The name in the attribute stays the real one -- this is only what
    the browser is told to call the file it saves.
    """
    cleaned = name.replace('"', "").replace("\\", "")
    return cleaned.encode("ascii", "replace").decode("ascii") or "document"


def _account(hass: HomeAssistant, entry_id: str) -> PronoteAccount | None:
    """The loaded account for one entry, or ``None``."""
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None or entry.domain != DOMAIN:
        return None
    return getattr(entry, "runtime_data", None)
