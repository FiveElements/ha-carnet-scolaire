"""EcoleDirecte connector session, limiter, and collection contract."""

# ruff: noqa: PT018 -- the two plan tests are copied verbatim.

import asyncio
import base64
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
import json
import random
from urllib.parse import parse_qs
from zoneinfo import ZoneInfo

import pytest

from custom_components.carnet_scolaire.connectors.ecoledirecte.connector import (
    EcoledirecteConnector,
)
from custom_components.carnet_scolaire.connectors.ecoledirecte.ed_client import (
    EcoleDirecteClient,
)
from custom_components.carnet_scolaire.connectors.ecoledirecte.ed_limiter import (
    EdRateLimiter,
    _sleep,
)
from custom_components.carnet_scolaire.connectors.errors import (
    ConnectorChallengeRequired,
    ConnectorCredentialsError,
    ConnectorError,
    ConnectorTransportError,
    ConnectorUnsupportedError,
)
from custom_components.carnet_scolaire.connectors.protocol import Source
from custom_components.carnet_scolaire.const import LimiterState, Priority, Tier
from custom_components.carnet_scolaire.ratelimit import (
    DeferReason,
    LoginRefusedByLimiter,
    RateLimitConfig,
    RateLimiter,
    TierDeferred,
)
from tests.test_ecoledirecte_client import RecordingTransport, load_fixture


# fmt: off
@pytest.mark.asyncio
async def test_timetable_collect_declares_the_posts_the_client_actually_made() -> None:
    """A declared 1 with two POSTs is the lazy-property bug, on a new protocol."""
    client = EcoleDirecteClient(
        RecordingTransport.scripted(
            [
                ("GET", "login.awp", {"gtk": "gtk-demo"}),
                ("POST", "login.awp", load_fixture("login_ok.json")),
                ("POST", "emploidutemps.awp", load_fixture("emploi_du_temps.json")),
            ]
        )
    )
    connector = EcoledirecteConnector(client=client, zone="Europe/Paris")
    await connector.async_open()
    calls_before = client.calls
    result = await connector.async_collect(
        Tier.TIMETABLE, "1", priority=Priority.HIGH
    )
    assert result.calls == client.calls - calls_before
    assert result.facts.lessons[0].start.tzinfo is not None


@pytest.mark.asyncio
async def test_a_timetable_post_asks_monday_through_the_following_sunday() -> None:
    """One EDT window in the connector timezone, not an empty body."""
    transport = RecordingTransport.scripted(
        [
            ("GET", "login.awp", {"gtk": "gtk-demo"}),
            ("POST", "login.awp", load_fixture("login_ok.json")),
            ("POST", "emploidutemps.awp", load_fixture("emploi_du_temps.json")),
        ]
    )
    client = EcoleDirecteClient(transport)
    connector = EcoledirecteConnector(client=client, zone="Europe/Paris")
    connector.now = lambda: datetime(2026, 9, 11, 15, tzinfo=ZoneInfo("Europe/Paris"))
    await connector.async_open()

    await connector.async_collect(Tier.TIMETABLE, "1", priority=Priority.HIGH)

    body = json.loads(parse_qs(str(transport.data_on[2]))["data"][0])
    assert body == {
        "dateDebut": "2026-09-07",
        "dateFin": "2026-09-20",
        "avecTrous": False,
    }


@pytest.mark.asyncio
async def test_a_520_on_collect_relogs_on_the_next_collect() -> None:
    """Forgetting the token is useless unless the next collect bills a full login."""
    transport = RecordingTransport.scripted(
        [
            ("GET", "login.awp", {"gtk": "gtk-demo"}),
            ("POST", "login.awp", load_fixture("login_ok.json")),
            ("POST", "emploidutemps.awp", {"code": 520}),
            ("GET", "login.awp", {"gtk": "gtk-relogin"}),
            ("POST", "login.awp", load_fixture("login_ok.json")),
            ("POST", "emploidutemps.awp", load_fixture("emploi_du_temps.json")),
        ]
    )
    client = EcoleDirecteClient(transport)
    connector = EcoledirecteConnector(client=client, zone="Europe/Paris")
    await connector.async_open()
    logins_after_open = connector.limiter.logins_today

    with pytest.raises(ConnectorError):
        await connector.async_collect(Tier.TIMETABLE, "1", priority=Priority.HIGH)

    assert client.token is None
    result = await connector.async_collect(Tier.TIMETABLE, "1", priority=Priority.HIGH)

    assert connector.limiter.logins_today == logins_after_open + 1
    assert result.calls == 1
    assert client.token is not None


