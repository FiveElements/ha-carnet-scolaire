"""Session ownership: the four protocol verdicts, and the measured strategy.

This module had no test file at all, which was the wrong gap to have. Two of
the behaviours below are corrections of defects that already happened once in
this codebase, and both are silent when they regress:

* ``Erreur.G = 22`` is recognised **by type**, because ``_Communication.post``
  raises ``ExpiredObject`` on a branch that never attaches
  ``pronote_error_code``. Testing the code alone made the "this is our bug"
  arm dead code and sent a G = 22 to the exponential backoff -- punishing a
  perfectly healthy server for a fault of ours.
* the "short timeout" conclusion is lifted only by *counter-evidence*, not by
  the next successful call -- which, under the degradation, is always the call
  straight after a fresh login. The loose version of that rule made the
  strategy oscillate for ever, paying one extra request every batch.

Nothing here needs Home Assistant: ``SessionManager`` takes its monotonic
clock, its wall clock, its executor and its limiter as constructor arguments,
and the one seam that reaches the network -- ``build_client`` -- is patched. So
this file runs on any platform, and it drives the real ``SerialExecutor``
rather than a stand-in, which is how the deadline behaviour becomes observable.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import logging
import random
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

from pronotepy import ChildNotFound
from pronotepy.exceptions import ExpiredObject, PronoteAPIError
import pytest
import requests

from custom_components.carnet_scolaire.const import (
    LimiterState,
    LoginMode,
    Priority,
    SessionStrategy,
)
from custom_components.carnet_scolaire.hardened_client import protocol_error_code
from custom_components.carnet_scolaire.ratelimit import (
    RateLimitConfig,
    RateLimiter,
)
from custom_components.carnet_scolaire.session import (
    ERROR_PAGE_EXPIRED,
    ERROR_SESSION_EXPIRED,
    ERROR_TOO_MANY_AUTHORIZATIONS,
    MIN_LIFETIME_SAMPLE_SECONDS,
    PRESUMED_DEAD_AFTER_SECONDS,
    SHORT_TIMEOUT_EVIDENCE,
    IntegrationFault,
    SerialExecutor,
    SessionCredentials,
    SessionManager,
)

from .clock import FakeClock, RecordingSleeper

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Sequence


# ---------------------------------------------------------------------------
# The doubles
# ---------------------------------------------------------------------------


class StubClient:
    """The little of a client this module actually touches.

    Deliberately not ``FakeClient`` from ``tests/fixtures``: that one models
    the *data* surface the gateway reads, and what matters here is login state,
    child selection and credential rotation. A double that answered both would
    hide which of the two a failure came from.
    """

    def __init__(
        self,
        *,
        logged_in: bool = True,
        # Suppressed rather than renamed: the point of this field is that it
        # *rotates*, and the value is a sentinel every assertion matches
        # literally, not anything resembling a credential.
        token: str = "rotating-sentinel-1",  # noqa: S107 -- a sentinel, not a secret
        is_parent: bool = False,
        children: Sequence[str] = (),
    ) -> None:
        self.logged_in = logged_in
        self.is_parent_account = is_parent or bool(children)
        self.selected: list[str] = []
        self.closed = False
        self._token = token
        # What the login itself put in memory. Reading it costs nothing: the
        # real client fills `children` from the payload of the handshake, so a
        # test that charged a request here would model the wrong thing.
        self.children = tuple(SimpleNamespace(id=child) for child in children)
        self.info = SimpleNamespace(id=children[0] if children else "SOLO-1")

    def set_child(self, student_id: str) -> None:
        """Record the selection, as the real client mutates its own state."""
        self.selected.append(student_id)

    def export_credentials(self) -> dict[str, Any]:
        """What a login may have rotated."""
        return {
            "password": self._token,
            "username": "parent-under-test",
            "client_identifier": "client-1",
        }


@dataclass(frozen=True, slots=True)
class StubResult:
    """A gateway result, which is a DTO carrying its own call count."""

    calls: int = 1


class ProtocolError(PronoteAPIError):
    """A protocol refusal carrying an ``Erreur.G`` code, as pronotepy's do."""

    def __init__(self, code: int) -> None:
        super().__init__(f"Erreur.G = {code}")
        self.pronote_error_code = code


# ---------------------------------------------------------------------------
# The harness
# ---------------------------------------------------------------------------


def _credentials() -> SessionCredentials:
    """Credentials that are manifestly fictitious, and never a real account."""
    return SessionCredentials(
        login_mode=LoginMode.CREDENTIALS,
        pronote_url="https://demo.example.invalid/pronote/parent.html",
        username="parent-under-test",
        password="not-a-real-password",
    )


