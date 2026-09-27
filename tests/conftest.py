"""Shared fixtures.

The Home Assistant half comes from ``pytest_homeassistant_custom_component``,
which supplies ``hass``, ``enable_custom_integrations`` and the recorder
scaffolding. The rest is this project's own: simulated clocks, a fake pronotepy
client, and a config entry that never carries a real credential.
"""

from __future__ import annotations

from datetime import UTC, datetime
import sys
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import pytest

from custom_components.carnet_scolaire.const import (
    CHILD_KEY,
    CHILD_RESOURCE_ID,
    CONF_CHILD_KEYS,
    CONF_CHILDREN,
    CONF_LOGIN_MODE,
    CONF_PRONOTE_URL,
    DOMAIN,
    OPT_BURST_SIZE,
    OPT_ESTABLISHMENT_TIMEZONE,
    OPT_MAX_REQUESTS_PER_DAY,
    OPT_MAX_REQUESTS_PER_HOUR,
    OPT_MIN_REQUEST_INTERVAL,
    OPT_QUIET_HOURS_ENABLED,
    LoginMode,
)
from custom_components.carnet_scolaire.gateway import PronoteGateway
from custom_components.carnet_scolaire.ratelimit import RateLimitConfig, RateLimiter

from .clock import FakeClock, RecordingSleeper
from .fixtures.client import FakeClient

if TYPE_CHECKING:
    # Annotations only, so they are safe to import unconditionally even though
    # half of them cannot be *loaded* on Windows: `from __future__ import
    # annotations` is in force, and this block never runs.
    from collections.abc import AsyncIterator, Iterator

    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.core import HomeAssistant

    from custom_components.carnet_scolaire.account import PronoteAccount

#: Home Assistant's own test harness imports ``fcntl``, so it cannot be loaded
#: on Windows at all -- not by anything this project does. The plugin is
#: therefore registered conditionally, which lets a contributor on Windows still
#: run the half of this suite that has no Home Assistant dependency: the
#: limiter, the scheduler, the gateway, the delta detector, the DTOs and the
#: budget estimator -- which is also the half carrying the 100 % gate.
#:
#: CI runs on Linux and loads everything, so no gate is weakened by this.
#: :data:`REQUIRES_HASS` is the marker the dependent modules skip on.
HAS_HASS_HARNESS = sys.platform != "win32"

if HAS_HASS_HARNESS:
    pytest_plugins = ["pytest_homeassistant_custom_component"]

REQUIRES_HASS = pytest.mark.skipif(
    not HAS_HASS_HARNESS,
    reason=(
        "pytest-homeassistant-custom-component imports fcntl, which does not "
        "exist on Windows; run these under Linux, WSL or Docker, as CI does"
    ),
)

#: Europe/Paris, because every date arithmetic question this integration has --
#: the March DST transition, quiet hours, "today" -- is only interesting in the
#: establishment's own zone. A UTC-only test suite would pass while the running
#: integration got the school day wrong twice a year.
PARIS = ZoneInfo("Europe/Paris")

#: The instant every Home-Assistant-dependent test runs at: Thursday
#: 2026-03-12, 08:00 Europe/Paris -- the school day the raw fixtures in
#: :mod:`tests.fixtures.protocol` are built around. Frozen rather than relative
#: to today, because half the entity layer is a function of the clock ("in
#: class", "next lesson", "wake-up") and a suite that drifts with the calendar
#: tests something different every morning.
SCHOOL_DAY = datetime(2026, 3, 12, 8, 0, tzinfo=PARIS)

#: How many drain cycles the `account` fixture will spend waiting for the first
#: collection batch. Generous because it costs nothing when the batch is already
#: finished -- the loop exits on the first check -- and because the alternative
#: to a bound is a hung suite rather than a failed test.
_BATCH_DRAIN_ATTEMPTS = 50

