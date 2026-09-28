"""Domain constants, option keys and defaults.

Every default in this module traces back to a numbered requirement in
``docs/SPECIFICATION.md`` or ``docs/annexe-b-rate-limit.md``. Where a value looks
arbitrary, the comment says which requirement fixed it -- a default nobody can
justify is a default nobody can tune.
"""

from __future__ import annotations

from datetime import time as dt_time
from enum import StrEnum
from typing import Final

#: Named after what a parent follows, not after one platform: the integration
#: collects from PRONOTE and from EcoleDirecte, so neither vendor's name fits,
#: and a neutral domain can never collide with a custom integration that owns
#: ``pronote`` or ``ecoledirecte``. The repository is ``ha-carnet-scolaire`` and
#: the integration is displayed as "Carnet scolaire"; this domain is the only
#: place the underscored form appears. It was ``pronote_ng`` until 0.0.32, and
#: Home Assistant has no domain migration: an entry created under the old name
#: must be removed and added again.
DOMAIN: Final = "carnet_scolaire"

# ---------------------------------------------------------------------------
# Protocol surface
# ---------------------------------------------------------------------------
# Tab identifiers, verified against pronotepy 2.15.7. Two of these were wrong in
# specification v1 and are called out here so a future reader does not "fix"
# them back: reading the news feed is `PageActualites` (clients.py:856), while
# `SaisieActualites` is the *write* used by Information.mark_as_read
# (dataClasses.py:1148); the discussions list is `ListeMessagerie`
# (clients.py:828).
FUNC_TIMETABLE: Final = ("PageEmploiDuTemps", 16)
FUNC_HOMEWORK: Final = ("PageCahierDeTexte", 88)
FUNC_NEWS: Final = ("PageActualites", 8)
FUNC_NEWS_WRITE: Final = ("SaisieActualites", 8)
FUNC_DISCUSSIONS: Final = ("ListeMessagerie", 131)
FUNC_MARKS: Final = ("DernieresNotes", 198)
FUNC_REPORT: Final = ("PageBulletins", 13)
FUNC_ATTENDANCE: Final = ("PagePresence", 19)
FUNC_EVALUATIONS: Final = ("DernieresEvaluations", 201)
FUNC_MENUS: Final = ("PageMenus", 10)
FUNC_TEACHING_STAFF: Final = ("PageEquipePedagogique", 37)
FUNC_PERSONAL_INFO: Final = ("PageInfosPerso", 16)

# `PagePresence` returns absences, delays and punishments in one `listeAbsences`
# list, discriminated by `G`. Verified: dataClasses.py:606/621/636 all read the
# same list and only the filter differs.
PRESENCE_KIND_ABSENCE: Final = 13
PRESENCE_KIND_DELAY: Final = 14
PRESENCE_KIND_PUNISHMENT: Final = 41


class Tier(StrEnum):
    """Collection tiers (SPECIFICATION.md §5.2).

    Ten data tiers plus ``SESSION``, which is not one: it carries the facts that
    arrive with the login itself (periods, class, establishment) and therefore
    costs no call at all.
    """

    SESSION = "session"
    TIMETABLE = "timetable"
    HOMEWORK = "homework"
    NEWS = "news"
    DISCUSSIONS = "discussions"
    MARKS = "marks"
    ATTENDANCE = "attendance"
    EVALUATIONS = "evaluations"
    MENUS = "menus"
    STATIC = "static"
    HISTORY = "history"


class Priority(StrEnum):
    """Sacrifice order when the budget tightens (annexe B §2.4)."""

    CRITICAL = "critical"
    #: A person is waiting on this call: a service, a checkbox, a document
    #: opened, a refresh button. Shed like ``HIGH``, but it crosses quiet
    #: hours -- which protect the address against *automatic* collection, and
    #: a parent opening their child's homework at 22:30 is not that. Still
    #: under the daily cap, the hold, the spacing and the bucket.
    GESTURE = "gesture"
    HIGH = "high"
    NORMAL = "normal"
    LOW = "low"


