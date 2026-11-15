"""The rate limiter, to 100 %.

This module is the one that protects the user's account, so its coverage gate
is total rather than nominal. It also has no dependency on Home Assistant or on
PRONOTE -- both clocks, the RNG and the sleeper are constructor arguments -- so
"100 %" here means every branch, with no patching of module globals and no
sleeping.

The tests are grouped by the property each one defends, and every group names
the failure it prevents. A test called ``test_bad_password_counts`` that does
not say *why* is a test the next reader deletes.
"""

from datetime import UTC, datetime, time, timedelta
import random
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import pytest

from custom_components.carnet_scolaire.const import LimiterState, Priority
from custom_components.carnet_scolaire.ratelimit import (
    BATCH_GRACE_SECONDS,
    CAP_WARNING_FRACTION,
    LOGIN_COST_KEY,
    LOWEST_SHED_THRESHOLD,
    REQUESTS_PER_LOGIN,
    REQUESTS_PER_LOGIN_WORST_CASE,
    SECONDS_PER_HOUR,
    DeferReason,
    LoginOutcome,
    LoginRefusedByLimiter,
    RateLimitConfig,
    RateLimiter,
    TierDeferred,
    _asyncio_sleep,
    _elapsed,
    _is_number,
)

from .clock import FakeClock, RecordingSleeper

if TYPE_CHECKING:
    from collections.abc import Callable

PARIS = ZoneInfo("Europe/Paris")


def build(
    clock: FakeClock,
    sleeper: RecordingSleeper | None = None,
    **overrides: object,
) -> RateLimiter:
    """A limiter with every seam simulated and a seeded RNG."""
    return RateLimiter(
        RateLimitConfig(**overrides),  # type: ignore[arg-type]
        clock=clock.monotonic,
        now=clock.now,
        rng=random.Random(1),  # noqa: S311 -- jitter, not cryptography
        sleep=sleeper,
    )


async def _work(value: str = "done") -> str:
    """A unit of work that never fails."""
    return value


def _factory(value: str = "done") -> Callable[[], object]:
    """A zero-argument coroutine factory, which is what ``call`` takes."""
    return lambda: _work(value)


# ---------------------------------------------------------------------------
# Layer 1 -- spacing, charged at admission
# ---------------------------------------------------------------------------


async def test_spacing_is_charged_before_the_request_leaves(
    clock: FakeClock, sleeper: RecordingSleeper
) -> None:
    """Two callers must not compute the same wait and fire together.

    This is the whole reason admission is serialised. Charged retrospectively,
    a scheduled batch and a service invoked during it both read the same
    ``_last_call``, both computed a zero wait and both fired in the same
    millisecond -- desynchronising PRONOTE's encrypted request counter and
    breaking the session. That is the first sanction in annexe B §1, produced
    by the very layer meant to prevent it.
    """
    limiter = build(clock, sleeper, min_request_interval=1.0)

    await limiter.call("timetable", Priority.HIGH, _factory())
    await limiter.call("homework", Priority.NORMAL, _factory())

    # The second caller was made to wait the full interval, and the clock moved
    # while it did -- so the two requests cannot have left together.
    assert sleeper.slept == [pytest.approx(1.0)]


async def test_first_call_is_not_penalised_for_starting_up(clock: FakeClock) -> None:
    """``_last_call`` starts one interval in the past, so a cold start is free."""
    limiter = build(clock, min_request_interval=5.0)
    assert limiter.check("timetable", Priority.HIGH).wait == 0.0


# ---------------------------------------------------------------------------
# Layer 2 -- the token bucket
# ---------------------------------------------------------------------------


def test_the_cost_asked_of_the_bucket_is_the_cost_charged(clock: FakeClock) -> None:
    """Asking for one token while spending six removes the smoothing entirely.

    ``history`` costs eight requests. Declared as one, it drained four times the
    burst capacity per hour with no wait at all -- so the layer that exists to
    spread load did nothing for the one tier that most needs spreading.
    """
    limiter = build(clock, burst_size=10, max_requests_per_hour=3600)

    verdict = limiter.check("history", Priority.LOW, cost=8.0)
    assert verdict.allowed
    limiter.commit("history", 8)

    assert limiter.tokens == pytest.approx(2.0)
    assert limiter.calls_by_tier["history"] == 8


def test_an_overdraft_has_to_be_repaid(clock: FakeClock) -> None:
    """The bucket goes negative rather than forgiving the debt.

    Clamped at zero, a burst of expensive calls cost nothing to repay: the
    bucket read empty, refilled from empty, and the hourly rate stopped
    describing anything.
    """
    limiter = build(clock, burst_size=5, max_requests_per_hour=3600)
    limiter.commit("history", 8)
    assert limiter.tokens == pytest.approx(-3.0)

    # One second at 3600/h is one token, so the debt takes three seconds.
    clock.advance(3.0)
    assert limiter.tokens == pytest.approx(0.0)


def test_the_bucket_never_overfills(clock: FakeClock) -> None:
    """Refill is capped at ``burst_size``, however long the integration idled."""
    limiter = build(clock, burst_size=20, max_requests_per_hour=240)
    clock.advance(10 * SECONDS_PER_HOUR)
    assert limiter.tokens == pytest.approx(20.0)


def test_a_zero_hourly_rate_never_grants_a_token(clock: FakeClock) -> None:
    """An hourly rate of zero yields an infinite wait rather than a crash.

    Unreachable through the options flow, which is exactly why it is tested:
    a hand-edited ``.storage`` file or a restore from a different version can
    produce it, and ``(count - tokens) / 0`` is a ``ZeroDivisionError`` in the
    middle of a collection.
    """
    limiter = build(clock, burst_size=0, max_requests_per_hour=0, max_wait=60.0)
    verdict = limiter.check("timetable", Priority.HIGH)
    assert not verdict.allowed
    assert verdict.wait == float("inf")
    assert verdict.reason is DeferReason.HOURLY_BUDGET


def test_a_wait_beyond_max_wait_defers_rather_than_blocking(clock: FakeClock) -> None:
    """Postponed, not failed: the tier keeps its snapshot and moves its deadline."""
    limiter = build(clock, burst_size=1, max_requests_per_hour=1, max_wait=10.0)
    limiter.commit("timetable", 1)

    verdict = limiter.check("timetable", Priority.HIGH)
    assert not verdict.allowed
    assert verdict.reason is DeferReason.HOURLY_BUDGET
    assert limiter.throttled


# ---------------------------------------------------------------------------
# Layer 3 -- the daily cap and the order of sacrifice
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("priority", "fraction", "expected"),
    [
        (Priority.LOW, 0.61, False),
        (Priority.NORMAL, 0.61, True),
        (Priority.NORMAL, 0.81, False),
        (Priority.HIGH, 0.81, True),
        (Priority.HIGH, 1.01, False),
        (Priority.CRITICAL, 1.01, True),
        (Priority.CRITICAL, 50.0, True),
    ],
)
def test_the_order_of_sacrifice_is_decided_not_arbitrary(
    clock: FakeClock, priority: Priority, fraction: float, expected: bool
) -> None:
    """Low goes first, high next, and ``CRITICAL`` never.

    The last two rows are the ones that matter. ``calls_today`` has no ceiling,
    so *any* finite threshold for ``CRITICAL`` is reachable -- a user who sets
    the documented floor of 100 requests a day crosses 2.0 on the first day --
    and reaching it refuses the login every other tier depends on. Hence
    ``float("inf")`` rather than a large number.
    """
    # The bucket is opened wide on purpose: this test is about layer 3, and
    # with the default burst of 20 the *bucket* refused at 61 requests -- so
    # the assertion passed for the wrong reason on the rows that matter.
    limiter = build(
        clock,
        max_requests_per_day=100,
        burst_size=10_000,
        max_requests_per_hour=360_000,
    )
    limiter.commit("timetable", int(100 * fraction))

    assert limiter.check("t", priority).allowed is expected


