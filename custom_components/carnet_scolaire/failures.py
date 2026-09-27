"""A fixed, printable classification of a failure -- never its text.

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

A *decoding* failure is the same problem with a worse payload. ``pronotepy``'s
``ParsingError`` reads ``Error while converting value: <the converter's
error>``, and a converter's error quotes the value it refused --
``invalid literal for int() with base 10: 'Enfant Un'``; a ``KeyError`` names
whatever was looked up, which can be a value (a ``N``, a child's name) as
easily as a protocol key. That text reached the ERROR line of a login that
could not be decoded, the message of ``AccountUnreadable``, the cause chained
to the set-up refusal and the DEBUG traceback of every skipped entry. So a
decoding failure gets the category ``decode`` and, when it can be known
without reading a message, the *name* of the protocol field involved: the
path ``pronotepy``'s resolver was walking -- literals in its source, never
data -- or a ``KeyError`` whose key is one of :data:`PROTOCOL_KEYS`. Nothing
else about it is printed.
"""

from __future__ import annotations

import errno
import re
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

from .connectors.errors import ConnectorTransportError, ConnectorUndecodableError

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


#: The one category that is not a transport failure.
_DECODE: Final = "decode"

#: Built-in exceptions whose message quotes the data they failed on:
#: ``ValueError`` (``UnicodeDecodeError`` and ``json.JSONDecodeError``
#: included) prints the refused value, ``KeyError`` the key it looked up.
#: ``TypeError`` and ``IndexError`` are deliberately absent: their messages
#: name types and positions, and an unexpected one is more often a bug in this
#: integration -- whose traceback is the whole of a useful report -- than a
#: payload. A decoding failure that arrives as one of them through
#: ``pronotepy``'s resolver is a ``ParsingError`` by then.
_DECODING_BUILTINS: Final = (ValueError, KeyError)

#: ``pronotepy``'s decoding errors, recognised by name because this module must
#: not import ``pronotepy``: the config flow imports it, and a broken library
#: must stay "cannot log in" rather than become "cannot be added".
_PRONOTEPY_DECODING: Final = frozenset({"DataError", "ParsingError"})
_PRONOTEPY_EXCEPTIONS: Final = "pronotepy.exceptions"

#: What a protocol field name looks like. Applied even to the names taken from
#: ``pronotepy``'s source, so an upstream that one day put data in the path
#: would print nothing rather than that data.
_FIELD_NAME: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")

#: The keys a ``KeyError`` may be named by. The key of a ``KeyError`` is
#: whatever was looked up, and a lookup *by value* -- a child by name, an item
#: by its ``N`` -- raises one as readily as a missing protocol field; so only a
#: key that ``pronotepy`` or this integration spells literally while walking a
#: PRONOTE response is printed. Taken from the subscripts of ``pronotepy``'s
#: ``pronoteAPI.py`` and ``clients.py`` (the login path) and of ``gateway.py``.
#: A key missing from this list costs a bug report its field name, never more.
PROTOCOL_KEYS: Final = frozenset(
    {
        "Date",
        "DerniereDate",
        "Erreur",
        "G",
        "General",
        "L",
        "ListeCours",
        "ListeHeures",
        "ListeJours",
        "ListePeriodes",
        "ListeRepas",
        "ListeTravauxAFaire",
        "N",
        "NumeroSemaine",
        "PremierLundi",
        "Signature",
        "Titre",
        "V",
        "challenge",
        "cle",
        "data",
        "dataSec",
        "iCal",
        "jeton",
        "liste",
        "listeActualites",
        "listeClasses",
        "listeEtiquettes",
        "listeMessagerie",
        "listeModesAff",
        "listeOnglets",
        "listeRessourcesPourCommunication",
        "login",
        "membre",
        "modeCompLog",
        "modeCompMdp",
        "numeroSemaine",
        "paramICal",
        "paramSuppl",
        "periodeParDefaut",
        "ressource",
        "securisation",
        "url",
        "versionPN",
    }
)


