"""The one blocking login, and the classification that is its real content.

This module was at 0 % coverage, which is indefensible for the file that
handles credentials: it is the only place a password, a QR payload and a
four-digit PIN are all in scope at once, and it is the file that decides what
the user is told when a login fails.

**The six ``except`` arms are the module.** Five of the six exception types
``probe_account`` catches are subclasses of the sixth:

    PronoteAPIError
    ├── MFAError
    ├── ENTLoginError
    ├── BootstrapUnavailable      (this project's own)
    └── CryptoError
        └── QRCodeDecryptError

So the arms are not six independent cases -- they are one ordered chain, and
every one of them would still *catch* if it were moved to the bottom. Reordering
two lines therefore changes nothing that a type checker, a linter or a smoke
test would notice, and everything about what the user reads: a QR code that
failed to decrypt would say "check your password", and a school whose server is
down would say the same. ``test_the_order_of_the_arms_is_load_bearing`` is the
test that holds that line, and it asserts the subclass relationships explicitly
rather than leaving the reader to look them up in upstream.

Nothing here needs Home Assistant. ``probe_account`` is synchronous by design
-- constructing a ``pronotepy`` client *is* a network login, so the whole module
exists to be handed to ``async_add_executor_job`` -- which is why this file
carries no ``REQUIRES_HASS`` marker and runs on Windows as well as in CI.

The one seam mocked is ``hardened_client.build_client``, imported by
``probe_account`` at call time and therefore patchable on the module. Everything
above it runs for real: the mode mapping, the ENT resolution, the shape reading,
the credential export, and the ``finally`` that closes the client.
"""

import logging
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

from pronotepy.exceptions import (
    CryptoError,
    ENTLoginError,
    MFAError,
    PronoteAPIError,
    QRCodeDecryptError,
)
import pytest