def test_the_cap_warning_opens_before_anything_is_shed(clock: FakeClock) -> None:
    """80 % opens an informative repair; 60 % is where shedding starts."""
    limiter = build(
        clock,
        max_requests_per_day=100,
        burst_size=10_000,
        max_requests_per_hour=360_000,
    )
    limiter.commit("timetable", int(100 * CAP_WARNING_FRACTION))

    assert limiter.check("timetable", Priority.HIGH).allowed
    assert limiter.cap_warning_open
    assert CAP_WARNING_FRACTION > LOWEST_SHED_THRESHOLD


def test_a_daily_cap_of_zero_sheds_everything_but_critical(clock: FakeClock) -> None:
    """A cap of zero is a fraction of 1.0, not a division by zero."""
    limiter = build(clock, max_requests_per_day=0)
    assert not limiter.check("timetable", Priority.HIGH).allowed
    assert limiter.check("session", Priority.CRITICAL).allowed


def test_the_daily_cap_surfaces_its_reason_in_the_counters(clock: FakeClock) -> None:
    """Annexe B §2.3: the cap is never silent.

    Derived from the *hold* alone, ``reason`` read ``None`` for the one case the
    requirement is about -- data quietly ageing with nothing in the log, which
    is the hardest symptom there is to diagnose.
    """
    limiter = build(clock, max_requests_per_day=10)
    limiter.commit("timetable", 7)
    assert not limiter.check("news", Priority.LOW).allowed

    counters = limiter.snapshot_counters()
    assert counters["reason"] == str(DeferReason.DAILY_CAP)
    assert counters["throttled"] is True


# ---------------------------------------------------------------------------
# The day boundary
# ---------------------------------------------------------------------------


def test_the_counters_reset_when_the_date_moves_forward(clock: FakeClock) -> None:
    """Midnight clears requests, logins and the warning flag."""
    limiter = build(clock)
    limiter.commit("timetable", 5)
    limiter.note_login(LoginOutcome.SUCCESS)
    assert limiter.calls_today > 5

    clock.set_wall(clock.now() + timedelta(days=1))
    assert limiter.calls_today == 0
    assert limiter.logins_today == 0
    assert limiter.calls_by_tier == {}


def test_a_backwards_clock_step_does_not_re_grant_the_day(clock: FakeClock) -> None:
    """An NTP correction must not hand back the whole budget.

    Resetting on *any* date change re-granted both the request budget and the
    login cap whenever the host's clock stepped backwards -- which happens on a
    machine whose clock was wrong at boot, and would let a failing account
    retry its wrong password twenty-four more times.
    """
    limiter = build(clock)
    limiter.commit("timetable", 5)
    limiter.note_login(LoginOutcome.BAD_CREDENTIALS)

    clock.set_wall(clock.now() - timedelta(days=1))

    assert limiter.calls_today == 5 + REQUESTS_PER_LOGIN
    assert limiter.logins_today == 1


def test_the_bucket_is_untouched_by_the_date(clock: FakeClock) -> None:
    """The bucket smooths the hour and has no opinion about the date."""
    limiter = build(clock, burst_size=10, max_requests_per_hour=1)
    limiter.commit("timetable", 10)
    tokens_before = limiter.tokens

    clock.set_wall(clock.now() + timedelta(days=1))

    assert limiter.tokens == pytest.approx(tokens_before)


# ---------------------------------------------------------------------------
# Quiet hours
# ---------------------------------------------------------------------------


def test_quiet_hours_defer_everything_but_critical_and_gestures(
    clock: FakeClock,
) -> None:
    """At night the session tier runs, and so does whatever a person asked for.

    Quiet hours protect the address against *automatic* collection. A parent
    opening a homework sheet at 23:30 is one request started by a hand, and
    refusing it read as "disconnected" -- the integration is not, it is being
    polite to the school's server, and a click is not what that politeness is
    about. A timer-driven ``HIGH`` call still waits.
    """
    clock.set_wall(datetime(2026, 3, 12, 23, 30, tzinfo=PARIS))
    limiter = build(clock)

    verdict = limiter.check("timetable", Priority.HIGH)
    assert not verdict.allowed
    assert verdict.reason is DeferReason.QUIET_HOURS
    assert limiter.check("session", Priority.CRITICAL).allowed
    assert limiter.check("homework", Priority.GESTURE).allowed


def test_a_gesture_is_shed_at_the_daily_cap_like_high(clock: FakeClock) -> None:
    """Crossing quiet hours is the whole dispensation; the budget is not part of it.

    A gesture that also escaped the cap would let a dashboard -- or an
    automation calling a service in a loop -- spend without bound, which is
    the one thing the cap exists to prevent. It is shed exactly where ``HIGH``
    is, and never promoted to ``CRITICAL``'s infinite threshold.
    """
    limiter = build(clock, max_requests_per_day=10)
    for _ in range(10):
        limiter.commit("timetable")

    verdict = limiter.check("homework", Priority.GESTURE)

    assert not verdict.allowed
    assert verdict.reason is DeferReason.DAILY_CAP
    assert limiter.check("session", Priority.CRITICAL).allowed


def test_the_quiet_window_can_be_disabled(clock: FakeClock) -> None:
    """With quiet hours off, 03:00 is an ordinary time of day."""
    clock.set_wall(datetime(2026, 3, 12, 3, 0, tzinfo=PARIS))
    limiter = build(clock, quiet_hours_enabled=False)
    assert limiter.check("timetable", Priority.HIGH).allowed
    assert limiter.quiet_duration_seconds() == 0.0


def test_a_degenerate_quiet_window_is_no_window(clock: FakeClock) -> None:
    """``quiet_start == quiet_end`` means "never quiet", not "always quiet"."""
    clock.set_wall(datetime(2026, 3, 12, 3, 0, tzinfo=PARIS))
    limiter = build(clock, quiet_start=time(22, 0), quiet_end=time(22, 0))
    assert limiter.check("timetable", Priority.HIGH).allowed
    assert limiter.quiet_duration_seconds() == 0.0
    assert (
        limiter.quiet_seconds_between(
            datetime(2026, 3, 12, 0, 0, tzinfo=PARIS),
            datetime(2026, 3, 13, 0, 0, tzinfo=PARIS),
        )
        == 0.0
    )


@pytest.mark.parametrize(
    ("hour", "quiet"),
    [
        (23, True),
        (2, True),
        (5, True),
        (6, False),
        (12, False),
        (21, False),
        (22, True),
    ],
)
def test_a_window_crossing_midnight_is_read_correctly(
    clock: FakeClock, hour: int, quiet: bool
) -> None:
    """22:00 to 06:00 is the default, and it wraps."""
    clock.set_wall(datetime(2026, 3, 12, hour, 0, tzinfo=PARIS))
    limiter = build(clock)
    reason = limiter.check("timetable", Priority.HIGH).reason
    assert (reason is DeferReason.QUIET_HOURS) is quiet