#: Two children on one parent account. Two rather than one throughout, because
#: most defects in the collection loop are invisible with one: a tier that
#: forgets ``set_child``, a snapshot keyed by tier instead of by student, a
#: login counted per child instead of per batch.
CHILDREN = (("STUDENT-1", "Enfant Un"), ("STUDENT-2", "Enfant Deux"))


def child_key(entry: Any, student_id: str) -> str:
    """The key the integration minted for one child.

    Read out of the config entry rather than assumed, and that is the point:
    PRONOTE's resource identifier rotates between sessions, so an entity's
    identity is built from a key this integration allocates and stores. A test
    that spelled the key out by hand would be asserting the minting *order*,
    which is not a promise -- what is promised is that the stored table maps
    the child PRONOTE announced to the key its entities carry.
    """
    for record in entry.data.get(CONF_CHILD_KEYS) or ():
        if record.get(CHILD_RESOURCE_ID) == student_id:
            return str(record[CHILD_KEY])
    raise AssertionError(
        f"no minted key for {student_id!r}; the entry holds "
        f"{entry.data.get(CONF_CHILD_KEYS)!r}"
    )


#: Options for the end-to-end tests.
#:
#: The three limiter ceilings are widened and the spacing is taken to its floor
#: because the limiter's arithmetic is proved in ``test_ratelimit.py`` against a
#: simulated clock; here it would only add real seconds to every test. Quiet
#: hours are off for a blunter reason: on, a suite frozen to 08:00 passes and
#: the same suite frozen to 23:00 sheds every tier -- the tests would then
#: depend on the hour they happened to be written at.
FAST_OPTIONS: dict[str, Any] = {
    OPT_ESTABLISHMENT_TIMEZONE: "Europe/Paris",
    OPT_MIN_REQUEST_INTERVAL: 0.2,
    OPT_MAX_REQUESTS_PER_HOUR: 2000,
    OPT_BURST_SIZE: 100,
    OPT_MAX_REQUESTS_PER_DAY: 20000,
    OPT_QUIET_HOURS_ENABLED: False,
}


if HAS_HASS_HARNESS:

    @pytest.fixture(autouse=True)
    def _auto_enable_custom_integrations(enable_custom_integrations: Any) -> None:
        """Let Home Assistant load ``custom_components/carnet_scolaire``.

        Autouse, because forgetting it in one file produces a failure that
        reads like a bug in the integration rather than a missing fixture.
        Requesting the upstream fixture is the whole body: it is itself a
        yield fixture, so it does the setting up and the tearing down.
        """


@pytest.fixture(name="clock")
def clock_fixture() -> FakeClock:
    """A pair of simulated clocks starting on a plain Thursday morning."""
    return FakeClock(datetime(2026, 3, 12, 8, 0, tzinfo=PARIS))


@pytest.fixture(name="sleeper")
def sleeper_fixture(clock: FakeClock) -> RecordingSleeper:
    """A sleeper that advances ``clock`` rather than the test's own wall time."""
    return RecordingSleeper(clock)


@pytest.fixture(name="limiter")
def limiter_fixture(clock: FakeClock, sleeper: RecordingSleeper) -> RateLimiter:
    """A limiter on the default configuration, with every seam simulated.

    ``rng`` is seeded rather than patched: ``backoff_delay`` multiplies by
    ``0.5 + random()``, and a test asserting a delay range needs that factor to
    be reproducible without reaching into the module.
    """
    import random

    return RateLimiter(
        RateLimitConfig(),
        clock=clock.monotonic,
        now=clock.now,
        rng=random.Random(20260312),  # noqa: S311 -- jitter, not cryptography
        sleep=sleeper,
    )


@pytest.fixture(name="gateway")
def gateway_fixture(clock: FakeClock) -> PronoteGateway:
    """A gateway in the establishment's timezone, on a fixed clock.

    Fixed because several of the gateway's decisions are date-dependent -- does
    tomorrow fall in the next week, which week does the school year start in,
    is today a Sunday -- and a test that reads the real clock only asks those
    questions on some days of the year.
    """
    return PronoteGateway("Europe/Paris", clock=clock.now)


