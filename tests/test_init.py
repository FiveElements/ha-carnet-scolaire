"""Setting one account up, end to end.

Everything below the login is the real code path: the limiter, the scheduler,
the session manager, the tier collectors, the gateway, the coordinators and the
entities. Exactly one seam is mocked -- ``session.build_client`` -- because a
suite that stubbed the gateway or the tiers would prove only that the mocks
agree with each other.

That matters most for the three defects that are invisible in a unit test and
expensive in production: a tier that forgets ``set_child`` and files one child's
data under the other, a login counted per ``(child, tier)`` rather than per
batch, and a reload that makes all ten tiers immediately due. Each has a test
here.

The setup failure paths get the same attention, because §7.2 draws a line the
integration must not blur: a wrong password asks the user to re-authenticate,
while a server that answers badly must **not** -- sending a parent to re-enter
credentials that are correct is worse than saying nothing.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import ATTR_DEVICE_ID
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import (
    device_registry as dr,
    entity_registry as er,
    issue_registry as ir,
)
from pronotepy.exceptions import CryptoError, MFAError, PronoteAPIError
import pytest

from custom_components.carnet_scolaire import async_remove_config_entry_device
from custom_components.carnet_scolaire.account import (
    PronoteAccount,
    TierRecord,
    _establishment_timezone,
)
from custom_components.carnet_scolaire.child_keys import is_minted
from custom_components.carnet_scolaire.connectors.ecoledirecte.connector import (
    EcoledirecteConnector,
)
from custom_components.carnet_scolaire.connectors.ecoledirecte.ed_client import (
    EcoleDirecteClient,
)
from custom_components.carnet_scolaire.connectors.ecoledirecte.ed_limiter import (
    EdRateLimiter,
)
from custom_components.carnet_scolaire.connectors.errors import (
    ConnectorChallengeRequired,
    ConnectorChildMissingError,
    ConnectorCredentialsError,
    ConnectorError,
    ConnectorSessionExpiredError,
    ConnectorTransportError,
    ConnectorUndecodableError,
    ConnectorUnsupportedError,
)
from custom_components.carnet_scolaire.connectors.pronote import (
    RateLimiter as PronoteConnectorRateLimiter,
    SerialExecutor as PronoteConnectorSerialExecutor,
    SessionManager as PronoteConnectorSessionManager,
)
from custom_components.carnet_scolaire.connectors.protocol import ChallengeKind, Source
from custom_components.carnet_scolaire.const import (
    CHILD_KEY,
    CHILD_NAME,
    CHILD_RESOURCE_ID,
    CONF_CHILD_KEYS,
    CONF_CHILDREN,
    CONF_SOURCE,
    DOMAIN,
    FUNC_MENUS,
    FUNC_TIMETABLE,
    ISSUE_ACCOUNT_UNREADABLE,
    ISSUE_BOOTSTRAP_FAILED,
    ISSUE_INVALID_CREDENTIALS,
    ISSUE_MFA_REQUIRED,
    OPT_ESTABLISHMENT_TIMEZONE,
    OPT_READ_TIMEOUT,
    OPT_SESSION_STRATEGY,
    OPT_TIER_ENABLED,
    OPT_TIER_INTERVAL,
    OPT_WRITE_OPERATIONS_ENABLED,
    SERVICE_GENERATE_TIMETABLE_PDF,
    SERVICE_GET_ICAL_URL,
    SERVICE_GET_RATE_LIMIT_STATUS,
    SERVICE_REFRESH,
    TIER_PRIORITY,
    Priority,
    SessionStrategy,
    Tier,
)
from custom_components.carnet_scolaire.hardened_client import BootstrapUnavailable
from custom_components.carnet_scolaire.login_guard import limiter_state_store
from custom_components.carnet_scolaire.ratelimit import (
    LOGIN_COST_KEY,
    REQUESTS_PER_LOGIN,
    RateLimitConfig,
)
from custom_components.carnet_scolaire.sensor import LIST_SENSORS, PRIMITIVE_SENSORS
from custom_components.carnet_scolaire.tiers import (
    _FIRST_COLLECTION_ATTEMPTS,
    _priority_for,
    collect_tier,
)
from tests.test_ecoledirecte_client import RecordingTransport, load_fixture

from .conftest import CHILDREN, REQUIRES_HASS, child_key

if TYPE_CHECKING:
    from freezegun.api import FrozenDateTimeFactory
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from .fixtures.client import FakeClient

pytestmark = REQUIRES_HASS

STUDENT_ONE, STUDENT_TWO = (child_id for child_id, _name in CHILDREN)


async def _setup_failing(
    hass: HomeAssistant, entry: MockConfigEntry, error: BaseException
) -> None:
    """Attempt a set-up whose login raises ``error``."""
    with patch(
        "custom_components.carnet_scolaire.session.build_client", side_effect=error
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()


def _issue(hass: HomeAssistant, entry: MockConfigEntry, key: str) -> ir.IssueEntry:
    """The repair issue ``key`` opened for this entry, or fail the test."""
    registry = ir.async_get(hass)
    issue = registry.async_get_issue(DOMAIN, f"{key}_{entry.entry_id}")
    assert issue is not None, f"no {key} repair issue was opened"
    return issue


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------


async def test_setup_loads_the_entry_and_every_platform(
    hass: HomeAssistant, mock_entry: MockConfigEntry, account: PronoteAccount
) -> None:
    """One login, both children, and the entry ends up loaded."""
    assert mock_entry.state is ConfigEntryState.LOADED
    assert mock_entry.runtime_data is account
    assert [student.id for student in account.students] == [STUDENT_ONE, STUDENT_TWO]
    assert hass.data[DOMAIN][mock_entry.entry_id] is account


#: A pupil nobody has ever heard of, enrolled while Home Assistant was running.
THIRD_CHILD = "STUDENT-3"


async def _enrol_a_third_child(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    parent_client: FakeClient,
) -> tuple[PronoteAccount, set[str]]:
    """Set the account up, enrol a child, and run one more batch.

    ``PER_BATCH`` is only the cheapest way to make a second login happen: the
    announcement belongs to the login, not to the strategy.
    """
    hass.config_entries.async_update_entry(
        mock_entry,
        options={
            **mock_entry.options,
            OPT_SESSION_STRATEGY: str(SessionStrategy.PER_BATCH),
        },
    )

    with patch(
        "custom_components.carnet_scolaire.session.build_client",
        return_value=parent_client,
    ):
        assert await hass.config_entries.async_setup(mock_entry.entry_id)
        await hass.async_block_till_done()
        account: PronoteAccount = mock_entry.runtime_data
        known_before = {student.id for student in account.students}

        parent_client.enrol_child(THIRD_CHILD, "Enfant Trois")
        # Nothing is due right after the first batch, and an empty batch logs
        # nothing in -- so there would be no announcement to react to. Asking
        # for a tier is what the refresh button does.
        account.scheduler.request([Tier.TIMETABLE])
        await account.async_request_tick()
        await hass.async_block_till_done()

    return account, known_before


async def test_a_child_enrolled_after_set_up_gets_its_entities(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    parent_client: FakeClient,
    school_day: Any,
    no_spacing: None,
) -> None:
    """Knowing the child is not serving it.

    ``dynamic-devices`` is about the *entities*: every platform builds its set
    once, iterating ``account.students`` inside ``async_setup_entry`` and then
    dropping ``async_add_entities`` on the floor. A child learned afterwards is
    therefore in the account, in the registry's eyes nowhere, and the
    automations the integration exists for have nothing to bind to.

    The device is asserted through an entity on purpose: nothing creates a
    child device explicitly, it appears because an entity carries the
    ``DeviceInfo`` that names it.
    """
    account, _ = await _enrol_a_third_child(hass, mock_entry, parent_client)
    key = account.stable_key(THIRD_CHILD)

    registry = er.async_get(hass)
    owned = [
        entity
        for entity in registry.entities.values()
        if entity.config_entry_id == mock_entry.entry_id and key in entity.unique_id
    ]

    assert owned, f"no entity was created for the child keyed {key}"


async def test_a_child_enrolled_after_set_up_arrives_without_a_reload(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    parent_client: FakeClient,
    school_day: Any,
    no_spacing: None,
) -> None:
    """The ``dynamic-devices`` rule, in the only shape it can take here.

    A pupil enrolled on the parent account mid-year was invisible until
    somebody reloaded the entry: ``_async_load_session_facts`` reads the
    roster once and ``Tier.SESSION`` is excluded from the collected tiers, so
    no tick ever looked again. Reloading is not a fix -- it drops every
    snapshot the account holds.

    The child must therefore be learned from a login that was going to happen
    anyway, at **no extra request**, and be given a minted key before any
    entity is built: ``stable_key`` feeds every ``unique_id``, and a child
    without one would take the rotating PRONOTE identifier into its identity.
    """
    account, known_before = await _enrol_a_third_child(hass, mock_entry, parent_client)

    assert THIRD_CHILD not in known_before
    assert THIRD_CHILD in {student.id for student in account.students}
    # A minted key, not the rotating identifier: see `child_keys.py`.
    assert is_minted(account.stable_key(THIRD_CHILD))
    # The same account object: learning a child must not restart the entry.
    assert mock_entry.runtime_data is account
    assert mock_entry.state is ConfigEntryState.LOADED


async def test_adopting_a_child_costs_no_login_of_its_own(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    parent_client: FakeClient,
    school_day: Any,
    no_spacing: None,
) -> None:
    """The reason this seam sits on the login and not on a tick of its own.

    PRONOTE publishes no rate limit and sanctions instead, so a rule that made
    the integration look for children on a schedule would spend a login --
    five to seven requests against a cap of 24 a day -- on an event that
    happens once or twice in a school career. Reacting to a login that was
    going to happen anyway costs nothing, because the roster arrives inside
    the handshake that login already paid for.

    Measured as a difference between two batches of the same account rather
    than as an absolute, because the absolute is not the interesting number:
    ``PER_BATCH`` spends one login per batch whatever happens, so what has to
    be shown is that the batch which adopts a child spends no more than the
    batch which does not.
    """
    hass.config_entries.async_update_entry(
        mock_entry,
        options={
            **mock_entry.options,
            OPT_SESSION_STRATEGY: str(SessionStrategy.PER_BATCH),
        },
    )

    with patch(
        "custom_components.carnet_scolaire.session.build_client",
        return_value=parent_client,
    ):
        assert await hass.config_entries.async_setup(mock_entry.entry_id)
        await hass.async_block_till_done()
        account: PronoteAccount = mock_entry.runtime_data

        # A batch with nothing new, for the baseline.
        quiet_before = account.limiter.snapshot_counters()["logins_today"]
        account.scheduler.request([Tier.HOMEWORK])
        await account.async_request_tick()
        await hass.async_block_till_done()
        quiet_cost = account.limiter.snapshot_counters()["logins_today"] - quiet_before

        # The same batch, with a child that has just appeared.
        parent_client.enrol_child(THIRD_CHILD, "Enfant Trois")
        busy_before = account.limiter.snapshot_counters()["logins_today"]
        account.scheduler.request([Tier.TIMETABLE])
        await account.async_request_tick()
        await hass.async_block_till_done()
        busy_cost = account.limiter.snapshot_counters()["logins_today"] - busy_before

    assert THIRD_CHILD in {student.id for student in account.students}
    # Pinned, so the comparison cannot pass by both batches costing nothing:
    # a login is what carries the roster, so there has to be one.
    assert quiet_cost == 1
    assert busy_cost == quiet_cost


async def test_a_rotated_identifier_is_not_read_as_a_new_child(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    parent_client: FakeClient,
    school_day: Any,
    no_spacing: None,
) -> None:
    """The defect this whole mechanism could have re-introduced.

    A PRONOTE resource identifier is written ``46#<signature>`` and the
    signature is not stable between sessions -- three values were observed for
    one pupil in one day. A newcomer check that compared identifiers would
    read every rotation as an arrival, mint a second key, and build a second
    device and a full set of entities while the first generation was orphaned
    in the registry with every dashboard and automation pointing at it dead.

    So the account must end this batch with the children it started with, and
    each of them keeping the key it already had.
    """
    hass.config_entries.async_update_entry(
        mock_entry,
        options={
            **mock_entry.options,
            OPT_SESSION_STRATEGY: str(SessionStrategy.PER_BATCH),
        },
    )

    with patch(
        "custom_components.carnet_scolaire.session.build_client",
        return_value=parent_client,
    ):
        assert await hass.config_entries.async_setup(mock_entry.entry_id)
        await hass.async_block_till_done()
        account: PronoteAccount = mock_entry.runtime_data
        keys_before = {
            student.name: account.stable_key(student.id) for student in account.students
        }

        parent_client.rotate_identifiers()
        account.scheduler.request([Tier.TIMETABLE])
        await account.async_request_tick()
        await hass.async_block_till_done()

    assert len(account.students) == len(keys_before)
    assert {
        student.name: account.stable_key(student.id) for student in account.students
    } == keys_before


async def test_a_login_that_renames_the_children_leaves_no_data_behind(
    hass: HomeAssistant,
    account: PronoteAccount,
    parent_client: FakeClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The batch after a re-login collects, and the data follows the child.

    Measured on a live instance: a re-login inside a batch renamed the child,
    every tier of that batch then asked for the old identifier and failed
    with "not on this account any more", and the next batches published
    under the new identifier while every entity kept reading the old one --
    a dashboard frozen on the evening before, a first homework tick refused
    with "no child with id".
    """
    ids_before = {student.id for student in account.students}
    keys_before = {account.stable_key(sid) for sid in ids_before}
    posted = len(parent_client.posted_names)

    # Dropping the held client makes the next call log in again, and the
    # login announces the renamed roster -- what an expired session does.
    parent_client.rotate_identifiers()
    assert account.extras is not None
    await account.extras.session._reopen()
    account.scheduler.request([Tier.TIMETABLE])
    await account.async_request_tick()
    await hass.async_block_till_done()

    assert "not on this account any more" not in caplog.text
    assert FUNC_TIMETABLE[0] in parent_client.posted_names[posted:]
    ids_after = {student.id for student in account.students}
    assert ids_after.isdisjoint(ids_before)
    assert {account.stable_key(sid) for sid in ids_after} == keys_before
    for coordinator in account.coordinators.values():
        assert not set(coordinator.data or {}) & ids_before
    for student_id in ids_after:
        snapshot = account.snapshot(Tier.TIMETABLE, student_id)
        assert snapshot is not None
        assert snapshot.student_id == student_id
    state = hass.states.get("sensor.enfant_un_timetable_this_week")
    assert state is not None
    assert state.state not in ("unavailable", "unknown")