@pytest.mark.parametrize(("hour", "quiet"), [(10, True), (13, False), (9, False)])
def test_a_window_inside_one_day_is_read_correctly(
    clock: FakeClock, hour: int, quiet: bool
) -> None:
    """A same-day window -- 10:00 to 12:00 -- takes the other branch."""
    clock.set_wall(datetime(2026, 3, 12, hour, 0, tzinfo=PARIS))
    limiter = build(clock, quiet_start=time(10, 0), quiet_end=time(12, 0))
    reason = limiter.check("timetable", Priority.HIGH).reason
    assert (reason is DeferReason.QUIET_HOURS) is quiet


def test_the_default_quiet_window_is_eight_hours(clock: FakeClock) -> None:
    """22:00 through 06:00, measured the way staleness will measure it."""
    limiter = build(clock)
    assert limiter.quiet_duration_seconds() == pytest.approx(8 * SECONDS_PER_HOUR)


def test_a_same_day_quiet_window_measures_its_own_length(clock: FakeClock) -> None:
    """The non-wrapping branch of ``quiet_duration_seconds``."""
    limiter = build(clock, quiet_start=time(10, 0), quiet_end=time(12, 30))
    assert limiter.quiet_duration_seconds() == pytest.approx(2.5 * SECONDS_PER_HOUR)


def test_quiet_time_inside_an_interval_is_measured(clock: FakeClock) -> None:
    """This is what stops every fast tier going unavailable at 06:00.

    An eight-hour night exceeds ``stale_after`` times the interval of the
    timetable, homework, news and discussion tiers. Counted against their age,
    it made all four go ``unavailable`` every single morning -- and an entity
    that goes unavailable and comes back fires ``numeric_state`` triggers
    spuriously (§2.5). Reporting "I no longer know" because the integration was
    asleep on purpose is the one answer that is not allowed.
    """
    limiter = build(clock)
    overnight = limiter.quiet_seconds_between(
        datetime(2026, 3, 11, 20, 0, tzinfo=PARIS),
        datetime(2026, 3, 12, 8, 0, tzinfo=PARIS),
    )
    assert overnight == pytest.approx(8 * SECONDS_PER_HOUR)


def test_quiet_time_is_zero_for_an_empty_or_inverted_interval(
    clock: FakeClock,
) -> None:
    """``end <= start`` yields zero rather than a negative excuse."""
    limiter = build(clock)
    moment = datetime(2026, 3, 12, 8, 0, tzinfo=PARIS)
    assert limiter.quiet_seconds_between(moment, moment) == 0.0
    assert limiter.quiet_seconds_between(moment, moment - timedelta(hours=1)) == 0.0


def test_quiet_time_is_zero_when_quiet_hours_are_off(clock: FakeClock) -> None:
    """Nothing to excuse when nothing is postponed."""
    limiter = build(clock, quiet_hours_enabled=False)
    assert (
        limiter.quiet_seconds_between(
            datetime(2026, 3, 11, 20, 0, tzinfo=PARIS),
            datetime(2026, 3, 12, 8, 0, tzinfo=PARIS),
        )
        == 0.0
    )


def test_quiet_time_across_a_same_day_window_is_measured(clock: FakeClock) -> None:
    """The non-wrapping branch of the day-by-day walk."""
    limiter = build(clock, quiet_start=time(10, 0), quiet_end=time(12, 0))
    measured = limiter.quiet_seconds_between(
        datetime(2026, 3, 12, 9, 0, tzinfo=PARIS),
        datetime(2026, 3, 12, 13, 0, tzinfo=PARIS),
    )
    assert measured == pytest.approx(2 * SECONDS_PER_HOUR)


def test_the_quiet_walk_is_bounded(clock: FakeClock) -> None:
    """A staleness question never spans a year; a runaway loop would.

    The bound is what makes the answer *wrong but finite* for an absurd input
    rather than hanging the event loop, and the walk is only ever asked about
    an age.
    """
    limiter = build(clock)
    measured = limiter.quiet_seconds_between(
        datetime(2020, 1, 1, 0, 0, tzinfo=PARIS),
        datetime(2026, 1, 1, 0, 0, tzinfo=PARIS),
    )
    assert 0 < measured < 6 * 365 * 8 * SECONDS_PER_HOUR


def test_seconds_to_quiet_end_wraps_to_tomorrow(clock: FakeClock) -> None:
    """At 23:30 the window reopens at 06:00 *the next day*."""
    clock.set_wall(datetime(2026, 3, 12, 23, 30, tzinfo=PARIS))
    limiter = build(clock)
    verdict = limiter.check("timetable", Priority.HIGH)
    assert verdict.wait == pytest.approx(6.5 * SECONDS_PER_HOUR)


def test_seconds_to_quiet_end_is_today_when_the_window_still_closes_today(
    clock: FakeClock,
) -> None:
    """At 02:00 the window reopens at 06:00 the same day."""
    clock.set_wall(datetime(2026, 3, 12, 2, 0, tzinfo=PARIS))
    limiter = build(clock)
    verdict = limiter.check("timetable", Priority.HIGH)
    assert verdict.wait == pytest.approx(4 * SECONDS_PER_HOUR)


def test_elapsed_seconds_survive_a_dst_transition() -> None:
    """Subtracting two aware datetimes with the same ``tzinfo`` is not enough.

    CPython skips the offset lookup when both operands share the same
    ``tzinfo`` object and returns the *wall-clock* difference. On the March
    transition "01:30 until 06:00" then came out as four and a half hours where
    three and a half actually elapse -- so a quiet-hours excuse was an hour too
    generous exactly once a year, in the direction that hides staleness.
    """
    start = datetime(2026, 3, 29, 1, 30, tzinfo=PARIS)
    end = datetime(2026, 3, 29, 6, 0, tzinfo=PARIS)
    assert (end - start).total_seconds() == pytest.approx(4.5 * SECONDS_PER_HOUR)
    assert _elapsed(start, end) == pytest.approx(3.5 * SECONDS_PER_HOUR)


# ---------------------------------------------------------------------------
# Batches
# ---------------------------------------------------------------------------


def test_a_batch_begun_before_22_00_may_finish_after_it(clock: FakeClock) -> None:
    """Annexe B §8: entering quiet hours mid-batch postpones the *next* batch.

    Without a batch boundary the remaining tiers of a batch straddling 22:00
    were refused halfway through, which is neither of the two documented
    behaviours -- the user got a partial collection and no explanation.
    """
    clock.set_wall(datetime(2026, 3, 12, 21, 59, tzinfo=PARIS))
    limiter = build(clock)
    limiter.begin_batch()

    clock.advance(120)  # now 22:01
    assert limiter.check("homework", Priority.NORMAL).allowed


def test_the_grace_does_not_stay_open_all_night(clock: FakeClock) -> None:
    """A hung batch must not hold the exemption open until morning."""
    clock.set_wall(datetime(2026, 3, 12, 21, 59, tzinfo=PARIS))
    limiter = build(clock)
    limiter.begin_batch()

    clock.advance(BATCH_GRACE_SECONDS + 60)
    assert not limiter.check("homework", Priority.NORMAL).allowed


