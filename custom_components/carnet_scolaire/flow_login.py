"""The one blocking login the configuration flow needs, isolated.

Constructing a ``pronotepy`` client *is* a network login: ``ClientBase.__init__``
ends with ``self.logged_in = self._login()``. So this whole module is
synchronous and meant to be handed to ``async_add_executor_job`` -- calling it
from the event loop would block Home Assistant for the length of a PRONOTE
handshake (§3.2).

It lives apart from ``config_flow.py`` for a reason that is not cosmetic: the
flow must not import ``pronotepy`` at module scope, or a broken dependency turns
"the integration cannot log in" into "the integration cannot be added at all".

Nothing here is retried. A wrong password gets exactly one attempt per user
gesture, because repeatedly retrying one is what gets an address suspended, and
the suspension is undocumented and costly (annexe B §3.1).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Final

from .const import (
    CONF_ACCOUNT_PIN,
    CONF_CLIENT_IDENTIFIER,
    CONF_DEVICE_NAME,
    CONF_ENT,
    CONF_LOGIN_MODE,
    CONF_PRONOTE_URL,
    CONF_QR_PAYLOAD,
    CONF_QR_PIN,
    CONF_UUID,
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_READ_TIMEOUT,
    LoginMode,
)
from .ent_providers import resolve_ent_provider

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from .hardened_client import HardenedClient

_LOGGER: Final = logging.getLogger(__name__)


def probe_account(data: Mapping[str, Any]) -> dict[str, Any]:
    """Log in once, learn the account's shape, and hand back what to persist.

    Blocking. Returns a mapping carrying:

    ``account_id``
        A stable identifier for the config entry's ``unique_id``, so adding the
        same account twice is refused rather than duplicating every entity.
    ``children``
        ``(id, name)`` pairs -- empty for a student account, one or more for a
        parent account.
    ``account_name``
        The name on the account itself: the pupil's, on a student account. Used
        only to check a reconnection reached the same child, never persisted.
    ``title``
        What to call the entry.
    the rotated credentials
        In token mode every ``Authentification`` returns a fresh
        ``jetonConnexionAppliMobile`` and ``pronotepy`` overwrites
        ``self.password`` with it. Not persisting it means losing access at the
        next start (§7.2), which is why they come back from here rather than
        being assumed unchanged.
    """
    from .config_flow import (  # noqa: PLC0415 -- the flow owns these categories
        ProbeBootstrapFailed,
        ProbeEntRequired,
        ProbeError,
        ProbeInvalidCredentials,
        ProbeMfaRequired,
        ProbeQrInvalid,
        ProbeQrRefused,
    )
    from .hardened_client import (  # noqa: PLC0415 -- keeps pronotepy out of the flow
        BootstrapDelegated,
        BootstrapUnavailable,
        build_client,
        release_client,
    )

    try:
        from pronotepy.exceptions import (  # noqa: PLC0415 -- optional dependency
            CryptoError,
            ENTLoginError,
            MFAError,
            PronoteAPIError,
            QRCodeDecryptError,
        )
    except ImportError as error:  # pragma: no cover - a broken install
        raise ProbeBootstrapFailed(str(error)) from error

    mode = LoginMode(data.get(CONF_LOGIN_MODE, LoginMode.CREDENTIALS))
    #: Whether this call is a QR *enrolment* rather than a login with stored
    #: credentials. Read twice below -- once to pick the path, once to classify
    #: a `CryptoError` -- and the two must agree, so it is decided here rather
    #: than tested twice.
    enrolling = mode is LoginMode.QR_CODE and bool(data.get(CONF_QR_PAYLOAD))
    client: HardenedClient | None = None

    try:
        # Branching on the *payload*, not on the mode. A QR-enrolled entry keeps
        # `login_mode: qr_code` for the rest of its life, but §8.1 refuses to
        # persist the single-use payload -- so every login after the enrolment
        # one has the mode without the payload, the re-authentication probe
        # included. Branching on the mode alone made `_qr_login` raise
        # `KeyError: 'qr_payload'` there, unclassified, which the flow reported
        # as "unexpected error" on the one form whose whole job is to repair a
        # broken login. `build_client` maps `qr_code` to pronotepy's token mode,
        # which is exactly what the runtime session does with the same entry.
        if enrolling:
            client = _qr_login(data)
        else:
            client = build_client(
                login_mode=mode.value,
                pronote_url=str(data[CONF_PRONOTE_URL]),
                username=str(data.get("username", "")),
                password=str(data.get("password", "")),
                uuid=str(data.get(CONF_UUID, "")),
                account_pin=data.get(CONF_ACCOUNT_PIN),
                client_identifier=data.get(CONF_CLIENT_IDENTIFIER),
                device_name=data.get(CONF_DEVICE_NAME),
                ent=_ent_provider(data),
                connect_timeout=DEFAULT_CONNECT_TIMEOUT,
                read_timeout=DEFAULT_READ_TIMEOUT,
            )

        if not client.logged_in:
            # `_login` returns False when the `cle` key is absent from the
            # `Authentification` response, which is what a wrong password looks
            # like when the challenge happens to decrypt. Classifying it as
            # anything else would leave the credentials guard never firing
            # (annexe B §3.1).
            raise ProbeInvalidCredentials("the server did not open a session")

        return _describe(client, data)

    except MFAError as error:
        raise ProbeMfaRequired(str(error)) from error
    except QRCodeDecryptError as error:
        raise ProbeQrInvalid(str(error)) from error
    except CryptoError as error:
        # A wrong password does **not** raise `PronoteAPIError`: the challenge
        # decryption fails first. This is the single most important line in the
        # module (annexe B §3.1).
        #
        # But on the enrolment path it is not about a password, because there is
        # none. `qrcode_login` decrypts the QR code locally first -- a wrong
        # four-digit code raises `QRCodeDecryptError`, caught above -- and then
        # agrees the challenge from the payload's own `login` and `jeton`. The
        # account's two-factor PIN cannot be the cause either: it never enters
        # the key, which `ClientBase._login` derives as
        # `username + SHA256(alea + password)` and uses the PIN only in
        # `_do_2fa`, after a challenge that has already succeeded.
        #
        # So a failure here is about the QR code itself, and it is classified as
        # such. It was reported as "the credentials were refused", which sent a
        # parent looking at the one field that could not possibly matter --
        # upstream's own message compounds it by appending "probably the qr code
        # has expired" on nothing more than `login_mode == "qr_code"`.
        if enrolling:
            raise ProbeQrRefused(str(error)) from error
        raise ProbeInvalidCredentials(str(error)) from error
    except ENTLoginError as error:
        raise ProbeInvalidCredentials(str(error)) from error
    except BootstrapUnavailable as error:
        # A portal's login form instead of PRONOTE's page is the one bootstrap
        # failure with a known remedy, and only for a *direct* login: in ENT
        # mode the same page means the portal's cookies did not open PRONOTE,
        # and sending that parent to the mode they are already in would be a
        # loop. One arm rather than two, `BootstrapDelegated` being a subclass.
        delegated = (
            isinstance(error, BootstrapDelegated) and mode is LoginMode.CREDENTIALS
        )
        verdict: type[ProbeError] = (
            ProbeEntRequired if delegated else ProbeBootstrapFailed
        )
        raise verdict(str(error)) from error
    except PronoteAPIError as error:
        raise ProbeBootstrapFailed(str(error)) from error
    finally:
        # Closed either way. A client left open here holds a session the
        # integration is about to open again, and two live sessions on one
        # account desynchronise the encrypted request counter (annexe B §1).
        release_client(client)


def _qr_login(data: Mapping[str, Any]) -> HardenedClient:
    """Enrol from a QR payload. Two complete logins, by construction.

    ``qrcode_login`` builds a client -- whose constructor logs in -- posts
    ``PageInfosPerso`` 49 to read the security mode, then calls ``token_login``
    with the exported credentials. Both are real logins against the server;
    the doubling is upstream's design, not an accident, and annexe B §3.3
    exempts it from the failed-login guard rather than letting it consume two
    thirds of a three-attempt budget.
    """
    from .hardened_client import (  # noqa: PLC0415 -- keeps pronotepy out of the flow
        HardenedClient,
        _patched_communication,
        _period_registry_confined,
    )

    payload = dict(data[CONF_QR_PAYLOAD])
    with (
        _patched_communication(DEFAULT_CONNECT_TIMEOUT, DEFAULT_READ_TIMEOUT),
        _period_registry_confined() as confine,
    ):
        client: HardenedClient = HardenedClient.qrcode_login(
            payload,
            str(data[CONF_QR_PIN]),
            str(data[CONF_UUID]),
            account_pin=data.get(CONF_ACCOUNT_PIN),
            device_name=data.get(CONF_DEVICE_NAME),
        )
        confine(client)
    return client


def _ent_provider(data: Mapping[str, Any]) -> Callable[..., Any] | None:
    """Resolve the named ENT provider, or refuse.

    Resolved by name at call time rather than stored, because a config entry
    holds JSON and ``pronotepy.ent`` exposes plain functions.

    An unresolvable name **raises**, and it used to return ``None``. That looks
    like a graceful degradation and is the opposite of one: ``build_client``
    then runs with ``ent=None`` in ``mode="normal"``, which sends the *ENT
    portal's* username and password straight to the PRONOTE server. The login
    cannot succeed -- so the user was shown "invalid credentials" for what is a
    typo in a provider name, and one of the three slots on the IP guard was
    spent proving it. The provider list is a free-text-capable selector
    (``custom_value=True``), so a typo is not a remote possibility.

    Two names can reach here: one the user typed, and one persisted long ago
    that a ``pronotepy`` upgrade has since removed. The second is why the check
    cannot live only in the flow's form validation -- the runtime login path
    resolves the same stored name on every reconnection.
    """
    name = data.get(CONF_ENT)
    if not name:
        return None

    from .config_flow import ProbeEntUnknown  # noqa: PLC0415 -- the flow owns it

    provider = resolve_ent_provider(str(name))
    if provider is None:
        # The name is the user's own input, not a secret: it names a public
        # regional portal and is what has to appear in the log for the message
        # on the screen to be actionable.
        _LOGGER.error("unknown ENT provider %r", name)
        raise ProbeEntUnknown(str(name))
    return provider


def _describe(client: HardenedClient, data: Mapping[str, Any]) -> dict[str, Any]:
    """Read the account's shape and the credentials to persist.

    Zero extra requests: the children, the class and the establishment all come
    out of ``parametres_utilisateur`` and ``func_options``, already in memory
    once authenticated (annexe A §2).
    """
    from .hardened_client import (  # noqa: PLC0415 -- keeps pronotepy out of the flow
        export_credentials,
    )

    children = [(str(child.id), str(child.name)) for child in client.children]
    account_name = str(client.info.name)
    establishment = _establishment(client)

    described: dict[str, Any] = {
        "account_id": _account_id(client, data),
        # Read by the reconnection identity check on a pupil's own login, which
        # announces no child; dropped before anything is persisted
        # (`config_flow._NEVER_PERSISTED`).
        "account_name": account_name,
        "children": children,
        "title": (
            f"{establishment} - {account_name}" if establishment else account_name
        ),
    }
    described.update(export_credentials(client))
    return described


def _establishment(client: HardenedClient) -> str | None:
    """The establishment's name, tolerating an absent key."""
    try:
        options = client.func_options["dataSec"]["data"]
        name = options["ressource"]["L"]
    except KeyError, TypeError:
        return None
    return str(name) if name else None


def _account_id(client: HardenedClient, data: Mapping[str, Any]) -> str:
    """A stable ``unique_id`` for the entry.

    The establishment's URL plus the account's own PRONOTE identifier. Not the
    username: an ENT login and a direct login for the same account use
    different usernames, and not the token either, since it rotates at every
    login (§7.2).
    """
    url = str(data.get(CONF_PRONOTE_URL, "")).rstrip("/")
    return f"{url}::{client.info.id}"
