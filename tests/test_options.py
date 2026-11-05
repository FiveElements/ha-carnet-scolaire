"""Options, and the budget estimate the options page shows.

The estimator is the same function that produced annexe B §5.3, which is the
point: a document that drifts from the code is worse than no document, because
it is believed. The figures asserted here are therefore the annexe's figures,
and this module is what keeps the two in step.

``bounded_option`` is the limiter's whole boundary and is tested for what happens
below it: a stored value the UI cannot produce, because a hand-edited
``.storage`` file, a restore from another version and a future migration all
bypass the UI.
"""

from datetime import time

import pytest

from custom_components.carnet_scolaire.const import (
    DEFAULT_MAX_LOGINS_PER_DAY,
    DEFAULT_TIER_INTERVALS,
    OPT_MAX_LOGINS_PER_DAY,
    OPT_MAX_REQUESTS_PER_DAY,
    OPT_MAX_REQUESTS_PER_HOUR,
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
from custom_components.carnet_scolaire.options import (
    REQUESTS_PER_BATCH,
    bounded_option,
    build_rate_limit_config,
    estimate_daily_requests,
    tier_enabled,
    tier_intervals,
)
from custom_components.carnet_scolaire.ratelimit import REQUESTS_PER_LOGIN_WORST_CASE


def interval(tier: Tier) -> str:
    """The option key holding a tier's interval."""
    return OPT_TIER_INTERVAL.format(tier=tier.value)


def switch(tier: Tier) -> str:
    """The option key holding a tier's enabled flag."""
    return OPT_TIER_ENABLED.format(tier=tier.value)


# ---------------------------------------------------------------------------
# Reading the per-tier options
# ---------------------------------------------------------------------------


def test_tier_options_fall_back_to_the_defaults() -> None:
    """An entry saved before a tier existed must not lose it."""
    assert tier_intervals({}) == DEFAULT_TIER_INTERVALS
    assert all(tier_enabled({}).values())


def test_tier_options_are_read_from_the_stored_keys() -> None:
    """The key template and the reader have to agree, so both are exercised."""
    options = {interval(Tier.TIMETABLE): 5, switch(Tier.HISTORY): False}

    assert tier_intervals(options)[Tier.TIMETABLE] == 5
    assert tier_enabled(options)[Tier.HISTORY] is False
    assert tier_enabled(options)[Tier.HOMEWORK] is True


# ---------------------------------------------------------------------------
# The clamp
# ---------------------------------------------------------------------------


def test_a_stored_zero_tier_interval_is_clamped() -> None:
    """The one interval value that has to be caught, and it was not.

    ``OPTION_RANGES`` is keyed by option name, but the ten interval keys are
    produced by formatting :data:`OPT_TIER_INTERVAL`, so a lookup by key found
    nothing and the value went through untouched. A tier whose interval is zero
    is due again the instant its batch finishes: it fires on every master tick
    and spends the whole day's request budget before lunch.
    """
    low, _high = TIER_INTERVAL_RANGE
    intervals = tier_intervals({interval(Tier.TIMETABLE): 0})
    assert intervals[Tier.TIMETABLE] == low


def test_a_negative_tier_interval_is_clamped_too() -> None:
    """A negative deadline is permanently in the past, which is worse than zero."""
    low, _high = TIER_INTERVAL_RANGE
    assert tier_intervals({interval(Tier.MARKS): -30})[Tier.MARKS] == low


def test_an_absurd_tier_interval_is_clamped_down_to_a_day() -> None:
    """Past a day, "every N minutes" has stopped describing anything."""
    _low, high = TIER_INTERVAL_RANGE
    assert tier_intervals({interval(Tier.MENUS): 10**6})[Tier.MENUS] == high


def test_an_unparseable_tier_interval_falls_back_to_its_default() -> None:
    """A migration that wrote a string must not take the tier down with it."""
    assert (
        tier_intervals({interval(Tier.HOMEWORK): "later"})[Tier.HOMEWORK]
        == DEFAULT_TIER_INTERVALS[Tier.HOMEWORK]
    )


def test_the_interval_range_is_the_one_the_options_page_offers() -> None:
    """The field and the reader read from the same table.

    They used to disagree by construction: the number selector hard-coded
    1-1440 while the reader clamped nothing at all, so the only two places that
    knew the range had no way of staying in step.
    """
    assert TIER_INTERVAL_RANGE == (1.0, 1440.0)


def test_a_value_inside_its_range_passes_through() -> None:
    """The clamp is a floor and a ceiling, not a rewrite."""
    assert (
        bounded_option({OPT_MAX_REQUESTS_PER_HOUR: 120}, OPT_MAX_REQUESTS_PER_HOUR, 240)
        == 120
    )


def test_a_stored_zero_hourly_rate_is_clamped() -> None:
    """Zero makes the token wait infinite and defers every tier for ever.

    With no repair issue to explain it, which is the worst kind of stopped:
    entities keep their last value, nothing appears in the log, and the
    integration looks like it is working.
    """
    low, _high = OPTION_RANGES[OPT_MAX_REQUESTS_PER_HOUR]
    assert (
        bounded_option({OPT_MAX_REQUESTS_PER_HOUR: 0}, OPT_MAX_REQUESTS_PER_HOUR, 240)
        == low
    )


def test_a_stored_zero_daily_cap_is_clamped() -> None:
    """Zero sheds everything but the session tier, permanently."""
    low, _high = OPTION_RANGES[OPT_MAX_REQUESTS_PER_DAY]
    assert (
        bounded_option({OPT_MAX_REQUESTS_PER_DAY: 0}, OPT_MAX_REQUESTS_PER_DAY, 2000)
        == low
    )


def test_an_absurdly_large_value_is_clamped_down() -> None:
    """The ceiling protects the establishment, not the user."""
    _low, high = OPTION_RANGES[OPT_MAX_REQUESTS_PER_DAY]
    assert (
        bounded_option(
            {OPT_MAX_REQUESTS_PER_DAY: 10**9}, OPT_MAX_REQUESTS_PER_DAY, 2000
        )
        == high
    )


def test_an_unparseable_value_falls_back_to_the_default() -> None:
    """A migration that wrote a string, or a hand-edited file."""
    assert (
        bounded_option(
            {OPT_MIN_REQUEST_INTERVAL: "soon"}, OPT_MIN_REQUEST_INTERVAL, 1.0
        )
        == 1.0
    )
    assert (
        bounded_option({OPT_MIN_REQUEST_INTERVAL: None}, OPT_MIN_REQUEST_INTERVAL, 1.0)
        == 1.0
    )


def test_a_key_with_no_documented_range_is_not_clamped() -> None:
    """Only the tunables annexe B §7 gives bounds to are bounded."""
    assert bounded_option({"something_else": 12345}, "something_else", 0.0) == 12345


# ---------------------------------------------------------------------------
# Building the limiter's configuration
# ---------------------------------------------------------------------------


def test_the_default_options_build_the_documented_configuration() -> None:
    """Annexe B §7's defaults, read through the same path the runtime uses."""
    config = build_rate_limit_config({})

    assert config.min_request_interval == 1.0
    assert config.max_requests_per_hour == 240
    assert config.burst_size == 20
    assert config.max_requests_per_day == 2000
    assert config.max_logins_per_day == 24
    assert config.quiet_hours_enabled is True
    assert config.quiet_start == time(22, 0)
    assert config.quiet_end == time(6, 0)


def test_quiet_hours_accept_a_string_or_a_time() -> None:
    """The UI stores a string; a test or a migration may hand back a ``time``."""
    from_string = build_rate_limit_config(
        {OPT_QUIET_START: "23:15", OPT_QUIET_END: "05:45:30"}
    )
    assert from_string.quiet_start == time(23, 15)
    assert from_string.quiet_end == time(5, 45, 30)

    from_time = build_rate_limit_config({OPT_QUIET_START: time(21, 0)})
    assert from_time.quiet_start == time(21, 0)


def test_an_unparseable_quiet_hour_falls_back_rather_than_raising() -> None:
    """A bad value here would otherwise fail the whole config entry setup."""
    config = build_rate_limit_config({OPT_QUIET_START: "not a time"})
    assert config.quiet_start == time(22, 0)


def test_a_clamped_option_reaches_the_configuration() -> None:
    """The clamp is on the path, not beside it."""
    config = build_rate_limit_config({OPT_MAX_REQUESTS_PER_HOUR: 0})
    assert config.max_requests_per_hour > 0


# ---------------------------------------------------------------------------
# The per-batch cost table
# ---------------------------------------------------------------------------


def test_the_cost_table_covers_every_data_tier() -> None:
    """A tier absent from the table is a tier the estimate ignores."""
    assert set(REQUESTS_PER_BATCH) == set(DEFAULT_TIER_INTERVALS) - {Tier.SESSION}


def test_history_costs_eight_requests_not_six() -> None:
    """Four per closed period, not three, and the fourth is undocumented.

    ``DernieresNotes`` 198, ``PageBulletins`` 13, ``PagePresence`` 19 -- and
    ``DernieresEvaluations`` **201**, which appears in neither the
    specification nor the first version of annexe B. Counting from the code
    beats counting from the prose.
    """
    assert REQUESTS_PER_BATCH[Tier.HISTORY] == 8.0


def test_discussions_cost_two_requests_not_one() -> None:
    """The thread list is one; ``ListeMessages`` is charged per expansion.

    Two is the honest average for an account that receives a message or so per
    cycle. The limiter no longer *depends* on the figure being right, because
    the session reconciles the real cost the gateway reports -- but the
    estimate a user tunes against should still be the truth.
    """
    assert REQUESTS_PER_BATCH[Tier.DISCUSSIONS] == 2.0


def test_the_week_boundary_tiers_are_priced_between_one_and_two() -> None:
    """``timetable`` and ``menus`` cost two posts one day in seven."""
    assert REQUESTS_PER_BATCH[Tier.TIMETABLE] == pytest.approx(1.14)
    assert REQUESTS_PER_BATCH[Tier.MENUS] == pytest.approx(1.14)


# ---------------------------------------------------------------------------
# The estimate -- these are annexe B's own numbers
# ---------------------------------------------------------------------------


def test_the_default_estimate_matches_the_annexe() -> None:
    """≈ 198 a day with a generous server timeout (annexe B §5.3).

    The contract bound annexe B states for this configuration is 160-210, and
    this assertion is what keeps the document honest.
    """
    estimate = estimate_daily_requests({}, session_lifetime_minutes=30.0)

    assert 160 <= estimate <= 210
    assert estimate == 199


def test_the_degenerate_case_stays_under_the_v1_model() -> None:
    """A five-minute server timeout: lazy degenerates into v1's design.

    Which is the whole dominance argument -- lazy is never worse -- and the
    annexe's stated bound for the bad case is 500.
    """
    estimate = estimate_daily_requests({}, session_lifetime_minutes=5.0)

    assert estimate <= 500
    assert estimate == 346


def test_an_unmeasured_lifetime_quotes_the_pessimistic_figure() -> None:
    """The estimate shown before the first measurement must be the safe one.

    A user cannot be disappointed by a figure that only ever falls.
    """
    assert estimate_daily_requests(
        {}, session_lifetime_minutes=None
    ) == estimate_daily_requests({}, session_lifetime_minutes=5.0)


def test_a_second_child_costs_data_and_not_a_session() -> None:
    """The session is shared; the data is not (annexe B §5.6)."""
    one = estimate_daily_requests({}, students=1, session_lifetime_minutes=30.0)
    two = estimate_daily_requests({}, students=2, session_lifetime_minutes=30.0)

    assert two > one
    assert two - one == pytest.approx(178, abs=3)


def test_disabling_quiet_hours_raises_the_estimate() -> None:
    """Sixteen active hours become twenty-four."""
    with_quiet = estimate_daily_requests({}, session_lifetime_minutes=30.0)
    without = estimate_daily_requests(
        {OPT_QUIET_HOURS_ENABLED: False}, session_lifetime_minutes=30.0
    )

    assert without > with_quiet
    assert without == 283


def test_a_daily_tier_is_counted_once_and_not_two_thirds_of_a_time() -> None:
    """The clamp that produced the annexe's wrong ``history`` figure.

    ``min(runs, 1440 / minutes)`` was dead for every possible input -- ``runs``
    is already the smaller of the two -- and it left a 1440-minute tier counted
    0.667 times a day. So ``history`` came out at 4 requests where the annexe
    said 6 and the code actually spends 8.
    """
    only_history = {
        switch(tier): False
        for tier in DEFAULT_TIER_INTERVALS
        if tier is not Tier.HISTORY
    }
    estimate = estimate_daily_requests(only_history, session_lifetime_minutes=30.0)

    # Eight data requests, plus one login at the worst-case cost.
    assert estimate == 8 + REQUESTS_PER_LOGIN_WORST_CASE


def test_disabling_every_tier_still_costs_a_login() -> None:
    """The session tier is not sheddable, so the floor is one login a day."""
    nothing = {switch(tier): False for tier in DEFAULT_TIER_INTERVALS}
    assert estimate_daily_requests(nothing, session_lifetime_minutes=30.0) == 0


def test_the_per_batch_strategy_costs_a_login_per_batch() -> None:
    """Specification v1's design, priced (annexe B §5.4)."""
    lazy = estimate_daily_requests({}, session_lifetime_minutes=30.0)
    per_batch = estimate_daily_requests(
        {OPT_SESSION_STRATEGY: str(SessionStrategy.PER_BATCH)},
        session_lifetime_minutes=30.0,
    )
    assert per_batch > lazy


def test_the_estimate_respects_the_login_cap() -> None:
    """An estimate the running integration could not produce is not an estimate.

    Uncapped, the degenerate case quoted 64 logins a day against a configured
    maximum of 24.
    """
    capped = estimate_daily_requests(
        {OPT_MAX_LOGINS_PER_DAY: 2}, session_lifetime_minutes=5.0
    )
    uncapped = estimate_daily_requests(
        {OPT_MAX_LOGINS_PER_DAY: DEFAULT_MAX_LOGINS_PER_DAY},
        session_lifetime_minutes=5.0,
    )
    assert capped < uncapped


def test_the_knife_edge_falls_on_the_pessimistic_side() -> None:
    """A lifetime exactly equal to the fastest interval counts as too short.

    At that point the session expires exactly when the next batch is due, and
    the master tick's granularity makes the real gap no *shorter* than the
    interval -- so the equality belongs with the bad case.
    """
    fastest = min(
        minutes
        for tier, minutes in DEFAULT_TIER_INTERVALS.items()
        if tier is not Tier.SESSION
    )
    at_the_edge = estimate_daily_requests({}, session_lifetime_minutes=float(fastest))
    just_above = estimate_daily_requests(
        {}, session_lifetime_minutes=float(fastest) + 0.1
    )

    assert at_the_edge > just_above


def test_a_zero_interval_is_treated_as_one_minute() -> None:
    """Defensive: division by an interval of zero is not an estimate."""
    estimate = estimate_daily_requests(
        {interval(Tier.TIMETABLE): 0}, session_lifetime_minutes=30.0
    )
    assert estimate > 0


def test_the_fastest_interval_falls_back_when_nothing_is_enabled() -> None:
    """With no enabled tier there is no interval to be faster than."""
    nothing = {switch(tier): False for tier in DEFAULT_TIER_INTERVALS}
    assert estimate_daily_requests(nothing, session_lifetime_minutes=None) == 0
