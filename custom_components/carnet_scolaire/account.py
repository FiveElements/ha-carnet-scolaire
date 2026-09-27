"""``PronoteAccount``: one per config entry, and the only thing that ticks.

The master heartbeat asks the scheduler what is due, then runs those tiers in
one session, spaced by the limiter (§5.1). Nothing else in the integration owns
a timer that reaches the network.

The order of operations inside a tick is not incidental:

1. tiers are served in priority order, because the limiter sheds by priority and
   serving out of order would drop the wrong ones (annexe B §2.4);
2. a collection that succeeds publishes its snapshot *and then* runs the delta
   detector, so an event never fires for data the entities do not yet hold;
3. a collection that is deferred moves its deadline and keeps its snapshot;
4. a collection that fails flips the coordinator's success flag and keeps its
   snapshot too -- "I know, but it is old" beats "I no longer know" on school
   data (§4.4).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
import logging
import time
from typing import TYPE_CHECKING, Any, Final

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import (
    device_registry as dr,
    entity_registry as er,
    issue_registry as ir,
)
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from .child_keys import followed, is_legacy_selection, is_minted, pair
from .connectors.factory import build_connector, source_from_entry_data
from .connectors.protocol import LimiterView, PronoteExtras, Source, has_pronote_extras
from .const import (
    CHILD_RESOURCE_ID,
    CONF_ACCOUNT_PIN,
    CONF_CHILD_KEYS,
    CONF_CHILDREN,
    CONF_CLIENT_IDENTIFIER,
    CONF_DEVICE_NAME,
    CONF_ENT,
    CONF_LOGIN_MODE,
    CONF_PRONOTE_URL,
    CONF_UUID,
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_MASTER_TICK,
    DEFAULT_READ_TIMEOUT,
    DEFAULT_SESSION_STRATEGY,
    DEFAULT_STALE_AFTER,
    DOMAIN,
    ISSUE_ACCOUNT_UNREADABLE,
    ISSUE_BOOTSTRAP_FAILED,
    ISSUE_DAILY_CAP_NEAR,
    ISSUE_INVALID_CREDENTIALS,
    ISSUE_MFA_REQUIRED,
    LEGACY_WRITE_OPERATIONS_ENABLED,
    OPT_CONNECT_TIMEOUT,
    OPT_ESTABLISHMENT_TIMEZONE,
    OPT_HISTORY_PERIODS,
    OPT_MASTER_TICK,
    OPT_READ_TIMEOUT,
    OPT_SESSION_STRATEGY,
    OPT_STALE_AFTER,
    OPT_WRITE_OPERATIONS_ENABLED,
    LoginMode,
    SessionStrategy,
    Tier,
)
from .coordinator import PronoteTierCoordinator
from .delta import DeltaDetector
from .failures import describe_failure, is_traceback_safe
from .login_guard import limiter_state_store
from .models import Period, SessionFacts, Snapshot, Student
from .options import (
    bounded_option,
    build_rate_limit_config,
    tier_enabled,
    tier_intervals,
)
from .ratelimit import TierDeferred
from .scheduler import FetchScheduler, default_plans
from .session import (
    AccountUnreadable,
    BootstrapFailed,
    IntegrationFault,
    InvalidCredentials,
    LoginRefused,
    MfaRequired,
    SessionCredentials,
)
from .urls import public_url

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from . import PronoteConfigEntry
    from .connectors.ecoledirecte.ed_client import EcoleDirecteClient
    from .connectors.protocol import ConnectorCapabilities
    from .coordinator import TierData
    from .delta import DeltaEvent

_LOGGER: Final = logging.getLogger(__name__)

#: Signal fired on the Home Assistant bus for every detected change. The
#: ``event`` entities listen for it; nothing else should.
SIGNAL_DELTA: Final = f"{DOMAIN}_delta"

#: Tiers scoped to a period. They need to know *which* period, and the closed
#: ones are the ``history`` tier's business, not theirs.
PERIOD_TIERS: Final = frozenset({Tier.MARKS, Tier.ATTENDANCE, Tier.EVALUATIONS})


@dataclass(slots=True)
class TierRecord:
    """Bookkeeping the diagnostic entities read."""

    last_success: Any | None = None
    last_duration_ms: int | None = None
    last_calls: int = 0
    consecutive_failures: int = 0


@dataclass(slots=True)
class AccountState:
    """Everything about the account that entities may read synchronously."""

    students: tuple[Student, ...] = ()
    periods: tuple[Period, ...] = ()
    current_period: Period | None = None
    records: dict[Tier, TierRecord] = field(default_factory=dict)
    last_collection_tier: Tier | None = None


def _establishment_timezone(hass: HomeAssistant, options: Mapping[str, Any]) -> str:
    """The zone PRONOTE's naive local times are read in.

    Absence is a value here, not a gap. §4.2 makes the default "Home
    Assistant's own", which means this has to be *read* at every set-up rather
    than copied once -- so that somebody who corrects their instance's timezone
    gets the correction here on the next reload without touching this
    integration. The options page is careful not to store a snapshot of it for
    the same reason.

    An unusable stored name falls back instead of raising, for exactly the
    reason `bounded_option` exists a few lines below. `PronoteGateway.__init__`
    calls `ZoneInfo(name)`, which raises `ZoneInfoNotFoundError`, and this runs
    on the set-up path -- while the options page, the only place that checks
    this string, is not on the path a restored backup or a hand-edited
    `.storage` takes. Raising there costs every entity of the account and
    offers `unavailable` as the whole explanation. Falling back costs an hour
    of offset on a school abroad, and says so.
    """
    fallback = str(hass.config.time_zone)
    stored = options.get(OPT_ESTABLISHMENT_TIMEZONE)
    if not stored:
        return fallback
    name = str(stored)
    if dt_util.get_time_zone(name) is None:
        _LOGGER.warning(
            "the configured establishment timezone %r is not a zone this "
            "system knows; reading PRONOTE's local times in %s instead. "
            "Correct it on the integration's options page -- leaving the box "
            "empty follows Home Assistant's own timezone",
            name,
            fallback,
        )
        return fallback
    return name


def _selected_children_from_entry(data: Mapping[str, Any]) -> tuple[str, ...]:
    """The stored choice of children, in whichever form the entry holds it.

    Once an account has paired, `children` holds minted keys. Before that it
    is in the form the config flow writes: resource identifiers for PRONOTE,
    and for an EcoleDirecte entry nothing at all -- its flow records only the
    chosen children in `child_keys`, so their identifiers *are* the choice.
    `child_keys.followed` reads both forms.
    """
    stored = data.get(CONF_CHILDREN)
    if stored:
        return tuple(stored)
    if source_from_entry_data(dict(data)) is not Source.ECOLEDIRECTE:
        return ()
    return tuple(
        str(record[CHILD_RESOURCE_ID])
        for record in (data.get(CONF_CHILD_KEYS) or ())
        if isinstance(record, dict) and record.get(CHILD_RESOURCE_ID)
    )


def scheduled_tiers(
    capabilities: ConnectorCapabilities, enabled: Mapping[Tier, bool] | None
) -> frozenset[Tier]:
    """The tiers both configured on and actually supported by the connector."""
    announced = frozenset(capabilities.tiers)
    if enabled is None:
        return announced
    return frozenset(tier for tier in announced if enabled.get(tier, True))


class PronoteAccount:
    """Orchestrates one PRONOTE account: session, budget, cadence, snapshots."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: PronoteConfigEntry,
        *,
        connector_client: EcoleDirecteClient | None = None,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.state = AccountState()

        options = entry.options
        # Clamped, like everything else read out of the options mapping. A
        # stored read timeout of 0 makes every request time out before it is
        # sent, which presents as a total outage with a healthy server, and
        # the options page -- the only place that bounds these fields -- is
        # not on the path a restored or hand-edited entry takes.
        read_timeout = bounded_option(options, OPT_READ_TIMEOUT, DEFAULT_READ_TIMEOUT)
        connect_timeout = float(
            bounded_option(options, OPT_CONNECT_TIMEOUT, DEFAULT_CONNECT_TIMEOUT)
        )
        self.stale_after = int(
            bounded_option(options, OPT_STALE_AFTER, DEFAULT_STALE_AFTER)
        )

        connector_deps: dict[str, Any] = {
            "entry_id": entry.entry_id,
            "timezone": _establishment_timezone(hass, options),
            "credentials": _credentials_from_entry(entry),
            "rate_limit_config": build_rate_limit_config(options),
            "limiter_state": limiter_state_store(hass).get(entry.entry_id, {}),
            "strategy": SessionStrategy(
                options.get(OPT_SESSION_STRATEGY, DEFAULT_SESSION_STRATEGY)
            ),
            "connect_timeout": connect_timeout,
            "read_timeout": read_timeout,
            "history_periods": int(options.get(OPT_HISTORY_PERIODS, 0) or 0),
            "on_credentials_rotated": self._async_persist_credentials,
        }
        if connector_client is not None:
            connector_deps["client"] = connector_client
            connector_deps["username"] = str(entry.data.get("username", ""))
            connector_deps["password"] = str(entry.data.get("password", ""))
            connector_deps["qcm_json"] = entry.data.get("qcm_json") or {}
        self.connector = build_connector(hass, entry, **connector_deps)
        extras = self.extras
        if extras is not None:
            extras.session.resolve_child = self._live_child_id
        enabled = tier_enabled(options)
        supported = scheduled_tiers(self.connector.capabilities, enabled)
        self.scheduler = FetchScheduler(
            default_plans(
                tier_intervals(options),
                {tier: tier in supported for tier in enabled},
            ),
            clock=time.monotonic,
            now=self.connector.now,
        )
        self.delta = DeltaDetector()

        # A coordinator per tier, `session` included. The session tier costs
        # no request -- periods, class and establishment come free with the
        # login -- but its entities still need something to subscribe to.
        self.coordinators: dict[Tier, PronoteTierCoordinator] = {
            tier: PronoteTierCoordinator(hass, entry, tier) for tier in Tier
        }

        # An options save reloads the entry, which builds a new scheduler whose
        # every deadline is unset -- making all ten tiers immediately due. The
        # previous schedule is handed across the reload through `hass.data`, so
        # nudging one interval no longer costs a login plus a full batch (§7.3).
        #
        # The schedule travels with the data it describes, and **only** with
        # it. Handing across a deadline while the coordinators came back empty
        # is what left every child entity `unavailable` on a live instance: the
        # new scheduler believed the tiers were fresh, `due()` returned
        # nothing, and the batch that ran was a batch of nothing -- for a whole
        # tier interval, which is twenty-four hours for `menus`, `static` and
        # `history`. Inside quiet hours the limiter could not refill them
        # either, so a reload at 22:10 emptied the dashboard until 06:00.
        #
        # So a tier whose snapshot came back keeps its deadline, and a tier
        # without data is due immediately. That invariant is the whole point,
        # and it is enforced by the account rather than the scheduler because
        # only the account can see the coordinators -- `scheduler.py` knows
        # nothing of Home Assistant and must keep knowing nothing.
        #
        # Only *read* here, and applied by `async_setup` once the login has
        # named the children: the snapshots are keyed by PRONOTE's resource
        # identifier, which rotates between sessions, so they can only be
        # handed to the new identifiers after pairing has said which is which.
        self._carried_snapshots = _saved_snapshots(hass).get(entry.entry_id)
        self._carried_schedule = _saved_schedule(hass).get(entry.entry_id, {})

        #: The stored choice, re-read and rewritten as minted keys at every
        #: pairing: see `_async_pair_children`.
        self._selected_children = _selected_children_from_entry(entry.data)
        #: The roster the last pairing saw, followed or not. What
        #: `_async_adopt_announced_children` compares against to know whether a
        #: login changed it.
        self._announced_children: tuple[str, ...] = ()
        #: PRONOTE resource identifier -> the key this integration minted for
        #: that child, for the *followed* children only -- a declined child
        #: has a record in the table but no entity to build. Filled by
        #: `_async_pair_children` during set-up, before
        #: any platform is forwarded, because `entity.py` reads it to build
        #: every `unique_id`. Empty until then, and deliberately not defaulted
        #: to the resource identifier: see `stable_key`.
        self._child_keys: dict[str, str] = {}
        #: Every resource identifier ever paired, with its key -- the current
        #: ones and those a login has since replaced. What a caller still
        #: holding a replaced identifier is resolved through.
        self._keys_ever: dict[str, str] = {}
        self._unsub_tick: Any | None = None
        self._tick_lock = asyncio.Lock()
        #: Batches that have run to completion, empty ones included. It exists
        #: because "has the first collection finished?" had no answer a caller
        #: could wait on: the lock is only held *while* a batch runs, so a test
        #: that checked it before the first-collection task had started read
        #: `False` and concluded the batch was over. That produced the
        #: lowest-priority tier's entity `unavailable` on a loaded machine,
        #: about one run in twenty, on the gated row -- a flaky gate, which
        #: lets something through or blocks a release at random. Counted at the
        #: end and not at the start, so it means finished.
        self._completed_ticks = 0
        self._shutting_down: bool = False
        #: Registry id of this entry's account device, filled in by
        #: `async_setup_entry` right after it creates that device and before the
        #: platforms are forwarded. Child devices need it to declare their
        #: parent: `DeviceInfo` takes `via_device_id` -- a registry id -- and no
        #: longer the identifier tuple, so the link cannot be expressed without
        #: having created the parent first.
        self.account_device_id: str | None = None

    # -- properties --------------------------------------------------------

    @property
    def write_enabled(self) -> bool:
        """Whether write operations are permitted (§8.3).

        A missing key reads as the *legacy* default, not the current one: new
        entries are created with the key set, so its absence means an entry
        from before writes were on by default, whose owner never chose them.
        """
        return bool(
            self.entry.options.get(
                OPT_WRITE_OPERATIONS_ENABLED, LEGACY_WRITE_OPERATIONS_ENABLED
            )
        )

    @property
    def can_tick_homework(self) -> bool:
        """Whether a homework tick sent from this account can reach PRONOTE.

        Only a student's own session can. ``SaisieTAFFaitEleve`` sent from a
        parent session is answered normally and recorded nowhere -- measured
        on 2026-09-27 with the ``N`` of the posting session, and consistent
        with the Espace Parents, where the state of a homework item is text
        and not a checkbox. So a parent account is refused the tick up front,
        rather than billed for a write that is then reported as a success.
        """
        extras = self.extras
        return extras is not None and extras.session.parent_account is False

    @property
    def students(self) -> tuple[Student, ...]:
        """The children this entry follows."""
        return self.state.students

    @property
    def limiter(self) -> LimiterView:
        """The connector-owned limiter kept for account orchestration."""
        return self.connector.limiter

    @property
    def extras(self) -> PronoteExtras | None:
        """PRONOTE session extras, or None when the source has none."""
        connector = self.connector
        if has_pronote_extras(connector):
            return connector
        return None

    @property
    def establishment_name(self) -> str:
        """What to call the account device.

        The establishment of the first child that names one. A parent account
        can hold children in two establishments, in which case this is
        arbitrary -- but a device has one name, and naming it after one of the
        two schools is more useful than naming it "PRONOTE". The fallback
        exists for the window before the first login has told us anything.
        """
        return (
            next(
                (
                    student.establishment
                    for student in self.state.students
                    if student.establishment
                ),
                None,
            )
            or "PRONOTE"
        )

    def now(self) -> datetime:
        """The current instant in the establishment's timezone."""
        return self.connector.now()

    def today(self) -> date:
        """Today's date in the establishment's timezone."""
        return self.connector.today()

    # -- setup and teardown ------------------------------------------------

    async def async_setup(self) -> None:
        """Log in once, learn the account's shape, and start the heartbeat."""
        await self._async_load_session_facts()
        restored = self._restore_snapshots(self._carried_snapshots)
        self.scheduler.import_state(
            {
                name: instant
                for name, instant in self._carried_schedule.items()
                if name in restored
            }
        )

        # A master tick of 0 would schedule the next tick in the past, and
        # `async_track_point_in_time` fires an overdue callback immediately
        # -- an unthrottled loop rescheduling itself for ever.
        interval = int(
            bounded_option(self.entry.options, OPT_MASTER_TICK, DEFAULT_MASTER_TICK)
        )
        self._unsub_tick = async_track_time_interval(
            self.hass,
            self._async_tick,
            timedelta(minutes=interval),
            name=f"{DOMAIN} master tick",
        )

    def async_start_first_collection(self) -> None:
        """Schedule the first batch, once there are entities to receive it.

        Deliberately *not* called from :meth:`async_setup`, and that is
        load-bearing. Set-up forwards the platforms only after the account has
        learned its shape, so a batch scheduled during set-up races the
        creation of the very entities it collects for -- and a coordinator that
        publishes before an entity has subscribed pushes to nobody. The entity
        is added afterwards, renders once from ``coordinator.data``, finds
        nothing for its tier and renders ``unavailable``; the snapshot that
        arrived a moment earlier is sitting in the coordinator, and no second
        write ever goes out to notice it.

        Nothing recovers from it either, which is what makes the ordering worth
        a method of its own. The tier is *genuinely* collected: the scheduler
        holds its deadline and :meth:`has_data` is true, so the
        first-collection dispensation in ``collect_tier`` stops applying and a
        ``refresh`` is refused -- correctly, and uselessly. The entity then
        waits for the tier's own interval: three hours for ``marks``, a full
        day for ``menus`` and ``history``, and with quiet hours on, until
        06:00.

        Measured on a live instance at 00:12: all ten tiers placed their
        requests and published, and nineteen entities across seven tiers stayed
        ``unavailable`` with their data already present. The two tiers whose
        snapshots landed before the platforms were forwarded -- ``timetable``
        and ``homework`` -- were the only ones that displayed.
        """
        # Collect immediately rather than waiting a full tick: a fresh install
        # showing nothing for five minutes reads as broken.
        self.hass.async_create_task(
            self._async_tick(), name=f"{DOMAIN} first collection"
        )

    def _restore_snapshots(
        self, carried: Mapping[Tier, TierData] | None
    ) -> frozenset[str]:
        """Refill the coordinators from a reload, and say which tiers came back.

        ``carried`` is keyed by each child's *minted* key, not by the PRONOTE
        resource identifier the coordinators use, and that is the defect this
        re-keying removes. The identifier rotates between sessions, and the
        reload's own login is a new session: carried under the old identifier,
        every snapshot came back where no entity looked for it, while the tier
        still counted as restored and kept its deadline. A live instance lost
        all forty-four entities of its only child on an options save, for as
        long as each tier's interval -- the login had logged the rotation a
        second earlier. So this runs after pairing, and a tier counts as
        restored only if **every** child followed now has its snapshot back.
        The session tier is skipped: the login has just published it afresh.

        Returns tier *names*, because that is the key
        :meth:`FetchScheduler.export_state` uses and comparing the two in one
        vocabulary is what keeps the pair honest.

        A tier is only reported as restored when it actually carries a
        snapshot: an empty mapping is not data, and reporting it would restore
        that tier's deadline and leave its entities unavailable -- the exact
        defect this method exists to remove.

        The values are the same objects, not copies: snapshots are frozen DTOs
        (§2.2), so there is nothing to protect them from, and the mapping
        itself is copied because the coordinator mutates it.
        """
        if not carried:
            return frozenset()
        current = {key: student_id for student_id, key in self._child_keys.items()}
        followed = {student.id for student in self.state.students}
        restored: set[str] = set()
        for tier, data in carried.items():
            coordinator = self.coordinators.get(tier)
            if coordinator is None or tier is Tier.SESSION:
                continue
            rekeyed = {
                current[key]: replace(snapshot, student_id=current[key])
                for key, snapshot in data.items()
                if key in current
            }
            if not rekeyed:
                continue
            coordinator.data = rekeyed
            if followed <= rekeyed.keys():
                restored.add(str(tier))
        return frozenset(restored)

    async def async_unload(self) -> None:
        """Stop the heartbeat and release the session.

        Never joins the worker thread from the event loop: pronotepy's transport
        can be mid-request, and joining would freeze the reload. A leaked thread
        beats a frozen instance (§3.2).
        """
        self._shutting_down = True
        if self._unsub_tick is not None:
            self._unsub_tick()
            self._unsub_tick = None
        _saved_schedule(self.hass)[self.entry.entry_id] = self.scheduler.export_state()
        # Exported together with the schedule, because the two are only sound
        # together: see `_restore_snapshots`. Keyed by the minted key, which
        # survives the login the reload is about to make; the resource
        # identifier does not.
        _saved_snapshots(self.hass)[self.entry.entry_id] = {
            tier: {
                self._child_keys.get(student_id, student_id): snapshot
                for student_id, snapshot in coordinator.data.items()
            }
            for tier, coordinator in self.coordinators.items()
            if coordinator.data
        }
        limiter_state_store(self.hass)[self.entry.entry_id] = (
            self.limiter.export_state()
        )
        await self.connector.async_close()

    async def _async_load_session_facts(self) -> None:
        """Read what the login itself supplies -- students, periods, class.

        Zero calls beyond the login: ``periods``, ``class_name`` and
        ``establishment`` live in ``func_options`` and
        ``parametres_utilisateur``, already in memory once authenticated
        (annexe A §2).
        """
        students: list[Student] = []
        session_snapshots: list[SessionFacts] = []
        periods: tuple[Period, ...] = ()
        current: Period | None = None

        await self.connector.async_open()
        announced = self.connector.announced_children()
        self._announced_children = tuple(child_id for child_id, _ in announced)
        # Before the facts are read, because pairing is what says whom to
        # follow -- and a declined child's facts must never be decoded -- and
        # before the platforms are forwarded, because every `unique_id` is
        # built from what this establishes.
        student_ids = self._async_pair_children(announced)
        await self.connector.async_load_session_facts(student_ids)
        for student_id in student_ids:
            facts = self.connector.session_facts(student_id)
            students.append(facts.student)
            session_snapshots.append(facts)
            periods = facts.periods
            current = facts.current_period

        self.state.students = tuple(students)
        self.state.periods = periods
        self.state.current_period = current

        session_coordinator = self.coordinators[Tier.SESSION]
        for student, facts in zip(students, session_snapshots, strict=True):
            session_coordinator.publish(
                student.id,
                Snapshot(
                    data=facts,
                    fetched_at=self.now(),
                    tier=Tier.SESSION,
                    calls=0,
                    student_id=student.id,
                ),
            )

    @callback
    def _async_pair_children(
        self, announced: Sequence[tuple[str, str]]
    ) -> tuple[str, ...]:
        """Give every announced child its key, and say which ones to follow.

        The whole point of `child_keys` is that PRONOTE's resource identifier
        rotates, so this runs at every set-up and after every login that
        changed the roster: the table in the config entry is the durable
        artefact, and re-pairing against it is the normal mode of operation.

        Every announced child is paired, followed or not, and the selection is
        stored as the minted keys of the followed ones. It used to be stored
        as resource identifiers, which is to say as the one value known to
        rotate: every login that renamed a child made it a "newcomer", its new
        identifier was appended, and a one-child account ended up holding
        eleven identifiers of which none matched -- warning at every restart
        that the selection had lapsed, and silently re-following any child the
        user had declined. A declined child now keeps a record in the table and
        stays out of the selection, so a rotation cannot turn it into a
        newcomer; a child the table has never seen is still followed, because
        nobody was ever asked about it. `child_keys.followed` holds the rule,
        including the one-time translation of a selection still stored as
        identifiers.

        Writing the entry here is safe: only the options flow reloads the
        entry (`PronoteOptionsFlow`), and a write to ``entry.data`` wakes
        nothing, so this write cannot start a reload loop -- and it only
        writes when the table or the selection actually
        changed, which is the same guard `_async_persist_credentials` needs for
        the opposite reason. On an ordinary rotation the table changes (each
        record's identifier) and the selection does not.
        """
        stored = list(self.entry.data.get(CONF_CHILD_KEYS) or ())
        selection = _selected_children_from_entry(self.entry.data)
        keys, table, notes = pair(stored, announced)
        chosen = followed(stored, table, selection, keys)
        for note in notes:
            # Keys only, never a name: this lands in the log users attach to
            # public issues (§8.2). It is logged at all because the silence
            # here is what hid the duplicate-device defect for as long as it
            # existed.
            _LOGGER.info("child identity: %s", note)
        if selection and is_legacy_selection(selection):
            _LOGGER.info(
                "the child selection was stored as %d resource identifier(s), "
                "which PRONOTE rotates at every login; it is now stored "
                "as the %d minted key(s) of the children followed",
                len(selection),
                len(chosen),
            )

        student_ids = tuple(
            child_id for child_id, _ in announced if keys[child_id] in chosen
        )
        if not student_ids and announced:
            # Following every child is the right recovery -- refusing to
            # collect anything would take the whole integration down -- but
            # doing it silently is what once let a serious defect hide. Since
            # the selection is kept as minted keys, a rotation no longer lands
            # here: only an account on which every child the user chose has
            # gone, leaving only children they declined. The selection itself
            # is left as stored, so a returning child is followed again.
            _LOGGER.warning(
                "none of the %d children selected for this account is among "
                "the %d it now announces, all of which were declined; all %d "
                "are followed until the selection is changed",
                len(chosen),
                len(announced),
                len(announced),
            )
            student_ids = tuple(child_id for child_id, _ in announced)

        self._child_keys = {child_id: keys[child_id] for child_id in student_ids}
        self._keys_ever.update(keys)

        data: dict[str, Any] = {**self.entry.data, CONF_CHILD_KEYS: table}
        if chosen:
            data[CONF_CHILDREN] = chosen
        if data != dict(self.entry.data):
            self.hass.config_entries.async_update_entry(self.entry, data=data)
        self._selected_children = _selected_children_from_entry(self.entry.data)

        self._async_adopt_existing_registry_rows()
        return student_ids

    @callback
    def _async_adopt_existing_registry_rows(self) -> None:
        """Re-point rows created against PRONOTE's rotating identifier.

        Entities and devices that predate the minted keys carry
        ``<entry_id>_46#<signature>...`` in their ``unique_id``. Rewriting that
        **in place** is the whole difference between a fix and a second
        breakage: `async_update_entity` keeps the registry row, so the
        ``entity_id``, the device, a name the user set by hand, the area and
        the labels all survive. Creating new entities and leaving the old ones
        would *be* the defect, performed deliberately -- and it would break
        every dashboard badge and tile, which can only be wired to a literal
        ``entity_id``.

        Run here rather than from a version-numbered migration handler, and
        that is a deliberate choice: a migration runs before any login, so it
        cannot know which children the account announces *now* -- and on an
        instance that already suffered the defect the registry holds two
        generations for one child, with no way to tell from the registry alone
        which is alive. Pairing has just answered that question, so this runs
        immediately after it and only ever touches children the account
        actually announced. The other generation is left exactly as it is: its
        entities are dead either way, and deleting registry rows on a user's
        behalf is not this integration's decision.

        Idempotent by construction -- after the first pass nothing matches the
        old prefix -- so it also repairs an entry whose identifier rotated
        between one start and the next.
        """
        entities = er.async_get(self.hass)
        devices = dr.async_get(self.hass)
        entry_id = self.entry.entry_id
        rows = er.async_entries_for_config_entry(entities, entry_id)

        unclaimed: list[str] = []
        for resource_id, minted in self._child_keys.items():
            if resource_id == minted:
                continue
            moved = self._async_adopt_device(devices, entry_id, resource_id, minted)
            if self._async_adopt_entities(
                entities, rows, entry_id, resource_id, minted
            ):
                moved = True
            if not moved:
                unclaimed.append(minted)

        if unclaimed:
            self._async_adopt_rotated_rows(entities, devices, entry_id, rows, unclaimed)

    @callback
    def _async_adopt_rotated_rows(
        self,
        entities: er.EntityRegistry,
        devices: dr.DeviceRegistry,
        entry_id: str,
        rows: list[er.RegistryEntry],
        unclaimed: list[str],
    ) -> None:
        """Adopt rows whose identifier PRONOTE no longer announces.

        This is the case the repair exists for, and the first version missed
        it. It looked up ``<entry_id>_<the identifier announced now>`` -- so it
        worked only while the identifier had *not* rotated since the rows were
        created, which is precisely when there is nothing to repair. On the
        instance that motivated the fix the identifier had rotated, no row
        matched, and 56 entities were created beside the 56 already there: the
        defect performed deliberately. The test that was supposed to cover this
        used the identifier the fake account announces, so it never rotated
        anything.

        The rows are found through the **devices** and not by splitting a
        ``unique_id``: a resource identifier may itself contain an underscore
        (``46#_KlUc...`` was one of the three observed), so there is no safe
        separator to split on. A child device carries exactly
        ``<entry_id>_<identifier>`` and nothing more, which is unambiguous. The
        account device is ``<entry_id>`` alone and cannot be mistaken for one.

        Only ever with a **single** unmatched child. With siblings there is no
        sound way to say which orphaned generation belonged to which child --
        twins share a name -- and guessing would hand one pupil's history to
        another. Their rows are left alone and said so.
        """
        # Candidates first, and the order matters: on a fresh install every
        # child has no rows yet, so `unclaimed` is every child and a message
        # about an unrepairable registry would greet an installation with
        # nothing whatever to repair. No candidate means no history, which is
        # the ordinary case and says nothing.
        #
        # A candidate is an identifier that is neither one the account
        # announces now nor a key of ours -- including the key of a child who
        # has left, which stays ours for ever and must never be handed on.
        known = set(self._child_keys) | set(self._child_keys.values())
        prefix = f"{entry_id}_"
        candidates = [
            (device, identifier.removeprefix(prefix))
            for device in dr.async_entries_for_config_entry(devices, entry_id)
            for domain, identifier in device.identifiers
            if domain == DOMAIN
            and identifier.startswith(prefix)
            and identifier.removeprefix(prefix) not in known
            and not is_minted(identifier.removeprefix(prefix))
        ]
        if not candidates:
            return

        if len(unclaimed) != 1:
            _LOGGER.warning(
                "%d children have no registry rows under the identifier the "
                "account announces now, and %d orphaned generation(s) are "
                "present. That is repairable for one child and not for "
                "several: nothing in the registry says which generation "
                "belonged to which child, and a wrong guess would move one "
                "pupil's history onto another. The previous entities keep "
                "their last value and stop updating; the new ones work. "
                "Re-point your dashboards at the new entities, or delete the "
                "stale devices and start from the new set",
                len(unclaimed),
                len(candidates),
            )
            return

        minted = unclaimed[0]

        # The newest, when an instance carries several dead generations: an
        # older one was already superseded by the one after it, so the last
        # PRONOTE created is the one whose entities the dashboards were built
        # on. Measured on the live instance the two generations were two hours
        # apart, and the newer one held exactly the entities every badge and
        # automation referenced.
        #
        # By creation time and **not** by name, which was the first idea and
        # does not work: the integration names both generations from PRONOTE,
        # so `default_name` was byte-identical on both: only the rename the
        # user had applied to the older one differed. Matching on the name
        # would have had to choose between two equal candidates, and twins
        # share a name in any case.
        newest = max(device.created_at for device, _ in candidates)
        latest = [pair for pair in candidates if pair[0].created_at == newest]
        if len(latest) > 1:
            # Two generations created in the same instant is not something a
            # real server does; it does happen under a frozen test clock, and
            # it would happen to anyone restoring a hand-edited registry.
            # Picking one would be a coin toss over which set of entities a
            # dashboard keeps, so it is left alone and said out loud.
            _LOGGER.warning(
                "%d orphaned generations for %s share the same creation time, "
                "so there is nothing to prefer between them. Leaving them "
                "alone rather than choosing one: their entities keep their "
                "last value and stop updating, while the new ones work. "
                "Delete the stale devices from the device page to settle it",
                len(latest),
                minted,
            )
            return
        _, legacy = latest[0]
        _LOGGER.warning(
            "the identifier PRONOTE announces for %s is not the one its %d "
            "existing entities were created under -- the identifier rotated, "
            "which is expected and is what the minted key exists to absorb. "
            "Re-pointing those rows in place rather than creating a second "
            "set; every entity_id, custom name, area and label is kept",
            minted,
            sum(1 for row in rows if row.unique_id.startswith(f"{prefix}{legacy}_")),
        )
        self._async_adopt_device(devices, entry_id, legacy, minted)
        self._async_adopt_entities(entities, rows, entry_id, legacy, minted)

    @callback
    def _async_adopt_device(
        self, devices: dr.DeviceRegistry, entry_id: str, resource_id: str, minted: str
    ) -> bool:
        """Move one child device onto its minted identifier.

        Returns whether it moved one, because the caller needs to know that
        nothing matched in order to look for a rotated identifier.
        """
        was = (DOMAIN, f"{entry_id}_{resource_id}")
        now = (DOMAIN, f"{entry_id}_{minted}")
        # `async_get_device_by_identifier` and not `async_get_device`: the
        # latter is deprecated because a device identifier is no longer unique
        # across config entries, and it *raises* under the test harness. That
        # is also why it takes the entry id -- which is the more correct
        # question anyway, since two accounts may follow the same child.
        device = devices.async_get_device_by_identifier(was, entry_id)
        if device is None:
            return False
        taken = devices.async_get_device_by_identifier(now, entry_id)
        if taken is not None:
            if taken.id != device.id:
                # Both generations exist as devices. Claiming the identifier
                # would collide, and merging them would decide for the user
                # which of two devices keeps their custom name.
                _LOGGER.warning(
                    "a device already carries the minted identifier for %s, so "
                    "the older one is left as it is; it will not update again "
                    "and can be deleted from the device page",
                    minted,
                )
            # Already carried by this very device: adopted on an earlier start.
            return taken.id == device.id
        devices.async_update_device(device.id, new_identifiers={now})
        _LOGGER.info(
            "adopted the existing device for %s, keeping its name, area and labels",
            minted,
        )
        return True

    @callback
    def _async_adopt_entities(
        self,
        entities: er.EntityRegistry,
        rows: list[er.RegistryEntry],
        entry_id: str,
        resource_id: str,
        minted: str,
    ) -> int:
        """Rewrite the unique id of every entity of one child, in place.

        Returns how many rows moved, for the same reason the device helper
        does: none moving is what says the identifier has rotated.
        """
        was = f"{entry_id}_{resource_id}_"
        now = f"{entry_id}_{minted}_"
        adopted = 0
        for row in rows:
            if not row.unique_id.startswith(was):
                continue
            wanted = now + row.unique_id[len(was) :]
            taken = entities.async_get_entity_id(row.domain, DOMAIN, wanted)
            if taken is not None and taken != row.entity_id:
                # Refusing beats raising: `async_update_entity` would abort the
                # whole set-up over one row, and an entry that will not load is
                # worse than one entity left behind.
                _LOGGER.warning(
                    "cannot re-point %s: %s already holds the identity it would take",
                    row.entity_id,
                    taken,
                )
                continue
            entities.async_update_entity(row.entity_id, new_unique_id=wanted)
            adopted += 1
        if adopted:
            _LOGGER.info(
                "re-pointed %d existing entities of %s onto an identity that "
                "no longer follows PRONOTE's rotating identifier; every "
                "entity_id, custom name, area and label is unchanged",
                adopted,
                minted,
            )
        return adopted

    def student_id_for_key(self, key: str) -> str | None:
        """Turn a minted key back into the identifier PRONOTE announced.

        The reverse of `stable_key`, needed at every boundary where a *device*
        is the input: a device identifier carries the minted key, while the
        coordinators, the gateway and the bus events are all keyed by the
        session's resource identifier. Three call sites need it -- the
        diagnostics dump, the device triggers and the service target
        resolution -- and each is a place where a device that Home Assistant
        stored months ago has to be matched to the child of this session.

        A value that is *already* a known resource identifier is returned
        unchanged, which is what makes a device created before the keys
        existed keep working between the upgrade and the migration.
        """
        if key in self._child_keys:
            return key
        for student_id, minted in self._child_keys.items():
            if minted == key:
                return student_id
        return None

    def student_for_key(self, key: str) -> Student | None:
        """The child currently paired with a minted key, or ``None``."""
        student_id = self.student_id_for_key(key)
        return next(
            (student for student in self.state.students if student.id == student_id),
            None,
        )

    def _live_child_id(
        self, stale: str, announced: Sequence[tuple[str, str]]
    ) -> str | None:
        """Which of ``announced`` is the child once known as ``stale``.

        Run by the session in its worker thread, right before ``set_child``,
        when the identifier it was handed is not on the client it holds --
        because the call it is about to place has just logged in again and
        PRONOTE renamed the child. Pure: it pairs the new roster against the
        stored table without storing anything, which the account does itself
        after the batch.
        """
        key = self._keys_ever.get(stale)
        if key is None:
            return None
        keys, _, _ = pair(list(self.entry.data.get(CONF_CHILD_KEYS) or ()), announced)
        return next((child for child, found in keys.items() if found == key), None)

    def stable_key(self, student_id: str) -> str:
        """The minted key for a child, for use in a `unique_id`.

        Falls back to the resource identifier only if pairing never ran for
        this child, which cannot happen through `async_setup`: every student
        in `state.students` was paired before the platforms were forwarded.
        It is a loud fallback rather than an exception because raising here
        would fail the whole entry -- losing every child's entities -- to
        protect the identity of one. The error says what to look for, since
        the consequence is precisely the defect this replaced: an entity whose
        identity follows a value PRONOTE rotates.
        """
        key = self._child_keys.get(student_id)
        if key is None:
            _LOGGER.error(
                "no minted key for a child that entities are being built for; "
                "falling back to the PRONOTE resource identifier, which is "
                "not stable between sessions and will orphan those entities "
                "the next time it rotates. This is an integration bug: "
                "pairing runs before the platforms are forwarded"
            )
            return student_id
        return key

    def _stopping(self) -> bool:
        """Whether teardown has begun.

        A method and not a bare attribute read, because a batch awaits between
        tiers and ``async_unload`` sets the flag from another task in the
        meantime -- so the value genuinely changes across the loop. Read as
        ``self._shutting_down``, a type checker narrows it to ``False`` after
        the guard at the top of the tick and calls the in-loop check dead code,
        which is the one thing it is not.
        """
        return self._shutting_down

    # -- the master tick ---------------------------------------------------

    async def _async_tick(self, _now: Any = None) -> None:
        """Serve every due tier, once, in priority order."""
        if self._stopping():
            return
        if self._tick_lock.locked():
            # The previous batch is still running. Skipping is correct: the
            # deadlines have not moved, so the next tick picks the same tiers up
            # -- whereas queueing would let a slow server build a backlog that
            # then arrives as the burst the single heartbeat exists to prevent.
            _LOGGER.debug("skipping a tick: the previous batch is still running")
            return

        async with self._tick_lock:
            try:
                await self._async_run_batch()
            finally:
                self._completed_ticks += 1

    @property
    def completed_ticks(self) -> int:
        """How many batches have finished, empty ones included."""
        return self._completed_ticks

    async def _async_run_batch(self) -> None:
        """One batch, under the tick lock the caller already holds."""
        due = self.scheduler.due()
        if not due:
            return
        _LOGGER.debug("batch: %s", ", ".join(str(tier) for tier in due))
        # Declaring the batch is load-bearing twice over. The limiter needs
        # it so that entering quiet hours halfway through lets the batch
        # finish rather than refusing its remaining tiers (annexe B §8);
        # the session needs it because `per_batch` means one login per
        # *batch*, and without a boundary it meant one login per
        # `(child, tier)` -- 36 logins in a single tick for a two-child
        # account with three closed periods, against a cap of 24.
        self.limiter.begin_batch()
        extras = self.extras
        if extras is not None:
            extras.session.begin_batch()
        try:
            for tier in due:
                if self._stopping():
                    return
                await self._async_collect(tier)
        finally:
            self.limiter.end_batch()
        await self._async_adopt_announced_children()
        self._async_sync_issues()

    async def _async_adopt_announced_children(self) -> None:
        """Bring in a child the account gained while Home Assistant ran.

        After the batch rather than before it: the roster is refreshed by the
        login, so the newcomer can only be there once something has actually
        logged in, and re-reading the session facts needs the session lock the
        batch was holding.

        Re-reading costs nothing -- ``async_open`` and
        ``async_load_session_facts`` both declare ``cost=0`` because they read
        what the handshake already put in memory -- but it is still done only
        when the roster grew, so an ordinary batch pays no executor round trip
        for a child that arrives once or twice in a school career.
        """
        newcomers = set(self.connector.student_ids()) - set(self._announced_children)
        if not newcomers:
            return

        # An identifier the last pairing did not see is, far more often than a
        # child enrolled mid-year, a child PRONOTE renamed at the last login.
        # Which of the two it is, and whether it is followed, is decided by
        # re-pairing -- not here. This used to append every such identifier to
        # the stored selection, so a one-child account grew it by one per
        # login. Re-pairing recognises a renamed child by its record, keeps a
        # declined one declined, and follows a genuinely new one: unknown is
        # not refused, because the selection was chosen among the children
        # that existed *then*, and one that did not exist was never declined.
        _LOGGER.debug(
            "the last login announced %d identifier(s) not seen at the last "
            "pairing; pairing again",
            len(newcomers),
        )
        before = {
            student.id: self._child_keys.get(student.id)
            for student in self.state.students
        }
        await self._async_load_session_facts()
        self._async_follow_renamed_children(before)

    @callback
    def _async_follow_renamed_children(self, before: Mapping[str, str | None]) -> None:
        """Move what is held under a child's old identifier to its new one.

        A "newcomer" above is, far more often than a child enrolled mid-year, a
        child PRONOTE renamed at the last login. Re-reading the session facts
        gives the account the new identifier; the snapshots, the change
        detector's memory and the per-student dispensation all stayed under
        the old one. Left there, every entity read a snapshot nothing would
        refresh again, and each tier counted as never collected for the new
        identifier -- so it ran as a first collection, through quiet hours, to
        publish where no entity looked.
        """
        current = {key: student_id for student_id, key in self._child_keys.items()}
        renamed = {
            old: current[key]
            for old, key in before.items()
            if key is not None and key in current and current[key] != old
        }
        if not renamed:
            return
        for coordinator in self.coordinators.values():
            if not coordinator.data:
                continue
            moved = False
            for old, new in renamed.items():
                snapshot = coordinator.data.pop(old, None)
                if snapshot is not None and new not in coordinator.data:
                    coordinator.data[new] = replace(snapshot, student_id=new)
                    moved = True
            if moved:
                coordinator.async_update_listeners()
        for old, new in renamed.items():
            self.delta.rename(old, new)
        _LOGGER.info(
            "%d child(ren) renamed by PRONOTE at the last login; their data "
            "follows them",
            len(renamed),
        )

    async def async_request_tick(self) -> None:
        """Serve what is due now, without waiting for the next heartbeat.

        The refresh button and the ``refresh`` service both land here after
        boosting the tiers they want. Nothing is bypassed: the scheduler still
        decides what is due, the limiter still spaces the calls, and
        ``_tick_lock`` still refuses a second batch -- so a button pressed ten
        times in a minute costs one batch, not ten (§5.1).
        """
        if self._stopping() or self._tick_lock.locked():
            return
        self.hass.async_create_task(
            self._async_tick(), name=f"{DOMAIN} requested collection"
        )

    async def _async_collect(self, tier: Tier) -> None:
        """Collect one tier for every followed student."""
        coordinator = self.coordinators.get(tier)
        if coordinator is None:
            return

        record = self.state.records.setdefault(tier, TierRecord())
        started = time.monotonic()
        calls = 0
        events: list[tuple[str, DeltaEvent]] = []
        succeeded = False

        for student in self.state.students:
            try:
                snapshot, used, detected = await self._async_collect_student(
                    tier, student.id
                )
            except TierDeferred as deferred:
                self.scheduler.defer(tier, deferred.retry_after)
                _LOGGER.debug(
                    "tier %s deferred for student %s: %s",
                    tier,
                    student.id,
                    deferred.reason,
                )
                return
            except (
                LoginRefused,
                InvalidCredentials,
                MfaRequired,
                BootstrapFailed,
                AccountUnreadable,
                IntegrationFault,
            ) as error:
                # Authentication problems are the account's problem, not the
                # tier's: they open a repair and stop the batch rather than
                # being retried per tier.
                record.consecutive_failures += 1
                coordinator.note_failure(error)
                # `retry_delay()`, not `backoff_delay()`. The latter is zero
                # until there have been *consecutive* failures, which is
                # exactly the case for every authentication refusal -- bad
                # credentials, a demanded PIN, an unreadable bootstrap all
                # leave that counter at zero. Deferring by zero seconds made
                # the tier due again on the very next tick, so ten tiers
                # attempted ten logins a tick: the one gesture that gets an
                # address suspended.
                self.scheduler.mark_failed(tier, self.limiter.retry_delay())
                # Described, not quoted: `AccountUnreadable` is raised from a
                # decoding error, and that text is the payload.
                _LOGGER.debug(
                    "authentication blocked tier %s: %s",
                    tier,
                    describe_failure(error),
                )
                return
            except Exception as error:  # top of one cycle (§5.3)
                record.consecutive_failures += 1
                coordinator.note_failure(error)
                self.scheduler.mark_failed(tier, self.limiter.retry_delay())
                # No traceback for a transport or a decoding failure: it
                # prints the message of every link, the transport link quotes
                # the host and the path it failed on, and the decoding link
                # the data it could not read -- a name, a homework text, a
                # `N`. The session has already said, once, that the service
                # is unavailable.
                _LOGGER.debug(
                    "tier %s failed for student %s: %s",
                    tier,
                    student.id,
                    describe_failure(error),
                    exc_info=is_traceback_safe(error),
                )
                return

            calls += used
            succeeded = True
            coordinator.publish(student.id, snapshot)
            if (
                tier is Tier.MARKS
                and self.connector.capabilities.source is Source.ECOLEDIRECTE
            ):
                session = self.connector.session_facts(student.id)
                self.state.periods = session.periods
                self.state.current_period = session.current_period
                self.coordinators[Tier.SESSION].publish(
                    student.id,
                    Snapshot(
                        data=session,
                        fetched_at=self.now(),
                        tier=Tier.SESSION,
                        calls=0,
                        student_id=student.id,
                    ),
                )
            events.extend((student.id, event) for event in detected)

        if not succeeded:
            # No student produced a snapshot -- normally because the account
            # has none yet. The tier still has to be settled, or its `boosted`
            # flag survives every tick and the "raised for exactly one tick"
            # invariant quietly stops holding.
            #
            # `mark_failed` and not `defer`, because "produced nothing" is a
            # failure however politely it arrived: a bare deferral by one
            # back-off base is shorter than the master tick, so a tier in this
            # state was retried at every tick for as long as it lasted. The
            # floor lives in `mark_failed`, which is the only difference
            # between the two.
            self.scheduler.mark_failed(tier, self.limiter.retry_delay())
            return

        self.scheduler.mark_collected(tier)
        record.consecutive_failures = 0
        record.last_success = self.now()
        record.last_duration_ms = int((time.monotonic() - started) * 1000)
        record.last_calls = calls
        self.state.last_collection_tier = tier

        # Events go out only after the snapshots are published, so an
        # automation reacting to "a grade arrived" finds the sensor already
        # holding it.
        for student_id, event in events:
            self._async_fire(student_id, event)

    async def _async_collect_student(
        self, tier: Tier, student_id: str
    ) -> tuple[Snapshot[Any], int, list[DeltaEvent]]:
        """Run one ``(student, tier)`` unit and derive its events."""
        from .tiers import collect_tier  # noqa: PLC0415 -- avoids an import cycle

        return await collect_tier(self, tier, student_id)

    @callback
    def _async_fire(self, student_id: str, event: DeltaEvent) -> None:
        """Hand one detected change to the matching ``event`` entity."""
        self.hass.bus.async_fire(
            SIGNAL_DELTA,
            {
                "entry_id": self.entry.entry_id,
                "student_id": student_id,
                "entity_key": event.entity_key,
                "event_type": event.event_type,
                "attributes": event.attributes,
            },
        )

    # -- snapshots and staleness ------------------------------------------

    def snapshot(self, tier: Tier, student_id: str) -> Snapshot[Any] | None:
        """The latest successful snapshot for a (tier, student) pair."""
        coordinator = self.coordinators.get(tier)
        if coordinator is None:
            return None
        return coordinator.snapshot_for(student_id)

    def is_stale(self, tier: Tier) -> bool:
        """Whether a tier's data has aged past ``stale_after`` intervals.

        The session tier is never stale: it holds what the login handed us --
        the child, the class, the period list -- which changes once a school
        year, not on an interval. Ageing it would make the period sensor go
        unavailable overnight for no reason.

        For every other tier the quiet hours are excused from the age. With
        quiet hours on -- the default -- an eight-hour night exceeds
        ``stale_after`` times the interval of every fast tier, so without this
        the timetable, homework, news and discussion entities went
        ``unavailable`` at 06:00 every single morning, and §2.5 is explicit
        that an unavailable entity breaks automations.
        """
        if tier is Tier.SESSION:
            return False
        return self.scheduler.is_stale(
            tier, self.stale_after, excused=self._quiet_seconds_since(tier)
        )

    def _quiet_seconds_since(self, tier: Tier) -> float:
        """Quiet-hours time that elapsed since a tier was last collected."""
        collected_at = self.scheduler.last_collected_at(tier)
        if collected_at is None:
            return 0.0
        return self.limiter.quiet_seconds_between(collected_at, self.now())

    def has_data(self, tier: Tier, student_id: str) -> bool:
        """Whether a (tier, student) pair ever produced a snapshot."""
        return self.snapshot(tier, student_id) is not None

    def periods_for(self, tier: Tier) -> tuple[Period, ...]:
        """Which periods a period-scoped tier should read.

        ``history`` walks the **closed** periods and nothing else: a closed
        period cannot change, so re-reading it every three hours spends calls on
        a constant result (§5.2).
        """
        now = self.now()
        if tier is Tier.HISTORY:
            return tuple(
                period
                for period in self.state.periods
                if period.is_closed(now)
                and (
                    self.state.current_period is None
                    or period.id != self.state.current_period.id
                )
            )
        if self.state.current_period is not None:
            return (self.state.current_period,)
        return ()

    # -- credential rotation ----------------------------------------------

    async def _async_persist_credentials(self, credentials: Mapping[str, Any]) -> None:
        """Write the rotated credentials back into the config entry.

        The mobile token rotates at every ``Authentification``; not re-saving it
        means losing access at the next start (§7.2). The write is why the lazy
        session strategy matters beyond arithmetic: one login a day is one window
        a day in which an abrupt shutdown between the server's rotation and this
        write leaves a dead token.
        """
        data = dict(self.entry.data)
        changed = False
        for key in ("username", "password", "client_identifier", "uuid"):
            value = credentials.get(key)
            if value is not None and data.get(key) != value:
                data[key] = value
                changed = True
        if changed:
            self.hass.config_entries.async_update_entry(self.entry, data=data)

    # -- repair issues -----------------------------------------------------

    @callback
    def _async_sync_issues(self) -> None:
        """Open or close the repair issues the limiter's state implies."""
        if self.limiter.cap_warning_open:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                f"{ISSUE_DAILY_CAP_NEAR}_{self.entry.entry_id}",
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=ISSUE_DAILY_CAP_NEAR,
                translation_placeholders={
                    "calls": str(self.limiter.calls_today),
                    "cap": str(self.limiter.config.max_requests_per_day),
                },
            )
        else:
            ir.async_delete_issue(
                self.hass,
                DOMAIN,
                f"{ISSUE_DAILY_CAP_NEAR}_{self.entry.entry_id}",
            )

    @callback
    def async_open_credentials_issue(self) -> None:
        """Ask the user to check the credentials, and stop trying."""
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            f"{ISSUE_INVALID_CREDENTIALS}_{self.entry.entry_id}",
            # Not fixable *here*. `ConfigEntryAuthFailed` already starts the
            # re-authentication flow, which is the only thing that can fix
            # this; with `is_fixable=True` and no `repairs.py`, Home Assistant
            # falls back to `ConfirmRepairFlow` -- an empty dialog whose
            # "submit" deletes the issue without doing anything at all.
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key=ISSUE_INVALID_CREDENTIALS,
            translation_placeholders={
                "attempts": str(self.limiter.failed_logins_last_hour),
                "until": _format_until(self.limiter.hold_until_wallclock),
            },
            data={"entry_id": self.entry.entry_id},
        )

    @callback
    def async_open_bootstrap_issue(self) -> None:
        """Report an impossible bootstrap **without naming a cause**.

        pronotepy decides an address is suspended with ``if "IP" in html`` --
        two capitals anywhere in the page. Specification v1 built an hour-long
        hold and a repair with "explicit text" on that, which would have told
        parents their home address was banned because a school wrote "Espace IP"
        in a footer. This issue enumerates the possible causes and picks none
        (§6.3).
        """
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            f"{ISSUE_BOOTSTRAP_FAILED}_{self.entry.entry_id}",
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key=ISSUE_BOOTSTRAP_FAILED,
            translation_placeholders={
                # Trimmed, not raw. A repair issue's placeholders are written to
                # `.storage` and rendered in the Repairs panel, so they travel
                # into screenshots, support sessions and backups; and what a
                # parent pastes into the address field is regularly a deep link
                # or an ENT bounce carrying a ticket (§8.2).
                "url": public_url(self.entry.data.get(CONF_PRONOTE_URL)),
                "until": _format_until(self.limiter.hold_until_wallclock),
            },
        )

    @callback
    def async_open_unreadable_issue(self) -> None:
        """Report a login whose response could not be decoded.

        A separate repair from ``bootstrap_failed`` because the diagnosis is
        genuinely different and so is the remedy: the address is right, the
        credentials are right, and something in the establishment's response
        is outside the declared contract -- the pinned ``pronotepy`` on a
        PRONOTE entry, or the DTO mapping on another source. The user cannot
        fix that, so the text asks for a report rather than for a correction.
        """
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            f"{ISSUE_ACCOUNT_UNREADABLE}_{self.entry.entry_id}",
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key=ISSUE_ACCOUNT_UNREADABLE,
            translation_placeholders={
                "url": public_url(self.entry.data.get(CONF_PRONOTE_URL)),
            },
        )

    @callback
    def async_open_mfa_issue(self) -> None:
        """Ask for the 2FA PIN, which is deliberately never stored (§8.1)."""
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            f"{ISSUE_MFA_REQUIRED}_{self.entry.entry_id}",
            # Same reasoning as `invalid_credentials`: the PIN can only be
            # supplied through the re-authentication flow, and an empty confirm
            # dialog that silently closes the issue would be worse than a plain
            # message telling the user where to go.
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key=ISSUE_MFA_REQUIRED,
            data={"entry_id": self.entry.entry_id},
        )

    # -- diagnostics -------------------------------------------------------

    def diagnostics(self) -> dict[str, Any]:
        """Non-secret facts for the diagnostic entities and download.

        The iCal URL, the identity, the tokens and the credentials are absent by
        construction: none of them is ever stored in a snapshot, so there is
        nothing here to redact (§8.2, §8.4).
        """
        connector_diagnostics = self.connector.diagnostics()
        return {
            "limiter": self.limiter.snapshot_counters(),
            "session": connector_diagnostics["session"],
            "scheduler": self.scheduler.diagnostics(),
            # Batches finished since start-up. Reported because a tier with no
            # data is two different faults depending on this number: zero means
            # no batch has run at all -- a heartbeat that never started, or a
            # set-up that never got past the login -- while a number that keeps
            # climbing against an empty tier points at that tier's collector.
            "completed_ticks": self._completed_ticks,
            "students": [
                {
                    "id_hash": _short_hash(student.id),
                    "class_name": student.class_name,
                    # Whether PRONOTE says this child has a profile photo, which
                    # is what decides if the `image` platform creates an entity
                    # at all. Without it, "no photo entity" is indistinguishable
                    # from "the flag is being misread", and the first is a
                    # legitimate school setting while the second is a bug.
                    "has_photo": student.has_photo,
                }
                for student in self.state.students
            ],
            "periods": [
                {"index": period.index, "name": period.name}
                for period in self.state.periods
            ],
            "stale_after": self.stale_after,
            "write_enabled": self.write_enabled,
        }


