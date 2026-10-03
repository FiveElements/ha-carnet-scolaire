"""A graded test's paper and answers, and everything else PRONOTE says of a grade.

A teacher can join two documents to a graded test: its paper and its answers.
PRONOTE names them in two keys of the ``listeDevoirs`` entry and serves each by
the *grade's* ``N`` plus a file type -- which ``pronotepy`` cannot express, so
this is the one address the gateway builds itself. The rule of
:mod:`.attachment` holds unchanged: no published value opens anything, a card
gets a key and the service signs it at the click.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from custom_components.carnet_scolaire.attachment import (
    _locate_grade_document,
    grade_fingerprint,
    resolve,
)
from custom_components.carnet_scolaire.const import GradeDocumentRole, Tier
from custom_components.carnet_scolaire.gateway import AttachmentUnavailable
from custom_components.carnet_scolaire.models import GradeDocument

from .conftest import CHILDREN, REQUIRES_HASS
from .fixtures import protocol
from .test_gateway import current_period

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from custom_components.carnet_scolaire.account import PronoteAccount
    from custom_components.carnet_scolaire.gateway import PronoteGateway

    from .fixtures.client import FakeClient


def _graded(client: FakeClient, **overrides: Any) -> None:
    """Serve one grade carrying both documents, unless told otherwise."""
    fields: dict[str, Any] = {
        "subject_file": "sujet.pdf",
        "correction_file": "corrige.pdf",
    }
    fields.update(overrides)
    client.responses["DernieresNotes"] = protocol.marks_response(
        grades=[protocol.grade(**fields)]
    )


def _decoded(gateway: PronoteGateway, client: FakeClient) -> Any:
    period = current_period(gateway, client)
    return gateway.marks(client, period, with_report=False).facts.grades[0]  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Decoding
# ---------------------------------------------------------------------------


def test_a_graded_test_names_its_paper_and_its_answers(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """The role is which key carried the name, never a guess from the name."""
    _graded(client)

    grade = _decoded(gateway, client)

    assert grade.documents == (
        GradeDocument(name="sujet.pdf", role=GradeDocumentRole.SUBJECT),
        GradeDocument(name="corrige.pdf", role=GradeDocumentRole.CORRECTION),
    )


def test_most_grades_carry_no_document(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """Absent keys and empty names alike are no document, not a nameless one."""
    _graded(client, subject_file=None, correction_file="")

    assert _decoded(gateway, client).documents == ()


def test_answers_posted_later_keep_the_grade_it_belongs_to(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """The documents are not part of the grade's key.

    Answers are often joined days after the mark. Were they part of the key,
    that grade would read as a new one -- a second "new grade" announcement
    for a mark the family already heard about.
    """
    _graded(client, subject_file=None, correction_file=None)
    before = _decoded(gateway, client).id
    _graded(client)

    assert _decoded(gateway, client).id == before


def test_the_remark_on_the_mark_is_kept_apart_from_the_tests_title(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """``commentaire`` titles the test; ``commentaireSurNote`` is about this mark."""
    _graded(client, remark="Bon travail", in_groups=True)

    grade = _decoded(gateway, client)

    assert grade.comment == "Contrôle sur les fractions"
    assert grade.remark == "Bon travail"
    assert grade.subject_in_groups is True


def test_a_grade_without_a_remark_or_groups_says_so_plainly(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """The ordinary case: no remark, a subject taught to the whole class."""
    _graded(client)

    grade = _decoded(gateway, client)

    assert grade.remark is None
    assert grade.subject_in_groups is False


# ---------------------------------------------------------------------------
# The download
# ---------------------------------------------------------------------------


def _download(
    gateway: PronoteGateway,
    client: FakeClient,
    *,
    role: GradeDocumentRole = GradeDocumentRole.CORRECTION,
    grade_id: str | None = None,
    period_index: int | None = None,
) -> tuple[bytes, str | None, int]:
    grade = _decoded(gateway, client)
    period = current_period(gateway, client)
    client.posts.clear()
    return gateway.grade_document(
        client,
        period_index=period.index if period_index is None else period_index,  # type: ignore[attr-defined]
        grade_id=grade.id if grade_id is None else grade_id,
        role=role,
        name="sujet.pdf" if role is GradeDocumentRole.SUBJECT else "corrige.pdf",
    )


def test_the_address_names_the_grade_and_the_file_type(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """One ``N`` names two files; ``G`` says which, as PRONOTE's own client does.

    Encrypted by upstream's cipher -- the fake records each plaintext it was
    handed -- so this proves the segment carries both the grade's ``N`` and
    the type, and that nothing bypassed ``communication.encryption``.
    """
    _graded(client)

    content, declared, cost = _download(gateway, client)

    assert content == b"%PDF-1.4 not a real document"
    assert declared == "application/pdf"
    plaintext = client.communication.encryption.plaintexts[-1]
    segment = json.loads(plaintext[: plaintext.rindex(b"}") + 1])
    assert segment == {"N": "GRADE-1", "Actif": True, "G": "DevoirCorrige"}
    (address,) = client.communication.session.gets
    assert address.startswith("https://demo.example.invalid/pronote/FichiersExternes/")
    assert address.endswith("/corrige.pdf?Session=SESSION-NUMBER")
    # Two: the grades are read again in this session, then the bytes fetched.
    assert cost == 2
    assert client.posted_names == ["DernieresNotes"]


def test_the_paper_is_asked_for_under_its_own_type(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """The other role, the other type -- and the same grade's ``N``."""
    _graded(client)

    _download(gateway, client, role=GradeDocumentRole.SUBJECT)

    assert b'"G":"DevoirSujet"' in client.communication.encryption.plaintexts[-1]