#: Rank used to compare priorities. Lower sheds later.
PRIORITY_RANK: Final[dict[Priority, int]] = {
    Priority.CRITICAL: 0,
    Priority.GESTURE: 1,
    Priority.HIGH: 1,
    Priority.NORMAL: 2,
    Priority.LOW: 3,
}

#: The priorities quiet hours let through. ``CRITICAL`` because a login, or a
#: tier that has never collected, cannot wait until 06:00 without breaking
#: automations (§2.5); ``GESTURE`` because somebody is looking at the screen.
#: Everything a timer starts waits.
QUIET_HOURS_EXEMPT: Final[frozenset[Priority]] = frozenset(
    {Priority.CRITICAL, Priority.GESTURE}
)

TIER_PRIORITY: Final[dict[Tier, Priority]] = {
    Tier.SESSION: Priority.CRITICAL,
    Tier.TIMETABLE: Priority.HIGH,
    Tier.HOMEWORK: Priority.HIGH,
    Tier.NEWS: Priority.NORMAL,
    Tier.MARKS: Priority.NORMAL,
    Tier.ATTENDANCE: Priority.NORMAL,
    Tier.DISCUSSIONS: Priority.LOW,
    Tier.EVALUATIONS: Priority.LOW,
    Tier.MENUS: Priority.LOW,
    Tier.STATIC: Priority.LOW,
    Tier.HISTORY: Priority.LOW,
}

#: Default interval per tier, in minutes.
DEFAULT_TIER_INTERVALS: Final[dict[Tier, int]] = {
    Tier.TIMETABLE: 15,
    Tier.HOMEWORK: 30,
    Tier.NEWS: 60,
    Tier.DISCUSSIONS: 60,
    Tier.MARKS: 180,
    Tier.ATTENDANCE: 360,
    Tier.EVALUATIONS: 720,
    Tier.MENUS: 1440,
    Tier.STATIC: 1440,
    Tier.HISTORY: 1440,
}

#: The midday break is recognised by *when* it starts and *how long* it lasts,
#: never as "the day's largest gap". A child with one morning lesson and one
#: late-afternoon lesson would otherwise be given a 10:00 "end of morning",
#: which is not a lunch break and would send somebody home at the wrong hour.
#: Read in the establishment's timezone, like every other hour in this
#: integration.
MIDDAY_BREAK_EARLIEST: Final = dt_time(11, 0)
MIDDAY_BREAK_LATEST: Final = dt_time(14, 30)
#: Shorter than this is a corridor gap between two lessons, not a lunch break.
MIDDAY_BREAK_MIN_MINUTES: Final = 45

#: Tiers that feed an ``event`` entity cannot be slowed past this, or the event
#: stops being worth publishing (SPECIFICATION.md §5.2). A cancelled lesson
#: announced four hours late is not information.
MAX_INTERVAL_FOR_EVENT_TIERS: Final[dict[Tier, int]] = {
    Tier.TIMETABLE: 60,
}

#: Tiers the user may switch off entirely. ``SESSION`` is not among them.
CONFIGURABLE_TIERS: Final[tuple[Tier, ...]] = tuple(
    tier for tier in Tier if tier is not Tier.SESSION
)

# ---------------------------------------------------------------------------
# Option keys
# ---------------------------------------------------------------------------
CONF_LOGIN_MODE: Final = "login_mode"
CONF_PRONOTE_URL: Final = "pronote_url"
CONF_ENT: Final = "ent"
CONF_UUID: Final = "uuid"
CONF_CLIENT_IDENTIFIER: Final = "client_identifier"
CONF_DEVICE_NAME: Final = "device_name"
CONF_ACCOUNT_PIN: Final = "account_pin"
CONF_QR_PAYLOAD: Final = "qr_payload"
CONF_QR_PIN: Final = "qr_pin"
CONF_CHILDREN: Final = "children"
CONF_ACCOUNT_KIND: Final = "account_kind"
CONF_SOURCE: Final = "source"