from custom_components.carnet_scolaire.config_flow import (
    ProbeBootstrapFailed,
    ProbeEntRequired,
    ProbeEntUnknown,
    ProbeError,
    ProbeInvalidCredentials,
    ProbeMfaRequired,
    ProbeQrInvalid,
    ProbeQrRefused,
)
from custom_components.carnet_scolaire.const import (
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
from custom_components.carnet_scolaire.flow_login import probe_account
from custom_components.carnet_scolaire.hardened_client import (
    BootstrapDelegated,
    BootstrapUnavailable,
)

from .conftest import CHILDREN

if TYPE_CHECKING:
    from collections.abc import Iterator

    from .fixtures.client import FakeClient

#: The establishment address, trimmed, as the flow stores it. Synthetic, on the
#: reserved `.invalid` TLD -- the hard rule of this suite (see
#: `tests/fixtures/__init__.py`).
URL = "https://demo.example.invalid/pronote/parent.html"

#: A QR payload of the right *shape* and none of the right content. `probe`
#: never looks inside it; `pronotepy` would, and is mocked.
QR_PAYLOAD = {
    # Hexadecimal, and that is not cosmetic: `qrcode_login` runs
    # `bytes.fromhex` on both of these before it does anything else
    # (pronotepy 2.15.7, `clients.py:184-185`), so a payload that is not
    # hex is rejected by `_parse_qr_payload` and never reaches the probe.
    # A fixture written in prose was therefore testing a shape the code
    # now refuses. Visibly fictional all the same, per CONTRIBUTING §1.1.
    "jeton": "0bad0bad0bad0bad0bad0bad0bad0bad",
    "login": "0badc0de0badc0de",
    "url": URL,
}


def _credentials_data(**extra: Any) -> dict[str, Any]:
    """Entry data for a plain username-and-password login."""
    return {
        CONF_LOGIN_MODE: LoginMode.CREDENTIALS.value,
        CONF_PRONOTE_URL: URL,
        "username": "parent-under-test",
        "password": "not-a-real-password",
        **extra,
    }


def _qr_data(**extra: Any) -> dict[str, Any]:
    """Entry data for a QR enrolment."""
    return {
        CONF_LOGIN_MODE: LoginMode.QR_CODE.value,
        CONF_QR_PAYLOAD: dict(QR_PAYLOAD),
        CONF_QR_PIN: "0000",
        CONF_UUID: "UUID-UNDER-TEST",
        **extra,
    }


@pytest.fixture(name="build")
def build_fixture(client: FakeClient) -> Iterator[Any]:
    """Patch the one seam: the call that would log in for real."""
    with patch(
        "custom_components.carnet_scolaire.hardened_client.build_client",
        return_value=client,
    ) as build:
        yield build


# ---------------------------------------------------------------------------
# What comes back
# ---------------------------------------------------------------------------


def test_a_student_account_comes_back_with_no_children(
    client: FakeClient,
    build: Any,
) -> None:
    """A student account has no children, and an empty list says so.

    Not `None`, and not "one child, itself": the flow branches on
    `len(children) > 1` to decide whether to ask, and a student account must
    reach `_async_create` without a selection step.
    """
    described = probe_account(_credentials_data())

    assert described["children"] == []


def test_a_parent_account_comes_back_with_one_pair_per_child(
    parent_client: FakeClient,
) -> None:
    """`(id, name)` pairs, in the order the server gave them.

    The pairs are labels for a selection form, which is why the names come back
    at all -- they are deliberately *not* persisted (`config_flow` drops
    `CONF_CHILDREN` from the probe's result before merging).
    """
    with patch(
        "custom_components.carnet_scolaire.hardened_client.build_client",
        return_value=parent_client,
    ):
        described = probe_account(_credentials_data())

    assert described["children"] == [
        (child_id, child_name) for child_id, child_name in CHILDREN
    ]


def test_the_account_id_is_the_establishment_and_the_pronote_identifier(
    client: FakeClient,
    build: Any,
) -> None:
    """The `unique_id`, and what it deliberately is not.

    Not the username: an ENT login and a direct login for the same account use
    different ones, and the same account added twice through two doors must
    still be refused. Not the token either, which rotates at every login (§7.2).
    """
    described = probe_account(_credentials_data())

    assert described["account_id"] == f"{URL}::{client.info.id}"
    assert "parent-under-test" not in described["account_id"]


def test_a_trailing_slash_does_not_produce_a_second_account(
    client: FakeClient,
    build: Any,
) -> None:
    """`.../parent.html` and `.../parent.html/` are one account, not two."""
    with_slash = probe_account(_credentials_data(**{CONF_PRONOTE_URL: URL + "/"}))
    without = probe_account(_credentials_data())

    assert with_slash["account_id"] == without["account_id"]


def test_the_rotated_credentials_come_back_to_be_persisted(
    client: FakeClient,
    build: Any,
) -> None:
    """The whole reason `_describe` exists rather than the caller assuming.

    In token mode every `Authentification` returns a fresh
    `jetonConnexionAppliMobile` and `pronotepy` overwrites `self.password` with
    it. Not persisting the new one means losing access at the next start
    (§7.2), so the export is asserted here and not merely called.
    """
    described = probe_account(_credentials_data())

    assert described["password"] == "rotated-token-not-a-real-one"
    assert described["client_identifier"] == "CLIENT-ID-STUB"
    assert described["uuid"] == "UUID-STUB"
    assert client.credential_exports == 1


def test_reading_the_account_s_shape_costs_no_extra_request(
    client: FakeClient,
    build: Any,
) -> None:
    """Zero posts after the login itself (annexe A §2).

    The children, the class and the establishment all come out of
    `parametres_utilisateur` and `func_options`, already in memory once
    authenticated. A probe that posted again would spend budget on every
    keystroke-driven retry of the form.
    """
    probe_account(_credentials_data())

    assert client.posted_names == []


# ---------------------------------------------------------------------------
# The establishment name, which is allowed to be missing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "func_options",
    [
        pytest.param({}, id="nothing at all"),
        pytest.param(
            {"dataSec": {"data": {"General": {}}}}, id="the key simply absent"
        ),
        pytest.param({"dataSec": {"data": {"ressource": None}}}, id="a null resource"),
        pytest.param({"dataSec": {"data": {"ressource": []}}}, id="a list"),
        pytest.param(
            {"dataSec": {"data": {"ressource": {"L": ""}}}}, id="an empty name"
        ),
    ],
)
def test_a_malformed_establishment_block_is_not_an_error(
    client: FakeClient,
    build: Any,
    func_options: dict[str, Any],
) -> None:
    """Every shape the key can degenerate into falls back to the name.

    `func_options` is read here for a *label*. Losing it should cost the entry
    a nicer title and nothing else -- certainly not the ability to add the
    account, which is what an uncaught `KeyError` in a probe amounts to.

    `TypeError` as well as `KeyError`: subscripting `None` or a list raises the
    former, and a protocol change that turns an object into a list is exactly
    the kind of thing that happens between two PRONOTE releases.
    """
    client.func_options = func_options

    assert probe_account(_credentials_data())["title"] == "Enfant Un"


