"""A pair of simulated clocks, because the code under test takes two.

``RateLimiter``, ``FetchScheduler`` and ``SessionManager`` all accept a
monotonic ``clock`` and a wall-clock ``now`` as constructor arguments rather
than reaching for ``time.monotonic`` and ``dt_util.now`` themselves. That is
the seam this module drives: a test advances time by an hour in one statement,
with no ``freezegun`` and no sleeping, and every branch that depends on elapsed
time becomes reachable.

The two clocks advance **together** by default, which is the realistic case.
Tests that need them to disagree -- an NTP correction stepping the wall clock
backwards while the monotonic clock does not move -- move them separately, and
that asymmetry is exactly what makes ``_roll_day``'s backwards branch testable.
"""

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from zoneinfo import ZoneInfo


class FakeClock:
    """A monotonic clock and a wall clock a test can drive by hand."""

    def __init__(
        self,
        start: datetime | None = None,
        *,
        monotonic: float = 1_000.0,
    ) -> None:
        self._wall = (
            start if start is not None else datetime(2026, 3, 12, 8, 0, tzinfo=UTC)
        )
        self._monotonic = monotonic

    # -- reading -----------------------------------------------------------

    def monotonic(self) -> float:
        """The monotonic source, for spacing, refill and holds."""
        return self._monotonic

    def now(self) -> datetime:
        """The wall clock, for quiet hours and the midnight rollover."""
        return self._wall

    # -- driving -----------------------------------------------------------

    def advance(self, seconds: float) -> None:
        """Move both clocks forward, which is what really happens."""
        self._monotonic += seconds
        self._wall += timedelta(seconds=seconds)

    def advance_monotonic(self, seconds: float) -> None:
        """Move only the monotonic clock."""
        self._monotonic += seconds

    def set_wall(self, moment: datetime) -> None:
        """Put the wall clock at an absolute instant, forwards or backwards.

        The backwards direction is not a curiosity: an NTP correction or a
        machine whose clock was wrong at boot both produce it, and the daily
        counters must not be re-granted when it happens.
        """
        self._wall = moment

    def at(self, hour: int, minute: int = 0, *, zone: ZoneInfo | None = None) -> None:
        """Put the wall clock at a time of day, keeping the date."""
        self._wall = self._wall.replace(
            hour=hour, minute=minute, second=0, microsecond=0
        )
        if zone is not None:
            self._wall = self._wall.replace(tzinfo=zone)


class RecordingSleeper:
    """A sleeper that advances a :class:`FakeClock` instead of waiting.

    Substituted for ``asyncio.sleep`` through the limiter's ``sleep`` seam. It
    records what it was asked for, so a test can assert *how long* a caller was
    made to wait -- which is the observable behaviour of the spacing layer, and
    the only one that matters.
    """

    def __init__(self, clock: FakeClock) -> None:
        self._clock = clock
        self.slept: list[float] = []

    async def __call__(self, seconds: float) -> None:
        """Record the request and advance the simulated clock."""
        self.slept.append(seconds)
        self._clock.advance(seconds)

    @property
    def total(self) -> float:
        """Everything slept so far."""
        return sum(self.slept)
