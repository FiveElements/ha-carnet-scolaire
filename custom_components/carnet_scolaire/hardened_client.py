"""The hardened pronotepy client -- a prerequisite, not an option (§3.6).

Four upstream behaviours make the plain client unusable behind a rate limiter
that claims to be the only path to the network. All four are verified against
pronotepy 2.15.7.

**1. It re-authenticates behind the caller's back.** ``ClientBase.post``
catches *every* ``PronoteAPIError`` except ``ExpiredObject``, calls
``self.refresh()`` -- ``session.close()``, a GET on the HTML page,
``FonctionParametres``, ``Identification``, ``Authentification``,
``ParametresUtilisateur`` -- and then replays the call. One budgeted call can
therefore cost six network requests, so "the limiter is the only path to the
gateway" is false as written. Worse, ``Erreur.G = 25`` ("too many authorization
requests") is an ordinary ``PronoteAPIError``, so it provokes exactly the class
of request that ``G = 25`` punishes.

**2. ``ParentClient.post`` duplicates that handler without the recursion
guard.** ``ClientBase.post`` protects itself (``if self._refreshing: raise e``);
``ParentClient.post`` does not. A server erroring during ``refresh()``'s
``_login()`` yields unbounded recursion of full logins -- on a parent account,
which is this project's whole use case.

**3. ``refresh()`` silently loses the child selection.** It calls ``_login()``,
which rewrites ``parametres_utilisateur[...]["ressource"]`` with the *parent's*
resource, and ``ParentClient`` only rebuilds ``_selected_child`` in
``__init__``. After an automatic refresh, ``post()`` still stamps the right
``membre`` but ``lessons()`` sends the parent's resource in the body.

**4. There is no HTTP timeout anywhere.** ``grep -rn timeout`` over the package
returns nothing: neither the bootstrap GET nor the POSTs pass ``timeout=``. A
server that accepts the connection and never answers pins the single worker
thread for ever, the ``asyncio.Lock`` is never released, and §5.4's staleness
regime disguises a permanent deadlock as a slow network.

So: this module owns one client class whose ``post()`` never re-authenticates,
which stamps the child signature itself, and whose transport carries timeouts.
Re-login becomes the :class:`~.session.SessionManager`'s decision, counted by
the limiter -- and that is what makes ``Erreur.G = 10`` an observable, and
therefore measurable, event (§6.5).
"""

from __future__ import annotations

import contextlib
import logging
import re
import threading
from typing import TYPE_CHECKING, Any, Final

import pronotepy
from pronotepy import dataClasses, pronoteAPI
from pronotepy.exceptions import PronoteAPIError
import requests

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping

_LOGGER: Final = logging.getLogger(__name__)

#: The block the bootstrap page must carry. Its **absence** is the only
#: bootstrap test this integration accepts (§6.3).
_START_BLOCK: Final = re.compile(r"Start ?\({(?P<param>[^}]*)}\)")

#: A password field. PRONOTE's bootstrap page has none -- its login form is
#: built by script -- so a page that carries one and no ``Start`` block is
#: somebody else's login form: the portal the establishment delegates to.
_PASSWORD_FIELD: Final = re.compile(
    r"<input\b[^>]*\btype\s*=\s*[\"']?password\b", re.IGNORECASE
)

#: ``pronotepy.dataClasses.Period.instances`` is a mutable class attribute,
#: global to the process, and it is never cleared. Two config entries therefore
#: share a channel one can write while the other reads. Confining it needs a
#: process-wide lock, not a per-entry one.
#:
#: It guards **only** the set arithmetic, which takes microseconds. It used to
#: be held across the whole of ``ClientBase.__init__`` -- that is, across a
#: login: one GET plus four to six POSTs -- which turned every
#: ``release_client`` into a blocking wait on another entry's network round
#: trip. Called from the event loop, that froze Home Assistant outright.
_REGISTRY_LOCK: Final = threading.Lock()

#: Per-thread HTTP timeouts for the login currently running on this thread.
#:
#: A thread-local rather than a class attribute plus a lock. Each config entry
#: owns a single-worker executor, so "the login running on this thread" is
#: exactly the right scope: two entries with different ``read_timeout`` values
#: cannot contaminate each other, and no lock has to be held across a network
#: call for that to be true.
_TIMEOUTS: Final = threading.local()