#: The child key table: one record per child ever seen on this account, each
#: ``{"key", "resource_id", "name"}``. **This table is the durable artefact,
#: not the key.**
#:
#: PRONOTE writes a child's resource identifier as ``46#<signature>`` and that
#: signature is *not stable between sessions*. Three distinct values were
#: observed for one pupil on one account. Because an entity's ``unique_id``
#: embedded it, a rotation looked like the arrival of a new child: a second
#: device appeared, a full set of entities was created against it, and the
#: previous set was orphaned in the registry -- every dashboard, automation and
#: helper pointing at it dead, and nothing logged.
#:
#: So the integration mints a key it owns and pairs the announced children
#: against this table at each set-up: by resource identifier while it still
#: matches, by name when it does not. The name is not the key either -- an
#: establishment fixes a spelling, a family changes name -- it is only the
#: second way to recognise a record. §2.4 still forbids a position in the list.
CONF_CHILD_KEYS: Final = "child_keys"

#: Fields of one record in :data:`CONF_CHILD_KEYS`.
CHILD_KEY: Final = "key"
CHILD_RESOURCE_ID: Final = "resource_id"
CHILD_NAME: Final = "name"

OPT_MASTER_TICK: Final = "master_tick"
OPT_TIER_INTERVAL: Final = "interval_{tier}"
OPT_TIER_ENABLED: Final = "enabled_{tier}"

OPT_MIN_REQUEST_INTERVAL: Final = "min_request_interval"
OPT_MAX_REQUESTS_PER_HOUR: Final = "max_requests_per_hour"
OPT_BURST_SIZE: Final = "burst_size"
OPT_MAX_REQUESTS_PER_DAY: Final = "max_requests_per_day"
OPT_MAX_WAIT: Final = "max_wait"
OPT_MAX_LOGINS_PER_DAY: Final = "max_logins_per_day"
OPT_MAX_FAILED_LOGINS_PER_HOUR: Final = "max_failed_logins_per_hour"
OPT_CREDENTIALS_HOLD: Final = "credentials_hold"
OPT_BOOTSTRAP_HOLD: Final = "bootstrap_hold"
OPT_BACKOFF_BASE: Final = "backoff_base"
OPT_BACKOFF_MAX: Final = "backoff_max"
OPT_QUIET_HOURS_ENABLED: Final = "quiet_hours_enabled"
OPT_QUIET_START: Final = "quiet_start"
OPT_QUIET_END: Final = "quiet_end"

OPT_ESTABLISHMENT_TIMEZONE: Final = "establishment_timezone"
OPT_HOMEWORK_HORIZON: Final = "homework_horizon"
OPT_HISTORY_PERIODS: Final = "history_periods"
OPT_WAKE_MARGIN: Final = "wake_margin"
OPT_WRITE_OPERATIONS_ENABLED: Final = "write_operations_enabled"
OPT_STALE_AFTER: Final = "stale_after"
OPT_SESSION_STRATEGY: Final = "session_strategy"
OPT_CONNECT_TIMEOUT: Final = "connect_timeout"
OPT_READ_TIMEOUT: Final = "read_timeout"

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
DEFAULT_MASTER_TICK: Final = 5

# Rate limiter -- annexe B §7. These are a safety net against a software fault,
# not an operating constraint: nominal consumption is ~180 requests a day
# (annexe B §5.3), so the daily cap sits at a factor of eleven.
DEFAULT_MIN_REQUEST_INTERVAL: Final = 1.0
DEFAULT_MAX_REQUESTS_PER_HOUR: Final = 240
DEFAULT_BURST_SIZE: Final = 20
DEFAULT_MAX_REQUESTS_PER_DAY: Final = 2000
DEFAULT_MAX_WAIT: Final = 60.0

