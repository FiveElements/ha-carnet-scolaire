"""Session ownership: one thread, one lock, and a measured reconnection policy.

Two concerns live here, and they are related.

**Serialisation.** The protocol numbers requests with an encrypted counter --
``_Communication.post`` encrypts ``request_number``, writes it into the URL path
as well, and advances by two. Two concurrent calls on one session desynchronise
that counter and break the session. ``hass.async_add_executor_job`` uses a
shared thread pool, so two jobs started together *are* concurrent; hence one
single-worker executor and one ``asyncio.Lock`` per config entry (§3.2).

The lock covers more than the call. ``set_child()`` **mutates** the client by
rewriting ``parametres_utilisateur["dataSec"]["data"]["ressource"]``, which is
the same slot ``Client.lessons()`` reads to build its request body. So the
atomic unit of work is ``(child, tier)`` -- never ``tier`` alone, or a parent
account publishes one child's timetable under the other child's entities,
silently.

It also has to cover the *choice of client*. A second gate, ``_gate``, is held
across "pick a client, wait for budget, call, retry" as one unit. Without it a
service invoked during a tick captured a client, awaited its spacing, and woke
up to post on a client that a concurrent ``Erreur.G = 10`` retry had already
closed -- producing a spurious expiry sample, a third login, and a failed tier
from a single overlap.

**Reconnection.** Specification v1 closed the session between batches on the
strength of a cost model that was wrong three times over (annexe B §5.1). The
strategy here is lazy: keep the session, and log in again only when the server
says it expired.

Its defensibility rests on a dominance argument, and the argument has to be
stated precisely because a loose version of it is false. Lazy is not free in
the bad case: if the establishment's inactivity timeout is *shorter* than the
fastest tier's interval, every batch spends one wasted request discovering the
session is dead before logging in -- one request per batch worse than
per-batch, not equal to it. What bounds that is the degradation: three
consecutive expiries make this module conclude the timeout is short and
reconnect eagerly, so the waste is three requests, once, and then one a day
from the probe that keeps the conclusion falsifiable. Under that bound lazy is
never meaningfully worse for any value of the unknown parameter, and it is
dramatically better for the common one.

The degradation only works if a success does not casually undo it. Lifting the
conclusion requires *counter-evidence* -- a session that survived an inactivity
gap at least as long as the shortest one that previously expired -- and not
merely the next call after a fresh login succeeding, which it always does.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING, Any, Final

from pronotepy import ChildNotFound
from pronotepy.exceptions import (
    CryptoError,
    DataError,
    ExpiredObject,
    MFAError,
    PronoteAPIError,
)

from .const import (
    CALL_TIMEOUT_FACTOR,
    LoginMode,
    Priority,
    SessionStrategy,
)
from .ent_providers import resolve_ent_provider
from .failures import describe_failure
from .hardened_client import (
    BootstrapUnavailable,
    build_client,
    export_credentials,
    protocol_error_code,
    release_client,
)
from .models import SessionLifetime
from .ratelimit import (
    REQUESTS_PER_LOGIN,
    LoginOutcome,
    LoginRefusedByLimiter,
    TierDeferred,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping, Sequence
    from datetime import datetime

    from .hardened_client import HardenedClient
    from .ratelimit import RateLimiter

_LOGGER: Final = logging.getLogger(__name__)

#: ``Erreur.G`` codes we must recognise by number rather than by message.
ERROR_SESSION_EXPIRED: Final = 10
ERROR_PAGE_EXPIRED: Final = 8
ERROR_EXPIRED_OBJECT: Final = 22
ERROR_TOO_MANY_AUTHORIZATIONS: Final = 25

#: Every code that means "the session you are holding is gone; open another".
#:
#: 10 is the documented one and was for a long time the only one here. **8 is
#: the one a live establishment actually sent**, with ``Erreur.Titre`` reading
#: "La page a expiré !" -- the same statement in different words. It has to be
#: recognised by number, because ``pronotepy`` has no entry for 8 in
#: ``error_messages`` and therefore renders it as ``Unknown error from
#: pronote: 8 | La page a expiré !``. That string is a *message*, not a
#: classification; the code is on the exception either way
#: (``pronoteAPI.py:191`` attaches ``pronote_error_code`` for every code but
#: 22).
#:
#: Treating 8 as an unclassified refusal cost the live instance seven hours of
#: frozen data, and the shape of that failure is the argument for this set
#: existing at all. The session was dead server-side while
#: ``SessionManager.is_open`` still answered ``True``, so no branch ever
#: reached :meth:`SessionManager._reopen`; every attempt failed, each failure
#: deepened the exponential backoff, and no call could succeed to reset it.
#: That is not a pause that resolves -- it is a well, and only a restart got
#: the instance out of it. An unrecognised expiry code is therefore not a
#: cosmetic gap: it is a permanent outage with a plausible-looking limiter
#: state (``backoff``) on top of it.
SESSION_EXPIRED_CODES: Final = frozenset({ERROR_PAGE_EXPIRED, ERROR_SESSION_EXPIRED})

#: How long the presumption "the session I hold is usable" survives with not
#: one successful call to support it.
#:
#: This is the guard that catches the *next* unrecognised code, and it is the
#: reason :data:`SESSION_EXPIRED_CODES` is not the whole fix. A held client is
#: only ever evidence that a login once succeeded; after hours of silence it
#: is a belief, and the cheapest correct move is to stop holding it.
#:
#: One hour, derived rather than guessed: the default login cap is 24 a day,
#: which is one an hour, so a ceiling at or above 3600 s can never be the
#: reason that cap is reached. And it costs nothing in normal operation --
#: this fires lazily, at the next unit of work, so a quiet-hours night of
#: eight idle hours provokes exactly one extra login at 06:00 rather than one
#: per hour. A server whose real timeout is *shorter* than this is a different
#: problem, already handled by the expiry counter and the degradation to
#: ``PER_BATCH``.
PRESUMED_DEAD_AFTER_SECONDS: Final = 3600.0

#: How many consecutive expiries it takes to conclude the server's inactivity
#: timeout is shorter than our fastest tier. Three, because one expiry can be a
#: server restart and two can be a coincidence.
SHORT_TIMEOUT_EVIDENCE: Final = 3

#: Once "short timeout" is concluded, re-test it this often. A conclusion nobody
#: ever re-tests is an assumption wearing a measurement's clothes.
PROBE_INTERVAL_SECONDS: Final = 86400.0

#: Inactivity gaps below this are not recorded as lifetime samples. The first
#: call after a login can fail with ``Erreur.G = 10`` for reasons that have
#: nothing to do with a timeout -- a server restart, a session the establishment
#: invalidated -- and ``observed_minutes`` is a *minimum*, so one such sample
#: would pin the measured lifetime near zero for the rest of the run and
#: falsify both the diagnostic sensor and the options-page estimate.
MIN_LIFETIME_SAMPLE_SECONDS: Final = 60.0

#: Transport failures ``requests`` raises. Not one of them is a
#: ``PronoteAPIError``, and -- the trap -- not one of them is a
#: ``TimeoutError``: ``requests.Timeout`` inherits from ``OSError``, so
#: ``except TimeoutError`` caught nothing at all. A school down for a weekend
#: therefore produced an uncounted bootstrap GET every five minutes with no
#: backoff whatsoever, while ``calls_today`` reported zero. ``OSError`` alone
#: covers the family, built-in ``TimeoutError`` included.
_TRANSPORT_ERRORS: Final = (OSError,)

#: Decoding failures that can escape ``ClientBase.__init__``. ``ParsingError``
#: is a ``DataError``, which is **not** a ``PronoteAPIError``; and
#: ``clients.py`` parses ``PremierLundi`` with a bare ``strptime``, which raises
#: ``ValueError``. Both happen after real requests have gone out.
_DECODING_ERRORS: Final = (DataError, ValueError, KeyError, TypeError, IndexError)


class LoginRefused(Exception):  # noqa: N818 -- a decision, surfaced as control flow
    """The limiter refused a login attempt, or credentials are on hold."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"login refused: {reason}")
        self.reason = reason


