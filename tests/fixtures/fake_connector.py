from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

from custom_components.carnet_scolaire.connectors.errors import (
    ConnectorUnsupportedError,
)
from custom_components.carnet_scolaire.connectors.protocol import (
    ConnectorCapabilities,
    SchoolConnector,
    Source,
)
from custom_components.carnet_scolaire.const import Priority, Tier
from custom_components.carnet_scolaire.models import (
    GatewayResult,
    SessionFacts,
    Student,
)

_DEFAULT_NOW = dt.datetime(2026, 3, 12, 8, 0, tzinfo=dt.UTC)


class FakeConnector(SchoolConnector):
    def __init__(
        self,
        *,
        capabilities: ConnectorCapabilities | None = None,
        facts_by_tier: Mapping[Tier, Any] | None = None,
        cost_by_tier: Mapping[Tier, int] | None = None,
        now: Callable[[], dt.datetime] | None = None,
        today: Callable[[], dt.date] | None = None,
    ) -> None:
        self._facts_by_tier = dict(facts_by_tier or {})
        self.calls_by_tier = dict(cost_by_tier or {})
        self._now = now or (lambda: _DEFAULT_NOW)
        self._today = today or (lambda: self._now().date())
        self.capabilities = capabilities or ConnectorCapabilities(
            source=Source.PRONOTE,
            tiers=frozenset(
                tier for tier in self._facts_by_tier if tier is not Tier.SESSION
            ),
            writes=frozenset(),
            services=frozenset(),
        )
        self.limiter = object()
        self._session_facts = SessionFacts(
            student=Student(
                id="STUDENT-1",
                name="Enfant Un",
                class_name=None,
                establishment=None,
                has_photo=False,
            ),
            periods=(),
            current_period=None,
        )

    def now(self) -> dt.datetime:
        return self._now()

    def today(self) -> dt.date:
        return self._today()

    async def async_open(self) -> None:
        return None

    async def async_load_session_facts(self, student_ids: Sequence[str]) -> None:
        del student_ids

    def session_facts(self, student_id: str) -> SessionFacts:
        return self._session_facts

    def student_ids(self) -> tuple[str, ...]:
        return ("STUDENT-1",)

    def announced_children(self) -> tuple[tuple[str, str], ...]:
        return (("STUDENT-1", self._session_facts.student.name),)

    async def async_collect(
        self,
        tier: Tier,
        student_id: str,
        *,
        priority: Priority,
    ) -> GatewayResult[Any]:
        del student_id, priority
        if tier is Tier.SESSION or tier not in self.capabilities.tiers:
            raise ConnectorUnsupportedError(f"unsupported tier: {tier}")
        return GatewayResult(self._facts_by_tier[tier], self.calls_by_tier[tier])

    async def async_close(self) -> None:
        return None

    def diagnostics(self) -> dict[str, Any]:
        return {
            "source": self.capabilities.source,
            "tiers": tuple(str(tier) for tier in sorted(self.capabilities.tiers)),
            "calls_by_tier": {
                str(tier): calls for tier, calls in self.calls_by_tier.items()
            },
        }