@pytest.fixture(name="client")
def client_fixture() -> FakeClient:
    """A fake pronotepy client that answers from canned raw payloads."""
    return FakeClient()


@pytest.fixture(name="parent_client")
def parent_client_fixture() -> FakeClient:
    """A parent account holding two children, as most real ones do."""
    return FakeClient(children=CHILDREN)


@pytest.fixture(name="account_client")
def account_client_fixture(
    request: pytest.FixtureRequest, client: FakeClient, parent_client: FakeClient
) -> FakeClient:
    """The client the ``account`` fixture logs in with.

    The parent account unless a test asks for ``"student"`` by indirect
    parametrisation. The two are not interchangeable for writes: PRONOTE
    records a homework tick from a student session and ignores one from a
    parent session, so a test of the tick has to say which it is on.
    """
    return client if getattr(request, "param", "parent") == "student" else parent_client


@pytest.fixture(name="entry_data")
def entry_data_fixture() -> dict[str, Any]:
    """Config-entry data with invented credentials.

    Every value here is synthetic. That is a hard rule for this suite and not
    a stylistic preference: the fixtures are committed to a public repository,
    and a token-mode entry carries a working autonomous bearer.
    """
    return {
        CONF_PRONOTE_URL: "https://demo.example.invalid/pronote/parent.html",
        CONF_LOGIN_MODE: str(LoginMode.CREDENTIALS),
        "username": "parent-under-test",
        "password": "not-a-real-password",
        # The followed-child selection, which every real entry carries: the
        # flow writes it on creation and the runtime reads it on every tick.
        # Its absence here was not neutral -- it is what let a diagnostics
        # download ship these identifiers unredacted without a test noticing.
        CONF_CHILDREN: [child_id for child_id, _ in CHILDREN],
    }


@pytest.fixture(name="utc_now")
def utc_now_fixture() -> datetime:
    """A fixed instant, for tests that only need one."""
    return datetime(2026, 3, 12, 8, 0, tzinfo=UTC)


@pytest.fixture(name="domain")
def domain_fixture() -> str:
    """The Home Assistant domain, which is *not* the project name.

    ``carnet_scolaire`` names what a parent follows rather than one platform,
    since the integration collects from PRONOTE and from EcoleDirecte. The
    repository is ``ha-carnet-scolaire`` and the integration is displayed as
    "Carnet scolaire".
    """
    return DOMAIN


# ---------------------------------------------------------------------------
# The Home Assistant half
# ---------------------------------------------------------------------------
#
# Guarded by `HAS_HASS_HARNESS` for the same reason the plugin is: importing
# `pytest_homeassistant_custom_component.common` reaches `fcntl` transitively,
# so on Windows this block cannot even be defined -- and a bare import at the
# top of the module would break the half of the suite that does run there.