@dataclass(slots=True)
class Harness:
    """Everything a test needs to drive one session manager."""

    manager: SessionManager
    limiter: RateLimiter
    clock: FakeClock
    executor: SerialExecutor
    clients: list[StubClient]
    builds: list[int]
    #: Mutable on purpose: a test changes it between two logins to model a
    #: child enrolled on the account while Home Assistant was running.
    roster: list[str]
    #: One entry per login, in order.
    announced: list[tuple[str, ...]]

    def logins(self) -> int:
        """How many times a client was actually built."""
        return len(self.builds)


@pytest.fixture(name="harness")
async def harness_fixture(
    request: pytest.FixtureRequest,
) -> AsyncIterator[Harness]:
    """A session manager whose every seam is simulated.

    The limiter is real, with the spacing floor at its own minimum so tests do
    not have to sleep, and the sleeper advances the simulated clock. The one
    patched function is ``build_client``: it is the only thing in this module
    that would reach a school.
    """
    strategy = getattr(request, "param", SessionStrategy.LAZY)
    clock = FakeClock(datetime(2026, 3, 12, 8, 0, tzinfo=UTC))
    limiter = RateLimiter(
        RateLimitConfig(min_request_interval=0.2, max_requests_per_hour=2000),
        clock=clock.monotonic,
        now=clock.now,
        rng=random.Random(1),  # noqa: S311 -- jitter, not cryptography
        sleep=RecordingSleeper(clock),
    )
    executor = SerialExecutor("entry-under-test", read_timeout=30.0)

    clients: list[StubClient] = []
    builds: list[int] = []

    roster: list[str] = []
    announced: list[tuple[str, ...]] = []

    def build(**_kwargs: Any) -> StubClient:
        builds.append(1)
        client = StubClient(children=tuple(roster))
        clients.append(client)
        return client

    manager = SessionManager(
        entry_id="entry-under-test",
        credentials=_credentials(),
        limiter=limiter,
        executor=executor,
        strategy=strategy,
        now=clock.now,
        clock=clock.monotonic,
        connect_timeout=10.0,
        read_timeout=30.0,
        on_children_announced=announced.append,
    )

    with (
        patch(
            "custom_components.carnet_scolaire.session.build_client", side_effect=build
        ),
        patch("custom_components.carnet_scolaire.session.release_client"),
    ):
        yield Harness(
            manager=manager,
            limiter=limiter,
            clock=clock,
            executor=executor,
            clients=clients,
            builds=builds,
            roster=roster,
            announced=announced,
        )
        await manager.close()


def _work(*, result: Any = None, raises: BaseException | None = None) -> Callable:
    """A unit of work that either returns a DTO or raises, once per call."""

    def fn(_client: Any) -> Any:
        if raises is not None:
            raise raises
        return result if result is not None else StubResult()

    return fn


# ---------------------------------------------------------------------------
# The happy path, which every other test depends on
# ---------------------------------------------------------------------------


async def test_the_first_call_opens_a_session_and_the_second_reuses_it(
    harness: Harness,
) -> None:
    """The lazy strategy in one assertion: one login for two calls.

    Worth stating plainly, because it is the whole benefit being claimed. The
    per-batch strategy of specification v1 would show two.
    """
    await harness.manager.run("timetable", Priority.HIGH, _work())
    await harness.manager.run("homework", Priority.NORMAL, _work())

    assert harness.logins() == 1
    assert harness.manager.is_open


@pytest.mark.parametrize("harness", [SessionStrategy.PER_BATCH], indirect=True)
async def test_a_login_announces_the_children_it_just_learned(
    harness: Harness,
) -> None:
    """The session is the only object that knows a login just happened.

    ``student_ids`` is a property read on demand, and the account reads it
    exactly once, from ``_async_load_session_facts``. Nothing tells it that a
    later login refreshed the roster, so a pupil enrolled on the parent
    account while Home Assistant was running stays invisible until the entry
    is reloaded -- which is what the ``dynamic-devices`` rule forbids.

    Announcing costs no request: the roster arrives inside the handshake the
    login already paid for. That is the whole reason this seam sits on the
    login rather than on a tick of its own.
    """
    harness.roster[:] = ["STUDENT-1"]
    await harness.manager.run("timetable", Priority.HIGH, _work())

    assert harness.announced == [("STUDENT-1",)]

    # A second child, learned by the next login and by nothing else. The
    # per-batch strategy is only the cheapest way to make a second login
    # happen here; the announcement belongs to the login, not to the strategy.
    harness.roster[:] = ["STUDENT-1", "STUDENT-2"]
    harness.manager.begin_batch()
    await harness.manager.run("homework", Priority.NORMAL, _work())

    assert harness.announced == [("STUDENT-1",), ("STUDENT-1", "STUDENT-2")]
    assert harness.logins() == 2