def test_the_connector_applies_the_entry_read_timeout_to_the_client() -> None:
    """The deadline lives on the client; dropping it leaves 60 s forever."""
    client = EcoleDirecteClient(RecordingTransport.scripted([]), read_timeout=60.0)
    EcoledirecteConnector(client=client, zone="Europe/Paris", read_timeout=12.0)
    assert client.read_timeout == 12.0


@pytest.mark.asyncio
async def test_two_collects_on_one_connector_do_not_run_concurrently() -> None:
    """The token and GTK cookie are mutable; overlapping POSTs are a session bug."""
    import asyncio

    order: list[str] = []

    class SlowTransport(RecordingTransport):
        async def request(self, method: str, url: str, **kwargs: object) -> object:
            order.append("start")
            await asyncio.sleep(0.02)
            response = await super().request(method, url, **kwargs)
            order.append("end")
            return response

    transport = SlowTransport.scripted(
        [
            ("GET", "login.awp", {"gtk": "gtk-demo"}),
            ("POST", "login.awp", load_fixture("login_ok.json")),
            ("POST", "emploidutemps.awp", load_fixture("emploi_du_temps.json")),
            ("POST", "emploidutemps.awp", load_fixture("emploi_du_temps.json")),
        ]
    )
    connector = EcoledirecteConnector(
        client=EcoleDirecteClient(transport), zone="Europe/Paris"
    )
    await connector.async_open()
    first, second = await asyncio.gather(
        connector.async_collect(Tier.TIMETABLE, "1", priority=Priority.HIGH),
        connector.async_collect(Tier.TIMETABLE, "1", priority=Priority.HIGH),
    )
    collect_order = order[4:]  # skip GTK + login (2 requests = 4 start/end)
    assert collect_order == ["start", "end", "start", "end"]
    assert first.calls >= 1 and second.calls >= 1
# fmt: on


def _config(**changes: object) -> RateLimitConfig:
    """Build a permissive limiter configuration with focused overrides."""
    defaults: dict[str, object] = {
        "min_request_interval": 0,
        "max_requests_per_hour": 3600,
        "burst_size": 20,
        "max_requests_per_day": 2000,
        "max_wait": 60,
        "max_logins_per_day": 24,
        "max_failed_logins_per_hour": 3,
        "credentials_hold": 3600,
        "bootstrap_hold": 3600,
        "backoff_base": 30,
        "backoff_max": 3600,
        "quiet_hours_enabled": False,
    }
    defaults.update(changes)
    return RateLimitConfig(**defaults)  # type: ignore[arg-type]


@dataclass
class ManualClock:
    """Advance monotonic and wall clocks together without real sleeps."""

    elapsed: float = 0
    wall: datetime = datetime(2026, 9, 11, 12, tzinfo=UTC)

    def monotonic(self) -> float:
        return self.elapsed

    def now(self) -> datetime:
        return self.wall + timedelta(seconds=self.elapsed)

    async def sleep(self, seconds: float) -> None:
        self.elapsed += seconds


@pytest.mark.asyncio
async def test_a_505_holds_only_the_ecoledirecte_entry_that_failed() -> None:
    """A wrong ED password must not stop a neighboring account."""
    first = EdRateLimiter(_config(max_failed_logins_per_hour=1))
    second = EdRateLimiter(_config(max_failed_logins_per_hour=1))
    connector = EcoledirecteConnector(
        client=EcoleDirecteClient(
            RecordingTransport.scripted(
                [
                    ("GET", "login.awp", {"gtk": "gtk-demo"}),
                    ("POST", "login.awp", load_fixture("login_505.json")),
                ]
            )
        ),
        zone="Europe/Paris",
        limiter=first,
    )

    with pytest.raises(ConnectorCredentialsError):
        await connector.async_open()

    assert first.state is LimiterState.CREDENTIALS_HOLD
    assert second.state is LimiterState.NOMINAL