def test_a_batch_begun_inside_quiet_hours_earns_no_grace(clock: FakeClock) -> None:
    """The exemption is for a batch interrupted by the night, not born in it."""
    clock.set_wall(datetime(2026, 3, 12, 23, 0, tzinfo=PARIS))
    limiter = build(clock)
    limiter.begin_batch()
    assert not limiter.check("homework", Priority.NORMAL).allowed


def test_ending_a_batch_closes_the_grace(clock: FakeClock) -> None:
    """``end_batch`` is what the account's ``finally`` clause exists for."""
    clock.set_wall(datetime(2026, 3, 12, 21, 59, tzinfo=PARIS))
    limiter = build(clock)
    limiter.begin_batch()
    clock.advance(120)
    limiter.end_batch()
    assert not limiter.check("homework", Priority.NORMAL).allowed


# ---------------------------------------------------------------------------
# Authentication -- the only sanction that really hurts
# ---------------------------------------------------------------------------


def test_a_wrong_password_charges_the_ip_guard(clock: FakeClock) -> None:
    """``BAD_CREDENTIALS``, not ``TRANSPORT``, and this is the important one.

    A wrong password does **not** raise ``PronoteAPIError``: the challenge
    decryption fails and ``_login`` raises ``CryptoError``, or it returns
    ``False`` because the ``cle`` key is absent. Hooked to the wrong exception,
    the counter meant to protect the address would never increment -- the guard
    rail would exist in the documentation and not in the running code.
    """
    limiter = build(clock, max_failed_logins_per_hour=3)
    for _ in range(2):
        limiter.note_login(LoginOutcome.BAD_CREDENTIALS)

    assert limiter.failed_logins_last_hour == 2
    assert limiter.may_login().allowed


def test_the_third_wrong_password_stops_the_attempts(clock: FakeClock) -> None:
    """Repeatedly retrying a wrong password is what gets an address suspended."""
    limiter = build(clock, max_failed_logins_per_hour=3, credentials_hold=3600.0)
    for _ in range(3):
        limiter.note_login(LoginOutcome.BAD_CREDENTIALS)

    verdict = limiter.may_login()
    assert not verdict.allowed
    assert verdict.reason is DeferReason.CREDENTIALS_HOLD
    assert limiter.state is LimiterState.CREDENTIALS_HOLD


def test_qr_enrolment_does_not_eat_two_thirds_of_the_guard(clock: FakeClock) -> None:
    """QR enrolment performs two complete logins **by construction**.

    ``qrcode_login`` builds a client -- whose constructor logs in -- posts
    ``PageInfosPerso`` 49, then calls ``token_login`` with the exported
    credentials. That deliberate doubling must not consume two of three
    attempts, or a single enrolment would leave the account one failure from a
    one-hour hold.
    """
    limiter = build(clock, max_failed_logins_per_hour=3)
    limiter.note_login(LoginOutcome.BAD_CREDENTIALS, counts_against_guard=False)

    assert limiter.failed_logins_last_hour == 0
    # It still costs requests, and the budget must see them.
    assert limiter.calls_today == REQUESTS_PER_LOGIN
    assert limiter.logins_today == 1


def test_every_login_attempt_counts_successful_or_not(clock: FakeClock) -> None:
    """Counting only successes let a failing account attempt without bound.

    The counter meant to stop it stayed at zero while the loop ran, which is
    the precise shape of "a guard rail in the documentation only".
    """
    limiter = build(clock)
    limiter.note_login(LoginOutcome.SUCCESS)
    limiter.note_login(LoginOutcome.TRANSPORT)
    limiter.note_login(LoginOutcome.BOOTSTRAP)
    assert limiter.logins_today == 3


def test_a_failure_expires_after_a_full_hour_not_at_it(clock: FakeClock) -> None:
    """Strictly older than an hour, and the strictness is load-bearing.

    Keeping a failure for exactly 3600 s made a ``may_login()`` landing on the
    instant a hold expired restart that hold -- doubling it -- and made any
    test advancing a simulated clock by exactly ``credentials_hold`` fail for a
    reason unrelated to what it was testing.
    """
    limiter = build(clock, max_failed_logins_per_hour=1, credentials_hold=10.0)
    limiter.note_login(LoginOutcome.BAD_CREDENTIALS)
    assert limiter.failed_logins_last_hour == 1

    clock.advance(SECONDS_PER_HOUR)
    assert limiter.failed_logins_last_hour == 0


def test_the_login_cap_is_a_real_ceiling(clock: FakeClock) -> None:
    """At the cap, the next attempt waits for midnight."""
    limiter = build(clock, max_logins_per_day=2)
    limiter.note_login(LoginOutcome.SUCCESS)
    limiter.note_login(LoginOutcome.SUCCESS)

    verdict = limiter.may_login()
    assert not verdict.allowed
    assert verdict.reason is DeferReason.LOGIN_CAP
    assert 0 < verdict.wait <= 24 * SECONDS_PER_HOUR
    assert limiter.throttled


def test_a_login_is_spaced_and_drawn_from_the_bucket(clock: FakeClock) -> None:
    """Exempt from *shedding* only -- never from the other two layers.

    A login costs five to seven HTTP requests. A budget that did not see them
    would be describing a different integration than the one running.
    """
    limiter = build(clock, burst_size=6, max_requests_per_hour=3600)
    limiter.commit("timetable", 3)

    verdict = limiter.may_login(cost=REQUESTS_PER_LOGIN)
    assert verdict.allowed
    assert verdict.wait > 0


def test_a_login_is_never_shed_by_the_daily_cap(clock: FakeClock) -> None:
    """Refusing to log in leaves every entity stale with no way back."""
    limiter = build(clock, max_requests_per_day=10)
    limiter.commit("timetable", 50)
    assert limiter.may_login().allowed


def test_the_mfa_hold_stops_a_thousand_pointless_logins(clock: FakeClock) -> None:
    """A hold, not merely a flag.

    PRONOTE is waiting for a PIN this integration deliberately never stores, so
    no amount of retrying can succeed -- and each retry costs five to seven
    requests. Left as a flag this produced thousands of login attempts a day:
    the one gesture that gets an address suspended.
    """
    limiter = build(clock, credentials_hold=1800.0)
    limiter.note_login(LoginOutcome.MFA_REQUIRED)

    verdict = limiter.may_login()
    assert not verdict.allowed
    assert verdict.reason is DeferReason.MFA_HOLD
    # Annexe A §7 fixes the value set, so no state is invented for this case.
    assert limiter.state is LimiterState.CREDENTIALS_HOLD


def test_only_a_human_lifts_the_mfa_hold(clock: FakeClock) -> None:
    """``reset_after_reauth`` is the only way out, which is correct.

    That hold exists precisely because a person has to act.
    """
    limiter = build(clock, credentials_hold=1800.0)
    limiter.note_login(LoginOutcome.MFA_REQUIRED)
    limiter.reset_after_reauth()

    assert limiter.may_login().allowed
    assert limiter.state is LimiterState.NOMINAL
    assert not limiter.throttled


