"""Per-tier collection: one place that maps a tier to its request and its DTO.

Kept apart from :mod:`.account` so the orchestration stays readable, and kept
out of :mod:`.gateway` so the gateway keeps its property: one public function per
*protocol call*, not one per displayed value (§3.3).

Each function returns the snapshot, what it cost, and the events its change
detector found. The cost is returned rather than inferred, which is what makes
the per-tier contract test of §11.1 possible -- an accidental property access
doubles the cost with nothing breaking, and that is the regression this project
exists to prevent.
"""

from typing import TYPE_CHECKING, Any, Final

from .const import TIER_PRIORITY, Priority, Tier
from .models import Snapshot

if TYPE_CHECKING:
    from .account import PronoteAccount
    from .delta import DeltaEvent


#: How many failed attempts the first-collection dispensation survives.
#:
#: Three, and the number is chosen against the cadence rather than picked. A
#: failing tier is retried at a quarter of its own interval
#: (`scheduler._RETRIES_PER_INTERVAL`), so three attempts is three quarters of
#: one interval: long enough that a transient failure -- a school's server
#: restarting, one bad response -- never costs a fresh install its
#: dispensation, and short enough that a tab which simply does not work stops
#: being the only tier awake at four in the morning.
_FIRST_COLLECTION_ATTEMPTS: Final = 3


def _priority_for(account: PronoteAccount, tier: Tier, student_id: str) -> Priority:
    """The tier's declared priority, raised for a first collection that works.

    A tier that has never produced a snapshot is publishing `unavailable`, and
    §2.5 is explicit that an unavailable entity breaks automations rather than
    merely looking empty. So its **first** collection outranks quiet hours,
    which is the one dispensation annexe B already grants: ``CRITICAL`` is the
    priority the login itself uses, and the quiet-hours branch of
    ``RateLimiter.check`` exempts it. Without that, an instance restarted -- or
    installed -- at 23:00 showed nothing at all until 06:00 and looked broken,
    and the first collection is exactly the one a user is watching for.

    The dispensation used to be described as self-limiting because a tier that
    collects stops qualifying. True, and insufficient: **success was its only
    exit**. A tier that can never succeed stays data-less for ever, so it held
    a *standing* quiet-hours exemption. On a live instance the establishment's
    unusable teaching-staff tab was therefore the only tier awake between 22:00
    and 06:00 -- which made it the only tier able to accumulate failures, with
    no other tier's success available to reset the account's back-off. One
    broken tab became an account-wide ``backoff``, and entities it does not own
    went stale behind it.

    So the exit is now *evidence* as well as success. The argument for the
    dispensation is about a first collection that works, and it cannot survive
    the demonstration that this one does not: after
    :data:`_FIRST_COLLECTION_ATTEMPTS` failures the tier drops back to its
    declared priority and waits for 06:00 like everything else. Nothing is
    given up -- it keeps its deadline, ``mark_failed`` still floors its retry at
    a quarter of its interval, and the first success resets the counter and
    restores the dispensation for any future gap.

    A tier carrying an armed boost is collected as ``GESTURE``: every boost is
    armed by a person -- a refresh button, the refresh service, or a write that
    asks for its own tier to be re-read -- and a person pressing refresh at
    23:00 is not the automatic collection quiet hours hold back. It cannot
    become a standing exemption either: the scheduler disarms a boost on the
    first outcome, served, deferred or failed, and serves at most one per
    interval. The price is stated rather than hidden: a boosted ``LOW`` tier is
    shed at the daily cap like ``HIGH`` for that one collection.
    """
    if (
        not account.has_data(tier, student_id)
        and account.scheduler.failures(tier) < _FIRST_COLLECTION_ATTEMPTS
    ):
        return Priority.CRITICAL
    if str(tier) in account.scheduler.boosted_tiers():
        return Priority.GESTURE
    return TIER_PRIORITY[tier]


