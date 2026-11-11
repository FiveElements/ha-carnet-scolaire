"""Auditable HTTP client for the EcoleDirecte protocol."""

from __future__ import annotations

import asyncio
import base64
import json
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import quote

from aiohttp import ClientError, ClientTimeout

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

from ..errors import (  # noqa: TID252
    ConnectorChallengeRequired,
    ConnectorCredentialsError,
    ConnectorSessionExpiredError,
    ConnectorTransportError,
    ConnectorUndecodableError,
)
from ..protocol import ChallengeKind  # noqa: TID252

API_BASE = "https://api.ecoledirecte.com/v3"
ECOLEDIRECTE_API_VERSION = "4.104.3"
DEFAULT_READ_TIMEOUT = 60.0

_BASE_HEADERS = {
    "accept": "application/json, text/plain, */*",
    "accept-encoding": "gzip, deflate, br, zstd",
    "accept-language": "fr-FR,fr;q=0.9",
    "connection": "keep-alive",
    "content-type": "application/x-www-form-urlencoded",
    "dnt": "1",
    "origin": "https://www.ecoledirecte.com",
    "priority": "1",
    "referer": "https://www.ecoledirecte.com/",
    "sec-ch-ua": ('"Chromium";v="134", "Not:A-Brand";v="24", "Google Chrome";v="134"'),
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-site",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/58.0.3029.110 Safari/537.3"
    ),
}


class Response(Protocol):
    """Response surface required from aiohttp or a test transport."""

    headers: Mapping[str, str]
    cookies: Mapping[str, object]

    async def json(self, *, content_type: None = None) -> dict[str, Any]: ...


class Transport(Protocol):
    """Injectable HTTP boundary used by the client."""

    def request(
        self,
        method: str,
        url: str,
        **kwargs: object,
    ) -> Awaitable[Response]: ...