def test_an_unreadable_response_is_neither_a_bad_password_nor_a_transient(
    clock: FakeClock,
) -> None:
    """``UNDECODABLE`` holds for the bootstrap duration and spares the IP guard.

    A protocol change is not the credentials' fault, so it must not spend the
    guard; it is not transient either, so a bare backoff would retry for ever.
    """
    limiter = build(clock, bootstrap_hold=3600.0)
    limiter.note_login(LoginOutcome.UNDECODABLE)

    assert limiter.failed_logins_last_hour == 0
    assert limiter.state is LimiterState.BOOTSTRAP_FAILED


def test_an_unusable_bootstrap_never_claims_to_know_why(clock: FakeClock) -> None:
    """There is no ``ip_suspended`` state, because nothing can establish one.

    Upstream infers it from ``if "IP" in html``, which is a substring test on a
    marketing page. Annexe B §3.4 refuses to build a user-facing accusation on
    that.
    """
    limiter = build(clock)
    limiter.note_login(LoginOutcome.BOOTSTRAP)
    assert limiter.state is LimiterState.BOOTSTRAP_FAILED
    assert not hasattr(LimiterState, "IP_SUSPENDED")


def test_a_success_clears_the_backoff_entirely(clock: FakeClock) -> None:
    """A server that answers once is a server that works (annexe B §4)."""
    limiter = build(clock)
    limiter.note_failure()
    limiter.note_failure()
    assert limiter.consecutive_failures == 2

    limiter.note_success()
    assert limiter.consecutive_failures == 0
    assert limiter.state is LimiterState.NOMINAL


def test_a_transport_error_cannot_downgrade_a_credentials_hold(
    clock: FakeClock,
) -> None:
    """The severity ordering, and it is not decorative.

    Without it a single transport error replaced an hour-long credentials hold
    with a thirty-second backoff, and the next success cleared it -- so the hold
    annexe B §3.4 requires simply evaporated, and the wrong password went back
    to being retried every few minutes.
    """
    limiter = build(clock, max_failed_logins_per_hour=1, credentials_hold=3600.0)
    limiter.note_login(LoginOutcome.BAD_CREDENTIALS)
    assert limiter.state is LimiterState.CREDENTIALS_HOLD

    limiter.note_failure()
    limiter.note_success()

    assert limiter.state is LimiterState.CREDENTIALS_HOLD
    assert not limiter.may_login().allowed


def test_a_worse_hold_replaces_a_backoff(clock: FakeClock) -> None:
    """The ordering works upwards as well as downwards."""
    limiter = build(clock, bootstrap_hold=3600.0)
    limiter.note_failure()
    assert limiter.state is LimiterState.BACKOFF

    limiter.note_login(LoginOutcome.BOOTSTRAP)
    assert limiter.state is LimiterState.BOOTSTRAP_FAILED


def test_a_hold_expires_on_its_own(clock: FakeClock) -> None:
    """Time passing is enough; nothing has to notice."""
    limiter = build(clock, bootstrap_hold=60.0)
    limiter.note_login(LoginOutcome.BOOTSTRAP)
    assert not limiter.check("timetable", Priority.HIGH).allowed

    clock.advance(61)
    assert limiter.check("timetable", Priority.HIGH).allowed


def test_the_hold_remaining_is_total(clock: FakeClock) -> None:
    """With no hold there is nothing left to wait, not a negative number.

    Both callers guard on ``_hold_until`` before asking, so this is a
    total-function property rather than a reachable path -- and stating it here
    is cheaper than leaving the reader to prove it.
    """
    limiter = build(clock)
    assert limiter._hold_remaining() == 0.0


def test_the_limiter_reports_the_night_as_its_state(clock: FakeClock) -> None:
    """``sensor.<compte>_etat_limiteur`` says ``quiet_hours``, not ``nominal``.

    "Nothing is happening and everything is fine" and "nothing is happening
    because it is 03:00" look identical from the outside, and the first is the
    one a user opens an issue about.
    """
    clock.set_wall(datetime(2026, 3, 12, 3, 0, tzinfo=PARIS))
    limiter = build(clock)
    assert limiter.state is LimiterState.QUIET_HOURS


def test_a_reached_daily_cap_reports_throttled_as_its_state(
    clock: FakeClock,
) -> None:
    """The last branch of the state machine: no hold, daytime, but shedding."""
    limiter = build(
        clock,
        max_requests_per_day=100,
        burst_size=10_000,
        max_requests_per_hour=360_000,
    )
    limiter.commit("news", 61)
    assert not limiter.check("news", Priority.LOW).allowed
    assert limiter.state is LimiterState.THROTTLED


def test_an_expired_hold_does_not_forget_the_failures_behind_it(
    clock: FakeClock,
) -> None:
    """The guard re-arms the hold rather than granting a fourth attempt.

    A short ``credentials_hold`` expires while the three failures are still
    inside their trailing hour. Without re-arming, the account got a free
    attempt every ``credentials_hold`` seconds for the rest of the hour -- which
    is the retry loop the guard exists to stop, merely slowed down.
    """
    limiter = build(clock, max_failed_logins_per_hour=2, credentials_hold=10.0)
    limiter.note_login(LoginOutcome.BAD_CREDENTIALS)
    limiter.note_login(LoginOutcome.BAD_CREDENTIALS)
    assert not limiter.may_login().allowed

    clock.advance(11.0)

    verdict = limiter.may_login()
    assert not verdict.allowed
    assert verdict.reason is DeferReason.CREDENTIALS_HOLD
    assert verdict.wait == pytest.approx(10.0)


def test_the_hold_end_is_reported_as_a_wall_clock_instant(clock: FakeClock) -> None:
    """The UI needs "until 09:15", not "in 3600 seconds"."""
    limiter = build(clock, bootstrap_hold=3600.0)
    assert limiter.hold_until_wallclock is None

    limiter.note_login(LoginOutcome.BOOTSTRAP)
    until = limiter.hold_until_wallclock
    assert until is not None
    assert until - clock.now() == pytest.approx(
        timedelta(hours=1), abs=timedelta(seconds=1)
    )


# ---------------------------------------------------------------------------
# Backoff and retry
# ---------------------------------------------------------------------------


def test_the_backoff_is_zero_before_any_consecutive_failure(clock: FakeClock) -> None:
    """Which is exactly why ``retry_delay`` exists."""
    limiter = build(clock)
    assert limiter.backoff_delay() == 0.0


def test_the_backoff_grows_and_is_jittered(clock: FakeClock) -> None:
    """Full jitter, so several instances do not resynchronise after an outage."""
    limiter = build(clock, backoff_base=30.0, backoff_max=3600.0)
    limiter.note_failure()
    first = limiter.backoff_delay()
    limiter.note_failure()
    second = limiter.backoff_delay()

    assert 15.0 <= first <= 45.0
    assert 30.0 <= second <= 90.0


def test_a_pathological_failure_count_cannot_overflow(clock: FakeClock) -> None:
    """Computing ``2 ** 1024`` first turned a deep backoff into a crash.

    The exponent is clamped *before* the shift, which is the only order that
    works: ``min(backoff_max, base * 2 ** failures)`` still evaluates the shift.
    """
    limiter = build(clock, backoff_base=30.0, backoff_max=3600.0)
    for _ in range(2000):
        limiter._consecutive_failures += 1

    delay = limiter.backoff_delay()
    assert 1800.0 <= delay <= 5400.0