async def test_a_child_is_selected_inside_the_same_locked_unit_as_the_call(
    harness: Harness,
) -> None:
    """The atomic unit is ``(child, tier)``, never the tier alone.

    ``set_child`` mutates the slot the tab call reads to build its request
    body, so a selection made outside the lock lets a parent account publish
    one child's timetable under the other child's entities -- silently, with
    plausible data.
    """
    harness.manager  # noqa: B018 -- see the comment below
    await harness.manager.run("timetable", Priority.HIGH, _work())
    # The stub reports itself as a single-child account, so nothing is
    # selected; the parent case is asserted next.
    assert harness.clients[0].selected == []

    harness.clients[0].is_parent_account = True
    await harness.manager.run(
        "timetable", Priority.HIGH, _work(), student_id="STUDENT-2"
    )
    assert harness.clients[0].selected == ["STUDENT-2"]


async def test_a_result_that_cost_more_than_declared_is_reconciled(
    harness: Harness,
) -> None:
    """What makes ``GatewayResult.calls`` load-bearing instead of decorative.

    Without this the limiter only ever saw the a-priori cost the caller
    declared, so an accidental lazy-property access placed real requests that
    the spacing, the bucket and the daily cap all missed -- and the truth
    appeared only in a diagnostics field nobody compares.
    """
    # The session is opened first, so what is measured is the call and not
    # the five requests of the login that precedes it.
    await harness.manager.run("timetable", Priority.HIGH, _work())
    before = harness.limiter.calls_today

    await harness.manager.run(
        "homework", Priority.NORMAL, _work(result=StubResult(calls=7)), cost=2
    )
    assert harness.limiter.calls_today - before == 7, (
        "two were declared and seven were spent; the difference must be charged"
    )


# ---------------------------------------------------------------------------
# The four protocol verdicts
# ---------------------------------------------------------------------------


async def test_an_expired_session_is_reopened_and_the_call_replayed(
    harness: Harness,
) -> None:
    """``Erreur.G = 10``: the one error worth retrying.

    It is also the only reason the lazy strategy is measurable at all -- the
    hardened client refuses to re-handshake on its own precisely so that this
    arrives here instead of being absorbed upstream.
    """
    attempts: list[int] = []

    def fn(_client: Any) -> Any:
        attempts.append(1)
        if len(attempts) == 1:
            raise ProtocolError(ERROR_SESSION_EXPIRED)
        return StubResult()

    result = await harness.manager.run("timetable", Priority.HIGH, fn)

    assert isinstance(result, StubResult)
    assert len(attempts) == 2, "the call is replayed, once"
    assert harness.logins() == 2, "on a fresh session"


async def test_a_page_that_expired_is_the_same_verdict_as_a_session_that_expired(
    harness: Harness,
) -> None:
    """``Erreur.G = 8`` -- "La page a expiré !" -- must reopen, not back off.

    This is the defect that froze the live instance for seven hours and ten
    minutes. The establishment answers 8 where the specification documents 10,
    ``pronotepy`` has no message for 8 and renders it as ``Unknown error from
    pronote: 8``, and the handler classified by a single code -- so the arm
    that reopens the session was never reached. The session stayed dead,
    ``is_open`` kept answering ``True``, every attempt failed and each failure
    deepened the backoff, with no possible success to reset it. Nothing but a
    restart got the instance out, which is why this is asserted by code and
    not left to the message.
    """
    attempts: list[int] = []

    def fn(_client: Any) -> Any:
        attempts.append(1)
        if len(attempts) == 1:
            raise ProtocolError(ERROR_PAGE_EXPIRED)
        return StubResult()

    result = await harness.manager.run("timetable", Priority.HIGH, fn)

    assert isinstance(result, StubResult)
    assert len(attempts) == 2, "the call is replayed"
    assert harness.logins() == 2, "on a session that was actually reopened"
    assert harness.limiter.state is not LimiterState.BACKOFF, (
        "an expiry that was handled must leave no backoff behind"
    )


async def test_a_refusal_that_survives_a_fresh_login_backs_off_instead_of_retrying(
    harness: Harness,
) -> None:
    """The ceiling that makes widening the expiry set safe rather than reckless.

    Every code in ``SESSION_EXPIRED_CODES`` is a claim about what the server
    meant, and a claim can be wrong for some establishment. Retried without a
    bound, a wrong claim costs one login per tier per tick -- ten tiers against
    a cap of twenty-four exhaust the day in three ticks and then keep going,
    which is exactly the repeated-failed-login gesture annexe B §1 says cannot
    be worked around. So the second refusal is charged to the backoff, and the
    integration degrades to stale rather than to sanctioned.
    """
    with pytest.raises(PronoteAPIError):
        await harness.manager.run(
            "timetable",
            Priority.HIGH,
            _work(raises=ProtocolError(ERROR_PAGE_EXPIRED)),
        )

    assert harness.logins() == 2, "one reconnection, and not a third login"
    assert harness.limiter.state is LimiterState.BACKOFF, (
        "the refusal that reconnecting did not fix must reach the backoff"
    )