# 24, not 120. The design expects one to three logins a day (§6.5); a cap of 120
# could not catch the fault it exists for.
DEFAULT_MAX_LOGINS_PER_DAY: Final = 24
DEFAULT_MAX_FAILED_LOGINS_PER_HOUR: Final = 3
DEFAULT_CREDENTIALS_HOLD: Final = 3600.0
DEFAULT_BOOTSTRAP_HOLD: Final = 3600.0
DEFAULT_BACKOFF_BASE: Final = 30.0
DEFAULT_BACKOFF_MAX: Final = 3600.0
DEFAULT_QUIET_HOURS_ENABLED: Final = True
DEFAULT_QUIET_START: Final = "22:00:00"
DEFAULT_QUIET_END: Final = "06:00:00"

DEFAULT_HOMEWORK_HORIZON: Final = 14
DEFAULT_WAKE_MARGIN: Final = 90
DEFAULT_HISTORY_PERIODS: Final = 0  # 0 means "every closed period"
#: What a *new* PRONOTE entry is created with (§8.3). Seeded into the entry's
#: options at creation, never read as a fallback: an entry that lacks the key
#: was created while writes were off by default, and reading this constant for
#: it would turn writes on for somebody who never chose them.
DEFAULT_WRITE_OPERATIONS_ENABLED: Final = True
#: What an entry without the key is: one created before the default changed.
LEGACY_WRITE_OPERATIONS_ENABLED: Final = False
DEFAULT_STALE_AFTER: Final = 6

# pronotepy passes no `timeout=` anywhere -- verified, `grep -rn timeout` over
# the package returns nothing. Without these a mute server pins the single
# worker thread for ever, the lock is never released, and §5.4's staleness
# regime disguises a permanent deadlock as a slow network (§3.2).
DEFAULT_CONNECT_TIMEOUT: Final = 30.0
DEFAULT_READ_TIMEOUT: Final = 60.0

#: A call lasting longer than this multiple of the read timeout is a deadlock,
#: not a slow network: it raises, logs at ERROR and faults the account (§3.2).
CALL_TIMEOUT_FACTOR: Final = 3.0

# Option ranges, validated in the options flow (annexe B §7).
OPTION_RANGES: Final[dict[str, tuple[float, float]]] = {
    OPT_MIN_REQUEST_INTERVAL: (0.2, 10.0),
    OPT_MAX_REQUESTS_PER_HOUR: (30, 2000),
    OPT_BURST_SIZE: (5, 100),
    OPT_MAX_REQUESTS_PER_DAY: (100, 20000),
    OPT_MAX_WAIT: (5, 300),
    OPT_MAX_LOGINS_PER_DAY: (5, 500),
    OPT_MAX_FAILED_LOGINS_PER_HOUR: (1, 10),
    OPT_CREDENTIALS_HOLD: (300, 86400),
    OPT_BOOTSTRAP_HOLD: (600, 86400),
    OPT_BACKOFF_BASE: (5, 300),
    OPT_BACKOFF_MAX: (60, 21600),
    OPT_HOMEWORK_HORIZON: (1, 365),
    OPT_WAKE_MARGIN: (0, 300),
    OPT_STALE_AFTER: (2, 48),
    OPT_MASTER_TICK: (1, 60),
    OPT_CONNECT_TIMEOUT: (5, 120),
    OPT_READ_TIMEOUT: (10, 300),
}

