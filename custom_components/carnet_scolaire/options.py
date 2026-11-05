"""Reading options, and estimating what they will cost.

The budget estimator is not decoration. Annexe B §7 requires the options flow to
show the daily request estimate the entered values imply, computed by the *same*
function that produced the numbers in the annexe -- a setting whose consequence
you cannot see gets set at random.
"""

from datetime import time
import logging
from typing import TYPE_CHECKING, Any, Final

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
    DEFAULT_SESSION_STRATEGY,
    DEFAULT_TIER_INTERVALS,
    OPT_BACKOFF_BASE,
    OPT_BACKOFF_MAX,
    OPT_BOOTSTRAP_HOLD,
    OPT_BURST_SIZE,
    OPT_CREDENTIALS_HOLD,
    OPT_MAX_FAILED_LOGINS_PER_HOUR,
    OPT_MAX_LOGINS_PER_DAY,
    OPT_MAX_REQUESTS_PER_DAY,
    OPT_MAX_REQUESTS_PER_HOUR,
    OPT_MAX_WAIT,
    OPT_MIN_REQUEST_INTERVAL,
    OPT_QUIET_END,
    OPT_QUIET_HOURS_ENABLED,
    OPT_QUIET_START,
    OPT_SESSION_STRATEGY,
    OPT_TIER_ENABLED,
    OPT_TIER_INTERVAL,
    OPTION_RANGES,
    TIER_INTERVAL_RANGE,
    SessionStrategy,
    Tier,
)
from .ratelimit import REQUESTS_PER_LOGIN_WORST_CASE, RateLimitConfig

if TYPE_CHECKING:
    from collections.abc import Mapping

_LOGGER: Final = logging.getLogger(__name__)

#: Requests one batch of a tier costs, counted against the upstream source
#: rather than assumed -- this table is the thing specification v1 got wrong
#: three times, and two of its entries were still wrong after review.
#:
#: ``timetable`` is 1.14 because ``Client.lessons()`` bills by *week*: asking
#: for today and tomorrow costs the same single request as the whole week,
#: except on the one day in seven where tomorrow crosses into the next week.
#: ``menus`` is 1.14 for the same reason -- ``Client.menus`` walks from the
#: Monday of the start week, so it posts twice exactly when today is a Sunday.
#:
#: ``marks`` is 2 because the current period's report card rides along.
#:
#: ``history`` is **8**, not the 6 the annexe derived. Four requests per closed
#: period, not three: ``DernieresNotes`` 198, ``PageBulletins``, ``PagePresence``
#: 19 and ``DernieresEvaluations`` 201. Tab 201 is in neither the specification
#: nor the annexe, which is exactly why counting from the code beats counting
#: from the prose.
#:
#: ``discussions`` is **2**, not 1. The thread list is one request, and
#: ``Discussion.messages`` posts ``ListeMessages`` on every read, so expanding
#: the newly active threads costs up to
#: ``gateway.MAX_DISCUSSION_EXPANSIONS`` more. Two is the honest average for an
#: account that receives a message or so per cycle; the limiter no longer
#: relies on this figure being right, because the session reconciles the real
#: cost the gateway reports (see ``RateLimiter.reconcile``).
REQUESTS_PER_BATCH: Final[dict[Tier, float]] = {
    Tier.TIMETABLE: 1.14,
    Tier.HOMEWORK: 1.0,
    Tier.NEWS: 1.0,
    Tier.DISCUSSIONS: 2.0,
    Tier.MARKS: 2.0,
    Tier.ATTENDANCE: 1.0,
    Tier.EVALUATIONS: 1.0,
    Tier.MENUS: 1.14,
    Tier.STATIC: 1.0,
    Tier.HISTORY: 8.0,
}

ACTIVE_HOURS_WITH_QUIET: Final = 16.0
ACTIVE_HOURS_WITHOUT_QUIET: Final = 24.0


def _interval_key(tier: Tier) -> str:
    """Option key holding a tier's interval."""
    return OPT_TIER_INTERVAL.format(tier=tier.value)


def _enabled_key(tier: Tier) -> str:
    """Option key holding a tier's enabled flag."""
    return OPT_TIER_ENABLED.format(tier=tier.value)


def tier_intervals(options: Mapping[str, Any]) -> dict[Tier, int]:
    """Per-tier intervals in minutes, clamped, falling back to the defaults.

    Clamped for the reason :func:`bounded_option` gives at length: the options
    page bounds these ten fields to 1-1440 already, and a value that never came
    from the options page has never met that bound. Zero is the one that
    matters, and the comment on :data:`TIER_INTERVAL_RANGE` says why.
    """
    return {
        tier: int(
            bounded_option(
                options,
                _interval_key(tier),
                default,
                bounds=TIER_INTERVAL_RANGE,
            )
        )
        for tier, default in DEFAULT_TIER_INTERVALS.items()
    }


