from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol, TypeGuard

from ..const import LimiterState, Tier  # noqa: TID252

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date, datetime

    from ..const import Priority  # noqa: TID252
    from ..gateway import PronoteGateway  # noqa: TID252
    from ..models import GatewayResult, SessionFacts  # noqa: TID252
    from ..ratelimit import RateLimitConfig  # noqa: TID252
    from ..session import SerialExecutor, SessionManager  # noqa: TID252


class Source(StrEnum):
    PRONOTE = "pronote"
    ECOLEDIRECTE = "ecoledirecte"


class ChallengeKind(StrEnum):
    PIN = "pin"
    QCM = "qcm"


@dataclass(frozen=True, slots=True)
class ConnectorCapabilities:
    source: Source
    tiers: frozenset[Tier]
    writes: frozenset[str]
    services: frozenset[str]


class LimiterView(Protocol):
    """Read-only budget surface shared by every source's limiter."""

    @property
    def calls_today(self) -> int: ...

    @property
    def logins_today(self) -> int: ...

    @property
    def failed_logins_last_hour(self) -> int: ...

    @property
    def calls_by_tier(self) -> dict[str, int]: ...

    @property
    def tokens(self) -> float: ...

    @property
    def throttled(self) -> bool: ...

    @property
    def throttled_since(self) -> datetime | None: ...

    @property
    def consecutive_failures(self) -> int: ...

    @property
    def hold_until_wallclock(self) -> datetime | None: ...

    @property
    def state(self) -> LimiterState: ...

    @property
    def config(self) -> RateLimitConfig: ...

    @property
    def cap_warning_open(self) -> bool: ...

    def snapshot_counters(self) -> dict[str, Any]: ...

    def begin_batch(self) -> None: ...

    def end_batch(self) -> None: ...

    def quiet_seconds_between(self, start: datetime, end: datetime) -> float: ...

    def retry_delay(self) -> float: ...

    def export_state(self) -> dict[str, Any]: ...


class PronoteExtras(Protocol):
    """PRONOTE-only façade: gateway, session lock, serial executor."""

    gateway: PronoteGateway
    session: SessionManager
    executor: SerialExecutor


def has_pronote_extras(connector: object) -> TypeGuard[PronoteExtras]:
    """Whether the connector owns the PRONOTE session/gateway façade."""
    return (
        hasattr(connector, "session")
        and hasattr(connector, "gateway")
        and hasattr(connector, "executor")
    )


class SchoolConnector(Protocol):
    capabilities: ConnectorCapabilities
    limiter: LimiterView

    def now(self) -> datetime: ...

    def today(self) -> date: ...

    async def async_open(self) -> None: ...

    async def async_load_session_facts(self, student_ids: Sequence[str]) -> None: ...

    def session_facts(self, student_id: str) -> SessionFacts: ...

    def student_ids(self) -> tuple[str, ...]: ...

    def announced_children(self) -> tuple[tuple[str, str], ...]: ...

    async def async_collect(
        self,
        tier: Tier,
        student_id: str,
        *,
        priority: Priority,
    ) -> GatewayResult[Any]: ...

    async def async_close(self) -> None: ...

    def diagnostics(self) -> dict[str, Any]: ...