async def test_an_identifier_the_account_never_paired_is_left_to_fail(
    account: PronoteAccount,
) -> None:
    """Resolution goes through a known key, never through a guess.

    An identifier no pairing ever produced has no key, so no child it could
    safely be mapped to; ``set_child`` then refuses it as it always did,
    rather than a call landing on whichever child happened to be first.
    """
    assert account._live_child_id("NEVER-PAIRED", [("NEW-1", "Enfant Un")]) is None


async def test_a_rename_skips_a_tier_that_holds_nothing(
    account: PronoteAccount,
) -> None:
    """A tier with no data yet has nothing to carry, and must not raise."""
    student = account.students[0]
    key = account.stable_key(student.id)
    account.coordinators[Tier.HISTORY].data = {}

    account._async_follow_renamed_children({"GONE-1": key})

    assert account.coordinators[Tier.HISTORY].data == {}
    assert account.snapshot(Tier.TIMETABLE, student.id) is not None


async def test_setup_uses_the_pronote_connector(account: PronoteAccount) -> None:
    """The account source must stay explicit when another school backend is added."""
    assert account.connector.capabilities.source is Source.PRONOTE


async def test_an_unselected_child_with_unreadable_facts_does_not_block_setup(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    parent_client: FakeClient,
    school_day: Any,
    no_spacing: None,
) -> None:
    """A child excluded in the flow must be filtered before its facts are decoded."""
    from custom_components.carnet_scolaire.gateway import PronoteGateway

    del school_day, no_spacing
    hass.config_entries.async_update_entry(
        mock_entry,
        data={**mock_entry.data, CONF_CHILDREN: [STUDENT_ONE]},
    )
    original = PronoteGateway.session_facts

    def session_facts(gateway: PronoteGateway, client: FakeClient) -> Any:
        if client.selected_child_id == STUDENT_TWO:
            raise ValueError("unselected child is unreadable")
        return original(gateway, client)

    with (
        patch(
            "custom_components.carnet_scolaire.session.build_client",
            return_value=parent_client,
        ),
        patch.object(PronoteGateway, "session_facts", session_facts),
    ):
        assert await hass.config_entries.async_setup(mock_entry.entry_id)
        await hass.async_block_till_done()

    account = mock_entry.runtime_data
    assert [student.id for student in account.students] == [STUDENT_ONE]

    await hass.config_entries.async_unload(mock_entry.entry_id)
    await hass.async_block_till_done()


async def test_account_now_is_timezone_aware(account: PronoteAccount) -> None:
    """Naive datetimes in entity state lose ordering and locale in one stroke."""
    assert account.now().tzinfo is not None
    assert account.today() == account.now().date()


async def test_a_device_per_child_hangs_off_the_account_device(
    hass: HomeAssistant, mock_entry: MockConfigEntry, account: PronoteAccount
) -> None:
    """Which is what makes the device triggers of §2.3 readable.

    "When a lesson is cancelled" has to be asked about *somebody*, so each
    child is a device in its own right, and the establishment is the device they
    hang off -- not a prefix in their names.
    """
    registry = dr.async_get(hass)
    devices = dr.async_entries_for_config_entry(registry, mock_entry.entry_id)
    by_identifier = {next(iter(device.identifiers))[1]: device for device in devices}

    account_device = by_identifier[mock_entry.entry_id]
    assert account_device.name == "Collège d'Essai"

    for student in account.students:
        key = child_key(mock_entry, student.id)
        child = by_identifier[f"{mock_entry.entry_id}_{key}"]
        assert child.via_device_id == account_device.id
        assert child.name == student.name
        assert child.model == student.class_name


async def test_every_entity_id_is_derived_from_the_pronote_identifier(
    hass: HomeAssistant, mock_entry: MockConfigEntry, account: PronoteAccount
) -> None:
    """Never from a label, a period name or a rank (§2.4).

    An automation that breaks in September because an establishment relabelled
    "Trimestre 1" is a regression even though no code failed, so the unique id
    is built from the config entry, the identifier PRONOTE gave the child, and a
    functional key.

    The entry prefix matters as much as the rest. The entry's own ``unique_id``
    is the *account*, so two entries following the same child are legitimate --
    a mother's account and a father's, or a parent account beside the child's
    own -- and ``student.id`` is only unique inside one establishment's
    database. Without the prefix the registry does not reject the second entry:
    it re-points the existing entity at it, moving the entity to the other
    entry's device and silently breaking every automation that named it.
    """
    registry = er.async_get(hass)
    entries = er.async_entries_for_config_entry(registry, mock_entry.entry_id)
    assert entries, "the platforms created no entities at all"

    prefixes = tuple(
        f"{mock_entry.entry_id}_{child_key(mock_entry, student)}_"
        for student in (STUDENT_ONE, STUDENT_TWO)
    )
    per_child = [entry for entry in entries if entry.unique_id.startswith(prefixes[0])]
    assert per_child

    for entry in entries:
        # Either a child's entity, or one of the account's own diagnostics.
        assert entry.unique_id.startswith((*prefixes, f"{mock_entry.entry_id}_"))
        assert entry.unique_id.startswith(mock_entry.entry_id)


async def test_both_children_get_their_own_snapshot_of_every_tier(
    account: PronoteAccount,
) -> None:
    """Snapshots are keyed by student, because one entry is one *account*.

    A parent account holds several children who share a session, a budget and
    an IP address (§7.1) -- but not a timetable. Keying by tier alone would have
    the second child's collection overwrite the first's, and both devices would
    then show the same lessons.
    """
    for tier in Tier:
        for student_id in (STUDENT_ONE, STUDENT_TWO):
            snapshot = account.snapshot(tier, student_id)
            assert snapshot is not None, f"{tier} has no snapshot for {student_id}"
            assert snapshot.student_id == student_id


async def test_every_child_is_selected_before_its_data_is_read(
    account: PronoteAccount, parent_client: FakeClient
) -> None:
    """The one defect a single-child test cannot see.

    ``set_child`` is what makes the next tab call return *this* child's data. A
    tier that omits it silently files the first child's timetable under the
    second, which no unit test on the gateway can detect: the gateway is handed
    a client and asks it for a tab, and both answers are well-formed.
    """
    assert set(parent_client.child_selections) == {STUDENT_ONE, STUDENT_TWO}


async def test_the_whole_first_batch_costs_one_login(
    account: PronoteAccount,
) -> None:
    """Per *batch*, not per ``(child, tier)``.

    Comparing only the strategy made ``_reconnect_required()`` true on every
    call, and ``run()`` is called once per unit of work: one tick of a two-child
    account with a closed period asked for dozens of full logins against a cap
    of 24, and the integration died before its first batch finished.
    """
    counters = account.limiter.snapshot_counters()
    assert counters["logins_today"] == 1
    assert counters["calls_today"] > 0


async def test_the_session_tier_costs_nothing(account: PronoteAccount) -> None:
    """Periods, class and establishment come free with the login (annexe A §2).

    Specification v1 filed them under a data tier and budgeted requests for
    them. They are in ``func_options`` and ``parametres_utilisateur``, already
    in memory once authenticated.
    """
    for student_id in (STUDENT_ONE, STUDENT_TWO):
        snapshot = account.snapshot(Tier.SESSION, student_id)
        assert snapshot is not None
        assert snapshot.calls == 0

    # And the login's own five requests are booked under their own key, not
    # under this tier's. They are real requests and they are counted -- but
    # attributing them to `session` made the one free tier read as the most
    # expensive thing the integration does, on the attribute the diagnostics
    # point a user at when tuning the budget.
    by_tier = account.limiter.snapshot_counters()["calls_by_tier"]
    assert by_tier[str(Tier.SESSION)] == 0
    assert by_tier[LOGIN_COST_KEY] == REQUESTS_PER_LOGIN


async def test_the_declared_cost_matches_what_was_actually_posted(
    account: PronoteAccount, parent_client: FakeClient
) -> None:
    """The §11.1 contract, checked at the account level.

    ``GatewayResult.calls`` is reconciled against the limiter, so the sum of
    every tier's recorded calls has to equal the number of requests the client
    really placed. An accidental lazy-property access adds a request that the
    spacing, the bucket and the daily cap would all miss -- which is the exact
    regression the gateway's design exists to prevent.
    """
    counters = account.limiter.snapshot_counters()
    charged = sum(
        calls
        for tier, calls in counters["calls_by_tier"].items()
        # The login is charged to the budget but placed by the mocked seam, so
        # it never reaches `posts`; everything else did.
        if tier != LOGIN_COST_KEY
    )
    assert charged == len(parent_client.posts)


