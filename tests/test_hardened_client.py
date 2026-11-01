"""The hardening itself: the four upstream defects, held down by assertions.

``hardened_client.py`` is the only module in this integration whose reason to
exist is another library's behaviour. Nothing in it is a feature; every line is
a countermeasure, and a countermeasure with no test is indistinguishable from a
line somebody added by mistake. That is what this file is for.

Three things are asserted here that no other test file can see:

* **the timeout exists at all.** ``grep -rn timeout`` over pronotepy returns
  nothing, so the only thing standing between a half-open socket and a
  permanently held ``asyncio.Lock`` is ``_TimeoutSession.request`` putting a
  default in ``kwargs``. It is two lines, and if either one goes the whole
  integration degrades into §5.4's staleness regime with no error anywhere.
* **the bootstrap verdict is honest.** Upstream decides an address is banned
  with ``if "IP" in html`` -- two capitals anywhere in the page. The test that
  a page saying "Espace IP" still parses is the only thing that keeps that
  check from creeping back in.
* **the period registry does not leak.** ``Period.instances`` is a mutable
  class attribute, global to the process, never cleared, and every period in it
  pins a client, a session and a socket pool. The interesting case is not the
  happy path but the one the module documents at length: the constructor raised,
  ``attach`` never ran, and the ``finally`` has to sweep by difference.

**Nothing here touches the network, and nothing here can.** Two seams are
replaced and they are the only two that would: ``requests.Session.request``, on
the base class, so ``_TimeoutSession`` can be watched without a socket; and
``pronotepy.Client.__init__``, because -- as the module docstring says --
constructing a pronotepy client *is* a login. ``_Communication.__init__`` and
``dataClasses.Period`` are used for real: neither opens a connection.

The ``pronotepy`` logger is never enabled, at any level, in any test here. It
writes the hex of every request at DEBUG, credentials included. The one
``caplog`` assertion in this file names this integration's own logger
explicitly for that reason.

No Home Assistant: every function under test is synchronous and takes its
collaborators as arguments, so this file carries no ``REQUIRES_HASS`` marker
and runs on Windows as well as in CI.
"""

from __future__ import annotations

import contextlib
import logging
import threading
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pronotepy
from pronotepy import dataClasses, pronoteAPI
from pronotepy.exceptions import MFAError, PronoteAPIError
import pytest
import requests

from custom_components.carnet_scolaire import hardened_client
from custom_components.carnet_scolaire.const import LoginMode
from custom_components.carnet_scolaire.hardened_client import (
    BootstrapDelegated,
    BootstrapUnavailable,
    HardenedClient,
    _ConfinedRegistry,
    _current_timeouts,
    _HardenedCommunication,
    _patched_communication,
    _period_registry_confined,
    _TimeoutSession,
    build_client,
    export_credentials,
    install_hardened_transport,
    protocol_error_code,
    release_client,
)

from .conftest import CHILDREN

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

#: The establishment address, synthetic and on the reserved ``.invalid`` TLD.
URL = "https://demo.example.invalid/pronote/parent.html"

#: A bootstrap page of the right shape. The values are inventions; ``h`` is the
#: only attribute the code actually requires.
BOOTSTRAP_PAGE = (
    "<html><head><title>PRONOTE</title></head><body>"
    "<script>Start ({h:'session-under-test',a:'8',d:'0'})</script>"
    "</body></html>"
)


# ---------------------------------------------------------------------------
# The doubles
# ---------------------------------------------------------------------------


class _RecordingSession:
    """The little of a ``requests.Session`` that ``close()`` touches."""

    def __init__(self, *, raises: bool = False) -> None:
        self.closes = 0
        self._raises = raises

    def close(self) -> None:
        """Count the close, and optionally fail the way a dead pool does."""
        self.closes += 1
        if self._raises:
            raise OSError("the connection pool is already gone")


class _RecordingCommunication:
    """Stands in for ``_Communication``, recording what was posted."""

    def __init__(self, *, close_raises: bool = False) -> None:
        self.posts: list[tuple[str, dict[str, Any]]] = []
        self.session = _RecordingSession(raises=close_raises)

    def post(self, function_name: str, data: dict[str, Any]) -> dict[str, Any]:
        """Record the call, including the signature the client stamped."""
        self.posts.append((function_name, data))
        return {"dataSec": {"data": {}}}


