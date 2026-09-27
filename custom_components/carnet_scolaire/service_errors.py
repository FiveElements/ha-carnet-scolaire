"""What a person who asked for something is told when PRONOTE did not answer.

A service call and a tap on a checkbox are *gestures*: somebody -- or some
automation -- is waiting on the answer. A scheduled collection that fails keeps
its snapshot and tries again at the next tick, and the session's exceptions are
shaped for that: they are control flow for the account, the backoff and the
repair issues. Let through to a gesture they reach Home Assistant raw, which
renders them as an unexplained error with a Python class name in it -- the
Silver rule ``action-exceptions`` exists to forbid exactly that.

So every gesture that reaches the session goes through :func:`async_run_gesture`
here, and nowhere else: ``services.py`` for every action, ``todo.py`` for the
checkbox. One chokepoint, so a family of failure added to the session tomorrow
is translated in one place rather than in whichever caller remembered it.

Two rules shape the translation.

**A fixed sentence, never the exception's text.** A transport error from
``requests`` quotes the URL it failed on, and on an ENT bounce or a PRONOTE page
that URL can carry session parameters; ``pronotepy``'s own messages can quote
the request. None of that may reach a notification, a trace or a log line an
automation author pastes into an issue. The only placeholders are values this
integration minted itself -- the limiter's reason codes and a whole number of
seconds.

**What the call-specific errors mean stays with the caller.** ``ItemNotFound``,
``WriteNotApplied`` and the discussion errors are raised by the gateway *inside*
the call and pass through here untouched; each caller translates them before
anything generic can, because they say something precise about the one item
the gesture named.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.exceptions import HomeAssistantError
from pronotepy.exceptions import DataError, PronoteAPIError

from .const import DOMAIN, Priority
from .gateway import ProtocolChanged
from .ratelimit import TierDeferred
from .session import (
    AccountUnreadable,
    BootstrapFailed,
    IntegrationFault,
    InvalidCredentials,
    LoginRefused,
    MfaRequired,
)

if TYPE_CHECKING:
    from .connectors.protocol import PronoteExtras
    from .const import Tier


def _fail(key: str, **placeholders: str) -> HomeAssistantError:
    """A translated error for ``key``, carrying only minted placeholders.

    Every caller raises it ``from`` the original, so the traceback in the log
    keeps the real cause for whoever turns on
    ``custom_components.carnet_scolaire: debug``; the *message* is the
    translation and nothing else.
    """
    return HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key=key,
        translation_placeholders=placeholders or None,
    )


async def async_run_gesture(
    extras: PronoteExtras,
    tier: Tier,
    student_id: str | None,
    fn: Any,
    *,
    cost: int,
    priority: Priority = Priority.GESTURE,
) -> Any:
    """Run one gesture through the session, translating every known failure.

    Plain ``HomeAssistantError`` throughout, never ``ServiceValidationError``:
    none of these says the call was wrong, and the validation class would tell
    an automation's author to fix a call that has nothing wrong with it.

    The arms are disjoint today, and the two broad ones come last so that
    they stay the fallback: ``OSError`` because ``requests``' exceptions and
    the built-in ``TimeoutError`` all inherit from it, ``PronoteAPIError``
    because it is the base of the ``pronotepy`` errors the session has already
    turned into its own classes. A session class that later subclassed either
    would still be caught by its own, more precise arm.
    """
    try:
        return await extras.session.run(
            str(tier), priority, fn, student_id=student_id, cost=cost
        )
    except TierDeferred as deferred:
        # A service that is silently postponed looks like one that did
        # nothing, so unlike a scheduled collection a deferred gesture fails --
        # saying why and roughly when to try again. Whole seconds, truncated:
        # "wait 42 seconds" then being refused at 43 is worse than waiting one
        # second longer than told.
        raise _fail(
            "service_deferred",
            reason=str(deferred.reason),
            seconds=str(int(deferred.retry_after)),
        ) from deferred
    except LoginRefused as refused:
        # The limiter would not let a login through -- the daily cap, or a hold
        # after failed logins. Nothing was sent, and retrying sooner is the one
        # thing that makes it worse.
        raise _fail("service_login_refused", reason=str(refused.reason)) from refused
    except (InvalidCredentials, MfaRequired) as rejected:
        # The remedy is the re-authentication flow, not a retry: a second
        # wrong password is exactly what the IP guard counts. Nothing is
        # retried from here.
        raise _fail("service_reauth_required") from rejected
    except BootstrapFailed as unusable:
        raise _fail("service_unreachable") from unusable
    except IntegrationFault as fault:
        # The session raises this for two unrelated reasons. A call that
        # outlived its deadline is an outage like any other; a child the
        # account no longer announces is this integration's fault, and the
        # session has already logged which child.
        if isinstance(fault.__cause__, TimeoutError):
            raise _fail("service_unreachable") from fault
        raise _fail("service_internal_error") from fault
    except (AccountUnreadable, ProtocolChanged, DataError) as unreadable:
        raise _fail("service_unreadable") from unreadable
    except OSError as unreachable:
        # `requests` exceptions and `TimeoutError` are all `OSError`. Their text
        # quotes the URL they failed on, which is exactly why it goes nowhere.
        raise _fail("service_unreachable") from unreachable
    except PronoteAPIError as refusal:
        # A protocol refusal the session did not recover from by reconnecting:
        # an unrecognised `Erreur.G`, a sanction, a session refused twice.
        raise _fail("service_refused") from refusal