#: The mark shown at the top of the first screen of the configuration flow.
#:
#: A URL and not a path -- though not because the local brand folder goes
#: unread. Since Home Assistant 2026.3 a custom integration's own ``brand/``
#: directory is served by the frontend through the brands proxy API
#: (``/api/brands/integration/{domain}/{image}``), and those local images take
#: precedence over the ``home-assistant/brands`` repository; so
#: ``custom_components/carnet_scolaire/brand/`` is read by the frontend itself on a
#: recent instance, not only by the HACS validation.
#:
#: The mechanism is version-gated, and the gate is now closed behind us:
#: ``hacs.json`` declares a floor of 2026.8.0, which is above the 2026.3 that
#: introduced the proxy, so **every supported instance serves the local
#: folder**. There is therefore no longer a version on which this constant is
#: the only way to put the mark on that screen -- the argument that justified
#: it has expired.
#:
#: What still holds it here is one unverified claim: that the config-flow
#: dialog's own header consumes the proxy. The proxy's existence does not
#: establish that this particular screen uses it, and if it does, the markdown
#: image below renders the logo a second time. So the removal is pending a
#: visual check of the first step of the flow on a 2026.8-or-newer instance,
#: and not pending anything else. When that check comes back positive, this
#: constant, its import, the ``description_placeholders`` argument in
#: ``config_flow.async_step_user`` and the ``{logo}`` placeholder in
#: ``scripts/build_translations.py`` all go together.
#:
#: Passed as a description placeholder rather than written into the translation
#: string, because hassfest rejects a URL inside one and names this as the
#: mechanism to use instead -- which is also the better shape: the address
#: lives here, in code, rather than duplicated across three catalogues.
#:
#: Two consequences worth stating rather than discovering: rendering that
#: dialog performs one outbound request to raw.githubusercontent.com, and an
#: instance with no route to GitHub shows the alt text instead of a picture.
#: Neither blocks the flow, and this screen sends nothing to PRONOTE.
BRAND_LOGO_URL: Final = (
    "https://raw.githubusercontent.com/FiveElements/ha-carnet-scolaire/"
    "main/custom_components/carnet_scolaire/brand/logo.png"
)

#: The range shared by every per-tier interval. It cannot live in
#: :data:`OPTION_RANGES`, which is keyed by option name, because there are ten
#: of these keys and they are produced by formatting
#: :data:`OPT_TIER_INTERVAL` -- so a lookup by key finds nothing and the value
#: went through unclamped.
#:
#: The lower bound is what makes this worth having. A tier whose interval is
#: zero is due again the instant its batch finishes, so it fires on every
#: master tick and spends the day's entire request budget before lunch; the
#: upper bound is a day, past which "every N minutes" stops meaning anything.
TIER_INTERVAL_RANGE: Final[tuple[float, float]] = (1.0, 1440.0)


class LoginMode(StrEnum):
    """How the account authenticates."""

    QR_CODE = "qr_code"
    CREDENTIALS = "credentials"
    ENT = "ent"


class SessionStrategy(StrEnum):
    """How the session is kept (SPECIFICATION.md §6.5).

    ``LAZY`` is the default and the recommended value: keep the session, and
    re-login only when the server says it expired. Which codes say that is
    ``session.SESSION_EXPIRED_CODES`` and not a single number: an
    establishment answered ``Erreur.G = 8`` ("La page a expiré !") where the
    specification documents 10, and recognising only 10 left a dead session
    held for seven hours. A held session is also given up after
    ``session.PRESUMED_DEAD_AFTER_SECONDS`` without one successful call, which
    is what catches the *next* code nobody has seen yet. It cannot
    be worse than ``PER_BATCH`` for any value of the server's inactivity
    timeout -- if the timeout turns out to be shorter than the fastest tier,
    every batch finds a dead session and opens one, which *is* ``PER_BATCH``.

    ``PER_BATCH`` is kept as an explicit escape hatch: it is what specification
    v1 prescribed, and an establishment with an unusual policy may want it
    pinned rather than discovered.
    """

    LAZY = "lazy"
    PER_BATCH = "per_batch"


DEFAULT_SESSION_STRATEGY: Final = SessionStrategy.LAZY


class LimiterState(StrEnum):
    """Closed value set of ``sensor.<account>_etat_limiteur``.

    Note what is *absent*: there is no ``ip_suspended``. pronotepy decides an
    address is banned with ``if "IP" in html`` -- two capitals anywhere in the
    page, so "Espace IP" in a school's footer triggers it. A state the
    integration cannot establish reliably must not exist in its vocabulary, so
    an unusable bootstrap is reported as ``BOOTSTRAP_FAILED`` and the repair
    text enumerates the possible causes without picking one (§6.3).
    """

    NOMINAL = "nominal"
    THROTTLED = "throttled"
    BACKOFF = "backoff"
    QUIET_HOURS = "quiet_hours"
    CREDENTIALS_HOLD = "credentials_hold"
    BOOTSTRAP_FAILED = "bootstrap_failed"


