"""Serving a homework document, and never publishing PRONOTE's address for it.

This file guards one rule and one cost.

**The rule.** A file's PRONOTE address is
``FichiersExternes/<hex>/<name>?Session=<h>`` where the hex segment is the
document's identifier encrypted with the session's own AES key and IV. It opens
the document with no credentials and it dies with the session, so it must not
reach a state, an attribute, the recorder or a diagnostics download -- the rule
§8.2 established for the iCal URL. What is published is a signed path to this
integration's own view, which relays the bytes.

**The cost.** The download is one of the two fetches in the whole integration
that do not go through ``ClientBase.post`` -- the other is a graded test's
document, ``tests/test_grade_documents.py`` -- so the limiter cannot see it
unless the caller declares it. A missing declaration would spend budget invisibly, which
is the class of defect this project exists to prevent (§11.1).
"""

from __future__ import annotations

from datetime import timedelta
import re
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest

from custom_components.carnet_scolaire.attachment import (
    _SAFE_CONTENT_TYPES,
    FINGERPRINT_PATTERN,
    SIGNATURE_LIFETIME,
    _ByteCache,
    _content_type,
    _locate,
    fingerprint,
)
from custom_components.carnet_scolaire.const import AttachmentKind, Tier
from custom_components.carnet_scolaire.gateway import AttachmentUnavailable

from .conftest import CHILDREN, REQUIRES_HASS
from .fixtures import protocol

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from custom_components.carnet_scolaire.account import PronoteAccount
    from custom_components.carnet_scolaire.gateway import PronoteGateway

    from .fixtures.client import FakeClient


# ---------------------------------------------------------------------------
# The download itself
# ---------------------------------------------------------------------------


def _listed_file(
    gateway: PronoteGateway, client: FakeClient, *, n: str = "ATTACHMENT-1"
) -> tuple[str, str]:
    """Serve one homework item carrying ``enonce.pdf``, and return both keys.

    The keys are what a snapshot holds and what the relay is handed; the ``N``
    is what the fake serves this session. ``n`` lets a test serve the same
    document under another ``N``, which is what a reconnection does.
    """
    client.responses["PageCahierDeTexte"] = protocol.homework_response(
        [protocol.homework(attachments=("enonce.pdf",))]
    )
    item = gateway.homework(client).facts.homework[0]
    if n != "ATTACHMENT-1":
        entry = protocol.homework(attachments=("enonce.pdf",))
        entry["ListePieceJointe"]["V"][0]["N"] = n
        client.responses["PageCahierDeTexte"] = protocol.homework_response([entry])
    return item.id, item.attachments[0].id