def tier_enabled(options: Mapping[str, Any]) -> dict[Tier, bool]:
    """Per-tier enabled flags, defaulting to on."""
    return {
        tier: bool(options.get(_enabled_key(tier), True))
        for tier in DEFAULT_TIER_INTERVALS
    }


def _as_time(raw: Any, fallback: str) -> time:
    """Parse an ``HH:MM[:SS]`` option into a ``time``."""
    if isinstance(raw, time):
        return raw
    try:
        return time.fromisoformat(str(raw))
    except TypeError, ValueError:
        return time.fromisoformat(fallback)


def bounded_option(
    options: Mapping[str, Any],
    key: str,
    default: float,
    *,
    bounds: tuple[float, float] | None = None,
) -> float:
    """Read one option and clamp it to the range annexe B §7 gives it.

    ``bounds`` overrides the lookup in :data:`OPTION_RANGES`, which is what the
    ten per-tier interval keys need: they are formatted per tier, so no lookup
    by key can find them.

    Clamped here and not only in the options flow, because this function is the
    limiter's whole boundary. A stored ``max_requests_per_hour`` of 0 makes
    ``_token_wait`` return infinity and defers every tier for ever with no
    repair issue to explain it; a stored ``max_requests_per_day`` of 0 sheds
    everything but the session tier permanently. Neither is reachable through
    the UI, but a hand-edited ``.storage`` file, a restore from a different
    version, or a future migration all bypass the UI.
    """
    try:
        value = float(options.get(key, default))
    except TypeError, ValueError:
        return default
    if bounds is None:
        bounds = OPTION_RANGES.get(key)
    if bounds is None:
        return value
    low, high = bounds
    clamped = min(max(value, float(low)), float(high))
    if clamped != value:
        _LOGGER.warning(
            "option %s was %s, which is outside the accepted range %s-%s; "
            "using %s instead",
            key,
            value,
            low,
            high,
            clamped,
        )
    return clamped


def build_rate_limit_config(options: Mapping[str, Any]) -> RateLimitConfig:
    """Turn the stored options into the limiter's configuration."""
    return RateLimitConfig(
        min_request_interval=float(
            bounded_option(
                options, OPT_MIN_REQUEST_INTERVAL, DEFAULT_MIN_REQUEST_INTERVAL
            )
        ),
        max_requests_per_hour=int(
            bounded_option(
                options, OPT_MAX_REQUESTS_PER_HOUR, DEFAULT_MAX_REQUESTS_PER_HOUR
            )
        ),
        burst_size=int(bounded_option(options, OPT_BURST_SIZE, DEFAULT_BURST_SIZE)),
        max_requests_per_day=int(
            bounded_option(
                options, OPT_MAX_REQUESTS_PER_DAY, DEFAULT_MAX_REQUESTS_PER_DAY
            )
        ),
        max_wait=float(bounded_option(options, OPT_MAX_WAIT, DEFAULT_MAX_WAIT)),
        max_logins_per_day=int(
            bounded_option(options, OPT_MAX_LOGINS_PER_DAY, DEFAULT_MAX_LOGINS_PER_DAY)
        ),
        max_failed_logins_per_hour=int(
            bounded_option(
                options,
                OPT_MAX_FAILED_LOGINS_PER_HOUR,
                DEFAULT_MAX_FAILED_LOGINS_PER_HOUR,
            )
        ),
        credentials_hold=float(
            bounded_option(options, OPT_CREDENTIALS_HOLD, DEFAULT_CREDENTIALS_HOLD)
        ),
        bootstrap_hold=float(
            bounded_option(options, OPT_BOOTSTRAP_HOLD, DEFAULT_BOOTSTRAP_HOLD)
        ),
        backoff_base=float(
            bounded_option(options, OPT_BACKOFF_BASE, DEFAULT_BACKOFF_BASE)
        ),
        backoff_max=float(
            bounded_option(options, OPT_BACKOFF_MAX, DEFAULT_BACKOFF_MAX)
        ),
        quiet_hours_enabled=bool(
            options.get(OPT_QUIET_HOURS_ENABLED, DEFAULT_QUIET_HOURS_ENABLED)
        ),
        quiet_start=_as_time(options.get(OPT_QUIET_START), DEFAULT_QUIET_START),
        quiet_end=_as_time(options.get(OPT_QUIET_END), DEFAULT_QUIET_END),
    )