def test_a_named_establishment_prefixes_the_title(
    client: FakeClient,
    build: Any,
) -> None:
    """Two accounts at two schools should not both be called "Enfant Un"."""
    client.func_options = {
        "dataSec": {"data": {"ressource": {"L": "Lycée de démonstration"}}}
    }

    described = probe_account(_credentials_data())

    assert described["title"] == "Lycée de démonstration - Enfant Un"


# ---------------------------------------------------------------------------
# What the login is handed
# ---------------------------------------------------------------------------


def test_the_login_is_handed_the_mode_and_the_timeouts(
    client: FakeClient,
    build: Any,
) -> None:
    """A probe with no deadline hangs a worker thread for ever.

    The timeouts are asserted because `_patched_communication` publishes them
    through a thread-local: nothing in the type system connects the constant to
    the socket, so only a test does.
    """
    probe_account(_credentials_data())

    kwargs = build.call_args.kwargs
    assert kwargs["login_mode"] == LoginMode.CREDENTIALS.value
    assert kwargs["pronote_url"] == URL
    assert kwargs["connect_timeout"] == DEFAULT_CONNECT_TIMEOUT
    assert kwargs["read_timeout"] == DEFAULT_READ_TIMEOUT


def test_an_absent_optional_field_is_passed_as_an_empty_string_not_a_none(
    client: FakeClient,
    build: Any,
) -> None:
    """Credentials mode carries no UUID, and upstream's default is `""`.

    `HardenedClient(uuid=None)` is not the same call as `uuid=""`, and the
    difference only shows up as a `TypeError` deep inside upstream's
    enrolment path.
    """
    probe_account(_credentials_data())

    kwargs = build.call_args.kwargs
    assert kwargs["uuid"] == ""
    assert kwargs["account_pin"] is None
    assert kwargs["client_identifier"] is None
    assert kwargs["device_name"] is None


def test_the_pin_and_the_device_name_reach_the_login_when_they_exist(
    client: FakeClient,
    build: Any,
) -> None:
    """The PIN is used here, and this is the only place it legitimately is.

    §8.1 refuses to persist it; the flow therefore has to hand it straight to
    the one login that needs it, which is what this asserts.
    """
    probe_account(
        _credentials_data(
            **{
                CONF_ACCOUNT_PIN: "0000",
                CONF_DEVICE_NAME: "Home Assistant",
                CONF_CLIENT_IDENTIFIER: "CLIENT-ID-UNDER-TEST",
            }
        )
    )

    kwargs = build.call_args.kwargs
    assert kwargs["account_pin"] == "0000"
    assert kwargs["device_name"] == "Home Assistant"
    assert kwargs["client_identifier"] == "CLIENT-ID-UNDER-TEST"


