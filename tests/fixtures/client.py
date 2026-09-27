"""A fake pronotepy client: canned responses in, recorded posts out.

Deliberately **not** a ``MagicMock``. Two of the defects this suite exists to
prevent are invisible to a mock:

* ``identity()`` must post through ``communication.post`` with an explicit
  ``ressource``, not through ``client.post`` -- which stamps the account
  holder as ``membre`` and, on a parent account, returns the *parent's* birth
  date and INE number under the child's device. A mock accepts both calls
  identically; this class records which one was used and with what signature.
* the cost each tier declares to the limiter must match the number of requests
  it actually places. ``posts`` is the ground truth for that, and
  ``test_tiers.py`` compares it against ``GatewayResult.calls``.

Everything is served from :mod:`tests.fixtures.protocol`, so a test that wants
a degenerate response overrides one function name and leaves the rest alone.
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING, Any

from . import protocol

if TYPE_CHECKING:
    from collections.abc import Sequence


class FakeAttachment:
    """Stands in for ``pronotepy.Attachment``.

    ``url`` is present and is a *stub*, because the whole point of the
    ``attachments`` field holding names is that nothing downstream may read a
    URL. A test that starts passing after touching this attribute has broken
    that rule.
    """

    def __init__(self, name: str, payload: bytes = b"not-a-real-image") -> None:
        self.name = name
        self._payload = payload
        self.reads = 0

    @property
    def url(self) -> str:
        """A stub URL. The real one interpolates the live session token."""
        return "https://demo.example.invalid/attachment?Session=STUB"

    @property
    def data(self) -> bytes:
        """The bytes, counting reads so a caller cannot fetch twice unnoticed."""
        self.reads += 1
        return self._payload


class FakeInfo:
    """Stands in for ``pronotepy.ClientInfo``.

    The lazy, network-touching properties -- ``address``, ``email``, ``phone``,
    ``ine_number`` -- are deliberately **absent**. If a future change reads one
    of them from an entity, this class raises ``AttributeError`` rather than
    quietly performing an unbudgeted, possibly misattributed request.
    """

    def __init__(self, resource: dict[str, Any], *, photo: bool = True) -> None:
        self.raw_resource = resource
        self._photo = FakeAttachment("photo.jpg") if photo else None

    @property
    def id(self) -> str:
        """The resource identifier."""
        return str(self.raw_resource["N"])

    @property
    def name(self) -> str:
        """The student's display name."""
        return str(self.raw_resource["L"])

    @property
    def class_name(self) -> str:
        """The class label, empty when absent."""
        return str(self.raw_resource.get("classeDEleve", {}).get("L", ""))

    @property
    def establishment(self) -> str:
        """The establishment label, empty when absent."""
        return str(self.raw_resource.get("Etablissement", {"V": {"L": ""}})["V"]["L"])

    @property
    def profile_picture(self) -> FakeAttachment | None:
        """The photo attachment, or ``None`` when the account has no photo."""
        if not self.raw_resource.get("avecPhoto"):
            return None
        return self._photo


class FakeEncryption:
    """Stands in for ``pronotepy.pronoteAPI._Encryption``.

    Only ``aes_encrypt`` is modelled, and it is modelled as a *keyed* function
    rather than as a constant: the point of the real one is that the ciphertext
    depends on the session's key and IV, so a fake that returned a fixed string
    would let a test pass while the code published a session-independent
    address -- which is the whole hazard.
    """

    def __init__(self, key: bytes = b"session-key") -> None:
        self.key = key
        #: Everything encrypted, in order -- so a test can say *which* ``N`` an
        #: address was built from, which the digest alone cannot show.
        self.plaintexts: list[bytes] = []

    def aes_encrypt(self, data: bytes) -> bytes:
        """A reversible stand-in, not a cipher. Keyed, and that is what counts."""
        import hashlib

        self.plaintexts.append(data)
        return hashlib.sha256(self.key + data).digest()