class MfaRequired(Exception):  # noqa: N818 -- surfaced to open a re-auth flow
    """PRONOTE asked for the 2FA PIN, which is deliberately not stored.

    Specification v1 combined "never keep the PIN" with 64 logins a day, which
    is 64 daily chances to fail hard at three in the morning with no way to
    recover. The lazy strategy resolves the contradiction rather than masking
    it -- one to three logins a day -- and this exception opens a
    re-authentication flow that *asks the user for the PIN*. A secret you refuse
    to store is a secret you must know how to ask for again (§8.1).
    """


class InvalidCredentials(Exception):  # noqa: N818 -- surfaced to open a re-auth flow
    """``CryptoError``, or ``logged_in is False``.

    These are the two shapes a wrong password takes, and naming them is the
    whole point: a wrong password does **not** raise ``PronoteAPIError``, so a
    failure counter hooked to that would never increment and the guard rail
    meant to protect the IP address would exist only in the documentation
    (annexe B §3.1).
    """


class BootstrapFailed(Exception):  # noqa: N818 -- surfaced to open a repair issue
    """The bootstrap page carried no usable ``Start({…})`` block.

    Says nothing about the cause, on purpose. See
    :class:`~.hardened_client.BootstrapUnavailable`.
    """


class AccountUnreadable(Exception):  # noqa: N818 -- surfaced to open a repair issue
    """The login succeeded on the wire but its response could not be decoded.

    Distinct from every other failure because it is neither the credentials'
    fault nor a transport problem: something in this establishment's response
    is outside what the pinned ``pronotepy`` can parse. Retrying at the tier's
    cadence would spend requests for ever on a response that cannot improve, so
    it earns a hold -- and it must never touch the credentials guard, because
    nothing about the password is in question.
    """


class IntegrationFault(Exception):  # noqa: N818 -- a bug, surfaced as one
    """A client-side fault that must not be charged to the server's backoff.

    ``ChildNotFound`` is the live example: ``pronotepy`` makes it a
    ``PronoteAPIError``, so without this it opened a backoff hold on a
    perfectly healthy server because *this* integration asked for a child
    identifier that is no longer on the account.
    """