@pytest.mark.asyncio
async def test_a_505_does_not_touch_the_pronote_login_guard() -> None:
    """Mixed households have separate failed-login counters."""
    pronote = RateLimiter(
        _config(max_failed_logins_per_hour=1),
        clock=lambda: 0,
        now=lambda: datetime(2026, 9, 11, 12, tzinfo=UTC),
    )
    connector = EcoledirecteConnector(
        client=EcoleDirecteClient(
            RecordingTransport.scripted(
                [
                    ("GET", "login.awp", {"gtk": "gtk-demo"}),
                    ("POST", "login.awp", load_fixture("login_505.json")),
                ]
            )
        ),
        zone="Europe/Paris",
        limiter=EdRateLimiter(_config(max_failed_logins_per_hour=1)),
    )

    with pytest.raises(ConnectorCredentialsError):
        await connector.async_open()

    assert pronote.failed_logins_last_hour == 0
    assert pronote.state is LimiterState.NOMINAL


@pytest.mark.asyncio
async def test_a_250_does_not_increment_failed_logins() -> None:
    """A required quiz is not evidence that the password is wrong."""
    limiter = EdRateLimiter(_config())
    connector = EcoledirecteConnector(
        client=EcoleDirecteClient(
            RecordingTransport.scripted(
                [
                    ("GET", "login.awp", {"gtk": "gtk-demo"}),
                    ("POST", "login.awp", load_fixture("login_250.json")),
                ]
            )
        ),
        zone="Europe/Paris",
        limiter=limiter,
    )

    with pytest.raises(ConnectorChallengeRequired):
        await connector.async_open()

    assert limiter.failed_logins_last_hour == 0
    assert limiter.calls_today == 6


def test_note_qcm_does_not_arm_credentials_hold() -> None:
    """A 250 must not spend the credentials hold that blocks the next login."""
    limiter = EdRateLimiter(_config())
    limiter.note_qcm()
    assert limiter.state is LimiterState.NOMINAL


@pytest.mark.asyncio
async def test_async_open_replays_a_stored_qcm_answer() -> None:
    """Runtime open must receive qcm_json; credentials alone never reach 200."""
    question = "Couleur préférée ?"
    answer = "Bleu"
    encoded_question = base64.b64encode(question.encode()).decode()
    encoded_answer = base64.b64encode(answer.encode()).decode()
    client = EcoleDirecteClient(
        RecordingTransport.scripted(
            [
                ("GET", "login.awp", {"gtk": "gtk-demo"}),
                ("POST", "login.awp", load_fixture("login_250.json")),
                (
                    "POST",
                    "doubleauth.awp",
                    {
                        "code": 200,
                        "data": {
                            "question": encoded_question,
                            "propositions": [encoded_answer],
                        },
                    },
                ),
                (
                    "POST",
                    "doubleauth.awp",
                    {
                        "code": 200,
                        "data": {"cn": "not-a-real-cn", "cv": "not-a-real-cv"},
                    },
                ),
                ("GET", "login.awp", {"gtk": "gtk-after-qcm"}),
                ("POST", "login.awp", load_fixture("login_ok.json")),
            ]
        )
    )
    connector = EcoledirecteConnector(
        client=client,
        zone="Europe/Paris",
        username="demo.example.invalid",
        password="not-a-real-password",
        qcm_json={question: answer},
    )

    await connector.async_open()

    assert client.calls == 6
    assert connector.student_ids() == ("1",)


@pytest.mark.asyncio
@pytest.mark.parametrize("tier", [Tier.MENUS, Tier.SESSION])
async def test_an_uncollectable_tier_is_rejected(tier: Tier) -> None:
    """Capabilities must fail loudly if a caller still asks for an absent tier."""
    connector = EcoledirecteConnector(
        client=EcoleDirecteClient(RecordingTransport.scripted([])),
        zone="Europe/Paris",
    )

    with pytest.raises(ConnectorUnsupportedError):
        await connector.async_collect(tier, "1", priority=Priority.HIGH)