def _answering(client: FakeClient, *statuses: int) -> None:
    """Answer the relay's GETs with these statuses, in order, recording each."""
    from .fixtures.client import FakeResponse

    queue = list(statuses)

    def get(url: str) -> FakeResponse:
        client.communication.session.gets.append(url)
        return FakeResponse(status_code=queue.pop(0))

    client.communication.session.get = get  # type: ignore[method-assign]


def test_a_paper_refused_under_one_type_is_asked_for_under_the_next(
    gateway: PronoteGateway, client: FakeClient, caplog: pytest.LogCaptureFixture
) -> None:
    """The live defect: the paper answered 404 under ``DevoirSujet``.

    The answers of the same tests opened, so the session, the grade's ``N``
    and the address were right and only the file type was in doubt. The next
    type is tried, the cost says so, and the fallback is logged -- without the
    address or the file name -- so the order can be settled from evidence.
    """
    _graded(client)
    _answering(client, 404, 200)

    content, _declared, cost = _download(
        gateway, client, role=GradeDocumentRole.SUBJECT
    )

    assert content == b"%PDF-1.4 not a real document"
    assert cost == 3
    assert len(client.communication.session.gets) == 2
    assert b'"G":"EvaluationSujet"' in client.communication.encryption.plaintexts[-1]
    (record,) = [r for r in caplog.records if "file type" in r.getMessage()]
    assert "EvaluationSujet" in record.getMessage()
    assert "sujet.pdf" not in record.getMessage()
    assert "FichiersExternes" not in record.getMessage()


def test_a_paper_refused_under_every_type_is_a_404_and_says_so_once(
    gateway: PronoteGateway, client: FakeClient, caplog: pytest.LogCaptureFixture
) -> None:
    """Both types tried, then a refusal -- logged, so the gap is visible."""
    _graded(client)
    _answering(client, 404, 404)

    with pytest.raises(AttachmentUnavailable) as refusal:
        _download(gateway, client, role=GradeDocumentRole.SUBJECT)

    assert refusal.value.status == 404
    assert len(client.communication.session.gets) == 2
    assert any("every file type" in r.getMessage() for r in caplog.records)