def test_retry_delay_is_never_zero(clock: FakeClock) -> None:
    """Deferring by zero seconds is what turned ten tiers into ten logins a tick.

    Bad credentials, a demanded PIN and an unreadable bootstrap all leave the
    *consecutive* failure counter at zero, so ``backoff_delay()`` returns 0.0
    for exactly the refusals that most need a pause. The tier was then due again
    on the very next tick.
    """
    limiter = build(clock, backoff_base=30.0)
    assert limiter.backoff_delay() == 0.0
    assert limiter.retry_delay() >= 15.0


def test_retry_delay_uses_the_backoff_when_there_is_one(clock: FakeClock) -> None:
    """It is a floor, not a replacement."""
    limiter = build(clock, backoff_base=30.0)
    limiter.note_failure()
    limiter.note_failure()
    limiter.note_failure()
    assert limiter.retry_delay() >= 60.0


# ---------------------------------------------------------------------------
# The throttled flag
# ---------------------------------------------------------------------------


def test_the_throttled_flag_does_not_flap_while_tiers_are_still_dropped(
    clock: FakeClock,
) -> None:
    """Cleared on every success, the cap became silent.

    One successful ``HIGH`` call reset the flag while ``LOW`` and ``NORMAL``
    tiers were still being dropped, so ``binary_sensor.<compte>_bride`` flapped
    back to ``off`` every quarter of an hour -- and annexe B §2.3 is explicit
    that the cap is never silent.
    """
    limiter = build(
        clock,
        max_requests_per_day=100,
        burst_size=10_000,
        max_requests_per_hour=360_000,
    )
    limiter.commit("news", 61)
    assert not limiter.check("news", Priority.LOW).allowed
    assert limiter.throttled

    assert limiter.check("timetable", Priority.HIGH).allowed
    limiter.note_success()

    assert limiter.throttled


def test_the_throttled_flag_clears_once_nothing_is_postponed(
    clock: FakeClock,
) -> None:
    """And it does clear -- the flag is not a one-way door."""
    limiter = build(
        clock,
        max_requests_per_day=100,
        burst_size=10_000,
        max_requests_per_hour=360_000,
    )
    limiter.commit("news", 61)
    assert not limiter.check("news", Priority.LOW).allowed

    clock.set_wall(clock.now() + timedelta(days=1))
    limiter.note_success()

    assert not limiter.throttled
    assert limiter.throttled_since is None


def test_the_throttled_flag_stays_while_the_login_cap_is_reached(
    clock: FakeClock,
) -> None:
    """A reached login cap is a postponement too, so the flag must reflect it."""
    limiter = build(clock, max_logins_per_day=1)
    limiter.note_login(LoginOutcome.SUCCESS)
    assert not limiter.may_login().allowed
    assert limiter.throttled

    limiter.note_success()
    assert limiter.throttled


def test_the_throttled_flag_stays_while_a_hold_is_active(clock: FakeClock) -> None:
    """The first guard of ``_maybe_clear_throttled``."""
    limiter = build(clock, bootstrap_hold=3600.0)
    limiter.note_login(LoginOutcome.BOOTSTRAP)
    limiter.note_success()
    assert limiter.throttled


def test_throttled_since_records_when_it_began(clock: FakeClock) -> None:
    """``binary_sensor.<compte>_bride`` reports "since 08:00", not just "on"."""
    limiter = build(clock, max_requests_per_day=1)
    began = clock.now()
    limiter.commit("news", 5)
    assert not limiter.check("news", Priority.LOW).allowed
    assert limiter.throttled_since == began

    clock.advance(600)
    assert not limiter.check("news", Priority.LOW).allowed
    assert limiter.throttled_since == began


# ---------------------------------------------------------------------------
# reconcile -- what makes GatewayResult.calls load-bearing
# ---------------------------------------------------------------------------


def test_reconcile_charges_a_call_that_cost_more_than_it_declared(
    clock: FakeClock,
) -> None:
    """Without this, ``GatewayResult.calls`` is decorative.

    An accidental lazy-property access -- ``Information.content``,
    ``Period.grades``, ``Discussion.messages`` -- places real requests that
    the spacing, the bucket and the daily cap all miss, and the truth appears
    only in a diagnostics field nobody compares.
    """
    limiter = build(clock)
    limiter.commit("discussions", 1)
    limiter.reconcile("discussions", charged=1, actual=4)

    assert limiter.calls_by_tier["discussions"] == 4


def test_reconcile_never_refunds(clock: FakeClock) -> None:
    """A limiter that gave budget back would make optimism a burst allowance."""
    limiter = build(clock)
    limiter.commit("history", 8)
    limiter.reconcile("history", charged=8, actual=2)

    assert limiter.calls_by_tier["history"] == 8


# ---------------------------------------------------------------------------
# The two public entry points
# ---------------------------------------------------------------------------


async def test_call_charges_then_runs_then_clears_the_backoff(
    clock: FakeClock, sleeper: RecordingSleeper
) -> None:
    """The happy path, and the order matters: charge, wait, run."""
    limiter = build(clock, sleeper, backoff_base=30.0)
    limiter.note_failure()
    # `note_failure` also starts a hold, which is the correct behaviour and is
    # asserted elsewhere. Here the hold is waited out, so what is being tested
    # is the *success* path: charge, then run, then clear the failure count.
    clock.advance(120)

    result = await limiter.call("timetable", Priority.HIGH, _factory("lessons"))

    assert result == "lessons"
    assert limiter.calls_by_tier["timetable"] == 1
    assert limiter.consecutive_failures == 0


async def test_call_raises_rather_than_returning_a_verdict(
    clock: FakeClock, sleeper: RecordingSleeper
) -> None:
    """Raised so no caller can forget to check it.

    ``TierDeferred`` is a control-flow signal, not a failure: the tier keeps its
    previous snapshot and only its deadline moves.
    """
    clock.set_wall(datetime(2026, 3, 12, 23, 0, tzinfo=PARIS))
    limiter = build(clock, sleeper)

    with pytest.raises(TierDeferred) as raised:
        await limiter.call("timetable", Priority.HIGH, _factory())

    assert raised.value.reason is DeferReason.QUIET_HOURS
    assert raised.value.retry_after > 0
    assert not sleeper.slept


async def test_call_honours_the_wait_before_the_request_leaves(
    clock: FakeClock, sleeper: RecordingSleeper
) -> None:
    """The sleep happens *inside* the admission lock, which is the point."""
    limiter = build(clock, sleeper, burst_size=2, max_requests_per_hour=3600)
    limiter.commit("timetable", 2)

    await limiter.call("timetable", Priority.HIGH, _factory())

    assert sleeper.slept
    assert sleeper.total > 0


async def test_call_accepts_a_per_call_sleeper(
    clock: FakeClock, sleeper: RecordingSleeper
) -> None:
    """The seam exists at the call site too, for the session's own deadline."""
    limiter = build(clock, min_request_interval=2.0)
    await limiter.call("timetable", Priority.HIGH, _factory(), sleep=sleeper)
    await limiter.call("timetable", Priority.HIGH, _factory(), sleep=sleeper)
    assert sleeper.slept == [pytest.approx(2.0)]