@pytest.mark.asyncio
async def test_a_login_waits_for_two_bucket_tokens_not_the_pronote_cost() -> None:
    """The PRONOTE five-request handshake must not leak into ED accounting."""
    elapsed = 0.0
    waits: list[float] = []

    async def sleep(seconds: float) -> None:
        nonlocal elapsed
        waits.append(seconds)
        elapsed += seconds

    limiter = EdRateLimiter(
        _config(burst_size=0),
        clock=lambda: elapsed,
        now=lambda: datetime(2026, 9, 11, 12, tzinfo=UTC),
        sleep=sleep,
    )

    await limiter.login(lambda: asyncio.sleep(0), cost=2)

    assert waits == [2.0]
    assert limiter.calls_today == 2


@pytest.mark.asyncio
async def test_marks_refresh_session_facts_without_an_extra_call() -> None:
    """Periods learned from notes must republish SESSION without another POST."""
    connector = EcoledirecteConnector(
        client=EcoleDirecteClient(
            RecordingTransport.scripted(
                [
                    ("GET", "login.awp", {"gtk": "gtk-demo"}),
                    ("POST", "login.awp", load_fixture("login_ok.json")),
                    ("POST", "notes.awp", load_fixture("notes.json")),
                ]
            )
        ),
        zone="Europe/Paris",
    )
    await connector.async_open()

    result = await connector.async_collect(Tier.MARKS, "1", priority=Priority.NORMAL)
    session = connector.session_facts("1")

    assert result.calls == 1
    assert session.current_period is not None
    assert session.current_period.id == "A001"
    assert connector.session_update("1").calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tier", "path", "fixture", "attribute"),
    [
        (Tier.HOMEWORK, "cahierdetexte.awp", "cahier_de_texte.json", "homework"),
        (Tier.ATTENDANCE, "viescolaire.awp", "vie_scolaire.json", "absences"),
    ],
)
async def test_each_remaining_ed_tier_places_one_mapped_post(
    tier: Tier, path: str, fixture: str, attribute: str
) -> None:
    """Homework and attendance must cross the same one-POST seam."""
    connector = EcoledirecteConnector(
        client=EcoleDirecteClient(
            RecordingTransport.scripted(
                [
                    ("GET", "login.awp", {"gtk": "gtk-demo"}),
                    ("POST", "login.awp", load_fixture("login_ok.json")),
                    ("POST", path, load_fixture(fixture)),
                ]
            )
        ),
        zone="Europe/Paris",
    )
    await connector.async_open()
    result = await connector.async_collect(tier, "1", priority=Priority.HIGH)
    assert result.calls == 1
    assert getattr(result.facts, attribute)


@pytest.mark.asyncio
async def test_a_memory_token_makes_the_second_open_a_zero_call_noop() -> None:
    """A scheduler tick must never relog merely to keep the token alive."""
    client = EcoleDirecteClient(
        RecordingTransport.scripted(
            [
                ("GET", "login.awp", {"gtk": "gtk-demo"}),
                ("POST", "login.awp", load_fixture("login_ok.json")),
            ]
        )
    )
    connector = EcoledirecteConnector(client=client, zone="Europe/Paris")
    await connector.async_open()
    before = client.calls
    await connector.async_open()
    assert client.calls == before


@pytest.mark.asyncio
async def test_switching_a_parent_child_charges_renewtoken_to_login() -> None:
    """A child context change is authentication cost, not timetable cost."""
    login = {
        "code": 200,
        "data": {
            "accounts": [
                {
                    "typeCompte": "P",
                    "profile": {
                        "eleves": [
                            {
                                "id": 1,
                                "idLogin": 101,
                                "prenom": "Enfant",
                                "nom": "Un",
                            },
                            {
                                "id": 2,
                                "idLogin": 102,
                                "prenom": "Enfant",
                                "nom": "Deux",
                            },
                        ]
                    },
                }
            ]
        },
    }
    connector = EcoledirecteConnector(
        client=EcoleDirecteClient(
            RecordingTransport.scripted(
                [
                    ("GET", "login.awp", {"gtk": "gtk-demo"}),
                    ("POST", "login.awp", login),
                    ("POST", "renewtoken.awp", {"code": 200}),
                    (
                        "POST",
                        "cahierdetexte.awp",
                        load_fixture("cahier_de_texte.json"),
                    ),
                ]
            )
        ),
        zone="Europe/Paris",
    )
    await connector.async_open()
    result = await connector.async_collect(Tier.HOMEWORK, "1", priority=Priority.HIGH)
    counters = connector.limiter.calls_by_tier
    assert result.calls == 1
    assert counters["login"] == 3
    assert counters["homework"] == 1