def test_the_mode_defaults_to_credentials_when_the_entry_predates_the_field(
    client: FakeClient,
    build: Any,
) -> None:
    """An entry written before `login_mode` existed still logs in.

    The default is not cosmetic: `LoginMode(None)` raises `ValueError`, which
    would surface as "unknown error" on an entry that has worked for months.
    """
    probe_account({CONF_PRONOTE_URL: URL, "username": "u", "password": "p"})

    assert build.call_args.kwargs["login_mode"] == LoginMode.CREDENTIALS.value


# ---------------------------------------------------------------------------
# The ENT provider, resolved by name
# ---------------------------------------------------------------------------


def test_no_ent_named_means_no_ent_callable(
    client: FakeClient,
    build: Any,
) -> None:
    """`None`, not a no-op function: upstream branches on it being falsy."""
    probe_account(_credentials_data())

    assert build.call_args.kwargs["ent"] is None


def test_a_named_ent_is_resolved_to_the_upstream_function(
    client: FakeClient,
    build: Any,
) -> None:
    """Resolved at call time rather than stored: an entry holds JSON.

    `ac_reunion` is picked for being an ordinary member of `pronotepy.ent` --
    the test is about the resolution mechanism, not about that académie.
    """
    from pronotepy import ent as ent_module

    probe_account(_credentials_data(**{CONF_ENT: "ac_reunion"}))

    assert build.call_args.kwargs["ent"] is ent_module.ac_reunion