class AttachmentKind(StrEnum):
    """What one homework attachment *is*, decided once, where the payload is read.

    Declared rather than inferred, for the reason costs are: a consumer that
    tells a file from a link by looking at whether an address is present will
    guess wrong on exactly the case that matters. A link whose address is
    unusable carries no address either, and read that way it was classified as
    a file -- given a signed path, and relayed through the school's session as
    though it were one.

    ``OPAQUE`` is the fail-closed answer: a name that can be shown and opens
    nothing. It is also the default, so an attachment built without deciding
    is never openable by accident.
    """

    #: ``G = 1`` with an identifier: bytes this integration can relay.
    FILE = "file"
    #: ``G = 0`` with a third party's ``http``/``https`` address, publishable.
    LINK = "link"
    #: Everything else: a link with no usable address, a link PRONOTE itself
    #: would authenticate, a file with no identifier to fetch it by.
    OPAQUE = "opaque"


class GradeStatus(StrEnum):
    """The eight grade sentinels, plus one the protocol has not sent yet.

    ``Util.grade_translate`` has exactly eight entries and is indexed by
    ``int(string[1]) - 1``: a future ``|9`` would raise ``IndexError`` upstream
    and fail *every* grade in the batch. ``UNKNOWN`` is how we refuse to inherit
    that failure mode (§3.3.3).
    """

    ABSENT = "absent"
    EXEMPTED = "exempted"
    NOT_GRADED = "not_graded"
    UNFIT = "unfit"
    NOT_SUBMITTED = "not_submitted"
    ABSENT_ZERO = "absent_zero"
    NOT_SUBMITTED_ZERO = "not_submitted_zero"
    CONGRATULATIONS = "congratulations"
    UNKNOWN = "unknown"


#: Maps the raw ``|N`` sentinel to the enum. Index N-1 of the upstream table.
GRADE_SENTINELS: Final[dict[str, GradeStatus]] = {
    "1": GradeStatus.ABSENT,
    "2": GradeStatus.EXEMPTED,
    "3": GradeStatus.NOT_GRADED,
    "4": GradeStatus.UNFIT,
    "5": GradeStatus.NOT_SUBMITTED,
    "6": GradeStatus.ABSENT_ZERO,
    "7": GradeStatus.NOT_SUBMITTED_ZERO,
    "8": GradeStatus.CONGRATULATIONS,
}

# ---------------------------------------------------------------------------
# Event types (annexe A §4)
# ---------------------------------------------------------------------------
EVENT_GRADE_ADDED: Final = "grade_added"
EVENT_HOMEWORK_ADDED: Final = "homework_added"
EVENT_LESSON_CANCELED: Final = "lesson_canceled"
#: A cancellation that was **lifted**: the lesson is back on.
#:
#: Its absence is what made the delta detector's old "if nothing else matched,
#: call it a cancellation" fallback fire ``lesson_canceled`` for exactly the
#: opposite event -- so an automation that notifies "no school first period,
#: sleep in" fired on the morning the lesson was reinstated.
EVENT_LESSON_RESTORED: Final = "lesson_restored"
#: Cancelled, with another lesson taught on the slot: a substitution, a room
#: change or a "cours maintenu", which PRONOTE all serve as the original flagged
#: ``estAnnule`` plus the entry replacing it. The child still has a lesson, so
#: reporting it as ``lesson_canceled`` announced an absent teacher on a morning
#: with an evaluation in another room.
EVENT_LESSON_REPLACED: Final = "lesson_replaced"
EVENT_LESSON_MOVED: Final = "lesson_moved"
#: Only the ``Statut`` label moved -- no flag, no time, no room, no teacher.
#:
#: PRONOTE uses that field for things it does not set ``estAnnule`` for
#: ("Prof. absent", "Cours dépl."), so the change is real and worth reporting;
#: it is just not a cancellation, which is what it used to be reported as.
EVENT_LESSON_STATUS_CHANGED: Final = "lesson_status_changed"
EVENT_ROOM_CHANGED: Final = "room_changed"
EVENT_TEACHER_CHANGED: Final = "teacher_changed"
EVENT_INFORMATION_ADDED: Final = "information_added"
EVENT_ABSENCE_ADDED: Final = "absence_added"
EVENT_DELAY_ADDED: Final = "delay_added"
EVENT_PUNISHMENT_ADDED: Final = "punishment_added"
EVENT_MESSAGE_RECEIVED: Final = "message_received"
EVENT_EVALUATION_ADDED: Final = "evaluation_added"