def test_the_address_is_built_by_upstream_and_carries_the_session(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """Not re-implemented here, and the test proves which code ran.

    The encrypted segment is produced by ``communication.encryption``, so an
    address that did not pass through it -- a hand-rolled path, a cached
    string, a constant -- would not contain the fake's keyed digest. Asserting
    on the *shape* rather than on a literal is deliberate: the point is that
    upstream's construction was used, not that it produces any particular
    bytes.
    """
    homework_id, attachment_id = _listed_file(gateway, client)
    content, declared, cost = gateway.homework_attachment(
        client, homework_id=homework_id, attachment_id=attachment_id, name="enonce.pdf"
    )

    # The homework is read again first: the `N` the address is built from is
    # only valid in the session that read it.
    assert cost == 2
    assert content == b"%PDF-1.4 not a real document"
    assert declared == "application/pdf"

    (address,) = client.communication.session.gets
    assert address.startswith("https://demo.example.invalid/pronote/FichiersExternes/")
    assert address.endswith("?Session=SESSION-NUMBER")
    assert "enonce.pdf" in address


def test_a_refused_download_is_an_error_and_not_an_error_page(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """``Attachment.data`` returns the body whatever the status.

    That is the trap. ``Attachment.save`` checks the status; the property does
    not, so an expired session yields PRONOTE's login page as ``bytes`` and a
    dashboard would render a few kilobytes of HTML as though it were the
    exercise. Checked here rather than delegated for exactly that reason.
    """
    homework_id, attachment_id = _listed_file(gateway, client)
    client.communication.session.response.status_code = 403
    client.communication.session.response.content = b"<html>login</html>"

    with pytest.raises(AttachmentUnavailable) as refusal:
        gateway.homework_attachment(
            client,
            homework_id=homework_id,
            attachment_id=attachment_id,
            name="enonce.pdf",
        )

    assert refusal.value.status == 403
    assert refusal.value.name == "enonce.pdf"


def test_a_document_opened_after_a_reconnection_is_fetched_by_its_new_identifier(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """The address is built from the ``N`` of *this* session, never a snapshot's.

    PRONOTE re-encrypts every ``N`` at each login, and the encrypted path
    segment is built from it: a card opened before a reconnection held the key
    of a document whose ``N`` no longer meant anything, and the relay built an
    address for nothing. The fake's segment is a digest of what it was given,
    so the new ``N`` is observable in the address.
    """
    homework_id, attachment_id = _listed_file(gateway, client, n="ATTACHMENT-NEXT")

    gateway.homework_attachment(
        client, homework_id=homework_id, attachment_id=attachment_id, name="enonce.pdf"
    )

    (address,) = client.communication.session.gets
    assert b"ATTACHMENT-NEXT" in client.communication.encryption.plaintexts[-1]
    assert address.endswith("?Session=SESSION-NUMBER")


@pytest.mark.parametrize(
    ("homework_known", "attachment_known"),
    [
        pytest.param(False, True, id="homework-gone"),
        pytest.param(True, False, id="document-gone"),
    ],
)
def test_a_document_no_longer_listed_is_a_404_and_fetches_nothing(
    gateway: PronoteGateway,
    client: FakeClient,
    homework_known: bool,
    attachment_known: bool,
) -> None:
    """A key that matches nothing in this session is refused before the wire."""
    homework_id, attachment_id = _listed_file(gateway, client)

    with pytest.raises(AttachmentUnavailable) as refusal:
        gateway.homework_attachment(
            client,
            homework_id=homework_id if homework_known else "hw-0000000000000000",
            attachment_id=attachment_id if attachment_known else "att-0",
            name="enonce.pdf",
        )

    assert refusal.value.status == 404
    assert client.communication.session.gets == []


# ---------------------------------------------------------------------------
# The fingerprint
# ---------------------------------------------------------------------------


def test_a_fingerprint_names_a_document_and_not_a_position() -> None:
    """Which is what makes a re-ordering between two collections harmless.

    An address built from an index would keep resolving after PRONOTE returned
    the same attachments in another order, and would then serve the wrong file
    -- silently, because both are documents of the same homework entry.
    """
    first = fingerprint("HOMEWORK-1", "ATTACHMENT-1")
    second = fingerprint("HOMEWORK-1", "ATTACHMENT-2")

    assert first != second
    assert first == fingerprint("HOMEWORK-1", "ATTACHMENT-1")


def test_the_same_document_id_under_two_homework_entries_differs() -> None:
    """The pair is hashed, not the attachment alone.

    Identifiers are unique per account in practice, but the lookup resolves the
    pair -- so a digest that ignored the homework entry would let one entry's
    row address another's document.
    """
    assert fingerprint("HOMEWORK-1", "SAME") != fingerprint("HOMEWORK-2", "SAME")


def test_a_fingerprint_is_safe_in_a_path() -> None:
    """A real PRONOTE identifier carries a ``#``.

    That is a fragment delimiter: put in a path it truncates the address, so
    everything after it is never sent to the server. Hex is the point, not a
    style choice.
    """
    print_ = fingerprint("29#abc", "79#def")

    assert print_.isalnum()
    assert "#" not in print_
    assert print_ == print_.lower()


# ---------------------------------------------------------------------------
# What the browser is told
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("declared", "expected"),
    [
        pytest.param("application/pdf", "application/pdf", id="a pdf"),
        pytest.param("image/png", "image/png", id="an image"),
        pytest.param(
            "text/plain; charset=utf-8", "text/plain", id="a type with parameters"
        ),
        pytest.param(
            "text/html", "application/octet-stream", id="markup, which must not render"
        ),
        pytest.param(
            "image/svg+xml", "application/octet-stream", id="svg, which carries script"
        ),
        pytest.param(None, "application/octet-stream", id="nothing declared"),
    ],
)
def test_only_a_safe_content_type_is_echoed_back(
    declared: str | None, expected: str
) -> None:
    """Because the bytes are served from Home Assistant's own origin.

    A teacher's upload echoed as ``text/html`` would run its own script with
    the viewer's Home Assistant session -- stored cross-site scripting, through
    a file nobody in this project ever reviewed. ``svg+xml`` is the same hazard
    wearing an image's name. Anything not on the whitelist downloads instead of
    rendering.
    """
    assert _content_type(declared) == expected


def test_markup_is_not_on_the_whitelist() -> None:
    """Stated as its own assertion, so widening the list trips a test.

    A future contributor adding ``text/html`` to make some establishment's
    attachment display would be reopening the injection route above, and a
    parametrised case is easy to delete by accident.
    """
    assert "text/html" not in _SAFE_CONTENT_TYPES
    assert "image/svg+xml" not in _SAFE_CONTENT_TYPES


# ---------------------------------------------------------------------------
# The byte cache
# ---------------------------------------------------------------------------


def test_the_cache_bounds_what_a_page_load_can_cost() -> None:
    """The reason the cost is per document and not per click.

    A browser prefetcher or a link scanner on the dashboard can request every
    address on the page. Cached, that is one request per document per Home
    Assistant run; uncached it would be one per fetch, against a school server
    whose one sanction applies to an IP address.
    """
    cache = _ByteCache()
    for index in range(40):
        cache.put(f"print-{index}", (b"x", "application/pdf"))

    assert cache.get("print-39") == (b"x", "application/pdf")
    assert cache.get("print-0") is None, "the cache grew without bound"


def test_a_minted_address_lives_minutes_and_not_hours() -> None:
    """It is minted at the click and opened at once, so it has no reason to last.

    This used to be twelve hours, derived from a constraint that is gone: the
    address sat in an attribute and had to outlive the homework tier's
    interval. Now every minute beyond the round trip is a minute a copied
    address still opens a child's document. The floor is there too, because a
    PDF viewer asks for a second byte range after the first one arrives, and
    an address that died between the two would read as a broken document.
    """
    assert timedelta(seconds=30) <= SIGNATURE_LIFETIME <= timedelta(minutes=10)


# ---------------------------------------------------------------------------
# Resolution against the snapshot
# ---------------------------------------------------------------------------


@REQUIRES_HASS
class TestWhatTheViewWillServe:
    """The lookup, exercised against a real account holding real snapshots."""

    @pytest.fixture(name="parent_client")
    def parent_client_fixture(self) -> FakeClient:
        """Homework with one file and one link, which is the live shape."""
        from .fixtures.client import FakeClient

        client = FakeClient(children=CHILDREN)
        client.responses["PageCahierDeTexte"] = protocol.homework_response(
            [
                protocol.homework(
                    attachments=(
                        "enonce.pdf",
                        ("Le sujet en ligne", "https://exemple.invalid/sujet"),
                    )
                )
            ]
        )
        return client

    async def test_a_fingerprint_from_the_snapshot_resolves_to_its_document(
        self, hass: HomeAssistant, account: PronoteAccount
    ) -> None:
        """The snapshot is the authority on what this account may fetch.

        Which is why the address needs no student and no homework identifier:
        anything not in the snapshot is refused without ever reaching PRONOTE,
        so an address cannot be edited into a request for somebody else's
        document.
        """
        del hass
        snapshot = account.snapshot(Tier.HOMEWORK, CHILDREN[0][0])
        assert snapshot is not None
        item = snapshot.data.homework[0]
        document = next(a for a in item.attachments if a.kind is AttachmentKind.FILE)

        located = _locate(account, fingerprint(item.id, document.id))

        assert located is not None
        student_id, homework_id, found = located
        assert student_id == CHILDREN[0][0]
        assert homework_id == item.id
        assert found.name == document.name

    async def test_an_unknown_fingerprint_resolves_to_nothing(
        self, hass: HomeAssistant, account: PronoteAccount
    ) -> None:
        """A guessed or stale address must not reach the school's server.

        Refused from the snapshot alone, so a request for a document that has
        left the display horizon costs zero PRONOTE requests -- the integration
        cannot vouch for what it no longer holds.
        """
        del hass
        assert _locate(account, "0" * 16) is None

    async def test_a_document_with_no_identifier_is_never_addressable(
        self, hass: HomeAssistant, account: PronoteAccount
    ) -> None:
        """An attachment PRONOTE sent without an ``N`` cannot be fetched.

        Its fingerprint would be a digest of the empty string -- the same for
        every such attachment on the account -- so matching one would serve an
        arbitrary document. Skipped in the lookup rather than trusted.
        """
        del hass
        assert _locate(account, fingerprint("HOMEWORK-1", "")) is None

    async def test_a_link_is_never_located(
        self, hass: HomeAssistant, account: PronoteAccount
    ) -> None:
        """Only a file can be relayed, and the lookup is where that is decided.

        A link is a third party's page: fetching it through the relay would use
        the school's session on an unrelated site. Both the view and the service
        trust what `_locate` returns, so a link is unreachable from either by
        construction rather than by a check each of them must remember.
        """
        del hass
        snapshot = account.snapshot(Tier.HOMEWORK, CHILDREN[0][0])
        assert snapshot is not None
        item = snapshot.data.homework[0]
        link = next(a for a in item.attachments if a.kind is AttachmentKind.LINK)

        assert _locate(account, fingerprint(item.id, link.id)) is None

    async def test_the_lookup_can_be_held_to_one_child(
        self, hass: HomeAssistant, account: PronoteAccount
    ) -> None:
        """``student_id`` narrows the scan, and the service always passes it.

        This fixture serves both children the same homework, so the same key
        is valid for each: which one answers must be the child that was asked
        for, and a child who is not on the account answers nothing at all.
        """
        del hass
        snapshot = account.snapshot(Tier.HOMEWORK, CHILDREN[0][0])
        assert snapshot is not None
        item = snapshot.data.homework[0]
        document = next(a for a in item.attachments if a.kind is AttachmentKind.FILE)
        key = fingerprint(item.id, document.id)

        second = _locate(account, key, student_id=CHILDREN[1][0])

        assert second is not None
        assert second[0] == CHILDREN[1][0]
        assert _locate(account, key, student_id="NOT-ON-THIS-ACCOUNT") is None

    async def test_the_download_is_charged_to_the_limiter(
        self, hass: HomeAssistant, account: PronoteAccount, parent_client: FakeClient
    ) -> None:
        """A fetch that bypasses ``ClientBase.post``.

        ``Attachment.data`` goes straight to ``communication.session.get``, so
        nothing in the hardened client counts it. If the caller stopped
        declaring the cost, the budget would be spent invisibly -- exactly the
        regression §11.1's contract exists to catch.
        """
        del hass
        from custom_components.carnet_scolaire.attachment import _fetch

        snapshot = account.snapshot(Tier.HOMEWORK, CHILDREN[0][0])
        assert snapshot is not None
        item = snapshot.data.homework[0]
        document = next(a for a in item.attachments if a.kind is AttachmentKind.FILE)

        before = account.limiter.calls_today
        reads = parent_client.posted_names.count("PageCahierDeTexte")
        content, content_type = await _fetch(account, document, item.id, CHILDREN[0][0])
        after = account.limiter.calls_today

        assert content == b"%PDF-1.4 not a real document"
        assert content_type == "application/pdf"
        # Two: the homework is read again in this session to find the
        # document's current `N`, then the bytes are fetched.
        assert after - before == 2
        assert parent_client.posted_names.count("PageCahierDeTexte") == reads + 1
        assert len(parent_client.communication.session.gets) == 1


# ---------------------------------------------------------------------------
# What reaches an attribute
# ---------------------------------------------------------------------------


@REQUIRES_HASS
class TestWhatTheDashboardReceives:
    """The published address, which is Home Assistant's and never PRONOTE's."""

    @pytest.fixture(name="parent_client")
    def parent_client_fixture(self) -> FakeClient:
        """One file and one link on the same homework entry."""
        from .fixtures.client import FakeClient

        client = FakeClient(children=CHILDREN)
        client.responses["PageCahierDeTexte"] = protocol.homework_response(
            [
                protocol.homework(
                    attachments=(
                        "enonce.pdf",
                        ("Le sujet en ligne", "https://exemple.invalid/sujet"),
                    )
                )
            ]
        )
        return client

    @staticmethod
    def _items(hass: HomeAssistant) -> list[dict[str, Any]]:
        state = hass.states.get("sensor.enfant_un_homework_to_do")
        assert state is not None
        items: list[dict[str, Any]] = state.attributes["items"]
        return items

    async def test_a_file_gets_a_key_and_a_link_keeps_its_address(
        self, hass: HomeAssistant, account: PronoteAccount
    ) -> None:
        """Both kinds are offered, and neither entry can open anything by itself.

        A link keeps its own address: it is a third party's, already visible to
        the student in PRONOTE, and a card holding it can name the destination
        before anybody clicks. A file gets a ``key`` -- a fingerprint that
        names a document and authorises nothing. The address that authorises is
        minted by the service at the click.
        """
        del account
        refs = {
            entry["name"]: entry for entry in self._items(hass)[0]["attachment_refs"]
        }

        assert refs["Le sujet en ligne"] == {
            "name": "Le sujet en ligne",
            "kind": "external",
            "url": "https://exemple.invalid/sujet",
        }
        assert refs["enonce.pdf"]["kind"] == "local"
        assert set(refs["enonce.pdf"]) == {"name", "kind", "key"}

    async def test_a_key_has_the_exact_shape_a_card_matches(
        self, hass: HomeAssistant, account: PronoteAccount
    ) -> None:
        """Lower-case hex of a fixed length, and the service schema agrees.

        The card matches ``/^[0-9a-f]{16}$/`` and renders a document mute on
        anything else, so a digest that ever came out upper-case would silently
        disable every file. Checked against the pattern the service validates
        with, so the two cannot drift apart.
        """
        del account
        ref = next(
            entry
            for entry in self._items(hass)[0]["attachment_refs"]
            if entry["kind"] == "local"
        )

        assert re.fullmatch(FINGERPRINT_PATTERN, ref["key"])
        assert re.fullmatch(r"[0-9a-f]{16}", ref["key"])

    async def test_no_published_value_carries_pronotes_own_address(
        self, hass: HomeAssistant, account: PronoteAccount
    ) -> None:
        """The rule of this whole module, asserted on the published shape.

        `Session=` and `FichiersExternes` are the two halves of the address
        that must never leave the process. Searched across the entire attribute
        payload rather than field by field, so a future field cannot reintroduce
        it quietly.
        """
        del account
        payload = repr(self._items(hass))

        assert "FichiersExternes" not in payload
        assert "Session=" not in payload

    async def test_the_identifier_is_not_published_either(
        self, hass: HomeAssistant, account: PronoteAccount
    ) -> None:
        """A fingerprint, not an ``N``.

        Two reasons, and the second is the one a reader forgets: a real
        identifier carries a ``#`` that would truncate a path, *and* an
        attribute is written to the recorder, so publishing one puts a real
        PRONOTE identifier in a database that outlives the session.
        """
        item = self._items(hass)[0]
        payload = repr(item["attachment_refs"])
        snapshot = account.snapshot(Tier.HOMEWORK, CHILDREN[0][0])
        assert snapshot is not None
        (document,) = (
            attachment
            for homework in snapshot.data.homework
            if homework.id == item["id"]
            for attachment in homework.attachments
            if attachment.ref == "ATTACHMENT-1"
        )

        assert "ATTACHMENT-1" not in payload
        assert fingerprint(item["id"], document.id) in payload

    async def test_the_deprecated_key_keeps_links_and_no_longer_carries_files(
        self, hass: HomeAssistant, account: PronoteAccount
    ) -> None:
        """A card that has not updated keeps opening links, and loses files.

        That trade is deliberate. The key used to give each file a signed path,
        and a signed path is a bearer token readable by every account in
        ``/api/states``. Keeping it "for one more release" would have kept open
        the exact hole this release closes.
        """
        del account
        links = self._items(hass)[0]["attachment_links"]

        assert links == [
            {"name": "Le sujet en ligne", "url": "https://exemple.invalid/sujet"}
        ]

    async def test_the_names_are_still_plain_strings(
        self, hass: HomeAssistant, account: PronoteAccount
    ) -> None:
        """`attachments` predates the addresses and must keep its shape.

        A template doing ``| join(', ')`` on it is the normal way to write
        "Pièces jointes : a.pdf, b.pdf" into a notification, and that is why
        the addresses went into a second key instead of enriching this one.
        """
        del account
        names = self._items(hass)[0]["attachments"]

        assert names == ["enonce.pdf", "Le sujet en ligne"]
        assert all(isinstance(name, str) for name in names)


@REQUIRES_HASS
async def test_the_route_is_registered_once_for_the_whole_instance(
    hass: HomeAssistant,
) -> None:
    """Registered from ``async_setup``, like the actions, and for one reason.

    A route is per Home Assistant, not per account. Registering it from
    ``async_setup_entry`` would re-register the same path on every entry and on
    every reload -- at best redundantly, at worst replacing a handler while a
    request is in flight.
    """
    from custom_components.carnet_scolaire import async_setup

    with patch.object(hass, "http", create=True) as http:
        assert await async_setup(hass, {})

    assert http.register_view.call_count == 1


# ---------------------------------------------------------------------------
# What must not become durable
# ---------------------------------------------------------------------------


@REQUIRES_HASS
class TestNoStateCanOpenADocument:
    """No state, of any entity, carries an address that opens a document.

    This is the invariant the service exists for, and it is stronger than the
    one it replaces. Until this release a file's signed path sat inside
    ``items``, and the guard was that the *recorder* never stored it -- which
    took three versions to get right, and even then left the token readable by
    every account in ``/api/states``, kept by automation traces in
    ``.storage``, and shown in the more-info dialog. Now there is nothing to
    exclude, because nothing is published.
    """

    @pytest.fixture(name="parent_client")
    def parent_client_fixture(self) -> FakeClient:
        """One file, so there is a document an address could have opened."""
        from .fixtures.client import FakeClient

        client = FakeClient(children=CHILDREN)
        client.responses["PageCahierDeTexte"] = protocol.homework_response(
            [protocol.homework(attachments=("enonce.pdf",))]
        )
        return client

    async def test_no_state_carries_a_signed_address(
        self, hass: HomeAssistant, account: PronoteAccount
    ) -> None:
        """Searched across every state on the instance, not one attribute.

        A new entity, a new attribute or a reshaped one cannot reintroduce a
        signature without failing this. The precondition is asserted too: a
        file must actually be offered, or the absence proves nothing.
        """
        del account
        state = hass.states.get("sensor.enfant_un_homework_to_do")
        assert state is not None
        refs = state.attributes["items"][0]["attachment_refs"]
        assert any(ref["kind"] == "local" for ref in refs), (
            "no file was offered, so this proves nothing"
        )

        carrying = [
            other.entity_id
            for other in hass.states.async_all()
            if "authSig=" in repr(other.attributes) or "authSig=" in other.state
        ]

        assert carrying == []


def test_no_entity_class_shadows_the_shared_exclusion() -> None:
    """A subclass declaration replaces the base set; it does not extend it.

    `Entity.__init_subclass__` computes
    ``_entity_component_unrecorded_attributes | cls._unrecorded_attributes``,
    and the right-hand side resolves by ordinary attribute lookup. So any class
    in this integration that declares the name silently drops every entry
    `PronoteEntity` had declared -- which is exactly what v0.0.22 did, handing
    the recorder `items`, `lessons`, the menu fields, `messages`, `address` and
    `fetched_at` for every sensor, and writing signed addresses into a live
    database.

    This is the barrier for the whole class of mistake rather than for the one
    instance of it: a declaration is allowed, but only if it still covers the
    shared set. The same defect had already been paid for once, on the
    diagnostic entities that are a *sibling* of `PronoteEntity` rather than a
    subclass (see the comment in `entity.py`).
    """
    from importlib import import_module

    from homeassistant.helpers.entity import Entity

    from custom_components.carnet_scolaire import DOMAIN, PLATFORMS
    from custom_components.carnet_scolaire.const import UNRECORDED_LIST_ATTRIBUTES

    # Derived from `PLATFORMS`, not listed here, so a platform added later is
    # covered without anybody remembering to extend this test. `entity.py` is
    # added because the two bases live there and both are meant to declare.
    modules = [import_module(f"custom_components.{DOMAIN}.entity")] + [
        import_module(f"custom_components.{DOMAIN}.{platform}")
        for platform in PLATFORMS
    ]

    declaring = [
        (module.__name__, name, member._unrecorded_attributes)
        for module in modules
        for name, member in vars(module).items()
        if isinstance(member, type)
        and issubclass(member, Entity)
        and "_unrecorded_attributes" in member.__dict__
    ]
    assert declaring, "nothing declared an exclusion, so this proves nothing"

    offenders = {
        f"{module}.{name}": sorted(UNRECORDED_LIST_ATTRIBUTES - declared)
        for module, name, declared in declaring
        if not declared >= UNRECORDED_LIST_ATTRIBUTES
    }

    assert not offenders, (
        f"these classes shadow the shared exclusion and drop entries: {offenders}"
    )


@REQUIRES_HASS
class TestHowARefusalIsAnswered:
    """The two refusals a parent can actually meet, and what they say."""

    @pytest.fixture(name="parent_client")
    def parent_client_fixture(self) -> FakeClient:
        """One file, which is the case that needs fetching."""
        from .fixtures.client import FakeClient

        client = FakeClient(children=CHILDREN)
        client.responses["PageCahierDeTexte"] = protocol.homework_response(
            [protocol.homework(attachments=("enonce.pdf",))]
        )
        return client

    @staticmethod
    def _document(account: PronoteAccount) -> tuple[str, Any]:
        snapshot = account.snapshot(Tier.HOMEWORK, CHILDREN[0][0])
        assert snapshot is not None
        item = snapshot.data.homework[0]
        return fingerprint(item.id, item.attachments[0].id), item.attachments[0]

    @staticmethod
    def _homework_key(account: PronoteAccount) -> str:
        snapshot = account.snapshot(Tier.HOMEWORK, CHILDREN[0][0])
        assert snapshot is not None
        return snapshot.data.homework[0].id

    async def test_a_click_is_run_at_the_priority_of_a_human_request(
        self, hass: HomeAssistant, account: PronoteAccount
    ) -> None:
        """Observed, not read off the source.

        A click is a gesture, like every human-initiated call in `services.py`:
        somebody is waiting, so it crosses quiet hours and is shed at the cap
        like `HIGH`. It is deliberately **not** `CRITICAL`, which is never shed
        at all and is reserved for the login and a tier that has never
        collected.
        """
        del hass
        from custom_components.carnet_scolaire.attachment import _fetch
        from custom_components.carnet_scolaire.const import Priority

        _print, document = self._document(account)
        seen: list[Priority] = []
        original = account.extras.session.run

        async def spy(name: str, priority: Priority, fn: Any, **kwargs: Any) -> Any:
            seen.append(priority)
            return await original(name, priority, fn, **kwargs)

        with patch.object(account.extras.session, "run", spy):
            await _fetch(account, document, self._homework_key(account), CHILDREN[0][0])

        assert seen == [Priority.GESTURE]

    async def test_a_document_opens_during_quiet_hours(
        self, hass: HomeAssistant, account: PronoteAccount
    ) -> None:
        """The defect a parent hit at 22:30: "deferred by the rate limiter".

        Through the real limiter, with only the clock's verdict forced. The
        control half is what makes the first half mean anything: a ``HIGH``
        call on the same limiter in the same instant is still refused, so the
        window really is closed and the click is what crosses it.
        """
        del hass
        from custom_components.carnet_scolaire.attachment import _fetch
        from custom_components.carnet_scolaire.const import Priority
        from custom_components.carnet_scolaire.ratelimit import DeferReason

        _print, document = self._document(account)

        with patch.object(account.limiter, "_in_quiet_hours", return_value=True):
            refused = account.limiter.check("marks", Priority.HIGH)
            content, _content_type = await _fetch(
                account, document, self._homework_key(account), CHILDREN[0][0]
            )

        assert refused.reason is DeferReason.QUIET_HOURS
        assert content

    async def test_a_deferral_answers_with_the_limiters_own_delay(
        self, hass: HomeAssistant, account: PronoteAccount
    ) -> None:
        """A hard-coded minute would invite a client to retry all night.

        The daily cap defers until midnight, not for a minute, and this
        endpoint is reachable by a browser that honours `Retry-After`. Sending
        the limiter's own figure is what keeps an automatic retry from
        hammering a school server that has already been asked enough today.
        """
        from custom_components.carnet_scolaire.attachment import PronoteAttachmentView
        from custom_components.carnet_scolaire.ratelimit import (
            DeferReason,
            TierDeferred,
        )

        print_, _document = self._document(account)
        request = _FakeRequest(hass)

        with patch(
            "custom_components.carnet_scolaire.attachment._fetch",
            side_effect=TierDeferred(DeferReason.DAILY_CAP, 7200.0),
        ):
            response = await PronoteAttachmentView().get(
                request,  # type: ignore[arg-type]
                account.entry.entry_id,
                print_,
            )

        assert response.status == 503
        assert response.headers["Retry-After"] == "7200"

    async def test_an_unknown_account_is_refused_without_a_traceback(
        self, hass: HomeAssistant, account: PronoteAccount
    ) -> None:
        """The address embeds an entry id, so it can name an entry that is gone.

        A removed or unloaded account has to answer with a status rather than
        raising out of the handler -- aiohttp would turn that into a 500, which
        reads like a broken integration rather than "this account is not
        loaded".
        """
        del account
        from custom_components.carnet_scolaire.attachment import PronoteAttachmentView

        response = await PronoteAttachmentView().get(
            _FakeRequest(hass),  # type: ignore[arg-type]
            "01ABSENTABSENTABSENTABSENT",
            "0" * 16,
        )

        assert response.status == 404

    async def test_a_link_is_not_relayed_through_the_school_session(
        self, hass: HomeAssistant, account: PronoteAccount, parent_client: FakeClient
    ) -> None:
        """A link has its own address and the dashboard was given it directly.

        Relaying one would mean fetching an unrelated site with the
        establishment's authenticated session -- which is not this
        integration's business, and would make Home Assistant an open proxy for
        anything a teacher pastes.
        """
        from custom_components.carnet_scolaire.attachment import PronoteAttachmentView
        from custom_components.carnet_scolaire.models import HomeworkAttachment

        link = HomeworkAttachment(
            name="Le sujet",
            url="https://exemple.invalid/sujet",
            id="ATTACHMENT-9",
            kind=AttachmentKind.LINK,
        )
        with patch(
            "custom_components.carnet_scolaire.attachment._locate",
            return_value=(CHILDREN[0][0], "hw-0000000000000000", link),
        ):
            response = await PronoteAttachmentView().get(
                _FakeRequest(hass),  # type: ignore[arg-type]
                account.entry.entry_id,
                "0" * 16,
            )

        assert response.status == 404
        assert "no such document" in response.text
        # Refused before the wire: the school was never asked for a link.
        assert parent_client.communication.session.gets == []


class _FakeRequest:
    """An aiohttp request, as far as the view reads one.

    The view takes ``hass`` out of ``request.app`` and nothing else, so the
    handler can be exercised without standing up an HTTP server -- which is
    what lets these tests assert on statuses and headers rather than on a
    rendered page.
    """

    def __init__(self, hass: HomeAssistant) -> None:
        from homeassistant.helpers.http import KEY_HASS

        self.app = {KEY_HASS: hass}


# ---------------------------------------------------------------------------
# The view: what a browser gets when the document is not there
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        pytest.param('Devoir "maison".pdf', "Devoir maison.pdf", id="quotes-removed"),
        pytest.param(
            "Exercices" + chr(92) + "4.pdf",
            "Exercices4.pdf",
            id="backslash-removed",
        ),
        pytest.param("Énoncé été.pdf", "Enonce ete.pdf", id="accents-folded"),
        pytest.param("Leçon à écouter.mp3", "Lecon a ecouter.mp3", id="cedilla"),
        pytest.param("Œuvre.pdf", "_uvre.pdf", id="no-fold-becomes-underscore"),
        pytest.param("Éé", "Ee", id="a-name-made-of-accents"),
        pytest.param('""', "document", id="nothing-left-is-still-a-filename"),
        pytest.param(
            "sujet" + chr(13) + chr(10) + "X-Injected: 1.pdf",
            "sujetX-Injected: 1.pdf",
            id="no-line-break",
        ),
    ],
)
def test_the_download_filename_is_reduced_to_something_a_header_can_carry(
    name: str, expected: str
) -> None:
    """A ``Content-Disposition`` header is ASCII, and a quote closes it early.

    Never a ``?`` for an accent: Windows forbids it in a filename, and a
    browser that meets one saved the file under the URL's 16-hex key with no
    extension -- every accented name, on a live instance.

    The name in the attribute stays the real one -- this is only what the
    browser is told to call the file it saves. The empty case is the one worth
    pinning: a name made entirely of characters that drop out would otherwise
    produce a header naming no file at all, which browsers answer by saving
    the page instead of the document.
    """
    from custom_components.carnet_scolaire.attachment import _ascii

    assert _ascii(name) == expected


