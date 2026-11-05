"""The rate limiter: three layers for calls, two counters for logins.

PRONOTE publishes no rate limit. It applies sanctions, and they do not punish
the same gesture (annexe B §1):

============================ ===================================== ============
Sanction                     Trigger                               Severity
============================ ===================================== ============
Broken session               two concurrent calls desynchronise the immediate,
                             encrypted request counter              recoverable
``Erreur.G = 8`` or ``10``    session expired through inactivity     benign
``Erreur.G = 25``            too many *authorization* requests      wait; do not
                                                                   insist
Address suspension           repeated failed logins                 costly, and
                                                                   undocumented
============================ ===================================== ============

Only the last one really hurts, and it does not target calls -- it targets
*failed logins*. A limiter counting only requests would miss its target, which
is why authentication has its own accounting here.

Five properties are load-bearing, and each one exists because its absence was a
real defect found by review rather than a hypothetical:

* **admission, not retrospection.** Spacing and the token bucket are charged
  *before* the request leaves, inside an admission lock. Charged afterwards,
  two callers -- a scheduled batch and a service invoked during it -- read the
  same ``_last_call``, computed the same wait, slept it together and fired in
  the same millisecond: the desynchronised request counter of §1's first row,
  produced by the very layer meant to prevent it.
* **authentication is subject to the layers too.** A login costs five to seven
  HTTP requests, so it goes through the same admission as any call. It is only
  exempt from *shedding*, never from spacing, from the bucket, or from the
  daily counter.
* **``CRITICAL`` is genuinely unsheddable.** Its threshold is infinite, not a
  large number. ``calls_today`` has no ceiling, so any finite threshold is
  reachable -- a user who sets ``max_requests_per_day`` to its documented floor
  of 100 crosses 2.0 on the first day -- and reaching it refuses the login that
  every other tier depends on.
* **the cost asked of the bucket is the cost charged.** Asking for one token
  while charging six let ``history`` drain four times the burst capacity per
  hour with no wait, which removes exactly the smoothing §2.2 exists for.
* **every login attempt counts, successful or not.** Counting only successes
  left a failing account attempting without bound while the counter meant to
  stop it stayed at zero.

This module knows neither PRONOTE nor Home Assistant. It takes injectable
clocks and returns decisions, which is what makes it testable to 100 % with no
network and no instance (annexe B §6).
"""

import asyncio
from dataclasses import dataclass, field
from datetime import (
    UTC,
    date as date_type,
    datetime,
    time,
    timedelta,
    tzinfo,
)
from enum import StrEnum, auto
import logging
import random
from typing import TYPE_CHECKING, Any, Final, TypeGuard, TypeVar