#: Serialises the one-time installation of the hardened transport.
_INSTALL_LOCK: Final = threading.Lock()
_installed = False


class BootstrapUnavailable(PronoteAPIError):  # noqa: N818 -- a condition, not an error class
    """The bootstrap page did not carry a usable ``Start({…})`` block.

    Deliberately says nothing about *why*. pronotepy decides an address is
    suspended with ``if "IP" in html`` -- two capitals anywhere in the page, so
    a school writing "Espace IP" in a footer, or a CSS class, or the letters
    inside ``SKIP``, ``ZIP`` or ``EQUIPE``, all trigger it. Specification v1
    built an hour-long hold, a full stop to every call, and a repair issue with
    "explicit text" on that signal: it would have told parents their home
    address was banned because of a footer.

    A state the integration cannot establish reliably must not exist in its
    vocabulary. The repair opened for this exception enumerates the possible
    causes and picks none (§6.3).
    """


class BootstrapDelegated(BootstrapUnavailable):
    """The address answered with a login form that is not PRONOTE's.

    An establishment that delegates authentication to its ENT has PRONOTE
    redirect every visit, direct login included, to the portal's own login
    page. That page has a password field and no ``Start`` block, and until
    this class existed it was reported as the generic "no session page" --
    true, and useless to a parent whose address was right, whose space was
    open, and who needed only to pick the ENT login instead.

    Still a :class:`BootstrapUnavailable`, so every caller that does not know
    this finer verdict keeps handling it exactly as before. It names what the
    page *is*, which is observable, and nothing about the account.
    """


class _TimeoutSession(requests.Session):
    """A ``requests.Session`` that refuses to wait for ever.

    ``requests`` has no session-level default timeout, so the only way to give
    pronotepy one is to substitute the session object and inject the default in
    ``request()``. A caller passing an explicit ``timeout`` still wins.
    """

    def __init__(self, connect_timeout: float, read_timeout: float) -> None:
        super().__init__()
        self._default_timeout = (connect_timeout, read_timeout)

    def request(
        self,
        method: str | bytes,
        url: str | bytes,
        *args: Any,
        **kwargs: Any,
    ) -> requests.Response:
        kwargs.setdefault("timeout", self._default_timeout)
        return super().request(method, url, *args, **kwargs)


class _HardenedCommunication(pronoteAPI._Communication):
    """Bootstrap transport with timeouts and honest failure classification.

    Two overrides, and no more: replacing the session so every request is
    bounded, and replacing ``_parse_html`` so the only bootstrap verdict is
    "the ``Start({…})`` block is missing" -- never a claim about the caller's
    IP address.
    """

    #: Fallback used when no login is in progress on this thread. The real
    #: values come from :data:`_TIMEOUTS`, published by
    #: :func:`_patched_communication` on the worker thread that is about to
    #: construct the client.
    connect_timeout: float = 30.0
    read_timeout: float = 60.0

    def __init__(
        self,
        site: str,
        cookies: requests.cookies.RequestsCookieJar | None,
        mobile: bool,  # noqa: FBT001 -- upstream's positional signature
    ) -> None:
        super().__init__(site, cookies, mobile)
        connect, read = _current_timeouts()
        headers = dict(self.session.headers)
        self.session.close()
        self.session = _TimeoutSession(connect, read)
        self.session.headers.update(headers)

    def _parse_html(self, html: str) -> dict[str, str]:
        """Parse the bootstrap attributes, without guessing at causes.

        Three deliberate divergences from upstream, listed because "mirrors
        upstream" would send a maintainer looking for code that is not here:

        * the ``if "IP" in html`` check is dropped entirely -- see
          :class:`BootstrapUnavailable`;
        * attributes are split with ``partition(":")`` rather than
          ``split(":")``. Upstream's version raises ``ValueError`` on any
          attribute containing no colon or two of them, which in practice is
          the main producer of its three-GET retry loop. ``partition`` cannot
          raise, so a garbled block fails once and clearly instead of three
          times and obscurely. The retry contract still holds through the
          missing-``h`` case below, which is the one ``initialise()`` catches;
        * upstream also builds a ``BeautifulSoup`` whose result it never uses.
          Skipping it saves a parse on every login.
        """
        match = _START_BLOCK.search(html)
        if not match and _PASSWORD_FIELD.search(html):
            raise BootstrapDelegated(
                "The PRONOTE address answered with another service's login form"
            )
        if not match:
            raise BootstrapUnavailable(
                "The PRONOTE bootstrap page did not contain a Start({...}) block"
            )

        attributes: dict[str, str] = {}
        for attribute in match.group("param").split(","):
            key, _, value = attribute.partition(":")
            attributes[key.strip().strip("'\"")] = value.strip().strip("'\"")

        if "h" not in attributes:
            # Upstream signals "retry" with ValueError; initialise() catches it.
            message = "bootstrap attributes carry no session id"
            raise ValueError(message)

        return attributes