async def test_a_repeated_refresh_costs_one_batch(
    hass: HomeAssistant, account: PronoteAccount, parent_client: FakeClient
) -> None:
    """The button and the service may not become a way round the limiter.

    Nothing is bypassed: the scheduler still decides what is due, so ten
    requests in a minute cost one batch at most -- and here, nothing at all,
    because the first batch just ran and no tier is due again yet (§5.1).
    """
    before = len(parent_client.posts)

    for _ in range(10):
        await account.async_request_tick()
    await hass.async_block_till_done()

    assert len(parent_client.posts) == before


# ---------------------------------------------------------------------------
# Reload
# ---------------------------------------------------------------------------


async def test_an_options_change_reloads_without_recollecting(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    account: PronoteAccount,
    parent_client: FakeClient,
) -> None:
    """§7.3: tuning the cadence must not cost a full round of requests.

    An options save reloads the entry, which builds a new scheduler whose every
    deadline is unset -- making all ten tiers immediately due. The schedule is
    handed across the reload through ``hass.data``, so the reload is free.
    """
    before = len(parent_client.posts)

    hass.config_entries.async_update_entry(
        mock_entry,
        options={**mock_entry.options, OPT_TIER_ENABLED.format(tier="menus"): False},
    )
    await hass.async_block_till_done()

    assert mock_entry.state is ConfigEntryState.LOADED
    assert len(parent_client.posts) == before
    assert mock_entry.runtime_data is not account


async def test_a_data_only_write_does_not_reload_the_entry(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    account: PronoteAccount,
) -> None:
    """Minting a child key at runtime must not restart the account.

    ``_async_pair_children`` persists its table with ``async_update_entry``,
    which wakes the update listener. That listener exists to apply an
    *options* change -- its own docstring says so -- yet it reloads on any
    write at all. Pairing a child discovered during a tick would therefore
    reload the very entry whose entities we are adding without a reload, and
    the ``dynamic-devices`` rule would be unreachable by construction.
    """
    stored = list(mock_entry.data.get(CONF_CHILD_KEYS) or ())

    hass.config_entries.async_update_entry(
        mock_entry,
        data={
            **mock_entry.data,
            CONF_CHILD_KEYS: [
                *stored,
                {
                    CHILD_KEY: "child-9",
                    CHILD_RESOURCE_ID: "STUDENT-9",
                    CHILD_NAME: "Enfant Neuf",
                },
            ],
        },
    )
    await hass.async_block_till_done()

    assert mock_entry.state is ConfigEntryState.LOADED
    assert mock_entry.runtime_data is account


async def test_unloading_stops_the_heartbeat_and_releases_the_client(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    account: PronoteAccount,
    parent_client: FakeClient,
) -> None:
    """And leaves the actions in place, which is the opposite of tidiness.

    The actions belong to the integration, not to this entry: they are
    registered from `async_setup` and deliberately never removed. Home Assistant
    can only validate an automation against actions that *exist*, so removing
    them when the last entry unloaded made every automation referencing
    `carnet_scolaire.mark_homework_done` unvalidatable exactly when the user was
    already looking at a broken account -- and being driven by automations is
    this integration's stated purpose (§1).

    The handlers are built for it: each resolves its account from the target
    device at call time and raises `ServiceValidationError` for one it cannot
    place. So the action stays present and fails with a reason, rather than
    vanishing and failing with "action not found".
    """
    assert hass.services.has_service(DOMAIN, SERVICE_REFRESH)

    assert await hass.config_entries.async_unload(mock_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_entry.state is ConfigEntryState.NOT_LOADED
    assert parent_client.closed
    assert not hass.data.get(DOMAIN)
    assert hass.services.has_service(DOMAIN, SERVICE_REFRESH)
    assert hass.services.has_service(DOMAIN, SERVICE_GET_RATE_LIMIT_STATUS)


# ---------------------------------------------------------------------------
# Failure paths -- §7.2's line between "your password" and "their server"
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(CryptoError("challenge"), id="wrong-password"),
        pytest.param(MFAError("pin"), id="pin-demanded"),
    ],
)
async def test_a_credential_problem_opens_a_reauthentication_flow(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    school_day: Any,
    no_spacing: None,
    error: Exception,
) -> None:
    """Never a silently broken entry (§7.2).

    For a demanded PIN the flow has to *ask* for it: a secret we refuse to
    store is a secret we must know how to request again (§8.1).
    """
    await _setup_failing(hass, mock_entry, error)

    assert mock_entry.state is ConfigEntryState.SETUP_ERROR
    flows = [
        flow
        for flow in hass.config_entries.flow.async_progress()
        if flow["handler"] == DOMAIN
    ]
    assert [flow["context"]["source"] for flow in flows] == [SOURCE_REAUTH]


async def test_a_login_with_no_session_key_is_a_credential_problem(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    parent_client: FakeClient,
    school_day: Any,
    no_spacing: None,
) -> None:
    """``_login`` returning ``False`` is a wrong password by another route.

    pronotepy answers a refused authentication by leaving ``logged_in`` false
    rather than raising, so a client built without error is not yet a client
    that works.
    """
    parent_client.logged_in = False

    with patch(
        "custom_components.carnet_scolaire.session.build_client",
        return_value=parent_client,
    ):
        await hass.config_entries.async_setup(mock_entry.entry_id)
        await hass.async_block_till_done()

    assert mock_entry.state is ConfigEntryState.SETUP_ERROR
    assert parent_client.closed


async def test_an_impossible_bootstrap_never_names_a_cause(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    school_day: Any,
    no_spacing: None,
) -> None:
    """§6.3, and the reason this repair reads the way it does.

    pronotepy decides an address is suspended with ``if "IP" in html`` -- two
    capitals anywhere in the page. Telling parents their home connection is
    banned because a school wrote "Espace IP" in a footer would be worse than
    saying nothing, so the issue enumerates the possible causes and picks none.
    """
    await _setup_failing(hass, mock_entry, BootstrapUnavailable("page unavailable"))

    assert mock_entry.state is ConfigEntryState.SETUP_RETRY
    issue = _issue(hass, mock_entry, ISSUE_BOOTSTRAP_FAILED)
    assert issue.severity is ir.IssueSeverity.ERROR
    assert not issue.is_fixable
    assert issue.translation_placeholders is not None
    assert issue.translation_placeholders["url"] == mock_entry.data["pronote_url"]


async def test_an_undecodable_login_asks_for_a_report(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    school_day: Any,
    no_spacing: None,
) -> None:
    """A separate repair from ``bootstrap_failed``, because the remedy differs.

    The address is right and the credentials are right; something in this
    establishment's response is outside what the pinned ``pronotepy`` handles.
    The user cannot fix that, so the text asks for a report rather than a
    correction. ``ValueError`` is the shape it really arrives in: ``clients.py``
    parses ``PremierLundi`` with a bare ``strptime``.
    """
    await _setup_failing(hass, mock_entry, ValueError("PremierLundi"))

    assert mock_entry.state is ConfigEntryState.SETUP_RETRY
    _issue(hass, mock_entry, ISSUE_ACCOUNT_UNREADABLE)


@pytest.mark.parametrize(
    ("error", "state", "issue_key", "reauth"),
    [
        pytest.param(
            ConnectorCredentialsError("505"),
            ConfigEntryState.SETUP_ERROR,
            None,
            True,
            id="credentials",
        ),
        pytest.param(
            ConnectorChallengeRequired(ChallengeKind.QCM),
            ConfigEntryState.SETUP_ERROR,
            None,
            True,
            id="challenge",
        ),
        pytest.param(
            ConnectorTransportError("timeout"),
            ConfigEntryState.SETUP_RETRY,
            ISSUE_BOOTSTRAP_FAILED,
            False,
            id="transport",
        ),
        pytest.param(
            ConnectorUndecodableError("notes key missing"),
            ConfigEntryState.SETUP_RETRY,
            ISSUE_ACCOUNT_UNREADABLE,
            False,
            id="undecodable",
        ),
        pytest.param(
            ConnectorError("a connector failure with no class of its own"),
            ConfigEntryState.SETUP_RETRY,
            ISSUE_ACCOUNT_UNREADABLE,
            False,
            id="bare",
        ),
        pytest.param(
            ConnectorSessionExpiredError(
                "EcoleDirecte session token is no longer valid"
            ),
            ConfigEntryState.SETUP_RETRY,
            None,
            False,
            id="session-expired",
        ),
        pytest.param(
            ConnectorChildMissingError("STUDENT-9"),
            ConfigEntryState.SETUP_RETRY,
            ISSUE_ACCOUNT_UNREADABLE,
            False,
            id="child-missing",
        ),
        pytest.param(
            ConnectorUnsupportedError("menus"),
            ConfigEntryState.SETUP_RETRY,
            ISSUE_ACCOUNT_UNREADABLE,
            False,
            id="unsupported",
        ),
    ],
)
async def test_each_seam_error_at_setup_has_a_named_home_assistant_outcome(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    school_day: Any,
    no_spacing: None,
    error: Exception,
    state: ConfigEntryState,
    issue_key: str | None,
    reauth: bool,
) -> None:
    """The scale is re-auth / retry / repair, not an unhandled traceback (#20).

    Credentials and a human challenge start re-authentication, the same
    outcome as ``InvalidCredentials``. An undecodable payload -- and a bare
    ``ConnectorError``, which is how a vanished EcoleDirecte token arrives --
    must open the same repair as ``AccountUnreadable``: retry, do not send the
    parent to re-enter credentials that are still correct. Transport stays on
    the bootstrap repair. Caller-bug subclasses that leak to set-up still get
    a named issue rather than ``SETUP_ERROR`` with no repair.

    An expired session is the exception that proves the scale is about what
    the user should *do*: it retries and opens **nothing**. The connector has
    already dropped its tokens, so the next attempt logs in again -- and a
    repair card asking for a bug report, which nothing ever closes, would
    outlive the condition by weeks.
    """
    del school_day, no_spacing
    with patch.object(PronoteAccount, "async_setup", side_effect=error):
        await hass.config_entries.async_setup(mock_entry.entry_id)
        await hass.async_block_till_done()

    assert mock_entry.state is state
    flows = [
        flow
        for flow in hass.config_entries.flow.async_progress()
        if flow["handler"] == DOMAIN
    ]
    if reauth:
        assert [flow["context"]["source"] for flow in flows] == [SOURCE_REAUTH]
    else:
        assert flows == []
    if issue_key is None:
        registry = ir.async_get(hass)
        assert (
            registry.async_get_issue(
                DOMAIN, f"{ISSUE_ACCOUNT_UNREADABLE}_{mock_entry.entry_id}"
            )
            is None
        )
        assert (
            registry.async_get_issue(
                DOMAIN, f"{ISSUE_BOOTSTRAP_FAILED}_{mock_entry.entry_id}"
            )
            is None
        )
    else:
        _issue(hass, mock_entry, issue_key)


async def test_a_server_refusal_is_not_reported_as_a_credential_problem(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    school_day: Any,
    no_spacing: None,
) -> None:
    """A protocol refusal with no ``Erreur.G`` we recognise is the server's.

    So: retry, and no re-authentication flow. Sending a parent to re-enter
    credentials that are correct is the failure mode §7.2 is about.
    """
    await _setup_failing(hass, mock_entry, PronoteAPIError("refused"))

    assert mock_entry.state is ConfigEntryState.SETUP_RETRY
    assert not [
        flow
        for flow in hass.config_entries.flow.async_progress()
        if flow["handler"] == DOMAIN
    ]