def test_the_real_name_travels_in_filename_star() -> None:
    """RFC 6266: ``filename*`` carries the name with its accents, UTF-8 and
    percent-encoded; ``filename`` the folded fallback beside it."""
    from custom_components.carnet_scolaire.attachment import _disposition

    assert _disposition("Leçon à écouter.mp3") == (
        'inline; filename="Lecon a ecouter.mp3"; '
        "filename*=UTF-8''Le%C3%A7on%20%C3%A0%20%C3%A9couter.mp3"
    )


def test_a_line_break_cannot_reach_either_filename() -> None:
    """Both parameters come from the same printable name, so neither can
    start a header of its own."""
    from custom_components.carnet_scolaire.attachment import _disposition

    value = _disposition("a" + chr(13) + chr(10) + "X-Injected: 1")
    assert chr(10) not in value
    assert chr(13) not in value
    assert "%0D" not in value


@pytest.mark.parametrize("declared", ["audio/mpeg", "audio/mp4", "audio/ogg"])
def test_a_recording_plays_in_the_browser(declared: str) -> None:
    """A language teacher's recording is declared as what it is: an audio
    stream runs nothing in the origin, and octet-stream sent it to downloads."""
    from custom_components.carnet_scolaire.attachment import _content_type

    assert _content_type(declared) == declared