async def test_login_refuses_distinctly_from_a_deferred_tier(
    clock: FakeClock, sleeper: RecordingSleeper
) -> None:
    """``LoginRefusedByLimiter`` cannot be mistaken for a server problem.

    Nothing was sent, no credential was tried, and retrying sooner would make
    it worse -- which is a different thing to tell the user than "the school's
    server is slow".
    """
    limiter = build(clock, sleeper, max_logins_per_day=0)

    with pytest.raises(LoginRefusedByLimiter) as raised:
        await limiter.login(_factory())

    assert raised.value.reason is DeferReason.LOGIN_CAP


async def test_login_runs_and_spaces(
    clock: FakeClock, sleeper: RecordingSleeper
) -> None:
    """The happy path, spaced like any other request."""
    limiter = build(clock, sleeper, min_request_interval=1.0)
    limiter.commit("timetable", 1)

    result = await limiter.login(_factory("client"))

    assert result == "client"
    assert sleeper.slept == [pytest.approx(1.0)]


async def test_a_login_with_nothing_to_wait_for_does_not_sleep(
    clock: FakeClock, sleeper: RecordingSleeper
) -> None:
    """A full bucket and no spacing debt means the attempt goes straight out."""
    limiter = build(clock, sleeper, min_request_interval=0.0)

    assert await limiter.login(_factory("client")) == "client"
    assert sleeper.slept == []


async def test_the_default_sleeper_is_asyncio_sleep() -> None:
    """The seam has a real default, and it is exercised rather than assumed."""
    await _asyncio_sleep(0)


async def test_a_limiter_built_without_a_sleeper_still_sleeps(
    clock: FakeClock,
) -> None:
    """Constructing without the seam falls back to ``asyncio.sleep``."""
    limiter = RateLimiter(
        RateLimitConfig(min_request_interval=0.0),
        clock=clock.monotonic,
        now=clock.now,
    )
    assert await limiter.call("timetable", Priority.HIGH, _factory()) == "done"


def test_a_limiter_built_without_an_rng_still_jitters(clock: FakeClock) -> None:
    """Same for the RNG seam."""
    limiter = RateLimiter(
        RateLimitConfig(),
        clock=clock.monotonic,
        now=clock.now,
    )
    limiter.note_failure()
    assert limiter.backoff_delay() > 0


# ---------------------------------------------------------------------------
# The diagnostic surface
# ---------------------------------------------------------------------------


def test_the_counters_are_json_and_carry_no_secret(clock: FakeClock) -> None:
    """They go straight into a service response and a diagnostics download.

    So the shape is checked here rather than trusted: every value is a scalar
    or a flat mapping of scalars, and nothing in it could be a credential.
    """
    limiter = build(clock)
    limiter.commit("timetable", 2)
    limiter.note_login(LoginOutcome.SUCCESS)

    counters = limiter.snapshot_counters()

    assert counters["calls_today"] == 2 + REQUESTS_PER_LOGIN
    assert counters["calls_by_tier"] == {
        "timetable": 2,
        # Under `login`, not under `session`. The session tier is the one tier
        # that costs nothing, so booking the login's requests there made it
        # read as the most expensive thing the integration does -- on the very
        # attribute the diagnostics tell a user to read when tuning the budget.
        LOGIN_COST_KEY: REQUESTS_PER_LOGIN,
    }
    assert counters["logins_today"] == 1
    assert counters["state"] == str(LimiterState.NOMINAL)
    assert counters["reason"] is None
    for value in counters.values():
        assert isinstance(value, (int, float, str, bool, dict, type(None)))


def test_remaining_today_never_goes_negative(clock: FakeClock) -> None:
    """A number a dashboard divides by must not be below zero."""
    limiter = build(clock, max_requests_per_day=5)
    limiter.commit("timetable", 50)
    counters = limiter.snapshot_counters()
    assert counters["remaining_today"] == 0
    assert counters["hourly_remaining"] >= 0


def test_the_configuration_is_readable_back(clock: FakeClock) -> None:
    """The options flow and the estimator both read it off the limiter."""
    limiter = build(clock, max_requests_per_day=1234)
    assert limiter.config.max_requests_per_day == 1234


def test_a_login_costs_more_in_token_mode_than_the_floor() -> None:
    """Five is the floor, seven the worst case, and the estimator uses seven.

    An estimate a user can be disappointed by is worse than no estimate, and
    QR enrolment -- the mode this integration recommends -- is the expensive
    one.
    """
    assert REQUESTS_PER_LOGIN < REQUESTS_PER_LOGIN_WORST_CASE


def test_a_naive_wall_clock_is_still_usable() -> None:
    """The limiter is given UTC by the integration but must not require it.

    ``dt_util.now()`` is zone-aware, and every caller in this project passes
    one; the arithmetic here nevertheless has to work for a plain UTC clock,
    because that is what the tests and the estimator use.
    """
    clock = FakeClock(datetime(2026, 3, 12, 8, 0, tzinfo=UTC))
    limiter = build(clock)
    assert limiter.check("timetable", Priority.HIGH).allowed


# ---------------------------------------------------------------------------
# Charging a login at admission, and reconciling it afterwards
# ---------------------------------------------------------------------------


async def test_a_login_is_charged_before_the_handshake_returns() -> None:
    """The window this closes is a real one, and it is seconds wide.

    A login is five to seven requests and a PRONOTE handshake is slow. Charged
    on the way out, the bucket, the spacing stamp and ``logins_today`` all stay
    untouched for the whole duration of the handshake, so a second caller
    arriving inside that window reads a full budget and is admitted -- which is
    how a login cap of five lets ten logins through.

    The assertion is therefore made *inside* the callable, which is the only
    place from which the window is observable.
    """
    clock = FakeClock(datetime(2026, 3, 12, 8, 0, tzinfo=UTC))
    limiter = build(clock, RecordingSleeper(clock))
    seen: dict[str, int] = {}

    async def handshake() -> str:
        seen["calls"] = limiter.calls_today
        seen["logins"] = limiter.logins_today
        return "ok"

    assert await limiter.login(handshake) == "ok"
    assert seen == {"calls": REQUESTS_PER_LOGIN, "logins": 1}


async def test_the_login_charge_is_booked_to_the_login_key() -> None:
    """Not to the session tier, which is free.

    ``REQUESTS_PER_BATCH`` prices the session tier at zero because a batch that
    reuses a live session performs no request. Booking the five requests of an
    actual login there produced a per-tier breakdown in which the one line
    whose documented cost is nothing carried the largest figure in the table.
    """
    clock = FakeClock(datetime(2026, 3, 12, 8, 0, tzinfo=UTC))
    limiter = build(clock, RecordingSleeper(clock))

    async def handshake() -> None:
        return None

    await limiter.login(handshake)
    assert limiter.snapshot_counters()["calls_by_tier"] == {
        LOGIN_COST_KEY: REQUESTS_PER_LOGIN
    }


async def test_reporting_the_outcome_does_not_charge_the_login_twice() -> None:
    """``note_login`` reconciles; it does not re-bill.

    Every caller in this project calls ``login()`` and then ``note_login()``,
    so double-charging here would inflate every counter the user sees by a
    factor of two and shed tiers against a budget that was never spent.
    """
    clock = FakeClock(datetime(2026, 3, 12, 8, 0, tzinfo=UTC))
    limiter = build(clock, RecordingSleeper(clock))

    async def handshake() -> None:
        return None

    await limiter.login(handshake)
    limiter.note_login(LoginOutcome.SUCCESS)
    assert limiter.calls_today == REQUESTS_PER_LOGIN
    assert limiter.logins_today == 1