def _is_decoding(error: BaseException) -> bool:
    """Whether this one exception is a decoding failure, by its type alone."""
    if isinstance(error, (ConnectorUndecodableError, *_DECODING_BUILTINS)):
        return True
    return any(
        cls.__module__ == _PRONOTEPY_EXCEPTIONS and cls.__name__ in _PRONOTEPY_DECODING
        for cls in type(error).__mro__
    )


def _field(error: BaseException) -> str | None:
    """The protocol field a decoding failure is about, or ``None``.

    Two sources, neither of them a message. ``ParsingError.path`` is the tuple
    of keys ``pronotepy``'s resolver was walking, each a literal in its source.
    A ``KeyError``'s key is printed only if it is one of :data:`PROTOCOL_KEYS`.
    """
    path = getattr(error, "path", None)
    if (
        _is_decoding(error)
        and isinstance(path, tuple)
        and path
        and all(isinstance(p, str) and _FIELD_NAME.fullmatch(p) for p in path)
    ):
        return ".".join(path)
    if isinstance(error, KeyError) and len(error.args) == 1:
        key = error.args[0]
        if isinstance(key, str) and key in PROTOCOL_KEYS:
            return key
    return None


def _http_status(error: BaseException) -> str | None:
    """``http <status>`` for an HTTP-status failure, ``None`` otherwise."""
    if isinstance(error, requests.exceptions.HTTPError):
        response = error.response
        return f"http {response.status_code}" if response is not None else "http"
    if isinstance(error, ClientResponseError):
        return f"http {error.status}"
    return None


def _category(error: BaseException) -> str | None:
    """The coarse category of one exception, or ``None`` if it has none.

    Every category but ``decode`` is a transport one.
    """
    for types, category in _CATEGORIES:
        if isinstance(error, types):
            return category
    if (status := _http_status(error)) is not None:
        return status
    for types, category in _LATE_CATEGORIES:
        if isinstance(error, types):
            return category
    if not isinstance(error, OSError):
        # Decoding last: a transport failure that is also a `ValueError`
        # (`requests`' `JSONDecodeError`) has been classified above.
        return _DECODE if _is_decoding(error) else None
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
        _category(link) not in {None, _DECODE} for link in _chain(error)
    )


def is_decoding_failure(error: BaseException) -> bool:
    """Whether the error, or anything it was raised from, failed to decode.

    Link by link, transport first, exactly as :func:`describe_failure` reads
    it: ``requests``' ``JSONDecodeError`` is a ``ValueError`` too, and it is a
    transport failure.
    """
    return any(_category(link) == _DECODE for link in _chain(error))


def is_traceback_safe(error: BaseException) -> bool:
    """Whether a traceback of this error may be written, at any level.

    A traceback prints the message of every link in the chain. It is refused
    for a transport failure, which quotes the address, and for a decoding
    failure, which quotes the data. Everything else keeps it: an unexpected
    exception is a bug in this integration, and the traceback is the whole of
    what a report about it can contain.
    """
    return not (is_transport_failure(error) or is_decoding_failure(error))


def safe_cause[E: BaseException](error: E) -> E | None:
    """``error``, to re-raise ``from``, or ``None`` when its traceback is unsafe.

    For ``raise ... from safe_cause(error)``: Home Assistant writes the "Full
    exception" of a refused set-up at DEBUG, and that includes the cause.
    """
    return error if is_traceback_safe(error) else None


def describe_failure(error: BaseException) -> str:
    """A fixed description of a failure: class names and a category, no text.

    ``ConnectTimeout (timeout)`` for a direct failure; for a wrapped one, the
    outer class, the class that carries the category, and the category:
    ``ConnectorTransportError <- ClientConnectorError (connection)``. A
    decoding failure adds the protocol field it is about when that can be
    known without reading a message --
    ``AccountUnreadable <- ParsingError (decode, field dateDemande.V)`` -- and
    nothing otherwise. Any other exception is reduced to its class name,
    because its message is no more ours than a transport one.
    """
    name = type(error).__name__
    links = _chain(error)
    for link in links:
        category = _category(link)
        if category is None:
            continue
        if link is not error:
            name = f"{name} <- {type(link).__name__}"
        if category == _DECODE:
            field = next((f for f in map(_field, links) if f is not None), None)
            if field is not None:
                return f"{name} (decode, field {field})"
        return f"{name} ({category})"
    return name