async def collect_tier(  # noqa: PLR0911 -- one arm per tier is the point
    account: PronoteAccount,
    tier: Tier,
    student_id: str,
) -> tuple[Snapshot[Any], int, list[DeltaEvent]]:
    """Collect one tier for one student and derive its events."""
    priority = _priority_for(account, tier, student_id)

    match tier:
        case Tier.TIMETABLE:
            return await _timetable(account, student_id, priority)
        case Tier.HOMEWORK:
            return await _homework(account, student_id, priority)
        case Tier.NEWS:
            return await _news(account, student_id, priority)
        case Tier.DISCUSSIONS:
            return await _discussions(account, student_id, priority)
        case Tier.MARKS:
            return await _marks(account, student_id, priority)
        case Tier.ATTENDANCE:
            return await _attendance(account, student_id, priority)
        case Tier.EVALUATIONS:
            return await _evaluations(account, student_id, priority)
        case Tier.MENUS:
            return await _menus(account, student_id, priority)
        case Tier.STATIC:
            return await _static(account, student_id, priority)
        case Tier.HISTORY:
            return await _history(account, student_id, priority)
        case _:
            message = f"tier {tier} is not collectable"
            raise ValueError(message)


def _snapshot(
    account: PronoteAccount, tier: Tier, student_id: str, data: Any, calls: int
) -> Snapshot[Any]:
    """Wrap facts with the instant they were obtained."""
    return Snapshot(
        data=data,
        fetched_at=account.now(),
        tier=tier,
        calls=calls,
        student_id=student_id,
    )


# ---------------------------------------------------------------------------
# Timetable
# ---------------------------------------------------------------------------


async def _timetable(
    account: PronoteAccount, student_id: str, priority: Priority
) -> tuple[Snapshot[Any], int, list[DeltaEvent]]:
    """The current week and the next: two requests, every collection.

    The horizon was "today and tomorrow" until next week was only asked for
    when tomorrow crossed a week boundary -- one day in seven, Sunday. Which
    meant that from Saturday's first collection the timetable knew nothing
    about Monday for some forty-eight hours: an empty day in the card, no next
    lesson, and ``binary_sensor.<eleve>_vacances`` reading ``on`` every
    weekend. Two requests per batch instead of the 1.14 annexe B used to
    budget, against a daily cap of 2000 and a measured 286.
    """
    result = await account.connector.async_collect(
        Tier.TIMETABLE, student_id, priority=priority
    )
    events = account.delta.timetable(student_id, result.facts)
    return (
        _snapshot(account, Tier.TIMETABLE, student_id, result.facts, result.calls),
        result.calls,
        events,
    )


# ---------------------------------------------------------------------------
# Homework
# ---------------------------------------------------------------------------


async def _homework(
    account: PronoteAccount, student_id: str, priority: Priority
) -> tuple[Snapshot[Any], int, list[DeltaEvent]]:
    """The whole school year, in one request (§5.2)."""
    result = await account.connector.async_collect(
        Tier.HOMEWORK, student_id, priority=priority
    )
    events = account.delta.homework(student_id, result.facts)
    return (
        _snapshot(account, Tier.HOMEWORK, student_id, result.facts, result.calls),
        result.calls,
        events,
    )


# ---------------------------------------------------------------------------
# News
# ---------------------------------------------------------------------------


async def _news(
    account: PronoteAccount, student_id: str, priority: Priority
) -> tuple[Snapshot[Any], int, list[DeltaEvent]]:
    """News items and surveys."""
    result = await account.connector.async_collect(
        Tier.NEWS, student_id, priority=priority
    )
    events = account.delta.news(student_id, result.facts)
    return (
        _snapshot(account, Tier.NEWS, student_id, result.facts, result.calls),
        result.calls,
        events,
    )


# ---------------------------------------------------------------------------
# Discussions
# ---------------------------------------------------------------------------


async def _discussions(
    account: PronoteAccount, student_id: str, priority: Priority
) -> tuple[Snapshot[Any], int, list[DeltaEvent]]:
    """Thread list, expanding only the threads that became active.

    The previous unread counts are handed in so the gateway can decide which
    threads are worth a second request -- reading
    ``pronotepy.Discussion.messages`` costs one each, and doing it for every
    thread would roughly double the integration's whole budget.
    """
    result = await account.connector.async_collect(
        Tier.DISCUSSIONS, student_id, priority=priority
    )
    events = account.delta.discussions(student_id, result.facts)
    return (
        _snapshot(account, Tier.DISCUSSIONS, student_id, result.facts, result.calls),
        result.calls,
        events,
    )


