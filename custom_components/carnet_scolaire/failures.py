"""A fixed, printable classification of a transport failure -- never its text.

The message of a transport exception is not ours and it is not safe to print.
``requests`` writes ``HTTPSConnectionPool(host='…', port=443): Max retries
exceeded with url: /…`` and ``aiohttp`` writes ``Cannot connect to host …``:
the host names the establishment, and the path of a PRONOTE page can carry the
parameters of a session. Those lines went to the log -- at INFO, on the
``log-when-unavailable`` line, and in a DEBUG traceback -- and into the reason
Home Assistant shows for an entry that is not ready. That log is what a user is
invited to paste into a public issue (§8.2).

So a failure is described by what can be known without reading its message:
the class name of the exception, and a coarse category taken from its type
(``timeout``, ``ssl``, ``dns``, ``connection``, ``http <status>``, ``os error
<errno name>``). That is enough to tell "the school's server is down" from
"the certificate is refused" in a bug report, and it cannot carry an address.

The chain is walked: an EcoleDirecte failure arrives as a
``ConnectorTransportError`` raised *from* the ``aiohttp`` error, and the
category belongs to the cause.
"""

from __future__ import annotations

import errno
import socket
import ssl
from typing import Final

from aiohttp import (
    ClientConnectionError,
    ClientError,
    ClientResponseError,
    ClientSSLError,
    ServerTimeoutError,
)
import requests

from .connectors.errors import ConnectorTransportError

#: How deep the ``__cause__`` / ``__context__`` chain is followed. A real chain
#: is two or three links; the bound only guards against a pathological one.
_MAX_CHAIN: Final = 8


#: Fixed categories, tried in order. Order matters: ``requests``'s
#: ``SSLError`` is a ``ConnectionError``, its ``ConnectTimeout`` is both, and
#: ``socket.gaierror`` is an ``OSError`` like everything else here. HTTP
#: statuses and bare ``OSError`` are handled by :func:`_category` itself.
_CATEGORIES: Final[tuple[tuple[tuple[type[BaseException], ...], str], ...]] = (
    ((ssl.SSLError, requests.exceptions.SSLError, ClientSSLError), "ssl"),
    ((TimeoutError, requests.exceptions.Timeout, ServerTimeoutError), "timeout"),
    ((socket.gaierror,), "dns"),
)

#: Categories tried after the HTTP statuses, for the same reason of order.
_LATE_CATEGORIES: Final[tuple[tuple[tuple[type[BaseException], ...], str], ...]] = (
    (
        (ConnectionError, requests.exceptions.ConnectionError, ClientConnectionError),
        "connection",
    ),
    ((requests.exceptions.RequestException, ClientError), "request"),
)


def _http_status(error: BaseException) -> str | None:
    """``http <status>`` for an HTTP-status failure, ``None`` otherwise."""
    if isinstance(error, requests.exceptions.HTTPError):
        response = error.response
        return f"http {response.status_code}" if response is not None else "http"
    if isinstance(error, ClientResponseError):
        return f"http {error.status}"
    return None


def _category(error: BaseException) -> str | None:
    """The coarse category of one exception, or ``None`` if it is not transport."""
    for types, category in _CATEGORIES:
        if isinstance(error, types):
            return category
    if (status := _http_status(error)) is not None:
        return status
    for types, category in _LATE_CATEGORIES:
        if isinstance(error, types):
            return category
    if not isinstance(error, OSError):
        return None
    code = error.errno
    if isinstance(code, int) and code in errno.errorcode:
        return f"os error {errno.errorcode[code]}"
    return "os error"


def _chain(error: BaseException) -> list[BaseException]:
    """The exception followed by its causes, outermost first, without cycles."""
    links: list[BaseException] = []
    current: BaseException | None = error
    while current is not None and current not in links and len(links) < _MAX_CHAIN:
        links.append(current)
        current = current.__cause__ or current.__context__
    return links


def is_transport_failure(error: BaseException) -> bool:
    """Whether the error, or anything it was raised from, is a transport failure.

    Used to decide that a traceback must not be written: the traceback prints
    the message of every link in the chain, and the transport link is the one
    that quotes the address.
    """
    return isinstance(error, ConnectorTransportError) or any(
        _category(link) is not None for link in _chain(error)
    )


def describe_failure(error: BaseException) -> str:
    """A fixed description of a failure: class names and a category, no text.

    ``ConnectTimeout (timeout)`` for a direct failure; for a wrapped one, the
    outer class, the class that carries the category, and the category:
    ``ConnectorTransportError <- ClientConnectorError (connection)``. An
    exception that is not a transport failure is reduced to its class name,
    because its message is no more ours than a transport one.
    """
    name = type(error).__name__
    for link in _chain(error):
        category = _category(link)
        if category is None:
            continue
        if link is not error:
            name = f"{name} <- {type(link).__name__}"
        return f"{name} ({category})"
    return name
