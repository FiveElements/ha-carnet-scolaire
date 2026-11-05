"""House HTTP client contract for EcoleDirecte."""

# ruff: noqa: E501 -- Task 7 requires the three plan tests verbatim.

import asyncio
import base64
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Self
from urllib.parse import parse_qs

from aiohttp import ClientPayloadError
import pytest

from custom_components.carnet_scolaire.connectors.ecoledirecte.ed_client import (
    ECOLEDIRECTE_API_VERSION,
    EcoleDirecteClient,
)
from custom_components.carnet_scolaire.connectors.errors import (
    ConnectorChallengeRequired,
    ConnectorCredentialsError,
    ConnectorError,
    ConnectorSessionExpiredError,
    ConnectorTransportError,
    ConnectorUndecodableError,
)
from custom_components.carnet_scolaire.connectors.protocol import ChallengeKind

FIXTURES = Path(__file__).parent / "fixtures" / "ecoledirecte"


def load_fixture(name: str) -> dict[str, Any]:
    """Load one hand-written EcoleDirecte protocol response."""
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@dataclass(slots=True)
class FakeResponse:
    """Minimal response shape consumed by the house client."""

    payload: dict[str, Any]
    headers: dict[str, str]
    cookies: dict[str, str]
    json_delay: float = 0
    json_error: Exception | None = None

    async def json(self, *, content_type: None = None) -> dict[str, Any]:
        """Return the scripted JSON response."""
        await asyncio.sleep(self.json_delay)
        if self.json_error is not None:
            raise self.json_error
        return self.payload


@dataclass(slots=True)
class StaticTransport:
    """Return one response while keeping the HTTP call itself immediate."""

    response: FakeResponse

    async def request(
        self,
        method: str,
        url: str,
        **kwargs: object,
    ) -> FakeResponse:
        """Return the response whose body behavior is under test."""
        return self.response


class RecordingTransport:
    """Strict scripted transport that records the emitted HTTP contract."""

    def __init__(
        self,
        script: Sequence[tuple[str, str, object]],
    ) -> None:
        self._script = list(script)
        self.urls: list[str] = []
        self.headers_on: list[dict[str, str]] = []
        self.data_on: list[object] = []

    @classmethod
    def scripted(
        cls,
        script: Sequence[tuple[str, str, object]],
    ) -> Self:
        """Build a transport from ordered method/path/payload triples."""
        return cls(script)

    async def request(
        self,
        method: str,
        url: str,
        **kwargs: object,
    ) -> FakeResponse:
        """Record one call and return its exact scripted response."""
        expected_method, expected_path, payload = self._script.pop(0)
        assert method == expected_method
        assert expected_path in url
        headers = kwargs.get("headers", {})
        assert isinstance(headers, Mapping)
        self.urls.append(url)
        self.headers_on.append(dict(headers))
        self.data_on.append(kwargs.get("data"))
        business_payload = (
            payload
            if isinstance(payload, dict) and "code" in payload
            else {"code": 200, "data": payload}
        )
        response_headers = (
            {"x-token": "not-a-real-token"}
            if business_payload.get("code") in {200, 250}
            else {}
        )
        cookies = (
            {"GTK": str(payload["gtk"])}
            if isinstance(payload, dict) and "gtk" in payload
            else {}
        )
        return FakeResponse(business_payload, response_headers, cookies)


@pytest.mark.asyncio
async def test_a_login_fetches_gtk_before_posting_credentials() -> None:
    """Aplim treats a missing GTK as a wrong password, which would trip the IP-like hold."""
    transport = RecordingTransport.scripted(
        [
            ("GET", "login.awp", {"gtk": "gtk-demo"}),
            ("POST", "login.awp", load_fixture("login_ok.json")),
        ]
    )
    client = EcoleDirecteClient(transport)
    await client.login("demo.example.invalid", "not-a-real-password")
    assert transport.urls[0].endswith("gtk=1")
    assert transport.headers_on[1].get("x-gtk") == "gtk-demo"
    assert client.calls == 2
    assert ECOLEDIRECTE_API_VERSION  # named constant used in both URLs