async def test_a_session_that_has_not_worked_for_an_hour_is_not_reused(
    harness: Harness,
) -> None:
    """The general form of the fix, for the next code we do not recognise.

    ``is_open`` answers "am I holding a client", which is a fact about this
    process and says nothing about the server: it answered ``True`` for seven
    hours about a session PRONOTE had already discarded. A protocol code we
    cannot classify, a server that stops answering without saying why, an
    establishment that invalidates sessions on its own schedule -- all of them
    look identical from here, and all of them are caught by refusing to trust
    a session that has not worked in an hour.
    """
    await harness.manager.run("timetable", Priority.HIGH, _work())
    assert harness.logins() == 1

    harness.clock.advance(PRESUMED_DEAD_AFTER_SECONDS + 1)
    await harness.manager.run("homework", Priority.NORMAL, _work())

    assert harness.logins() == 2, (
        "the held session had not worked for an hour and must not be trusted"
    )


async def test_a_session_that_worked_recently_is_still_reused(
    harness: Harness,
) -> None:
    """The guard on the guard: the ceiling must not become a per-call login.

    Without this the previous test passes just as well against a manager that
    reconnects on every call, which is the policy the lazy strategy exists to
    avoid and which costs five requests each time. One second below the
    threshold is the interesting side of the boundary.
    """
    await harness.manager.run("timetable", Priority.HIGH, _work())

    harness.clock.advance(PRESUMED_DEAD_AFTER_SECONDS - 1)
    await harness.manager.run("homework", Priority.NORMAL, _work())

    assert harness.logins() == 1, "a session that worked recently is kept"


async def test_an_object_from_a_previous_session_is_reported_as_our_own_bug(
    harness: Harness,
) -> None:
    """``ExpiredObject`` arrives with **no** code, which is the whole trap.

    ``_Communication.post`` raises it on its own branch, before the branch that
    attaches ``pronote_error_code``. Classifying by code alone therefore made
    the G = 22 arm unreachable and this error fell through to the generic
    backoff -- holding a healthy server for an hour because *we* leaked a
    pronotepy object across the DTO boundary. Annexe B §4 is explicit that this
    one must never be absorbed into the backoff.
    """
    leaked = ExpiredObject("object from a previous session")
    # The premise, asserted rather than assumed: `PronoteAPIError.__init__`
    # declares `pronote_error_code` and defaults it to None, and the branch of
    # `_Communication.post` that raises `ExpiredObject` passes no code. So the
    # attribute is present and empty -- which is precisely why classifying by
    # code alone silently skipped the arm below. If a future pronotepy starts
    # attaching 22 here, this assertion is what tells us the `isinstance`
    # special case can go.
    assert protocol_error_code(leaked) is None
    failures_before = harness.limiter.snapshot_counters()["consecutive_failures"]

    with pytest.raises(ExpiredObject):
        await harness.manager.run("timetable", Priority.HIGH, _work(raises=leaked))

    assert harness.limiter.snapshot_counters()["consecutive_failures"] == (
        failures_before
    ), "our bug must not charge the server's backoff"
    assert harness.logins() == 1, "and must not provoke a reconnection"


async def test_a_sanction_on_authorizations_backs_off_without_reconnecting(
    harness: Harness,
) -> None:
    """``Erreur.G = 25``: a refusal, answered by waiting rather than by asking.

    Upstream's ``ClientBase.post`` would answer a sanction on authorization
    requests with another authorization request. The hardened client is what
    stops that, and this is where the decision is taken.
    """
    with pytest.raises(PronoteAPIError):
        await harness.manager.run(
            "timetable",
            Priority.HIGH,
            _work(raises=ProtocolError(ERROR_TOO_MANY_AUTHORIZATIONS)),
        )

    assert harness.limiter.snapshot_counters()["consecutive_failures"] >= 1
    assert harness.logins() == 1, "no reconnection"


async def test_any_other_protocol_error_goes_to_the_backoff(
    harness: Harness,
) -> None:
    """The default arm, which is the one most errors actually take."""
    with pytest.raises(PronoteAPIError):
        await harness.manager.run(
            "timetable", Priority.HIGH, _work(raises=ProtocolError(7))
        )

    assert harness.limiter.snapshot_counters()["consecutive_failures"] >= 1