async def test_a_school_that_is_simply_down_is_retried(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    school_day: Any,
    no_spacing: None,
) -> None:
    """``requests.Timeout`` inherits from ``OSError``, not ``TimeoutError``.

    ``except TimeoutError`` therefore caught nothing at all, and a school down
    for a weekend produced an uncounted bootstrap GET every five minutes with no
    backoff whatsoever while ``calls_today`` reported zero.
    """
    await _setup_failing(hass, mock_entry, OSError("connection refused"))

    assert mock_entry.state is ConfigEntryState.SETUP_RETRY


async def test_a_snapshot_collected_for_another_child_is_refused(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    account: PronoteAccount,
) -> None:
    """The one runtime defence against publishing the wrong child's data.

    `pronotepy`'s parent client sets `_selected_child = self.children[0]` in
    its constructor, so a path that forgets `set_child` raises nothing: it
    returns the *first* child's data. With one child -- the likeliest test
    configuration -- the defect is undetectable. With two it publishes one
    child's timetable under the other child's entities, and the only detector
    left is a parent recognising the wrong lessons on their dashboard (§7.1).

    Every snapshot carries the child it was collected for, and this is the
    check that makes the stamp worth having. Asserted here rather than trusted
    to the atomic `(child, tier)` closure in `session.py`, because the point of
    a safety net is that it holds when the thing above it does not.
    """
    coordinator = account.coordinators[Tier.TIMETABLE]
    theirs = coordinator.snapshot_for(STUDENT_TWO)
    assert theirs is not None, "the fixture collected nothing for the second child"

    with pytest.raises(ValueError, match="another"):
        coordinator.publish(STUDENT_ONE, theirs)

    # And the first child's own snapshot is untouched by the refusal.
    assert coordinator.snapshot_for(STUDENT_ONE) is not None
    assert coordinator.snapshot_for(STUDENT_ONE).student_id == STUDENT_ONE


async def test_no_tab_call_happens_outside_the_selection_it_belongs_to(
    account: PronoteAccount, parent_client: FakeClient
) -> None:
    """The ordered form of the invariant, which the set form cannot express.

    `test_every_child_is_selected_before_its_data_is_read` asserts the *set* of
    selections, and a set is blind to the defect that matters: both children
    could be selected, both tiers collected, and the pairing between them
    crossed. Upstream makes that failure silent rather than loud --
    `ParentClient.__init__` sets `_selected_child = self.children[0]`, so a
    call placed with no selection in force answers for the first child instead
    of raising (§7.1).

    So this reads one interleaved journal of selections and server calls, and
    checks the only property that rules the crossing out: every call is
    preceded by a selection, and the child in force when a call was made is the
    child whose snapshot the collection then published.

    Asserted on a two-child account because with one child the property holds
    vacuously -- which is precisely why the defect survives single-child
    testing.
    """
    parent_client.journal.clear()

    await account._async_collect(Tier.TIMETABLE)

    journal = list(parent_client.journal)
    assert journal, "collecting a tier placed no call at all"
    assert journal[0][0] == "select", (
        f"a call was placed before any child was selected: {journal[:3]}"
    )

    # Attribute every call to the selection that was in force when it happened.
    in_force: str | None = None
    calls_by_child: dict[str, list[str]] = {}
    for kind, value in journal:
        if kind == "select":
            in_force = value
            calls_by_child.setdefault(value, [])
            continue
        assert in_force is not None
        calls_by_child[in_force].append(value)

    expected = {student.id for student in account.students}
    assert set(calls_by_child) == expected, (
        "the collection selected children it does not follow, or skipped one"
    )
    for student_id, calls in calls_by_child.items():
        assert calls, f"{student_id} was selected and then never read"

    # And the snapshot each child ended up with was published under the same id
    # the client was pointed at while its data was being fetched.
    coordinator = account.coordinators[Tier.TIMETABLE]
    for student_id in expected:
        snapshot = coordinator.snapshot_for(student_id)
        assert snapshot is not None, f"no timetable published for {student_id}"
        assert snapshot.student_id == student_id


async def test_a_stale_child_selection_says_so_instead_of_recovering_quietly(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    parent_client: FakeClient,
    school_day: FrozenDateTimeFactory,
    no_spacing: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The silence that hid the worst defect this integration has had.

    A PRONOTE resource identifier is written ``46#<signature>``, and that
    signature is **not stable between sessions**. So the identifiers stored in
    ``entry.data["children"]`` at configuration time eventually match nothing
    the account announces, and following every child instead is the right
    recovery -- refusing to collect because a stored string went stale would
    take the whole integration down.

    Recovering *quietly* is what cost. An entity's ``unique_id`` embeds the
    identifier, so a changed one creates a new device and a full set of new
    entities while the previous generation is orphaned in the registry:
    every dashboard, automation and helper pointing at it dead, and nothing
    logged anywhere. On a live instance three generations accumulated before
    anybody noticed, and only because a dashboard read the dead set.

    ``config_flow._account_identity`` already learned this lesson for the
    account identifier -- it drops the signature before comparing, and its
    docstring explains why. Nobody carried it here.
    """
    hass.config_entries.async_update_entry(
        mock_entry,
        data={**mock_entry.data, CONF_CHILDREN: ["46#a-signature-from-last-session"]},
    )
    caplog.set_level(logging.WARNING)

    with patch(
        "custom_components.carnet_scolaire.session.build_client",
        return_value=parent_client,
    ):
        assert await hass.config_entries.async_setup(mock_entry.entry_id)
        await hass.async_block_till_done()

        account = mock_entry.runtime_data
        # Recovered: every announced child is followed, so the account works.
        assert {student.id for student in account.students} == {
            child_id for child_id, _name in CHILDREN
        }
        # And said so, naming both counts so the reader can see it is a
        # mismatch and not an empty account.
        assert "selected children match" in caplog.text
        assert "not stable between sessions" in caplog.text

        await hass.config_entries.async_unload(mock_entry.entry_id)
        await hass.async_block_till_done()


async def test_a_finished_batch_is_counted_so_a_caller_can_wait_for_it(
    hass: HomeAssistant,
    account: PronoteAccount,
) -> None:
    """The lock could not tell "over" from "not begun", and that flaked CI.

    Set-up schedules the first collection as a task. A caller that checked
    ``_tick_lock`` before that task had taken it read the lock as free and
    concluded the batch was finished, having waited for nothing -- so the
    lowest-priority tier's entity was still ``unavailable`` when the test read
    it. It failed about one run in twenty, on the *gated* CI row, which is the
    worst place for it: a barrier that flickers either lets something through
    or blocks a release at random.

    ``completed_ticks`` is incremented at the end of a batch, so a non-zero
    value cannot mean "not started yet".
    """
    assert account.completed_ticks >= 1, (
        "set-up returned before its own first collection had finished"
    )
    assert not account._tick_lock.locked()


async def test_a_batch_with_nothing_due_is_still_counted(
    hass: HomeAssistant,
    account: PronoteAccount,
) -> None:
    """Counting only *productive* batches would reintroduce the same hang.

    Some tests deliberately leave no tier due -- that is how a broken tier or
    a spent budget is exercised -- and a wait that required a collection would
    never be satisfied by them. So an empty batch counts too, which is the one
    property the discarded lock check did have.
    """
    before = account.completed_ticks

    # Nothing has become due since the fixture drained the first batch.
    await account._async_tick()

    assert account.completed_ticks == before + 1


async def test_an_ed_batch_and_diagnostics_never_touch_session_manager() -> None:
    """ED owns an asyncio lock, so a SessionManager access crashes its first tick."""
    limiter = EdRateLimiter(
        RateLimitConfig(min_request_interval=0, quiet_hours_enabled=False)
    )
    connector = SimpleNamespace(
        capabilities=EcoledirecteConnector.CAPABILITIES,
        limiter=limiter,
        diagnostics=lambda: {
            "limiter": limiter.snapshot_counters(),
            "session": {"authenticated": True},
        },
        # Part of the connector protocol, and read at the end of every batch
        # to see whether the account gained a child. Announcing the two it
        # already has is what a connector with a stable roster does.
        student_ids=lambda: (STUDENT_ONE, STUDENT_TWO),
    )
    account = object.__new__(PronoteAccount)
    account.connector = connector  # type: ignore[assignment]
    account.scheduler = SimpleNamespace(  # type: ignore[assignment]
        due=lambda: (Tier.TIMETABLE,),
        diagnostics=dict,
    )
    account._shutting_down = False
    account._announced_children = (STUDENT_ONE, STUDENT_TWO)
    account._completed_ticks = 0
    account.state = SimpleNamespace(students=(), periods=())
    account.stale_after = 3
    account.entry = SimpleNamespace(options={})  # type: ignore[assignment]

    collected: list[Tier] = []

    async def collect(tier: Tier) -> None:
        collected.append(tier)

    account._async_collect = collect  # type: ignore[method-assign]
    account._async_sync_issues = lambda: None  # type: ignore[method-assign]

    await account._async_run_batch()

    assert collected == [Tier.TIMETABLE]
    assert account.diagnostics()["session"] == {"authenticated": True}


async def test_an_ed_entry_instantiates_no_pronote_network_primitive(
    hass: HomeAssistant,
) -> None:
    """A second backend must not silently inherit PRONOTE's thread and login costs."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="EcoleDirecte",
        data={
            CONF_SOURCE: Source.ECOLEDIRECTE.value,
            "username": "demo.example.invalid",
            "password": "not-a-real-password",
            CONF_CHILDREN: ["1"],
        },
    )
    entry.add_to_hass(hass)

    with (
        patch.object(PronoteAccount, "async_setup", return_value=None),
        patch.object(PronoteAccount, "async_start_first_collection"),
        patch(
            "custom_components.carnet_scolaire.connectors.pronote.SerialExecutor",
            wraps=PronoteConnectorSerialExecutor,
        ) as executor,
        patch(
            "custom_components.carnet_scolaire.connectors.pronote.SessionManager",
            wraps=PronoteConnectorSessionManager,
        ) as session_manager,
        patch(
            "custom_components.carnet_scolaire.connectors.pronote.RateLimiter",
            wraps=PronoteConnectorRateLimiter,
        ) as pronote_limiter,
        patch.object(
            hass.config_entries,
            "async_forward_entry_setups",
            return_value=None,
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    account = entry.runtime_data
    assert isinstance(account.connector, EcoledirecteConnector)
    executor.assert_not_called()
    session_manager.assert_not_called()
    pronote_limiter.assert_not_called()


async def test_an_ed_account_without_children_follows_child_key_resource_ids(
    hass: HomeAssistant,
) -> None:
    """ED persists the selection as minted keys, not a second children list."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="EcoleDirecte",
        data={
            CONF_SOURCE: Source.ECOLEDIRECTE.value,
            "username": "demo.example.invalid",
            "password": "not-a-real-password",
            "qcm_json": {},
            CONF_CHILD_KEYS: [
                {
                    CHILD_KEY: "child-1",
                    CHILD_RESOURCE_ID: "1",
                    CHILD_NAME: "Enfant Un",
                }
            ],
        },
    )
    entry.add_to_hass(hass)
    account = PronoteAccount(
        hass,
        entry,
        connector_client=EcoleDirecteClient(RecordingTransport.scripted([])),
    )

    assert account._selected_children == ("1",)


def _ed_entry_data() -> dict[str, Any]:
    """Fictional ED entry: password persisted, selection only as child_keys."""
    return {
        CONF_SOURCE: Source.ECOLEDIRECTE.value,
        "username": "demo.example.invalid",
        "password": "not-a-real-password",
        "qcm_json": {},
        CONF_CHILD_KEYS: [
            {
                CHILD_KEY: "child-1",
                CHILD_RESOURCE_ID: "1",
                CHILD_NAME: "Enfant Un",
            }
        ],
    }


def _ed_collect_script() -> list[tuple[str, str, object]]:
    """Login plus one métier POST per capable tier for the first collection."""
    return [
        ("GET", "login.awp", {"gtk": "gtk-demo"}),
        ("POST", "login.awp", load_fixture("login_ok.json")),
        ("POST", "emploidutemps.awp", load_fixture("emploi_du_temps.json")),
        ("POST", "cahierdetexte.awp", load_fixture("cahier_de_texte.json")),
        ("POST", "notes.awp", load_fixture("notes.json")),
        ("POST", "viescolaire.awp", load_fixture("vie_scolaire.json")),
    ]


def _make_ed_client(
    script: list[tuple[str, str, object]],
) -> tuple[Any, RecordingTransport]:
    """Build a client that ignores the HA session and uses a scripted transport."""
    transport = RecordingTransport.scripted(script)

    def factory(*_args: object, **kwargs: object) -> EcoleDirecteClient:
        timeout = kwargs.get("read_timeout", 60.0)
        assert isinstance(timeout, (int, float))
        return EcoleDirecteClient(transport, read_timeout=float(timeout))

    return factory, transport


async def _setup_ed_entry(
    hass: HomeAssistant,
    *,
    script: list[tuple[str, str, object]] | None = None,
    options: dict[str, Any] | None = None,
) -> tuple[Any, Any]:
    """Stand an ED entry up through real platform forward."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    factory, _transport = _make_ed_client(script or _ed_collect_script())
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="EcoleDirecte",
        unique_id="ecoledirecte:demo.example.invalid",
        data=_ed_entry_data(),
        options=options or {},
    )
    entry.add_to_hass(hass)
    with (
        patch(
            "custom_components.carnet_scolaire.EcoleDirecteClient", side_effect=factory
        ),
        patch(
            "custom_components.carnet_scolaire.connectors.pronote.SessionManager",
            wraps=PronoteConnectorSessionManager,
        ) as session_manager,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry, session_manager


async def test_an_ed_entry_reaches_platforms_without_a_pronote_session(
    hass: HomeAssistant,
) -> None:
    """Limiter tiles and options must work; session_age and iCal must not."""
    from homeassistant.data_entry_flow import FlowResultType

    entry, session_manager = await _setup_ed_entry(hass, options={OPT_READ_TIMEOUT: 12})
    session_manager.assert_not_called()
    assert isinstance(entry.runtime_data.connector, EcoledirecteConnector)
    assert entry.runtime_data.connector.client.read_timeout == 12.0

    registry = er.async_get(hass)
    calls_id = registry.async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}_calls_today"
    )
    limiter_id = registry.async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}_limiter_state"
    )
    assert calls_id is not None
    assert limiter_id is not None
    assert hass.states.get(calls_id) is not None
    assert hass.states.get(limiter_id) is not None
    assert (
        registry.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_session_age")
        is None
    )
    assert (
        registry.async_get_entity_id(
            "sensor", DOMAIN, f"{entry.entry_id}_session_lifetime"
        )
        is None
    )
    assert (
        registry.async_get_entity_id(
            "sensor", DOMAIN, f"{entry.entry_id}_child-1_next_test"
        )
        is None
    )
    for key in ("outing_today", "test_today", "holidays"):
        assert (
            registry.async_get_entity_id(
                "binary_sensor", DOMAIN, f"{entry.entry_id}_child-1_{key}"
            )
            is None
        )

    class_id = registry.async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}_child-1_class_name"
    )
    assert class_id is not None
    class_state = hass.states.get(class_id)
    assert class_state is not None
    assert class_state.state == "4e Demo"

    options = await hass.config_entries.options.async_init(entry.entry_id)
    assert options["type"] is FlowResultType.MENU
    general = await hass.config_entries.options.async_configure(
        options["flow_id"], {"next_step_id": "general"}
    )
    general_keys = {
        getattr(key, "schema", key) for key in general["data_schema"].schema
    }
    assert "session_strategy" not in general_keys
    assert "write_operations_enabled" not in general_keys

    child = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, f"{entry.entry_id}_child-1"), entry.entry_id
    )
    assert child is not None
    for service in (SERVICE_GET_ICAL_URL, SERVICE_GENERATE_TIMETABLE_PDF):
        with pytest.raises(ServiceValidationError):
            await hass.services.async_call(
                DOMAIN,
                service,
                {ATTR_DEVICE_ID: child.id},
                blocking=True,
            )

    account = entry.runtime_data
    with patch.object(account, "async_request_tick"):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_REFRESH,
            {ATTR_DEVICE_ID: child.id},
            blocking=True,
        )
    status = await hass.services.async_call(
        DOMAIN,
        SERVICE_GET_RATE_LIMIT_STATUS,
        {ATTR_DEVICE_ID: child.id},
        blocking=True,
        return_response=True,
    )
    assert isinstance(status, dict)
    assert "calls_today" in status
    assert "scheduler" in status
    assert "authenticated" in status["session"]