def test_only_a_404_moves_on_to_the_next_type(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """A dead session is not a wrong type: one 403, no second request."""
    _graded(client)
    _answering(client, 403, 200)

    with pytest.raises(AttachmentUnavailable) as refusal:
        _download(gateway, client, role=GradeDocumentRole.SUBJECT)

    assert refusal.value.status == 403
    assert len(client.communication.session.gets) == 1


def test_the_declared_cost_covers_every_type_that_may_be_tried() -> None:
    """Charged at admission and never refunded, so the worst case is declared."""
    from custom_components.carnet_scolaire.gateway import grade_document_cost

    assert grade_document_cost(GradeDocumentRole.SUBJECT) == 3
    assert grade_document_cost(GradeDocumentRole.CORRECTION) == 2


def test_a_graded_test_with_a_paper_logs_its_shape_and_no_value(
    gateway: PronoteGateway, client: FakeClient, caplog: pytest.LogCaptureFixture
) -> None:
    """The diagnostic for the paper PRONOTE refuses, and what it must not say.

    Keys and type names only: no ``N``, no file name, no mark, no comment --
    the log is what a parent pastes into an issue.
    """
    import logging

    _graded(client)
    caplog.set_level(logging.DEBUG, logger="custom_components.carnet_scolaire.gateway")

    _decoded(gateway, client)

    (record,) = [r for r in caplog.records if "has this shape" in r.getMessage()]
    message = record.getMessage()
    assert "'libelleSujet': 'str'" in message
    assert "'service': {'V': {" in message
    for value in ("GRADE-1", "sujet.pdf", "corrige.pdf", "14,5", "fractions"):
        assert value not in message


def test_a_grade_without_a_paper_logs_no_shape(
    gateway: PronoteGateway, client: FakeClient, caplog: pytest.LogCaptureFixture
) -> None:
    """Only an entry with a paper is the case being diagnosed."""
    import logging

    _graded(client, subject_file=None)
    caplog.set_level(logging.DEBUG, logger="custom_components.carnet_scolaire.gateway")

    _decoded(gateway, client)

    assert not [r for r in caplog.records if "has this shape" in r.getMessage()]


def test_a_shape_describes_lists_by_their_first_item_and_stops_deep_down() -> None:
    """Bounded, so a deep payload cannot turn one log line into a dump."""
    from custom_components.carnet_scolaire.gateway import _structure

    assert _structure({"b": [{"x": 1}, {"y": 2}], "a": [], "c": True}) == {
        "a": [],
        "b": [{"x": "int"}],
        "c": "bool",
    }
    assert _structure({"1": {"2": {"3": {"4": {"5": 0}}}}}) == {
        "1": {"2": {"3": {"4": "..."}}}
    }


def test_a_document_opened_after_a_reconnection_uses_the_new_n(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """The ``N`` is this session's, never the snapshot's.

    Every login re-encrypts it, so a key read before a reconnection must be
    looked up again: the address is built from what *this* session serves.
    """
    _graded(client)
    grade = _decoded(gateway, client)
    _graded(client, identifier="GRADE-NEXT")
    period = current_period(gateway, client)

    gateway.grade_document(
        client,
        period_index=period.index,  # type: ignore[attr-defined]
        grade_id=grade.id,
        role=GradeDocumentRole.SUBJECT,
        name="sujet.pdf",
    )

    assert b'"N":"GRADE-NEXT"' in client.communication.encryption.plaintexts[-1]


@pytest.mark.parametrize(
    ("overrides", "download"),
    [
        pytest.param({}, {"grade_id": "grade-0000000000000000"}, id="grade-gone"),
        pytest.param({"correction_file": None}, {}, id="answers-withdrawn"),
        pytest.param({}, {"period_index": 99}, id="period-not-in-session"),
    ],
)
def test_a_document_no_longer_listed_is_a_404_and_fetches_nothing(
    gateway: PronoteGateway,
    client: FakeClient,
    overrides: dict[str, Any],
    download: dict[str, Any],
) -> None:
    """Refused before the wire: this session's grades are the authority."""
    _graded(client)
    grade = _decoded(gateway, client)
    _graded(client, **overrides)
    period = current_period(gateway, client)

    with pytest.raises(AttachmentUnavailable) as refusal:
        gateway.grade_document(
            client,
            period_index=download.get("period_index", period.index),  # type: ignore[attr-defined]
            grade_id=download.get("grade_id", grade.id),
            role=GradeDocumentRole.CORRECTION,
            name="corrige.pdf",
        )

    assert refusal.value.status == 404
    assert client.communication.session.gets == []


def test_a_refused_download_is_an_error_and_not_an_error_page(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """An expired session answers PRONOTE's login page; that is not the answers."""
    _graded(client)
    client.communication.session.response.status_code = 403

    with pytest.raises(AttachmentUnavailable) as refusal:
        _download(gateway, client)

    assert refusal.value.status == 403


def test_a_documents_key_names_the_grade_the_role_and_the_file() -> None:
    """Paper and answers differ, and so do the answers before and after a fix.

    A grade's document has no identifier of its own and the grade's key
    ignores its documents, so the file name is what tells a replaced
    correction from the one it replaced -- and the byte cache is keyed by
    this fingerprint.
    """
    subject = grade_fingerprint("grade-1", GradeDocumentRole.SUBJECT, "sujet.pdf")
    first = grade_fingerprint("grade-1", GradeDocumentRole.CORRECTION, "corrige.pdf")
    fixed = grade_fingerprint("grade-1", GradeDocumentRole.CORRECTION, "corrige-v2.pdf")

    assert len({subject, first, fixed}) == 3


def test_a_remark_written_after_the_mark_keeps_the_grade(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """The remark is not part of the key: a teacher's comment is not news."""
    _graded(client)
    before = _decoded(gateway, client).id
    _graded(client, remark="Bon travail")

    assert _decoded(gateway, client).id == before


def test_replaced_answers_are_not_served_under_the_old_key(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """The relay matches the file this session lists, not just its role.

    A key read before the replacement names ``corrige.pdf``; the new answers
    are ``corrige-v2.pdf``. Serving the new file under the old key would put
    it in the cache under a fingerprint that never named it.
    """
    _graded(client)
    grade = _decoded(gateway, client)
    _graded(client, correction_file="corrige-v2.pdf")
    period = current_period(gateway, client)

    with pytest.raises(AttachmentUnavailable) as refusal:
        gateway.grade_document(
            client,
            period_index=period.index,  # type: ignore[attr-defined]
            grade_id=grade.id,
            role=GradeDocumentRole.CORRECTION,
            name="corrige.pdf",
        )

    assert refusal.value.status == 404
    assert client.communication.session.gets == []


def test_a_transport_error_never_carries_the_address(
    gateway: PronoteGateway, client: FakeClient
) -> None:
    """``requests`` writes the URL into its message; that must not escape.

    An exception escaping the view is logged with its message, and this
    message would hold an address that opens the document without
    credentials. Re-raised bare, as a status.
    """
    import requests

    _graded(client)

    def unreachable(url: str) -> None:
        raise requests.ConnectionError(f"Max retries exceeded with url: {url}")

    client.communication.session.get = unreachable  # type: ignore[method-assign]

    with pytest.raises(AttachmentUnavailable) as refusal:
        _download(gateway, client)

    assert refusal.value.status == 502
    assert refusal.value.__cause__ is None
    assert refusal.value.__suppress_context__
    assert "FichiersExternes" not in str(refusal.value)


# ---------------------------------------------------------------------------
# Through a real account
# ---------------------------------------------------------------------------


@REQUIRES_HASS
class TestAGradedTestsDocumentsReachTheCard:
    """Published as keys, resolved against the marks snapshot, charged honestly.

    Each child is served a *different* graded test, so a lookup that ignored
    the child would be caught: with one shared grade, both children would
    hold the same key and the cross-child refusal would prove nothing.
    """

    @pytest.fixture(name="parent_client")
    def parent_client_fixture(self) -> FakeClient:
        """The first child's test has both documents; the second's, other ones."""
        from .fixtures.client import FakeClient

        client = FakeClient(children=CHILDREN)

        def per_child(_body: Any) -> dict[str, Any]:
            if client.selected_child_id == CHILDREN[0][0]:
                return protocol.marks_response(
                    grades=[
                        protocol.grade(
                            subject_file="sujet.pdf",
                            correction_file="corrige.pdf",
                            remark="Bon travail",
                            in_groups=True,
                            out_of_20=True,
                        )
                    ]
                )
            return protocol.marks_response(
                grades=[
                    protocol.grade(
                        identifier="GRADE-2",
                        comment="Évaluation de grammaire",
                        subject="Français",
                        subject_id="SUBJECT-FR",
                        correction_file="corrige-francais.pdf",
                    )
                ]
            )

        client.responses["DernieresNotes"] = per_child
        return client

    @staticmethod
    def _item(
        hass: HomeAssistant, entity_id: str = "sensor.enfant_un_grades"
    ) -> dict[str, Any]:
        state = hass.states.get(entity_id)
        assert state is not None
        item: dict[str, Any] = state.attributes["items"][0]
        return item

    async def test_the_documents_are_offered_as_keys_with_their_role(
        self, hass: HomeAssistant, account: PronoteAccount
    ) -> None:
        """The homework shape, plus ``role``, and nothing that opens anything."""
        del account
        refs = self._item(hass)["attachment_refs"]

        assert [(ref["name"], ref["kind"], ref["role"]) for ref in refs] == [
            ("sujet.pdf", "local", "subject"),
            ("corrige.pdf", "local", "correction"),
        ]
        assert all(set(ref) == {"name", "kind", "key", "role"} for ref in refs)
        payload = repr(refs)
        assert "FichiersExternes" not in payload
        assert "Session=" not in payload
        assert "GRADE-1" not in payload

    async def test_every_field_pronote_sends_reaches_the_card(
        self, hass: HomeAssistant, account: PronoteAccount
    ) -> None:
        """Read off the attribute with non-default values, so none is hard-coded.

        And no ``subject_id``: on a grade it is the session's ``N`` for the
        subject, which changes at every login and would rewrite the attribute
        with nothing new to say.
        """
        del account
        item = self._item(hass)

        assert item["min"] == 4.0
        assert item["max"] == 18.0
        assert item["remark"] == "Bon travail"
        assert item["subject_in_groups"] is True
        assert item["is_out_of_20"] is True
        assert item["default_out_of"] == 20.0
        assert "subject_id" not in item

    async def test_one_childs_key_names_nothing_for_the_other_child(
        self, hass: HomeAssistant, account: PronoteAccount
    ) -> None:
        """The scan holds to the child it is given, sibling included."""
        key = self._item(hass)["attachment_refs"][1]["key"]

        found = _locate_grade_document(account, key, student_id=CHILDREN[0][0])

        assert found is not None
        assert found[0] == CHILDREN[0][0]
        assert found[3].role is GradeDocumentRole.CORRECTION
        assert _locate_grade_document(account, key, student_id=CHILDREN[1][0]) is None
        assert resolve(account, key, student_id=CHILDREN[1][0]) is None

    async def test_a_closed_periods_document_is_found_and_read_by_its_position(
        self, hass: HomeAssistant, account: PronoteAccount
    ) -> None:
        """A card shows closed periods too; their documents resolve the same way.

        The closed period is named by its position, the one handle on a period
        that survives a login, and that is what the relay is handed.
        """
        del hass
        import dataclasses
        from unittest.mock import patch

        from custom_components.carnet_scolaire.models import HistoryFacts

        current = account.snapshot(Tier.MARKS, CHILDREN[0][0])
        assert current is not None
        closed = dataclasses.replace(current.data, period_index=1)
        history = dataclasses.replace(
            current,
            data=HistoryFacts(marks=(closed,), attendance=(), evaluations=()),
            tier=Tier.HISTORY,
        )
        grade = closed.grades[0]
        document = grade.documents[0]
        key = grade_fingerprint(grade.id, document.role, document.name)
        real = account.snapshot

        def only_history(tier: Tier, student_id: str) -> Any:
            if tier is Tier.HISTORY:
                return history
            if tier is Tier.MARKS:
                return None
            return real(tier, student_id)

        with patch.object(account, "snapshot", only_history):
            found = _locate_grade_document(account, key, student_id=CHILDREN[0][0])

        assert found is not None
        assert found[1] == 1
        assert found[2] == grade.id

    async def test_opening_the_answers_is_charged_two_requests_to_the_limiter(
        self, hass: HomeAssistant, account: PronoteAccount, parent_client: FakeClient
    ) -> None:
        """The relay's GET bypasses ``ClientBase.post``; the declaration covers it."""
        key = self._item(hass)["attachment_refs"][1]["key"]
        resolved = resolve(account, key, student_id=CHILDREN[0][0])
        assert resolved is not None
        name, download = resolved

        before = account.limiter.calls_today
        reads = parent_client.posted_names.count("DernieresNotes")
        content, content_type = await download()

        assert name == "corrige.pdf"
        assert content == b"%PDF-1.4 not a real document"
        assert content_type == "application/pdf"
        assert account.limiter.calls_today - before == 2
        assert parent_client.posted_names.count("DernieresNotes") == reads + 1
        assert len(parent_client.communication.session.gets) == 1

    async def test_a_click_on_the_answers_is_a_gesture(
        self, hass: HomeAssistant, account: PronoteAccount
    ) -> None:
        """Like a homework file: it crosses quiet hours, and nothing more.

        A parent opening the answers at 22:30 is not the automatic collection
        quiet hours hold back; ``HIGH`` here would refuse them all evening.
        """
        from unittest.mock import patch

        from custom_components.carnet_scolaire.const import Priority

        key = self._item(hass)["attachment_refs"][1]["key"]
        resolved = resolve(account, key, student_id=CHILDREN[0][0])
        assert resolved is not None
        assert account.extras is not None
        seen: list[tuple[str, Priority]] = []
        original = account.extras.session.run

        async def spy(name: str, priority: Priority, fn: Any, **kwargs: Any) -> Any:
            seen.append((name, priority))
            return await original(name, priority, fn, **kwargs)

        with patch.object(account.extras.session, "run", spy):
            await resolved[1]()

        assert seen == [(str(Tier.MARKS), Priority.GESTURE)]

    async def test_the_view_serves_the_answers_inline_and_once(
        self, hass: HomeAssistant, account: PronoteAccount, parent_client: FakeClient
    ) -> None:
        """Through the real handler: the name, the bytes, and the cache."""
        from custom_components.carnet_scolaire.attachment import PronoteAttachmentView

        from .test_attachment import _FakeRequest

        key = self._item(hass)["attachment_refs"][1]["key"]
        view = PronoteAttachmentView()

        first = await view.get(
            _FakeRequest(hass),  # type: ignore[arg-type]
            account.entry.entry_id,
            key,
        )
        second = await view.get(
            _FakeRequest(hass),  # type: ignore[arg-type]
            account.entry.entry_id,
            key,
        )

        assert first.status == 200
        assert first.headers["Content-Disposition"] == 'inline; filename="corrige.pdf"'
        assert second.body == first.body
        assert len(parent_client.communication.session.gets) == 1

    async def test_a_path_that_is_not_ascii_is_a_404_not_a_traceback(
        self, hass: HomeAssistant, account: PronoteAccount
    ) -> None:
        """``compare_digest`` raises on such a ``str``; the browser sends anything."""
        from custom_components.carnet_scolaire.attachment import PronoteAttachmentView

        from .test_attachment import _FakeRequest

        response = await PronoteAttachmentView().get(
            _FakeRequest(hass),  # type: ignore[arg-type]
            account.entry.entry_id,
            "é" * 16,
        )

        assert response.status == 404

    async def test_waiting_is_only_advised_for_a_tier_that_will_collect(
        self, hass: HomeAssistant, account: PronoteAccount
    ) -> None:
        """Marks missing means "wait"; marks switched off means "unknown".

        The card tells the user to wait on `attachment_not_collected`. A tier
        disabled in the options never collects, so counting it would make that
        advice permanent and false.
        """
        del hass
        import dataclasses
        from unittest.mock import PropertyMock, patch

        from custom_components.carnet_scolaire.attachment import pending

        real = account.snapshot

        def no_marks(tier: Tier, student_id: str) -> Any:
            return None if tier is Tier.MARKS else real(tier, student_id)

        plans = account.scheduler.plans
        switched_off = {
            tier: dataclasses.replace(plan, enabled=tier is not Tier.MARKS)
            for tier, plan in plans.items()
        }

        with patch.object(account, "snapshot", no_marks):
            assert pending(account, CHILDREN[0][0]) is True
            with patch.object(
                type(account.scheduler),
                "plans",
                new_callable=PropertyMock,
                return_value=switched_off,
            ):
                assert pending(account, CHILDREN[0][0]) is False

    async def test_a_source_with_no_pronote_session_cannot_download_at_all(
        self, hass: HomeAssistant, account: PronoteAccount
    ) -> None:
        """EcoleDirecte has no session to fetch through, and says so with 501."""
        from unittest.mock import patch

        from custom_components.carnet_scolaire.attachment import (
            _fetch_grade_document,
        )

        del hass
        snapshot = account.snapshot(Tier.MARKS, CHILDREN[0][0])
        assert snapshot is not None
        grade = snapshot.data.grades[0]

        with (
            patch(
                "custom_components.carnet_scolaire.account.has_pronote_extras",
                return_value=False,
            ),
            pytest.raises(AttachmentUnavailable) as raised,
        ):
            await _fetch_grade_document(
                account,
                grade.documents[0],
                snapshot.data.period_index,
                grade.id,
                CHILDREN[0][0],
            )

        assert raised.value.status == 501