async def test_a_child_who_left_the_establishment_does_not_hold_the_account(
    harness: Harness,
) -> None:
    """A client-side fault, so it must not charge the server's backoff.

    Otherwise asking for a child who has moved schools holds a healthy account
    for an hour, and the repair -- reconfiguring the children -- is a login the
    hold then refuses.
    """
    failures_before = harness.limiter.snapshot_counters()["consecutive_failures"]

    with pytest.raises(IntegrationFault):
        await harness.manager.run(
            "timetable",
            Priority.HIGH,
            _work(raises=ChildNotFound("no such child")),
            student_id="STUDENT-9",
        )

    assert harness.limiter.snapshot_counters()["consecutive_failures"] == (
        failures_before
    )


async def test_a_call_that_never_returns_marks_the_account_as_failing(
    harness: Harness,
) -> None:
    """§3.2: an abandoned call is a real failure, even with nothing to classify.

    This is the case the staleness regime of §5.4 would otherwise disguise
    completely -- entities quietly ageing, and nothing in the log.
    """
    failures_before = harness.limiter.snapshot_counters()["consecutive_failures"]

    with pytest.raises(IntegrationFault):
        await harness.manager.run(
            "timetable", Priority.HIGH, _work(raises=TimeoutError())
        )

    assert harness.limiter.snapshot_counters()["consecutive_failures"] > (
        failures_before
    )