def _english_entity_id(platform: str, child_name: str, key: str) -> str:
    """Entity id HA mints from the English translated name, never the key."""
    from homeassistant.util import slugify

    catalogue = json.loads(
        Path("custom_components/carnet_scolaire/translations/en.json").read_text(
            encoding="utf-8"
        )
    )
    name = catalogue["entity"][platform][key]["name"]
    return f"{platform}.{slugify(child_name)}_{slugify(name)}"


async def test_an_ecoledirecte_entry_creates_no_menu_entity(
    hass: HomeAssistant,
) -> None:
    """Empty canteen tiles were how a missing ED page looked on the dashboard.

    Coordinators are built for every ``Tier``, menus included, so
    ``account.coordinators.get(Tier.MENUS)`` is not ``None`` on this entry.
    Skipping the sensor because the coordinator is missing would therefore
    not fire -- the capabilities filter in ``sensor.async_setup_entry`` is
    what keeps ``menu_today`` and ``menu_tomorrow`` off an Ecoledirecte child.
    """
    entry, _session_manager = await _setup_ed_entry(hass)
    account = entry.runtime_data
    assert Tier.MENUS in account.coordinators
    assert Tier.MENUS not in account.connector.capabilities.tiers

    registry = er.async_get(hass)
    for key in ("menu_today", "menu_tomorrow"):
        assert (
            registry.async_get_entity_id(
                "sensor", DOMAIN, f"{entry.entry_id}_child-1_{key}"
            )
            is None
        )
        assert hass.states.get(_english_entity_id("sensor", "Enfant Un", key)) is None


async def test_an_ecoledirecte_options_page_omits_the_menus_tier(
    hass: HomeAssistant,
) -> None:
    """A menus checkbox on ED would look like the canteen page had been turned on.

    ``async_step_tiers`` walks ``_capable_tiers``. Without that filter the
    form still shows ``interval_menus`` and ``enabled_menus`` for a connector
    that cannot collect them, and storing the switch as on would be a silent
    no-op the user would read as a broken menu sensor.
    """
    from homeassistant.data_entry_flow import FlowResultType

    entry, _session_manager = await _setup_ed_entry(hass)

    options = await hass.config_entries.options.async_init(entry.entry_id)
    assert options["type"] is FlowResultType.MENU
    tiers = await hass.config_entries.options.async_configure(
        options["flow_id"], {"next_step_id": "tiers"}
    )
    assert tiers["type"] is FlowResultType.FORM
    fields = {str(key) for key in tiers["data_schema"].schema}

    assert OPT_TIER_INTERVAL.format(tier=Tier.MENUS.value) not in fields
    assert OPT_TIER_ENABLED.format(tier=Tier.MENUS.value) not in fields
    for tier in EcoledirecteConnector.CAPABILITIES.tiers:
        assert OPT_TIER_INTERVAL.format(tier=tier.value) in fields
        assert OPT_TIER_ENABLED.format(tier=tier.value) in fields


async def test_an_ecoledirecte_entry_offers_no_menu_button(
    hass: HomeAssistant,
) -> None:
    """A refresh-menus control must not appear for a connector without that page.

    ``BUTTONS`` currently has no menus key, so a registry scan of the live
    entry would pass even with the capabilities filter deleted. This test
    therefore asks ``button.async_setup_entry`` to consider a menus button
    while the menus coordinator is present -- the same double-skip trap as
    the sensors -- and checks the filter is what discards it.
    """
    from custom_components.carnet_scolaire.button import (
        PronoteButtonDescription,
        async_setup_entry as async_setup_buttons,
    )

    entry, _session_manager = await _setup_ed_entry(hass)
    account = entry.runtime_data
    assert Tier.MENUS in account.coordinators
    assert Tier.MENUS not in account.connector.capabilities.tiers

    registry = er.async_get(hass)
    assert (
        registry.async_get_entity_id(
            "button", DOMAIN, f"{entry.entry_id}_child-1_refresh"
        )
        is not None
    )
    assert (
        registry.async_get_entity_id(
            "button", DOMAIN, f"{entry.entry_id}_child-1_refresh_marks"
        )
        is not None
    )

    created: list[Any] = []

    def capture(entities: Any, *_args: Any, **_kwargs: Any) -> None:
        created.extend(entities)

    menus_button = PronoteButtonDescription(
        key="refresh_menus",
        tier=Tier.MENUS,
        requests=(Tier.MENUS,),
    )
    with patch(
        "custom_components.carnet_scolaire.button.BUTTONS",
        (menus_button,),
    ):
        await async_setup_buttons(hass, entry, capture)  # type: ignore[arg-type]

    assert created == []