def estimate_daily_requests(
    options: Mapping[str, Any],
    *,
    students: int = 1,
    session_lifetime_minutes: float | None = None,
) -> int:
    """Estimate daily HTTP requests for a set of options.

    Same function that produced annexe B §5.3, so the options page and the
    document cannot drift apart.

    ``session_lifetime_minutes`` is the unknown parameter the whole session
    debate turned on. Passing the measured value gives the real estimate;
    leaving it ``None`` assumes the pessimistic case, which is the point of the
    dominance argument -- the estimate shown to a user who has no measurement
    yet must be the one they cannot be disappointed by.
    """
    intervals = tier_intervals(options)
    enabled = tier_enabled(options)
    quiet = bool(options.get(OPT_QUIET_HOURS_ENABLED, DEFAULT_QUIET_HOURS_ENABLED))
    active_hours = ACTIVE_HOURS_WITH_QUIET if quiet else ACTIVE_HOURS_WITHOUT_QUIET

    data_requests = 0.0
    batches_per_day = 0.0
    for tier, per_batch in REQUESTS_PER_BATCH.items():
        if not enabled.get(tier, True):
            continue
        minutes = max(1, intervals.get(tier, DEFAULT_TIER_INTERVALS[tier]))
        # A tier whose interval is longer than the active window still runs
        # once a day. The previous `min(runs, 1440/minutes)` clamp was dead for
        # every possible input -- `runs` is already the smaller of the two --
        # and it left a 1440-minute tier counted 0.667 times a day, which is
        # why `history` came out at 4 requests where the annexe says 6 (and
        # where the code actually spends 8).
        runs = max((active_hours * 60.0) / minutes, 1.0)
        data_requests += runs * per_batch * students
        batches_per_day = max(batches_per_day, runs)

    strategy = SessionStrategy(
        options.get(OPT_SESSION_STRATEGY, DEFAULT_SESSION_STRATEGY)
    )
    logins = _estimate_logins(
        strategy=strategy,
        batches_per_day=batches_per_day,
        fastest_interval_minutes=_fastest_interval(intervals, enabled),
        session_lifetime_minutes=session_lifetime_minutes,
    )
    # The login cap is a real ceiling, so the estimate has to respect it. Left
    # uncapped, the degenerate case of a five-minute server timeout quoted 64
    # logins a day against a configured maximum of 24 -- an estimate the
    # running integration could not produce even if it tried.
    logins = min(
        logins,
        float(
            bounded_option(options, OPT_MAX_LOGINS_PER_DAY, DEFAULT_MAX_LOGINS_PER_DAY)
        ),
    )

    # The worst case, not the floor. A login is five requests in normal mode and
    # up to seven in token mode -- the QR enrolment this integration
    # recommends -- and quoting five would understate the one figure a user
    # tunes against.
    return round(data_requests + logins * REQUESTS_PER_LOGIN_WORST_CASE)


def _fastest_interval(
    intervals: Mapping[Tier, int], enabled: Mapping[Tier, bool]
) -> int:
    """The shortest enabled interval, which sets the batch floor."""
    candidates = [
        minutes
        for tier, minutes in intervals.items()
        if enabled.get(tier, True) and tier in REQUESTS_PER_BATCH
    ]
    return min(candidates) if candidates else 15


def _estimate_logins(
    *,
    strategy: SessionStrategy,
    batches_per_day: float,
    fastest_interval_minutes: int,
    session_lifetime_minutes: float | None,
) -> float:
    """How many logins a day the strategy implies.

    This is the arithmetic the whole session decision rested on. With
    ``PER_BATCH`` it is one login per batch -- specification v1's design. With
    ``LAZY`` it is one login per batch *only if* the server's inactivity timeout
    is shorter than the fastest tier's interval; otherwise the data traffic keeps
    the session alive on its own and it falls to a handful a day.

    Which is the dominance property, in one expression: lazy is never worse.
    """
    if batches_per_day <= 0:
        # Every data tier switched off. There is then no batch to open a
        # session for, so quoting the three-a-day floor described the login
        # traffic of a configuration that performs none -- and the options page
        # showed 21 requests a day for an integration doing nothing at all.
        return 0.0
    if strategy is SessionStrategy.PER_BATCH:
        return batches_per_day
    if session_lifetime_minutes is None:
        # No measurement yet: quote the pessimistic figure.
        return batches_per_day
    if session_lifetime_minutes <= fastest_interval_minutes:
        # `<=`, not `<`. At the knife edge the session expires exactly when the
        # next batch is due, and the master tick's granularity makes the real
        # gap no shorter than the interval -- so the equality case belongs on
        # the pessimistic side of the line.
        return batches_per_day
    return 3.0


_ED_ESTIMATOR_TIERS: Final = frozenset(
    {Tier.TIMETABLE, Tier.HOMEWORK, Tier.MARKS, Tier.ATTENDANCE}
)


def estimate_ecoledirecte_daily_requests(
    options: Mapping[str, Any],
    *,
    students: int = 1,
    include_qcm: bool = False,
    multi_establishment: bool = False,
) -> int:
    """Estimate one ED run: login 2, optional QCM 4, one POST per capable tier.

    This is not the annexe B Pronote estimator. A login is two requests, a
    remembered QCM adds four once, each capable enabled tier posts once per
    child, and a parent spanning establishments adds one ``renewtoken``.
    """
    enabled = tier_enabled(options)
    posts = sum(1 for tier in _ED_ESTIMATOR_TIERS if enabled.get(tier, True)) * max(
        1, students
    )
    total = 2 + posts
    if include_qcm:
        total += 4
    if multi_establishment:
        total += 1
    return total