def _current_timeouts() -> tuple[float, float]:
    """The HTTP timeouts for whatever login is running on this thread."""
    value: tuple[float, float] | None = getattr(_TIMEOUTS, "value", None)
    if value is None:
        return (
            _HardenedCommunication.connect_timeout,
            _HardenedCommunication.read_timeout,
        )
    return value


def install_hardened_transport() -> None:
    """Install :class:`_HardenedCommunication` once, for the process.

    pronotepy resolves ``_Communication`` as a module global inside
    ``ClientBase.__init__``, between creating the transport and bootstrapping
    it -- there is no seam between the two, so substituting the module
    attribute is the only way to get a timeout onto the bootstrap GET without
    issuing a second one.

    Installed **permanently and idempotently** rather than around each login.
    The scoped version had to hold a process-wide lock across the whole
    construction -- a full network login -- so that two entries could not race
    on the module attribute; and that lock then blocked every other entry's
    teardown for the length of somebody else's handshake. A permanent
    installation needs no lock at all, because nothing is left to race on: the
    class is the same object for everybody, and the only per-login value, the
    timeout pair, lives in a thread-local.

    This integration is the only consumer of pronotepy in the process, and the
    substitution is strictly a hardening -- bounded requests, and one fewer
    unfounded claim about the caller's IP address -- so making it permanent
    costs nothing and removes a whole class of contention.
    """
    global _installed  # noqa: PLW0603 -- one idempotent, process-wide install
    with _INSTALL_LOCK:
        if _installed:
            return
        # Substituting a class for a class is the whole point here; the
        # checker only objects because the target name is itself a type.
        pronotepy.clients._Communication = _HardenedCommunication  # type: ignore[misc]
        _installed = True


@contextlib.contextmanager
def _patched_communication(
    connect_timeout: float,
    read_timeout: float,
) -> Iterator[None]:
    """Publish this login's timeouts to the thread about to perform it.

    Cheap by construction: it sets a thread-local, makes sure the transport is
    installed, and restores the previous value on the way out. No lock is held
    across the login, so a hung server delays one entry's worker thread and
    nothing else.

    One limitation is worth stating rather than hiding. In ENT mode upstream
    calls ``ent(username, password, pronote_url=…)`` *before* ``_Communication``
    exists, and every ENT helper builds its own ``requests.Session`` with no
    timeout -- ``grep -rn timeout`` over ``pronotepy/ent/`` returns nothing. An
    identity provider that accepts the connection and never answers therefore
    hangs that worker thread. What bounds the damage is the executor's own
    deadline, which abandons the call from the event loop's point of view; the
    thread is leaked, which §3.2 already accepts as the better half of the
    trade.
    """
    install_hardened_transport()
    previous: tuple[float, float] | None = getattr(_TIMEOUTS, "value", None)
    _TIMEOUTS.value = (connect_timeout, read_timeout)
    try:
        yield
    finally:
        _TIMEOUTS.value = previous


def _shape(value: str) -> str:
    """Describe a credential without disclosing it.

    Three facts, and they are the three the key agreement turns on: how long it
    is, whether lowercasing it would change it, and whether it looks like an
    opaque hexadecimal identifier rather than something a human typed. None of
    them narrows the value usefully -- a length and a character class leave the
    search space astronomically large -- and without them the failure below
    cannot be told apart from a wrong token.
    """
    hexish = bool(value) and all(char in "0123456789abcdefABCDEF" for char in value)
    return f"{len(value)}ch/lower={value == value.lower()}/hex={hexish}"


