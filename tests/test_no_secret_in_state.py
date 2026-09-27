"""The one test §8.2 asks for by name: run a full cycle, then hunt the secrets.

The design keeps secrets out of the state machine by construction -- the iCal
URL, the identity block and the PDF link are ``SupportsResponse.ONLY`` service
results, and nothing stores them. But "by construction" is an argument, and
arguments do not survive refactors. This module is the thing that does: it sets
the integration up with known sentinel secrets, runs a real collection cycle,
and then sweeps everything a user could hand to somebody else.

Four surfaces, because they leak differently:

* **states and attributes** -- the recorder writes them to a database that goes
  into every backup, and they are visible in screenshots;
* **the diagnostics download** -- the file users are explicitly asked to attach
  to public issues;
* **service responses** -- the one place a secret is *allowed*, so what is
  checked here is that it appears in exactly that one place and nowhere else;
* **the log** -- captured at ``DEBUG``, which is what a user turns on before
  reporting a bug.

The sentinels are deliberately implausible strings rather than realistic ones.
A test that searched for a realistic-looking token would tempt somebody to
paste a real one in, and this file is committed to a public repository.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components.diagnostics import REDACTED
from pronotepy.exceptions import ParsingError
import pytest

from custom_components.carnet_scolaire.const import (
    CONF_ACCOUNT_PIN,
    CONF_CLIENT_IDENTIFIER,
    CONF_PRONOTE_URL,
    CONF_UUID,
    DOMAIN,
    SERVICE_GET_ICAL_URL,
    SERVICE_GET_RATE_LIMIT_STATUS,
    Tier,
)
from custom_components.carnet_scolaire.diagnostics import (
    async_get_config_entry_diagnostics,
)

from .conftest import CHILDREN, REQUIRES_HASS, child_key

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.carnet_scolaire.account import PronoteAccount

    from .fixtures.client import FakeClient

pytestmark = REQUIRES_HASS

STUDENT_ONE = CHILDREN[0][0]

#: Sentinels planted in the config entry. Every one is a value that must never
#: appear in anything a user can share.
SECRETS: dict[str, str] = {
    "password": "SENTINEL-PASSWORD-DO-NOT-LEAK",
    CONF_UUID: "SENTINEL-UUID-DO-NOT-LEAK",
    CONF_CLIENT_IDENTIFIER: "SENTINEL-CLIENT-ID-DO-NOT-LEAK",
    CONF_ACCOUNT_PIN: "SENTINEL-PIN-DO-NOT-LEAK",
}

#: The ticket a school's deep link carries. Trimmed at the config-flow boundary,
#: but an entry restored from a backup can still hold it, so the diagnostics
#: must trim it again.
TICKET = "SENTINEL-TICKET-DO-NOT-LEAK"

#: Written to the log by the test itself. On a healthy cycle this integration
#: logs nothing at all, so without a canary an empty capture would make every
#: "no secret in the log" assertion below pass vacuously.
CANARY = "canary-proving-the-log-capture-is-live"


@pytest.fixture(name="entry_data")
def entry_data_with_sentinels() -> dict[str, Any]:
    """Override the shared fixture so the entry carries the sentinels.

    Including a PIN, which §8.1 says is never persisted: if this test ever finds
    one in a diagnostic it means either the redaction or the flow regressed, and
    the belt-and-braces entry in ``TO_REDACT`` exists for exactly that day.
    """
    return {
        CONF_PRONOTE_URL: (
            f"https://demo.example.invalid/pronote/parent.html?identifiant={TICKET}"
        ),
        "login_mode": "credentials",
        "username": "parent-under-test",
        **SECRETS,
    }


def _all_state_text(hass: HomeAssistant) -> str:
    """Every state and every attribute of every entity, as one string."""
    return json.dumps(
        [
            {
                "entity_id": state.entity_id,
                "state": state.state,
                "attributes": dict(state.attributes),
            }
            for state in hass.states.async_all()
        ],
        default=str,
    )


async def test_no_secret_reaches_a_state_or_an_attribute(
    hass: HomeAssistant, account: PronoteAccount
) -> None:
    """The recorder writes these to a database that goes into every backup."""
    payload = _all_state_text(hass)
    assert payload != "[]"

    for name, secret in SECRETS.items():
        assert secret not in payload, f"{name} leaked into a state or attribute"
    assert TICKET not in payload


async def test_no_secret_reaches_the_diagnostics_download(
    hass: HomeAssistant, mock_entry: MockConfigEntry, account: PronoteAccount
) -> None:
    """The file users are asked to attach to a public issue.

    The credential fields are redacted, the address is re-trimmed, and the child
    identifiers are fingerprinted rather than removed -- a fingerprint still
    answers "do both children show the same tier failing?" without handing
    anybody the material to replay a session (annexe A §7).
    """
    diagnostics = await async_get_config_entry_diagnostics(hass, mock_entry)
    payload = json.dumps(diagnostics, default=str)

    for name, secret in SECRETS.items():
        assert secret not in payload, f"{name} leaked into the diagnostics"
    assert TICKET not in payload

    data = diagnostics["entry"]["data"]
    assert data["password"] == REDACTED
    assert data[CONF_UUID] == REDACTED
    assert data[CONF_PRONOTE_URL] == (
        "https://demo.example.invalid/pronote/parent.html"
    )
    assert diagnostics["entry"]["url_host"] == "demo.example.invalid"

    # And the student identifiers appear only as fingerprints.
    assert STUDENT_ONE not in payload
    assert diagnostics["account"]["students"]
    for student in diagnostics["account"]["students"]:
        assert student["id_hash"] != STUDENT_ONE
        assert len(student["id_hash"]) == 8


async def test_the_budget_service_response_carries_no_secret(
    hass: HomeAssistant, mock_entry: MockConfigEntry, account: PronoteAccount
) -> None:
    """This is the payload users are encouraged to paste into an issue.

    ``ratelimit.py`` says in a comment that a test keeps secrets out of service
    responses. This is that test; before it existed, the claim rested on the
    fact that the mapping *happened* to hold only scalars.
    """
    from homeassistant.helpers import device_registry as dr

    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, mock_entry.entry_id), mock_entry.entry_id
    )
    assert device is not None

    response = await hass.services.async_call(
        DOMAIN,
        SERVICE_GET_RATE_LIMIT_STATUS,
        {"device_id": device.id},
        blocking=True,
        return_response=True,
    )

    payload = json.dumps(response, default=str)
    for secret in (*SECRETS.values(), TICKET):
        assert secret not in payload


async def test_the_ical_url_exists_only_in_its_service_response(
    hass: HomeAssistant, mock_entry: MockConfigEntry, account: PronoteAccount
) -> None:
    """The positive half of §8.2, which the negative half depends on.

    A URL that never appeared anywhere would satisfy every "no secret in a
    state" assertion trivially. What the design actually promises is narrower
    and more useful: the service *does* hand it back, once, and it is nowhere
    else afterwards -- not in a state, not in an attribute, not in the
    diagnostics.
    """
    from homeassistant.helpers import device_registry as dr

    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, f"{mock_entry.entry_id}_{child_key(mock_entry, STUDENT_ONE)}"),
        mock_entry.entry_id,
    )
    assert device is not None

    response = await hass.services.async_call(
        DOMAIN,
        SERVICE_GET_ICAL_URL,
        {"device_id": device.id},
        blocking=True,
        return_response=True,
    )
    assert response is not None
    url = response["url"]
    assert url

    assert url not in _all_state_text(hass)
    diagnostics = await async_get_config_entry_diagnostics(hass, mock_entry)
    assert url not in json.dumps(diagnostics, default=str)


async def test_nothing_secret_is_logged_even_at_debug(
    hass: HomeAssistant,
    account: PronoteAccount,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """DEBUG is what a user turns on *before* reporting a bug.

    Which is why the ``pronotepy`` logger is the one thing this integration must
    never enable: ``pronoteAPI.py`` logs the hexadecimal of every request body
    at DEBUG, credentials included. The integration owns no logger
    configuration and declares no ``loggers`` key in its manifest -- a
    ``scripts/check_manifest.py`` check enforces the second half, and this test
    covers the first by running a full cycle with everything captured.
    """
    caplog.set_level(logging.DEBUG)
    logging.getLogger(f"{__name__}.canary").debug(CANARY)
    await account.async_request_tick()
    await hass.async_block_till_done()

    # Setup is swept as well as the call, deliberately: setup is the phase
    # that reads the config entry, builds the client and logs in, so it is
    # the phase where a credential would surface if anything logged one. The
    # call phase, on a healthy run, is expected to be quiet -- which is why
    # the canary is here rather than an assertion that something was logged.
    haystacks = [
        caplog.text,
        *(record.getMessage() for record in caplog.get_records("setup")),
        *(record.getMessage() for record in caplog.get_records("call")),
    ]

    assert any(CANARY in hay for hay in haystacks), "log capture is not wired up"
    for name, secret in SECRETS.items():
        assert not any(secret in hay for hay in haystacks), (
            f"{name} was written to the log"
        )
    assert not any(TICKET in hay for hay in haystacks)


async def test_a_payload_that_cannot_be_decoded_is_not_logged_even_at_debug(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    account: PronoteAccount,
    parent_client: FakeClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The one path where the data itself, not a credential, reached the log.

    The text of a decoding error is a fragment of what it could not decode:
    ``pronotepy`` writes ``Error while converting value: <the converter's
    error>``, which quotes the value, and a ``KeyError`` names what was looked
    up. It went into a DEBUG traceback on this integration's logger -- the
    one a user is told to enable. Only that logger is read here: the
    ``pronotepy`` ones are never enabled, and this test does not either.
    """
    caplog.set_level(logging.DEBUG, logger="custom_components.carnet_scolaire")
    name = "Enfant Un"
    session_n = "46#NOT-A-REAL-N"
    text = f"invalid literal for int() with base 10: '{name}' ({session_n})"
    failures: list[Exception] = [
        ParsingError(f"Error while converting value: {text}", {"L": name}, ("V",)),
        KeyError(session_n),
        ValueError(text),
    ]

    def _undecodable(_body: Any) -> dict[str, Any]:
        raise failures.pop(0)

    parent_client.responses["PageCahierDeTexte"] = _undecodable
    for _attempt in range(3):
        await account._async_collect(Tier.HOMEWORK)
    await hass.async_block_till_done()

    ours = [
        caplog.handler.format(record)
        for record in caplog.records
        if record.name.startswith("custom_components.carnet_scolaire")
    ]
    assert not failures, "the three failures were not all raised"
    assert any("(decode" in line for line in ours), "log capture is not wired up"
    diagnostics = json.dumps(
        await async_get_config_entry_diagnostics(hass, mock_entry), default=str
    )
    for hay in (*ours, diagnostics):
        assert name not in hay
        assert session_n not in hay
        assert "Traceback" not in hay