def test_an_unknown_ent_refuses_instead_of_degrading_to_a_direct_login(
    client: FakeClient,
    build: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """This test used to assert the opposite, and the reasoning was wrong.

    It read: "degrading to a direct login is the right failure -- it produces
    'wrong credentials', which is recoverable". Both halves are false. What a
    direct login does with ENT credentials is send the *portal's* username and
    password to the PRONOTE server, which is not a fallback but a disclosure to
    the wrong party; and "wrong credentials" is only recoverable if it names
    something the user can act on, whereas here it points at two fields that
    are correct and hides the one that is not.

    It also spends a slot on the three-attempt IP guard to establish something
    that was knowable without asking anybody, since the provider list comes
    from the installed library.

    Upstream does rename providers, which is the case this test was written
    for, and a stored name that stops resolving is when it matters most: the
    runtime login path resolves the same name on every reconnection.
    """
    caplog.set_level(logging.ERROR)

    with pytest.raises(ProbeEntUnknown):
        probe_account(_credentials_data(**{CONF_ENT: "no_such_ent_provider"}))

    assert build.call_count == 0, "nothing may be sent under a name we cannot resolve"
    assert "no_such_ent_provider" in caplog.text


def test_an_ent_name_that_resolves_to_something_uncallable_is_refused(
    client: FakeClient,
    build: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`getattr` on a module finds more than functions.

    `pronotepy.ent.__doc__` resolves; calling it does not. Checking
    `callable` rather than only `is None` is what stops a stored name like
    `typing` -- or any module-level constant upstream adds -- from being handed
    to `HardenedClient` as a login hook.
    """
    caplog.set_level(logging.ERROR)

    with pytest.raises(ProbeEntUnknown):
        probe_account(_credentials_data(**{CONF_ENT: "__doc__"}))

    assert build.call_count == 0
    assert "__doc__" in caplog.text


# ---------------------------------------------------------------------------
# QR enrolment
# ---------------------------------------------------------------------------


def test_qr_enrolment_hands_over_the_payload_the_pin_and_the_uuid(
    client: FakeClient,
) -> None:
    """The three arguments upstream's `qrcode_login` takes positionally.

    Asserted positionally because that is how it is called: a keyword rename
    upstream would be caught by mypy, but a reordering of three `str`
    parameters would not, and it would swap the PIN and the UUID silently.
    """
    with patch(
        "custom_components.carnet_scolaire.hardened_client.HardenedClient.qrcode_login",
        return_value=client,
    ) as qr_login:
        described = probe_account(_qr_data(**{CONF_DEVICE_NAME: "Home Assistant"}))

    payload, pin, device_uuid = qr_login.call_args.args
    assert payload == QR_PAYLOAD
    assert pin == "0000"
    assert device_uuid == "UUID-UNDER-TEST"
    assert qr_login.call_args.kwargs["device_name"] == "Home Assistant"
    assert described["children"] == []


def test_qr_enrolment_copies_the_payload_instead_of_handing_over_the_entry_s(
    client: FakeClient,
) -> None:
    """A copy, so upstream cannot mutate the config entry's own dictionary.

    `qrcode_login` pops keys out of the mapping it is given. Handing it the
    entry's live dictionary would empty the stored payload as a side effect of
    reading it.
    """
    data = _qr_data()

    with patch(
        "custom_components.carnet_scolaire.hardened_client.HardenedClient.qrcode_login",
        return_value=client,
    ) as qr_login:
        probe_account(data)

    handed_over = qr_login.call_args.args[0]
    assert handed_over is not data[CONF_QR_PAYLOAD]
    assert data[CONF_QR_PAYLOAD] == QR_PAYLOAD


def test_qr_enrolment_confines_the_periods_the_login_registered(
    client: FakeClient,
) -> None:
    """`confine(client)` is not decoration.

    Every `Period` a login creates goes into a class-level set on
    `pronotepy.dataClasses` and keeps its client -- and its socket pool --
    alive for the life of the process. The registry attached here is what
    `release_client` uses to take them back out again.
    """
    with patch(
        "custom_components.carnet_scolaire.hardened_client.HardenedClient.qrcode_login",
        return_value=client,
    ):
        probe_account(_qr_data())

    # Attached during the login, then emptied by `release_client` in the
    # `finally`: what matters is that the client carried one at all.
    assert hasattr(client, "period_registry")


def test_a_qr_account_without_its_payload_logs_in_through_the_token_mode(
    client: FakeClient,
    build: Any,
) -> None:
    """The crash a real parent hit, at the level where it happened.

    §8.1 refuses to persist the single-use QR payload, so a QR-enrolled entry
    carries ``login_mode: qr_code`` for the rest of its life with nothing
    beside it. Every login after the enrolment one therefore arrives here with
    the mode and no payload -- and the branch tested the mode alone, so
    ``_qr_login`` reached for ``data[CONF_QR_PAYLOAD]`` and raised
    ``KeyError: 'qr_payload'``. Unclassified by any of the six arms, it escaped
    as an unexpected error and the re-authentication form said "unexpected
    error, check the log" to the one person trying to repair the account.

    What must happen instead is what the running integration already does with
    the very same entry: hand the mode to ``build_client``, which maps
    ``qr_code`` to ``pronotepy``'s token mode and logs in with the stored
    rotated token.
    """
    described = probe_account(
        {
            CONF_LOGIN_MODE: LoginMode.QR_CODE.value,
            CONF_PRONOTE_URL: URL,
            "username": "not-a-real-login",
            "password": "not-a-real-rotated-token",
            CONF_UUID: "UUID-UNDER-TEST",
            CONF_CLIENT_IDENTIFIER: "CLIENT-ID-UNDER-TEST",
        }
    )

    assert build.call_count == 1
    # The mode is passed through unchanged: `build_client` owns the mapping to
    # `token`, and it is asserted there rather than duplicated here.
    assert build.call_args.kwargs["login_mode"] == LoginMode.QR_CODE.value
    assert build.call_args.kwargs["password"] == "not-a-real-rotated-token"
    assert build.call_args.kwargs["uuid"] == "UUID-UNDER-TEST"
    assert build.call_args.kwargs["client_identifier"] == "CLIENT-ID-UNDER-TEST"
    assert described["account_id"] == f"{URL}::{client.info.id}"


def test_a_qr_account_whose_payload_is_present_still_enrols(
    client: FakeClient,
    build: Any,
) -> None:
    """The other half of the same branch, so neither can be lost alone.

    The two paths are one decision, and a fix that routed *everything* through
    the token mode would have broken enrolment instead -- silently, because a
    token login with a payload's contents fails at the server rather than in
    Python.
    """
    with patch(
        "custom_components.carnet_scolaire.hardened_client.HardenedClient.qrcode_login",
        return_value=client,
    ) as qr_login:
        probe_account(_qr_data())

    assert qr_login.call_count == 1
    assert build.call_count == 0


def test_an_empty_payload_is_treated_as_an_absent_one(
    client: FakeClient,
    build: Any,
) -> None:
    """``{}`` cannot enrol anything, so it takes the token path too.

    ``qrcode_login`` pops ``jeton``, ``login`` and ``url`` out of the mapping;
    handed an empty one it raises ``KeyError`` again, in a different place. The
    truth condition is "is there a payload to enrol with", not "is the key
    present".
    """
    probe_account(
        {
            CONF_LOGIN_MODE: LoginMode.QR_CODE.value,
            CONF_PRONOTE_URL: URL,
            CONF_QR_PAYLOAD: {},
            "password": "not-a-real-rotated-token",
        }
    )

    assert build.call_count == 1


# ---------------------------------------------------------------------------
# Closing the client, every way out
# ---------------------------------------------------------------------------


def test_a_successful_probe_still_closes_its_client(
    client: FakeClient,
    build: Any,
) -> None:
    """Two live sessions on one account desynchronise the request counter.

    The probe's session is *not* reused: the entry opens its own a moment
    later. Leaving this one open is what makes the next request fail with
    `Erreur.G = 10` for no reason the user can see (annexe B §1).
    """
    probe_account(_credentials_data())

    assert client.closed is True


def test_a_login_that_never_produced_a_client_closes_nothing(
    client: FakeClient,
) -> None:
    """`release_client(None)` is reached on the most common failure of all.

    When `build_client` itself raises -- a wrong password, which is the common
    path through this function -- there is no client to close, and the `finally`
    runs anyway. Returning early on `None` rather than raising inside a
    `finally` is what stops one classified failure being replaced by an
    unclassified `AttributeError`.
    """
    with (
        patch(
            "custom_components.carnet_scolaire.hardened_client.build_client",
            side_effect=CryptoError("challenge did not decrypt"),
        ),
        pytest.raises(ProbeInvalidCredentials),
    ):
        probe_account(_credentials_data())

    # `build_client` raised, so there was never a client to close; the fixture's
    # own client must therefore be untouched rather than closed by accident.
    assert client.closed is False


def test_a_login_that_returns_without_opening_a_session_is_a_wrong_password(
    client: FakeClient,
    build: Any,
) -> None:
    """`logged_in = False` with no exception at all.

    `_login` returns `False` when the `cle` key is absent from the
    `Authentification` response -- which is what a wrong password looks like
    when the challenge happens to decrypt anyway. Classified as anything other
    than bad credentials, the failed-login guard would never fire on the one
    path it exists for (annexe B §3.1).
    """
    client.logged_in = False

    with pytest.raises(ProbeInvalidCredentials):
        probe_account(_credentials_data())

    assert client.closed is True


# ---------------------------------------------------------------------------
# The classification, which is what this module is for
# ---------------------------------------------------------------------------

#: Every failure upstream can raise, and the reason the form must show for it.
CLASSIFICATION: tuple[tuple[BaseException, type[ProbeError]], ...] = (
    (MFAError("a security code is required"), ProbeMfaRequired),
    (QRCodeDecryptError("the payload did not decrypt"), ProbeQrInvalid),
    (CryptoError("the challenge did not decrypt"), ProbeInvalidCredentials),
    (ENTLoginError("the identity provider refused"), ProbeInvalidCredentials),
    (BootstrapDelegated("the portal's login form was served"), ProbeEntRequired),
    (BootstrapUnavailable("the session page was not served"), ProbeBootstrapFailed),
    (PronoteAPIError("the server answered something else"), ProbeBootstrapFailed),
)


@pytest.mark.parametrize(
    ("raised", "expected"),
    CLASSIFICATION,
    ids=[type(raised).__name__ for raised, _ in CLASSIFICATION],
)
def test_every_upstream_failure_maps_to_a_reason_the_form_can_show(
    client: FakeClient,
    raised: BaseException,
    expected: type[ProbeError],
) -> None:
    """No upstream exception may reach Home Assistant unclassified.

    An unhandled one becomes "unknown error occurred" with no field
    highlighted, which is the single least actionable thing a login form can
    say -- and the user's response to it is to try again, immediately, which is
    what the address-level sanction punishes.
    """
    with (
        patch(
            "custom_components.carnet_scolaire.hardened_client.build_client",
            side_effect=raised,
        ),
        pytest.raises(expected) as caught,
    ):
        probe_account(_credentials_data())

    # Chained rather than swallowed: the original is what a diagnostic report
    # needs, and `raise ... from error` is what puts it there.
    assert caught.value.__cause__ is raised


@pytest.mark.parametrize(
    ("subclass", "parent"),
    [
        (MFAError, PronoteAPIError),
        (ENTLoginError, PronoteAPIError),
        (BootstrapUnavailable, PronoteAPIError),
        (BootstrapDelegated, BootstrapUnavailable),
        (CryptoError, PronoteAPIError),
        (QRCodeDecryptError, CryptoError),
    ],
)
def test_the_order_of_the_arms_is_load_bearing(
    subclass: type[BaseException], parent: type[BaseException]
) -> None:
    """Five of the six arms would still catch if they were moved last.

    This is not a test of upstream's type hierarchy for its own sake. It is the
    statement of *why* `probe_account`'s `except` clauses cannot be sorted,
    alphabetised or merged: each specific arm sits above a class that would
    otherwise swallow it, and the resulting mistake is invisible to every other
    gate this project has -- ruff, mypy and a smoke test all pass on the
    reordered version.

    If this assertion ever fails because upstream flattened the hierarchy, the
    right response is to check `probe_account` still classifies correctly, not
    to delete this test.
    """
    assert issubclass(subclass, parent)


def test_the_portal_login_form_in_ent_mode_does_not_send_the_parent_to_ent_mode(
    client: FakeClient,
) -> None:
    """ "Choose the ENT login" is advice for a direct login only.

    In ENT mode the same page means the portal's cookies did not open PRONOTE.
    Telling that parent to pick the mode they are already in would be a loop,
    so it stays the generic bootstrap failure.
    """
    with (
        patch(
            "custom_components.carnet_scolaire.hardened_client.build_client",
            side_effect=BootstrapDelegated("the portal's login form was served"),
        ),
        pytest.raises(ProbeBootstrapFailed) as caught,
    ):
        probe_account(
            _credentials_data(
                **{CONF_LOGIN_MODE: LoginMode.ENT.value, CONF_ENT: "ac_reunion"}
            )
        )

    assert not isinstance(caught.value, ProbeEntRequired)


def test_a_qr_code_that_will_not_decrypt_does_not_blame_the_password(
    client: FakeClient,
) -> None:
    """The most consequential instance of the ordering above.

    `QRCodeDecryptError` is a `CryptoError`. Caught by the `CryptoError` arm,
    a mistyped four-digit PIN would tell the user their *password* is wrong --
    sending them to change a credential that is not involved, on a form that
    is not the one they were on, while the QR code they must re-generate
    expires.
    """
    with (
        patch(
            "custom_components.carnet_scolaire.hardened_client.HardenedClient.qrcode_login",
            side_effect=QRCodeDecryptError("wrong pin"),
        ),
        pytest.raises(ProbeQrInvalid),
    ):
        probe_account(_qr_data())


def test_a_challenge_refused_while_enrolling_blames_the_qr_code_not_the_password(
    client: FakeClient,
) -> None:
    """The same arm, the other side of its one branch.

    A bare `CryptoError` -- not the `QRCodeDecryptError` above -- means the
    payload decrypted locally and the *server* then refused the challenge. On
    the credentials path that is a wrong password, and the test two functions
    up asserts exactly that. On the enrolment path it cannot be: there is no
    password. `qrcode_login` agrees the challenge from the payload's own
    `login` and `jeton`, and the account's two-factor PIN never enters the key
    -- `ClientBase._login` derives it as `username + SHA256(alea + password)`
    and uses the PIN only in `_do_2fa`, after a challenge that has already
    succeeded.

    Reported as `ProbeQrInvalid` this would send the user to re-type a
    four-digit code that decrypted correctly; reported as
    `ProbeInvalidCredentials` -- which is what it was -- it sends a parent to
    look at the one field that could not possibly matter, while the QR code
    they actually need to re-generate expires. Upstream compounds it by
    appending "probably the qr code has expired" to its own message on nothing
    more than `login_mode == "qr_code"`.

    This is the `enrolling` branch of the `CryptoError` arm, and it is the only
    thing that distinguishes the two outcomes.
    """
    raised = CryptoError("the server refused the challenge")

    with (
        patch(
            "custom_components.carnet_scolaire.hardened_client.HardenedClient.qrcode_login",
            side_effect=raised,
        ),
        pytest.raises(ProbeQrRefused) as caught,
    ):
        probe_account(_qr_data())

    # Chained, like every other arm: the original is what a diagnostic needs.
    assert caught.value.__cause__ is raised


def test_a_qr_entry_without_its_payload_is_not_enrolling_and_blames_the_password(
    client: FakeClient,
) -> None:
    """The branch is on the *payload*, not on the mode, and that matters.

    A QR-enrolled entry keeps `login_mode: qr_code` for the rest of its life,
    long after the payload was dropped -- so every later login, the
    re-authentication probe included, runs through pronotepy's token mode with
    no enrolment in sight. Branching on the mode alone would classify a
    genuinely rotten token as "the QR code was refused", offering to re-scan a
    code when the fix is to re-authenticate.

    Paired deliberately with the test above: same exception, same login mode,
    opposite classification, and the payload is the only difference.
    """
    with (
        patch(
            "custom_components.carnet_scolaire.hardened_client.build_client",
            side_effect=CryptoError("the server refused the challenge"),
        ),
        pytest.raises(ProbeInvalidCredentials),
    ):
        probe_account(
            {
                CONF_LOGIN_MODE: LoginMode.QR_CODE.value,
                CONF_PRONOTE_URL: URL,
                "username": "not-a-real-login",
                "password": "not-a-real-rotated-token",
                CONF_UUID: "UUID-UNDER-TEST",
            }
        )


def test_a_school_whose_server_is_down_does_not_blame_the_password_either(
    client: FakeClient,
) -> None:
    """`BootstrapUnavailable` is a `PronoteAPIError`, and both mean "not you".

    Kept separate from the generic arm because the distinction is what the
    repair flow branches on: "the establishment's server did not answer" is
    waited out, "your credentials were refused" is not.
    """
    with (
        patch(
            "custom_components.carnet_scolaire.hardened_client.build_client",
            side_effect=BootstrapUnavailable("no session page"),
        ),
        pytest.raises(ProbeBootstrapFailed),
    ):
        probe_account(_credentials_data())


def test_nothing_is_retried_inside_the_probe(
    client: FakeClient,
) -> None:
    """One attempt per user gesture, and the count is the assertion.

    Retrying a wrong password is precisely what gets an address suspended, and
    the sanction is undocumented and slow to lift (annexe B §3.1). The guard in
    `login_guard` counts *gestures*; it cannot see a loop hidden in here.
    """
    with (
        patch(
            "custom_components.carnet_scolaire.hardened_client.build_client",
            side_effect=CryptoError("nope"),
        ) as build,
        pytest.raises(ProbeInvalidCredentials),
    ):
        probe_account(_credentials_data())

    assert build.call_count == 1