@pytest.mark.asyncio
async def test_a_renewtoken_transport_failure_enters_ed_backoff() -> None:
    """Child selection is a real POST and must not bypass transport punishment."""
    login = {
        "code": 200,
        "data": {
            "accounts": [
                {
                    "typeCompte": "P",
                    "profile": {
                        "eleves": [
                            {
                                "id": 1,
                                "idLogin": 101,
                                "prenom": "Enfant",
                                "nom": "Un",
                            },
                            {
                                "id": 2,
                                "idLogin": 102,
                                "prenom": "Enfant",
                                "nom": "Deux",
                            },
                        ]
                    },
                }
            ]
        },
    }

    class RenewFailureTransport(RecordingTransport):
        async def request(self, method: str, url: str, **kwargs: object) -> object:
            if "renewtoken.awp" in url:
                raise OSError("scripted renewtoken failure")
            return await super().request(method, url, **kwargs)

    connector = EcoledirecteConnector(
        client=EcoleDirecteClient(
            RenewFailureTransport.scripted(
                [
                    ("GET", "login.awp", {"gtk": "gtk-demo"}),
                    ("POST", "login.awp", login),
                ]
            )
        ),
        zone="Europe/Paris",
    )
    await connector.async_open()

    with pytest.raises(ConnectorTransportError):
        await connector.async_collect(Tier.HOMEWORK, "1", priority=Priority.HIGH)

    assert connector.limiter.consecutive_failures == 1
    assert connector.limiter.state is LimiterState.BACKOFF


def test_ecoledirecte_capabilities_are_the_four_read_only_tiers() -> None:
    """An ED entry must not advertise unsupported pages or writes."""
    assert EcoledirecteConnector.CAPABILITIES.source is Source.ECOLEDIRECTE
    assert EcoledirecteConnector.CAPABILITIES.tiers == {
        Tier.TIMETABLE,
        Tier.HOMEWORK,
        Tier.MARKS,
        Tier.ATTENDANCE,
    }
    assert EcoledirecteConnector.CAPABILITIES.writes == frozenset()


@pytest.mark.asyncio
async def test_daily_and_hourly_limits_defer_without_sending() -> None:
    """A previous snapshot must survive when either budget layer is exhausted."""
    daily = EdRateLimiter(_config(max_requests_per_day=1))
    await daily.call("timetable", Priority.HIGH, lambda: asyncio.sleep(0))
    with pytest.raises(TierDeferred) as capped:
        await daily.call("timetable", Priority.HIGH, lambda: asyncio.sleep(0))
    assert capped.value.reason is DeferReason.DAILY_CAP

    hourly = EdRateLimiter(_config(max_requests_per_hour=0, burst_size=0, max_wait=60))
    with pytest.raises(TierDeferred) as bucket:
        await hourly.call("timetable", Priority.HIGH, lambda: asyncio.sleep(0))
    assert bucket.value.reason is DeferReason.HOURLY_BUDGET


@pytest.mark.asyncio
async def test_quiet_hours_defer_until_the_active_window() -> None:
    """A 23:00 refusal must retry at 06:00, not wait until midnight."""
    clock = ManualClock(wall=datetime(2026, 9, 11, 23, tzinfo=UTC))
    limiter = EdRateLimiter(
        _config(
            quiet_hours_enabled=True,
            quiet_start=time(22),
            quiet_end=time(6),
        ),
        clock=clock.monotonic,
        now=clock.now,
        sleep=clock.sleep,
    )
    with pytest.raises(TierDeferred) as deferred:
        await limiter.call("marks", Priority.NORMAL, lambda: asyncio.sleep(0))
    assert deferred.value.reason is DeferReason.QUIET_HOURS
    assert deferred.value.retry_after == 7 * 3600
    assert limiter.state is LimiterState.QUIET_HOURS


