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
        name="corrige.pdf",
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


def test_the_two_documents_of_one_grade_have_two_keys() -> None:
    """A key names a document, so the paper and the answers cannot share one."""
    assert grade_fingerprint("grade-1", GradeDocumentRole.SUBJECT) != (
        grade_fingerprint("grade-1", GradeDocumentRole.CORRECTION)
    )


# ---------------------------------------------------------------------------
# Through a real account
# ---------------------------------------------------------------------------


@REQUIRES_HASS
class TestAGradedTestsDocumentsReachTheCard:
    """Published as keys, resolved against the marks snapshot, charged honestly."""

    @pytest.fixture(name="parent_client")
    def parent_client_fixture(self) -> FakeClient:
        """Both children hold one grade with its paper and answers."""
        from .fixtures.client import FakeClient

        client = FakeClient(children=CHILDREN)
        _graded(client, remark="Bon travail")
        return client

    @staticmethod
    def _item(hass: HomeAssistant) -> dict[str, Any]:
        state = hass.states.get("sensor.enfant_un_grades")
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
        """A card should not have to wait for a release to show a field PRONOTE has."""
        del account
        item = self._item(hass)

        assert item["min"] == 4.0
        assert item["max"] == 18.0
        assert item["remark"] == "Bon travail"
        assert item["subject_id"] == "SUBJECT-MATHS"
        assert item["subject_in_groups"] is False
        assert item["default_out_of"] == 20.0
        assert item["is_out_of_20"] is False

    async def test_a_published_key_resolves_to_its_document_for_its_child_only(
        self, hass: HomeAssistant, account: PronoteAccount
    ) -> None:
        """The marks snapshot is the authority, and the scan holds to one child."""
        key = self._item(hass)["attachment_refs"][1]["key"]

        found = _locate_grade_document(account, key, student_id=CHILDREN[0][0])

        assert found is not None
        student_id, _period_index, _grade_id, document = found
        assert student_id == CHILDREN[0][0]
        assert document.role is GradeDocumentRole.CORRECTION
        assert _locate_grade_document(account, key, student_id="NOT-HERE") is None
        assert resolve(account, "0" * 16) is None

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