LESSON_EVENT_TYPES: Final = (
    EVENT_LESSON_CANCELED,
    EVENT_LESSON_RESTORED,
    EVENT_LESSON_REPLACED,
    EVENT_LESSON_MOVED,
    EVENT_ROOM_CHANGED,
    EVENT_TEACHER_CHANGED,
    EVENT_LESSON_STATUS_CHANGED,
)

# ---------------------------------------------------------------------------
# Repair issue identifiers
# ---------------------------------------------------------------------------
ISSUE_INVALID_CREDENTIALS: Final = "invalid_credentials"
ISSUE_BOOTSTRAP_FAILED: Final = "bootstrap_failed"
ISSUE_ACCOUNT_UNREADABLE: Final = "account_unreadable"
ISSUE_DAILY_CAP_NEAR: Final = "daily_cap_near"
ISSUE_MFA_REQUIRED: Final = "mfa_required"

# ---------------------------------------------------------------------------
# Service names
# ---------------------------------------------------------------------------
SERVICE_REFRESH: Final = "refresh"
SERVICE_GET_ICAL_URL: Final = "get_ical_url"
SERVICE_GET_IDENTITY: Final = "get_identity"
SERVICE_MARK_HOMEWORK_DONE: Final = "mark_homework_done"
SERVICE_MARK_INFORMATION_READ: Final = "mark_information_read"
SERVICE_SEND_MESSAGE: Final = "send_message"
SERVICE_GENERATE_TIMETABLE_PDF: Final = "generate_timetable_pdf"
SERVICE_GET_RATE_LIMIT_STATUS: Final = "get_rate_limit_status"
SERVICE_GET_ATTACHMENT_URL: Final = "get_attachment_url"

#: Services every source implements: they never touch a school backend.
AGNOSTIC_SERVICES: Final = frozenset({SERVICE_REFRESH, SERVICE_GET_RATE_LIMIT_STATUS})

#: Attributes never written to the recorder: PRONOTE lists blow past the 16 KiB
#: attribute limit, and the useful history is the count, not the payload (§9).
#:
#: This set is declared once and consumed by **every** entity base, and that
#: matters more than it looks. Home Assistant does not union
#: ``_unrecorded_attributes`` up a class hierarchy: ``Entity.__init_subclass__``
#: computes ``_entity_component_unrecorded_attributes | cls._unrecorded_attributes``,
#: and ``cls._unrecorded_attributes`` resolves by ordinary attribute lookup, so
#: a subclass that declares the name **replaces** its parent's set instead of
#: extending it. A subclass therefore never declares it here; a new name is
#: added to this constant. v0.0.22 learned this the expensive way -- see the
#: comment where ``PronoteSensor`` deliberately has no declaration.
UNRECORDED_LIST_ATTRIBUTES: Final = frozenset(
    {
        "items",
        "lessons",
        "subjects",
        "comments",
        "first_meal",
        "main_meal",
        "side_meal",
        "other_meal",
        "cheese",
        "dessert",
        "acquisitions",
        "schedule",
        "weeks",
        "by_tier",
        "tiers_due",
        "guardians",
        "address",
        "messages",
        # Not a top-level attribute today -- it lives inside `items`, so
        # excluding `items` already covers it. Named anyway, because the
        # addresses it carries are bearer tokens: if the shape ever flattens,
        # the guard is already in place rather than needing to be remembered.
        "attachment_links",
    }
)