def _log_identification(
    response: Mapping[str, Any], username: str, password: str, login_mode: str
) -> None:
    """Log what decides whether a login can possibly succeed.

    This exists because upstream's own diagnostic is unusable here.
    ``pronotepy.clients`` has no logger of its own -- ``clients.log is
    pronoteAPI.log`` -- so the DEBUG line that would show the ``Identification``
    response shares a logger with ``pronoteAPI`` line 139, which writes the hex
    of every request body, credentials included (§8.2). Turning that on to read
    one field would write a parent's token into a file they are then invited to
    attach to a public issue.

    So the client reads the response it already holds, and logs only what
    decides the key agreement in ``ClientBase._login``, which is::

        key = username[.lower()] + SHA256(alea + password[.lower()]).upper()

    * ``modeCompLog`` -- when true, upstream lowercases the *username* before
      putting it in the key.
    * ``modeCompMdp`` -- the same for the password.
    * ``alea`` -- the salt prefixed to the password before the SHA-256. Its
      presence changes the key, so "absent" and "empty" are recorded apart.
    * the two credentials' *shapes*, because a case-folding flag only matters
      if the value it folds actually has an upper-case character in it. Without
      that, reading ``modeCompLog=1`` says nothing about whether it did any
      harm -- which is the difference between a cause and a coincidence.

    Never the ``challenge`` and never a credential's content: the point of this
    function is to make the pronotepy logger unnecessary, not to reproduce it.
    """
    try:
        data = response["dataSec"]["data"]
    except KeyError, TypeError:
        _LOGGER.debug("the identification response had no data section")
        return

    alea = data.get("alea") or ""
    _LOGGER.debug(
        "identification: mode=%s modeCompLog=%s modeCompMdp=%s alea=%s(%d chars) "
        "login=%s token=%s keys=%s",
        login_mode,
        data.get("modeCompLog"),
        data.get("modeCompMdp"),
        "present" if alea else "absent",
        len(str(alea)),
        _shape(username),
        _shape(password),
        sorted(data),
    )