@pytest.mark.asyncio
async def test_critical_calls_cross_quiet_hours_but_normal_hits_eighty_percent() -> (
    None
):
    """Bootstrap remains possible while normal ED tiers shed at 80 percent."""
    clock = ManualClock(wall=datetime(2026, 9, 11, 23, tzinfo=UTC))
    limiter = EdRateLimiter(
        _config(
            max_requests_per_day=10,
            quiet_hours_enabled=True,
            quiet_start=time(22),
            quiet_end=time(6),
        ),
        clock=clock.monotonic,
        now=clock.now,
        sleep=clock.sleep,
    )
    for _ in range(8):
        await limiter.call("login", Priority.CRITICAL, lambda: asyncio.sleep(0))
    with pytest.raises(TierDeferred) as deferred:
        await limiter.call("marks", Priority.NORMAL, lambda: asyncio.sleep(0))
    assert deferred.value.reason is DeferReason.QUIET_HOURS
    # A person's click crosses quiet hours on this connector too: the two
    # limiters must not disagree about what quiet hours are for.
    await limiter.call("homework", Priority.GESTURE, lambda: asyncio.sleep(0))

    daytime = ManualClock()
    limiter = EdRateLimiter(
        _config(max_requests_per_day=10),
        clock=daytime.monotonic,
        now=daytime.now,
    )
    limiter.import_state(
        {
            "day": daytime.now().date().isoformat(),
            "calls_today": 8,
        }
    )
    with pytest.raises(TierDeferred) as capped:
        await limiter.call("marks", Priority.NORMAL, lambda: asyncio.sleep(0))
    assert capped.value.reason is DeferReason.DAILY_CAP
    assert limiter.state is LimiterState.THROTTLED


def test_credentials_bootstrap_and_backoff_holds_have_distinct_states() -> None:
    """Diagnostics must tell a human which class of recovery is needed."""
    clock = ManualClock()
    credentials = EdRateLimiter(
        _config(max_failed_logins_per_hour=1, credentials_hold=10),
        clock=clock.monotonic,
        now=clock.now,
    )
    credentials.note_bad_credentials()
    assert credentials.state is LimiterState.CREDENTIALS_HOLD
    assert credentials.retry_delay() == 10
    credentials.reset_after_reauth()
    assert credentials.state is LimiterState.NOMINAL

    bootstrap = EdRateLimiter(
        _config(backoff_base=10, backoff_max=10),
        clock=clock.monotonic,
        now=clock.now,
        rng=random.Random(0),  # noqa: S311 -- deterministic jitter seam
    )
    bootstrap.note_transport_failure(bootstrap=True)
    assert bootstrap.state is LimiterState.BOOTSTRAP_FAILED
    bootstrap.reset_after_reauth()
    bootstrap.note_transport_failure()
    assert bootstrap.state is LimiterState.BACKOFF
    bootstrap.note_success()
    assert bootstrap.state is LimiterState.NOMINAL


def test_holds_expire_and_failed_login_windows_are_pruned() -> None:
    """Elapsed punitive state must not survive beyond its configured window."""
    clock = ManualClock()
    limiter = EdRateLimiter(
        _config(max_failed_logins_per_hour=2, credentials_hold=10),
        clock=clock.monotonic,
        now=clock.now,
    )
    limiter.note_bad_credentials()
    clock.elapsed = 3601
    assert limiter.failed_logins_last_hour == 0
    limiter.note_bad_credentials()
    limiter.note_bad_credentials()
    clock.elapsed += 11
    assert limiter.state is LimiterState.NOMINAL
    assert limiter.retry_delay() == limiter.config.backoff_base


@pytest.mark.asyncio
async def test_login_cap_refuses_before_the_operation_runs() -> None:
    """The daily authentication ceiling counts successful ED logins too."""
    limiter = EdRateLimiter(_config(max_logins_per_day=1))
    await limiter.login(lambda: asyncio.sleep(0), cost=2)
    with pytest.raises(LoginRefusedByLimiter) as refused:
        await limiter.login(lambda: asyncio.sleep(0), cost=2)
    assert refused.value.reason is DeferReason.LOGIN_CAP


