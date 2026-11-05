"""Independent admission control for the EcoleDirecte HTTP session."""

import asyncio
from datetime import UTC, datetime, timedelta
import random
import time
from typing import TYPE_CHECKING, Final, TypeVar

from ...const import QUIET_HOURS_EXEMPT, LimiterState, Priority  # noqa: TID252
from ...ratelimit import (  # noqa: TID252
    CAP_WARNING_FRACTION,
    DeferReason,
    LoginRefusedByLimiter,
    RateLimitConfig,
    TierDeferred,
    Verdict,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping
    from typing import Any

T = TypeVar("T")

ED_LOGIN_COST: Final = 2
ED_QCM_COST: Final = 4
ED_LOGIN_COST_KEY: Final = "login"
_SECONDS_PER_HOUR: Final = 3600.0
_SHED_THRESHOLD: Final = {
    Priority.CRITICAL: float("inf"),
    Priority.GESTURE: 1.0,
    Priority.HIGH: 1.0,
    Priority.NORMAL: CAP_WARNING_FRACTION,
    Priority.LOW: 0.6,
}


async def _sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


class EdRateLimiter:
    """Apply spacing, an hourly bucket, a daily cap, and ED-specific holds."""

    def __init__(
        self,
        config: RateLimitConfig,
        *,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] | None = None,
        sleep: Callable[[float], Awaitable[None]] = _sleep,
        rng: random.Random | None = None,
    ) -> None:
        self.config = config
        self._clock = clock
        self._now = now or (lambda: datetime.now(UTC))
        self._sleep = sleep
        self._rng = rng or random.Random()  # noqa: S311 -- backoff jitter
        self._admission = asyncio.Lock()
        self._last_call = clock() - config.min_request_interval
        self._tokens = float(config.burst_size)
        self._refilled = clock()
        self._day = self._now().date()
        self._calls_today = 0
        self._calls_by_tier: dict[str, int] = {}
        self._logins_today = 0
        self._failed_logins: list[float] = []
        self._hold_until: float | None = None
        self._hold_reason: DeferReason | None = None
        self._consecutive_failures = 0
        self._throttled_since: datetime | None = None
        self._cap_warning_open = False
        self._batch_started: float | None = None
        self._batch_started_in_quiet = False

    @property
    def calls_today(self) -> int:
        self._roll_day()
        return self._calls_today

    @property
    def failed_logins_last_hour(self) -> int:
        self._prune_failed_logins()
        return len(self._failed_logins)

    @property
    def calls_by_tier(self) -> dict[str, int]:
        self._roll_day()
        return dict(self._calls_by_tier)

    @property
    def logins_today(self) -> int:
        self._roll_day()
        return self._logins_today

    @property
    def tokens(self) -> float:
        self._refill()
        return self._tokens

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    @property
    def throttled(self) -> bool:
        return self._throttled_since is not None

    @property
    def throttled_since(self) -> datetime | None:
        return self._throttled_since

    @property
    def cap_warning_open(self) -> bool:
        return self._cap_warning_open

    @property
    def hold_until_wallclock(self) -> datetime | None:
        if not self._hold_active():
            return None
        seconds = max(0.0, (self._hold_until or self._clock()) - self._clock())
        return self._now() + timedelta(seconds=seconds)

    @property
    def state(self) -> LimiterState:
        if self._hold_active():
            if self._hold_reason is DeferReason.CREDENTIALS_HOLD:
                return LimiterState.CREDENTIALS_HOLD
            if self._hold_reason is DeferReason.BOOTSTRAP_HOLD:
                return LimiterState.BOOTSTRAP_FAILED
            return LimiterState.BACKOFF
        if self._in_quiet_hours():
            return LimiterState.QUIET_HOURS
        if self._throttled_since is not None:
            return LimiterState.THROTTLED
        return LimiterState.NOMINAL

    def _roll_day(self) -> None:
        today = self._now().date()
        if today > self._day:
            self._calls_today = 0
            self._calls_by_tier.clear()
            self._logins_today = 0
            self._cap_warning_open = False
        self._day = today

    def _refill(self) -> None:
        now = self._clock()
        rate = self.config.max_requests_per_hour / _SECONDS_PER_HOUR
        self._tokens = min(
            float(self.config.burst_size),
            self._tokens + (now - self._refilled) * rate,
        )
        self._refilled = now

    def _wait(self, cost: int) -> float:
        self._refill()
        spacing = max(
            0.0,
            self.config.min_request_interval - (self._clock() - self._last_call),
        )
        if self._tokens >= cost:
            bucket = 0.0
        elif self.config.max_requests_per_hour <= 0:
            bucket = float("inf")
        else:
            bucket = (
                (cost - self._tokens)
                * _SECONDS_PER_HOUR
                / self.config.max_requests_per_hour
            )
        return max(spacing, bucket)

    def _record(self, key: str, cost: int) -> None:
        self._roll_day()
        self._refill()
        self._calls_today += cost
        self._calls_by_tier[key] = self._calls_by_tier.get(key, 0) + cost
        self._tokens -= cost
        self._last_call = self._clock()

    def _hold_active(self) -> bool:
        if self._hold_until is None:
            return False
        if self._clock() < self._hold_until:
            return True
        self._hold_until = None
        self._hold_reason = None
        self._throttled_since = None
        return False

    def _start_hold(self, reason: DeferReason, seconds: float) -> None:
        self._hold_reason = reason
        self._hold_until = self._clock() + seconds
        self._throttled_since = self._throttled_since or self._now()

    def _prune_failed_logins(self) -> None:
        cutoff = self._clock() - _SECONDS_PER_HOUR
        self._failed_logins = [stamp for stamp in self._failed_logins if stamp > cutoff]

    def _in_quiet_hours(self) -> bool:
        if not self.config.quiet_hours_enabled:
            return False
        wall_time = self._now().timetz().replace(tzinfo=None)
        start = self.config.quiet_start
        end = self.config.quiet_end
        if start == end:
            return False
        if start > end:
            return wall_time >= start or wall_time < end
        return start <= wall_time < end

    def _seconds_to_midnight(self) -> float:
        now = self._now()
        tomorrow = (now + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        return (tomorrow.astimezone(UTC) - now.astimezone(UTC)).total_seconds()

    def _seconds_to_quiet_end(self) -> float:
        now = self._now()
        end = self.config.quiet_end
        candidate = now.replace(
            hour=end.hour, minute=end.minute, second=end.second, microsecond=0
        )
        if candidate <= now:
            candidate += timedelta(days=1)
        return (candidate.astimezone(UTC) - now.astimezone(UTC)).total_seconds()

    def begin_batch(self) -> None:
        self._batch_started = self._clock()
        self._batch_started_in_quiet = self._in_quiet_hours()

    def end_batch(self) -> None:
        self._batch_started = None
        self._batch_started_in_quiet = False

    def _batch_may_finish(self) -> bool:
        return (
            self._batch_started is not None
            and not self._batch_started_in_quiet
            and self._clock() - self._batch_started <= 300
        )

    def quiet_seconds_between(self, start: datetime, end: datetime) -> float:
        if not self.config.quiet_hours_enabled or end <= start:
            return 0.0
        total = 0.0
        day = start.date() - timedelta(days=1)
        while day <= end.date():
            opens = datetime.combine(day, self.config.quiet_start).replace(
                tzinfo=start.tzinfo
            )
            closes_day = (
                day + timedelta(days=1)
                if self.config.quiet_start > self.config.quiet_end
                else day
            )
            closes = datetime.combine(closes_day, self.config.quiet_end).replace(
                tzinfo=start.tzinfo
            )
            overlap_start = max(start, opens)
            overlap_end = min(end, closes)
            if overlap_end > overlap_start:
                total += (
                    overlap_end.astimezone(UTC) - overlap_start.astimezone(UTC)
                ).total_seconds()
            day += timedelta(days=1)
        return total

    def _verdict(self, priority: Priority, cost: int) -> Verdict:
        if self._hold_active():
            return Verdict(
                allowed=False,
                wait=max(0.0, (self._hold_until or self._clock()) - self._clock()),
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
        self._roll_day()
        fraction = self._calls_today / self.config.max_requests_per_day
        if fraction >= CAP_WARNING_FRACTION:
            self._cap_warning_open = True
        if fraction >= _SHED_THRESHOLD[priority]:
            self._throttled_since = self._throttled_since or self._now()
            return Verdict(
                allowed=False,
                wait=self._seconds_to_midnight(),
                reason=DeferReason.DAILY_CAP,
            )
        wait = self._wait(cost)
        if wait > self.config.max_wait:
            self._throttled_since = self._throttled_since or self._now()
            return Verdict(allowed=False, wait=wait, reason=DeferReason.HOURLY_BUDGET)
        return Verdict(allowed=True, wait=wait)

    async def _admit(self, key: str, priority: Priority, cost: int) -> None:
        async with self._admission:
            verdict = self._verdict(priority, cost)
            if not verdict.allowed:
                raise TierDeferred(
                    verdict.reason or DeferReason.HOURLY_BUDGET, verdict.wait
                )
            if verdict.wait:
                await self._sleep(verdict.wait)
            self._record(key, cost)

    async def call(
        self,
        key: str,
        priority: Priority,
        operation: Callable[[], Awaitable[T]],
        *,
        cost: int = 1,
    ) -> T:
        await self._admit(key, priority, cost)
        result = await operation()
        self.note_success()
        return result

    async def login(
        self,
        operation: Callable[[], Awaitable[T]],
        *,
        cost: int = ED_LOGIN_COST,
    ) -> T:
        self._roll_day()
        if self._logins_today >= self.config.max_logins_per_day:
            raise LoginRefusedByLimiter(
                DeferReason.LOGIN_CAP, self._seconds_to_midnight()
            )
        try:
            await self._admit(ED_LOGIN_COST_KEY, Priority.CRITICAL, cost)
        except TierDeferred as error:
            raise LoginRefusedByLimiter(error.reason, error.retry_after) from error
        self._logins_today += 1
        return await operation()

    async def charge_qcm(self) -> None:
        await self._admit(ED_LOGIN_COST_KEY, Priority.CRITICAL, ED_QCM_COST)

    async def reconcile_login(self, *, charged: int, actual: int) -> None:
        """Charge an unexpectedly expensive login without refunding optimism."""
        if actual > charged:
            await self._admit(ED_LOGIN_COST_KEY, Priority.CRITICAL, actual - charged)

    def note_bad_credentials(self) -> None:
        self._failed_logins.append(self._clock())
        self._prune_failed_logins()
        if len(self._failed_logins) >= self.config.max_failed_logins_per_hour:
            self._start_hold(DeferReason.CREDENTIALS_HOLD, self.config.credentials_hold)

    def note_qcm(self) -> None:
        """A 250 is a challenge, not a 505: do not arm credentials_hold."""
        return

    def note_transport_failure(self, *, bootstrap: bool = False) -> None:
        self._consecutive_failures += 1
        nominal = min(
            self.config.backoff_max,
            self.config.backoff_base * 2 ** (self._consecutive_failures - 1),
        )
        self._start_hold(
            DeferReason.BOOTSTRAP_HOLD if bootstrap else DeferReason.BACKOFF,
            nominal * (0.5 + self._rng.random()),
        )

    def note_success(self) -> None:
        self._consecutive_failures = 0
        if self._hold_reason is DeferReason.BACKOFF:
            self._hold_until = None
            self._hold_reason = None
            self._throttled_since = None

    def retry_delay(self) -> float:
        if self._hold_active():
            return max(0.0, (self._hold_until or self._clock()) - self._clock())
        return self.config.backoff_base

    def reset_after_reauth(self) -> None:
        self._failed_logins.clear()
        self._hold_until = None
        self._hold_reason = None
        self._throttled_since = None

    def export_state(self) -> dict[str, Any]:
        self._roll_day()
        return {
            "day": self._day.isoformat(),
            "calls_today": self._calls_today,
            "calls_by_tier": dict(self._calls_by_tier),
            "logins_today": self._logins_today,
            "failed_logins": list(self._failed_logins),
            "hold_until": self._hold_until,
            "hold_reason": str(self._hold_reason) if self._hold_reason else None,
            "cap_warning_open": self._cap_warning_open,
        }

    def import_state(self, state: Mapping[str, Any]) -> None:
        if state.get("day") == self._day.isoformat():
            self._calls_today = int(state.get("calls_today", 0))
            self._calls_by_tier = {
                str(key): int(value)
                for key, value in dict(state.get("calls_by_tier") or {}).items()
            }
            self._logins_today = int(state.get("logins_today", 0))
            self._cap_warning_open = bool(state.get("cap_warning_open", False))
        self._failed_logins = [float(value) for value in state.get("failed_logins", ())]
        hold_until = state.get("hold_until")
        if isinstance(hold_until, (int, float)) and hold_until > self._clock():
            self._hold_until = float(hold_until)
            reason = state.get("hold_reason")
            self._hold_reason = next(
                (item for item in DeferReason if str(item) == reason), None
            )

    def snapshot_counters(self) -> dict[str, Any]:
        reason = self._hold_reason if self._hold_active() else None
        return {
            "calls_today": self.calls_today,
            "calls_by_tier": dict(self._calls_by_tier),
            "logins_today": self._logins_today,
            "failed_logins_hour": self.failed_logins_last_hour,
            "daily_cap": self.config.max_requests_per_day,
            "remaining_today": max(
                0, self.config.max_requests_per_day - self._calls_today
            ),
            "state": str(self.state),
            "reason": str(reason) if reason else None,
            "consecutive_failures": self._consecutive_failures,
            "throttled": self._throttled_since is not None,
        }