def _saved_schedule(hass: HomeAssistant) -> dict[str, dict[str, float]]:
    """Per-entry collection schedules, kept across a reload.

    In ``hass.data`` under its own key rather than in the config entry: this is
    runtime state with no business being persisted to disk, and it is only
    meaningful within one process because the values are monotonic.
    """
    store: dict[str, dict[str, float]] = hass.data.setdefault(f"{DOMAIN}_schedule", {})
    return store


def _saved_snapshots(hass: HomeAssistant) -> dict[str, dict[Tier, TierData]]:
    """Per-entry tier snapshots, kept across a reload.

    The companion of :func:`_saved_schedule`, and deliberately the same shape
    of storage: ``hass.data``, never the config entry and never disk. Two
    reasons, and the second is the one that matters. It is runtime state with
    no business being persisted; and a snapshot restored from disk after a
    restart would be data of unknown age presented as current, which is the
    one thing this integration works hardest never to do -- ``fetched_at`` and
    the staleness rule exist precisely so that "I know, but it is old" is said
    out loud rather than implied.

    Within one process the pairing is sound: the entry unloads, the snapshots
    are set aside, the entry sets up again microseconds later, and the data is
    exactly as old as it says it is. Across a real restart this store is empty,
    every tier is therefore due, and the first collection runs -- which is what
    a restart should do.
    """
    store: dict[str, dict[Tier, TierData]] = hass.data.setdefault(
        f"{DOMAIN}_snapshots", {}
    )
    return store