@pytest.mark.asyncio
async def test_a_505_is_credentials_and_not_transport() -> None:
    """The HTTP status is 200; classifying on it would never increment the hold."""
    transport = RecordingTransport.scripted(
        [
            ("GET", "login.awp", {"gtk": "gtk-demo"}),
            ("POST", "login.awp", load_fixture("login_505.json")),
        ]
    )
    client = EcoleDirecteClient(transport)
    with pytest.raises(ConnectorCredentialsError):
        await client.login("demo.example.invalid", "wrong")


@pytest.mark.asyncio
async def test_a_250_keeps_the_token_for_the_qcm() -> None:
    """The unofficial docs require the login token to answer the quiz."""
    transport = RecordingTransport.scripted(
        [
            ("GET", "login.awp", {"gtk": "gtk-demo"}),
            ("POST", "login.awp", load_fixture("login_250.json")),
        ]
    )
    client = EcoleDirecteClient(transport)
    with pytest.raises(ConnectorChallengeRequired) as caught:
        await client.login("demo.example.invalid", "not-a-real-password")
    assert caught.value.kind is ChallengeKind.QCM
    assert client.token == "not-a-real-token"


@pytest.mark.asyncio
async def test_an_unknown_qcm_question_exposes_decoded_choices() -> None:
    """The flow needs readable choices before it can ask a human for one."""
    question = "Couleur préférée ?"
    propositions = ("Bleu", "Vert")
    transport = RecordingTransport.scripted(
        [
            ("GET", "login.awp", {"gtk": "gtk-demo"}),
            ("POST", "login.awp", load_fixture("login_250.json")),
            (
                "POST",
                "doubleauth.awp",
                {
                    "code": 200,
                    "data": {
                        "question": base64.b64encode(question.encode()).decode(),
                        "propositions": [
                            base64.b64encode(value.encode()).decode()
                            for value in propositions
                        ],
                    },
                },
            ),
        ]
    )
    client = EcoleDirecteClient(transport)

    with pytest.raises(ConnectorChallengeRequired) as caught:
        await client.login(
            "demo.example.invalid",
            "not-a-real-password",
            qcm_json={},
        )

    assert caught.value.question == question
    assert caught.value.propositions == propositions
    assert client.calls == 3


@pytest.mark.asyncio
async def test_a_known_qcm_answer_completes_the_login_with_four_more_calls() -> None:
    """A stored answer must drive doubleauth and the final 200 login."""
    question = "Couleur préférée ?"
    answer = "Bleu"
    encoded_question = base64.b64encode(question.encode()).decode()
    encoded_answer = base64.b64encode(answer.encode()).decode()
    transport = RecordingTransport.scripted(
        [
            ("GET", "login.awp", {"gtk": "gtk-demo"}),
            ("POST", "login.awp", load_fixture("login_250.json")),
            (
                "POST",
                "doubleauth.awp",
                {
                    "code": 200,
                    "data": {
                        "question": encoded_question,
                        "propositions": [encoded_answer],
                    },
                },
            ),
            (
                "POST",
                "doubleauth.awp",
                {"code": 200, "data": {"cn": "not-a-real-cn", "cv": "not-a-real-cv"}},
            ),
            ("GET", "login.awp", {"gtk": "gtk-after-qcm"}),
            ("POST", "login.awp", load_fixture("login_ok.json")),
        ]
    )
    client = EcoleDirecteClient(transport)

    result = await client.login(
        "demo.example.invalid",
        "not-a-real-password",
        qcm_json={question: answer},
    )

    assert result["code"] == 200
    assert client.calls == 6
    assert json.loads(parse_qs(str(transport.data_on[3]))["data"][0]) == {
        "choix": encoded_answer
    }
    assert json.loads(parse_qs(str(transport.data_on[5]))["data"][0]) == {
        "identifiant": "demo.example.invalid",
        "motdepasse": "not-a-real-password",
        "isReLogin": False,
        "cn": "not-a-real-cn",
        "cv": "not-a-real-cv",
        "uuid": "",
        "fa": [{"cn": "not-a-real-cn", "cv": "not-a-real-cv"}],
    }