class _StubClient:
    """Stands in for the client whose *construction* is a network login.

    It records what ``build_client`` handed it and, crucially, what
    :func:`_current_timeouts` said at the moment of construction -- which is
    the only way to observe that the thread-local was published *before* the
    transport was built rather than after.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.args = args
        self.kwargs = kwargs
        self.timeouts_during_construction = _current_timeouts()
        self.closes = 0

    def close(self) -> None:
        """Count the close ``release_client`` owes this object."""
        self.closes += 1


def _resource(children: Sequence[tuple[str, str]] = ()) -> dict[str, Any]:
    """The ``ressource`` block a login lands in ``parametres_utilisateur``.

    ``listeRessources`` is present exactly on a parent account, which is what
    ``HardenedClient.__init__`` reads to decide whether it holds children.
    """
    resource: dict[str, Any] = {"N": "PARENT-1", "L": "Parent Un"}
    if children:
        resource["listeRessources"] = [
            {"N": identifier, "L": name} for identifier, name in children
        ]
    return resource


@contextlib.contextmanager
def _upstream_login_replaced(
    *,
    children: Sequence[tuple[str, str]] = (),
    posts_a_tab_during_login: bool = False,
) -> Iterator[None]:
    """Replace ``pronotepy.Client.__init__``, which is a network login.

    Patched at ``Client`` rather than at ``ClientBase`` because ``Client`` is
    the base ``HardenedClient.__init__`` reaches through ``super()``, and what
    is being tested is *our* constructor: the child derivation, the automatic
    selection of the first child, and the attribute that has to exist before
    upstream's login posts anything.

    ``posts_a_tab_during_login`` reproduces the hazard the module's comment
    describes: a login post that names an ``onglet`` and therefore reaches our
    ``post()`` before the child list exists.
    """

    def fake_init(
        self: HardenedClient,
        pronote_url: str,
        username: str = "",
        password: str = "",
        **kwargs: Any,
    ) -> None:
        self.pronote_url = pronote_url
        self.username = username
        self.password = password
        self.uuid = kwargs.get("uuid", "")
        self.client_identifier = kwargs.get("client_identifier")
        self.login_mode = kwargs.get("mode", "normal")
        self.communication = _RecordingCommunication()  # type: ignore[assignment]
        self.parametres_utilisateur = {
            "dataSec": {"data": {"ressource": _resource(children)}}
        }
        self.logged_in = True
        if posts_a_tab_during_login:
            # Upstream's three login posts pass no tab today. The day one does,
            # this is the call that would raise `AttributeError` from `post()`.
            self.post("ParametresUtilisateur", 16)

    with patch.object(pronotepy.Client, "__init__", fake_init):
        yield


def _client(
    *,
    children: Sequence[tuple[str, str]] = (),
    posts_a_tab_during_login: bool = False,
) -> HardenedClient:
    """A fully constructed hardened client that never logged in."""
    with _upstream_login_replaced(
        children=children, posts_a_tab_during_login=posts_a_tab_during_login
    ):
        return HardenedClient(URL, "parent-under-test", "not-a-real-password")


@contextlib.contextmanager
def _bootstrap_transport(*, mobile: bool = False) -> Iterator[_HardenedCommunication]:
    """A real ``_HardenedCommunication``, closed on the way out.

    Constructing one places no request: ``_Communication.__init__`` splits the
    address, builds a session and generates its AES material. The session is
    closed anyway, because a test that leaves one behind leaves a connection
    pool behind.
    """
    communication = _HardenedCommunication(URL, None, mobile)
    try:
        yield communication
    finally:
        communication.session.close()


# ---------------------------------------------------------------------------
# The fixtures that keep the process-global state global to one test
# ---------------------------------------------------------------------------


@pytest.fixture(name="fresh_install")
def fresh_install_fixture() -> Iterator[None]:
    """Undo the permanent transport installation for one test.

    ``install_hardened_transport`` is deliberately process-wide and permanent,
    so the only way to test that it installs anything is to put the module back
    to its pre-install state and then put it back as it was. Restoring matters:
    the substitution is what every other test in this file depends on.
    """
    original = pronotepy.clients._Communication
    installed = hardened_client._installed
    pronotepy.clients._Communication = pronoteAPI._Communication
    hardened_client._installed = False
    try:
        yield
    finally:
        pronotepy.clients._Communication = original
        hardened_client._installed = installed


@pytest.fixture(name="make_period")
def make_period_fixture() -> Iterator[Callable[[str], dataClasses.Period]]:
    """A ``Period`` factory whose products never outlive the test.

    Real ``dataClasses.Period`` objects, because the thing under test is the
    arithmetic on ``Period.instances`` and a stand-in would prove nothing about
    it. The teardown restores the registry to exactly what it held on entry --
    the whole module exists to stop periods accumulating there, and a test file
    that leaked one would break a later one from a distance.
    """
    registry = dataClasses.Period.instances
    before = set(registry)

    def make(identifier: str) -> dataClasses.Period:
        return dataClasses.Period(
            object(),  # type: ignore[arg-type]
            {
                "N": identifier,
                "L": f"Trimestre {identifier}",
                "dateDebut": {"V": "01/09/2025"},
                "dateFin": {"V": "20/12/2025"},
            },
        )

    yield make

    registry.clear()
    registry.update(before)
    dataClasses.Period.instances = registry


# ---------------------------------------------------------------------------
# The timeout, which is the whole of defect 4
# ---------------------------------------------------------------------------


def test_a_request_with_no_timeout_of_its_own_gets_the_session_default() -> None:
    """The two lines that stand between a half-open socket and a deadlock.

    pronotepy passes no ``timeout`` anywhere, and ``requests`` has no
    session-level default, so without this ``setdefault`` a server that accepts
    the connection and never answers pins the account's single worker thread
    for ever. The base method is replaced rather than a socket opened.
    """
    session = _TimeoutSession(3.0, 7.0)
    captured: list[dict[str, Any]] = []

    def fake_request(
        _self: requests.Session,
        _method: str,
        _url: str,
        *_args: Any,
        **kwargs: Any,
    ) -> str:
        captured.append(kwargs)
        return "sentinel-response"

    with patch.object(requests.Session, "request", fake_request):
        answer = session.request("GET", URL)

    session.close()
    assert answer == "sentinel-response", "the base method's result is returned as is"
    assert captured == [{"timeout": (3.0, 7.0)}]


@pytest.mark.parametrize(
    "explicit",
    [
        0.5,
        (1.0, 2.0),
        # An explicit ``None`` wins too. That is the documented contract -- a
        # caller passing a timeout keeps it -- and not an oversight, since
        # `setdefault` cannot tell None from a considered choice. It is
        # harmless because pronotepy passes no timeout at all, so the only
        # code that can reach this branch is ours.
        None,
    ],
)
def test_a_caller_who_names_a_timeout_keeps_it(explicit: Any) -> None:
    """The session injects a default; it does not impose a policy.

    Without this the subclass would silently rewrite the timeout of any caller
    that had thought about the question -- including a future one of ours that
    wants a longer read for a large attachment.
    """
    session = _TimeoutSession(3.0, 7.0)
    captured: list[dict[str, Any]] = []

    def fake_request(
        _self: requests.Session,
        _method: str,
        _url: str,
        *_args: Any,
        **kwargs: Any,
    ) -> None:
        captured.append(kwargs)

    with patch.object(requests.Session, "request", fake_request):
        session.request("POST", URL, timeout=explicit)

    session.close()
    assert captured[0]["timeout"] == explicit


@pytest.mark.parametrize(
    ("mobile", "user_agent"),
    [(False, "Mozilla/5.0 (X11"), (True, "Mozilla/5.0 (Linux; Android")],
)
def test_the_bootstrap_transport_swaps_the_session_and_keeps_the_headers(
    mobile: bool,
    user_agent: str,
) -> None:
    """Substituting the session must not lose what upstream put on it.

    ``_Communication.__init__`` sets the User-Agent, and token mode depends on
    it: PRONOTE serves the mobile protocol to a mobile User-Agent -- here the
    official app's own, ``PRONOTE Mobile APP`` included -- and the browser
    protocol to everything else. Replacing the session object without copying
    the headers across would turn every QR-code account into a login failure
    with nothing in the log to explain it.
    """
    with (
        _patched_communication(2.0, 5.0),
        _bootstrap_transport(mobile=mobile) as communication,
    ):
        assert isinstance(communication.session, _TimeoutSession)
        assert communication.session._default_timeout == (2.0, 5.0)
        assert communication.session.headers["User-Agent"].startswith(user_agent)
        assert (
            communication.session.headers["User-Agent"].endswith(
                "PRONOTE Mobile APP Version/2.0.17"
            )
            is mobile
        )


def test_the_transport_falls_back_to_its_class_defaults_outside_a_login() -> None:
    """A transport built with no login in progress is still bounded.

    Nothing in the integration does that today -- ``build_client`` is the only
    caller and it always publishes a pair -- which is exactly why the fallback
    needs a test: it is the value that would apply if a future caller forgot.
    Unbounded is the one answer that must never be given.
    """
    with _bootstrap_transport() as communication:
        assert communication.session._default_timeout == (30.0, 60.0)


def test_a_thread_with_no_login_in_progress_reads_the_class_defaults() -> None:
    """``_current_timeouts`` on a thread that never entered the context.

    The thread-local is per-thread by design, so "absent" is a normal state and
    not an error: the event loop thread itself is in it.
    """
    seen: list[tuple[float, float]] = []

    thread = threading.Thread(target=lambda: seen.append(_current_timeouts()))
    thread.start()
    thread.join()

    assert seen == [(30.0, 60.0)]


# ---------------------------------------------------------------------------
# Publishing the timeouts, and putting the previous pair back
# ---------------------------------------------------------------------------


def test_a_login_publishes_its_timeouts_and_restores_the_previous_pair() -> None:
    """The context manager's contract, in both directions.

    Restoring rather than clearing is what makes it nestable, and nesting is
    not hypothetical: a service call can log in while a scheduled batch is
    already inside the context on the same worker thread. Clearing on the way
    out of the inner block would silently hand the outer one the class
    defaults.
    """
    with _patched_communication(1.0, 2.0):
        assert _current_timeouts() == (1.0, 2.0)

        with _patched_communication(11.0, 22.0):
            assert _current_timeouts() == (11.0, 22.0)

        assert _current_timeouts() == (1.0, 2.0), "the outer login's pair came back"

    assert _current_timeouts() == (30.0, 60.0), "and then nothing is in progress"


def test_the_previous_pair_is_restored_even_when_the_login_raises() -> None:
    """The path every failing login takes, which is the one that matters.

    A login that raises is the *normal* case here -- wrong password, MFA, a
    school whose server is down -- so leaking the timeout pair on the exception
    path would leak it permanently for that worker thread, and the next login
    on it would run under some other entry's deadline.
    """

    def login() -> None:
        with _patched_communication(1.0, 2.0):
            raise MFAError("the account asks for a PIN")

    with pytest.raises(MFAError):
        login()

    assert _current_timeouts() == (30.0, 60.0)


def test_a_login_s_timeouts_are_invisible_to_another_thread() -> None:
    """Why a thread-local and not a class attribute plus a lock.

    Each config entry owns a single-worker executor, so two entries with
    different ``read_timeout`` values run on different threads. A class
    attribute would let the second entry's login rewrite the first's deadline
    mid-request; a lock held across the login would make ``release_client``
    wait on somebody else's network round trip, which is the defect the module
    docstring records as freezing Home Assistant outright.
    """
    seen: list[tuple[float, float]] = []

    with _patched_communication(1.0, 2.0):
        thread = threading.Thread(target=lambda: seen.append(_current_timeouts()))
        thread.start()
        thread.join()

    assert seen == [(30.0, 60.0)], "the other thread saw no part of this login"


@pytest.mark.usefixtures("fresh_install")
def test_entering_a_login_installs_the_hardened_transport() -> None:
    """The substitution has no other seam.

    ``ClientBase.__init__`` resolves ``_Communication`` as a module global
    between creating the transport and bootstrapping it, so there is no way to
    get a timeout onto the bootstrap GET other than replacing that attribute
    before the constructor runs.
    """
    assert pronotepy.clients._Communication is not _HardenedCommunication

    with _patched_communication(1.0, 2.0):
        pass

    assert pronotepy.clients._Communication is _HardenedCommunication


@pytest.mark.usefixtures("fresh_install")
def test_installing_the_transport_twice_installs_it_once() -> None:
    """Idempotent because it is called on every login of every entry.

    The scoped version this replaced had to hold a process-wide lock across a
    full network handshake so two entries could not race on the module
    attribute; that lock then blocked every other entry's teardown. Permanence
    is what removes the contention, and idempotence is what makes permanence
    safe to call from anywhere.
    """
    install_hardened_transport()
    first = pronotepy.clients._Communication

    install_hardened_transport()

    assert pronotepy.clients._Communication is first is _HardenedCommunication


# ---------------------------------------------------------------------------
# The bootstrap page: the most fragile parse in the integration
# ---------------------------------------------------------------------------


def test_the_bootstrap_attributes_are_read_from_the_start_block() -> None:
    """The happy path, which every login depends on.

    Values arrive quoted and keys unquoted, or the other way round, depending
    on the server version, so both are stripped.
    """
    with _bootstrap_transport() as communication:
        attributes = communication._parse_html(BOOTSTRAP_PAGE)

    assert attributes == {"h": "session-under-test", "a": "8", "d": "0"}


@pytest.mark.parametrize(
    ("label", "page"),
    [
        ("empty", ""),
        ("a JSON error body", '{"error":"not found","status":404}'),
        ("a plain-text notice", "Le service est momentanement indisponible."),
        ("a portal redirect", "<html><body><h1>Connexion ENT</h1></body></html>"),
        ("an unbraced call", "<script>Start (nothing here)</script>"),
        # Not HTML at all, and not decodable as anything meaningful: what
        # `response.text` yields when the body is compressed or binary and the
        # declared charset is wrong. The function must reach a verdict, not a
        # traceback.
        ("mojibake", "\x1f\x8b\x08\x00��Start\x00�("),
    ],
)
def test_a_page_without_a_start_block_is_a_bootstrap_condition(
    label: str,
    page: str,
) -> None:
    """One verdict, and it says nothing about why.

    ``BootstrapUnavailable`` is deliberately mute: specification v1 built an
    hour-long hold and a repair issue with "explicit text" on upstream's
    ``if "IP" in html`` signal, which would have told parents their home
    address was banned because a school wrote "Espace IP" in a footer. The
    assertion on the message is part of the test, not decoration.
    """
    with (
        _bootstrap_transport() as communication,
        pytest.raises(BootstrapUnavailable) as error,
    ):
        communication._parse_html(page)

    assert "IP" not in str(error.value), f"{label} must not produce a claim about IP"
    assert isinstance(error.value, PronoteAPIError), (
        "the flow's except chain catches PronoteAPIError; a condition outside "
        "that hierarchy would reach the user as a raw traceback"
    )


PORTAL_LOGIN_PAGE = """<html><body>
<form id="kc-form-login" action="/auth/realms/demo/login-actions/authenticate"
      method="post">
  <input id="username" name="username" type="text">
  <input id="password" name="password" type='PASSWORD'>