@pytest.mark.asyncio
async def test_login_cap_resets_before_refusal_on_the_next_day() -> None:
    """Yesterday's exhausted login cap must not block the first login after midnight."""
    clock = ManualClock(wall=datetime(2026, 9, 11, 23, 59, tzinfo=UTC))
    limiter = EdRateLimiter(
        _config(max_logins_per_day=1),
        clock=clock.monotonic,
        now=clock.now,
        sleep=clock.sleep,
    )
    await limiter.login(lambda: asyncio.sleep(0))
    clock.elapsed = 120

    await limiter.login(lambda: asyncio.sleep(0))

    assert limiter.logins_today == 1


def test_state_round_trip_keeps_only_same_day_budget_and_live_holds() -> None:
    """Setup retry state preserves punishment without resurrecting old calls."""
    clock = ManualClock()
    source = EdRateLimiter(
        _config(max_failed_logins_per_hour=1),
        clock=clock.monotonic,
        now=clock.now,
    )
    source.note_bad_credentials()
    state = source.export_state()

    same_day = EdRateLimiter(_config(), clock=clock.monotonic, now=clock.now)
    same_day.import_state(state)
    assert same_day.calls_today == source.calls_today
    assert same_day.state is LimiterState.CREDENTIALS_HOLD
    assert same_day.snapshot_counters()["reason"] is not None

    tomorrow_clock = ManualClock(wall=clock.wall + timedelta(days=1))
    next_day = EdRateLimiter(
        _config(), clock=tomorrow_clock.monotonic, now=tomorrow_clock.now
    )
    next_day.import_state(state)
    assert next_day.calls_today == 0
    assert next_day.state is LimiterState.CREDENTIALS_HOLD


def test_day_rollover_resets_budget_and_snapshot_is_secret_free() -> None:
    """Midnight grants the new cap without exposing session material."""
    clock = ManualClock()
    limiter = EdRateLimiter(_config(), clock=clock.monotonic, now=clock.now)
    limiter.import_state(
        {
            "day": clock.now().date().isoformat(),
            "calls_today": 3,
            "calls_by_tier": {"marks": 3},
            "logins_today": 1,
        }
    )
    clock.elapsed = 24 * 3600
    snapshot = limiter.snapshot_counters()
    assert snapshot["calls_today"] == 0
    assert snapshot["remaining_today"] == limiter.config.max_requests_per_day
    assert "token" not in snapshot


def test_equal_quiet_boundaries_disable_the_window() -> None:
    """An empty configured window must not defer the whole day."""
    clock = ManualClock()
    limiter = EdRateLimiter(
        _config(
            quiet_hours_enabled=True,
            quiet_start=time(6),
            quiet_end=time(6),
        ),
        clock=clock.monotonic,
        now=clock.now,
    )
    assert limiter.state is LimiterState.NOMINAL


@pytest.mark.asyncio
async def test_daytime_quiet_window_and_default_sleeper_are_total() -> None:
    """Non-crossing windows defer to their same-day end."""
    await _sleep(0)
    clock = ManualClock()
    limiter = EdRateLimiter(
        _config(
            quiet_hours_enabled=True,
            quiet_start=time(6),
            quiet_end=time(22),
        ),
        clock=clock.monotonic,
        now=clock.now,
    )
    with pytest.raises(TierDeferred) as deferred:
        await limiter.call("marks", Priority.NORMAL, lambda: asyncio.sleep(0))
    assert deferred.value.retry_after == 10 * 3600


