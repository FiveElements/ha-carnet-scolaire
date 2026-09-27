"""The fixed description of a failure (``failures.py``).

HA-free on purpose: the classification is a pure function of an exception's
type, and it is the one thing standing between a ``requests`` message -- the
establishment's host, the page path and its parameters -- or a decoding
error's -- the payload it could not read -- and the log a user pastes into a
public issue.
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
from pronotepy import dataClasses
from pronotepy.exceptions import DataError, ParsingError
import pytest
import requests

from custom_components.carnet_scolaire.connectors.errors import (
    ConnectorError,
    ConnectorTransportError,
    ConnectorUndecodableError,
)
from custom_components.carnet_scolaire.failures import (
    describe_failure,
    is_decoding_failure,
    is_traceback_safe,
    is_transport_failure,
    safe_cause,
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


# ---------------------------------------------------------------------------
# Decoding failures
# ---------------------------------------------------------------------------

#: What the text of a decoding error can carry: a fragment of the payload it
#: could not read. Both fictional; the tests below exist to show that neither
#: survives a description.
_NAME = "Enfant Un"
_N = "46#NOT-A-REAL-N"
_PAYLOAD_TEXT = f"invalid literal for int() with base 10: '{_NAME}' ({_N})"


def _parsing_error(path: tuple[str, ...]) -> ParsingError:
    """A ``ParsingError`` as ``pronotepy``'s resolver raises it, cause included."""
    error = ParsingError(
        f"Error while converting value: {_PAYLOAD_TEXT}",
        {"N": _N, "L": _NAME},
        path,
    )
    error.__cause__ = ValueError(_PAYLOAD_TEXT)
    return error


def _wrapped(outer: Exception, cause: BaseException) -> Exception:
    outer.__cause__ = cause
    return outer


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        pytest.param(KeyError(_N), "KeyError (decode)", id="key-that-is-a-value"),
        pytest.param(KeyError(_NAME), "KeyError (decode)", id="key-that-is-a-name"),
        pytest.param(
            KeyError("PremierLundi"),
            "KeyError (decode, field PremierLundi)",
            id="protocol-key",
        ),
        pytest.param(ValueError(_PAYLOAD_TEXT), "ValueError (decode)", id="value"),
        pytest.param(
            _parsing_error(("dateDemande", "V")),
            "ParsingError (decode, field dateDemande.V)",
            id="resolver-path",
        ),
        pytest.param(
            _parsing_error(("dateDemande", _N)),
            "ParsingError (decode)",
            id="path-that-is-not-a-name",
        ),
        pytest.param(DataError(_PAYLOAD_TEXT), "DataError (decode)", id="data-error"),
        pytest.param(
            _wrapped(RuntimeError(_PAYLOAD_TEXT), _parsing_error(("cle",))),
            "RuntimeError <- ParsingError (decode, field cle)",
            id="wrapped",
        ),
        pytest.param(
            _wrapped(ConnectorUndecodableError("fixed"), ValueError(_PAYLOAD_TEXT)),
            "ConnectorUndecodableError (decode)",
            id="connector",
        ),
        pytest.param(
            _wrapped(DataError(_PAYLOAD_TEXT), KeyError("N")),
            "DataError (decode, field N)",
            id="field-from-a-deeper-link",
        ),
    ],
)
def test_a_decoding_failure_is_described_without_the_data_it_could_not_read(
    error: BaseException, expected: str
) -> None:
    """The text of a decoding error is the payload, so it is never printed.

    ``pronotepy`` writes ``Error while converting value: <the converter's
    error>``, and the converter's error quotes the refused value; a
    ``KeyError`` names whatever was looked up, which is a value as often as a
    protocol key. That text reached the ERROR line of an undecodable login and
    the reason of the set-up refusal. What survives is the class, the
    category, and a field name only when it comes from code: the resolver's
    path, or a key on the fixed list.
    """
    described = describe_failure(error)

    assert described == expected
    assert _NAME not in described
    assert _N not in described
    assert is_decoding_failure(error)
    assert not is_transport_failure(error)
    assert not is_traceback_safe(error)
    assert safe_cause(error) is None


def test_a_real_resolver_failure_names_its_path_and_not_its_value() -> None:
    """The shape above, produced by the pinned ``pronotepy`` itself.

    A hand-built ``ParsingError`` proves the function; this proves the
    assumption it rests on -- that upstream puts the keys it walked in
    ``path`` and the value only in the message.
    """
    entry = {
        "N": _N,
        "dateDebut": {"_T": 7, "V": _NAME},
        "dateFin": {"_T": 7, "V": _NAME},
    }
    with pytest.raises(DataError) as raised:
        dataClasses.Absence(entry)

    described = describe_failure(raised.value)

    assert described == "ParsingError (decode, field dateDebut.V)"
    assert _NAME not in described


def test_a_json_error_from_requests_stays_a_transport_failure() -> None:
    """``requests``' ``JSONDecodeError`` is a ``ValueError`` too.

    It is classified as the request failure it is, before the decoding rule
    can claim it, so neither the set-up arm nor the category moves under it.
    """
    error = requests.exceptions.JSONDecodeError("Expecting value", _PAYLOAD_TEXT, 0)

    assert describe_failure(error) == "JSONDecodeError (request)"
    assert is_transport_failure(error)
    assert not is_decoding_failure(error)


@pytest.mark.parametrize(
    "error",
    [RuntimeError("a bug"), TypeError("'NoneType' object is not subscriptable")],
)
def test_an_ordinary_bug_keeps_its_traceback(error: BaseException) -> None:
    """Refusing every traceback, to be safe, would trade one defect for another.

    ``TypeError`` is deliberately not a decoding error: its message names
    types, and an unexpected one is usually this integration's own bug.
    """
    assert is_traceback_safe(error)
    assert safe_cause(error) is error
    assert not is_decoding_failure(error)