@dataclass(slots=True)
class SessionCredentials:
    """What is needed to log in, and what a login may rotate.

    ``password`` is the mobile application token in QR-code mode, and PRONOTE
    hands back a fresh one at every ``Authentification``. Failing to re-persist
    it means losing access at the next start (§7.2).
    """

    login_mode: LoginMode
    pronote_url: str
    username: str
    password: str
    uuid: str = ""
    client_identifier: str | None = None
    device_name: str | None = None
    account_pin: str | None = None
    ent_provider: str | None = None


class SerialExecutor:
    """One worker thread, one lock, and a shutdown that cannot hang.

    ``shutdown(wait=True)`` is never called from the event loop. pronotepy
    passes no HTTP timeout of its own -- ``grep -rn timeout`` over the package
    returns nothing -- so the hardened client injects one; but a thread can
    still be mid-request at unload time, and joining it would freeze Home
    Assistant's reload. A leaked thread beats a frozen instance (§3.2).
    """

    def __init__(self, entry_id: str, read_timeout: float) -> None:
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix=f"pronote-{entry_id[:8]}"
        )
        self._lock = asyncio.Lock()
        self._read_timeout = read_timeout
        self._closed = False

    @property
    def lock(self) -> asyncio.Lock:
        """The lock guarding every access to the client."""
        return self._lock

    @property
    def deadline(self) -> float:
        """How long a single call may take before it is abandoned."""
        return self._read_timeout * CALL_TIMEOUT_FACTOR

    async def run(self, fn: Callable[[], Any]) -> Any:
        """Run ``fn`` on the single worker, under the lock, with a deadline.

        The deadline is not redundant with the HTTP timeout. It bounds the one
        case the staleness regime of §5.4 would otherwise disguise: a call that
        never returns, whose symptom is entities quietly ageing. The user-facing
        log for that outage is the session's one INFO (Silver
        ``log-when-unavailable``), not a line per abandoned call.

        What happens after the deadline is worth being exact about, because the
        obvious guess is wrong. The abandoned callable keeps running in the
        worker, but the pool has a single thread, so the *next* submission does
        not run beside it -- it waits in the pool's queue and burns its own
        deadline there. Nothing can therefore desynchronise the encrypted
        request counter this way. The real cost is a stall, which is why the
        caller marks the account as failing rather than retrying.
        """
        if self._closed:
            message = "the account's executor has been shut down"
            raise RuntimeError(message)

        async with self._lock:
            loop = asyncio.get_running_loop()
            future = loop.run_in_executor(self._executor, fn)
            try:
                return await asyncio.wait_for(future, timeout=self.deadline)
            except TimeoutError:
                # debug(), not error(): the Silver rule ``log-when-unavailable``
                # allows one INFO for the outage, on the session, not one error
                # per abandoned call. The stall mechanics still belong here for
                # somebody who has turned debug on.
                _LOGGER.debug(
                    "A PRONOTE call exceeded %.0fs and was abandoned. The worker "
                    "thread is still finishing it, so the next call queues "
                    "behind it rather than running beside it -- expect a stall, "
                    "not a broken session",
                    self.deadline,
                )
                raise

    def shutdown(self) -> None:
        """Stop accepting work and never join from the loop."""
        self._closed = True
        self._executor.shutdown(wait=False, cancel_futures=True)


