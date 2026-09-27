"""The fixed description of a transport failure (``failures.py``).

HA-free on purpose: the classification is a pure function of an exception's
type, and it is the one thing standing between a ``requests`` message -- the
establishment's host, the page path and its parameters -- and the log a user
pastes into a public issue.
"""

from __future__ import annotations

import errno
import socket
import ssl
from unittest.mock import MagicMock

from aiohttp import (
    ClientConnectionError,
    ClientError,
    ClientResponseError,
    ClientSSLError,
    ServerTimeoutError,
)
import pytest
import requests

from custom_components.carnet_scolaire.connectors.errors import (
    ConnectorError,
    ConnectorTransportError,
)
from custom_components.carnet_scolaire.failures import (
    describe_failure,
    is_transport_failure,
)

_LEAKY = (
    "HTTPSConnectionPool(host='demo.example.invalid', port=443): Max retries "
    "exceeded with url: /pronote/parent.html?identifiant=NOT-A-REAL-SESSION"
)


def _http_error(status: int | None) -> requests.HTTPError:
    if status is None:
        return requests.HTTPError(_LEAKY)
    response = requests.Response()
    response.status_code = status
    return requests.HTTPError(_LEAKY, response=response)


def _ssl_client_error() -> ClientSSLError:
    return ClientSSLError(MagicMock(), OSError(_LEAKY))


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        pytest.param(requests.ConnectTimeout(_LEAKY), "ConnectTimeout (timeout)"),
        pytest.param(requests.ReadTimeout(_LEAKY), "ReadTimeout (timeout)"),
        pytest.param(TimeoutError(_LEAKY), "TimeoutError (timeout)"),
        pytest.param(ServerTimeoutError(_LEAKY), "ServerTimeoutError (timeout)"),
        pytest.param(requests.exceptions.SSLError(_LEAKY), "SSLError (ssl)"),
        pytest.param(ssl.SSLError(_LEAKY), "SSLError (ssl)"),
        pytest.param(_ssl_client_error(), "ClientSSLError (ssl)"),
        pytest.param(socket.gaierror(_LEAKY), "gaierror (dns)"),
        pytest.param(_http_error(503), "HTTPError (http 503)"),
        pytest.param(_http_error(None), "HTTPError (http)"),
        pytest.param(
            ClientResponseError(MagicMock(), (), status=502, message=_LEAKY),
            "ClientResponseError (http 502)",
        ),
        pytest.param(requests.ConnectionError(_LEAKY), "ConnectionError (connection)"),
        pytest.param(
            ConnectionRefusedError(_LEAKY), "ConnectionRefusedError (connection)"
        ),
        pytest.param(
            ClientConnectionError(_LEAKY), "ClientConnectionError (connection)"
        ),
        pytest.param(requests.TooManyRedirects(_LEAKY), "TooManyRedirects (request)"),
        pytest.param(ClientError(_LEAKY), "ClientError (request)"),
        pytest.param(
            OSError(errno.ENETUNREACH, _LEAKY), "OSError (os error ENETUNREACH)"
        ),
        pytest.param(OSError(99999, _LEAKY), "OSError (os error)"),
        pytest.param(OSError(_LEAKY), "OSError (os error)"),
        pytest.param(RuntimeError(_LEAKY), "RuntimeError"),
    ],
)
def test_a_failure_is_described_by_its_type_and_never_by_its_message(
    error: BaseException, expected: str
) -> None:
    """The description is a class name and a category, whatever the text says.

    ``str(error)`` of a ``requests`` failure quotes the host and the path, and
    it used to be what the unavailability line, the set-up reason and a DEBUG
    traceback printed. Each row feeds that text in and asserts it comes out as
    nothing but a fixed vocabulary.
    """
    described = describe_failure(error)

    assert described == expected
    assert "demo.example.invalid" not in described
    assert "NOT-A-REAL-SESSION" not in described


def test_a_wrapped_failure_takes_its_category_from_the_cause() -> None:
    """EcoleDirecte raises its own error *from* the ``aiohttp`` one.

    Its fixed message ("EcoleDirecte request failed") is safe but says nothing
    about why; the category of the cause is the useful part, so both classes
    are named.
    """
    cause = ClientConnectionError(_LEAKY)
    error = ConnectorTransportError("EcoleDirecte request failed")
    error.__cause__ = cause

    assert describe_failure(error) == (
        "ConnectorTransportError <- ClientConnectionError (connection)"
    )


def test_a_transport_failure_is_recognised_anywhere_in_its_chain() -> None:
    """The traceback prints every link, so one transport link is enough.

    A connector error with no transport cause -- a GTK cookie that did not come
    back -- is still a transport failure by declaration, and a plain bug with
    no transport link is not.
    """
    outer = ConnectorError("wrapped")
    outer.__context__ = OSError(_LEAKY)

    assert is_transport_failure(outer)
    assert is_transport_failure(ConnectorTransportError("no GTK cookie"))
    assert not is_transport_failure(RuntimeError("a bug"))
    assert not is_transport_failure(ConnectorError("undecodable"))


def test_a_cyclic_chain_terminates() -> None:
    """``__context__`` can loop back; walking it must not hang a log line."""
    first = RuntimeError("first")
    second = RuntimeError("second")
    first.__context__ = second
    second.__context__ = first

    assert describe_failure(first) == "RuntimeError"
    assert not is_transport_failure(first)