@pytest.mark.asyncio
async def test_login_rejects_210_while_collection_accepts_it() -> None:
    """An empty collection is valid, but an empty login cannot name an account."""
    login_transport = RecordingTransport.scripted(
        [
            ("GET", "login.awp", {"gtk": "gtk-demo"}),
            ("POST", "login.awp", {"code": 210, "data": {}}),
        ]
    )
    with pytest.raises(ConnectorUndecodableError, match=r"login.*210"):
        await EcoleDirecteClient(login_transport).login(
            "demo.example.invalid", "not-a-real-password"
        )

    collection_client = EcoleDirecteClient(
        StaticTransport(FakeResponse({"code": 210, "data": {}}, {}, {}))
    )
    assert (await collection_client.request("/empty.awp", verbe="get", data={}))[
        "code"
    ] == 210


@pytest.mark.asyncio
async def test_qcm_continuation_is_charged_before_the_get() -> None:
    """Admission must precede the GET: a late charge lets a second caller through."""
    charged_after: list[int] = []

    async def charge_qcm() -> None:
        charged_after.append(client.calls)

    transport = RecordingTransport.scripted(
        [
            ("GET", "login.awp", {"gtk": "gtk-demo"}),
            ("POST", "login.awp", load_fixture("login_250.json")),
            (
                "POST",
                "doubleauth.awp",
                {
                    "code": 200,
                    "data": {
                        "question": base64.b64encode(
                            "Couleur préférée ?".encode()
                        ).decode(),
                        "propositions": [base64.b64encode(b"Bleu").decode()],
                    },
                },
            ),
        ]
    )
    client = EcoleDirecteClient(transport)

    with pytest.raises(ConnectorChallengeRequired):
        await client.login(
            "demo.example.invalid",
            "not-a-real-password",
            qcm_json={},
            charge_qcm=charge_qcm,
        )

    assert charged_after == [2]


@pytest.mark.asyncio
async def test_login_form_encodes_the_complete_json_object() -> None:
    """Quotes and literal percent escapes must survive form decoding as valid JSON."""
    transport = RecordingTransport.scripted(
        [
            ("GET", "login.awp", {"gtk": "gtk-demo"}),
            ("POST", "login.awp", load_fixture("login_ok.json")),
        ]
    )
    client = EcoleDirecteClient(transport)

    await client.login('demo"a%2Fb', "not-a-real-password")

    encoded_form = transport.data_on[1]
    assert isinstance(encoded_form, str)
    login_data = json.loads(parse_qs(encoded_form)["data"][0])
    assert login_data == {
        "identifiant": 'demo"a%2Fb',
        "motdepasse": "not-a-real-password",
        "isReLogin": False,
    }


@pytest.mark.asyncio
async def test_a_json_read_timeout_is_a_transport_error() -> None:
    """The read deadline must cover the body, not only response headers."""
    response = FakeResponse(
        {"code": 200},
        {},
        {},
        json_delay=0.02,
    )
    client = EcoleDirecteClient(StaticTransport(response), read_timeout=0.001)

    with pytest.raises(ConnectorTransportError):
        await client.request("/eleves/1/notes.awp", verbe="get", data={})


@pytest.mark.asyncio
async def test_a_payload_read_failure_is_a_transport_error() -> None:
    """An interrupted aiohttp body is a transport failure, not a raw library error."""
    response = FakeResponse(
        {"code": 200},
        {},
        {},
        json_error=ClientPayloadError("scripted body failure"),
    )
    client = EcoleDirecteClient(StaticTransport(response))

    with pytest.raises(ConnectorTransportError):
        await client.request("/eleves/1/notes.awp", verbe="get", data={})