if HAS_HASS_HARNESS:
    from unittest.mock import patch

    from homeassistant.config_entries import ConfigEntryState
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    async def _no_sleep(_seconds: float) -> None:
        """Skip the limiter's spacing wait without skipping its decision.

        Only the *wait* is skipped. The admission decision, the charge and the
        shedding all still happen, and they are what these tests are about; the
        0.2 s floor on the spacing would otherwise add several real seconds to
        every batch to re-prove what `test_ratelimit.py` proves on a simulated
        clock.
        """

    @pytest.fixture(name="school_day")
    def school_day_fixture(freezer: FrozenDateTimeFactory) -> FrozenDateTimeFactory:
        """Freeze the clock on the school day the fixtures describe."""
        freezer.move_to(SCHOOL_DAY)
        return freezer

    @pytest.fixture(name="no_spacing")
    def no_spacing_fixture() -> Iterator[None]:
        """Replace the limiter's default sleeper for the whole test.

        Patched at the module attribute rather than passed in, because
        `PronoteAccount` builds its own limiter and deliberately exposes no seam
        for it: the running integration must not be able to install a different
        sleeper.
        """
        with patch(
            "custom_components.carnet_scolaire.ratelimit._asyncio_sleep", new=_no_sleep
        ):
            yield

    @pytest.fixture(name="mock_entry")
    def mock_entry_fixture(
        hass: HomeAssistant, entry_data: dict[str, Any]
    ) -> MockConfigEntry:
        """A config entry for the account, added to `hass` but not set up."""
        entry = MockConfigEntry(
            domain=DOMAIN,
            title="PRONOTE",
            data=entry_data,
            options=dict(FAST_OPTIONS),
            unique_id="demo.example.invalid|parent-under-test",
        )
        entry.add_to_hass(hass)
        return entry

    @pytest.fixture(name="account")
    async def account_fixture(
        hass: HomeAssistant,
        mock_entry: MockConfigEntry,
        account_client: FakeClient,
        # Requested for their side effects: one freezes the clock, the other
        # replaces the limiter's sleeper. Neither is read.
        school_day: FrozenDateTimeFactory,
        no_spacing: None,
    ) -> AsyncIterator[PronoteAccount]:
        """A fully set-up integration, with every platform loaded.

        The login is the only thing mocked, at exactly one seam:
        `session.build_client`. Everything above it is the real code path --
        limiter, scheduler, session manager, tier collectors, gateway,
        coordinators, entities -- which is what makes these tests worth more
        than the sum of the unit suites: the per-tier request costs, the child
        selection and the snapshot keying are only wrong once they are wired
        together.

        Unloaded on the way out, and not only for tidiness: the master tick is
        an `async_track_time_interval` subscription, and the harness fails any
        test that leaves a timer or a worker thread behind.
        """
        with patch(
            "custom_components.carnet_scolaire.session.build_client",
            return_value=account_client,
        ):
            assert await hass.config_entries.async_setup(mock_entry.entry_id)
            await hass.async_block_till_done()

            account: PronoteAccount = mock_entry.runtime_data
            # Wait for the *first batch* and not merely for the loop to go
            # quiet. Set-up schedules the batch as a task, and one
            # `async_block_till_done` is not a guarantee that it finished: a
            # tier's work crosses the single-worker executor and back, so a
            # continuation can be created after the drain has decided there is
            # nothing pending. On an idle machine the batch always won that
            # race; under CPU contention it did not, and the symptom was a
            # single test failing -- the *first* in its module, whose drain
            # competes with pytest importing that module -- with the
            # lowest-priority tier's entity `unavailable` because its snapshot
            # had not landed yet.
            #
            # Every entity test in this suite assumes the first collection has
            # happened, so the guarantee belongs here rather than in the one
            # test that happened to expose its absence. Waiting on the tick
            # lock, and not on any tier having data, is deliberate: tests that
            # break a tier on purpose must still get past this line.
            # Waiting on `completed_ticks` and not on the lock, because the
            # lock could not distinguish "the batch is over" from "the batch
            # has not begun". Set-up schedules the first collection as a task,
            # so on a loaded machine this loop ran before that task had taken
            # the lock, read it as free and broke out immediately -- having
            # waited for nothing. The symptom was the previous comment's, one
            # run in twenty, on the gated CI row: the lowest-priority tier's
            # entity `unavailable` because its snapshot had not landed.
            # `completed_ticks` only ever goes up, and counts empty batches
            # too, so a test that deliberately leaves nothing due still gets
            # past this line -- which is the property the lock had and that
            # waiting on any tier having data would lose.
            for _ in range(_BATCH_DRAIN_ATTEMPTS):
                if account.completed_ticks and not account._tick_lock.locked():
                    break
                await hass.async_block_till_done()
            else:  # pragma: no cover - a batch that never ends is a defect
                pytest.fail("the first collection batch never finished")
            await hass.async_block_till_done()

            yield account

            if mock_entry.state is ConfigEntryState.LOADED:
                await hass.config_entries.async_unload(mock_entry.entry_id)
                await hass.async_block_till_done()