def _credentials_from_entry(entry: PronoteConfigEntry) -> SessionCredentials:
    """Read the credentials out of the config entry."""
    data = entry.data
    return SessionCredentials(
        login_mode=LoginMode(data.get(CONF_LOGIN_MODE, LoginMode.CREDENTIALS)),
        pronote_url=str(data.get(CONF_PRONOTE_URL, "")),
        username=str(data.get("username", "")),
        password=str(data.get("password", "")),
        uuid=str(data.get(CONF_UUID, "")),
        client_identifier=data.get(CONF_CLIENT_IDENTIFIER),
        device_name=data.get(CONF_DEVICE_NAME),
        # Deliberately not persisted: read only if a re-authentication flow just
        # supplied it in this session (§8.1).
        account_pin=data.get(CONF_ACCOUNT_PIN),
        ent_provider=data.get(CONF_ENT),
    )


def _format_until(moment: Any) -> str:
    """Format a hold's end for a repair issue's placeholders."""
    if moment is None:
        return ""
    return dt_util.as_local(moment).isoformat(timespec="minutes")


def _short_hash(value: str) -> str:
    """A truncated fingerprint, never the identifier itself.

    A diagnostic must not hand anybody the material to replay a session
    (annexe A §7).
    """
    from hashlib import blake2s  # noqa: PLC0415 -- only needed for diagnostics

    return blake2s(value.encode(), digest_size=4).hexdigest()