class HardenedClient(pronotepy.Client):
    """A PRONOTE client that never reconnects on its own.

    Handles the student and the parent account with one class. Upstream splits
    them into ``Client`` and ``ParentClient``, but ``ParentClient`` is the
    subclass carrying the recursion bug and the lost-child bug, and its
    ``refresh()`` cannot be salvaged -- so the parent behaviour (a child list,
    a selected child, the ``membre`` signature) is reimplemented here, on top
    of the base whose ``post()`` we replace anyway.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        # Set *before* the base constructor, which logs in, which posts, which
        # reaches our own `post()` -- and `post()` reads this attribute. It only
        # survived because none of upstream's three login posts passes an
        # `onglet`, so the branch that reads it was skipped. The day one of them
        # gains a tab, every login would raise `AttributeError`, which is in
        # neither `_DECODING_ERRORS` nor the set-up handler's `except` chain: a
        # raw traceback and no classification at all.
        self._selected_child: dataClasses.ClientInfo | None = None

        super().__init__(*args, **kwargs)

        resource = self.parametres_utilisateur["dataSec"]["data"]["ressource"]
        raw_children = resource.get("listeRessources") or []

        #: Non-empty exactly when this is a parent account.
        self.children: list[dataClasses.ClientInfo] = [
            dataClasses.ClientInfo(self, child) for child in raw_children
        ]
        self._parent_resource: dict[str, Any] = resource

        if self.children:
            self.set_child(self.children[0])

    # -- child selection ---------------------------------------------------

    @property
    def is_parent_account(self) -> bool:
        """True when the account holds children rather than being a student."""
        return bool(self.children)

    @property
    def selected_child_id(self) -> str | None:
        """Identifier of the child currently stamped on outgoing requests."""
        return self._selected_child.id if self._selected_child else None

    def set_child(self, child: dataClasses.ClientInfo | str) -> None:
        """Point the client at one child.

        This **mutates** the client: the selected child's resource replaces
        ``parametres_utilisateur["dataSec"]["data"]["ressource"]``, which is the
        same slot ``Client.lessons()`` reads to build its request body. That is
        why the atomic unit of work is ``(child, tier)`` and never ``tier``
        alone -- interleaving ``set_child(A) · post() · set_child(B) · post()``
        at the wrong granularity publishes child B's timetable under child A's
        entities, silently (§3.2).
        """
        if isinstance(child, str):
            match = next((c for c in self.children if c.id == child), None)
            if match is None:
                raise pronotepy.ChildNotFound(f"no child with id {child!r}")
            child = match

        self._selected_child = child
        self.parametres_utilisateur["dataSec"]["data"]["ressource"] = child.raw_resource
        self.info = child

    # -- the point of this class -------------------------------------------

    def post(
        self,
        function_name: str,
        onglet: int | None = None,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Post once. Never re-authenticate.

        Deliberately *not* wrapped in a ``try``. Every protocol error --
        ``G = 10`` (session expired), ``G = 25`` (too many authorization
        requests), ``G = 22`` (object from a previous session) -- propagates to
        the caller, which is the only way the session manager can decide what
        to do and the limiter can count what it costs.
        """
        post_data: dict[str, Any] = {}
        if onglet:
            signature: dict[str, Any] = {"onglet": onglet}
            if self._selected_child is not None:
                signature["membre"] = {"N": self._selected_child.id, "G": 4}
            post_data["Signature"] = signature
        if function_name == "Identification" and data:
            # The official app's body ends with this key, null outside an
            # enrolment; upstream omits it. A session opened without it is
            # refused a graded test's paper the app is served -- this is the
            # last difference the two logins showed.
            data = {**data, "informationsAppareil": None}
        if data:
            post_data["data"] = data

        result: dict[str, Any] = self.communication.post(function_name, post_data)
        if function_name == "Identification":
            # Read before the key is derived, because everything the failure
            # turns on is in this one response and nothing else ever shows it.
            username = str(getattr(self, "username", "") or "")
            password = str(getattr(self, "password", "") or "")
            mode = str(getattr(self, "login_mode", "") or "")
            _log_identification(result, username, password, mode)
        return result

    def refresh(self) -> None:
        """Refuse to refresh in place.

        Upstream's ``refresh()`` rebuilds the transport and re-logs in but does
        *not* restore the child selection, so a parent account silently starts
        requesting the parent's own resource. The session manager discards the
        client and builds a new one instead, which re-derives the child list by
        construction.
        """
        message = (
            "HardenedClient does not refresh in place; the session manager "
            "builds a fresh client instead (see docs/SPECIFICATION.md §3.6)"
        )
        raise NotImplementedError(message)

    def keep_alive(self) -> pronoteAPI._KeepAlive:
        """Refuse the active keep-alive.

        It only pays off below a 110-second collection interval, and no tier
        goes there (§6.5).
        """
        message = "the active keep-alive is not used; see docs/SPECIFICATION.md §6.5"
        raise NotImplementedError(message)

    def close(self) -> None:
        """Drop the client-side transport.

        Worth being precise about what this does *not* do: there is no logout in
        the protocol as pronotepy exposes it -- ``grep -n "Deconnexion|logout"``
        returns nothing. This throws away the TCP pool and the cookie jar on our
        side; the server-side session lives until its own inactivity timeout.
        That asymmetry is exactly why reconnecting once per batch created 64
        *overlapping* sessions a day rather than 64 successive ones (§6.5).
        """
        with contextlib.suppress(Exception):
            self.communication.session.close()


def build_client(
    *,
    login_mode: str,
    pronote_url: str,
    username: str,
    password: str,
    uuid: str = "",
    account_pin: str | None = None,
    client_identifier: str | None = None,
    device_name: str | None = None,
    ent: Callable[..., Any] | None = None,
    connect_timeout: float = 30.0,
    read_timeout: float = 60.0,
) -> HardenedClient:
    """Construct and log in a hardened client.

    ``ClientBase.__init__`` ends with ``self.logged_in = self._login()``, so
    **constructing the client is a network login**. This function must therefore
    run in the executor, under the account's lock; ``async_setup_entry`` never
    calls it from the event loop (§3.2).
    """
    mode = "token" if login_mode == "qr_code" else "normal"

    with (
        _patched_communication(connect_timeout, read_timeout),
        _period_registry_confined() as confine,
    ):
        client = HardenedClient(
            pronote_url,
            username,
            password,
            ent=ent,
            mode=mode,
            uuid=uuid,
            account_pin=account_pin,
            client_identifier=client_identifier,
            device_name=device_name,
        )
        confine(client)

    return client