class FakeResponse:
    """One ``requests`` response, as far as the gateway reads one."""

    def __init__(
        self,
        content: bytes = b"%PDF-1.4 not a real document",
        status_code: int = 200,
        content_type: str | None = "application/pdf",
    ) -> None:
        self.content = content
        self.status_code = status_code
        self.headers: dict[str, str] = (
            {} if content_type is None else {"content-type": content_type}
        )


class FakeHttpSession:
    """The ``requests.Session`` upstream downloads an attachment through.

    ``Attachment.data`` does ``communication.session.get(self.url)``, which is
    the one place in the library that fetches bytes outside
    ``ClientBase.post`` -- so it is outside the request accounting unless the
    caller declares it. Every GET is recorded here so a test can assert both
    the count and the address.
    """

    def __init__(self) -> None:
        self.gets: list[str] = []
        self.response = FakeResponse()

    def get(self, url: str) -> FakeResponse:
        self.gets.append(url)
        return self.response


class FakeCommunication:
    """Stands in for ``pronotepy._Communication``."""

    def __init__(self, owner: FakeClient) -> None:
        self._owner = owner
        #: The three things ``dataClasses.Attachment`` reads to build a file's
        #: address. Present so the tests exercise *upstream's* construction
        #: rather than a re-implementation of it.
        self.encryption = FakeEncryption()
        self.root_site = "https://demo.example.invalid/pronote"
        self.session = FakeHttpSession()

    def post(self, name: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        """Record a direct post and answer from the canned table."""
        self._owner.communication_posts.append((name, data))
        return self._owner._answer(name)


class FakeThread:
    """Stands in for ``pronotepy.Discussion``.

    ``messages`` is a **property** that counts its reads, because that is the
    real behaviour that drives the whole discussions budget: upstream re-posts
    ``ListeMessages`` on every access, so a caller reading it in a loop costs
    one request per iteration. A plain attribute would hide the defect.
    """

    def __init__(
        self,
        identifier: str,
        *,
        subject: str = "Sortie du 20 mars",
        creator: str = "Direction",
        unread: int = 0,
        closed: bool = False,
        labels: Sequence[str] = (),
        messages: Sequence[FakeMessage] = (),
    ) -> None:
        self.id = identifier
        self.subject = subject
        self.creator = creator
        self.unread = unread
        self.closed = closed
        self.labels = list(labels)
        self._messages = list(messages)
        self.message_reads = 0
        self.replies: list[str] = []

    @property
    def messages(self) -> list[FakeMessage]:
        """The thread's messages, counting the request each read costs."""
        self.message_reads += 1
        return list(self._messages)

    def reply(self, content: str) -> None:
        """Record a reply."""
        self.replies.append(content)


class FakeMessage:
    """Stands in for ``pronotepy.Message``."""

    def __init__(
        self,
        identifier: str,
        *,
        author: str = "Direction",
        created: dt.datetime | None = None,
        content: str | None = "Le départ est à 8h.",
    ) -> None:
        self.id = identifier
        self.author = author
        self.created = created or dt.datetime(2026, 3, 11, 10, 0)  # noqa: DTZ001 -- pronotepy hands back naive local times
        self.content = content


class FakeFood:
    """Stands in for ``pronotepy.Menu.Food``."""

    def __init__(self, name: str) -> None:
        self.name = name


class FakeMenu:
    """Stands in for ``pronotepy.Menu``."""

    def __init__(
        self,
        identifier: str,
        day: dt.date,
        *,
        is_lunch: bool = True,
        is_dinner: bool = False,
    ) -> None:
        self.id = identifier
        self.date = day
        self.name = "Menu du jour"
        self.is_lunch = is_lunch
        self.is_dinner = is_dinner
        self.first_meal = [FakeFood("Carottes râpées")]
        self.main_meal = [FakeFood("Poisson pané")]
        self.side_meal = [FakeFood("Riz")]
        self.other_meal = None
        self.cheese = [FakeFood("Yaourt")]
        self.dessert = [FakeFood("Pomme")]


class FakeRecipient:
    """Stands in for ``pronotepy.Recipient``."""

    def __init__(self, name: str) -> None:
        self.name = name


class FakeClient:
    """A pronotepy client that never touches a socket.

    ``responses`` maps a function name (``"PageEmploiDuTemps"``) to either a
    payload or a callable taking the posted body. A test overrides one entry to
    make a single tier degenerate and leaves every other tier working, which is
    what a per-tier tolerance test needs.
    """

    def __init__(
        self,
        *,
        student_id: str = protocol.STUDENT_ID,
        has_photo: bool = True,
        marks_tab: bool = True,
        first_monday: dt.date = dt.date(2025, 9, 1),
        last_date: dt.date | None = dt.date(2026, 7, 4),
        children: Sequence[tuple[str, str]] = (),
        menus_published: bool = True,
    ) -> None:
        self.func_options = protocol.func_options(
            first_monday=first_monday, last_date=last_date
        )
        self.parametres_utilisateur = protocol.parametres_utilisateur(
            student_id=student_id, has_photo=has_photo, marks_tab=marks_tab
        )
        self.info = FakeInfo(
            self.parametres_utilisateur["dataSec"]["data"]["ressource"],
            photo=has_photo,
        )

        # -- the session's surface, not the gateway's ----------------------
        #
        # A parent account differs from a student account in one respect that
        # matters here: `client.info` follows `set_child`, so the gateway reads
        # whichever child was selected last. Modelling that rather than
        # returning one fixed resource is what lets a two-child test catch a
        # tier that forgets to select.
        self.logged_in = True
        self.closed = False
        self.credential_exports = 0
        #: Every ``set_child`` argument, in order.
        self.child_selections: list[str] = []
        #: Selections *and* server calls in one sequence, which is the only way
        #: their order is observable. Two separate ledgers can both look right
        #: while the pairing between them is crossed -- and a crossed pairing is
        #: exactly the defect worth testing for, because upstream's
        #: ``ParentClient.__init__`` sets ``_selected_child = self.children[0]``
        #: and a call made without a selection therefore answers for the first
        #: child instead of failing.
        self.journal: list[tuple[str, str]] = []
        self._child_payloads: dict[str, dict[str, Any]] = {
            child_id: protocol.parametres_utilisateur(
                student_id=child_id,
                name=child_name,
                has_photo=has_photo,
                marks_tab=marks_tab,
            )
            for child_id, child_name in children
        }
        self._children = tuple(
            FakeInfo(payload["dataSec"]["data"]["ressource"], photo=has_photo)
            for payload in self._child_payloads.values()
        )
        self._selected_child_id: str | None = None
        if self._children:
            self.set_child(str(self._children[0].id))
        self.communication = FakeCommunication(self)
        #: ``Attachment`` interpolates ``client.attributes["h"]`` -- the session
        #: number -- into the address it builds, so it has to be here for
        #: upstream's own code to run at all.
        self.attributes: dict[str, Any] = {"h": "SESSION-NUMBER"}
        self.start_day = first_monday
        #: Whether the canteen publishes anything. ``False`` is a real and
        #: common configuration -- a school with no canteen, or a holiday week
        #: -- and it is *not* an error: the request succeeds and answers with
        #: an empty week. Modelled as a flag rather than by overriding `menus`
        #: in a test, so the request is still recorded and the tier still
        #: costs what it declares.
        self.menus_published = menus_published

        #: ``(function name, tab, body)`` for every ``client.post``.
        self.posts: list[tuple[str, int, Any]] = []
        #: ``(function name, body)`` for every direct ``communication.post``.
        self.communication_posts: list[tuple[str, Any]] = []

        self.responses: dict[str, Any] = {
            "PageEmploiDuTemps": protocol.timetable_response([protocol.lesson()]),
            "PageCahierDeTexte": protocol.homework_response([protocol.homework()]),
            "PageActualites": protocol.news_response([protocol.information()]),
            "DernieresNotes": protocol.marks_response(),
            "PageBulletins": protocol.report_response(),
            "PagePresence": protocol.attendance_response(
                [protocol.absence(), protocol.delay(), protocol.punishment()]
            ),
            "DernieresEvaluations": protocol.evaluations_response(
                [protocol.evaluation()]
            ),
            "PageInfosPerso": protocol.personal_info_response(),
            "PageEquipePedagogique": protocol.teaching_staff_response(
                [protocol.teaching_staff()]
            ),
            "SaisieTAFFaitEleve": {"dataSec": {"data": {}}},
            "SaisieActualites": {"dataSec": {"data": {}}},
        }

        self.threads: list[FakeThread] = []
        self.recipients: list[FakeRecipient] = [FakeRecipient("Prof. Un")]
        self.new_discussions: list[tuple[str, str, list[str]]] = []
        self.ical_calls = 0
        self.pdf_calls = 0

    # -- the session's surface --------------------------------------------

    def enrol_child(self, child_id: str, child_name: str) -> None:
        """Add a child to the account, as an establishment does mid-year.

        Places **no** request, and that is the point rather than a shortcut:
        the roster arrives inside the payload of `Authentification`, so the
        real client learns a new child at its next login and never by asking.
        A helper that charged one here would let a test pass against an
        implementation that polls -- the one behaviour the limiter forbids.
        """
        self._child_payloads[child_id] = protocol.parametres_utilisateur(
            student_id=child_id,
            name=child_name,
            has_photo=False,
        )
        self._children = tuple(
            FakeInfo(payload["dataSec"]["data"]["ressource"], photo=False)
            for payload in self._child_payloads.values()
        )

    def rotate_identifiers(self) -> None:
        """Re-announce the same children under fresh resource identifiers.

        This is what PRONOTE actually does between sessions -- the signature
        in `46#<signature>` changed three times in one day for one pupil --
        and it is the single event a newcomer check must not mistake for an
        arrival. Places no request, for the same reason `enrol_child` does
        not: the roster comes with the handshake.
        """
        rotated: dict[str, dict[str, Any]] = {}
        for index, (child_id, payload) in enumerate(self._child_payloads.items(), 1):
            resource = payload["dataSec"]["data"]["ressource"]
            fresh = f"{child_id}#rotation-{index}"
            resource["N"] = fresh
            rotated[fresh] = payload
        self._child_payloads = rotated
        self._children = tuple(
            FakeInfo(payload["dataSec"]["data"]["ressource"], photo=False)
            for payload in self._child_payloads.values()
        )
        if self._children:
            self.set_child(str(self._children[0].id))

    @property
    def is_parent_account(self) -> bool:
        """Whether this account holds children rather than being one."""
        return bool(self._children)

    @property
    def children(self) -> tuple[FakeInfo, ...]:
        """The children on the account, in the order the server listed them."""
        return self._children

    @property
    def selected_child_id(self) -> str | None:
        """Which child the next call reads, or ``None`` on a student login."""
        return self._selected_child_id

    def set_child(self, child: FakeInfo | str) -> None:
        """Point the client at one child, as the hardened client does.

        ``info`` and ``parametres_utilisateur`` both move, because the gateway
        reads both -- and a fake where only one of them moved would make a tier
        that selects the wrong child look correct.
        """
        child_id = str(child if isinstance(child, str) else child.id)
        if child_id not in self._child_payloads:
            message = f"no child {child_id!r} on this account"
            raise KeyError(message)
        self.child_selections.append(child_id)
        self.journal.append(("select", child_id))
        self._selected_child_id = child_id
        self.parametres_utilisateur = self._child_payloads[child_id]
        self.info = next(info for info in self._children if str(info.id) == child_id)

    def export_credentials(self) -> dict[str, Any]:
        """What a real login hands back, token rotation included.

        The password is a rotated *stub*: in token mode PRONOTE returns a fresh
        ``jetonConnexionAppliMobile`` at every ``Authentification``, and the
        rotation is what the persistence callback exists for. No real token has
        ever been near this file (see the package docstring).
        """
        self.credential_exports += 1
        return {
            "username": "parent.test",
            "password": "rotated-token-not-a-real-one",
            "client_identifier": "CLIENT-ID-STUB",
            "uuid": "UUID-STUB",
        }

    def close(self) -> None:
        """Release the client. Idempotent, as the real one has to be."""
        self.closed = True

    # -- the protocol seam -------------------------------------------------

    def _answer(self, name: str, body: Any = None) -> dict[str, Any]:
        """Look up a canned response, calling it when it is a callable."""
        try:
            answer = self.responses[name]
        except KeyError:  # pragma: no cover -- a test asked for an unstubbed tab
            raise AssertionError(
                f"the fake client has no response for {name!r}; add one to "
                f"`FakeClient.responses` rather than reaching for a mock"
            ) from None
        if callable(answer):
            return dict(answer(body))
        return answer

    def _record(self, name: str, tab: int, body: Any = None) -> None:
        """The single choke point for "this touched the server".

        Every path that would place a request goes through here, so a helper
        added later cannot reach the server without appearing in the journal --
        which is what keeps the ordering assertions honest as the fake grows.
        """
        self.posts.append((name, tab, body))
        self.journal.append(("post", name))

    def post(self, name: str, tab: int, body: Any = None) -> dict[str, Any]:
        """Record a signed post and answer from the canned table."""
        self._record(name, tab, body)
        return self._answer(name, body)

    # -- pure arithmetic, no request ---------------------------------------

    def get_week(self, day: dt.date | dt.datetime) -> int:
        """Mirror ``ClientBase.get_week`` exactly, including its integer maths."""
        if isinstance(day, dt.datetime):
            day = day.date()
        return 1 + int((day - self.start_day).days / 7)

    # -- helpers the gateway calls ----------------------------------------

    def discussions(self) -> list[FakeThread]:
        """The thread list -- one request in the real client."""
        self._record("ListeMessagerie", 131)
        return list(self.threads)

    def menus(self, start: dt.date, end: dt.date) -> list[FakeMenu]:
        """Menus between two dates."""
        self._record("PageMenus", 10, {"start": start, "end": end})
        day = start
        out: list[FakeMenu] = []
        while self.menus_published and day <= end:
            out.append(FakeMenu(f"MENU-{day.isoformat()}", day))
            day += dt.timedelta(days=1)
        return out

    def get_recipients(self) -> list[FakeRecipient]:
        """Who a new discussion may be addressed to."""
        self._record("ListeRessourcesPourCommunication", 131)
        return list(self.recipients)

    def new_discussion(
        self, subject: str, content: str, recipients: Sequence[FakeRecipient]
    ) -> None:
        """Record a new thread."""
        self.new_discussions.append(
            (subject, content, [recipient.name for recipient in recipients])
        )

    def export_ical(self) -> str:
        """A stub iCal URL.

        Stub, and obviously so. The real one carries ``icalsecurise=<token>``,
        which reads the student's whole timetable with no credential -- which
        is why §8.2 keeps it out of every state, every attribute and every
        diagnostic, and why no fixture in this suite may contain a real one.
        """
        self.ical_calls += 1
        return "https://demo.example.invalid/ical/STUB.ics"

    def generate_timetable_pdf(
        self,
        day: dt.date | None = None,
        portrait: bool = False,
    ) -> str:
        """A stub PDF URL."""
        self.pdf_calls += 1
        return f"https://demo.example.invalid/pdf/STUB?day={day}&portrait={portrait}"

    # -- test conveniences -------------------------------------------------

    @property
    def posted_names(self) -> list[str]:
        """Just the function names, in order."""
        return [name for name, _tab, _body in self.posts]

    def body_for(self, name: str) -> Any:
        """The body of the first post to ``name``."""
        for posted, _tab, body in self.posts:
            if posted == name:
                return body
        raise AssertionError(f"nothing was posted to {name!r}")