</form></body></html>"""


def test_another_services_login_form_is_named_as_such() -> None:
    """A space that delegates login redirects to its portal's form.

    Reported as the generic "no session page", a parent with the right
    address and an open space was told to check both, and never learnt that
    the ENT login was the one that works. Still a `BootstrapUnavailable`, so
    every caller unaware of the finer verdict handles it as before.
    """
    with (
        _bootstrap_transport() as communication,
        pytest.raises(BootstrapDelegated) as error,
    ):
        communication._parse_html(PORTAL_LOGIN_PAGE)

    assert isinstance(error.value, BootstrapUnavailable)


@pytest.mark.parametrize(
    "page",
    [
        pytest.param("<html><body>Maintenance</body></html>", id="no-form"),
        pytest.param('<input name="password-hint" type="text">', id="named-only"),
    ],
)
def test_a_page_without_a_password_field_stays_the_generic_verdict(page: str) -> None:
    """Only a real password input earns the "use the ENT" advice.

    A maintenance page sent to the ENT login would send a parent the wrong way
    on the one day the address simply was not answering.
    """
    with (
        _bootstrap_transport() as communication,
        pytest.raises(BootstrapUnavailable) as error,
    ):
        communication._parse_html(page)

    assert not isinstance(error.value, BootstrapDelegated)


def test_the_two_capitals_upstream_reads_as_a_ban_are_ignored() -> None:
    """Divergence 1, asserted directly, because it is a deletion.

    A maintainer diffing this against upstream finds a missing check and no
    code to explain it. This test is that explanation: three ordinary French
    words containing the letters ``IP``, on a page that is perfectly healthy.
    Upstream refuses all three.
    """
    page = (
        "<html><body><footer>Espace IP -- contactez l'EQUIPE.</footer>"
        "<div class='SKIP-nav'>ZIP</div>"
        "<script>Start ({h:'session-under-test'})</script></body></html>"
    )

    with _bootstrap_transport() as communication:
        attributes = communication._parse_html(page)

    assert attributes == {"h": "session-under-test"}


def test_an_attribute_value_containing_a_colon_does_not_break_the_parse() -> None:
    """Divergence 2, in the form that actually happens.

    Upstream's ``key, value = attribute.split(":")`` raises ``ValueError`` on
    any value carrying a colon -- a URL, a time -- and ``initialise()`` reads
    that as "retry", so a page that will never parse costs three GETs before
    failing with "Unable to connect to pronote, please try again later".
    ``partition`` keeps the whole value and fails once, clearly, if at all.
    """
    page = (
        "<script>Start ({h:'session-under-test',"
        "url:'https://demo.example.invalid/pronote'})</script>"
    )

    with _bootstrap_transport() as communication:
        attributes = communication._parse_html(page)

    assert attributes["url"] == "https://demo.example.invalid/pronote"


def test_an_attribute_with_no_colon_at_all_is_tolerated() -> None:
    """The other half of divergence 2: a garbled block still yields its ``h``.

    A malformed extra attribute is not a reason to throw away a session id that
    is right there in the page.
    """
    with _bootstrap_transport() as communication:
        attributes = communication._parse_html(
            "<script>Start ({h:'session-under-test',garbled})</script>"
        )

    assert attributes["h"] == "session-under-test"
    assert attributes["garbled"] == ""


def test_attributes_without_a_session_id_ask_upstream_for_a_retry() -> None:
    """``ValueError`` is not sloppiness here; it is the retry contract.

    ``_Communication.initialise`` catches ``ValueError`` and re-GETs, up to
    three times. A block that parsed but carries no ``h`` is the one case where
    a retry is genuinely worth a request -- the page was truncated -- so this
    is the single condition allowed to keep upstream's signal.
    """
    with (
        _bootstrap_transport() as communication,
        pytest.raises(ValueError, match="no session id"),
    ):
        communication._parse_html("<script>Start ({a:'8',d:'0'})</script>")


# ---------------------------------------------------------------------------
# The period registry: the anti-leak mechanism
# ---------------------------------------------------------------------------


def test_a_period_registered_during_the_block_is_adopted_then_released(
    make_period: Callable[[str], dataClasses.Period],
) -> None:
    """The happy path of the confinement, end to end.

    Without it every login leaks: each ``Period`` stays in the class-level set
    for ever and holds its client, that client's ``requests.Session`` and its
    socket pool. At one login a day that is invisible; at a reload every few
    minutes while a user configures the integration, it is not.
    """
    client = _StubClient()

    with _period_registry_confined() as confine:
        mine = make_period("P1")
        confine(client)

    assert isinstance(client.period_registry, _ConfinedRegistry)
    assert mine in dataClasses.Period.instances, (
        "the client is alive, so its periods must still resolve"
    )

    release_client(client)

    assert mine not in dataClasses.Period.instances
    assert client.closes == 1


def test_the_release_removes_by_difference_and_never_by_clearing(
    make_period: Callable[[str], dataClasses.Period],
) -> None:
    """A second config entry's periods must survive the first one's teardown.

    Clearing the registry instead would make ``Util.get(Period.instances,
    id=...)`` return an empty list, so ``Grade.__init__``'s ``[0]`` raises
    ``IndexError``, wrapped as ``ParsingError`` -- and the whole marks batch of
    the *other* account fails for a teardown it had nothing to do with.
    """
    foreign = make_period("FOREIGN")
    client = _StubClient()

    with _period_registry_confined() as confine:
        mine = make_period("P1")
        confine(client)

    release_client(client)

    assert mine not in dataClasses.Period.instances
    assert foreign in dataClasses.Period.instances, (
        "another entry registered this one before the block; it is not ours"
    )


def test_releasing_twice_is_harmless(
    make_period: Callable[[str], dataClasses.Period],
) -> None:
    """``release_client`` is called from teardown paths that can both run.

    Unload and the failure handler each release the session, so the second call
    must not attempt the difference again -- by then another entry may have
    registered a period this one no longer owns.
    """
    client = _StubClient()

    with _period_registry_confined() as confine:
        make_period("P1")
        confine(client)

    release_client(client)
    release_client(client)

    assert client.period_registry._owned == set()
    assert client.closes == 2


def test_a_block_that_registered_and_confined_nothing_is_a_no_op(
    make_period: Callable[[str], dataClasses.Period],
) -> None:
    """The confinement must cost nothing when there was nothing to confine.

    ``build_client`` enters this block before it knows whether the login will
    get as far as creating a period, so the empty case is a real one: a
    connection refused, a bad address, an ENT that rejects the credentials. It
    must leave the process-global registry byte for byte as it found it,
    including whatever another entry put there while we were inside.
    """
    foreign = make_period("FOREIGN")
    before = set(dataClasses.Period.instances)

    with _period_registry_confined():
        pass

    assert set(dataClasses.Period.instances) == before
    assert foreign in dataClasses.Period.instances


def test_a_constructor_that_raises_does_not_leak_the_periods_it_registered(
    make_period: Callable[[str], dataClasses.Period],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The real defect this module records, and the most useful test here.

    Upstream runs ``self.periods_ = self.periods`` *before*
    ``self.logged_in = self._login()``. An account with 2FA and no stored PIN
    raises ``MFAError`` on every attempt -- after its periods are already in
    the global set, each one holding a client, a session and a socket pool.
    ``attach`` never ran, so nothing removed them, and every retry added more.
    The ``finally`` sweeping by difference is the fix, and this is the only
    test that exercises it.

    ``caplog`` is scoped to this integration's own logger by name. The
    ``pronotepy`` logger must never be enabled, not even here: it writes the
    hex of every request at DEBUG, credentials included.
    """
    foreign = make_period("FOREIGN")
    registered: list[dataClasses.Period] = []

    def failing_login() -> None:
        with _period_registry_confined():
            # Upstream's `periods_ = self.periods`, which runs before the login.
            registered.append(make_period("P1"))
            raise MFAError("the account asks for a PIN")

    with (
        caplog.at_level(logging.DEBUG, logger=hardened_client.__name__),
        pytest.raises(MFAError),
    ):
        failing_login()

    assert registered[0] not in dataClasses.Period.instances, (
        "the period the failed login registered is still holding its client"
    )
    assert foreign in dataClasses.Period.instances
    assert "removed them from the global registry" in caplog.text


