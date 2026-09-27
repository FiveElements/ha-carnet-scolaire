"""Read-only SchoolConnector implementation for EcoleDirecte."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
import time
from typing import TYPE_CHECKING, Any, Final
from zoneinfo import ZoneInfo

from ...const import Priority, Tier  # noqa: TID252
from ...models import GatewayResult, SessionFacts  # noqa: TID252
from ...ratelimit import RateLimitConfig  # noqa: TID252
from ..errors import (  # noqa: TID252
    ConnectorChallengeRequired,
    ConnectorChildMissingError,
    ConnectorCredentialsError,
    ConnectorTransportError,
    ConnectorUnsupportedError,
)
from ..protocol import ConnectorCapabilities, Source  # noqa: TID252
from .ed_limiter import EdRateLimiter
from .ed_mapping import (
    attendance_facts,
    current_period_from_notes,
    homework_facts,
    marks_facts,
    periods_from_notes,
    session_facts_from_login,
    student_login_ids_from_accounts,
    timetable_facts,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from .ed_client import EcoleDirecteClient

_TIERS: Final = frozenset({Tier.TIMETABLE, Tier.HOMEWORK, Tier.MARKS, Tier.ATTENDANCE})


def _default_config() -> RateLimitConfig:
    """Return deterministic direct-construction defaults for injected clients."""
    return RateLimitConfig(min_request_interval=0, quiet_hours_enabled=False)


class EcoledirecteConnector:
    """Own one mutable ED token and serialize every request made with it."""

    CAPABILITIES: Final = ConnectorCapabilities(
        source=Source.ECOLEDIRECTE,
        tiers=_TIERS,
        writes=frozenset(),
        services=frozenset(),
    )

    def __init__(
        self,
        *,
        client: EcoleDirecteClient,
        zone: str | None = None,
        timezone: str | None = None,
        username: str | None = None,
        password: str | None = None,
        qcm_json: Mapping[str, Any] | None = None,
        limiter: EdRateLimiter | None = None,
        rate_limit_config: RateLimitConfig | None = None,
        limiter_state: Mapping[str, Any] | None = None,
        read_timeout: float | None = None,
        **_unused: Any,
    ) -> None:
        self.client = client
        if read_timeout is not None:
            self.client.configure_read_timeout(read_timeout)
        self._zone = ZoneInfo(zone or timezone or "UTC")
        self._username = username or ""
        self._password = password or ""
        self._qcm_json = qcm_json
        self.limiter = limiter or EdRateLimiter(
            rate_limit_config or _default_config(),
            now=self.now,
        )
        if limiter_state:
            self.limiter.import_state(limiter_state)
        self._lock = asyncio.Lock()
        self._student_ids: tuple[str, ...] = ()
        self._login_ids: dict[str, str] = {}
        self._session_facts: dict[str, SessionFacts] = {}
        self._current_login_id: str | None = None
        self._authenticated_at: float | None = None

    @property
    def capabilities(self) -> ConnectorCapabilities:
        return self.CAPABILITIES

    def now(self) -> datetime:
        return datetime.now(self._zone)

    def today(self) -> date:
        return self.now().date()

    async def async_open(self) -> None:
        """Authenticate once and derive zero-call session facts from login."""
        if self.client.token is not None and self._student_ids:
            return
        calls_before = self.client.calls
        try:
            payload = await self.limiter.login(
                lambda: self.client.login(
                    self._username,
                    self._password,
                    self._qcm_json,
                    charge_qcm=self.limiter.charge_qcm
                    if self._qcm_json is not None
                    else None,
                ),
                cost=2,
            )
        except ConnectorCredentialsError:
            self.limiter.note_bad_credentials()
            raise
        except ConnectorChallengeRequired:
            if self._qcm_json is None:
                await self.limiter.charge_qcm()
            self.limiter.note_qcm()
            raise
        except ConnectorTransportError:
            self.limiter.note_transport_failure(bootstrap=True)
            raise
        else:
            self.limiter.note_success()
        actual = self.client.calls - calls_before
        admitted = 2 + (4 if actual > 2 else 0)
        await self.limiter.reconcile_login(charged=admitted, actual=actual)
        self._authenticated_at = time.monotonic()
        facts = session_facts_from_login(payload)
        self._session_facts = {item.student.id: item for item in facts}
        self._student_ids = tuple(self._session_facts)
        self._login_ids = student_login_ids_from_accounts(payload)
        login_ids = set(self._login_ids.values())
        self._current_login_id = next(iter(login_ids)) if len(login_ids) == 1 else None

    async def async_load_session_facts(self, student_ids: Sequence[str]) -> None:
        """Validate selected children; login already supplied their facts."""
        for student_id in student_ids:
            self.session_facts(student_id)

    def student_ids(self) -> tuple[str, ...]:
        return self._student_ids

    def announced_children(self) -> tuple[tuple[str, str], ...]:
        """Every child the login announced, as ``(id, name)``."""
        return tuple(
            (student_id, self._session_facts[student_id].student.name)
            for student_id in self._student_ids
        )

    def session_facts(self, student_id: str) -> SessionFacts:
        try:
            return self._session_facts[student_id]
        except KeyError as error:
            raise ConnectorChildMissingError(student_id) from error

    def session_update(self, student_id: str) -> GatewayResult[SessionFacts]:
        """Return the in-memory SESSION republish produced by MARKS."""
        return GatewayResult(self.session_facts(student_id), calls=0)

    async def async_collect(
        self,
        tier: Tier,
        student_id: str,
        *,
        priority: Priority,
    ) -> GatewayResult[Any]:
        if tier not in self.CAPABILITIES.tiers:
            raise ConnectorUnsupportedError(f"unsupported tier: {tier}")
        if student_id not in self._session_facts:
            raise ConnectorChildMissingError(student_id)

        async with self._lock:
            await self.async_open()
            await self._select_student(student_id)
            calls_before = self.client.calls
            try:
                payload = await self.limiter.call(
                    str(tier),
                    priority,
                    lambda: self.client.request(
                        self._path(tier, student_id),
                        verbe="get",
                        data=self._request_data(tier),
                    ),
                )
            except ConnectorTransportError:
                self.limiter.note_transport_failure()
                raise
            calls = self.client.calls - calls_before
            facts = self._map(tier, student_id, payload)
            return GatewayResult(facts, calls=calls)

    async def _select_student(self, student_id: str) -> None:
        await self.async_open()
        login_id = self._login_ids[student_id]
        if login_id == self._current_login_id:
            return
        try:
            await self.limiter.call(
                "login",
                Priority.CRITICAL,
                lambda: self.client.request(
                    "/renewtoken.awp",
                    verbe="post",
                    data={"idUser": login_id},
                ),
            )
        except ConnectorTransportError:
            self.limiter.note_transport_failure()
            raise
        self._current_login_id = login_id

    def _request_data(self, tier: Tier) -> dict[str, Any]:
        """Only TIMETABLE posts a date window; other capable tiers stay empty."""
        if tier is not Tier.TIMETABLE:
            return {}
        today = self.today()
        monday = today - timedelta(days=today.weekday())
        sunday_next = monday + timedelta(days=13)
        return {
            "dateDebut": monday.isoformat(),
            "dateFin": sunday_next.isoformat(),
            "avecTrous": False,
        }

    @staticmethod
    def _path(tier: Tier, student_id: str) -> str:
        return {
            Tier.TIMETABLE: f"/E/{student_id}/emploidutemps.awp",
            Tier.HOMEWORK: f"/Eleves/{student_id}/cahierdetexte.awp",
            Tier.MARKS: f"/eleves/{student_id}/notes.awp",
            Tier.ATTENDANCE: f"/eleves/{student_id}/viescolaire.awp",
        }[tier]

    def _map(self, tier: Tier, student_id: str, payload: object) -> object:
        if tier is Tier.TIMETABLE:
            return timetable_facts(payload, zone=self._zone)
        if tier is Tier.HOMEWORK:
            return homework_facts(payload)
        if tier is Tier.MARKS:
            periods = periods_from_notes(payload, zone=self._zone)
            current = current_period_from_notes(payload, zone=self._zone)
            previous = self.session_facts(student_id)
            self._session_facts[student_id] = SessionFacts(
                student=previous.student,
                periods=periods,
                current_period=current,
            )
            return marks_facts(
                payload,
                current_period_id=current.id if current is not None else "",
            )
        return attendance_facts(payload, zone=self._zone)

    async def async_close(self) -> None:
        """The caller owns the injected aiohttp transport."""

    def diagnostics(self) -> dict[str, Any]:
        return {
            "limiter": self.limiter.snapshot_counters(),
            "session": {
                "authenticated": self.client.token is not None,
                "token_age_seconds": (
                    max(0.0, time.monotonic() - self._authenticated_at)
                    if self.client.token is not None
                    and self._authenticated_at is not None
                    else None
                ),
            },
        }