async def test_an_unreachable_server_is_logged_once_and_again_when_it_returns(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    """Silver ``log-when-unavailable``: one INFO down, one INFO up, nothing else.

    The coordinator cannot do this job. There is one coordinator per tier, and
    a tick that cannot reach PRONOTE would otherwise print ten error lines for
    one outage -- then stay silent on recovery, because these coordinators
    never run ``_async_refresh``. The session is the one place that sees both
    the transport failure and the next success, for the account as a whole.
    """
    caplog.set_level(logging.INFO, logger="custom_components.carnet_scolaire.session")

    with pytest.raises(IntegrationFault):
        await harness.manager.run(
            "timetable", Priority.HIGH, _work(raises=TimeoutError())
        )
    down = [
        record
        for record in caplog.records
        if record.levelno == logging.INFO and "unavailable" in record.getMessage()
    ]
    assert len(down) == 1

    # The first timeout opened a backoff hold. Advance past it so the next
    # call is another real attempt, not a silent ``TierDeferred``.
    harness.clock.advance(120)
    caplog.clear()
    with pytest.raises(IntegrationFault):
        await harness.manager.run(
            "homework", Priority.NORMAL, _work(raises=TimeoutError())
        )
    assert not [
        record
        for record in caplog.records
        if record.levelno == logging.INFO and "unavailable" in record.getMessage()
    ]

    harness.clock.advance(120)
    caplog.clear()
    await harness.manager.run("timetable", Priority.HIGH, _work())
    up = [
        record
        for record in caplog.records
        if record.levelno == logging.INFO and "back online" in record.getMessage()
    ]
    assert len(up) == 1

    caplog.clear()
    await harness.manager.run("homework", Priority.NORMAL, _work())
    assert not [
        record
        for record in caplog.records
        if record.levelno == logging.INFO and "back online" in record.getMessage()
    ]


# ---------------------------------------------------------------------------
# Measuring the session's lifetime
# ---------------------------------------------------------------------------


async def test_an_expiry_records_the_inactivity_gap_and_not_the_session_age(
    harness: Harness,
) -> None:
    """What expires is inactivity, so that is what is measured.

    Recording the session's total age instead would over-state the lifetime for
    any account whose tiers are staggered -- which is all of them.
    """
    await harness.manager.run("timetable", Priority.HIGH, _work())
    harness.clock.advance(1800)  # Thirty idle minutes.

    attempts: list[int] = []

    def fn(_client: Any) -> Any:
        attempts.append(1)
        if len(attempts) == 1:
            raise ProtocolError(ERROR_SESSION_EXPIRED)
        return StubResult()

    await harness.manager.run("homework", Priority.NORMAL, fn)

    observed = harness.manager.lifetime.observed_minutes
    assert observed is not None
    assert 29 <= observed <= 31


async def test_an_expiry_too_soon_to_be_a_timeout_is_not_a_lifetime_sample(
    harness: Harness,
) -> None:
    """A server restart is not a measurement.

    ``observed_minutes`` is a *minimum*, so one spurious near-zero sample would
    pin it near zero for the rest of the run and falsify both the diagnostic
    sensor and the options-page estimate. The expiry is still counted -- it
    cost a request and it still argues for reconnecting eagerly.
    """
    await harness.manager.run("timetable", Priority.HIGH, _work())
    harness.clock.advance(MIN_LIFETIME_SAMPLE_SECONDS / 2)

    attempts: list[int] = []

    def fn(_client: Any) -> Any:
        attempts.append(1)
        if len(attempts) == 1:
            raise ProtocolError(ERROR_SESSION_EXPIRED)
        return StubResult()

    await harness.manager.run("homework", Priority.NORMAL, fn)

    assert harness.manager.lifetime.observed_minutes is None


async def test_three_expiries_in_a_row_degrade_the_lazy_strategy(
    harness: Harness,
) -> None:
    """The bound that makes the lazy strategy defensible in its bad case.

    Lazy is not free when the establishment's inactivity timeout is shorter
    than the fastest tier: every batch then wastes one request discovering the
    session is dead. What bounds that is exactly this degradation -- three
    wasted requests, once, and then eager reconnection.
    """
    assert harness.manager.effective_strategy is SessionStrategy.LAZY

    for tick in range(SHORT_TIMEOUT_EVIDENCE):
        harness.clock.advance(600)
        attempts: list[int] = []

        def fn(_client: Any, attempts: list[int] = attempts) -> Any:
            attempts.append(1)
            if len(attempts) == 1:
                raise ProtocolError(ERROR_SESSION_EXPIRED)
            return StubResult()

        await harness.manager.run(f"tier-{tick}", Priority.NORMAL, fn)

    assert harness.manager.effective_strategy is SessionStrategy.PER_BATCH


async def test_a_successful_call_alone_does_not_lift_the_conclusion(
    harness: Harness,
) -> None:
    """The defect this rule was written to fix, asserted directly.

    Under the degradation the next call is always the one straight after a
    fresh login, and it always succeeds. Treating that as counter-evidence made
    the conclusion survive exactly one call, so the strategy oscillated for
    ever and paid an extra request every batch.
    """
    for tick in range(SHORT_TIMEOUT_EVIDENCE):
        harness.clock.advance(600)
        attempts: list[int] = []

        def fn(_client: Any, attempts: list[int] = attempts) -> Any:
            attempts.append(1)
            if len(attempts) == 1:
                raise ProtocolError(ERROR_SESSION_EXPIRED)
            return StubResult()

        await harness.manager.run(f"tier-{tick}", Priority.NORMAL, fn)

    assert harness.manager.effective_strategy is SessionStrategy.PER_BATCH

    # A perfectly ordinary success, straight away.
    await harness.manager.run("timetable", Priority.HIGH, _work())
    assert harness.manager.effective_strategy is SessionStrategy.PER_BATCH


async def test_surviving_a_longer_gap_than_ever_killed_one_lifts_it(
    harness: Harness,
) -> None:
    """Counter-evidence, which is a specific thing and not merely a success.

    A session that stayed usable across an idle gap at least as long as the
    shortest gap that previously expired one is evidence the earlier reading
    was wrong. Nothing weaker is.
    """
    for tick in range(SHORT_TIMEOUT_EVIDENCE):
        harness.clock.advance(600)
        attempts: list[int] = []

        def fn(_client: Any, attempts: list[int] = attempts) -> Any:
            attempts.append(1)
            if len(attempts) == 1:
                raise ProtocolError(ERROR_SESSION_EXPIRED)
            return StubResult()

        await harness.manager.run(f"tier-{tick}", Priority.NORMAL, fn)

    assert harness.manager.effective_strategy is SessionStrategy.PER_BATCH
    measured = harness.manager.lifetime.observed_minutes
    assert measured is not None

    harness.clock.advance(measured * 60.0 + 60.0)
    await harness.manager.run("timetable", Priority.HIGH, _work())

    assert harness.manager.effective_strategy is SessionStrategy.LAZY


# ---------------------------------------------------------------------------
# The per-batch strategy, and the mistake that made it ruinous
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("harness", [SessionStrategy.PER_BATCH], indirect=True)
async def test_per_batch_means_one_login_per_batch_and_not_one_per_call(
    harness: Harness,
) -> None:
    """The distinction cost 36 logins against a cap of 24.

    ``run()`` is called once per ``(child, tier)``. Comparing the strategy
    alone made ``_reconnect_required()`` true on every one of them, so a single
    tick of a two-child account with three closed periods asked for 36 full
    logins and the integration died before its first batch finished.
    """
    harness.manager.begin_batch()
    for tier in ("timetable", "homework", "marks", "news"):
        await harness.manager.run(tier, Priority.NORMAL, _work())

    assert harness.logins() == 1, "one batch, one login"

    harness.manager.begin_batch()
    await harness.manager.run("timetable", Priority.HIGH, _work())
    assert harness.logins() == 2, "the next batch, the next login"


@pytest.mark.parametrize("harness", [SessionStrategy.PER_BATCH], indirect=True)
async def test_a_lone_call_outside_any_batch_reuses_the_held_session(
    harness: Harness,
) -> None:
    """A service call or an image render is not a batch.

    Re-logging in for one of them would spend five requests to save one.
    """
    harness.manager.begin_batch()
    await harness.manager.run("timetable", Priority.HIGH, _work())
    await harness.manager.run("static", Priority.LOW, _work())

    assert harness.logins() == 1


# ---------------------------------------------------------------------------
# Credential rotation
# ---------------------------------------------------------------------------


async def test_a_rotated_token_is_persisted_after_every_login(
    harness: Harness,
) -> None:
    """In token mode PRONOTE hands back a fresh one at every login.

    This is also why the lazy strategy matters beyond call counting: at one
    login a day there is one window a day in which an abrupt shutdown between
    the server's rotation and the local write leaves a dead token. At 64 logins
    a day there were 64.
    """
    saved: list[dict[str, Any]] = []

    async def on_rotated(credentials: Any) -> None:
        saved.append(dict(credentials))

    harness.manager._on_rotated = on_rotated
    await harness.manager.run("timetable", Priority.HIGH, _work())

    assert saved, "the login did not persist anything"
    assert saved[0]["password"] == "rotating-sentinel-1"


async def test_a_failed_credential_write_is_loud_but_not_fatal(
    harness: Harness,
) -> None:
    """Swallowing it silently was the defect.

    That left the server holding the new token, memory holding the new token
    and storage holding a dead one -- with the only symptom appearing at the
    next restart, as a QR code that has to be scanned again.
    """

    async def on_rotated(_credentials: Any) -> None:
        message = "the store is unwritable"
        raise OSError(message)

    harness.manager._on_rotated = on_rotated

    # The session still works, which is the half that must not regress.
    result = await harness.manager.run("timetable", Priority.HIGH, _work())
    assert isinstance(result, StubResult)


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------


async def test_closing_releases_the_session_and_refuses_further_work(
    harness: Harness,
) -> None:
    """A leaked worker thread beats a frozen Home Assistant.

    ``shutdown(wait=True)`` is never called from the event loop: a thread can
    be mid-request at unload time, and joining it would freeze the reload.
    """
    await harness.manager.run("timetable", Priority.HIGH, _work())
    assert harness.manager.is_open

    await harness.manager.close()
    assert not harness.manager.is_open

    with pytest.raises(RuntimeError):
        await harness.executor.run(lambda: None)


async def test_the_diagnostics_report_the_strategy_and_never_a_credential(
    harness: Harness,
) -> None:
    """This mapping ends up in a file people attach to public issues."""
    await harness.manager.run("timetable", Priority.HIGH, _work())

    diagnostics = harness.manager.diagnostics()
    payload = repr(diagnostics)

    assert "not-a-real-password" not in payload
    assert "rotating-sentinel-1" not in payload
    assert diagnostics


async def test_an_os_error_on_a_call_marks_the_server_unreachable_and_re_raises(
    harness: Harness,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``requests`` failures are ``OSError``, not ``PronoteAPIError``.

    ``requests.Timeout`` inherits from ``OSError``, so a transport failure
    during a *call* -- not during the login -- arrives here and nowhere else.
    Two things have to happen and neither is optional: the backoff has to
    engage, or the next tick retries against a server that is down at the
    tier's cadence; and the exception has to keep travelling, or the tier
    records a success it did not have.
    """
    caplog.set_level(logging.INFO, logger="custom_components.carnet_scolaire.session")

    with pytest.raises(OSError, match="network is down"):
        await harness.manager.run(
            "timetable",
            Priority.HIGH,
            _work(raises=OSError("network is down")),
        )

    assert "PRONOTE is unavailable: OSError (os error)" in caplog.text
    assert "network is down" not in caplog.text
    # And the failure is recorded, so the next attempt is held off rather than
    # made at the tier's cadence against a server that is down.
    assert harness.limiter.retry_delay() > 0


async def test_a_client_that_will_not_release_cleanly_does_not_break_the_unload(
    harness: Harness,
) -> None:
    """Teardown must not be able to fail.

    Releasing goes through the single worker, which may already be shutting
    down or wedged on a socket. Letting that raise would leave
    ``async_unload_entry`` reporting failure for an entry that is, as far as
    Home Assistant is concerned, gone -- and the user cannot retry a reload
    that already removed the account.
    """
    await harness.manager.run("timetable", Priority.HIGH, _work())
    assert harness.manager.is_open

    with patch(
        "custom_components.carnet_scolaire.session.release_client",
        side_effect=RuntimeError("the worker is gone"),
    ):
        await harness.manager.close()

    assert not harness.manager.is_open


def test_a_non_ent_login_resolves_no_provider() -> None:
    """Credentials and QR code modes must not reach into ``pronotepy.ent``."""
    from custom_components.carnet_scolaire.session import _resolve_ent

    assert _resolve_ent(_credentials()) is None


def test_an_ent_provider_nobody_recognises_is_refused_rather_than_guessed() -> None:
    """A stale provider name must not become a login attempt.

    ENT provider functions are named after the regional portals and upstream
    renames them between releases. Passing ``None`` on to the client would
    make ``pronotepy`` log in without the federation step, which the portal
    answers as bad credentials -- and two of those count against the IP guard,
    whose sanction is the expensive one.
    """
    from custom_components.carnet_scolaire.session import _resolve_ent

    credentials = SessionCredentials(
        login_mode=LoginMode.ENT,
        pronote_url="https://demo.example.invalid/pronote/parent.html",
        username="parent-under-test",
        password="not-a-real-password",
        ent_provider="a_portal_that_upstream_has_renamed",
    )

    assert _resolve_ent(credentials) is None


def test_an_ent_provider_upstream_still_ships_is_resolved_to_its_callable() -> None:
    """The counterpart, so the refusal above cannot be "always refuses".

    The provider is looked up by name rather than imported, so this asserts
    against whatever the pinned ``pronotepy`` actually exposes instead of
    hard-coding a regional portal that a version bump may rename.
    """
    from pronotepy import ent as ent_module

    from custom_components.carnet_scolaire.session import _resolve_ent

    name = next(
        attribute
        for attribute in dir(ent_module)
        if not attribute.startswith("_")
        and callable(getattr(ent_module, attribute))
        and getattr(ent_module, attribute).__module__.startswith("pronotepy")
    )
    credentials = SessionCredentials(
        login_mode=LoginMode.ENT,
        pronote_url="https://demo.example.invalid/pronote/parent.html",
        username="parent-under-test",
        password="not-a-real-password",
        ent_provider=name,
    )

    assert _resolve_ent(credentials) is getattr(ent_module, name)


# ---------------------------------------------------------------------------
# A transport failure is described, never quoted
# ---------------------------------------------------------------------------

#: What ``requests`` writes when a school's server does not answer: the host,
#: and the page path with whatever parameters it carried. Fictional, and
#: shaped like the real thing.
_LEAKY_HOST = "demo.example.invalid"
_LEAKY_PARAMETER = "identifiant=NOT-A-REAL-SESSION"
_LEAKY_MESSAGE = (
    f"HTTPSConnectionPool(host='{_LEAKY_HOST}', port=443): Max retries exceeded "
    f"with url: /pronote/parent.html?{_LEAKY_PARAMETER}"
)


def _leak_free(text: str) -> bool:
    return _LEAKY_HOST not in text and _LEAKY_PARAMETER not in text


async def test_an_unreachable_school_is_logged_without_its_address(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    """The ``log-when-unavailable`` line printed ``str(error)`` verbatim.

    For a ``requests`` failure that is the establishment's host and the path
    the call failed on, session parameters included -- at INFO, in the log a
    user pastes into a public issue. The line must still say *what kind* of
    failure it was, or it stops being worth reading.
    """
    caplog.set_level(logging.DEBUG, logger="custom_components.carnet_scolaire")

    with pytest.raises(requests.ConnectionError):
        await harness.manager.run(
            "timetable",
            Priority.HIGH,
            _work(raises=requests.ConnectionError(_LEAKY_MESSAGE)),
        )

    assert "PRONOTE is unavailable: ConnectionError (connection)" in caplog.text
    assert _leak_free(caplog.text)
    assert _leak_free(repr(harness.manager.diagnostics()))


async def test_a_login_that_cannot_reach_the_school_is_logged_without_its_address(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    """The login path had its own copy of the same ``str(error)``.

    A school that is down fails at the login first -- the bootstrap GET is the
    first request of every session -- so this is the copy an outage actually
    reaches, and it logged the page address a parent had typed.
    """
    caplog.set_level(logging.DEBUG, logger="custom_components.carnet_scolaire")

    with (
        patch(
            "custom_components.carnet_scolaire.session.build_client",
            side_effect=requests.ConnectTimeout(_LEAKY_MESSAGE),
        ),
        pytest.raises(requests.ConnectTimeout),
    ):
        await harness.manager.run("timetable", Priority.HIGH, _work())

    assert "PRONOTE is unavailable: ConnectTimeout (timeout)" in caplog.text
    assert _leak_free(caplog.text)
    assert _leak_free(repr(harness.manager.diagnostics()))