# ---------------------------------------------------------------------------
# Marks
# ---------------------------------------------------------------------------


async def _marks(
    account: PronoteAccount, student_id: str, priority: Priority
) -> tuple[Snapshot[Any], int, list[DeltaEvent]]:
    """One ``DernieresNotes`` request plus the current period's report card.

    Nothing to collect when the current period cannot be determined: pronotepy
    would fall back to ``onglets[0]``, which silently names the wrong period in
    an establishment that does not publish grades, and a wrong average is worse
    than a missing one (§3.3.3).
    """
    result = await account.connector.async_collect(
        Tier.MARKS, student_id, priority=priority
    )
    events = account.delta.marks(student_id, result.facts)
    return (
        _snapshot(account, Tier.MARKS, student_id, result.facts, result.calls),
        result.calls,
        events,
    )


# ---------------------------------------------------------------------------
# Attendance
# ---------------------------------------------------------------------------


async def _attendance(
    account: PronoteAccount, student_id: str, priority: Priority
) -> tuple[Snapshot[Any], int, list[DeltaEvent]]:
    """One ``PagePresence`` request, three datasets."""
    result = await account.connector.async_collect(
        Tier.ATTENDANCE, student_id, priority=priority
    )
    events = account.delta.attendance(student_id, result.facts)
    return (
        _snapshot(account, Tier.ATTENDANCE, student_id, result.facts, result.calls),
        result.calls,
        events,
    )


# ---------------------------------------------------------------------------
# Evaluations
# ---------------------------------------------------------------------------


async def _evaluations(
    account: PronoteAccount, student_id: str, priority: Priority
) -> tuple[Snapshot[Any], int, list[DeltaEvent]]:
    """Competency evaluations for the current period."""
    result = await account.connector.async_collect(
        Tier.EVALUATIONS, student_id, priority=priority
    )
    events = account.delta.evaluations(student_id, result.facts)
    return (
        _snapshot(account, Tier.EVALUATIONS, student_id, result.facts, result.calls),
        result.calls,
        events,
    )


# ---------------------------------------------------------------------------
# Menus
# ---------------------------------------------------------------------------


async def _menus(
    account: PronoteAccount, student_id: str, priority: Priority
) -> tuple[Snapshot[Any], int, list[DeltaEvent]]:
    """Today's and tomorrow's canteen menus."""
    result = await account.connector.async_collect(
        Tier.MENUS, student_id, priority=priority
    )
    return (
        _snapshot(account, Tier.MENUS, student_id, result.facts, result.calls),
        result.calls,
        [],
    )


# ---------------------------------------------------------------------------
# Static
# ---------------------------------------------------------------------------


async def _static(
    account: PronoteAccount, student_id: str, priority: Priority
) -> tuple[Snapshot[Any], int, list[DeltaEvent]]:
    """The teaching staff -- one request, and the iCal URL is *not* here.

    Collecting the iCal URL on a schedule would put an autonomous
    authentication bearer into the snapshot store, a long-lived structure whose
    purpose is to be dumped into a diagnostic report. ``get_ical_url`` fetches
    it on demand instead (§8.2).
    """
    result = await account.connector.async_collect(
        Tier.STATIC, student_id, priority=priority
    )
    return (
        _snapshot(account, Tier.STATIC, student_id, result.facts, result.calls),
        result.calls,
        [],
    )


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------


async def _history(
    account: PronoteAccount, student_id: str, priority: Priority
) -> tuple[Snapshot[Any], int, list[DeltaEvent]]:
    """Closed periods, read once a day.

    A closed period cannot change, so re-reading it every three hours -- as the
    integration this replaces does -- spends requests on a constant result
    (§5.2). No events either: a closed period produces no news.
    """
    result = await account.connector.async_collect(
        Tier.HISTORY, student_id, priority=priority
    )
    return (
        _snapshot(account, Tier.HISTORY, student_id, result.facts, result.calls),
        result.calls,
        [],
    )