def test_a_document_that_belongs_to_no_homework_is_not_located(
    account: PronoteAccount,
) -> None:
    """A fingerprint nobody can match must resolve to nothing, not to anything.

    The view answers 404 on this, and the alternative is worse than an error:
    a resolver that fell through to "the first attachment" would serve one
    child's document to a signature minted for another.
    """
    assert _locate(account, "a" * 64) is None


def test_an_attachment_with_no_identifier_is_skipped_rather_than_matched(
    account: PronoteAccount,
) -> None:
    """PRONOTE publishes documents that carry a name and no ``N``.

    Those cannot be downloaded -- there is nothing to ask for -- and their
    fingerprint would be built from an empty identifier, so every one of them
    would share it. Two homework items with such a document would then be
    indistinguishable, and the view would serve whichever came first.
    """
    from custom_components.carnet_scolaire.attachment import fingerprint

    # The fingerprint an empty identifier would produce, if one were minted.
    collision = fingerprint("HOMEWORK-1", "")

    assert _locate(account, collision) is None


@REQUIRES_HASS
class TestWhatTheBrowserGetsBack:
    """The view end to end: the bytes, the cache, and the two failures.

    Asserted through ``PronoteAttachmentView.get`` rather than on ``_fetch``,
    because what a dashboard tile meets is a status and a set of headers. A
    document that downloads correctly and is then served with the wrong
    ``Content-Disposition`` lands in the downloads folder instead of opening,
    and nothing below the view would notice.
    """

    @pytest.fixture(name="parent_client")
    def parent_client_fixture(self) -> FakeClient:
        """One homework item with one file, which is what needs serving."""
        from .fixtures.client import FakeClient

        client = FakeClient(children=CHILDREN)
        client.responses["PageCahierDeTexte"] = protocol.homework_response(
            [protocol.homework(attachments=("enonce.pdf",))]
        )
        return client

    @staticmethod
    def _print(account: PronoteAccount) -> str:
        snapshot = account.snapshot(Tier.HOMEWORK, CHILDREN[0][0])
        assert snapshot is not None
        item = snapshot.data.homework[0]
        return fingerprint(item.id, item.attachments[0].id)

    async def test_an_address_naming_no_document_is_a_404_and_costs_nothing(
        self, hass: HomeAssistant, account: PronoteAccount, parent_client: FakeClient
    ) -> None:
        """A signed address outlives the document it names.

        The signature bounds *who* may ask, not *what* is still there: a tile
        rendered from yesterday's snapshot carries an address for a homework
        item that has since left the display horizon. That has to answer with a
        status and without a request -- the snapshot is the authority, so a
        fingerprint it cannot match is refused before the wire.
        """
        from custom_components.carnet_scolaire.attachment import PronoteAttachmentView

        before = len(parent_client.communication.session.gets)

        response = await PronoteAttachmentView().get(
            _FakeRequest(hass),  # type: ignore[arg-type]
            account.entry.entry_id,
            "0" * 64,
        )

        assert response.status == 404
        assert "no such document" in response.text
        assert len(parent_client.communication.session.gets) == before

    async def test_the_bytes_are_served_inline_and_the_second_read_is_free(
        self, hass: HomeAssistant, account: PronoteAccount, parent_client: FakeClient
    ) -> None:
        """One request per document, however many times a card is rendered.

        A dashboard re-renders on every state change of every entity on it, and
        an exercise sheet is the same bytes for ever -- it is addressed by a
        digest of its identifiers, not by a position. Without the cache a page
        holding four documents would spend four requests each time somebody
        opened it, which is how a daily budget disappears into a screen nobody
        is even looking at.
        """
        from custom_components.carnet_scolaire.attachment import PronoteAttachmentView

        print_ = self._print(account)
        view = PronoteAttachmentView()

        first = await view.get(
            _FakeRequest(hass),  # type: ignore[arg-type]
            account.entry.entry_id,
            print_,
        )

        assert first.status == 200
        assert first.body == b"%PDF-1.4 not a real document"
        assert first.content_type == "application/pdf"
        assert first.headers["Content-Disposition"] == (
            "inline; filename=\"enonce.pdf\"; filename*=UTF-8''enonce.pdf"
        )
        assert "private" in first.headers["Cache-Control"]
        fetched = len(parent_client.communication.session.gets)
        assert fetched == 1

        second = await view.get(
            _FakeRequest(hass),  # type: ignore[arg-type]
            account.entry.entry_id,
            print_,
        )

        assert second.status == 200
        assert second.body == first.body
        assert len(parent_client.communication.session.gets) == fetched, (
            "the second read went to PRONOTE: the cache is not being reused"
        )

    async def test_a_download_that_fails_answers_502_and_caches_nothing(
        self, hass: HomeAssistant, account: PronoteAccount
    ) -> None:
        """A failed download must not become a cached empty document.

        502 and not 500: the school's server refused, this integration did not
        break, and the distinction is what tells a reader whether to open an
        issue here. Caching the failure would be worse than the failure -- the
        document would stay broken until a reload, long after the server
        recovered.
        """
        from custom_components.carnet_scolaire.attachment import (
            PronoteAttachmentView,
            cache_for,
        )

        print_ = self._print(account)

        with patch(
            "custom_components.carnet_scolaire.attachment._fetch",
            side_effect=AttachmentUnavailable("enonce.pdf", 403),
        ):
            response = await PronoteAttachmentView().get(
                _FakeRequest(hass),  # type: ignore[arg-type]
                account.entry.entry_id,
                print_,
            )

        assert response.status == 502
        assert cache_for(hass, account.entry.entry_id).get(print_) is None

    async def test_a_source_with_no_pronote_session_cannot_download_at_all(
        self, hass: HomeAssistant, account: PronoteAccount
    ) -> None:
        """Ecoledirecte has no session to fetch through, and must say so.

        The view is registered once for the whole instance, so it answers for
        every entry whatever its source. Reaching the gateway with no session
        would be an ``AttributeError`` inside the handler, which aiohttp turns
        into a 500 -- a broken integration, rather than a source that does not
        offer the feature.
        """
        del hass
        from custom_components.carnet_scolaire.attachment import _fetch

        snapshot = account.snapshot(Tier.HOMEWORK, CHILDREN[0][0])
        assert snapshot is not None
        item = snapshot.data.homework[0]
        document = item.attachments[0]

        with (
            patch(
                "custom_components.carnet_scolaire.account.has_pronote_extras",
                return_value=False,
            ),
            pytest.raises(AttachmentUnavailable) as raised,
        ):
            await _fetch(account, document, item.id, CHILDREN[0][0])

        assert raised.value.status == 501


