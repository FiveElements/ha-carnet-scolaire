"""The scheduler: one master tick, tiers that carry deadlines rather than timers.

Independent timers eventually coincide and produce bursts, so there is exactly
one heartbeat. It asks which tiers are due, and the batch runs them in one
session, spaced by the rate limiter (§5.1).

A deferred tier is never abandoned: its deadline moves and it keeps its previous
snapshot, so its entities keep their value (annexe B §2.4). And reducing an
interval does not trigger an immediate collection -- the next deadline is
recomputed from the last collection, not from the moment the option changed
(§7.3), because otherwise saving the options page would fire a full batch.

Like the limiter, this module knows neither PRONOTE nor Home Assistant: it takes
an injectable clock and answers questions.
"""

from dataclasses import dataclass, replace
from datetime import datetime
import logging
from typing import TYPE_CHECKING, Any, Final

from .const import (
    DEFAULT_TIER_INTERVALS,
    PRIORITY_RANK,
    TIER_PRIORITY,
    Priority,
    Tier,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping

_LOGGER: Final = logging.getLogger(__name__)

SECONDS_PER_MINUTE: Final = 60.0

#: How many times a tier that produced *nothing* may be retried inside one of
#: its own intervals. The limiter's back-off is account-wide and resets as soon
#: as any other tier succeeds, so a tier failing for a reason of its own -- an
#: establishment that publishes no teaching team, say -- never accrues any. It
#: was therefore deferred by one back-off base, about thirty seconds, which is
#: inside the five-minute master tick: a tier declaring one collection a day
#: was attempted at every tick, 288 times, and annexe B's budget for it was
#: wrong by that factor. Four rather than one because a transient failure
#: deserves a retry before tomorrow.
_RETRIES_PER_INTERVAL: Final = 4.0


@dataclass(frozen=True, slots=True)
class TierPlan:
    """How often a tier should run, and how readily it may be sacrificed."""

    tier: Tier
    interval_minutes: int
    priority: Priority
    enabled: bool = True

    @property
    def interval_seconds(self) -> float:
        """The interval, in seconds."""
        return self.interval_minutes * SECONDS_PER_MINUTE


@dataclass(slots=True)
class TierState:
    """Mutable bookkeeping for one tier."""

    #: Monotonic instant of the last *successful* collection. ``None`` until the
    #: first one, which is what makes a tier due immediately after start-up.
    last_collected: float | None = None
    #: Monotonic instant before which the tier must not run. Set when a tier is
    #: deferred, so a throttled tier does not spin on every tick.
    not_before: float = 0.0
    #: Raised for exactly one tick by the refresh button. A button asks for a
    #: priority pass; it does not get a dispensation from the limiter (§6.2).
    boosted: bool = False
    #: Monotonic instant at which a boost was last *served* -- that is, followed
    #: by a real collection. ``None`` while no boost has ever been honoured.
    #: This is what makes ten presses inside one interval cost one batch; see
    #: :meth:`FetchScheduler.request`.
    boost_served_at: float | None = None
    #: Wall-clock twin of :attr:`boost_served_at`, kept for the same reason
    #: :attr:`last_collected_at` doubles :attr:`last_collected`: a monotonic
    #: instant means nothing outside this process, so it can be compared but
    #: never displayed. A card asking "did my press land?" needs a time of day,
    #: and the three answers it has to tell apart are "nothing armed" (the
    #: ceiling refused: already asked for), "armed and not yet served"
    #: (:attr:`boosted`) and "served at *this* time".
    boost_served_wall: datetime | None = None
    #: Consecutive deferrals, for the diagnostic attribute only.
    deferrals: int = 0
    #: Consecutive *failures*, which is a different fact from a deferral: a
    #: deferred tier was refused before it was tried, a failed one was tried
    #: and did not work. Unlike :attr:`deferrals` this one is load-bearing --
    #: it is what bounds the first-collection dispensation in ``tiers.py``.
    failures: int = 0
    #: Wall-clock instant of the last successful collection. Kept alongside the
    #: monotonic one because staleness has to exclude quiet hours, and only a
    #: wall clock can say whether the night intervened.
    last_collected_at: datetime | None = None


def default_plans(
    intervals: Mapping[Tier, int] | None = None,
    enabled: Mapping[Tier, bool] | None = None,
) -> dict[Tier, TierPlan]:
    """Build a plan per data tier from options, falling back to the defaults."""
    chosen_intervals = intervals or {}
    chosen_enabled = enabled or {}
    plans: dict[Tier, TierPlan] = {}
    for tier, default_interval in DEFAULT_TIER_INTERVALS.items():
        plans[tier] = TierPlan(
            tier=tier,
            interval_minutes=chosen_intervals.get(tier, default_interval),
            priority=TIER_PRIORITY[tier],
            enabled=chosen_enabled.get(tier, True),
        )
    return plans


class FetchScheduler:
    """Answers "what is due now?", in the order things should be sacrificed."""

    def __init__(
        self,
        plans: Mapping[Tier, TierPlan],
        clock: Callable[[], float],
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._plans: dict[Tier, TierPlan] = dict(plans)
        self._clock = clock
        # Two clocks, for the same reason the limiter has two: the monotonic one
        # measures intervals and cannot jump, the wall clock knows whether the
        # night has passed. Staleness needs both.
        self._now = now
        self._states: dict[Tier, TierState] = {
            tier: TierState() for tier in self._plans
        }

    # -- surviving a reload ------------------------------------------------

    def export_state(self) -> dict[str, float]:
        """Last-collection instants, keyed by tier name.

        Exists because an options save reloads the config entry, and a reload
        builds a brand-new scheduler whose every ``last_collected`` is ``None``
        -- which makes all ten tiers immediately due. Preserving
        ``last_collected`` inside :meth:`reconfigure` was the right idea aimed
        at the wrong seam: nothing ever called it, because Home Assistant
        reloads rather than reconfigures. So the state is handed across the
        reload instead, and nudging one interval no longer costs a login plus a
        full batch every time the options page is saved (§7.3).

        Monotonic values are used as-is because a reload happens inside the same
        process, so the same monotonic origin still applies.
        """
        return {
            str(tier): state.last_collected
            for tier, state in self._states.items()
            if state.last_collected is not None
        }

    def import_state(self, saved: Mapping[str, float]) -> None:
        """Restore last-collection instants from :meth:`export_state`."""
        now = self._clock()
        for tier, state in self._states.items():
            value = saved.get(str(tier))
            if value is None:
                continue
            # A value from the future means the monotonic origin moved after
            # all -- a restart rather than a reload -- so it is discarded
            # rather than trusted.
            if value <= now:
                state.last_collected = value

    # -- configuration -----------------------------------------------------

    @property
    def plans(self) -> Mapping[Tier, TierPlan]:
        """The plans in force."""
        return dict(self._plans)

    def reconfigure(self, plans: Mapping[Tier, TierPlan]) -> None:
        """Apply new intervals without disturbing the schedule.

        ``last_collected`` is preserved, so the next deadline is computed from
        the last collection rather than from this call. That is weaker than
        "shortening an interval never fires an immediate batch", which is
        simply false: a tier collected forty minutes ago on a sixty-minute
        interval *is* due the moment the interval becomes fifteen, and it
        should be. What preservation buys is that the change is measured from
        real history rather than from the save (§7.3).

        The reload path is handled by :meth:`export_state` / :meth:`import_state`
        instead, because Home Assistant reloads a config entry rather than
        reconfiguring it in place.
        """
        self._plans = dict(plans)
        for tier in self._plans:
            self._states.setdefault(tier, TierState())
        for tier in list(self._states):
            if tier not in self._plans:
                del self._states[tier]

    def set_interval(self, tier: Tier, minutes: int) -> None:
        """Change one tier's interval, keeping its history."""
        plan = self._plans.get(tier)
        if plan is None:
            return
        self._plans[tier] = replace(plan, interval_minutes=minutes)

    def set_enabled(self, tier: Tier, *, enabled: bool) -> None:
        """Switch one tier on or off."""
        plan = self._plans.get(tier)
        if plan is None:
            return
        self._plans[tier] = replace(plan, enabled=enabled)

    # -- the question the master tick asks --------------------------------

    def due(self) -> list[Tier]:
        """Tiers that should run now, highest priority first.

        Ordering matters when the budget tightens: the limiter sheds by
        priority, so serving in priority order means the tiers that get dropped
        are the ones the sacrifice table already nominated (annexe B §2.4).
        """
        now = self._clock()
        candidates: list[tuple[int, float, Tier]] = []

        for tier, plan in self._plans.items():
            if not plan.enabled:
                continue
            state = self._states[tier]
            if now < state.not_before:
                continue
            if state.last_collected is None:
                overdue = float("inf")
            else:
                overdue = (now - state.last_collected) - plan.interval_seconds
                if overdue < 0 and not state.boosted:
                    continue
            # A boost raises the tier by one rank, floored at the rank just
            # below CRITICAL. Giving it -1 outranked the session tier, so a
            # refresh button on `history` (6 requests, LOW) was served first,
            # drained the bucket and the `max_wait` allowance, and `timetable`
            # -- HIGH -- was then deferred: the exact inverse of the sacrifice
            # order annexe B §2.4 lays down. The button asks for a priority
            # pass, not for supremacy.
            rank = PRIORITY_RANK[plan.priority]
            if state.boosted:
                rank = max(PRIORITY_RANK[Priority.CRITICAL] + 1, rank - 1)
            candidates.append((rank, -overdue, tier))

        candidates.sort(key=lambda item: (item[0], item[1], item[2].value))
        return [tier for _, _, tier in candidates]

    def next_due_in(self) -> float | None:
        """Seconds until the earliest deadline; **negative when overdue**.

        ``None`` means there is no enabled tier at all -- not "nothing is due",
        which is what the previous wording claimed.

        The sign matters, and it used to be clamped to zero. Feeding
        ``sensor.<account>_prochaine_collecte`` a clamped value made it publish
        *the instant of its own last refresh* as the next collection, and that
        entity is refreshed by a collection: a tier that was due and failing
        left the timestamp frozen forty minutes in the past with nothing
        explaining why. The card was right to call it absurd -- it was reading
        "now" written down some time ago.

        Unclamped, the value is a **deadline** and not a countdown, so it does
        not depend on when it was computed. A past timestamp then means exactly
        what it says: the collection is late by that much, which with
        ``tiers_due`` beside it is a diagnosis rather than a puzzle.

        Note the master tick's granularity bounds how closely execution follows
        the deadline: raising the tick rate costs nothing, because deadlines and
        not the heartbeat fix the budget (annexe B §5.6).
        """
        now = self._clock()
        best: float | None = None
        for tier, plan in self._plans.items():
            if not plan.enabled:
                continue
            state = self._states[tier]
            if state.last_collected is None:
                due_at = max(now, state.not_before)
            else:
                due_at = max(
                    state.last_collected + plan.interval_seconds, state.not_before
                )
            remaining = due_at - now
            if best is None or remaining < best:
                best = remaining
        return best

    def tiers_due_names(self) -> tuple[str, ...]:
        """Tier names currently due, for a diagnostic attribute."""
        return tuple(str(tier) for tier in self.due())

    # -- outcomes ----------------------------------------------------------

    def mark_collected(self, tier: Tier) -> None:
        """Record a successful collection and clear any boost or deferral."""
        state = self._states.get(tier)
        if state is None:
            return
        if state.boosted:
            # A boost that led to a collection has been *served*, and that is
            # remembered separately from the collection itself: `request` uses
            # it to refuse a second boost inside the same interval. Only set
            # here, and deliberately not in `defer` -- a boost that was
            # deferred was never honoured, so the user is entitled to ask
            # again.
            state.boost_served_at = self._clock()
            state.boost_served_wall = self._now() if self._now is not None else None
        state.last_collected = self._clock()
        state.last_collected_at = self._now() if self._now is not None else None
        state.not_before = 0.0
        state.boosted = False
        state.deferrals = 0
        state.failures = 0

    def defer(self, tier: Tier, retry_after: float) -> None:
        """Push a tier's earliest run forward, without cancelling it.

        Bounded by the tier's own interval: a limiter reporting "retry at
        midnight" must not silence a fifteen-minute tier for eleven hours if the
        budget frees up sooner. The deadline moves; it is never dropped.
        """
        state = self._states.get(tier)
        plan = self._plans.get(tier)
        if state is None or plan is None:
            return
        wait = max(0.0, min(retry_after, plan.interval_seconds))
        state.not_before = self._clock() + wait
        state.boosted = False
        state.deferrals += 1
        _LOGGER.debug(
            "tier %s deferred for %.0fs (deferral #%d)", tier, wait, state.deferrals
        )

    def mark_failed(self, tier: Tier, retry_after: float) -> None:
        """Treat a failure like a deferral: retry later, keep the snapshot.

        Unlike a plain deferral, a failure is floored at a share of the tier's
        own interval. The two are asked for by different situations and must
        not be answered alike: a *deferred* tier was refused by the limiter and
        has to come back the moment the budget allows, which is why
        :meth:`defer` honours the delay it is handed and clamps it only from
        above. A *failing* tier is asking to be retried at a rate its own
        interval never promised, and the limiter cannot say so on its behalf --
        its back-off counts the account's consecutive failures, which any other
        tier succeeding resets.

        Without the floor, a tier collected once a day was retried at every
        five-minute tick for as long as it kept failing. See
        :data:`_RETRIES_PER_INTERVAL`.
        """
        state = self._states.get(tier)
        if state is not None:
            state.failures += 1
        plan = self._plans.get(tier)
        if plan is not None:
            retry_after = max(
                retry_after, plan.interval_seconds / _RETRIES_PER_INTERVAL
            )
        self.defer(tier, retry_after)

    def request(self, tiers: Iterable[Tier] | None = None) -> None:
        """Ask for a priority pass on the next tick (the refresh button).

        Deliberately not a call: the button raises priority for the next
        heartbeat, it does not bypass the limiter and it does not short-circuit
        the minimum spacing (annexe B §6).

        **At most one boost per tier per interval**, and that ceiling is the
        substance of this method rather than a refinement of it. A boosted tier
        is due regardless of its deadline -- that is what makes the button do
        something at all -- so without a ceiling here, each press re-armed the
        dispensation and each press bought another collection. Ten presses on
        ``history``, which costs six requests per child, were 120 requests
        against a server whose one sanction applies to an IP address; §5.1 says
        ten presses cost one batch, and this is the line that makes that true.

        It was previously true only by accident: the ten collections used to
        overlap, so all but the first hit the account's "a batch is already
        running" guard and skipped. That is a property of the event loop, not of
        this code, and it stopped holding between two Home Assistant releases
        with nothing changed here -- measured at 2 requests on 2026.2 and 20 on
        2026.8 for the same ten presses.

        A press whose boost is refused is not an error: the tier keeps its
        deadline and will be collected on time. Pressing again in the next
        interval works, and a boost that was *deferred* rather than served does
        not count against the ceiling.
        """
        targets = list(tiers) if tiers is not None else list(self._plans)
        now = self._clock()
        for tier in targets:
            state = self._states.get(tier)
            plan = self._plans.get(tier)
            if state is None or plan is None or not plan.enabled:
                continue
            if (
                state.boost_served_at is not None
                and now - state.boost_served_at < plan.interval_seconds
            ):
                _LOGGER.debug(
                    "tier %s already had a boost served %.0fs ago; "
                    "refusing a second one inside its %.0fs interval",
                    tier,
                    now - state.boost_served_at,
                    plan.interval_seconds,
                )
                continue
            state.boosted = True
            state.not_before = 0.0

    # -- introspection -----------------------------------------------------

    def last_collected(self, tier: Tier) -> float | None:
        """Monotonic instant of the last successful collection."""
        state = self._states.get(tier)
        return state.last_collected if state else None

    def boosted_tiers(self) -> tuple[str, ...]:
        """Tier names carrying an armed, not-yet-served boost.

        Exists so a button can say something other than "the request was
        sent", which is true and useless. Pressing refresh has three outcomes
        and they are not distinguishable from the outside: the ceiling refused
        it because one was already served inside this interval, it is armed and
        waiting for the next heartbeat, or it has been served. This answers the
        middle one; :meth:`boosts_served` answers the third, and the absence of
        a tier from both answers the first.

        A tuple of names rather than a mapping, to sit beside ``tiers_due`` on
        the same entity and be asked the same way.
        """
        return tuple(str(tier) for tier, state in self._states.items() if state.boosted)

    def boosts_served(self) -> dict[str, datetime]:
        """When each tier's last boost was actually followed by a collection.

        Wall clock, because the monotonic instant this doubles cannot be
        rendered: it is meaningful only inside this process. Same reason
        ``last_collected_at`` exists beside ``last_collected``.

        Only tiers that have one appear, so the mapping reads like ``failing``
        beside it -- an empty mapping means nobody's press has been served yet,
        not that the scheduler stopped answering.
        """
        return {
            str(tier): state.boost_served_wall
            for tier, state in self._states.items()
            if state.boost_served_wall is not None
        }

    def failures(self, tier: Tier) -> int:
        """Consecutive failed collections, reset by the first success.

        Read by ``tiers.collect_tier`` to bound the first-collection
        dispensation. A tier with no data outranks quiet hours because its
        first collection is the one a user is watching for -- but that argument
        is about a collection that *works*, and it cannot survive the evidence
        that this one does not. An unbounded dispensation made a permanently
        failing tier the only caller awake all night, which is how a single
        broken tab turned into an account-wide back-off.
        """
        state = self._states.get(tier)
        return state.failures if state else 0

    def age(self, tier: Tier) -> float | None:
        """Seconds since the last successful collection of ``tier``."""
        last = self.last_collected(tier)
        if last is None:
            return None
        return self._clock() - last

    def is_stale(self, tier: Tier, stale_after: int, *, excused: float = 0.0) -> bool:
        """Whether a tier's data has aged past ``stale_after`` intervals.

        This is the boundary of §5.4: below it an entity keeps its value and
        flags its age; above it -- or with no data at all -- it becomes
        unavailable. Keeping the last known value and marking it dated is safer
        than admitting ignorance every ten minutes, because a
        ``numeric_state`` trigger on an entity that goes unavailable and comes
        back fires spuriously.

        ``excused`` is time that must not count against the age -- in practice
        the quiet hours the account deliberately did not collect during. It is
        not a nicety: with quiet hours on, which is the default, an eight-hour
        night is longer than ``stale_after`` times the interval of every fast
        tier, so without it the timetable, homework, news and discussion
        entities of every installation went ``unavailable`` at 06:00 each
        morning. Reporting "I no longer know" because the integration was
        asleep on purpose is the one answer §2.5 rules out.
        """
        plan = self._plans.get(tier)
        age = self.age(tier)
        if plan is None:
            return True
        if age is None:
            # No recorded collection is **not** "too old": there is no age to
            # exceed. Answering `True` here cost a live instance its whole
            # dashboard, because of an ordering that is easy to miss.
            #
            # `_async_collect` publishes each student's snapshot and only then
            # calls `mark_collected`. Publishing is what notifies the entities,
            # so at the instant they are told their data has arrived, the tier
            # still has no `last_collected` -- and every one of them evaluated
            # `available` as False and wrote itself `unavailable`, on the very
            # push that delivered the snapshot. `mark_collected` then set the
            # age a microsecond later with nothing left to re-render.
            #
            # What made it look like a different bug on every tier is which
            # entities recover. Those in `CLOCK_DRIVEN_KEYS` schedule their own
            # re-evaluation and repaired themselves within the hour, so
            # `timetable` and `homework` appeared to work; `marks`, `news`,
            # `menus`, `discussions`, `attendance` and `evaluations` have no
            # such timer and stayed `unavailable` with their data present --
            # until the tier's next interval, which is a full day for `menus`,
            # and until 06:00 with quiet hours on.
            #
            # Returning False cannot hide a missing snapshot, which is the
            # thing a caller might fear: `PronoteEntity.available` asks
            # `snapshot is not None` *first* and `extra_state_attributes`
            # returns nothing without one. Absence of data and staleness of
            # data are two questions, and this one is only ever the second.
            return False
        return (age - max(0.0, excused)) > stale_after * plan.interval_seconds

    def last_collected_at(self, tier: Tier) -> datetime | None:
        """Wall-clock instant of the last successful collection."""
        state = self._states.get(tier)
        return state.last_collected_at if state else None

    def diagnostics(self) -> dict[str, dict[str, Any]]:
        """Per-tier state for the diagnostic download, and for the service."""
        return {
            str(tier): {
                "interval_minutes": plan.interval_minutes,
                "priority": str(plan.priority),
                "enabled": plan.enabled,
                "age_seconds": self.age(tier),
                "deferrals": self._states[tier].deferrals,
                "failures": self._states[tier].failures,
                "boosted": self._states[tier].boosted,
                "boost_served_at": (
                    served.isoformat()
                    if (served := self._states[tier].boost_served_wall)
                    else None
                ),
            }
            for tier, plan in self._plans.items()
        }