def test_a_failure_before_any_period_was_registered_says_nothing(
    make_period: Callable[[str], dataClasses.Period],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The common failure -- a wrong password -- leaks nothing and logs nothing.

    Most logins fail before ``periods_`` is reached, so the sweep finds an
    empty difference. A debug line on that path would appear on every failed
    attempt and say nothing true.
    """
    foreign = make_period("FOREIGN")

    def failing_login() -> None:
        with _period_registry_confined():
            raise PronoteAPIError("wrong credentials")

    with (
        caplog.at_level(logging.DEBUG, logger=hardened_client.__name__),
        pytest.raises(PronoteAPIError),
    ):
        failing_login()

    assert foreign in dataClasses.Period.instances
    assert caplog.text == ""


# ---------------------------------------------------------------------------
# build_client: the one function that would reach a school
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("login_mode", "expected_mode"),
    [
        (str(LoginMode.QR_CODE), "token"),
        (str(LoginMode.CREDENTIALS), "normal"),
        (str(LoginMode.ENT), "normal"),
        # Not a value the integration stores; asserted because the vocabulary
        # here is *ours* (`LoginMode`) and not pronotepy's, and the mapping is
        # one-directional. A caller passing pronotepy's own word gets a
        # credentials login, not a token one.
        ("token", "normal"),
        ("", "normal"),
    ],
)
def test_the_login_mode_maps_to_pronotepy_s_mode(
    login_mode: str,
    expected_mode: str,
) -> None:
    """Two vocabularies, one translation, and no third state.

    ``mode != "normal"`` is what makes pronotepy demand a UUID, send the mobile
    User-Agent and treat the password as a rotating token. Getting this mapping
    wrong turns a QR enrolment into an authentication failure whose message
    mentions neither the QR code nor the mode.
    """
    with patch.object(hardened_client, "HardenedClient", _StubClient):
        client = build_client(
            login_mode=login_mode,
            pronote_url=URL,
            username="parent-under-test",
            password="not-a-real-password",
            uuid="UUID-UNDER-TEST",
        )

    assert client.kwargs["mode"] == expected_mode


def test_every_credential_reaches_the_constructor_unchanged() -> None:
    """The argument list is the whole of the function's other job.

    ``ClientBase.__init__`` takes the address, the username and the password
    positionally and the rest by keyword; a dropped ``account_pin`` reads to
    the user as "wrong PIN" and a dropped ``client_identifier`` re-registers
    the device on every login, which is itself an authorization request.
    """
    ent_marker = object()

    with patch.object(hardened_client, "HardenedClient", _StubClient):
        client = build_client(
            login_mode=str(LoginMode.QR_CODE),
            pronote_url=URL,
            username="parent-under-test",
            password="not-a-real-password",
            uuid="UUID-UNDER-TEST",
            account_pin="0000",
            client_identifier="client-under-test",
            device_name="device-under-test",
            ent=ent_marker,  # type: ignore[arg-type]
        )

    assert client.args == (URL, "parent-under-test", "not-a-real-password")
    assert client.kwargs == {
        "ent": ent_marker,
        "mode": "token",
        "uuid": "UUID-UNDER-TEST",
        "account_pin": "0000",
        "client_identifier": "client-under-test",
        "device_name": "device-under-test",
    }


def test_the_timeouts_are_published_before_the_client_is_constructed() -> None:
    """Ordering, not merely presence.

    The transport is built inside ``ClientBase.__init__``, and it reads the
    thread-local *at construction time*. Publishing the pair after the client
    exists would leave the bootstrap GET -- the one request most likely to hang
    on a school that is down -- running under the class defaults instead of the
    entry's configured deadline.
    """
    with patch.object(hardened_client, "HardenedClient", _StubClient):
        client = build_client(
            login_mode=str(LoginMode.CREDENTIALS),
            pronote_url=URL,
            username="parent-under-test",
            password="not-a-real-password",
            connect_timeout=4.0,
            read_timeout=9.0,
        )

    assert client.timeouts_during_construction == (4.0, 9.0)
    assert _current_timeouts() == (30.0, 60.0), "and the pair does not outlive the call"


def test_the_client_it_returns_owns_the_periods_its_login_created(
    make_period: Callable[[str], dataClasses.Period],
) -> None:
    """``build_client`` is where the confinement is actually wired up.

    The context manager and the registry can both be perfect and still leak if
    ``confine(client)`` is not called on the way out -- which is exactly what
    the ``attached`` flag exists to detect. This asserts the wiring from the
    outside, through the public entry point.
    """
    created: list[dataClasses.Period] = []

    class _LoggingInClient(_StubClient):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            created.append(make_period("P1"))

    with patch.object(hardened_client, "HardenedClient", _LoggingInClient):
        client = build_client(
            login_mode=str(LoginMode.CREDENTIALS),
            pronote_url=URL,
            username="parent-under-test",
            password="not-a-real-password",
        )

    assert client.period_registry._owned == set(created)

    release_client(client)

    assert created[0] not in dataClasses.Period.instances


def test_a_login_that_fails_propagates_and_still_sweeps_the_registry(
    make_period: Callable[[str], dataClasses.Period],
) -> None:
    """A failed login must reach the session manager, which classifies it.

    ``build_client`` catches nothing on purpose: ``MFAError``,
    ``ENTLoginError``, ``BootstrapUnavailable`` and ``CryptoError`` are told
    apart by the caller, and each one produces a different repair. What the
    ``finally`` adds is that the failure costs no memory either.
    """
    registered: list[dataClasses.Period] = []

    class _FailingClient(_StubClient):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            registered.append(make_period("P1"))
            raise MFAError("the account asks for a PIN")

    with (
        patch.object(hardened_client, "HardenedClient", _FailingClient),
        pytest.raises(MFAError),
    ):
        build_client(
            login_mode=str(LoginMode.CREDENTIALS),
            pronote_url=URL,
            username="parent-under-test",
            password="not-a-real-password",
        )

    assert registered[0] not in dataClasses.Period.instances, (
        "the failed login left its periods, and their sockets, behind"
    )


# ---------------------------------------------------------------------------
# release_client
# ---------------------------------------------------------------------------


def test_releasing_nothing_is_allowed() -> None:
    """The teardown path runs whether or not a session was ever opened.

    ``async_unload_entry`` and the failed-setup handler both release, and with
    the lazy strategy an entry can be unloaded having never logged in.
    """
    release_client(None)


def test_a_client_without_a_registry_is_still_closed() -> None:
    """Defensive, and load-bearing for one real case.

    A client built outside ``build_client`` -- a test double, or a future
    diagnostic path -- carries no ``period_registry``. Reading the attribute
    unguarded would raise ``AttributeError`` from a ``finally``, which loses
    the original exception and leaks the socket pool it was called to close.
    """
    client = _StubClient()

    release_client(client)

    assert client.closes == 1


# ---------------------------------------------------------------------------
# HardenedClient: the child signature, and the two refusals
# ---------------------------------------------------------------------------


def test_a_parent_account_derives_its_children_and_selects_the_first() -> None:
    """Upstream rebuilds this in ``ParentClient.__init__`` and loses it later.

    Deriving the child list in our own constructor is what makes discarding the
    client the correct answer to an expired session: a fresh client re-derives
    the list by construction, so there is no state left to lose.
    """
    client = _client(children=CHILDREN)

    assert client.is_parent_account
    assert [child.id for child in client.children] == ["STUDENT-1", "STUDENT-2"]
    assert client.selected_child_id == "STUDENT-1"


def test_a_student_account_holds_no_children_and_stamps_no_membre() -> None:
    """One class for both account types, so the student case needs saying.

    ``listeRessources`` is simply absent on a student account, and an empty
    child list is what makes ``is_parent_account`` false and leaves the
    ``membre`` signature off every request.
    """
    client = _client()

    assert not client.is_parent_account
    assert client.selected_child_id is None

    client.post("PageEmploiDuTemps", 16, {"a": 1})

    assert client.communication.posts == [
        ("PageEmploiDuTemps", {"Signature": {"onglet": 16}, "data": {"a": 1}})
    ]


def test_the_child_signature_field_exists_before_upstream_s_login_posts() -> None:
    """Why ``_selected_child`` is set on the line above ``super().__init__``.

    The base constructor logs in, which posts, which reaches *our* ``post()``,
    which reads that attribute. It only survives today because none of
    upstream's three login posts passes an ``onglet``. The day one of them
    gains a tab, every login raises ``AttributeError`` -- which is in neither
    ``_DECODING_ERRORS`` nor the set-up handler's ``except`` chain, so the user
    gets a raw traceback and no classification at all.
    """
    client = _client(children=CHILDREN, posts_a_tab_during_login=True)

    assert client.communication.posts[0] == (
        "ParametresUtilisateur",
        {"Signature": {"onglet": 16}},
    )
    # And the login's own post carried no child, because none was known yet.
    assert "membre" not in client.communication.posts[0][1]["Signature"]


def test_a_tab_call_stamps_the_selected_child_as_membre() -> None:
    """The signature that decides whose data comes back.

    Without ``membre`` the server answers with the *parent's* own resource, so
    a two-child account publishes the account holder's empty timetable under
    both children -- plausible, silent, and wrong.
    """
    client = _client(children=CHILDREN)
    client.set_child("STUDENT-2")

    client.post("PageEmploiDuTemps", 16, {"domaine": {"_T": 8}})

    assert client.communication.posts == [
        (
            "PageEmploiDuTemps",
            {
                "Signature": {"onglet": 16, "membre": {"N": "STUDENT-2", "G": 4}},
                "data": {"domaine": {"_T": 8}},
            },
        )
    ]


def test_a_call_that_names_no_tab_carries_no_signature_at_all() -> None:
    """The login posts and ``FonctionParametres`` take this path.

    ``if onglet:`` rather than ``if onglet is not None:`` is deliberate --
    ``onglet=0`` is not a tab -- and an empty body is what upstream's own
    handshake expects.
    """
    client = _client(children=CHILDREN)

    client.post("FonctionParametres")
    client.post("Identification", 0)

    assert client.communication.posts == [
        ("FonctionParametres", {}),
        ("Identification", {}),
    ]


def test_identification_ends_with_the_apps_null_device_information() -> None:
    """The official app's ``Identification`` ends with ``informationsAppareil``,
    null outside an enrolment, and upstream's omits it. Sent like the app, so
    a login differs from the app's by nothing a client can see.
    """
    client = _client(children=CHILDREN)

    client.post("Identification", data={"identifiant": "demo", "loginTokenSAV": ""})

    ((name, body),) = client.communication.posts[-1:]
    assert name == "Identification"
    assert list(body["data"]) == [
        "identifiant",
        "loginTokenSAV",
        "informationsAppareil",
    ]
    assert body["data"]["informationsAppareil"] is None


def test_setting_a_child_mutates_the_slot_the_tab_calls_read() -> None:
    """``set_child`` is a mutation, which is why the unit of work is a pair.

    ``Client.lessons()`` builds its request body from
    ``parametres_utilisateur["dataSec"]["data"]["ressource"]``, so interleaving
    ``set_child(A) · post() · set_child(B) · post()`` at the wrong granularity
    publishes child B's timetable under child A's entities. §3.2 makes
    ``(child, tier)`` atomic precisely because of this line.
    """
    client = _client(children=CHILDREN)

    client.set_child("STUDENT-2")

    slot = client.parametres_utilisateur["dataSec"]["data"]["ressource"]
    assert slot["N"] == "STUDENT-2"
    assert client.info.id == "STUDENT-2"
    assert client.selected_child_id == "STUDENT-2"


def test_a_child_can_also_be_selected_by_object() -> None:
    """The batch collector already holds the ``ClientInfo`` it iterates.

    Accepting both spares it a lookup per tier, and the identity assertion is
    what says the object is used as given rather than re-resolved by id.
    """
    client = _client(children=CHILDREN)
    second = client.children[1]

    client.set_child(second)

    assert client.info is second


def test_a_child_who_left_the_establishment_raises_child_not_found() -> None:
    """A client-side fault, and it must be told apart from a server refusal.

    The session manager maps ``ChildNotFound`` to an integration fault rather
    than to the backoff: asking for a child who has moved schools must not hold
    a healthy account for an hour, because the repair -- reconfiguring the
    children -- is itself a login that the hold would refuse.
    """
    client = _client(children=CHILDREN)

    with pytest.raises(pronotepy.ChildNotFound):
        client.set_child("STUDENT-9")

    assert client.selected_child_id == "STUDENT-1", "the selection is unchanged"


def test_the_client_refuses_to_refresh_in_place() -> None:
    """Defect 3, refused rather than fixed.

    Upstream's ``refresh()`` re-logs in and rewrites the resource slot with the
    *parent's* resource, and only ``__init__`` rebuilds the child selection.
    After an automatic refresh, ``post()`` still stamps the right ``membre``
    while ``lessons()`` sends the parent's resource in the body -- a
    disagreement no error surfaces. Raising is the only honest answer available
    to a subclass.
    """
    client = _client(children=CHILDREN)

    with pytest.raises(NotImplementedError, match="does not refresh in place"):
        client.refresh()


def test_the_client_refuses_the_active_keep_alive() -> None:
    """It only pays off below a 110-second collection interval (§6.5).

    No tier goes there, so a keep-alive would be a request per interval spent
    to save nothing -- against a limiter whose whole purpose is that every
    request is accounted for.
    """
    client = _client()

    with pytest.raises(NotImplementedError, match="keep-alive is not used"):
        client.keep_alive()


def test_closing_drops_the_transport() -> None:
    """What ``close()`` does, stated so that what it does not do is visible.

    There is no logout in the protocol as pronotepy exposes it; this throws
    away the TCP pool and the cookie jar on our side only. The server-side
    session lives on until its own inactivity timeout, which is why
    reconnecting once per batch created 64 *overlapping* sessions a day.
    """
    client = _client()

    client.close()

    assert client.communication.session.closes == 1


def test_closing_a_transport_that_is_already_gone_still_succeeds() -> None:
    """``close()`` is called from ``finally`` blocks and from unload.

    Letting an exception out of it would abort the teardown that follows -- the
    period release, the timer cancellation -- and replace a clean unload with a
    config entry stuck in an unloading state.
    """
    with _upstream_login_replaced():
        client = HardenedClient(URL, "parent-under-test", "not-a-real-password")
    client.communication = _RecordingCommunication(close_raises=True)  # type: ignore[assignment]

    client.close()

    assert client.communication.session.closes == 1


# ---------------------------------------------------------------------------
# The two small helpers the session manager classifies on
# ---------------------------------------------------------------------------


def test_a_protocol_refusal_carries_its_erreur_g_code() -> None:
    """The number the whole session strategy is decided on.

    ``G = 10`` means "log in again", ``G = 25`` means "wait", ``G = 22`` means
    "this is our bug". Reading the code off the exception is what keeps those
    three apart.
    """
    error = PronoteAPIError("Erreur.G = 10")
    error.pronote_error_code = 10

    assert protocol_error_code(error) == 10


@pytest.mark.parametrize(
    "error",
    [
        BootstrapUnavailable("no Start block"),
        requests.ConnectionError("connection refused"),
        TimeoutError("the executor gave up"),
        PronoteAPIError("Unable to connect to pronote, please try again later"),
    ],
)
def test_a_failure_that_never_reached_the_protocol_carries_no_code(
    error: BaseException,
) -> None:
    """``None`` is a verdict here, not a missing value.

    It is what sends an HTTP 502, a DNS failure and a missing bootstrap block
    to the exponential backoff instead of to the "session expired, log in
    again" arm -- which would answer a dead server with a login attempt every
    tick.
    """
    assert protocol_error_code(error) is None


def test_the_credential_snapshot_carries_the_token_the_server_rotated() -> None:
    """§7.2: in token mode every login hands back a new bearer.

    pronotepy overwrites ``self.password`` with the fresh
    ``jetonConnexionAppliMobile``, so a login that is not followed by a write
    leaves storage holding a dead token -- and the symptom appears only at the
    next restart, as a QR code that has to be scanned again.
    """
    client = _client()
    client.password = "rotating-sentinel-2"

    credentials = export_credentials(client)

    assert credentials["password"] == "rotating-sentinel-2"
    assert credentials["username"] == "parent-under-test"


def test_the_credential_snapshot_is_a_copy_the_caller_may_keep() -> None:
    """It crosses into the config entry, so it must not alias client state.

    The session manager hands this straight to the entry updater; if it were
    the client's own dict, a later rotation would mutate what Home Assistant
    believes it has already persisted.
    """
    client = _client()

    first = export_credentials(client)
    client.password = "rotating-sentinel-3"
    second = export_credentials(client)

    assert first["password"] == "not-a-real-password"
    assert second["password"] == "rotating-sentinel-3"


# ---------------------------------------------------------------------------
# The identification diagnostic
#
# This exists because upstream's own is unusable. `pronotepy.clients` has no
# logger of its own -- `clients.log is pronoteAPI.log` -- so the DEBUG line
# that would show the `Identification` response shares a logger with
# `pronoteAPI` line 139, which writes the hex of every request body,
# credentials included. Turning that on to read one boolean writes a parent's
# token into a file they are then invited to attach to a public issue (§8.2).
#
# So the client reads the response it already holds. What it logs is the three
# values that decide whether the login can possibly succeed, and nothing else.
# ---------------------------------------------------------------------------


def _identification(**data: Any) -> dict[str, Any]:
    """An `Identification` response of the shape upstream reads."""
    return {"dataSec": {"data": data}}


def test_the_flags_that_decide_the_key_are_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``modeCompMdp`` is the one that matters, and it is invisible otherwise.

    When it is true, ``ClientBase._login`` lowercases the password *before*
    hashing it. Harmless for a human password; destructive for a token or a QR
    code's ``jeton``, which are case-sensitive -- and the resulting failure is
    a challenge that will not decrypt, which is indistinguishable from a wrong
    token. Six real login attempts were spent on that ambiguity.
    """
    caplog.set_level(logging.DEBUG, logger=hardened_client.__name__)

    hardened_client._log_identification(
        _identification(modeCompLog=True, modeCompMdp=True, alea="abc", challenge="ff"),
        "STUDENT-1",
        "0badc0de",
        "qr_code",
    )

    assert "modeCompLog=True" in caplog.text
    assert "modeCompMdp=True" in caplog.text
    assert "alea=present(3 chars)" in caplog.text


def test_an_absent_salt_is_reported_as_absent_and_not_as_empty(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``alea`` changes the key, so "missing" and "empty" are one fact here.

    ``data.get("alea", "")`` is upstream's own default, and a response without
    the key derives a different key from one carrying an empty string only if
    something else differs -- so the log says which of the two it saw rather
    than flattening them into ``alea=``.
    """
    caplog.set_level(logging.DEBUG, logger=hardened_client.__name__)

    hardened_client._log_identification(
        _identification(modeCompLog=False), "student-1", "0badc0de", "normal"
    )

    assert "alea=absent(0 chars)" in caplog.text


def test_the_challenge_and_the_credentials_are_never_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The point is to make the pronotepy logger unnecessary, not to copy it.

    The whole reason this function exists is that upstream's DEBUG line cannot
    be enabled without writing credentials to disk. A diagnostic that leaked
    the challenge -- the AES material the login turns on -- would have
    reproduced the problem it was written to avoid. Only the *key names* of the
    response are recorded, which is enough to tell a shape change from a value
    change.
    """
    caplog.set_level(logging.DEBUG, logger=hardened_client.__name__)
    secret = "SENTINEL-CHALLENGE-DO-NOT-LEAK"

    hardened_client._log_identification(
        _identification(modeCompLog=False, modeCompMdp=False, challenge=secret),
        secret,
        secret,
        "normal",
    )

    assert secret not in caplog.text
    # The key name is there, because a response that stopped carrying a
    # challenge at all is a finding in itself.
    assert "challenge" in caplog.text


def test_a_response_of_the_wrong_shape_says_so_instead_of_raising(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A diagnostic that crashes the login it was added to observe is worse
    than no diagnostic.

    This runs inside ``post``, on the path of every request, so its failure
    mode has to be a log line. The response shape is upstream's and can change
    under us -- which is exactly the kind of thing worth being told about
    rather than being killed by.
    """
    caplog.set_level(logging.DEBUG, logger=hardened_client.__name__)

    hardened_client._log_identification(
        {"unexpected": "shape"}, "student-1", "0badc0de", "normal"
    )

    assert "no data section" in caplog.text


def test_the_credential_shapes_are_logged_without_the_credentials(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Reading ``modeCompLog=1`` is not enough to know it did any harm.

    A case-folding flag only matters if the value it folds has an upper-case
    character in it, so the log carries each credential's length, whether
    lowercasing would change it, and whether it looks like an opaque
    hexadecimal identifier. That is the difference between a cause and a
    coincidence -- and none of the three narrows the value usefully, which is
    why they are the only three.
    """
    caplog.set_level(logging.DEBUG, logger=hardened_client.__name__)
    secret = "SENTINEL-LOGIN-DO-NOT-LEAK"

    hardened_client._log_identification(
        _identification(modeCompLog=True, modeCompMdp=False),
        secret,
        "0badc0de",
        "qr_code",
    )

    assert secret not in caplog.text
    assert f"login={len(secret)}ch/lower=False/hex=False" in caplog.text
    assert "token=8ch/lower=True/hex=True" in caplog.text
    assert "mode=qr_code" in caplog.text
