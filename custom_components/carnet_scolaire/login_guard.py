"""Login accounting that has to outlive one config entry object.

Two things in this integration are longer-lived than a ``PronoteAccount``, and
both were lost because they lived on one.

**The punitive state must survive a set-up retry.** ``PronoteAccount`` builds
its own :class:`~.ratelimit.RateLimiter` in ``__init__``, so every attempt at
``async_setup_entry`` got a brand-new one. Home Assistant retries a
``ConfigEntryNotReady`` with a backoff capped at eighty seconds, so an
establishment down for a weekend produced a fresh limiter -- with its hour-long
bootstrap hold, its credentials hold and its exponential backoff all reset --
every eighty seconds for sixty-one hours. That is thousands of logins against a
configured ceiling of twenty-four a day, with ``calls_today`` reporting five,
because each reporter was a different object. The limiter's state is therefore
handed across set-up attempts through ``hass.data``.

**The configuration flow's logins have to be counted too.** The flow's probe
went straight to ``build_client``: no spacing, no bucket, no daily counter, and
-- the part that matters -- no failed-login guard. A user retyping a password
into the re-authentication form could try it as often as they liked, which is
precisely the one gesture that gets an address suspended (annexe B §3.1). Flow
logins now go through a guard held here.

Nothing here is persisted to disk. Monotonic timestamps are only meaningful
within one process, and a hold that survived a restart would be a hold nobody
could explain.
"""

import time
from typing import TYPE_CHECKING, Any, Final

from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .options import build_rate_limit_config
from .ratelimit import RateLimiter

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

#: ``hass.data`` keys. Separate from the integration's own entry store so
#: unloading the last entry does not discard the guard.
_GUARD_KEY: Final = f"{DOMAIN}_login_guard"
_STATE_KEY: Final = f"{DOMAIN}_limiter_state"


def login_guard(hass: HomeAssistant) -> RateLimiter:
    """The limiter that admits the configuration flow's logins.

    One per Home Assistant instance rather than one per entry, because the
    thing being protected is the *address*: the server sees one IP whatever
    number of accounts are configured behind it.

    It is deliberately not the account's limiter. A flow can run while its
    entry is unloaded, mid-set-up or not yet created, so there may be no
    account to ask -- and reaching into a half-built one would be worse than
    counting a flow login twice. The consequence is disclosed rather than
    hidden: a flow login and a set-up login are counted by two different
    counters, so the failed-login guard bounds each of them separately at
    ``max_failed_logins_per_hour`` rather than bounding their sum.
    """
    guard: RateLimiter | None = hass.data.get(_GUARD_KEY)
    if guard is None:
        # Home Assistant's own local time, not an establishment timezone: there
        # may be no entry yet to read one from, and the guard only needs a day
        # boundary and quiet hours it does not use.
        guard = RateLimiter(
            build_rate_limit_config({}),
            clock=time.monotonic,
            now=dt_util.now,
        )
        hass.data[_GUARD_KEY] = guard
    return guard


def limiter_state_store(hass: HomeAssistant) -> dict[str, dict[str, Any]]:
    """Per-entry limiter state, kept across set-up attempts and reloads."""
    store: dict[str, dict[str, Any]] = hass.data.setdefault(_STATE_KEY, {})
    return store


def clear_login_penalties(hass: HomeAssistant, entry_id: str | None) -> None:
    """Forget the failure counters after a *human* re-authentication.

    A person retyping a password with the correction in hand is not an
    automatic retry, so it earns a clean slate (annexe B §3.2) -- and it is the
    only way out of the MFA hold, which is correct, because that hold exists
    precisely because a person has to act.

    Three places hold that state and all three have to be cleared: the flow's
    own guard, the state saved for the entry being repaired, and the live
    account if the entry happens to be loaded.
    """
    login_guard(hass).reset_after_reauth()

    if entry_id is None:
        return

    limiter_state_store(hass).pop(entry_id, None)

    account = hass.data.get(DOMAIN, {}).get(entry_id)
    limiter = getattr(account, "limiter", None)
    if limiter is not None:
        limiter.reset_after_reauth()