@pytest.mark.asyncio
async def test_an_active_hold_and_a_blocked_login_refuse_at_admission() -> None:
    """No operation may run while punishment or hourly debt is active."""
    clock = ManualClock()
    held = EdRateLimiter(
        _config(max_failed_logins_per_hour=1),
        clock=clock.monotonic,
        now=clock.now,
    )
    held.note_bad_credentials()
    with pytest.raises(TierDeferred) as deferred:
        await held.call("marks", Priority.NORMAL, lambda: asyncio.sleep(0))
    assert deferred.value.reason is DeferReason.CREDENTIALS_HOLD

    blocked = EdRateLimiter(
        _config(max_requests_per_hour=0, burst_size=0, max_wait=0),
        clock=clock.monotonic,
        now=clock.now,
    )
    with pytest.raises(LoginRefusedByLimiter) as refused:
        await blocked.login(lambda: asyncio.sleep(0))
    assert refused.value.reason is DeferReason.HOURLY_BUDGET


@pytest.mark.asyncio
async def test_login_reconciliation_only_charges_positive_extra_cost() -> None:
    """An optimistic declaration accrues debt; a pessimistic one earns no refund."""
    limiter = EdRateLimiter(_config())
    await limiter.reconcile_login(charged=2, actual=2)
    assert limiter.calls_today == 0
    await limiter.reconcile_login(charged=2, actual=3)
    assert limiter.calls_today == 1


@pytest.mark.asyncio
async def test_batch_grace_and_diagnostic_properties_share_the_entry_state() -> None:
    """A batch crossing 22:00 finishes briefly and exposes one coherent budget."""
    clock = ManualClock(wall=datetime(2026, 9, 11, 21, 59, tzinfo=UTC))
    limiter = EdRateLimiter(
        _config(
            max_requests_per_day=10,
            quiet_hours_enabled=True,
            quiet_start=time(22),
            quiet_end=time(6),
        ),
        clock=clock.monotonic,
        now=clock.now,
        sleep=clock.sleep,
    )
    limiter.begin_batch()
    clock.elapsed = 120
    await limiter.call("timetable", Priority.HIGH, lambda: asyncio.sleep(0))
    assert limiter.calls_by_tier == {"timetable": 1}
    assert limiter.logins_today == 0
    assert limiter.tokens == 19
    assert limiter.consecutive_failures == 0
    assert limiter.throttled is False
    assert limiter.throttled_since is None
    assert limiter.cap_warning_open is False
    assert limiter.hold_until_wallclock is None
    limiter.end_batch()
    with pytest.raises(TierDeferred):
        await limiter.call("timetable", Priority.HIGH, lambda: asyncio.sleep(0))


def test_quiet_duration_counts_only_the_exact_overlap() -> None:
    """Staleness excludes the night, not adjacent active minutes."""
    limiter = EdRateLimiter(
        _config(
            quiet_hours_enabled=True,
            quiet_start=time(22),
            quiet_end=time(6),
        )
    )
    start = datetime(2026, 9, 11, 21, 30, tzinfo=UTC)
    end = datetime(2026, 9, 12, 6, 30, tzinfo=UTC)
    assert limiter.quiet_seconds_between(start, end) == 8 * 3600
    assert limiter.quiet_seconds_between(end, start) == 0


def test_the_ed_estimator_is_login_plus_one_post_per_capable_tier_per_child() -> None:
    """The annexe B Pronote estimator would quote ~180 requests for an ED run."""
    from custom_components.carnet_scolaire.options import (
        estimate_ecoledirecte_daily_requests,
    )

    assert estimate_ecoledirecte_daily_requests({}, students=2) == 2 + 4 * 2
    assert (
        estimate_ecoledirecte_daily_requests(
            {}, students=1, include_qcm=True, multi_establishment=True
        )
        == 2 + 4 + 4 + 1
    )


def test_hold_deadline_and_cap_warning_are_visible_and_persisted() -> None:
    """Repairs and diagnostics survive setup retry on the same day."""
    clock = ManualClock()
    limiter = EdRateLimiter(
        _config(max_requests_per_day=10, max_failed_logins_per_hour=1),
        clock=clock.monotonic,
        now=clock.now,
    )
    limiter.import_state(
        {
            "day": clock.now().date().isoformat(),
            "calls_today": 8,
            "cap_warning_open": True,
        }
    )
    assert limiter.cap_warning_open is True
    limiter.note_bad_credentials()
    assert limiter.hold_until_wallclock == clock.now() + timedelta(hours=1)
    exported = limiter.export_state()
    assert exported["cap_warning_open"] is True