@REQUIRES_HASS
class TestWhatTheScanSkipsOverRatherThanStopsAt:
    """``_locate`` walks every child, and a gap at one must not end the walk."""

    @pytest.fixture(name="parent_client")
    def parent_client_fixture(self) -> FakeClient:
        """A document PRONOTE sent with no ``N``, beside one that has one."""
        from .fixtures.client import FakeClient

        item = protocol.homework(attachments=("sans_n.pdf", "enonce.pdf"))
        # PRONOTE does publish documents carrying a name and no identifier;
        # the fixture builder always mints one, so it is removed here rather
        # than adding an option nothing else would use.
        item["ListePieceJointe"]["V"][0]["N"] = ""
        client = FakeClient(children=CHILDREN)
        client.responses["PageCahierDeTexte"] = protocol.homework_response([item])
        return client

    async def test_a_document_with_no_identifier_is_stepped_over(
        self, hass: HomeAssistant, account: PronoteAccount
    ) -> None:
        """Skipped, and the document after it is still found.

        Every attachment with no ``N`` would share one fingerprint -- the
        digest of an empty identifier -- so matching on it would serve an
        arbitrary document. Returning ``None`` at the first such attachment
        would be the other defect: the usable document sitting behind it in the
        same homework item would become unreachable.
        """
        del hass
        snapshot = account.snapshot(Tier.HOMEWORK, CHILDREN[0][0])
        assert snapshot is not None
        item = snapshot.data.homework[0]
        nameless, usable = item.attachments
        assert nameless.ref == ""
        assert nameless.kind is AttachmentKind.OPAQUE

        assert _locate(account, fingerprint(item.id, "")) is None
        assert _locate(account, fingerprint(item.id, nameless.id)) is None

        located = _locate(account, fingerprint(item.id, usable.id))
        assert located is not None
        assert located[2].name == usable.name

    async def test_a_child_whose_homework_never_collected_does_not_end_the_scan(
        self, hass: HomeAssistant, account: PronoteAccount
    ) -> None:
        """One child with no snapshot, and the sibling's document still serves.

        A second child's first collection can be deferred by the budget, or
        fail, or simply not have happened yet on a freshly added entry. If the
        scan answered ``None`` at that child instead of stepping over it, every
        document of every child listed after them would return 404 -- and the
        order children are announced in is PRONOTE's, so which siblings broke
        would look arbitrary.
        """
        del hass
        real = account.snapshot
        item = account.snapshot(Tier.HOMEWORK, CHILDREN[1][0])
        assert item is not None, "the fixture must give both children homework"

        def missing_for_the_first(tier: Tier, student_id: str) -> Any:
            if student_id == CHILDREN[0][0]:
                return None
            return real(tier, student_id)

        with patch.object(account, "snapshot", missing_for_the_first):
            homework = item.data.homework[0]
            (document,) = (
                attachment
                for attachment in homework.attachments
                if attachment.ref == "ATTACHMENT-2"
            )
            located = _locate(account, fingerprint(homework.id, document.id))

        assert located is not None
        assert located[0] == CHILDREN[1][0]