def test_client_repr_never_contains_credential_material() -> None:
    """Debugging an entry must reveal only cost and authentication state."""
    client = EcoleDirecteClient(
        RecordingTransport.scripted([]),
    )
    assert repr(client) == "EcoleDirecteClient(calls=0, authenticated=False)"


@pytest.mark.asyncio
async def test_a_login_without_a_gtk_cookie_is_a_transport_error() -> None:
    """Submitting credentials without GTK would misclassify a bootstrap defect."""
    client = EcoleDirecteClient(StaticTransport(FakeResponse({"code": 200}, {}, {})))
    with pytest.raises(ConnectorTransportError, match="GTK"):
        await client.login("demo.example.invalid", "not-a-real-password")


@pytest.mark.asyncio
async def test_a_bootstrap_socket_error_is_a_transport_error() -> None:
    """The initial GET belongs to the same transport failure vocabulary."""

    class BrokenTransport:
        async def request(
            self, method: str, url: str, **kwargs: object
        ) -> FakeResponse:
            raise OSError("scripted failure")

    client = EcoleDirecteClient(BrokenTransport())
    with pytest.raises(ConnectorTransportError):
        await client.login("demo.example.invalid", "not-a-real-password")


@pytest.mark.asyncio
async def test_invalid_json_is_an_undecodable_response() -> None:
    """A protocol body defect is not a transport or credentials failure."""
    response = FakeResponse({"code": 200}, {}, {}, json_error=ValueError("bad"))
    client = EcoleDirecteClient(StaticTransport(response))
    with pytest.raises(ConnectorUndecodableError, match="invalid JSON"):
        await client.request("/eleves/1/notes.awp", verbe="get", data={})


@pytest.mark.asyncio
async def test_a_non_object_json_body_is_undecodable() -> None:
    """Business codes cannot be read safely from a JSON array."""
    response = FakeResponse([], {}, {})  # type: ignore[arg-type]
    client = EcoleDirecteClient(StaticTransport(response))
    with pytest.raises(ConnectorUndecodableError, match="non-object"):
        await client.request("/eleves/1/notes.awp", verbe="get", data={})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "exception"),
    [
        (517, ConnectorUndecodableError),
        (520, ConnectorSessionExpiredError),
        (525, ConnectorSessionExpiredError),
        (999, ConnectorUndecodableError),
    ],
)
async def test_business_error_codes_keep_their_connector_vocabulary(
    code: int, exception: type[ConnectorError]
) -> None:
    """Version, token, and unknown failures must not become bad passwords.

    Nor all become the same thing: an expired token has its own class because
    set-up classifies it differently. It is a session that ended, not a payload
    nobody can read, and it must not open a repair asking for a bug report.
    """
    client = EcoleDirecteClient(StaticTransport(FakeResponse({"code": code}, {}, {})))
    client.token = "not-a-real-token"
    with pytest.raises(exception):
        await client.request("/eleves/1/notes.awp", verbe="get", data={})
    if code in {520, 525}:
        assert client.token is None


@pytest.mark.asyncio
async def test_a_2fa_token_is_sent_on_the_following_request() -> None:
    """QCM continuation depends on retaining the dedicated response header."""

    class TwoStepTransport:
        def __init__(self) -> None:
            self.calls = 0
            self.last_headers: Mapping[str, str] = {}

        async def request(
            self, method: str, url: str, **kwargs: object
        ) -> FakeResponse:
            self.calls += 1
            headers = kwargs["headers"]
            assert isinstance(headers, Mapping)
            self.last_headers = headers
            if self.calls == 1:
                return FakeResponse(
                    {"code": 200},
                    {"2FA-Token": "not-a-real-2fa-token"},
                    {},
                )
            return FakeResponse({"code": 200}, {}, {})

    transport = TwoStepTransport()
    client = EcoleDirecteClient(transport)
    await client.request("/first.awp", verbe="get", data={})
    await client.request("/second.awp", verbe="get", data={})
    assert transport.last_headers["2FA-Token"] == "not-a-real-2fa-token"