class _ConfinedRegistry:
    """Tracks which ``Period`` objects a given client put in the global set."""

    __slots__ = ("_owned",)

    def __init__(self) -> None:
        self._owned: set[dataClasses.Period] = set()

    def adopt(self, before: set[dataClasses.Period]) -> None:
        """Record the periods that appeared while a client was being built."""
        self._owned = set(dataClasses.Period.instances) - before

    def release(self) -> None:
        """Remove this client's periods from the process-global registry.

        By **difference**, never by clearing. Clearing would make
        ``Util.get(Period.instances, id=…)`` return an empty list, so
        ``Grade.__init__``'s ``[0]`` would raise ``IndexError``, wrapped as
        ``ParsingError``, and the whole marks batch would fail.

        This is safe here for one specific reason, and the two decisions are
        coupled: the gateway decodes grades itself (§3.3.2) and therefore never
        constructs a ``pronotepy.Grade``, which is the *only* reader of this
        registry in all of ``dataClasses.py``. Nothing reads it while we mutate
        it. If a future change starts using ``pronotepy.Grade``, this
        confinement has to be revisited at the same time.
        """
        with _REGISTRY_LOCK:
            dataClasses.Period.instances -= self._owned
        self._owned = set()


@contextlib.contextmanager
def _period_registry_confined() -> Iterator[Callable[[HardenedClient], None]]:
    """Attach a :class:`_ConfinedRegistry` to the client being constructed.

    Without this, every login leaks: each ``Period`` stays in the class-level
    set for ever and keeps its ``_client`` alive, so a dead client, a dead
    ``requests.Session`` and a dead socket pool with it.
    """
    with _REGISTRY_LOCK:
        before = set(dataClasses.Period.instances)
    registry = _ConfinedRegistry()
    attached = False

    def attach(client: HardenedClient) -> None:
        nonlocal attached
        registry.adopt(before)
        client.period_registry = registry  # type: ignore[attr-defined]
        attached = True

    try:
        yield attach
    finally:
        if not attached:
            # The constructor raised. Upstream runs `self.periods_ = self.periods`
            # *before* `self.logged_in = self._login()`, so an account with 2FA
            # and no stored PIN -- which raises `MFAError` on every attempt --
            # had already put its periods in the global set, each one holding
            # its client, its session and its socket pool. `attach` never ran,
            # so nothing removed them, and every retry added more.
            #
            # Sweeping "everything that appeared" could in principle also
            # remove a period another entry registered concurrently. That is
            # harmless, and harmless for a reason we can state: the only reader
            # of this registry in all of `dataClasses.py` is `Grade.__init__`,
            # and the gateway decodes grades itself (§3.3.2) so it never
            # constructs one. The coupling that makes `release()` safe makes
            # this safe.
            with _REGISTRY_LOCK:
                leaked = set(dataClasses.Period.instances) - before
                dataClasses.Period.instances -= leaked
            if leaked:
                _LOGGER.debug(
                    "login failed after registering %d period(s); removed them "
                    "from the global registry",
                    len(leaked),
                )


def release_client(client: HardenedClient | None) -> None:
    """Close a client and give back the periods it registered."""
    if client is None:
        return
    registry: _ConfinedRegistry | None = getattr(client, "period_registry", None)
    if registry is not None:
        registry.release()
    client.close()


def protocol_error_code(error: BaseException) -> int | None:
    """Return the ``Erreur.G`` code carried by an exception, if any.

    Protocol errors raised from ``_Communication.post`` carry
    ``pronote_error_code``; transport and bootstrap failures do not. That
    distinction is what lets ``G = 10`` be treated as "session expired, log in
    again" while an HTTP 502 goes to the exponential backoff instead.
    """
    return getattr(error, "pronote_error_code", None)


def export_credentials(client: HardenedClient) -> Mapping[str, Any]:
    """Snapshot the credentials to persist after a successful login.

    In token mode every ``Authentification`` returns a fresh
    ``jetonConnexionAppliMobile`` and pronotepy overwrites ``self.password``
    with it. Failing to re-persist it means losing access at the next start
    (§7.2).
    """
    credentials: dict[str, Any] = dict(client.export_credentials())
    return credentials