class EcoleDirecteClient:
    """Small stateful client that owns GTK and token headers."""

    __slots__ = ("_headers", "_read_timeout", "_transport", "calls", "token")

    def __init__(
        self,
        transport: Transport,
        *,
        read_timeout: float = DEFAULT_READ_TIMEOUT,
    ) -> None:
        self._transport = transport
        self._read_timeout = read_timeout
        self._headers = dict(_BASE_HEADERS)
        self.calls = 0
        self.token: str | None = None

    @property
    def read_timeout(self) -> float:
        """Deadline applied to every aiohttp call and ``wait_for``."""
        return self._read_timeout

    def configure_read_timeout(self, read_timeout: float) -> None:
        """Apply the entry deadline after construction."""
        self._read_timeout = read_timeout

    def __repr__(self) -> str:
        """Represent only non-secret client state."""
        return (
            f"{type(self).__name__}("
            f"calls={self.calls}, authenticated={self.token is not None})"
        )

    async def login(
        self,
        username: str,
        password: str,
        qcm_json: Mapping[str, Any] | None = None,
        *,
        charge_qcm: Callable[[], Awaitable[None]] | None = None,
    ) -> dict[str, Any]:
        """Fetch GTK, submit credentials, and continue a 250 through the QCM."""
        result = await self._handshake_login(username, password)
        if result.get("code") == 250 and qcm_json is not None:
            if charge_qcm is not None:
                await charge_qcm()
            return await self._complete_qcm(username, password, qcm_json)
        self._check_login_code(result)
        self._headers.pop("x-gtk", None)
        return result

    async def _handshake_login(
        self,
        username: str,
        password: str,
        extra: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """GTK + credential POST. Does not classify 250 so the caller can continue."""
        self._headers.pop("x-gtk", None)
        gtk_response = await self._send(
            "GET",
            (f"{API_BASE}/login.awp?v={ECOLEDIRECTE_API_VERSION}&gtk=1"),
            data=None,
        )
        gtk = _cookie_value(gtk_response.cookies.get("GTK"))
        if gtk is None:
            raise ConnectorTransportError("EcoleDirecte did not return a GTK cookie")
        self._headers["x-gtk"] = gtk

        body: dict[str, Any] = {
            "identifiant": username,
            "motdepasse": password,
            "isReLogin": False,
        }
        if extra is not None:
            body.update(extra)
        response, result = await self._send_json(
            "POST",
            f"{API_BASE}/login.awp?v={ECOLEDIRECTE_API_VERSION}",
            data=_encode_data(body),
        )
        self._remember_response_tokens(response)
        return result

    async def _complete_qcm(
        self,
        username: str,
        password: str,
        qcm_json: Mapping[str, Any],
    ) -> dict[str, Any]:
        """GET the question, POST a known choice, then GTK + relogin with cn/cv."""
        quiz = await self.request("/connexion/doubleauth.awp", verbe="get", data={})
        payload = quiz["data"]
        question = _decode_b64(payload["question"])
        propositions = tuple(_decode_b64(item) for item in payload["propositions"])
        answer = qcm_json.get(question)
        if not isinstance(answer, str):
            raise ConnectorChallengeRequired(
                ChallengeKind.QCM,
                question=question,
                propositions=propositions,
            )
        posted = await self.request(
            "/connexion/doubleauth.awp",
            verbe="post",
            data={"choix": base64.b64encode(answer.encode()).decode()},
        )
        factor = posted["data"]
        result = await self._handshake_login(
            username,
            password,
            extra={
                "cn": factor["cn"],
                "cv": factor["cv"],
                "uuid": "",
                "fa": [{"cn": factor["cn"], "cv": factor["cv"]}],
            },
        )
        self._check_login_code(result)
        self._headers.pop("x-gtk", None)
        return result

    def _check_login_code(self, result: Mapping[str, Any]) -> None:
        """Enrolment succeeds only on JSON 200; 210 is not a login."""
        if result.get("code") == 210:
            raise ConnectorUndecodableError(
                "EcoleDirecte login returned business code 210"
            )
        self._check_code(result)

    async def request(
        self,
        path: str,
        *,
        verbe: str,
        data: Mapping[str, Any],
    ) -> dict[str, Any]:
        """POST one protocol request and classify its JSON business code."""
        response, result = await self._send_json(
            "POST",
            f"{API_BASE}{path}",
            params={"verbe": verbe, "v": ECOLEDIRECTE_API_VERSION},
            data=_encode_data(data),
        )
        self._remember_response_tokens(response)
        self._check_code(result)
        return result

    async def _send(
        self,
        method: str,
        url: str,
        **kwargs: object,
    ) -> Response:
        self.calls += 1
        try:
            request = self._transport.request(
                method,
                url,
                headers=dict(self._headers),
                timeout=ClientTimeout(total=self._read_timeout),
                **kwargs,
            )
            return await asyncio.wait_for(request, timeout=self._read_timeout)
        except (ClientError, OSError, TimeoutError) as error:
            raise ConnectorTransportError("EcoleDirecte request failed") from error

    async def _send_json(
        self,
        method: str,
        url: str,
        **kwargs: object,
    ) -> tuple[Response, dict[str, Any]]:
        async def request_and_read() -> tuple[Response, dict[str, Any]]:
            self.calls += 1
            response = await self._transport.request(
                method,
                url,
                headers=dict(self._headers),
                timeout=ClientTimeout(total=self._read_timeout),
                **kwargs,
            )
            result = await response.json(content_type=None)
            return response, result

        try:
            response, result = await asyncio.wait_for(
                request_and_read(),
                timeout=self._read_timeout,
            )
        except (ClientError, OSError, TimeoutError) as error:
            raise ConnectorTransportError("EcoleDirecte request failed") from error
        except (TypeError, ValueError) as error:
            raise ConnectorUndecodableError(
                "EcoleDirecte returned invalid JSON"
            ) from error
        if not isinstance(result, dict):
            raise ConnectorUndecodableError(
                "EcoleDirecte returned a non-object payload"
            )
        return response, result

    def _remember_response_tokens(self, response: Response) -> None:
        token = _header(response.headers, "x-token")
        if token is not None:
            self.token = token
            self._headers["x-token"] = token
        token_2fa = _header(response.headers, "2FA-Token")
        if token_2fa is not None:
            self._headers["2FA-Token"] = token_2fa

    def _forget_tokens(self) -> None:
        self.token = None
        self._headers.pop("x-token", None)
        self._headers.pop("2FA-Token", None)

    def _check_code(self, result: Mapping[str, Any]) -> None:
        code = result.get("code")
        if code in {200, 210}:
            return
        if code == 250:
            raise ConnectorChallengeRequired(ChallengeKind.QCM)
        if code == 505:
            raise ConnectorCredentialsError("EcoleDirecte rejected the credentials")
        if code == 517:
            raise ConnectorUndecodableError(
                "EcoleDirecte rejected the declared API version"
            )
        if code in {520, 525}:
            self._forget_tokens()
            raise ConnectorSessionExpiredError(
                "EcoleDirecte session token is no longer valid"
            )
        raise ConnectorUndecodableError(
            f"EcoleDirecte returned an unsupported business code: {code!r}"
        )


def _encode_data(data: Mapping[str, Any]) -> str:
    serialized = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return "data=" + quote(serialized, safe="")


def _cookie_value(cookie: object | None) -> str | None:
    if cookie is None:
        return None
    value = getattr(cookie, "value", cookie)
    return value if isinstance(value, str) else None


def _header(headers: Mapping[str, str], name: str) -> str | None:
    wanted = name.casefold()
    return next(
        (value for key, value in headers.items() if key.casefold() == wanted),
        None,
    )


def _decode_b64(value: str) -> str:
    return base64.b64decode(value).decode()