class SessionManager:
    """Owns the client, decides when to log in, and measures how long it lives.

    Every login goes through the limiter, which is only possible because the
    hardened client of §3.6 refuses to reconnect on its own. Without it,
    ``ClientBase.post`` would silently re-handshake on any protocol error and
    ``Erreur.G = 10`` would never be observable -- so there would be nothing to
    measure and no defensible strategy to pick.
    """

    def __init__(
        self,
        *,
        entry_id: str,
        credentials: SessionCredentials,
        limiter: RateLimiter,
        executor: SerialExecutor,
        strategy: SessionStrategy,
        now: Callable[[], datetime],
        clock: Callable[[], float],
        connect_timeout: float,
        read_timeout: float,
        on_credentials_rotated: Callable[[Mapping[str, Any]], Awaitable[None]]
        | None = None,
        on_children_announced: Callable[[tuple[str, ...]], None] | None = None,
    ) -> None:
        self._entry_id = entry_id
        self._credentials = credentials
        self._limiter = limiter
        self._executor = executor
        self._strategy = strategy
        self._now = now
        self._clock = clock
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout
        self._on_rotated = on_credentials_rotated
        self._on_children_announced = on_children_announced
        #: Maps an identifier a caller still holds to the one this client
        #: announces for the same child, or ``None``. Set by the account, which
        #: owns the child keys; see :meth:`_bind`.
        self.resolve_child: (
            Callable[[str, Sequence[tuple[str, str]]], str | None] | None
        ) = None

        self._client: HardenedClient | None = None
        #: The account's shape as the last login announced it, kept after the
        #: session closes: an entity asks it at set-up and between batches,
        #: when no client may be held. ``None`` until a login succeeded.
        self._parent_account: bool | None = None
        self._opened_at: float | None = None
        self._last_success: float | None = None

        self._lifetime = SessionLifetime()
        self._consecutive_expiries = 0
        self._short_timeout_confirmed = False
        self._last_probe: float | None = None

        # Which batch this session was opened for. `PER_BATCH` means one login
        # per *batch*, and getting this wrong was expensive: comparing the
        # strategy alone made `_reconnect_required()` true on every `run()`, and
        # `run()` is called once per `(child, tier)` -- so one tick of a
        # two-child account with three closed periods asked for 36 full logins
        # against a cap of 24, and the integration died before its first batch
        # finished.
        self._batch_id = 0
        self._session_batch_id: int | None = None

        # Held across "pick a client, wait, call, retry" -- see the module
        # docstring.
        self._gate = asyncio.Lock()

        # Silver ``log-when-unavailable``: one INFO when PRONOTE stops
        # answering, one INFO when it answers again. The flag is on the
        # session, not on a coordinator, because there is one coordinator per
        # tier and a single outage would otherwise print ten lines.
        self._unavailable_logged = False

    # -- observable state --------------------------------------------------

    @property
    def lifetime(self) -> SessionLifetime:
        """What has actually been measured (§6.5)."""
        return self._lifetime

    @property
    def strategy(self) -> SessionStrategy:
        """The configured strategy."""
        return self._strategy

    @property
    def effective_strategy(self) -> SessionStrategy:
        """What the manager is currently *doing*, which can differ.

        A configured ``LAZY`` degrades to ``PER_BATCH`` once three consecutive
        expiries say the server's timeout is shorter than our fastest tier. That
        degradation is the dominance property made concrete, not a fallback: it
        is what bounds lazy's worst case to three wasted requests plus one a day
        from the probe.
        """
        if self._strategy is SessionStrategy.PER_BATCH:
            return SessionStrategy.PER_BATCH
        if self._short_timeout_confirmed and not self._probe_due():
            return SessionStrategy.PER_BATCH
        return SessionStrategy.LAZY

    @property
    def session_age(self) -> float | None:
        """Seconds since the current session was opened."""
        if self._opened_at is None:
            return None
        return self._clock() - self._opened_at

    @property
    def is_open(self) -> bool:
        """Whether a client is currently held."""
        return self._client is not None

    @property
    def parent_account(self) -> bool | None:
        """Whether the account logged in holds children, or ``None`` before any login.

        Read from the handshake, so it places no request. It outlives the
        client on purpose: a write that PRONOTE silently ignores from one shape
        of account has to be refused before a session is opened for it.
        """
        return self._parent_account

    @property
    def student_ids(self) -> tuple[str, ...]:
        """Identifiers of the children on the account, or the student's own."""
        if self._client is None:
            return ()
        if self._client.is_parent_account:
            return tuple(str(child.id) for child in self._client.children)
        return (str(self._client.info.id),)

    def diagnostics(self) -> dict[str, Any]:
        """Non-secret facts for the diagnostic entities and the download."""
        return {
            "strategy": str(self._strategy),
            "effective_strategy": str(self.effective_strategy),
            "open": self.is_open,
            "age_seconds": self.session_age,
            # Reported next to `open` on purpose: the two together are what
            # tell a live session from a session merely being held. `open` was
            # `True` and `age_seconds` was 25832 while every tier failed, and
            # the field that would have said so in one reading -- this one --
            # was the field that did not exist.
            "idle_seconds": self._inactivity_gap() if self.is_open else None,
            "measured_lifetime_minutes": self._lifetime.observed_minutes,
            "lifetime_samples": len(self._lifetime.samples),
            "consecutive_expiries": self._consecutive_expiries,
            "short_timeout_confirmed": self._short_timeout_confirmed,
        }

    # -- batches -----------------------------------------------------------

    def begin_batch(self) -> None:
        """Mark the start of one collection batch.

        ``PER_BATCH`` reconnects at the first call of a batch and not again
        until the next one. A call made outside any batch -- a service, an image
        entity -- reuses whatever session is held, because a lone call is not a
        batch and re-logging in for it would cost five requests to save one.
        """
        self._batch_id += 1

    # -- probing -----------------------------------------------------------

    def _probe_due(self) -> bool:
        """Whether the "short timeout" conclusion is due for a re-test."""
        if self._last_probe is None:
            return True
        return self._clock() - self._last_probe >= PROBE_INTERVAL_SECONDS

    # -- the atomic unit of work ------------------------------------------

    async def run(
        self,
        tier: str,
        priority: Priority,
        fn: Callable[[HardenedClient], Any],
        *,
        student_id: str | None = None,
        cost: int = 1,
    ) -> Any:
        """Run one ``(child, tier)`` unit of work against a live session.

        ``fn`` receives the client and must return a complete DTO. It performs
        the child selection, the tab call and the DTO construction as one
        indivisible step -- that is where the atomicity of §3.2 lives, and why
        the lock is held across all three rather than around the call alone.
        """
        async with self._gate:
            return await self._run_locked(
                tier, priority, fn, student_id=student_id, cost=cost
            )

    async def _run_locked(
        self,
        tier: str,
        priority: Priority,
        fn: Callable[[HardenedClient], Any],
        *,
        student_id: str | None,
        cost: int,
    ) -> Any:
        """The body of :meth:`run`, with ``_gate`` already held."""
        client = await self._ensure_client(eager=False)
        gap = self._inactivity_gap()

        try:
            result = await self._limiter.call(
                tier,
                priority,
                lambda: self._executor.run(self._bind(client, fn, student_id)),
                cost=cost,
            )
        except TierDeferred:
            raise
        except ChildNotFound as error:
            # A client-side fault. Charging it to the server's backoff would
            # hold a healthy account for an hour because we asked for a child
            # who has left the establishment.
            # error(), not exception(): the traceback points at pronotepy and
            # the message is the whole diagnosis.
            _LOGGER.error(  # noqa: TRY400
                "tier %s asked for child %s, who is not on this account any "
                "more. Reconfigure the integration to pick the children again",
                tier,
                student_id,
            )
            raise IntegrationFault(str(error)) from error
        except TimeoutError as error:
            # §3.2 requires an abandoned call to mark the account as failing;
            # it is a real failure even though nothing came back to classify.
            self._note_unreachable("a PRONOTE call exceeded its deadline")
            self._limiter.note_failure()
            raise IntegrationFault("a PRONOTE call exceeded its deadline") from error
        except OSError as error:
            self._note_unreachable(describe_failure(error))
            self._limiter.note_failure()
            raise
        except PronoteAPIError as error:
            return await self._handle_protocol_error(
                error, tier, priority, fn, student_id=student_id, cost=cost
            )

        self._last_success = self._clock()
        self._note_reachable()
        self._note_survival(gap)
        self._reconcile(tier, result, cost)
        return result

    def _bind(
        self,
        client: HardenedClient,
        fn: Callable[[HardenedClient], Any],
        student_id: str | None,
    ) -> Callable[[], Any]:
        """Bundle child selection and the call into one indivisible closure."""

        def work() -> Any:
            if student_id is not None and client.is_parent_account:
                client.set_child(self._live_child_id(client, student_id))
            return fn(client)

        return work

    def _live_child_id(self, client: HardenedClient, student_id: str) -> str:
        """The identifier this client announces for the child a caller named.

        PRONOTE renames a child at every login, and a login can happen inside
        any call -- the session expired, so this very call opened a new one.
        The caller still holds the identifier of the session before: the
        account learns the new roster only after its batch. Without this,
        every call after a re-login raised ``ChildNotFound`` -- the whole
        batch's collections, and on a live instance the first tick of a
        homework item, which Home Assistant reported as "no child with id".
        The account resolves it through the child's minted key; an identifier
        it cannot place is passed on unchanged, and ``set_child`` refuses it as
        before.
        """
        announced = [(child.id, child.name) for child in client.children]
        if self.resolve_child is None or any(
            child_id == student_id for child_id, _ in announced
        ):
            return student_id
        return self.resolve_child(student_id, announced) or student_id

    def _reconcile(self, tier: str, result: Any, charged: int) -> None:
        """Charge the difference when the gateway spent more than declared.

        This is what makes ``GatewayResult.calls`` load-bearing rather than
        decorative. Without it the limiter only ever saw the a-priori cost the
        caller declared, so an accidental lazy-property access placed real
        requests that the spacing, the bucket and the daily cap all missed --
        which is precisely the regression the whole gateway design exists to
        prevent.
        """
        actual = getattr(result, "calls", None)
        if isinstance(actual, int):
            self._limiter.reconcile(tier, charged=charged, actual=actual)

    def _inactivity_gap(self) -> float:
        """How long the session has been idle, before this call."""
        if self._last_success is None:
            return 0.0
        return self._clock() - self._last_success

    def _note_unreachable(self, reason: str) -> None:
        """Log once when PRONOTE stops answering.

        Silver ``log-when-unavailable``: INFO, and only the first time. A
        second failed call in the same outage must stay silent -- that is
        what used to fill the log when every coordinator printed its own
        error for the same unreachable server.

        ``reason`` is a fixed text or :func:`~.failures.describe_failure`,
        never ``str(error)``: a ``requests`` message quotes the establishment's
        host and the path it failed on, and this line is the one a user pastes
        into an issue.
        """
        if self._unavailable_logged:
            return
        _LOGGER.info("PRONOTE is unavailable: %s", reason)
        self._unavailable_logged = True

    def _note_reachable(self) -> None:
        """Log once when PRONOTE answers again after an outage."""
        if not self._unavailable_logged:
            return
        _LOGGER.info("PRONOTE is back online")
        self._unavailable_logged = False

    async def _handle_protocol_error(
        self,
        error: PronoteAPIError,
        tier: str,
        priority: Priority,
        fn: Callable[[HardenedClient], Any],
        *,
        student_id: str | None,
        cost: int,
    ) -> Any:
        """Decide what a protocol error means, and retry only when it helps."""
        code = protocol_error_code(error)

        # By type, not by code. `_Communication.post` raises `ExpiredObject`
        # for `Erreur.G = 22` on its own branch, *before* the branch that
        # attaches `pronote_error_code` (pronoteAPI.py:183 against :191), so
        # this error arrives with no code at all. Testing the code alone made
        # the whole "this is our bug, report it" arm below dead code, and a
        # G = 22 fell through to the generic backoff instead -- holding a
        # perfectly healthy server because *we* leaked an object across the
        # session boundary. Annexe B §4 is explicit that this one must never be
        # absorbed into the backoff.
        if isinstance(error, ExpiredObject):
            code = ERROR_EXPIRED_OBJECT

        if code in SESSION_EXPIRED_CODES:
            # The one class of error worth retrying, and the only reason the
            # lazy strategy is measurable at all.
            return await self._replay_on_a_fresh_session(
                tier, priority, fn, student_id=student_id, cost=cost
            )

        if code == ERROR_EXPIRED_OBJECT:
            # Not a server overload: a client-side design fault. The DTO
            # boundary of §3.1 excludes it by construction, so if it appears it
            # is a bug in this integration and must read as one -- never
            # absorbed into the backoff as if it were a network hiccup
            # (annexe B §4).
            _LOGGER.error(
                "Erreur.G = 22 (object from a previous session) on tier %s. This "
                "is a bug in this integration, not a server problem: a pronotepy "
                "object escaped the gateway. Please report it",
                tier,
            )
            raise error

        if code == ERROR_TOO_MANY_AUTHORIZATIONS:
            # Goes to the backoff and provokes no reconnection. Upstream's
            # ClientBase.post would answer a sanction on authorization requests
            # with an authorization request; the hardened client is what stops
            # that (annexe B §4).
            _LOGGER.warning(
                "PRONOTE refused with Erreur.G = 25 (too many authorization "
                "requests) on tier %s; backing off without reconnecting",
                tier,
            )
            self._limiter.note_failure()
            raise error

        self._limiter.note_failure()
        raise error

    async def _replay_on_a_fresh_session(
        self,
        tier: str,
        priority: Priority,
        fn: Callable[[HardenedClient], Any],
        *,
        student_id: str | None,
        cost: int,
    ) -> Any:
        """Open a new session and replay the call. **Once**, never in a loop.

        The bound is the load-bearing part, and it is what makes widening
        :data:`SESSION_EXPIRED_CODES` safe rather than reckless. Every code in
        that set is a claim about what the server meant, and a claim can be
        wrong -- on some establishment 8 may be answering something that a
        fresh login does not fix. Retried without a ceiling, a wrong claim is
        not a stale sensor: it is one login per tier per tick, so ten tiers
        against a cap of 24 exhaust the day's logins in three ticks and then
        keep hammering. That is the repeated-failed-login gesture annexe B §1
        names as the one sanction which cannot be worked around.

        So a second protocol refusal on a session opened moments ago is
        treated as what it is -- evidence that reconnecting is not the answer
        here -- and charged to the exponential backoff like any other refusal.
        The integration then degrades to slow and stale, which is recoverable,
        instead of fast and sanctioned, which is not.
        """
        self._note_expiry()
        await self._reopen()
        client = await self._ensure_client(eager=True)
        try:
            result = await self._limiter.call(
                tier,
                priority,
                lambda: self._executor.run(self._bind(client, fn, student_id)),
                cost=cost,
            )
        except PronoteAPIError:
            _LOGGER.warning(
                "tier %s was refused again on a session opened moments ago, so "
                "reconnecting is not the remedy here; backing off instead of "
                "logging in again",
                tier,
            )
            self._limiter.note_failure()
            raise
        self._last_success = self._clock()
        self._note_reachable()
        self._reconcile(tier, result, cost)
        return result

    # -- session lifecycle -------------------------------------------------

    async def _ensure_client(self, *, eager: bool) -> HardenedClient:
        """Return a usable client, logging in when the policy calls for it."""
        if self._client is not None and (eager or self._reconnect_required()):
            await self._reopen()

        if self._client is None:
            await self._login()

        client = self._client
        if client is None:  # pragma: no cover - _login raises instead
            message = "no PRONOTE session could be opened"
            raise LoginRefused(message)
        return client

    def _reconnect_required(self) -> bool:
        """Whether to log in again before using the held session.

        Under ``PER_BATCH`` this is true exactly once per batch. Comparing only
        the strategy made it true on every call, which is a different and far
        more expensive policy -- see ``_session_batch_id``.

        The presumption check comes first and applies under **every** strategy,
        because it is not a strategy: it is the expiry of a belief.
        """
        if self._session_is_presumed_dead():
            return True
        if self.effective_strategy is not SessionStrategy.PER_BATCH:
            return False
        return self._session_batch_id != self._batch_id

    def _session_is_presumed_dead(self) -> bool:
        """Whether the held session has gone too long without a success.

        :attr:`is_open` answers "am I holding a client", which is a fact about
        this process and says nothing about the server. On the night this was
        written it answered ``True`` for seven hours and ten minutes about a
        session PRONOTE had already discarded, and every tier failed against
        it -- so the fact was true and the belief it stood for was false.

        Keeping the two apart is the general form of the fix that
        :data:`SESSION_EXPIRED_CODES` only solves for one code. A protocol code
        we do not recognise, a server that stops answering without saying why,
        an establishment that invalidates sessions on its own schedule: all of
        them look the same from here, and all of them are caught by refusing to
        trust a session that has not worked in an hour.
        """
        if self._last_success is None:
            return False
        idle = self._clock() - self._last_success
        if idle < PRESUMED_DEAD_AFTER_SECONDS:
            return False
        _LOGGER.debug(
            "no PRONOTE call has succeeded for %.0fs; treating the held "
            "session as gone and opening a new one",
            idle,
        )
        return True

    async def _login(self) -> None:
        """Log in, through the limiter, classifying failure correctly."""
        credentials = self._credentials

        def build() -> HardenedClient:
            return build_client(
                login_mode=str(credentials.login_mode),
                pronote_url=credentials.pronote_url,
                username=credentials.username,
                password=credentials.password,
                uuid=credentials.uuid,
                account_pin=credentials.account_pin,
                client_identifier=credentials.client_identifier,
                device_name=credentials.device_name,
                ent=_resolve_ent(credentials),
                connect_timeout=self._connect_timeout,
                read_timeout=self._read_timeout,
            )

        try:
            client = await self._limiter.login(
                lambda: self._executor.run(build), cost=REQUESTS_PER_LOGIN
            )
        except LoginRefusedByLimiter as error:
            # Nothing was sent and no credential was tried, so this is not a
            # login attempt and must not be counted as one.
            raise LoginRefused(str(error.reason)) from error
        except MFAError as error:
            self._limiter.note_login(LoginOutcome.MFA_REQUIRED)
            raise MfaRequired(str(error)) from error
        except CryptoError as error:
            # A wrong password, or an expired QR payload. This is one of the two
            # shapes that count against the IP guard.
            self._limiter.note_login(LoginOutcome.BAD_CREDENTIALS)
            raise InvalidCredentials("challenge decryption failed") from error
        except BootstrapUnavailable as error:
            self._limiter.note_login(LoginOutcome.BOOTSTRAP)
            raise BootstrapFailed(str(error)) from error
        except PronoteAPIError:
            # A protocol-level refusal that is not one of the shapes above. It
            # is the server's answer, not a client fault, so it is re-raised
            # unchanged and `async_setup_entry` turns it into "not ready".
            self._limiter.note_login(LoginOutcome.TRANSPORT)
            raise
        except _DECODING_ERRORS as error:
            # The requests went out and the server answered; what came back is
            # not something the pinned pronotepy can read. Neither the
            # credentials' fault nor a transport failure, so it gets its own
            # outcome and its own hold rather than looping for ever at the
            # tier's cadence.
            # error(), not exception(): the type and message are the useful
            # part, and the traceback is a walk through pronotepy's resolver.
            _LOGGER.error(  # noqa: TRY400
                "The login response from this establishment could not be "
                "decoded (%s: %s). This usually means PRONOTE changed something "
                "the pinned pronotepy does not handle yet",
                type(error).__name__,
                error,
            )
            self._limiter.note_login(LoginOutcome.UNDECODABLE)
            raise AccountUnreadable(str(error)) from error
        except _TRANSPORT_ERRORS as error:
            # `requests` failures land here, and none of them is a
            # `PronoteAPIError`: `requests.Timeout` inherits from `OSError`.
            # Reaching this branch is what makes the backoff engage and the
            # bootstrap GET get counted.
            self._note_unreachable(describe_failure(error))
            self._limiter.note_login(LoginOutcome.TRANSPORT)
            raise

        if not client.logged_in:
            # The second shape: `_login` returned False because the
            # `Authentification` response carried no `cle` key.
            await self._release(client)
            self._limiter.note_login(LoginOutcome.BAD_CREDENTIALS)
            raise InvalidCredentials("authentication returned no session key")

        self._limiter.note_login(LoginOutcome.SUCCESS)
        self._client = client
        self._parent_account = client.is_parent_account
        self._opened_at = self._clock()
        self._last_success = self._opened_at
        self._note_reachable()
        self._session_batch_id = self._batch_id
        if self._short_timeout_confirmed and self._probe_due():
            self._last_probe = self._clock()

        await self._persist_rotated_credentials(client)
        self._announce_children()

    def _announce_children(self) -> None:
        """Say who the handshake just named, so the account can compare.

        Called on the login and nowhere else, because that is the only moment
        the roster changes and the only moment it is free: the children arrive
        inside the payload the login already paid for, so reading them places
        no request and the announcement cannot provoke one.

        Synchronous by design. It runs under the session lock, between the
        login and the call that triggered it, and an awaited callback there
        would let a second caller reach a half-open session.
        """
        if self._on_children_announced is None:
            return
        self._on_children_announced(self.student_ids)

    async def _persist_rotated_credentials(self, client: HardenedClient) -> None:
        """Re-persist the credentials the login may have rotated.

        In token mode PRONOTE returns a fresh ``jetonConnexionAppliMobile`` at
        every ``Authentification`` and pronotepy overwrites ``self.password``
        with it. This is also the reason the lazy strategy matters beyond call
        counting: at one login a day there is one window a day in which an
        abrupt shutdown between the server's rotation and the local write leaves
        a dead token; at 64 logins a day there were 64 (§6.5).

        The in-memory update happens whether or not a persistence callback was
        supplied, and a failed write is *loud*. Silently swallowing it left the
        server holding the new token, memory holding the new token, and storage
        holding a dead one -- with the only symptom appearing at the next
        restart, as a QR code that has to be scanned again.
        """
        exported = export_credentials(client)
        self._credentials.password = str(exported.get("password", ""))
        self._credentials.username = str(exported.get("username", ""))
        identifier = exported.get("client_identifier")
        self._credentials.client_identifier = str(identifier) if identifier else None

        if self._on_rotated is None:
            return
        try:
            await self._on_rotated(exported)
        except Exception:
            _LOGGER.exception(
                "Could not save the rotated PRONOTE credentials. The current "
                "session still works, but a restart before the next successful "
                "save may require re-enrolling this integration"
            )

    async def _reopen(self) -> None:
        """Drop the held client so the next call builds a fresh one.

        Never ``client.refresh()``. Upstream's refresh rebuilds the transport
        and re-logs in but does **not** restore the child selection: it rewrites
        ``parametres_utilisateur[...]["ressource"]`` with the parent's own
        resource and only ``__init__`` rebuilds ``_selected_child``. Building a
        new client re-derives the child list by construction (§3.6).
        """
        client, self._client = self._client, None
        self._opened_at = None
        self._session_batch_id = None
        if client is not None:
            await self._release(client)

    async def _release(self, client: HardenedClient) -> None:
        """Release a client from the worker thread, never from the loop.

        ``release_client`` takes a process-wide lock. Called directly from the
        event loop it blocked Home Assistant entirely for as long as *another*
        config entry's login held that lock -- minutes, on an unresponsive
        server. Everything that touches the upstream registry belongs on the
        worker.
        """
        try:
            await self._executor.run(lambda: release_client(client))
        except TimeoutError, RuntimeError:
            _LOGGER.debug("could not release the PRONOTE client cleanly")

    async def close(self) -> None:
        """Release the session and stop the worker."""
        try:
            await self._reopen()
        finally:
            self._executor.shutdown()

    # -- measurement -------------------------------------------------------

    def _note_survival(self, gap: float) -> None:
        """A call succeeded after ``gap`` seconds of inactivity.

        ``gap`` matters, and passing it in is the fix for a real defect. The
        conclusion "this server's timeout is short" used to be lifted by the
        *next successful call*, which under the degradation is always the call
        immediately after a fresh login -- so the degradation never survived one
        call and the strategy oscillated for ever, paying an extra request every
        batch. Lifting it now requires counter-evidence: a session that stayed
        usable across an idle gap at least as long as the shortest gap that
        previously killed one.
        """
        self._consecutive_expiries = 0
        if not self._short_timeout_confirmed:
            return

        measured = self._lifetime.observed_minutes
        threshold = measured * 60.0 if measured is not None else 0.0
        if threshold <= 0 or gap < threshold:
            return

        _LOGGER.info(
            "The PRONOTE session survived %.0fs of inactivity, longer than the "
            "%.0fs that previously expired one. Lifting the short-timeout "
            "conclusion and going back to keeping the session",
            gap,
            threshold,
        )
        self._short_timeout_confirmed = False
        self._last_probe = None

    def _note_expiry(self) -> None:
        """The server said ``Erreur.G = 10``: record how long the session lived.

        The sample is the gap between the last successful call and now, not the
        session's total age: what expires is *inactivity*.
        """
        if self._last_success is not None:
            gap = self._clock() - self._last_success
            if gap >= MIN_LIFETIME_SAMPLE_SECONDS:
                self._lifetime = self._lifetime.with_sample(gap, self._now())
                _LOGGER.debug("session expired after %.0fs of inactivity", gap)
            else:
                # Too short to be an inactivity timeout. Recorded as an expiry
                # -- it still cost a request and still argues for reconnecting
                # eagerly -- but not as a lifetime sample, because
                # `observed_minutes` is a minimum and this would pin it near
                # zero for the rest of the run.
                _LOGGER.debug(
                    "session refused after only %.0fs; counting the expiry but "
                    "not recording it as a lifetime measurement",
                    gap,
                )

        self._consecutive_expiries += 1
        if (
            self._consecutive_expiries >= SHORT_TIMEOUT_EVIDENCE
            and not self._short_timeout_confirmed
        ):
            _LOGGER.info(
                "The PRONOTE session expired %d times in a row. Concluding the "
                "establishment's inactivity timeout is shorter than the fastest "
                "tier, and reconnecting eagerly from now on. This is the lazy "
                "strategy degenerating into one login per batch, which is what "
                "bounds its worst case",
                self._consecutive_expiries,
            )
            self._short_timeout_confirmed = True
            self._last_probe = self._clock()


def _resolve_ent(credentials: SessionCredentials) -> Callable[..., Any] | None:
    """Look up the ENT provider function by name.

    Resolved lazily by name rather than stored, because the config entry holds
    JSON and ``pronotepy.ent`` exposes plain functions.
    """
    if credentials.login_mode is not LoginMode.ENT or not credentials.ent_provider:
        return None

    provider = resolve_ent_provider(credentials.ent_provider)
    if provider is None:
        _LOGGER.error("unknown ENT provider %r", credentials.ent_provider)
        return None
    return provider