async def test_a_login_that_cost_more_than_budgeted_is_topped_up() -> None:
    """QR enrolment performs two complete logins, and says so afterwards.

    The extra requests really did reach the school, so the difference is
    charged rather than discarded -- otherwise the mode this integration
    recommends is the one whose traffic it under-counts.
    """
    clock = FakeClock(datetime(2026, 3, 12, 8, 0, tzinfo=UTC))
    limiter = build(clock, RecordingSleeper(clock))

    async def handshake() -> None:
        return None

    await limiter.login(handshake)
    limiter.note_login(LoginOutcome.SUCCESS, requests_used=REQUESTS_PER_LOGIN * 2)
    assert limiter.calls_today == REQUESTS_PER_LOGIN * 2
    # Still one login, though: the cap counts handshakes, not requests.
    assert limiter.logins_today == 1


async def test_a_login_that_cost_less_is_not_refunded() -> None:
    """Under-spending is not a credit.

    Refunding would let a caller that reports a small figure claw back budget
    the limiter has already promised elsewhere, and the honest reading of "we
    charged five and used four" is that four requests and one wasted unit of
    budget both happened.
    """
    clock = FakeClock(datetime(2026, 3, 12, 8, 0, tzinfo=UTC))
    limiter = build(clock, RecordingSleeper(clock))

    async def handshake() -> None:
        return None

    await limiter.login(handshake)
    limiter.note_login(LoginOutcome.SUCCESS, requests_used=1)
    assert limiter.calls_today == REQUESTS_PER_LOGIN


def test_an_outcome_reported_without_an_admission_is_charged_in_full() -> None:
    """The honest accounting for a login that bypassed the gate.

    Nothing in this integration does that -- and if something ever starts, the
    counters must reflect its traffic rather than silently zero it.
    """
    clock = FakeClock(datetime(2026, 3, 12, 8, 0, tzinfo=UTC))
    limiter = build(clock)

    limiter.note_login(LoginOutcome.SUCCESS)
    assert limiter.calls_today == REQUESTS_PER_LOGIN
    assert limiter.logins_today == 1


# ---------------------------------------------------------------------------
# The handover between two limiter instances
# ---------------------------------------------------------------------------


def test_the_hold_survives_the_set_up_retry_that_would_otherwise_erase_it() -> None:
    """The defect this pair of methods exists for.

    Home Assistant answers ``ConfigEntryNotReady`` with a backoff capped at
    eighty seconds. A limiter rebuilt on each attempt forgets the hour-long
    credentials hold it had just opened, so the hold -- whose entire purpose is
    to stop that loop -- bounded nothing across attempts, and a wrong password
    produced a login attempt every eighty seconds for as long as the entry
    stayed unloaded. That is the shape PRONOTE sanctions by address.
    """
    clock = FakeClock(datetime(2026, 3, 12, 8, 0, tzinfo=UTC))
    first = build(clock)
    for _ in range(3):
        first.note_login(LoginOutcome.BAD_CREDENTIALS)
    assert not first.may_login().allowed

    second = build(clock)
    assert second.may_login().allowed, "a fresh limiter starts unencumbered"

    second.import_state(first.export_state())
    verdict = second.may_login()
    assert not verdict.allowed
    assert verdict.reason is DeferReason.CREDENTIALS_HOLD


def test_the_handover_carries_the_day_counters_within_the_same_day() -> None:
    """Otherwise a reload is a way to buy a fresh daily budget."""
    clock = FakeClock(datetime(2026, 3, 12, 8, 0, tzinfo=UTC))
    first = build(clock)
    first.commit("timetable", 40)

    second = build(clock)
    second.import_state(first.export_state())
    assert second.calls_today == 40
    assert second.snapshot_counters()["calls_by_tier"]["timetable"] == 40


def test_a_handover_from_yesterday_contributes_only_its_punitive_half() -> None:
    """Midnight resets the budget. It does not forgive a hold.

    The two halves are deliberately asymmetric: the daily cap is a promise
    about a calendar day and expires with it, whereas a hold is a promise not
    to hammer a server that just refused us, and midnight is not evidence that
    anything changed.
    """
    clock = FakeClock(datetime(2026, 3, 12, 23, 59, tzinfo=UTC))
    first = build(clock)
    first.commit("timetable", 40)
    for _ in range(3):
        first.note_login(LoginOutcome.BAD_CREDENTIALS)
    exported = first.export_state()

    clock.advance(120)  # Past midnight.
    second = build(clock)
    second.import_state(exported)

    assert second.calls_today == 0, "yesterday's budget does not follow us"
    assert not second.may_login().allowed, "yesterday's hold does"


def test_an_expired_hold_is_not_reinstated_by_the_handover() -> None:
    """A hold that has already run out must not come back to life."""
    clock = FakeClock(datetime(2026, 3, 12, 8, 0, tzinfo=UTC))
    first = build(clock)
    for _ in range(3):
        first.note_login(LoginOutcome.BAD_CREDENTIALS)
    exported = first.export_state()

    clock.advance(first.config.credentials_hold + 60)
    second = build(clock)
    second.import_state(exported)
    assert second.may_login().allowed


def test_an_empty_handover_leaves_a_fresh_limiter_alone() -> None:
    """The first set-up of an entry has nothing to import, and says so."""
    clock = FakeClock(datetime(2026, 3, 12, 8, 0, tzinfo=UTC))
    limiter = build(clock)
    limiter.import_state({})
    assert limiter.calls_today == 0
    assert limiter.may_login().allowed


def test_a_malformed_handover_is_ignored_rather_than_raising() -> None:
    """Nothing in ``import_state`` may stop an entry from setting up.

    The worst case of ignoring a corrupt handover is one extra login. The worst
    case of raising is an integration that cannot be loaded at all, and whose
    repair requires editing ``.storage`` by hand.
    """
    clock = FakeClock(datetime(2026, 3, 12, 8, 0, tzinfo=UTC))
    limiter = build(clock)

    limiter.import_state(
        {
            "day": clock.now().date().isoformat(),
            "tokens": "not a number",
            "last_call": None,
            "failed_logins": ["nonsense", True],
            "hold_until": True,
            "hold_reason": "no such reason",
        }
    )

    # ``True`` is excluded on purpose: ``bool`` is a subclass of ``int``, so a
    # stray ``True`` arriving where a monotonic timestamp belongs would install
    # a hold expiring one second after the process started.
    assert limiter.failed_logins_last_hour == 0
    assert limiter.may_login().allowed


def test_a_true_is_not_a_timestamp() -> None:
    """The narrow claim ``_is_number`` makes, made directly.

    Reached through ``import_state`` above as well, but stated on its own
    because the reason is a Python subtlety rather than a property of this
    integration.
    """
    # Bound rather than passed inline, because `bool` is a subclass of
    # `int` and that is the whole subtlety being asserted.
    a_bool: object = True
    assert _is_number(1)
    assert _is_number(1.5)
    assert not _is_number(a_bool)
    assert not _is_number(None)
    assert not _is_number("1")