from .const import (
    DEFAULT_BACKOFF_BASE,
    DEFAULT_BACKOFF_MAX,
    DEFAULT_BOOTSTRAP_HOLD,
    DEFAULT_BURST_SIZE,
    DEFAULT_CREDENTIALS_HOLD,
    DEFAULT_MAX_FAILED_LOGINS_PER_HOUR,
    DEFAULT_MAX_LOGINS_PER_DAY,
    DEFAULT_MAX_REQUESTS_PER_DAY,
    DEFAULT_MAX_REQUESTS_PER_HOUR,
    DEFAULT_MAX_WAIT,
    DEFAULT_MIN_REQUEST_INTERVAL,
    DEFAULT_QUIET_END,
    DEFAULT_QUIET_HOURS_ENABLED,
    DEFAULT_QUIET_START,
    QUIET_HOURS_EXEMPT,
    LimiterState,
    Priority,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

_LOGGER: Final = logging.getLogger(__name__)

T = TypeVar("T")

#: Fraction of the daily cap at which each priority stops being served
#: (annexe B §2.4). The order of sacrifice is decided, not arbitrary.
_SHED_THRESHOLD: Final[dict[Priority, float]] = {
    Priority.CRITICAL: float("inf"),  # never shed -- see the module docstring
    Priority.GESTURE: 1.0,  # quiet hours differ, the budget does not
    Priority.HIGH: 1.0,
    Priority.NORMAL: 0.8,
    Priority.LOW: 0.6,
}

#: The first threshold any priority can hit. Below it nothing is being shed, so
#: the throttled flag may legitimately clear.
LOWEST_SHED_THRESHOLD: Final = min(
    threshold
    for priority, threshold in _SHED_THRESHOLD.items()
    if priority is not Priority.CRITICAL
)

#: Fraction of the daily cap at which an informative repair issue opens.
CAP_WARNING_FRACTION: Final = 0.8

#: A successful login costs one bootstrap GET plus four POSTs. The counter is on
#: HTTP *requests*, bootstrap GET included, so five is the floor (annexe B §8).
REQUESTS_PER_LOGIN: Final = 5

#: Key a login's requests are booked under in ``calls_by_tier``.
#:
#: Its own key, and deliberately not ``str(Tier.SESSION)``. The session *tier*
#: costs nothing -- periods, class and establishment come free with the login
#: (annexe A §2) -- so booking the login's five to seven requests there made the
#: one free tier read as the most expensive thing the integration does, on a
#: diagnostic attribute users are told to look at when tuning the budget. It
#: also collided with a real tier name, so a session tier that ever did place a
#: request would have been indistinguishable from the login that preceded it.
#:
#: Not a tier, so it is never a shedding candidate and never appears in
#: `TIER_PRIORITY`: a login is admitted by `may_login`, which has no priority
#: argument.
LOGIN_COST_KEY: Final = "login"

#: What a login costs in token mode, where one or two
#: ``SecurisationCompteDoubleAuth`` requests ride along (annexe B §1.2). The
#: *estimator* uses this figure, because an estimate a user can be disappointed
#: by is worse than no estimate.
REQUESTS_PER_LOGIN_WORST_CASE: Final = 7

#: How long after a batch begins its remaining tiers may still be served once
#: quiet hours start. Annexe B §8 requires entering quiet hours mid-batch to let
#: that batch finish and to postpone only the *next* one; without a bound, a
#: hung batch would hold that exemption open all night.
BATCH_GRACE_SECONDS: Final = 300.0

SECONDS_PER_HOUR: Final = 3600.0
SECONDS_PER_DAY: Final = 86400.0

#: Guard on the exponent so a pathological failure count cannot raise
#: ``OverflowError`` inside the backoff. 2**40 already dwarfs any ``backoff_max``.
_MAX_BACKOFF_EXPONENT: Final = 40

#: Bound on the day-by-day walk in :meth:`RateLimiter.quiet_seconds_between`. A
#: staleness question never spans a year; a runaway loop would.
_MAX_QUIET_WALK_DAYS: Final = 400


def _is_number(value: Any) -> TypeGuard[float]:
    """Whether a value handed back by :meth:`RateLimiter.export_state` is one.

    ``bool`` is excluded deliberately: it is a subclass of ``int``, and a
    ``True`` arriving where a monotonic timestamp belongs would install a hold
    expiring one second after the process started.
    """
    return isinstance(value, (int, float)) and not isinstance(value, bool)


async def _asyncio_sleep(seconds: float) -> None:
    """Default sleeper. Replaced in tests by a simulated clock advance."""
    await asyncio.sleep(seconds)


class LoginOutcome(StrEnum):
    """How a login attempt ended, classified on the *right* exception.

    This is the single most important thing to get right in this module, and
    specification v1 did not name the exception.

    A wrong password does **not** raise ``PronoteAPIError``: the challenge
    decryption fails and ``_login`` raises ``CryptoError``, or ``_login``
    returns ``False`` when the ``cle`` key is missing from the
    ``Authentification`` response. Hooked to the wrong exception, the counter
    meant to protect the IP address would never increment -- the guard rail
    would exist in the documentation and not in the running code (annexe B
    §3.1).
    """

    SUCCESS = auto()
    #: ``CryptoError`` or ``logged_in is False``. Counts against the IP guard.
    BAD_CREDENTIALS = auto()
    #: HTTP or protocol failure. Goes to the backoff, **not** to the guard.
    TRANSPORT = auto()
    #: The bootstrap page carried no ``Start({…})`` block. Cause unknown.
    BOOTSTRAP = auto()
    #: PRONOTE asked for the 2FA PIN, which we deliberately do not store.
    MFA_REQUIRED = auto()
    #: The response could not be decoded at all -- a protocol change, or an
    #: establishment publishing something upstream cannot parse. Neither the
    #: credentials' fault nor a transport failure, but it still costs requests.
    UNDECODABLE = auto()


class DeferReason(StrEnum):
    """Why a tier was postponed rather than served."""

    HOURLY_BUDGET = "hourly_budget"
    DAILY_CAP = "daily_cap"
    QUIET_HOURS = "quiet_hours"
    BACKOFF = "backoff"
    CREDENTIALS_HOLD = "credentials_hold"
    BOOTSTRAP_HOLD = "bootstrap_hold"
    LOGIN_CAP = "login_cap"
    MFA_HOLD = "mfa_hold"


#: How severe each hold is. A hold is never *replaced* by a less severe one:
#: without this ordering a single transport error downgraded an hour-long
#: bootstrap hold to a thirty-second backoff, and the next success cleared it --
#: so the hold annexe B §3.4 requires simply evaporated.
_HOLD_SEVERITY: Final[dict[DeferReason, int]] = {
    DeferReason.BACKOFF: 1,
    DeferReason.MFA_HOLD: 2,
    DeferReason.BOOTSTRAP_HOLD: 3,
    DeferReason.CREDENTIALS_HOLD: 4,
}


class TierDeferred(Exception):  # noqa: N818 -- a control-flow signal, not an error
    """A tier could not run now and its due date has been pushed back.

    Not a failure: a deferred tier is never abandoned, its deadline moves and it
    keeps its previous snapshot, so its entities keep their value (annexe B
    §2.4). Raised rather than returned so no caller can forget to check.
    """

    def __init__(self, reason: DeferReason, retry_after: float) -> None:
        super().__init__(f"tier deferred: {reason}")
        self.reason = reason
        self.retry_after = retry_after


class LoginRefusedByLimiter(Exception):  # noqa: N818 -- a decision, not a failure
    """A login was refused by the limiter, not by the server.

    Distinct from :class:`TierDeferred` so it cannot be mistaken for a server
    problem: nothing was sent, no credential was tried, and retrying sooner
    would make it worse.
    """

    def __init__(self, reason: DeferReason, retry_after: float) -> None:
        super().__init__(f"login refused: {reason}")
        self.reason = reason
        self.retry_after = retry_after


@dataclass(frozen=True, slots=True)
class RateLimitConfig:
    """Every tunable of annexe B §7, with its verified default."""

    min_request_interval: float = DEFAULT_MIN_REQUEST_INTERVAL
    max_requests_per_hour: int = DEFAULT_MAX_REQUESTS_PER_HOUR
    burst_size: int = DEFAULT_BURST_SIZE
    max_requests_per_day: int = DEFAULT_MAX_REQUESTS_PER_DAY
    max_wait: float = DEFAULT_MAX_WAIT
    max_logins_per_day: int = DEFAULT_MAX_LOGINS_PER_DAY
    max_failed_logins_per_hour: int = DEFAULT_MAX_FAILED_LOGINS_PER_HOUR
    credentials_hold: float = DEFAULT_CREDENTIALS_HOLD
    bootstrap_hold: float = DEFAULT_BOOTSTRAP_HOLD
    backoff_base: float = DEFAULT_BACKOFF_BASE
    backoff_max: float = DEFAULT_BACKOFF_MAX
    quiet_hours_enabled: bool = DEFAULT_QUIET_HOURS_ENABLED
    quiet_start: time = field(
        default_factory=lambda: time.fromisoformat(DEFAULT_QUIET_START)
    )
    quiet_end: time = field(
        default_factory=lambda: time.fromisoformat(DEFAULT_QUIET_END)
    )


@dataclass(frozen=True, slots=True)
class Verdict:
    """A pure decision: may this tier call, and if not, when to retry."""

    allowed: bool
    wait: float = 0.0
    reason: DeferReason | None = None


class RateLimiter:
    """Counts requests and logins separately, and refuses politely.

    Two clocks are injected. ``clock`` is a monotonic float used for spacing,
    refill and holds; ``now`` yields a timezone-aware wall-clock datetime, which
    is what quiet hours and the midnight rollover need. Separating them is
    deliberate: a monotonic source cannot tell you it is 22:00, and a wall clock
    can jump.

    ``rng`` and ``sleep`` are seams too, resolved here rather than at each call
    site so a test constructs one limiter with a frozen generator and a
    simulated sleeper and every path is then reachable without patching a
    module global.
    """

    def __init__(
        self,
        config: RateLimitConfig,
        clock: Callable[[], float],
        now: Callable[[], datetime],
        *,
        rng: random.Random | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._config = config
        self._clock = clock
        self._now = now
        self._rng: random.Random = (
            rng if rng is not None else random.Random()  # noqa: S311 -- jitter
        )
        self._sleep = sleep if sleep is not None else _asyncio_sleep

        # Layer 1 -- spacing.
        self._last_call: float = clock() - config.min_request_interval

        # Layer 2 -- token bucket, starting full so the first batch is not
        # penalised for the integration having just started.
        self._tokens: float = float(config.burst_size)
        self._refilled: float = clock()

        # Layer 3 -- daily cap.
        self._day: date_type = self._now().date()
        self._calls_today = 0
        self._calls_by_tier: dict[str, int] = {}

        # Authentication accounting, kept apart on purpose.
        self._logins_today = 0
        self._failed_logins: list[float] = []
        #: What the login admitted under the lock was charged, until the caller
        #: reports its outcome. See :meth:`note_login`.
        self._login_charged: int | None = None

        # Backoff, holds and the reason a tier is currently being postponed.
        self._consecutive_failures = 0
        self._hold_until: float | None = None
        self._hold_reason: DeferReason | None = None
        self._shed_reason: DeferReason | None = None
        self._throttled_since: datetime | None = None
        self._cap_warning_open = False

        # A batch begun outside quiet hours may finish inside them.
        self._batch_started: float | None = None
        self._batch_started_in_quiet = False

        # Admission is serialised so layer 1 is a guarantee, not a hope.
        self._admission = asyncio.Lock()

    # -- properties --------------------------------------------------------

    @property
    def config(self) -> RateLimitConfig:
        """The configuration in force."""
        return self._config

    @property
    def calls_today(self) -> int:
        """HTTP requests since local midnight, bootstrap GETs included."""
        self._roll_day()
        return self._calls_today

    @property
    def calls_by_tier(self) -> dict[str, int]:
        """Request counts broken down by tier, for the diagnostic entity."""
        self._roll_day()
        return dict(self._calls_by_tier)

    @property
    def logins_today(self) -> int:
        """Login *attempts* since local midnight.

        Attempts, not successes. The cap bounds how much authentication traffic
        this integration generates, and a failed attempt costs the server
        exactly as much as a successful one.
        """
        self._roll_day()
        return self._logins_today

    @property
    def failed_logins_last_hour(self) -> int:
        """Failed logins in the trailing hour -- the IP guard's counter."""
        self._prune_failed_logins()
        return len(self._failed_logins)

    @property
    def consecutive_failures(self) -> int:
        """Backoff depth."""
        return self._consecutive_failures

    @property
    def tokens(self) -> float:
        """Tokens in the bucket. Negative while an overdraft is being repaid."""
        self._refill()
        return self._tokens

    @property
    def throttled(self) -> bool:
        """True while collections are being postponed."""
        return self._throttled_since is not None

    @property
    def throttled_since(self) -> datetime | None:
        """When throttling began, for ``binary_sensor.<account>_bride``."""
        return self._throttled_since

    @property
    def cap_warning_open(self) -> bool:
        """True once the daily cap crossed its warning fraction."""
        return self._cap_warning_open

    @property
    def hold_until_wallclock(self) -> datetime | None:
        """End of the current hold as a wall-clock instant, for the UI."""
        if self._hold_until is None:
            return None
        return self._now() + timedelta(seconds=self._hold_remaining())

    @property
    def state(self) -> LimiterState:
        """The closed value set of ``sensor.<account>_etat_limiteur``.

        There is no ``ip_suspended``: an unusable bootstrap yields
        ``BOOTSTRAP_FAILED`` and the repair text names no cause, because
        ``if "IP" in html`` cannot establish one (annexe B §3.4).
        """
        if self._hold_active():
            reason = self._hold_reason
            if reason in (DeferReason.CREDENTIALS_HOLD, DeferReason.MFA_HOLD):
                # Both mean the same thing to a user -- "we have stopped trying
                # and we need you" -- and annexe A §7 fixes the value set, so
                # no state is invented for the MFA case.
                return LimiterState.CREDENTIALS_HOLD
            if reason is DeferReason.BOOTSTRAP_HOLD:
                return LimiterState.BOOTSTRAP_FAILED
            return LimiterState.BACKOFF
        if self._in_quiet_hours():
            return LimiterState.QUIET_HOURS
        if self.throttled:
            return LimiterState.THROTTLED
        return LimiterState.NOMINAL

    # -- layer 3: the day boundary ----------------------------------------

    def _roll_day(self) -> None:
        """Reset the daily counters when the local date moves *forward*.

        Only forward. Resetting on any date change re-granted the whole day's
        budget *and* the login cap whenever the host's clock stepped backwards
        -- an NTP correction, or a machine whose clock was wrong at boot. Going
        backwards adopts the new date without clearing anything.

        The bucket is deliberately left alone either way: it smooths the hour
        and has no opinion about the date (annexe B §8).
        """
        today = self._now().date()
        if today == self._day:
            return
        if today < self._day:
            _LOGGER.debug(
                "wall clock moved backwards (%s -> %s); keeping today's counters",
                self._day,
                today,
            )
            self._day = today
            return
        self._day = today
        self._calls_today = 0
        self._calls_by_tier = {}
        self._logins_today = 0
        self._cap_warning_open = False

    def _cap_fraction(self) -> float:
        """How much of the daily cap is spent, as a fraction."""
        self._roll_day()
        if self._config.max_requests_per_day <= 0:
            return 1.0
        return self._calls_today / self._config.max_requests_per_day

    # -- layer 2: the token bucket -----------------------------------------

    def _refill(self) -> None:
        """Continuous refill at ``max_requests_per_hour / 3600`` per second."""
        now = self._clock()
        rate = self._config.max_requests_per_hour / SECONDS_PER_HOUR
        self._tokens = min(
            float(self._config.burst_size),
            self._tokens + (now - self._refilled) * rate,
        )
        self._refilled = now

    def _token_wait(self, count: float = 1.0) -> float:
        """Seconds until ``count`` tokens are available."""
        self._refill()
        if self._tokens >= count:
            return 0.0
        rate = self._config.max_requests_per_hour / SECONDS_PER_HOUR
        if rate <= 0:
            return float("inf")
        return (count - self._tokens) / rate

    # -- layer 1: spacing ---------------------------------------------------

    def _spacing_wait(self) -> float:
        """Seconds until the minimum inter-request interval has elapsed."""
        elapsed = self._clock() - self._last_call
        return max(0.0, self._config.min_request_interval - elapsed)

    # -- quiet hours --------------------------------------------------------

    def _in_quiet_hours(self, at: datetime | None = None) -> bool:
        """True outside the ``[quiet_end, quiet_start[`` active window."""
        if not self._config.quiet_hours_enabled:
            return False
        moment = (at if at is not None else self._now()).timetz().replace(tzinfo=None)
        start = self._config.quiet_start
        end = self._config.quiet_end
        if start == end:
            return False
        if start > end:
            # The usual case: quiet from 22:00 through 06:00, across midnight.
            return moment >= start or moment < end
        return start <= moment < end

    def quiet_duration_seconds(self) -> float:
        """Length of one quiet window, or zero when quiet hours are off."""
        if not self._config.quiet_hours_enabled:
            return 0.0
        start = self._config.quiet_start
        end = self._config.quiet_end
        if start == end:
            return 0.0
        start_seconds = start.hour * 3600 + start.minute * 60 + start.second
        end_seconds = end.hour * 3600 + end.minute * 60 + end.second
        if start > end:
            return SECONDS_PER_DAY - start_seconds + end_seconds
        return float(end_seconds - start_seconds)

    def quiet_seconds_between(self, start: datetime, end: datetime) -> float:
        """How much of ``[start, end)`` fell inside quiet hours.

        Exists so staleness can exclude the night. With quiet hours on -- the
        default -- an eight-hour pause is longer than ``stale_after`` times the
        interval of every fast tier, so without this the timetable, homework,
        news and discussion entities all went ``unavailable`` at 06:00 every
        morning. An unavailable entity fires automations spuriously (§2.5),
        which makes that the worst available way to say "nothing happened
        overnight".
        """
        if not self._config.quiet_hours_enabled or end <= start:
            return 0.0
        if self._config.quiet_start == self._config.quiet_end:
            return 0.0

        total = 0.0
        # Begin a day early, so a window that opened yesterday and closes today
        # is counted.
        day = start.date() - timedelta(days=1)
        last_day = end.date()
        walked = 0
        while day <= last_day and walked <= _MAX_QUIET_WALK_DAYS:
            walked += 1
            window_start, window_end = self._quiet_window(day, start.tzinfo)
            overlap_start = max(window_start, start)
            overlap_end = min(window_end, end)
            if overlap_end > overlap_start:
                total += _elapsed(overlap_start, overlap_end)
            day += timedelta(days=1)
        return total

    def _quiet_window(
        self, day: date_type, zone: tzinfo | None
    ) -> tuple[datetime, datetime]:
        """The quiet interval that *opens* on one calendar day."""
        quiet_start = self._config.quiet_start
        quiet_end = self._config.quiet_end
        opens = datetime.combine(day, quiet_start).replace(tzinfo=zone)
        if quiet_start > quiet_end:
            # Crosses midnight: 22:00 today through 06:00 tomorrow.
            closes = datetime.combine(day + timedelta(days=1), quiet_end).replace(
                tzinfo=zone
            )
        else:
            closes = datetime.combine(day, quiet_end).replace(tzinfo=zone)
        return opens, closes

    # -- batches ------------------------------------------------------------

    def begin_batch(self) -> None:
        """Mark the start of one collection batch.

        Annexe B §8: entering quiet hours *during* a batch lets that batch
        finish and postpones the next one. Without a batch boundary the
        remaining tiers of a batch straddling 22:00 were refused halfway
        through, which is neither of the two documented behaviours.
        """
        self._batch_started = self._clock()
        self._batch_started_in_quiet = self._in_quiet_hours()

    def end_batch(self) -> None:
        """Mark the end of one collection batch."""
        self._batch_started = None
        self._batch_started_in_quiet = False

    def _batch_may_finish(self) -> bool:
        """Whether a batch begun outside quiet hours may still be served."""
        if self._batch_started is None or self._batch_started_in_quiet:
            return False
        return (self._clock() - self._batch_started) <= BATCH_GRACE_SECONDS

    # -- holds and backoff --------------------------------------------------

    def _hold_active(self) -> bool:
        """True while a hold forbids every call."""
        if self._hold_until is None:
            return False
        if self._clock() >= self._hold_until:
            self._hold_until = None
            self._hold_reason = None
            return False
        return True

    def _hold_remaining(self) -> float:
        """Seconds left on the current hold, or zero."""
        if self._hold_until is None:
            return 0.0
        return max(0.0, self._hold_until - self._clock())

    def _start_hold(self, reason: DeferReason, seconds: float) -> None:
        """Refuse every call for ``seconds``, never downgrading a worse hold."""
        if self._hold_active() and _HOLD_SEVERITY.get(reason, 0) < _HOLD_SEVERITY.get(
            self._hold_reason or reason, 0
        ):
            # A transport error must not shorten a credentials, MFA or
            # bootstrap hold. Extend the existing one at most.
            self._hold_until = max(self._hold_until or 0.0, self._clock() + seconds)
            self._mark_throttled()
            return
        self._hold_until = self._clock() + seconds
        self._hold_reason = reason
        self._mark_throttled()

    def _mark_throttled(self) -> None:
        """Record that collections started being postponed."""
        if self._throttled_since is None:
            self._throttled_since = self._now()

    def _maybe_clear_throttled(self) -> None:
        """Clear the throttled flag only if nothing is being postponed.

        Clearing it unconditionally on every success made the daily cap silent:
        one successful ``HIGH`` call reset the flag while ``LOW`` and ``NORMAL``
        tiers were still being dropped, so ``binary_sensor.<account>_bride``
        flapped back to ``off`` every quarter of an hour. Annexe B §2.3 is
        explicit that the cap is never silent, because data that ages with no
        error in the log is the hardest symptom there is to diagnose.
        """
        if self._hold_active():
            return
        if self._cap_fraction() >= LOWEST_SHED_THRESHOLD:
            return
        if self._logins_today >= self._config.max_logins_per_day:
            return
        self._shed_reason = None
        self._throttled_since = None

    def backoff_delay(self) -> float:
        """``min(backoff_max, base × 2 ** (failures - 1)) × (0.5 + random())``.

        Full jitter, so several Home Assistant instances at the same
        establishment do not resynchronise on the same slot after a server
        outage (annexe B §4).
        """
        if self._consecutive_failures <= 0:
            return 0.0
        # The exponent is clamped before the shift: computing 2**1024 first
        # raises OverflowError, which turned a deep backoff into a crash.
        exponent = min(self._consecutive_failures - 1, _MAX_BACKOFF_EXPONENT)
        nominal = min(
            self._config.backoff_max, self._config.backoff_base * 2.0**exponent
        )
        return nominal * (0.5 + self._rng.random())

    def retry_delay(self) -> float:
        """A **non-zero** delay to postpone a failed tier by.

        ``backoff_delay()`` is zero while there have been no *consecutive*
        failures, which is exactly the case for a refused login: bad
        credentials, a demanded PIN and an unreadable bootstrap all leave that
        counter at zero. Deferring by zero seconds makes the tier due again on
        the very next tick, and ten tiers then attempt ten logins per tick --
        the repeated-failed-login gesture that is the one sanction annexe B §1
        says cannot be worked around.
        """
        delay = self.backoff_delay()
        if delay > 0:
            return delay
        return self._config.backoff_base * (0.5 + self._rng.random())

    # -- authentication accounting -----------------------------------------

    def _prune_failed_logins(self) -> None:
        """Drop failures strictly older than an hour.

        Strictly. Keeping a failure for exactly ``3600`` seconds made a
        ``may_login()`` landing on the instant a hold expired restart that
        hold, doubling it -- and made any test advancing a simulated clock by
        exactly ``credentials_hold`` fail for a reason unrelated to what it
        was testing.
        """
        cutoff = self._clock() - SECONDS_PER_HOUR
        self._failed_logins = [at for at in self._failed_logins if at > cutoff]

    def may_login(self, *, cost: int = REQUESTS_PER_LOGIN) -> Verdict:
        """Whether a login attempt is permitted right now.

        Subject to the same layers as any call, with exactly one exception: a
        login is never *shed* by the daily cap, because refusing to log in
        leaves every entity stale with no way back (§2.4). It is still spaced,
        still drawn from the bucket and still counted -- a login costs five to
        seven HTTP requests, and a budget that did not see them would be
        describing a different integration than the one running.
        """
        if self._hold_active():
            return Verdict(
                allowed=False,
                wait=self._hold_remaining(),
                # `_start_hold` always records a reason, so the fallback is
                # unreachable in practice; folding it in here keeps the
                # function total without leaving a branch nothing can cover.
                reason=self._hold_reason or DeferReason.BACKOFF,
            )

        self._prune_failed_logins()
        if len(self._failed_logins) >= self._config.max_failed_logins_per_hour:
            self._start_hold(
                DeferReason.CREDENTIALS_HOLD, self._config.credentials_hold
            )
            return Verdict(
                allowed=False,
                wait=self._config.credentials_hold,
                reason=DeferReason.CREDENTIALS_HOLD,
            )

        self._roll_day()
        if self._logins_today >= self._config.max_logins_per_day:
            self._mark_throttled()
            self._shed_reason = DeferReason.LOGIN_CAP
            return Verdict(
                allowed=False,
                wait=self._seconds_to_midnight(),
                reason=DeferReason.LOGIN_CAP,
            )

        # Allowed, but possibly not yet: the wait is the caller's to honour.
        return Verdict(
            allowed=True,
            wait=max(self._spacing_wait(), self._token_wait(float(cost))),
        )

    def _seconds_to_midnight(self) -> float:
        """Seconds until the daily counters reset."""
        now = self._now()
        tomorrow = (now + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        return _elapsed(now, tomorrow)

    def _commit_login(self, cost: int) -> None:
        """Charge an admitted login, under :attr:`_admission`."""
        self._record_requests(LOGIN_COST_KEY, cost)
        self._logins_today += 1
        self._login_charged = cost

    def note_login(
        self,
        outcome: LoginOutcome,
        *,
        requests_used: int = REQUESTS_PER_LOGIN,
        counts_against_guard: bool = True,
    ) -> None:
        """Record a login attempt's *outcome*, and reconcile its cost.

        The cost itself was charged at admission by :meth:`login`, so this only
        charges the difference -- which is not nothing: token mode returns a
        rotated bearer and QR enrolment performs two complete logins, so the
        real figure can exceed the five that were budgeted.

        A caller that reports an outcome without having gone through
        :meth:`login` is charged in full here instead. That is not a
        convenience for tests: it is the honest accounting for anything that
        logs in outside the admission gate, and the assertion that nothing does
        belongs in a test rather than in a silent zero.

        ``counts_against_guard`` exists for one specific case: QR enrolment
        performs **two** complete logins by construction -- ``qrcode_login``
        builds a client (whose constructor logs in), posts ``PageInfosPerso``
        49, then calls ``token_login`` with the exported credentials. That
        deliberate doubling must not eat two thirds of a three-attempt guard
        rail (annexe B §3.3).
        """
        self._roll_day()
        charged, self._login_charged = self._login_charged, None
        if charged is None:
            self._record_requests(LOGIN_COST_KEY, requests_used)
            self._logins_today += 1
        elif requests_used > charged:
            self._record_requests(LOGIN_COST_KEY, requests_used - charged)

        if outcome is LoginOutcome.SUCCESS:
            self._consecutive_failures = 0
            self._maybe_clear_throttled()
            return

        if outcome is LoginOutcome.BAD_CREDENTIALS:
            self._note_bad_credentials(counts_against_guard=counts_against_guard)
            return

        if outcome is LoginOutcome.BOOTSTRAP:
            self._start_hold(DeferReason.BOOTSTRAP_HOLD, self._config.bootstrap_hold)
            return

        if outcome is LoginOutcome.MFA_REQUIRED:
            # A hold, not merely a flag. PRONOTE is waiting for a PIN this
            # integration deliberately does not store (§8.1), so no amount of
            # retrying can succeed -- and each retry costs five to seven
            # requests. Left unbounded this produced thousands of login
            # attempts a day, which is the one gesture that gets an address
            # suspended. `reset_after_reauth()` lifts it the moment a human
            # supplies the PIN, which is the only thing that can.
            _LOGGER.warning(
                "PRONOTE is asking for the two-factor PIN; pausing login "
                "attempts until it is supplied through re-authentication"
            )
            self._start_hold(DeferReason.MFA_HOLD, self._config.credentials_hold)
            return

        if outcome is LoginOutcome.UNDECODABLE:
            # Not the credentials' fault, so it must not touch the IP guard;
            # not a transient either, so a bare backoff would retry for ever.
            # The bootstrap hold is the right length for "this establishment's
            # response is currently unusable".
            self._start_hold(DeferReason.BOOTSTRAP_HOLD, self._config.bootstrap_hold)
            return

        # LoginOutcome.TRANSPORT
        self.note_failure()

    def _note_bad_credentials(self, *, counts_against_guard: bool) -> None:
        """Charge one failure to the IP guard, and stop if it is exhausted."""
        if not counts_against_guard:
            return
        self._failed_logins.append(self._clock())
        self._prune_failed_logins()
        if len(self._failed_logins) < self._config.max_failed_logins_per_hour:
            return
        _LOGGER.warning(
            "Stopping login attempts after %d failures in the last hour; "
            "repeatedly retrying a wrong password is what gets an address "
            "suspended",
            len(self._failed_logins),
        )
        self._start_hold(DeferReason.CREDENTIALS_HOLD, self._config.credentials_hold)

    def reset_after_reauth(self) -> None:
        """Clear the failure counters after a *human* re-authentication.

        A deliberate human gesture with possibly corrected credentials is not
        an automatic retry, so it earns a clean slate (annexe B §3.2). This is
        also the only way out of the MFA hold, which is correct: that hold
        exists precisely because a person has to act.
        """
        self._failed_logins = []
        self._consecutive_failures = 0
        self._hold_until = None
        self._hold_reason = None
        self._shed_reason = None
        self._throttled_since = None

    # -- transport and protocol failures ------------------------------------

    def note_failure(self) -> None:
        """Record a transport error or ``Erreur.G = 25`` for the backoff.

        Never used for a bad password: that has its own counter, and conflating
        the two is how the IP guard ends up never firing.
        """
        self._consecutive_failures += 1
        self._start_hold(DeferReason.BACKOFF, self.backoff_delay())

    def note_success(self) -> None:
        """Reset the failure counter -- entirely, on the first success.

        Not gradually. A server that answers once is a server that works
        (annexe B §4). Whether collections are still being postponed is a
        separate question, answered by :meth:`_maybe_clear_throttled`.
        """
        self._consecutive_failures = 0
        if self._hold_reason is DeferReason.BACKOFF:
            self._hold_until = None
            self._hold_reason = None
        self._maybe_clear_throttled()

    # -- the single path to the network -------------------------------------

    def check(self, tier: str, priority: Priority, *, cost: float = 1.0) -> Verdict:
        """Decide, without side effects beyond bookkeeping housekeeping.

        Layers apply in order and a call must clear all three. ``cost`` is the
        number of requests the call will actually place.
        """
        if self._hold_active():
            return Verdict(
                allowed=False,
                wait=self._hold_remaining(),
                reason=self._hold_reason or DeferReason.BACKOFF,
            )

        if (
            priority not in QUIET_HOURS_EXEMPT
            and self._in_quiet_hours()
            and not self._batch_may_finish()
        ):
            return Verdict(
                allowed=False,
                wait=self._seconds_to_quiet_end(),
                reason=DeferReason.QUIET_HOURS,
            )

        fraction = self._cap_fraction()
        if fraction >= CAP_WARNING_FRACTION:
            self._cap_warning_open = True
        if fraction >= _SHED_THRESHOLD[priority]:
            self._mark_throttled()
            self._shed_reason = DeferReason.DAILY_CAP
            return Verdict(
                allowed=False,
                wait=self._seconds_to_midnight(),
                reason=DeferReason.DAILY_CAP,
            )

        wait = max(self._spacing_wait(), self._token_wait(cost))
        if wait > self._config.max_wait:
            # Postponed, not failed: the tier keeps its snapshot and its
            # deadline simply moves (annexe B §2.2).
            self._mark_throttled()
            self._shed_reason = DeferReason.HOURLY_BUDGET
            _LOGGER.debug("tier %s deferred: %.1fs wait exceeds max_wait", tier, wait)
            return Verdict(allowed=False, wait=wait, reason=DeferReason.HOURLY_BUDGET)

        return Verdict(allowed=True, wait=wait)

    def _seconds_to_quiet_end(self) -> float:
        """Seconds until the active window reopens."""
        now = self._now()
        end = self._config.quiet_end
        candidate = now.replace(
            hour=end.hour, minute=end.minute, second=end.second, microsecond=0
        )
        if candidate <= now:
            candidate += timedelta(days=1)
        return _elapsed(now, candidate)

    def _record_requests(self, tier: str, count: int) -> None:
        """Charge ``count`` HTTP requests to ``tier``.

        The bucket is allowed to go **negative**. Clamping the debt at zero
        forgave every overdraft, so a burst of expensive calls cost nothing to
        repay and the hourly rate stopped meaning anything.
        """
        self._roll_day()
        self._calls_today += count
        self._calls_by_tier[tier] = self._calls_by_tier.get(tier, 0) + count
        self._last_call = self._clock()
        self._refill()
        self._tokens -= count

    def commit(self, tier: str, count: int = 1) -> None:
        """Record that ``count`` requests went out for ``tier``.

        Charged at admission rather than on completion, and never refunded: the
        request has left by the time anything can fail, and a limiter that only
        counted successes would let a failing loop run unbounded. It is also
        what makes layers 1 and 2 pre-emptive -- see :meth:`call`.
        """
        self._record_requests(tier, count)

    def reconcile(self, tier: str, *, charged: int, actual: int) -> None:
        """Charge the difference when a call cost more than it declared.

        This is what makes the gateway's ``GatewayResult.calls`` load-bearing
        instead of decorative. Without it the limiter only ever saw the
        *a-priori* cost a caller declared, so an accidental lazy-property
        access -- ``Information.content``, ``Period.grades``,
        ``Discussion.messages`` -- placed real requests that the spacing, the
        bucket and the daily cap all missed, and the truth appeared only in a
        diagnostics field nobody compares.

        A call that cost *less* than declared is not refunded: the declared
        cost already left the bucket, and a limiter that gave budget back would
        let an optimistic declaration become a burst allowance.
        """
        extra = actual - charged
        if extra <= 0:
            return
        _LOGGER.debug(
            "tier %s cost %d requests, %d were budgeted; charging the difference",
            tier,
            actual,
            charged,
        )
        self._record_requests(tier, extra)

    async def call(
        self,
        tier: str,
        priority: Priority,
        fn: Callable[[], Awaitable[T]],
        *,
        cost: int = 1,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> T:
        """The one and only path to the gateway (annexe B §6).

        There is no public gateway function that does not come through here. A
        refresh button gets a raised priority for the next tick -- it does not
        get a dispensation, and it does not short-circuit the spacing.

        The decision, the wait and the charge all happen under
        ``self._admission``, which is what turns layer 1 from an intention into
        a guarantee: see the module docstring.
        """
        sleeper = sleep if sleep is not None else self._sleep

        async with self._admission:
            verdict = self.check(tier, priority, cost=float(cost))
            if not verdict.allowed:
                raise TierDeferred(
                    verdict.reason or DeferReason.HOURLY_BUDGET, verdict.wait
                )
            if verdict.wait > 0:
                await sleeper(verdict.wait)
            self.commit(tier, cost)

        result = await fn()
        self.note_success()
        return result

    async def login(
        self,
        fn: Callable[[], Awaitable[T]],
        *,
        cost: int = REQUESTS_PER_LOGIN,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> T:
        """The one and only path to a login.

        Same admission as :meth:`call`, minus the shedding. The caller still
        reports the outcome through :meth:`note_login`, which is where the
        credentials guard and the login cap live; this method only decides
        whether the attempt may be made at all, and spaces it.
        """
        sleeper = sleep if sleep is not None else self._sleep

        async with self._admission:
            verdict = self.may_login(cost=cost)
            if not verdict.allowed:
                raise LoginRefusedByLimiter(
                    verdict.reason or DeferReason.CREDENTIALS_HOLD, verdict.wait
                )
            if verdict.wait > 0:
                await sleeper(verdict.wait)
            # Charged here, inside the lock and *before* the request goes out,
            # exactly as `call()` does -- and for the same two reasons.
            #
            # Under the lock, because charging afterwards left the spacing
            # stamp, the bucket and `logins_today` untouched for the whole
            # duration of a real handshake: any second caller in that window
            # read a full bucket, a spacing debt of zero and a login count that
            # only the first caller would ever write. The invariant this module
            # exists to provide was being held by `SessionManager._gate`, a
            # lock in a different module, which is not the same promise.
            #
            # Before the request, because a cancellation during the handshake
            # -- an entry unloaded, an options save, a shutdown -- raises
            # `CancelledError` past every `except` in `session.py`, so
            # `note_login` was never reached while the worker thread completed
            # the login anyway: five to seven requests that reached the school
            # and appeared in no counter.
            self._commit_login(cost)

        return await fn()

    def export_state(self) -> dict[str, Any]:
        """Everything that must survive this object, and nothing else.

        Its purpose is the set-up retry: Home Assistant answers
        ``ConfigEntryNotReady`` with a backoff capped at eighty seconds, and a
        limiter rebuilt on every attempt forgets the hour-long hold it had just
        opened -- so the hold, which exists to stop exactly that loop, never
        bounded anything across attempts.

        Monotonic values travel as they are, which is sound because the store
        this feeds lives in ``hass.data`` and dies with the process. The wall
        clock is carried as the *day* only, so importing yesterday's state
        cannot resurrect yesterday's counters.
        """
        self._roll_day()
        self._refill()
        return {
            "day": self._day.isoformat(),
            "calls_today": self._calls_today,
            "calls_by_tier": dict(self._calls_by_tier),
            "logins_today": self._logins_today,
            "failed_logins": list(self._failed_logins),
            "consecutive_failures": self._consecutive_failures,
            "hold_until": self._hold_until,
            "hold_reason": str(self._hold_reason) if self._hold_reason else None,
            "tokens": self._tokens,
            "last_call": self._last_call,
            "refilled": self._refilled,
            "cap_warning_open": self._cap_warning_open,
        }

    def import_state(self, state: Mapping[str, Any]) -> None:
        """Adopt state exported by a previous instance, if it is still valid.

        Tolerant by design: an absent key keeps the fresh default, and a state
        from another day contributes only its punitive half. Nothing here may
        raise -- a malformed handover must not be able to stop an entry from
        setting up, since the worst case of ignoring it is one extra login.
        """
        if not state:
            return

        same_day = str(state.get("day", "")) == self._day.isoformat()
        if same_day:
            self._calls_today = int(state.get("calls_today", 0))
            self._calls_by_tier = {
                str(tier): int(count)
                for tier, count in dict(state.get("calls_by_tier") or {}).items()
            }
            self._logins_today = int(state.get("logins_today", 0))
            self._cap_warning_open = bool(state.get("cap_warning_open", False))
            for field, attribute in (
                ("tokens", "_tokens"),
                ("last_call", "_last_call"),
                ("refilled", "_refilled"),
            ):
                value = state.get(field)
                if _is_number(value):
                    setattr(self, attribute, float(value))

        # The punitive state crosses a day boundary. A hold is a promise not to
        # hammer a server that just refused us, and midnight is not evidence
        # that anything changed.
        self._failed_logins = [
            float(at) for at in state.get("failed_logins") or [] if _is_number(at)
        ]
        self._consecutive_failures = int(state.get("consecutive_failures", 0))
        hold_until = state.get("hold_until")
        if _is_number(hold_until) and hold_until > self._clock():
            self._hold_until = float(hold_until)
            reason = state.get("hold_reason")
            self._hold_reason = next(
                (item for item in DeferReason if str(item) == reason), None
            )

    def snapshot_counters(self) -> dict[str, Any]:
        """Everything the diagnostic entities of annexe A §7 need.

        Typed ``Any`` rather than ``object`` because the mapping is returned
        verbatim by the ``get_rate_limit_status`` service, whose contract is
        ``ServiceResponse`` -- that is, JSON. Every value here is a scalar or a
        flat mapping of scalars, and the test that keeps secrets out of service
        responses is what actually holds that line, not the annotation.
        """
        self._roll_day()
        self._refill()
        reason = self._hold_reason if self._hold_active() else self._shed_reason
        return {
            "calls_today": self._calls_today,
            "calls_by_tier": dict(self._calls_by_tier),
            "logins_today": self._logins_today,
            "failed_logins_hour": self.failed_logins_last_hour,
            "daily_cap": self._config.max_requests_per_day,
            "remaining_today": max(
                0, self._config.max_requests_per_day - self._calls_today
            ),
            "tokens": round(self._tokens, 3),
            "hourly_rate": self._config.max_requests_per_hour,
            "hourly_remaining": round(max(0.0, self._tokens), 3),
            "state": str(self.state),
            # Whether that is a hold or a shed. Annexe B §2.3 requires the
            # daily cap to surface `daily_cap` here; deriving it from the hold
            # alone reported `None` for the one case the requirement is about.
            "reason": str(reason) if reason else None,
            "consecutive_failures": self._consecutive_failures,
            "throttled": self.throttled,
        }


def _elapsed(start: datetime, end: datetime) -> float:
    """Real seconds between two aware datetimes, DST included.

    Both are converted to UTC first. Subtracting two aware datetimes that share
    the *same* ``tzinfo`` object makes CPython skip the offset lookup and return
    the wall-clock difference, so on the March transition "01:30 until 06:00"
    came out as four and a half hours where three and a half actually elapse.
    """
    return (end.astimezone(UTC) - start.astimezone(UTC)).total_seconds()