async def test_an_ed_505_at_setup_opens_reauthentication(hass: HomeAssistant) -> None:
    """A runtime 505 is bad credentials, not a silently broken entry."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    factory, _transport = _make_ed_client(
        [
            ("GET", "login.awp", {"gtk": "gtk-demo"}),
            ("POST", "login.awp", load_fixture("login_505.json")),
        ]
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="EcoleDirecte",
        unique_id="ecoledirecte:demo.example.invalid-505",
        data=_ed_entry_data(),
    )
    entry.add_to_hass(hass)
    with patch(
        "custom_components.carnet_scolaire.EcoleDirecteClient", side_effect=factory
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = [
        flow
        for flow in hass.config_entries.flow.async_progress()
        if flow["handler"] == DOMAIN
    ]
    assert [flow["context"]["source"] for flow in flows] == [SOURCE_REAUTH]


async def test_an_ed_not_ready_retry_restores_the_punitive_limiter(
    hass: HomeAssistant,
) -> None:
    """A fresh limiter every 80 s is the weekend-of-thousands-of-logins defect."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    class BoomTransport:
        async def request(self, *_args: object, **_kwargs: object) -> object:
            raise OSError("scripted transport failure")

    def boom_factory(*_args: object, **_kwargs: object) -> EcoleDirecteClient:
        return EcoleDirecteClient(BoomTransport())

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="EcoleDirecte",
        unique_id="ecoledirecte:demo.example.invalid-transport",
        data=_ed_entry_data(),
    )
    entry.add_to_hass(hass)
    with patch(
        "custom_components.carnet_scolaire.EcoleDirecteClient", side_effect=boom_factory
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_RETRY
    stored = limiter_state_store(hass)[entry.entry_id]
    assert stored.get("hold_until") is not None

    def empty_factory(*_args: object, **_kwargs: object) -> EcoleDirecteClient:
        return EcoleDirecteClient(RecordingTransport.scripted([]))

    with patch(
        "custom_components.carnet_scolaire.EcoleDirecteClient",
        side_effect=empty_factory,
    ):
        await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_RETRY
    restored = limiter_state_store(hass)[entry.entry_id]
    assert restored.get("hold_until") is not None


async def test_a_stale_child_device_can_be_deleted(
    hass: HomeAssistant,
    account: PronoteAccount,
    mock_entry: MockConfigEntry,
) -> None:
    """The guide's only remedy for a superseded generation is the delete button.

    Without `async_remove_config_entry_device` Home Assistant hides that
    button and refuses the call with "Config entry does not support device
    removal". §10.5 and the v0.0.10 release notes both told the reader to
    delete the stale device, so the documentation promised something the code
    made impossible -- found by trying to follow my own instructions on a live
    instance and being refused.
    """
    del account
    devices = dr.async_get(hass)
    stale = devices.async_get_or_create(
        config_entry_id=mock_entry.entry_id,
        identifiers={(DOMAIN, f"{mock_entry.entry_id}_46#SUPERSEDED")},
        name="Enfant Un",
    )

    assert await async_remove_config_entry_device(hass, mock_entry, stale) is True


async def test_the_account_device_cannot_be_deleted(
    hass: HomeAssistant,
    account: PronoteAccount,
    mock_entry: MockConfigEntry,
) -> None:
    """Every child declares it as `via_device`.

    Removing it would leave the children pointing at a parent that no longer
    exists, which is the state that once flattened the whole device tree until
    the next restart. Allowing everything would have been the shorter hook and
    would have shipped that.
    """
    del account
    devices = dr.async_get(hass)
    parent = devices.async_get_device_by_identifier(
        (DOMAIN, mock_entry.entry_id), mock_entry.entry_id
    )
    assert parent is not None, "the account device is missing from the fixture"

    assert await async_remove_config_entry_device(hass, mock_entry, parent) is False


async def test_a_boosted_tier_is_collected_as_a_gesture(
    account: PronoteAccount,
) -> None:
    """A refresh pressed at 23:00 must not wait for 06:00.

    Every boost is armed by a person -- the button, the refresh service, or a
    write re-reading its own tier -- so the collection it asks for is a gesture
    and crosses quiet hours. Asserted on the shared fixture, which has already
    collected every tier: the first-collection ``CRITICAL`` branch is out of
    the way, so what is observed is the boost and nothing else. The second half
    matters as much as the first: once the boost is served the tier returns to
    its declared priority, or a single press would become a standing
    exemption.
    """
    tier = Tier.MARKS
    assert _priority_for(account, tier, "STUDENT-1") is TIER_PRIORITY[tier]

    account.scheduler.request([tier])
    assert _priority_for(account, tier, "STUDENT-1") is Priority.GESTURE

    account.scheduler.mark_collected(tier)
    assert _priority_for(account, tier, "STUDENT-1") is TIER_PRIORITY[tier]


class TestHowLongAFirstCollectionOutranksQuietHours:
    """The dispensation, and the exit it was missing.

    A tier with no snapshot outranks quiet hours because its first collection
    is the one a user is watching for. That was described as self-limiting
    because a tier that collects stops qualifying -- true, and insufficient:
    **success was its only exit**. A tier that can never succeed stays
    data-less for ever and therefore held a standing exemption, and on a live
    instance the establishment's unusable teaching-staff tab became the only
    tier awake between 22:00 and 06:00. Being alone, it was the only tier able
    to accumulate failures, with no other tier's success available to reset the
    account's back-off: one broken tab produced an account-wide ``backoff`` and
    took every other tier's freshness down with it.

    Driven on an account that has been set up but has collected nothing, which
    is the state the dispensation is *about*. The shared ``account`` fixture
    has already collected every tier, so ``has_data`` is true there and this
    branch is unreachable -- a test written against that fixture would assert
    the declared priority in every case and pass whatever the bound was.
    """

    @staticmethod
    async def _fresh(
        hass: HomeAssistant, mock_entry: MockConfigEntry, client: FakeClient
    ) -> PronoteAccount:
        """An account that knows its shape and holds no snapshot."""
        account = PronoteAccount(hass, mock_entry)
        with patch(
            "custom_components.carnet_scolaire.session.build_client",
            return_value=client,
        ):
            await account.async_setup()
            await hass.async_block_till_done()
        return account

    async def test_a_tier_with_no_data_yet_outranks_quiet_hours(
        self,
        hass: HomeAssistant,
        mock_entry: MockConfigEntry,
        parent_client: FakeClient,
        no_spacing: None,
    ) -> None:
        """The dispensation itself, which must keep working.

        Asserted first and separately, because every bound below is only
        defensible if the thing being bounded still exists. An instance
        installed at 23:00 that shows nothing until 06:00 reads as broken, and
        that is what this branch is for.
        """
        del no_spacing
        account = await self._fresh(hass, mock_entry, parent_client)
        try:
            assert (
                _priority_for(account, Tier.STATIC, "STUDENT-1") is Priority.CRITICAL
            ), "a tier that has never collected must still outrank quiet hours"
        finally:
            await account.async_unload()
            await hass.async_block_till_done()

    async def test_a_tier_that_keeps_failing_loses_the_dispensation(
        self,
        hass: HomeAssistant,
        mock_entry: MockConfigEntry,
        parent_client: FakeClient,
        no_spacing: None,
    ) -> None:
        """The exit that was missing, expressed as evidence rather than success.

        The argument for the exemption is about a first collection that
        *works*; it cannot survive the demonstration that this one does not.
        After the third failure the tier drops back to its declared priority,
        so quiet hours apply to it again and it waits for 06:00 like
        everything else -- while keeping its deadline and its floored retry.
        """
        del no_spacing
        account = await self._fresh(hass, mock_entry, parent_client)
        try:
            for _ in range(_FIRST_COLLECTION_ATTEMPTS):
                account.scheduler.mark_failed(Tier.STATIC, 0.0)

            assert (
                _priority_for(account, Tier.STATIC, "STUDENT-1")
                is (TIER_PRIORITY[Tier.STATIC])
            ), "a tab that does not work must stop being the only tier awake at 04:00"
        finally:
            await account.async_unload()
            await hass.async_block_till_done()

    async def test_one_failure_does_not_spend_the_dispensation(
        self,
        hass: HomeAssistant,
        mock_entry: MockConfigEntry,
        parent_client: FakeClient,
        no_spacing: None,
    ) -> None:
        """The guard on the bound: a transient failure must not cost it.

        A school's server restarting, or one malformed response, is exactly
        the case the dispensation was granted for -- a fresh install that has
        nothing to show yet. Bounding it at the *first* failure would hand a
        new user an empty dashboard until morning for a fault that had already
        cleared.
        """
        del no_spacing
        account = await self._fresh(hass, mock_entry, parent_client)
        try:
            account.scheduler.mark_failed(Tier.STATIC, 0.0)

            assert (
                _priority_for(account, Tier.STATIC, "STUDENT-1") is Priority.CRITICAL
            ), "one bad response is not evidence that the collection cannot work"
        finally:
            await account.async_unload()
            await hass.async_block_till_done()


class TestWhenTheFirstBatchIsAllowedToRun:
    """The first collection must not race the entities it collects for.

    A defect measured on a live instance, and the second one this month whose
    symptom was "every entity is empty" with a different cause. Set-up learns
    the account's shape, then forwards the platforms -- but the first batch was
    scheduled from `async_setup`, which is *before* that. A coordinator
    publishing before its entities have subscribed pushes to nobody: each
    entity is added afterwards, renders once from `coordinator.data`, and a
    tier whose snapshot landed inside that window renders `unavailable` with
    its data already present.

    What makes it permanent rather than transient is that the tier is
    genuinely collected. The scheduler holds its deadline and `has_data` is
    true, so the first-collection dispensation of `collect_tier` no longer
    applies and a `refresh` is refused -- correctly, and to no effect. The
    entity then waits a full tier interval: three hours for `marks`, a day for
    `menus` and `history`, and with quiet hours on, until 06:00.

    Measured: ten tiers placed their requests and published; nineteen entities
    across seven tiers stayed `unavailable`. The only two that displayed were
    the two whose snapshots landed before the platforms were forwarded.
    """

    async def test_setting_the_account_up_does_not_collect_on_its_own(
        self,
        hass: HomeAssistant,
        mock_entry: MockConfigEntry,
        parent_client: FakeClient,
        school_day: FrozenDateTimeFactory,
        no_spacing: None,
    ) -> None:
        """The ordering contract, asserted where it can be asserted at all.

        This is the one test that pins the *cause* rather than the symptom.
        `async_setup` must leave the account collected-nothing, so that the
        only thing which can start a batch is the explicit call made after
        `async_forward_entry_setups`. Asserting the symptom instead -- "no
        entity is empty" -- cannot catch a reintroduction, because the race is
        won by whichever of two tasks the loop happens to run first, and on an
        idle machine that was the harmless order roughly nineteen times in
        twenty.

        `completed_ticks` and the coordinators together, because either alone
        is satisfiable by accident: a batch that ran and found nothing due
        would leave the coordinators empty, and a batch that never ran leaves
        the counter at zero.
        """
        del school_day, no_spacing
        account = PronoteAccount(hass, mock_entry)

        with patch(
            "custom_components.carnet_scolaire.session.build_client",
            return_value=parent_client,
        ):
            await account.async_setup()
            await hass.async_block_till_done()
            try:
                assert account.completed_ticks == 0
                assert all(
                    not coordinator.data
                    for tier, coordinator in account.coordinators.items()
                    if tier is not Tier.SESSION
                )
            finally:
                await account.async_unload()
                await hass.async_block_till_done()

    async def test_every_tier_that_collected_has_an_entity_that_shows_it(
        self, hass: HomeAssistant, account: PronoteAccount
    ) -> None:
        """The promise, stated the way a dashboard experiences it.

        Read through the entity registry by `unique_id` rather than by
        entity id: the identifier's suffix comes from the translated *name*,
        and guessing it from the translation key is the mistake that put
        eleven identifiers that do not exist into annexe A.

        Every tier holding a snapshot is required to have every one of its
        sensors available -- not a sampled one. The defect this replaces hit
        seven tiers out of ten and spared the two that happened to publish
        early, so a test naming one tier would have passed throughout.
        """
        registry = er.async_get(hass)
        student_id = account.state.students[0].id
        key = child_key(account.entry, student_id)

        empty: list[str] = []
        checked = 0
        for description in (*PRIMITIVE_SENSORS, *LIST_SENSORS):
            coordinator = account.coordinators.get(description.tier)
            if coordinator is None or not coordinator.data:
                continue
            unique_id = f"{account.entry.entry_id}_{key}_{description.key}"
            entity_id = registry.async_get_entity_id("sensor", DOMAIN, unique_id)
            if entity_id is None:
                continue
            checked += 1
            state = hass.states.get(entity_id)
            if state is None or state.state == "unavailable":
                empty.append(entity_id)

        assert not empty, (
            f"tiers collected but these entities stayed empty: {empty}; "
            f"schedule={account.scheduler.diagnostics()}"
        )
        # A guard on the guard: if the descriptions or the registry lookup ever
        # stop matching, the loop above would assert nothing at all and pass.
        assert checked > 20


class TestWhatSurvivesAReload:
    """A reload must never leave an entity empty.

    This is a defect that reached a live instance. An options save reloads the
    config entry; the reload handed the *schedule* across but built fresh,
    empty coordinators. The new scheduler therefore believed every tier was
    freshly collected, `due()` returned nothing, and the batch that ran was a
    batch of nothing -- so every child entity read `unavailable` for a whole
    tier interval, which is twenty-four hours for `menus`, `static` and
    `history`. Inside quiet hours the limiter refused to refill them at all, so
    a reload at 22:10 emptied the dashboard until 06:00.
    """

    async def test_the_data_survives_so_no_entity_goes_empty(
        self, hass: HomeAssistant, account: PronoteAccount
    ) -> None:
        """The fix, stated as the user-visible promise rather than a mechanism.

        Asserted on the state machine and not on the coordinators, because
        "the entity has a value" is the thing that was broken; a test that
        checked `coordinator.data` could pass with the entity still
        unavailable.
        """
        # Read from `translations/en.json` rather than derived from the
        # translation key: the suffix comes from the *name*, and guessing
        # it is the mistake that put eleven identifiers that do not exist
        # into annexe A.
        entity_id = "sensor.enfant_un_timetable_this_week"
        before = hass.states.get(entity_id)
        assert before is not None
        assert before.state not in ("unavailable", "unknown")

        await hass.config_entries.async_reload(account.entry.entry_id)
        await hass.async_block_till_done()

        after = hass.states.get(entity_id)
        assert after is not None
        assert after.state == before.state

    async def test_the_data_survives_a_login_that_renames_the_children(
        self,
        hass: HomeAssistant,
        account: PronoteAccount,
        parent_client: FakeClient,
    ) -> None:
        """The reload's own login rotates the identifiers; the data must follow.

        Measured on a live instance: an options save reloaded the entry, the
        new login announced the child under a fresh ``46#<signature>``, and
        every one of its forty-four entities went ``unavailable`` for six
        minutes -- and would have stayed so for each tier's whole interval but
        for a manual refresh. The snapshots had been carried under the old
        identifier, where no entity looked, while each tier counted as
        restored and kept its deadline.

        The control is the same as the previous test's: nothing is re-read,
        because nothing was lost. Re-collecting on every rotation would make
        each options save cost a full batch, which §7.3 forbids.
        """
        entity_id = "sensor.enfant_un_timetable_this_week"
        before = hass.states.get(entity_id)
        assert before is not None
        assert before.state not in ("unavailable", "unknown")
        ids_before = {student.id for student in account.students}
        posted = len(parent_client.posted_names)

        parent_client.rotate_identifiers()
        await hass.config_entries.async_reload(account.entry.entry_id)
        await hass.async_block_till_done()

        reloaded: PronoteAccount = hass.data[DOMAIN][account.entry.entry_id]
        assert {student.id for student in reloaded.students}.isdisjoint(ids_before)
        after = hass.states.get(entity_id)
        assert after is not None
        assert after.state == before.state
        for student in reloaded.students:
            snapshot = reloaded.snapshot(Tier.MENUS, student.id)
            assert snapshot is not None
            assert snapshot.student_id == student.id
        placed = parent_client.posted_names[posted:]
        assert FUNC_TIMETABLE[0] not in placed
        assert FUNC_MENUS[0] not in placed

    async def test_a_tier_whose_data_did_not_survive_is_due_again(
        self,
        hass: HomeAssistant,
        account: PronoteAccount,
        parent_client: FakeClient,
    ) -> None:
        """The invariant: no data means due, whatever the schedule claims.

        The schedule and the data are only sound *together*. This drops one
        tier's snapshot from the hand-off while leaving its deadline in place
        -- exactly the shape the defect had -- and requires the scheduler to
        ignore that deadline. Without the pairing the tier would sit idle,
        holding nothing, until its interval elapsed.
        """
        from custom_components.carnet_scolaire.account import (
            _saved_schedule,
            _saved_snapshots,
        )

        entry_id = account.entry.entry_id
        before = len(parent_client.posted_names)
        await hass.config_entries.async_unload(entry_id)
        await hass.async_block_till_done()

        # The schedule remembers every tier; the data forgets the timetable.
        inherited = _saved_schedule(hass)[entry_id]
        assert str(Tier.TIMETABLE) in inherited
        assert str(Tier.MENUS) in inherited
        _saved_snapshots(hass)[entry_id].pop(Tier.TIMETABLE)

        await hass.config_entries.async_setup(entry_id)
        await hass.async_block_till_done()

        account = hass.data[DOMAIN][entry_id]

        # Collected again, because it had nothing. The snapshot is the proof:
        # it was dropped from the hand-off above, so its only possible source
        # is a fresh request placed despite the inherited deadline.
        assert account.snapshot(Tier.TIMETABLE, CHILDREN[0][0]) is not None

        # The control, and the reason this is not simply "re-collect
        # everything on reload": `menus` kept its data, so it kept its
        # deadline and was never asked for. §7.3 -- tuning the cadence must
        # not cost a round of requests -- still holds.
        #
        # Asserted on the requests actually placed rather than on
        # `last_collected`, because the suite freezes the clock: a tier
        # re-collected under a frozen clock records the very instant it
        # inherited, so the two are indistinguishable by value. `posts` is
        # the ground truth for what went to the wire.
        placed = parent_client.posted_names[before:]
        assert FUNC_TIMETABLE[0] in placed
        assert FUNC_MENUS[0] not in placed

    async def test_an_empty_carry_restores_nothing_and_claims_nothing(
        self, hass: HomeAssistant, account: PronoteAccount
    ) -> None:
        """A tier carried as an empty mapping is not data.

        Reporting it as restored would hand its deadline back and strand its
        entities -- the defect, reintroduced through the door marked "we did
        carry something for that tier".
        """
        assert account._restore_snapshots(None) == frozenset()
        assert account._restore_snapshots({Tier.TIMETABLE: {}}) == frozenset()

    async def test_a_tier_with_no_data_collects_ahead_of_quiet_hours(
        self, hass: HomeAssistant, account: PronoteAccount
    ) -> None:
        """The half of the fix a reload cannot cover: a cold restart.

        After a real restart the hand-off store is empty, so every tier is due
        -- but during quiet hours the limiter refuses everything below
        `critical`, and an instance restarted at 23:00 published nothing at all
        until 06:00. §2.5 is explicit that an unavailable entity breaks
        automations rather than merely looking empty, and the first collection
        is precisely the one somebody is watching for.

        `critical` is not a new hole: it is the priority the login itself uses,
        and the quiet-hours branch of `RateLimiter.check` already exempts it.
        """
        del hass
        student_id = CHILDREN[0][0]
        recorded: list[Priority] = []
        original = account.extras.session.run

        async def spy(name: str, priority: Priority, fn: Any, **kwargs: Any) -> Any:
            recorded.append(priority)
            return await original(name, priority, fn, **kwargs)

        # A tier that has never produced anything, which is what a cold
        # restart leaves behind.
        account.coordinators[Tier.MENUS].data = {}
        with patch.object(account.extras.session, "run", spy):
            await collect_tier(account, Tier.MENUS, student_id)

        assert recorded == [Priority.CRITICAL]

    async def test_a_tier_that_already_has_data_keeps_its_own_priority(
        self, hass: HomeAssistant, account: PronoteAccount
    ) -> None:
        """The dispensation is one batch per tier, not a standing exemption.

        This is the assertion that keeps the previous one honest. If the
        priority did not fall back once a snapshot exists, `menus` would
        outrank quiet hours every night for the rest of the year -- which is
        the opposite of what quiet hours are for.
        """
        del hass
        student_id = CHILDREN[0][0]
        assert account.snapshot(Tier.MENUS, student_id) is not None

        recorded: list[Priority] = []
        original = account.extras.session.run

        async def spy(name: str, priority: Priority, fn: Any, **kwargs: Any) -> Any:
            recorded.append(priority)
            return await original(name, priority, fn, **kwargs)

        with patch.object(account.extras.session, "run", spy):
            await collect_tier(account, Tier.MENUS, student_id)

        assert recorded == [TIER_PRIORITY[Tier.MENUS]]
        assert TIER_PRIORITY[Tier.MENUS] is not Priority.CRITICAL


class TestWhichTimezonePronotesNaiveTimesAreReadIn:
    """The one option whose default is another system's setting, not a constant.

    §4.2: PRONOTE sends local times with no offset, the conversion happens once
    in the gateway, and the zone is `establishment_timezone` "whose default is
    Home Assistant's". Two consequences are asserted below, and both were
    wrong at some point.

    A *default* is not a stored value. The options page used to submit the
    instance's zone back as though the user had typed it, which pinned it: from
    then on the account never looked at `hass.config.time_zone` again, so
    correcting the instance's timezone -- the ordinary fix after a move, or
    after a first install left it on UTC -- silently changed nothing here.

    And a stored value is not necessarily a zone. The field is free text, and
    the string reaches `ZoneInfo` on the set-up path, where an unknown name
    fails the entry outright.
    """

    def test_nothing_stored_means_follow_the_instance(
        self, hass: HomeAssistant
    ) -> None:
        """Absence is the default, and the default is live rather than copied.

        Read at every set-up, so a user who fixes their instance's timezone
        gets the correction on the next reload without touching this
        integration.
        """
        assert _establishment_timezone(hass, {}) == str(hass.config.time_zone)

    def test_an_empty_string_is_not_a_timezone_either(
        self, hass: HomeAssistant
    ) -> None:
        """An emptied box must read as absence, not as a zone named "".

        Home Assistant's frontend may send a cleared optional text field as
        `""` rather than omitting it, and an entry stored before the field
        became clearable can carry one. `ZoneInfo("")` raises, so treating it
        as a value would fail the entry on a value nobody chose.
        """
        assert _establishment_timezone(hass, {OPT_ESTABLISHMENT_TIMEZONE: ""}) == str(
            hass.config.time_zone
        )

    def test_a_stored_zone_overrides_the_instance(self, hass: HomeAssistant) -> None:
        """The reason the option exists at all.

        A family in one timezone may follow a school in another, and PRONOTE
        never says which -- the protocol does not carry it. So a deliberate pin
        has to win over the instance, or the option would be decoration.
        """
        assert (
            _establishment_timezone(
                hass, {OPT_ESTABLISHMENT_TIMEZONE: "Pacific/Noumea"}
            )
            == "Pacific/Noumea"
        )

    def test_an_unusable_stored_zone_falls_back_and_says_which(
        self, hass: HomeAssistant, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Falling back beats raising, for the reason `bounded_option` exists.

        The options page is the only place that checks this string, and it is
        not on the path a restored backup or a hand-edited `.storage` takes.
        Raising there costs every entity of the account with `unavailable` as
        the only explanation; falling back costs an hour of offset on a school
        abroad and writes down what it did.
        """
        caplog.set_level(logging.WARNING)

        resolved = _establishment_timezone(
            hass, {OPT_ESTABLISHMENT_TIMEZONE: "Europe/Pariss"}
        )

        assert resolved == str(hass.config.time_zone)
        assert "Europe/Pariss" in caplog.text
        assert str(hass.config.time_zone) in caplog.text

    async def test_an_entry_pinned_to_a_nonsense_zone_still_loads(
        self,
        hass: HomeAssistant,
        mock_entry: MockConfigEntry,
        parent_client: FakeClient,
        school_day: Any,
        no_spacing: None,
    ) -> None:
        """The failure this is about, at the altitude a user sees it from.

        `PronoteGateway.__init__` calls `ZoneInfo(name)` and
        `PronoteAccount.__init__` calls that, so an unknown zone used to raise
        out of `async_setup_entry`: no session, no entities, and a log line
        about zone files rather than about a setting. The account is the
        application's data source, so a typo in one text box must not be able
        to take it off the air.
        """
        del school_day, no_spacing
        hass.config_entries.async_update_entry(
            mock_entry,
            options={**mock_entry.options, OPT_ESTABLISHMENT_TIMEZONE: "Paris"},
        )

        with patch(
            "custom_components.carnet_scolaire.session.build_client",
            return_value=parent_client,
        ):
            assert await hass.config_entries.async_setup(mock_entry.entry_id)
            await hass.async_block_till_done()

        assert mock_entry.state is ConfigEntryState.LOADED
        extras = mock_entry.runtime_data.extras
        assert extras is not None
        assert str(extras.gateway.timezone) == str(hass.config.time_zone)


async def test_a_refresh_naming_a_tier_the_source_lacks_asks_for_nothing(
    hass: HomeAssistant,
) -> None:
    """An automation shared between two sources must not fail on one of them.

    ``refresh`` is source-agnostic, so it is reachable on an Ecoledirecte
    entry -- but the tier list in a blueprint written for PRONOTE names pages
    Ecoledirecte does not have. Filtering them out leaves nothing to ask for,
    and that has to be a no-op: refusing would break the automation, and
    reading "nothing left" as "everything" would spend a full round of
    requests for a tier list that matched none of them.
    """
    entry, _session_manager = await _setup_ed_entry(hass)
    account = entry.runtime_data
    before = account.completed_ticks

    await hass.services.async_call(
        DOMAIN,
        SERVICE_REFRESH,
        {
            "device_id": _ed_account_device(hass, entry),
            "tiers": [Tier.MENUS.value],
        },
        blocking=True,
    )
    await hass.async_block_till_done()

    assert account.completed_ticks == before
    assert account.scheduler.boosted_tiers() == ()


async def test_a_service_the_source_does_not_declare_is_refused_by_name(
    hass: HomeAssistant,
) -> None:
    """Refused for the right reason, and the reason names the source.

    Ecoledirecte declares no services at all. A refusal that said only
    "unsupported" would send a user looking at their PRONOTE credentials; the
    message has to say which source is answering, because on a mixed household
    the same action works on the entry next to it.
    """
    entry, _session_manager = await _setup_ed_entry(hass)

    with pytest.raises(ServiceValidationError) as raised:
        await hass.services.async_call(
            DOMAIN,
            SERVICE_GET_ICAL_URL,
            {"device_id": _ed_account_device(hass, entry)},
            blocking=True,
            return_response=True,
        )

    assert raised.value.translation_key == "service_not_supported"
    assert raised.value.translation_placeholders == {
        "source": "ecoledirecte",
        "service": SERVICE_GET_ICAL_URL,
    }


async def test_an_ecoledirecte_homework_list_refuses_a_tick(
    hass: HomeAssistant,
) -> None:
    """Tapping a checkbox must fail cleanly, not half-write.

    The list is offered because Ecoledirecte collects homework; writing it
    back is a PRONOTE-only path that lives on ``PronoteExtras``. Without the
    guard the tick would reach ``None.gateway`` and raise ``AttributeError``,
    which Home Assistant shows as an unexplained error on the card instead of
    a sentence naming the source.

    Writes are enabled on purpose: with them off the list is read-only through
    its supported features and the checkbox is never offered, so the guard
    that matters -- the one behind an *enabled* write -- would stay
    unreachable.
    """
    from homeassistant.components.todo import TodoItem, TodoItemStatus

    from custom_components.carnet_scolaire.todo import PronoteHomeworkTodoList

    entry, _session_manager = await _setup_ed_entry(
        hass, options={OPT_WRITE_OPERATIONS_ENABLED: True}
    )
    account = entry.runtime_data
    assert account.extras is None
    assert account.write_enabled

    entity = PronoteHomeworkTodoList(
        account,
        account.coordinators[Tier.HOMEWORK],
        account.students[0],
    )

    with pytest.raises(ServiceValidationError) as raised:
        await entity.async_update_todo_item(
            TodoItem(
                uid="HOMEWORK-1",
                summary="Exercices 4 a 7",
                status=TodoItemStatus.COMPLETED,
            )
        )

    assert raised.value.translation_key == "homework_tick_not_supported"
    assert raised.value.translation_placeholders == {"source": "ecoledirecte"}


def _ed_account_device(hass: HomeAssistant, entry: Any) -> str:
    """The account device of an Ecoledirecte entry.

    Looked up through the owning entry: from HA 2026.9 an identifier is only
    unique within a config entry, and the entry-less lookup is deprecated.
    """
    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, entry.entry_id), entry.entry_id
    )
    assert device is not None
    return device.id


async def test_a_reauthentication_clears_the_hold_on_the_live_account_too(
    hass: HomeAssistant, mock_entry: MockConfigEntry, account: PronoteAccount
) -> None:
    """Three places hold the penalty, and the live account is the one that bites.

    Clearing only the flow's guard and the saved state leaves the loaded
    account holding its own counters, so the very next tick walks into the
    hold the user just cleared by retyping their password -- and the repair
    card comes straight back, which reads as "the new password is wrong too".
    """
    from custom_components.carnet_scolaire.login_guard import (
        clear_login_penalties,
        limiter_state_store,
    )
    from custom_components.carnet_scolaire.ratelimit import LoginOutcome

    account.limiter.note_login(LoginOutcome.BAD_CREDENTIALS)
    limiter_state_store(hass)[mock_entry.entry_id] = account.limiter.export_state()
    assert account.limiter.snapshot_counters()["failed_logins_hour"] > 0

    clear_login_penalties(hass, mock_entry.entry_id)

    assert account.limiter.snapshot_counters()["failed_logins_hour"] == 0
    assert mock_entry.entry_id not in limiter_state_store(hass)


async def test_a_reauthentication_with_no_entry_still_clears_the_flow_guard(
    hass: HomeAssistant,
) -> None:
    """A re-auth can begin before any entry exists to repair.

    The flow's own guard is shared by every attempt on the instance, so a
    person who mistyped a password twice in the *add* flow must not arrive at
    the repair flow already held. Passing no entry id is the ordinary shape of
    that call, and it must not reach into ``hass.data`` for an entry that is
    not there.
    """
    from custom_components.carnet_scolaire.login_guard import (
        clear_login_penalties,
        login_guard,
    )
    from custom_components.carnet_scolaire.ratelimit import LoginOutcome

    login_guard(hass).note_login(LoginOutcome.BAD_CREDENTIALS)
    assert login_guard(hass).snapshot_counters()["failed_logins_hour"] > 0

    clear_login_penalties(hass, None)

    assert login_guard(hass).snapshot_counters()["failed_logins_hour"] == 0


async def test_a_tick_is_skipped_while_the_previous_batch_is_still_running(
    hass: HomeAssistant, account: PronoteAccount
) -> None:
    """Skipping, never queueing.

    A queued tick lets a slow server build a backlog that then arrives all at
    once -- which is the burst the single heartbeat exists to prevent. Skipping
    is safe because the deadlines have not moved: the next tick picks the same
    tiers up.
    """
    before = account.completed_ticks

    await account._tick_lock.acquire()
    try:
        await account._async_tick()
        await account.async_request_tick()
        await hass.async_block_till_done()
    finally:
        account._tick_lock.release()

    assert account.completed_ticks == before


async def test_nothing_is_collected_once_the_account_is_stopping(
    hass: HomeAssistant, account: PronoteAccount
) -> None:
    """Unload must not race a batch into a session it has just closed.

    The executor is shut down on unload, so a tier that started afterwards
    raises from inside the worker -- which Home Assistant reports as an error
    during teardown, for an entry the user simply removed.
    """
    before = account.completed_ticks
    account._shutting_down = True
    try:
        await account._async_tick()
        await account.async_request_tick()
        await hass.async_block_till_done()
    finally:
        account._shutting_down = False

    assert account.completed_ticks == before


async def test_a_tier_with_no_coordinator_is_skipped_rather_than_crashing(
    account: PronoteAccount,
) -> None:
    """A tier can be capable and still have no coordinator: disabled in options.

    Both readers have to agree. ``_async_collect`` returning early keeps the
    batch going, and ``snapshot`` answering ``None`` keeps every entity that
    asks about that tier on the "no data" path rather than on an
    ``AttributeError``.
    """
    removed = account.coordinators.pop(Tier.MENUS)
    try:
        await account._async_collect(Tier.MENUS)
        assert account.snapshot(Tier.MENUS, account.students[0].id) is None
    finally:
        account.coordinators[Tier.MENUS] = removed


async def test_a_child_with_no_minted_key_falls_back_loudly(
    account: PronoteAccount, caplog: pytest.LogCaptureFixture
) -> None:
    """The fallback is a bug report, not a feature.

    Returning the PRONOTE resource identifier keeps the entry loading -- which
    is right, because raising would lose every child's entities to protect the
    identity of one -- but that identifier rotates, so the entities built on it
    are orphaned at the next session. The log line is the only thing that turns
    a silent orphaning into something a user can report.
    """
    caplog.set_level(logging.ERROR, logger="custom_components.carnet_scolaire.account")
    student_id = account.students[0].id
    keys = dict(account._child_keys)
    account._child_keys.clear()
    try:
        assert account.stable_key(student_id) == student_id
    finally:
        account._child_keys.update(keys)

    assert "no minted key" in caplog.text
    assert "not stable between sessions" in caplog.text


async def test_a_deferred_tier_is_deferred_and_not_recorded_as_a_failure(
    account: PronoteAccount,
) -> None:
    """Postponed by the limiter is not a broken page.

    Counting it as a failure would drive the tier's consecutive-failure count
    up and, at three, cost it the first-collection dispensation -- so a tight
    budget would look exactly like a tab the establishment does not publish.
    """
    from custom_components.carnet_scolaire.ratelimit import TierDeferred

    record = account.state.records.setdefault(Tier.MENUS, TierRecord())
    failures_before = record.consecutive_failures

    with patch.object(
        account.connector,
        "async_collect",
        side_effect=TierDeferred("the daily cap is close", 900.0),
    ):
        await account._async_collect(Tier.MENUS)

    assert record.consecutive_failures == failures_before


async def test_an_authentication_refusal_stops_the_batch_and_holds_the_retry(
    account: PronoteAccount,
) -> None:
    """The defect here got an address suspended.

    An authentication refusal leaves the *consecutive* failure counter at
    zero -- bad credentials, a demanded PIN and an unreadable bootstrap all
    do -- so the exponential back-off is still zero seconds. Deferring by that
    made the tier due again on the very next tick, and ten tiers then
    attempted ten logins a tick.
    """
    from custom_components.carnet_scolaire.session import InvalidCredentials

    with patch.object(
        account.connector,
        "async_collect",
        side_effect=InvalidCredentials("the password was refused"),
    ):
        await account._async_collect(Tier.MENUS)

    # Not due again immediately: the limiter's retry delay is what holds it.
    assert Tier.MENUS not in account.scheduler.due()


@pytest.mark.parametrize(
    ("opener", "issue"),
    [
        pytest.param(
            "async_open_credentials_issue", ISSUE_INVALID_CREDENTIALS, id="credentials"
        ),
        pytest.param("async_open_mfa_issue", ISSUE_MFA_REQUIRED, id="mfa"),
    ],
)
async def test_an_authentication_problem_asks_the_user_rather_than_retrying(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    account: PronoteAccount,
    opener: str,
    issue: str,
) -> None:
    """Neither of these can be fixed by trying again, and trying again is costly.

    A wrong password and a demanded PIN both count against the IP guard, whose
    sanction is the undocumented and expensive one. So the integration stops
    and opens a repair card; the card is the only thing that tells the user
    the integration is waiting for them rather than broken.
    """
    getattr(account, opener)()

    assert _issue(hass, mock_entry, issue) is not None
