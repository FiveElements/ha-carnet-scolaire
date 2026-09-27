"""Adding the account, and the two things that must not happen while doing it.

**No secret is persisted that §8.1 promised not to keep.** The 2FA PIN, the QR
payload and the QR code's four-digit PIN are single-use: the flow needs them for
one login and the entry must not carry them afterwards. "We do not store your
PIN" is only true if the entry really does not have it, so that is asserted on
the stored data rather than trusted to the code that builds it.

**No login escapes the limiter.** This is the one login path a *person* can
repeat at will -- the re-authentication form re-displays itself on every
failure -- and it went straight to ``build_client``: no counter, no guard. The
three-attempt rail exists because PRONOTE's one sanction applies to an address
rather than to an account (annexe B §3.1), and a parent convinced their password
is right could walk straight past it. ``test_the_flow_stops_asking_after_three``
is the test that now holds that line.

Everything is driven through ``hass.config_entries.flow``, not by calling the
steps directly: the flow's contract is what Home Assistant does with it -- the
menu, the unique id, the abort, the reload -- and half of that lives in the
framework rather than in the handler.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Iterator  # noqa: TC003 -- a pytest fixture annotation
import json
import logging
import pathlib
from string import Formatter
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

from homeassistant.config_entries import SOURCE_USER
from homeassistant.data_entry_flow import FlowResultType
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
import requests

from custom_components.carnet_scolaire.account import PronoteAccount
from custom_components.carnet_scolaire.config_flow import (
    ProbeBootstrapFailed,
    ProbeEntRequired,
    ProbeEntUnknown,
    ProbeInvalidCredentials,
    ProbeMfaRequired,
    ProbeQrInvalid,
    ProbeQrRefused,
)
from custom_components.carnet_scolaire.connectors.ecoledirecte.ed_limiter import (
    EdRateLimiter,
)
from custom_components.carnet_scolaire.connectors.errors import (
    ConnectorChallengeRequired,
    ConnectorCredentialsError,
)
from custom_components.carnet_scolaire.connectors.protocol import ChallengeKind
from custom_components.carnet_scolaire.const import (
    CONF_ACCOUNT_PIN,
    CONF_CHILD_KEYS,
    CONF_CHILDREN,
    CONF_CLIENT_IDENTIFIER,
    CONF_ENT,
    CONF_LOGIN_MODE,
    CONF_PRONOTE_URL,
    CONF_QR_PAYLOAD,
    CONF_QR_PIN,
    CONF_SOURCE,
    CONF_UUID,
    DOMAIN,
    OPT_WRITE_OPERATIONS_ENABLED,
    LoginMode,
)
from custom_components.carnet_scolaire.login_guard import (
    limiter_state_store,
    login_guard,
)
from custom_components.carnet_scolaire.options import build_rate_limit_config
from custom_components.carnet_scolaire.ratelimit import (
    REQUESTS_PER_LOGIN,
    DeferReason,
    LoginOutcome,
    LoginRefusedByLimiter,
)
from custom_components.carnet_scolaire.urls import public_url, url_host
from tests.test_ecoledirecte_client import RecordingTransport, load_fixture

from .conftest import REQUIRES_HASS

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

pytestmark = REQUIRES_HASS

#: A deep link of the shape establishments really mail out. The query string is
#: the point: it is what must not survive into the config entry.
PASTED_URL = (
    "https://demo.example.invalid/pronote/parent.html"
    "?login=true&identifiant=NOT-A-REAL-TICKET"
)
TRIMMED_URL = "https://demo.example.invalid/pronote/parent.html"

QR_PAYLOAD = {
    # Hexadecimal, and that is not cosmetic: `qrcode_login` runs
    # `bytes.fromhex` on both of these before it does anything else
    # (pronotepy 2.15.7, `clients.py:184-185`), so a payload that is not
    # hex is rejected by `_parse_qr_payload` and never reaches the probe.
    # A fixture written in prose was therefore testing a shape the code
    # now refuses. Visibly fictional all the same, per CONTRIBUTING §1.1.
    "jeton": "0bad0bad0bad0bad0bad0bad0bad0bad",
    "login": "0badc0de0badc0de",
    "url": PASTED_URL,
}


@pytest.fixture(name="no_setup", autouse=True)
def no_setup_fixture() -> Iterator[None]:
    """Stop a created entry from setting itself up for real.

    A flow that reaches `async_create_entry` makes Home Assistant set the entry
    up, which builds a `PronoteAccount`, a worker thread and a real login
    attempt -- none of which is what these tests are about, and the thread is
    left behind because the flow never tears it down. Autouse, because the
    failure it prevents shows up as a lingering-thread error in the *next*
    test's teardown, which is a genuinely confusing place to debug from.
    """
    with patch(
        "custom_components.carnet_scolaire.async_setup_entry", return_value=True
    ):
        yield


def _outcome(*children: tuple[str, str]) -> dict[str, Any]:
    """What ``probe_account`` hands back on success."""
    return {
        "account_id": "demo.example.invalid|parent-under-test",
        "children": list(children),
        "title": "PRONOTE",
        "username": "parent-under-test",
        "password": "not-a-real-password",
    }


async def _start(hass: HomeAssistant) -> str:
    """Open the flow and step through the menu to the credentials form."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.MENU
    assert set(result["menu_options"]) == {"pronote", "ecoledirecte"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "pronote"}
    )
    assert result["type"] is FlowResultType.MENU
    assert set(result["menu_options"]) == {"qr_code", "credentials", "ent"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "credentials"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "credentials"
    return str(result["flow_id"])


async def _submit_credentials(
    hass: HomeAssistant,
    flow_id: str,
    *,
    url: str = PASTED_URL,
    pin: str | None = None,
) -> dict[str, Any]:
    """Fill in the credentials form."""
    payload: dict[str, Any] = {
        CONF_PRONOTE_URL: url,
        "username": "parent-under-test",
        "password": "not-a-real-password",
    }
    if pin is not None:
        payload[CONF_ACCOUNT_PIN] = pin
    return dict(await hass.config_entries.flow.async_configure(flow_id, payload))


# ---------------------------------------------------------------------------
# The happy paths
# ---------------------------------------------------------------------------


async def test_an_ecoledirecte_login_stores_password_and_not_the_token(
    hass: HomeAssistant, no_spacing: None
) -> None:
    """Aplim has no durable device token; the password is the only replayable secret."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "ecoledirecte"}
    )
    assert result["type"] is FlowResultType.FORM

    with patch(
        "custom_components.carnet_scolaire.config_flow._probe_ecoledirecte",
        return_value={"students": (("1", "Enfant Un"),)},
    ):
        created = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"username": "demo.example.invalid", "password": "not-a-real-password"},
        )

    assert created["type"] is FlowResultType.CREATE_ENTRY
    assert created["data"]["password"] == "not-a-real-password"
    assert "token" not in created["data"]
    assert created["data"][CONF_SOURCE] == "ecoledirecte"
    # The connector offers no write, so there is nothing to switch on.
    assert OPT_WRITE_OPERATIONS_ENABLED not in created["options"]


async def test_a_250_opens_the_qcm_step_and_does_not_create_the_entry(
    hass: HomeAssistant, no_spacing: None
) -> None:
    """An entry that exists without a 200 login would be half-born."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "ecoledirecte"}
    )
    with patch(
        "custom_components.carnet_scolaire.config_flow._probe_ecoledirecte",
        side_effect=ConnectorChallengeRequired(
            ChallengeKind.QCM,
            question="Couleur préférée ?",
            propositions=("Bleu", "Vert"),
        ),
    ):
        challenged = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"username": "demo.example.invalid", "password": "not-a-real-password"},
        )
    assert challenged["type"] is FlowResultType.FORM
    assert challenged["step_id"] == "ecoledirecte_qcm"
    assert hass.config_entries.async_entries(DOMAIN) == []


async def test_an_ecoledirecte_qcm_submit_creates_the_entry_after_200(
    hass: HomeAssistant, no_spacing: None
) -> None:
    """A remembered answer is only persisted once the login itself is 200."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "ecoledirecte"}
    )
    with patch(
        "custom_components.carnet_scolaire.config_flow._probe_ecoledirecte",
        side_effect=ConnectorChallengeRequired(
            ChallengeKind.QCM,
            question="Couleur préférée ?",
            propositions=("Bleu", "Vert"),
        ),
    ):
        challenged = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"username": "demo.example.invalid", "password": "not-a-real-password"},
        )
    assert challenged["step_id"] == "ecoledirecte_qcm"

    answers = {"Couleur préférée ?": "Bleu"}
    schema_keys = {
        getattr(key, "schema", key) for key in challenged["data_schema"].schema
    }
    assert "choice" in schema_keys
    assert "qcm_json" not in schema_keys
    with patch(
        "custom_components.carnet_scolaire.config_flow._probe_ecoledirecte",
        return_value={"students": (("1", "Enfant Un"),)},
    ) as probe:
        created = await hass.config_entries.flow.async_configure(
            challenged["flow_id"],
            {"choice": "Bleu"},
        )

    assert created["type"] is FlowResultType.CREATE_ENTRY
    assert created["data"]["qcm_json"] == answers
    assert probe.call_args.args[3] == answers


async def test_an_ecoledirecte_qcm_submit_after_250_is_not_held_by_the_limiter(
    hass: HomeAssistant, no_spacing: None
) -> None:
    """A 250 is a challenge, not a 505: answers must reach login on the same guard."""
    question = "Couleur préférée ?"
    answer = "Bleu"
    encoded_question = base64.b64encode(question.encode()).decode()
    encoded_answer = base64.b64encode(answer.encode()).decode()
    quiz = {
        "code": 200,
        "data": {
            "question": encoded_question,
            "propositions": [encoded_answer, base64.b64encode(b"Vert").decode()],
        },
    }
    factors = {
        "code": 200,
        "data": {"cn": "not-a-real-cn", "cv": "not-a-real-cv"},
    }
    transport = RecordingTransport.scripted(
        [
            ("GET", "login.awp", {"gtk": "gtk-demo"}),
            ("POST", "login.awp", load_fixture("login_250.json")),
            ("POST", "doubleauth.awp", quiz),
            ("GET", "login.awp", {"gtk": "gtk-retry"}),
            ("POST", "login.awp", load_fixture("login_250.json")),
            ("POST", "doubleauth.awp", quiz),
            ("POST", "doubleauth.awp", factors),
            ("GET", "login.awp", {"gtk": "gtk-after-qcm"}),
            ("POST", "login.awp", load_fixture("login_ok.json")),
        ]
    )

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "ecoledirecte"}
    )
    with (
        patch(
            "custom_components.carnet_scolaire.config_flow.async_create_clientsession",
            return_value=transport,
        ),
        patch.object(PronoteAccount, "async_setup", return_value=None),
        patch.object(PronoteAccount, "async_start_first_collection"),
        patch.object(
            hass.config_entries, "async_forward_entry_setups", return_value=None
        ),
    ):
        challenged = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"username": "demo.example.invalid", "password": "not-a-real-password"},
        )
        assert challenged["type"] is FlowResultType.FORM
        assert challenged["step_id"] == "ecoledirecte_qcm"
        placeholders = challenged.get("description_placeholders") or {}
        assert placeholders.get("question") == question
        assert answer in str(placeholders.get("propositions"))

        created = await hass.config_entries.flow.async_configure(
            challenged["flow_id"],
            {"choice": answer},
        )
        await hass.async_block_till_done()

    assert created["type"] is FlowResultType.CREATE_ENTRY
    assert created["data"]["qcm_json"] == {question: answer}


async def test_an_ecoledirecte_entry_persists_exactly_the_contract_keys(
    hass: HomeAssistant, no_spacing: None
) -> None:
    """Title belongs to Home Assistant; children are recorded only as minted keys."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "ecoledirecte"}
    )
    with patch(
        "custom_components.carnet_scolaire.config_flow._probe_ecoledirecte",
        return_value={"students": (("1", "Enfant Un"),)},
    ):
        created = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"username": "demo.example.invalid", "password": "not-a-real-password"},
        )

    assert created["type"] is FlowResultType.CREATE_ENTRY
    assert created["title"]
    assert set(created["data"]) == {
        CONF_SOURCE,
        "username",
        "password",
        "qcm_json",
        CONF_CHILD_KEYS,
    }
    assert created["data"][CONF_CHILD_KEYS]


async def test_an_ecoledirecte_limiter_refusal_is_rate_limited(
    hass: HomeAssistant, no_spacing: None
) -> None:
    """A refused admission is a budget decision, not an unexpected stack trace."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "ecoledirecte"}
    )
    with patch(
        "custom_components.carnet_scolaire.config_flow._probe_ecoledirecte",
        side_effect=LoginRefusedByLimiter(DeferReason.LOGIN_CAP, 60),
    ):
        refused = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"username": "demo.example.invalid", "password": "not-a-real-password"},
        )

    assert refused["type"] is FlowResultType.FORM
    assert refused["errors"] == {"base": "rate_limited"}
    assert hass.config_entries.async_entries(DOMAIN) == []


async def test_an_ed_reauth_asks_for_identifier_and_qcm_not_a_pronote_probe(
    hass: HomeAssistant, no_spacing: None
) -> None:
    """Enrolment-style repair: identifier + QCM, and this entry's ED hold is cleared."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="ecoledirecte:demo.example.invalid",
        data={
            CONF_SOURCE: "ecoledirecte",
            "username": "demo.example.invalid",
            "password": "old-password",
            "qcm_json": {},
            CONF_CHILD_KEYS: [
                {
                    "key": "child-1",
                    "resource_id": "1",
                    "name": "Enfant Un",
                }
            ],
        },
    )
    entry.add_to_hass(hass)
    held = EdRateLimiter(build_rate_limit_config({}))
    held.note_bad_credentials()
    held.note_bad_credentials()
    held.note_bad_credentials()
    limiter_state_store(hass)[entry.entry_id] = held.export_state()

    result = await entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "ecoledirecte"
    assert entry.entry_id not in limiter_state_store(hass)

    with (
        patch(
            "custom_components.carnet_scolaire.config_flow._probe",
            side_effect=AssertionError("Pronote probe must not run for ED reauth"),
        ),
        patch(
            "custom_components.carnet_scolaire.config_flow._probe_ecoledirecte",
            return_value={"students": (("1", "Enfant Un"),)},
        ),
    ):
        done = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"username": "demo.example.invalid", "password": "not-a-real-password"},
        )

    assert done["type"] is FlowResultType.ABORT
    assert done["reason"] == "reauth_successful"
    assert entry.data["password"] == "not-a-real-password"
    assert CONF_CHILD_KEYS in entry.data


async def test_ed_options_hide_pronote_only_controls_and_count_child_keys(
    hass: HomeAssistant, no_spacing: None
) -> None:
    """ED options are not the Pronote page with an annexe B estimate."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="ecoledirecte:demo.example.invalid-options",
        data={
            CONF_SOURCE: "ecoledirecte",
            "username": "demo.example.invalid",
            "password": "not-a-real-password",
            "qcm_json": {},
            CONF_CHILD_KEYS: [
                {"key": "child-1", "resource_id": "1", "name": "Enfant Un"},
                {"key": "child-2", "resource_id": "2", "name": "Enfant Deux"},
            ],
        },
    )
    entry.add_to_hass(hass)

    opened = await hass.config_entries.options.async_init(entry.entry_id)
    assert opened["type"] is FlowResultType.MENU
    assert opened["description_placeholders"]["students"] == "2"
    assert opened["description_placeholders"]["estimate"] == "10"

    general = await hass.config_entries.options.async_configure(
        opened["flow_id"], {"next_step_id": "general"}
    )
    general_keys = {
        getattr(key, "schema", key) for key in general["data_schema"].schema
    }
    assert "session_strategy" not in general_keys
    assert "write_operations_enabled" not in general_keys

    hass.config_entries.options.async_abort(opened["flow_id"])
    tiers_menu = await hass.config_entries.options.async_init(entry.entry_id)
    tiers = await hass.config_entries.options.async_configure(
        tiers_menu["flow_id"], {"next_step_id": "tiers"}
    )
    tier_keys = {getattr(key, "schema", key) for key in tiers["data_schema"].schema}
    assert "interval_timetable" in tier_keys
    assert "enabled_timetable" in tier_keys
    assert "interval_news" not in tier_keys
    assert "enabled_news" not in tier_keys
    assert "interval_menus" not in tier_keys


async def test_a_single_child_account_is_created_straight_away(
    hass: HomeAssistant, no_spacing: None
) -> None:
    """One child means nothing to choose, so no extra step."""
    flow_id = await _start(hass)

    with patch(
        "custom_components.carnet_scolaire.config_flow._probe",
        return_value=_outcome(("STUDENT-1", "Enfant Un")),
    ):
        result = await _submit_credentials(hass, flow_id)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_CHILDREN] == ["STUDENT-1"]
    assert result["data"][CONF_LOGIN_MODE] == LoginMode.CREDENTIALS.value


async def test_a_new_entry_is_created_with_writes_on(
    hass: HomeAssistant, no_spacing: None
) -> None:
    """A homework list that cannot be ticked reads as a broken integration.

    Written into the entry's own options rather than left to a fallback,
    because the fallback must keep answering "off" for every entry created
    before this default changed (§8.3).
    """
    flow_id = await _start(hass)

    with patch(
        "custom_components.carnet_scolaire.config_flow._probe",
        return_value=_outcome(("STUDENT-1", "Enfant Un")),
    ):
        result = await _submit_credentials(hass, flow_id)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["options"][OPT_WRITE_OPERATIONS_ENABLED] is True
    assert result["result"].options[OPT_WRITE_OPERATIONS_ENABLED] is True


async def test_a_parent_account_asks_which_children_to_follow(
    hass: HomeAssistant, no_spacing: None
) -> None:
    """And the default is all of them, which is what a parent expects."""
    flow_id = await _start(hass)

    with patch(
        "custom_components.carnet_scolaire.config_flow._probe",
        return_value=_outcome(("STUDENT-1", "Enfant Un"), ("STUDENT-2", "Enfant Deux")),
    ):
        result = await _submit_credentials(hass, flow_id)
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "children"

        result = dict(
            await hass.config_entries.flow.async_configure(
                flow_id, {CONF_CHILDREN: ["STUDENT-2"]}
            )
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_CHILDREN] == ["STUDENT-2"]


async def test_the_same_account_cannot_be_added_twice(
    hass: HomeAssistant, mock_entry: MockConfigEntry, no_spacing: None
) -> None:
    """Otherwise every entity exists twice and every automation fires twice.

    The entry's ``unique_id`` is the account, which is why the fixture's entry
    and this flow collide: same server, same login.
    """
    flow_id = await _start(hass)

    with patch(
        "custom_components.carnet_scolaire.config_flow._probe",
        return_value=_outcome(("STUDENT-1", "Enfant Un")),
    ):
        result = await _submit_credentials(hass, flow_id)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_an_ent_account_records_its_provider(
    hass: HomeAssistant, no_spacing: None
) -> None:
    """The provider is resolved by name at login time, so it has to be stored."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "pronote"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "ent"}
    )
    assert result["type"] is FlowResultType.FORM

    schema_keys = {str(key.schema) for key in result["data_schema"].schema}
    assert CONF_ENT in schema_keys

    with patch(
        "custom_components.carnet_scolaire.config_flow._probe",
        return_value=_outcome(("STUDENT-1", "Enfant Un")),
    ):
        created = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_PRONOTE_URL: PASTED_URL,
                "username": "parent-under-test",
                "password": "not-a-real-password",
                CONF_ENT: "ac_reunion",
            },
        )

    assert created["type"] is FlowResultType.CREATE_ENTRY
    assert created["data"][CONF_ENT] == "ac_reunion"
    assert created["data"][CONF_LOGIN_MODE] == LoginMode.ENT.value


# ---------------------------------------------------------------------------
# The address
# ---------------------------------------------------------------------------


async def test_the_query_string_of_the_pasted_address_is_not_stored(
    hass: HomeAssistant, no_spacing: None
) -> None:
    """§8.2, at the point where the string enters the integration.

    Establishments mail out deep links and ENT portals bounce back through
    single sign-on with a ticket in the URL, so what a parent pastes regularly
    carries a session parameter. Stored, it went on to appear in a repair
    issue's placeholders -- written to ``.storage`` and rendered in the Repairs
    panel -- and in the diagnostics download, the file users are asked to
    attach to a public issue.
    """
    flow_id = await _start(hass)

    with patch(
        "custom_components.carnet_scolaire.config_flow._probe",
        return_value=_outcome(("STUDENT-1", "Enfant Un")),
    ) as probe:
        result = await _submit_credentials(hass, flow_id)

    assert result["data"][CONF_PRONOTE_URL] == TRIMMED_URL
    assert "identifiant" not in json.dumps(result["data"])
    # And the login itself is attempted against the trimmed address, so this is
    # not a display-only sanitisation that leaves the real value in flight.
    assert probe.call_args.args[0][CONF_PRONOTE_URL] == TRIMMED_URL


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param(PASTED_URL, TRIMMED_URL, id="query-string"),
        pytest.param(f"{TRIMMED_URL}#fragment", TRIMMED_URL, id="fragment"),
        pytest.param(
            "https://user:secret@demo.example.invalid/pronote/parent.html",
            TRIMMED_URL,
            id="userinfo",
        ),
        pytest.param(
            "https://demo.example.invalid:8443/pronote/parent.html",
            "https://demo.example.invalid:8443/pronote/parent.html",
            id="keeps-a-port",
        ),
        pytest.param("not a url at all", "not a url at all", id="unparseable"),
        pytest.param("", "", id="empty"),
    ],
)
def test_the_address_is_trimmed_but_never_second_guessed(
    raw: str, expected: str
) -> None:
    """A port is kept, a credential is dropped, and nonsense is passed through.

    Refusing an address the user can see in their own browser would be worse
    than storing an odd one: the login will fail with the server's own message,
    which is a better diagnosis than anything this function could invent.
    """
    assert public_url(raw) == expected


def test_the_host_is_extracted_for_the_places_that_only_need_it() -> None:
    """Used by the diagnostics, where even the path is more than is needed."""
    assert url_host(PASTED_URL) == "demo.example.invalid"
    assert url_host("nonsense") == ""
    assert url_host(None) == ""


# ---------------------------------------------------------------------------
# Failures, and the guard rail
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        pytest.param(ProbeInvalidCredentials(), "invalid_auth", id="wrong-password"),
        pytest.param(ProbeMfaRequired(), "mfa_required", id="pin-demanded"),
        pytest.param(ProbeBootstrapFailed(), "bootstrap_failed", id="no-session-page"),
        pytest.param(ProbeEntRequired(), "ent_required", id="delegated-to-ent"),
        pytest.param(OSError("refused"), "cannot_connect", id="unreachable"),
        pytest.param(RuntimeError("surprise"), "unknown", id="unexpected"),
    ],
)
async def test_a_failed_login_re_displays_the_form_with_a_reason(
    hass: HomeAssistant, no_spacing: None, error: Exception, expected: str
) -> None:
    """Never an abort: the user is one corrected field away from succeeding."""
    flow_id = await _start(hass)

    with patch(
        "custom_components.carnet_scolaire.config_flow._probe", side_effect=error
    ):
        result = await _submit_credentials(hass, flow_id)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "credentials"
    assert result["errors"] == {"base": expected}


async def test_an_undecodable_answer_in_the_flow_is_logged_without_its_payload(
    hass: HomeAssistant, no_spacing: None, caplog: pytest.LogCaptureFixture
) -> None:
    """The flow's login decodes what the runtime's does, with the same risk.

    An unexpected failure was logged with ``_LOGGER.exception``, and the
    traceback of a decoding error prints what it could not read -- a child's
    name, a ``N``. It is described instead; a genuine bug keeps its traceback.
    """
    caplog.set_level(logging.DEBUG, logger="custom_components.carnet_scolaire")
    flow_id = await _start(hass)
    error = KeyError("46#NOT-A-REAL-N")
    error.__context__ = ValueError("Enfant Un")

    with patch(
        "custom_components.carnet_scolaire.config_flow._probe", side_effect=error
    ):
        result = await _submit_credentials(hass, flow_id)

    assert result["errors"] == {"base": "unknown"}
    assert "unexpected failure while validating the account: KeyError (decode)" in (
        caplog.text
    )
    assert "Traceback" not in caplog.text
    assert "46#NOT-A-REAL-N" not in caplog.text
    assert "Enfant Un" not in caplog.text


async def test_the_flow_stops_asking_after_three_refusals(
    hass: HomeAssistant, no_spacing: None
) -> None:
    """The guard rail that was not connected to this path at all.

    ``max_failed_logins_per_hour`` is three, and the fourth attempt must not be
    *sent*: PRONOTE's one sanction applies to the address, not to the account,
    and it is undocumented and costly (annexe B §3.1). The form still comes
    back -- with "too many attempts" rather than "wrong password", because
    nothing was tried.
    """
    for _attempt in range(3):
        flow_id = await _start(hass)
        with patch(
            "custom_components.carnet_scolaire.config_flow._probe",
            side_effect=ProbeInvalidCredentials(),
        ):
            result = await _submit_credentials(hass, flow_id)
        assert result["errors"] == {"base": "invalid_auth"}

    flow_id = await _start(hass)
    with patch(
        "custom_components.carnet_scolaire.config_flow._probe",
        side_effect=ProbeInvalidCredentials(),
    ) as probe:
        result = await _submit_credentials(hass, flow_id)

    assert result["errors"] == {"base": "rate_limited"}
    assert probe.call_count == 0, "the fourth attempt reached the server"


async def test_the_guard_counts_the_requests_a_flow_login_really_spends(
    hass: HomeAssistant, no_spacing: None
) -> None:
    """Five for a login, and ten for QR enrolment, which does two.

    Counted because the daily cap is on HTTP requests: a flow that spent them
    invisibly made every subsequent budget figure wrong, and the diagnostics
    would have shown an integration well inside a cap it had already passed.
    """
    guard = login_guard(hass)
    before = guard.calls_today

    flow_id = await _start(hass)
    with patch(
        "custom_components.carnet_scolaire.config_flow._probe",
        return_value=_outcome(("STUDENT-1", "Enfant Un")),
    ):
        await _submit_credentials(hass, flow_id)

    assert guard.calls_today - before == 5
    assert guard.snapshot_counters()["logins_today"] >= 1


# ---------------------------------------------------------------------------
# QR enrolment
# ---------------------------------------------------------------------------


async def _start_qr(hass: HomeAssistant) -> str:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "pronote"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "qr_code"}
    )
    assert result["type"] is FlowResultType.FORM
    return str(result["flow_id"])


async def test_qr_enrolment_keeps_neither_the_payload_nor_its_pin(
    hass: HomeAssistant, no_spacing: None
) -> None:
    """§8.1, and both halves matter.

    The payload is an autonomous bearer until PRONOTE invalidates it, and the
    four-digit code decrypts it. Keeping either in the entry would put a working
    credential in ``.storage`` -- and in every backup -- for the sake of a value
    that is single-use by construction.
    """
    flow_id = await _start_qr(hass)

    with patch(
        "custom_components.carnet_scolaire.config_flow._probe",
        return_value=_outcome(("STUDENT-1", "Enfant Un")),
    ):
        result = dict(
            await hass.config_entries.flow.async_configure(
                flow_id,
                {
                    CONF_QR_PAYLOAD: json.dumps(QR_PAYLOAD),
                    CONF_QR_PIN: "1234",
                },
            )
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    data = result["data"]
    assert CONF_QR_PAYLOAD not in data
    assert CONF_QR_PIN not in data
    assert CONF_ACCOUNT_PIN not in data
    # The device UUID, on the other hand, has to persist: PRONOTE ties the
    # token to it, and a new one at the next login means a refused token.
    assert data[CONF_UUID]
    assert data[CONF_PRONOTE_URL] == TRIMMED_URL


async def test_a_qr_payload_that_is_not_one_is_refused_before_any_request(
    hass: HomeAssistant, no_spacing: None
) -> None:
    """Field-level, so the user sees which box is wrong."""
    flow_id = await _start_qr(hass)

    with patch("custom_components.carnet_scolaire.config_flow._probe") as probe:
        result = await hass.config_entries.flow.async_configure(
            flow_id, {CONF_QR_PAYLOAD: "this is not json", CONF_QR_PIN: "1234"}
        )

    assert result["errors"] == {CONF_QR_PAYLOAD: "invalid_qr_payload"}
    assert probe.call_count == 0


async def test_a_consumed_qr_code_says_to_generate_a_new_one(
    hass: HomeAssistant, no_spacing: None
) -> None:
    """A QR code is single-use, and PRONOTE invalidates it on enrolment."""
    flow_id = await _start_qr(hass)

    with patch(
        "custom_components.carnet_scolaire.config_flow._probe",
        side_effect=ProbeQrInvalid(),
    ):
        result = await hass.config_entries.flow.async_configure(
            flow_id,
            {CONF_QR_PAYLOAD: json.dumps(QR_PAYLOAD), CONF_QR_PIN: "1234"},
        )

    assert result["errors"] == {"base": "invalid_qr"}


async def test_qr_enrolment_is_charged_for_the_two_logins_it_performs(
    hass: HomeAssistant, no_spacing: None
) -> None:
    """``qrcode_login`` then ``token_login``: two handshakes, ten requests.

    Declared as two so the budget is honest, and exempted from the
    three-attempt guard rather than eating two thirds of it (annexe B §3.3).
    """
    guard = login_guard(hass)
    before = guard.calls_today

    flow_id = await _start_qr(hass)
    with patch(
        "custom_components.carnet_scolaire.config_flow._probe",
        return_value=_outcome(("STUDENT-1", "Enfant Un")),
    ):
        await hass.config_entries.flow.async_configure(
            flow_id,
            {CONF_QR_PAYLOAD: json.dumps(QR_PAYLOAD), CONF_QR_PIN: "1234"},
        )

    assert guard.calls_today - before == 10


# ---------------------------------------------------------------------------
# Re-authentication
# ---------------------------------------------------------------------------


async def test_reauthentication_asks_for_the_pin_and_forgets_it_again(
    hass: HomeAssistant, mock_entry: MockConfigEntry, no_spacing: None
) -> None:
    """The whole reason this flow exists (§8.1).

    A secret we refuse to store is a secret we must know how to ask for again;
    and having asked, we must not keep it -- otherwise "we do not store your
    PIN" is true only until the first time PRONOTE demands one.
    """
    result = await mock_entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"

    with patch(
        "custom_components.carnet_scolaire.config_flow._probe",
        return_value=_outcome(("STUDENT-1", "Enfant Un")),
    ) as probe:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"password": "corrected-not-real", CONF_ACCOUNT_PIN: "4321"},
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    # Supplied to the login...
    assert probe.call_args.args[0][CONF_ACCOUNT_PIN] == "4321"
    # ...and absent from what was persisted.
    assert CONF_ACCOUNT_PIN not in mock_entry.data
    assert mock_entry.data["password"] == "not-a-real-password"


async def test_reauthentication_lifts_the_hold_a_human_can_only_lift(
    hass: HomeAssistant, mock_entry: MockConfigEntry, no_spacing: None
) -> None:
    """``reset_after_reauth`` had no caller in the product at all.

    Which made the MFA hold inescapable: it exists precisely because a person
    has to supply a PIN, and the act of supplying it did not clear it. Here the
    guard is driven into the credentials hold first, then the re-authentication
    has to go through anyway.
    """
    guard = login_guard(hass)
    for _attempt in range(3):
        flow_id = await _start(hass)
        with patch(
            "custom_components.carnet_scolaire.config_flow._probe",
            side_effect=ProbeInvalidCredentials(),
        ):
            await _submit_credentials(hass, flow_id)
    assert not guard.may_login().allowed

    result = await mock_entry.start_reauth_flow(hass)
    with patch(
        "custom_components.carnet_scolaire.config_flow._probe",
        return_value=_outcome(("STUDENT-1", "Enfant Un")),
    ) as probe:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"password": "corrected-not-real"}
        )

    assert probe.call_count == 1, "the human's corrected password was not even tried"
    assert result["reason"] == "reauth_successful"
    assert guard.may_login().allowed


async def test_a_reauthentication_that_fails_says_so_and_stays_open(
    hass: HomeAssistant, mock_entry: MockConfigEntry, no_spacing: None
) -> None:
    """The form comes back, because the next attempt may be the right one."""
    result = await mock_entry.start_reauth_flow(hass)

    with patch(
        "custom_components.carnet_scolaire.config_flow._probe",
        side_effect=ProbeInvalidCredentials(),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"password": "still-wrong-not-real"}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    assert result["errors"] == {"base": "invalid_auth"}
    # And the form names the establishment's address, trimmed, so the user can
    # see *which* account is being repaired.
    placeholders = result["description_placeholders"] or {}
    assert placeholders["url"] == public_url(mock_entry.data[CONF_PRONOTE_URL])


@pytest.mark.parametrize("payload", ["[1, 2]", '"just a string"', "42", "null", "true"])
async def test_a_qr_payload_that_is_json_but_not_an_object_is_refused(
    hass: HomeAssistant, no_spacing: None, payload: str
) -> None:
    """Valid JSON of the wrong shape, which is what a bad decode produces.

    A reader handed a cropped or blurred code regularly returns something that
    parses -- a bare number, a string, a list -- and this was rejected with a
    ``TypeError`` while the flow caught only ``ValueError``. The result was an
    unhandled exception and Home Assistant's generic "unknown error occurred",
    which tells a parent nothing about the box they should re-scan into.
    """
    flow_id = await _start_qr(hass)

    with patch("custom_components.carnet_scolaire.config_flow._probe") as probe:
        result = await hass.config_entries.flow.async_configure(
            flow_id, {CONF_QR_PAYLOAD: payload, CONF_QR_PIN: "1234"}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_QR_PAYLOAD: "invalid_qr_payload"}
    assert probe.call_count == 0, "nothing may reach the school for a bad payload"


async def test_the_first_screen_asks_the_platform_before_any_login_method(
    hass: HomeAssistant,
) -> None:
    """The platform is chosen first, and PRONOTE's methods stay under PRONOTE.

    With both sources on one screen, "username and password" sat next to
    "EcoleDirecte" and a parent could not tell whose credentials it asked for.
    The first menu therefore names platforms only; the three PRONOTE methods
    appear once PRONOTE is chosen, and EcoleDirecte leads straight to its form.
    """
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.MENU
    assert result["menu_options"] == ["pronote", "ecoledirecte"]

    pronote = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "pronote"}
    )
    assert pronote["type"] is FlowResultType.MENU
    assert pronote["step_id"] == "pronote"
    assert pronote["menu_options"] == ["qr_code", "credentials", "ent"]

    other = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    ecoledirecte = await hass.config_entries.flow.async_configure(
        other["flow_id"], {"next_step_id": "ecoledirecte"}
    )
    assert ecoledirecte["type"] is FlowResultType.FORM
    assert ecoledirecte["step_id"] == "ecoledirecte"


async def test_the_first_screen_supplies_every_placeholder_its_text_uses(
    hass: HomeAssistant,
) -> None:
    """A placeholder nobody fills is rendered literally, braces and all.

    The login-mode screen carries the integration's logo as a markdown image,
    and the address is passed as a description placeholder rather than written
    into the translation string -- hassfest rejects a URL inside one and names
    this as the mechanism to use instead. The cost of that indirection is that
    the string and the code that feeds it can drift apart, and the failure is
    cosmetic-but-glaring: the very first screen of the flow greets a parent
    with `![Carnet scolaire]({logo})`.

    So this reads the placeholders the catalogue actually asks for and checks
    the flow supplies each one, rather than asserting a hard-coded name.
    """
    strings = json.loads(
        await asyncio.to_thread(
            pathlib.Path("custom_components/carnet_scolaire/strings.json").read_text,
            encoding="utf-8",
        )
    )
    description = strings["config"]["step"]["user"]["description"]
    wanted = {name for _, name, _, _ in Formatter().parse(description) if name}
    assert wanted, "the step's text uses no placeholder; this test is now moot"

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )

    assert result["type"] is FlowResultType.MENU
    supplied = result.get("description_placeholders") or {}
    missing = wanted - set(supplied)
    assert not missing, f"the flow does not supply {sorted(missing)}"
    assert supplied["logo"].startswith("https://")


@pytest.mark.parametrize(
    ("payload", "missing"),
    [
        pytest.param('{"login": "l", "jeton": "j"}', ["url"], id="no url"),
        pytest.param('{"url": "u"}', ["jeton", "login"], id="only the url"),
        pytest.param("{}", ["jeton", "login", "url"], id="an empty object"),
    ],
)
async def test_a_qr_payload_missing_a_key_is_refused_before_any_request(
    hass: HomeAssistant, no_spacing: None, payload: str, missing: list[str]
) -> None:
    """A JSON object of the right type and the wrong contents.

    This is not the cropped-code case above: it is what a QR reader produces
    from *another application's* code, or from a PRONOTE code generated by a
    version that names its fields differently. The three keys are the ones
    ``pronotepy`` indexes without checking, so letting such a payload through
    turns a re-scan into a `KeyError` inside the login -- reported as
    "unknown error" after having spent a login attempt against the address.
    """
    flow_id = await _start_qr(hass)

    with patch("custom_components.carnet_scolaire.config_flow._probe") as probe:
        result = await hass.config_entries.flow.async_configure(
            flow_id, {CONF_QR_PAYLOAD: payload, CONF_QR_PIN: "1234"}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_QR_PAYLOAD: "invalid_qr_payload"}
    assert probe.call_count == 0, "nothing may reach the school for a bad payload"


async def test_a_failed_ent_login_comes_back_to_the_ent_form(
    hass: HomeAssistant, no_spacing: None
) -> None:
    """Each mode re-displays *its own* form, and ENT is the one that differs.

    `_async_reshow` branches on the stored mode. Falling through to the
    credentials form -- which is what happens if the ENT arm is missed -- loses
    the provider the user chose, so the retry silently attempts a direct login
    against an establishment that only accepts federated ones, and fails for a
    second, unrelated reason.
    """
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "pronote"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "ent"}
    )

    with patch(
        "custom_components.carnet_scolaire.config_flow._probe",
        side_effect=ProbeInvalidCredentials("the identity provider refused"),
    ):
        refused = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_PRONOTE_URL: PASTED_URL,
                "username": "parent-under-test",
                "password": "not-a-real-password",
                CONF_ENT: "ac_reunion",
            },
        )

    assert refused["type"] is FlowResultType.FORM
    assert refused["step_id"] == "ent"
    assert refused["errors"] == {"base": "invalid_auth"}
    assert CONF_ENT in {str(key.schema) for key in refused["data_schema"].schema}


async def test_reauthentication_accepts_a_pin_without_a_new_password(
    hass: HomeAssistant, mock_entry: MockConfigEntry, no_spacing: None
) -> None:
    """The commonest reason this form is shown at all.

    PRONOTE asked for a security code; the password never changed and the user
    has no reason to retype it. Both fields are optional and each is applied
    only when filled, so an empty password box must leave the stored one alone
    rather than overwrite it with `""` -- which would turn one solvable
    interruption into a permanently broken entry.
    """
    stored = mock_entry.data["password"]
    result = await mock_entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"

    with patch(
        "custom_components.carnet_scolaire.config_flow._probe",
        return_value=_outcome(("STUDENT-1", "Enfant Un")),
    ):
        done = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_ACCOUNT_PIN: "1234"}
        )

    assert done["type"] is FlowResultType.ABORT
    assert done["reason"] == "reauth_successful"
    assert mock_entry.data["password"] == stored
    assert CONF_ACCOUNT_PIN not in mock_entry.data


# ---------------------------------------------------------------------------
# Re-authenticating a QR account
#
# The one login mode the documentation recommends, and the one the
# re-authentication flow could not repair. §8.1 forbids keeping the QR payload,
# so a QR-enrolled entry carries `login_mode: qr_code` for the rest of its life
# with no payload beside it -- and the probe branched on the mode alone, so it
# reached for `data[CONF_QR_PAYLOAD]` and raised `KeyError`. Unclassified, that
# surfaced as "unexpected error" on the form whose only job is to repair a
# broken login. Every reauth test above uses credentials mode, which is exactly
# why none of them saw it.
# ---------------------------------------------------------------------------

#: What a QR-enrolled entry really holds once the flow has finished with it:
#: the mode, the rotated token in `password`, the device identity -- and no
#: payload. And the key table the running account writes at every set-up,
#: whose recorded names are what the identity check compares.
QR_ENTRY_DATA: dict[str, Any] = {
    CONF_PRONOTE_URL: TRIMMED_URL,
    CONF_LOGIN_MODE: LoginMode.QR_CODE.value,
    "username": "not-a-real-login",
    "password": "not-a-real-rotated-token",
    CONF_UUID: "not-a-real-uuid",
    CONF_CLIENT_IDENTIFIER: "not-a-real-client-id",
    CONF_CHILDREN: ["STUDENT-2"],
    CONF_CHILD_KEYS: [
        {"key": "child-1", "resource_id": "STUDENT-1", "name": "Enfant Un"},
        {"key": "child-2", "resource_id": "STUDENT-2", "name": "Enfant Deux"},
    ],
}


#: Shaped like a real one, signature included: `flow_login._account_id` builds
#: `<url>::<resource N>`, and the `N` is written `46#<opaque blob>`. The blob
#: changes at every session; the `46` is the same for every child of every
#: account -- a resource type, not a number of its own -- which is why the
#: tests below refuse another account by its children and not by that prefix.
QR_ACCOUNT_ID = f"{TRIMMED_URL}::46#not-a-real-signature"


def _qr_outcome(
    account_id: str = QR_ACCOUNT_ID,
    children: list[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    """What the probe hands back after a successful re-enrolment.

    The token is a *new* one, which is the point of the whole exercise: the old
    one is what PRONOTE just refused.
    """
    return {
        "account_id": account_id,
        "children": (
            children
            if children is not None
            else [("STUDENT-1", "Enfant Un"), ("STUDENT-2", "Enfant Deux")]
        ),
        "title": "PRONOTE",
        CONF_LOGIN_MODE: LoginMode.QR_CODE.value,
        "username": "not-a-real-login",
        "password": "not-a-real-fresh-token",
        CONF_UUID: "not-a-real-uuid",
        CONF_CLIENT_IDENTIFIER: "not-a-real-new-client-id",
    }


@pytest.fixture(name="qr_entry")
def qr_entry_fixture(hass: HomeAssistant) -> MockConfigEntry:
    """A QR-enrolled entry, following one of the account's two children."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="PRONOTE",
        data=dict(QR_ENTRY_DATA),
        unique_id=QR_ACCOUNT_ID,
    )
    entry.add_to_hass(hass)
    return entry


async def test_reconnecting_a_qr_account_asks_for_a_qr_code_and_not_a_password(
    hass: HomeAssistant, qr_entry: MockConfigEntry, no_spacing: None
) -> None:
    """A token account has no password, so a password field is a dead end.

    PRONOTE rotates ``jetonConnexionAppliMobile`` at every login; once the
    stored one is refused it is dead for good, and nothing but a new QR code
    mints another. The old form offered a password box, which could not have
    worked even if the parent had typed the right thing into it.
    """
    result = await qr_entry.start_reauth_flow(hass)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_qr"
    schema = result["data_schema"]
    assert schema is not None
    fields = {str(key.schema) for key in schema.schema}
    assert {CONF_QR_PAYLOAD, CONF_QR_PIN} <= fields
    assert "password" not in fields
    # And it names the address, so a parent with two entries knows which one.
    placeholders = result["description_placeholders"] or {}
    assert placeholders["url"] == TRIMMED_URL


async def test_a_qr_reconnection_reaches_the_probe_instead_of_raising(
    hass: HomeAssistant, qr_entry: MockConfigEntry, no_spacing: None
) -> None:
    """The regression, stated in terms of what the parent sees.

    Before, submitting this form ended in ``KeyError: 'qr_payload'``, caught by
    the flow's blanket handler and shown as ``unknown`` -- "unexpected error,
    check the log". The account was unrepairable from the interface.
    """
    result = await qr_entry.start_reauth_flow(hass)

    with patch(
        "custom_components.carnet_scolaire.config_flow._probe",
        return_value=_qr_outcome(),
    ) as probe:
        done = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_QR_PAYLOAD: json.dumps(QR_PAYLOAD), CONF_QR_PIN: "1234"},
        )

    # Drained here rather than left to teardown: `async_update_reload_and_abort`
    # *schedules* the reload, and a reload that runs after `no_setup` has
    # released its patch sets the entry up for real -- a live login attempt out
    # of a config-flow test, and a worker thread the test never tears down.
    await hass.async_block_till_done()

    assert done["type"] is FlowResultType.ABORT
    assert done["reason"] == "reauth_successful"
    assert probe.call_count == 1
    submitted = probe.call_args.args[0]
    assert submitted[CONF_QR_PAYLOAD]["jeton"] == QR_PAYLOAD["jeton"]
    assert submitted[CONF_QR_PIN] == "1234"


async def test_a_qr_reconnection_reuses_the_device_uuid_it_was_enrolled_with(
    hass: HomeAssistant, qr_entry: MockConfigEntry, no_spacing: None
) -> None:
    """PRONOTE lists enrolled devices, and a new UUID adds one every time.

    Reusing the stored UUID keeps this Home Assistant a single device in the
    account's list rather than one copy per reconnection.
    """
    result = await qr_entry.start_reauth_flow(hass)

    with patch(
        "custom_components.carnet_scolaire.config_flow._probe",
        return_value=_qr_outcome(),
    ) as probe:
        await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_QR_PAYLOAD: json.dumps(QR_PAYLOAD), CONF_QR_PIN: "1234"},
        )

    await hass.async_block_till_done()

    assert probe.call_args.args[0][CONF_UUID] == QR_ENTRY_DATA[CONF_UUID]
    assert qr_entry.data[CONF_UUID] == QR_ENTRY_DATA[CONF_UUID]


async def test_a_qr_reconnection_persists_the_token_and_no_single_use_secret(
    hass: HomeAssistant, qr_entry: MockConfigEntry, no_spacing: None
) -> None:
    """§8.1 applies to a reconnection exactly as it does to an enrolment.

    The re-authentication path used to strip the account PIN and nothing else,
    which was enough while the only reconnection was a password one. A QR
    reconnection puts a single-use payload and its four-digit code into the same
    dictionary, and both would have been written to the entry.
    """
    result = await qr_entry.start_reauth_flow(hass)

    with patch(
        "custom_components.carnet_scolaire.config_flow._probe",
        return_value=_qr_outcome(),
    ):
        await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_QR_PAYLOAD: json.dumps(QR_PAYLOAD),
                CONF_QR_PIN: "1234",
                CONF_ACCOUNT_PIN: "4321",
            },
        )

    await hass.async_block_till_done()

    assert qr_entry.data["password"] == "not-a-real-fresh-token"
    assert qr_entry.data[CONF_CLIENT_IDENTIFIER] == "not-a-real-new-client-id"
    for forbidden in (CONF_QR_PAYLOAD, CONF_QR_PIN, CONF_ACCOUNT_PIN, "account_id"):
        assert forbidden not in qr_entry.data, f"{forbidden} was persisted"


async def test_a_qr_code_from_another_account_is_refused_rather_than_applied(
    hass: HomeAssistant, qr_entry: MockConfigEntry, no_spacing: None
) -> None:
    """A QR code carries its own account, so it can name a different child.

    Same establishment and the same ``46#`` prefix -- which every child
    carries, so the account identifier cannot tell the two families apart --
    but none of this entry's children. A parent with two accounts has two apps
    to generate a code from; applying the wrong one would keep this entry's
    devices, entity ids and history and quietly point them at somebody else's
    child.
    """
    result = await qr_entry.start_reauth_flow(hass)

    with patch(
        "custom_components.carnet_scolaire.config_flow._probe",
        return_value=_qr_outcome(
            account_id=f"{TRIMMED_URL}::46#another-family-signature",
            children=[("OTHER-1", "Autre Enfant")],
        ),
    ):
        done = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_QR_PAYLOAD: json.dumps(QR_PAYLOAD), CONF_QR_PIN: "1234"},
        )

    assert done["type"] is FlowResultType.ABORT
    assert done["reason"] == "wrong_account"
    # And nothing was written: the entry still holds everything it had.
    assert dict(qr_entry.data) == QR_ENTRY_DATA


async def test_a_re_enrolment_of_the_same_account_survives_a_rotated_signature(
    hass: HomeAssistant, qr_entry: MockConfigEntry, no_spacing: None
) -> None:
    """The identity guard must not mistake a new session for a new account.

    A parent resource's PRONOTE identifier is written ``46#<opaque blob>``. The
    number is establishment-local and stable; the blob is undocumented, and
    nothing promises it survives a re-enrolment. A guard comparing the whole
    string would therefore fire on the *correct* QR code and tell a parent their
    own account belongs to somebody else -- with no way out but deleting the
    entry and losing its history, which is exactly the outcome the guard exists
    to prevent.
    """
    rotated = f"{TRIMMED_URL}::46#a-different-signature-same-account"
    assert rotated != qr_entry.unique_id

    result = await qr_entry.start_reauth_flow(hass)
    with patch(
        "custom_components.carnet_scolaire.config_flow._probe",
        return_value=_qr_outcome(account_id=rotated),
    ):
        done = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_QR_PAYLOAD: json.dumps(QR_PAYLOAD), CONF_QR_PIN: "1234"},
        )
    await hass.async_block_till_done()

    assert done["type"] is FlowResultType.ABORT
    assert done["reason"] == "reauth_successful"


async def test_the_same_resource_at_another_establishment_is_a_different_account(
    hass: HomeAssistant, qr_entry: MockConfigEntry, no_spacing: None
) -> None:
    """The number alone is not the identity: it is local to one establishment.

    Resource 46 exists at every school. Dropping the address from the
    comparison to tolerate the rotating signature would make two unrelated
    parents at two schools look like the same account -- the loose end of the
    same trade-off, checked here so neither half can be relaxed alone.
    """
    result = await qr_entry.start_reauth_flow(hass)
    with patch(
        "custom_components.carnet_scolaire.config_flow._probe",
        return_value=_qr_outcome(
            account_id="https://autre.example.invalid/pronote/mobile.parent.html::46#s"
        ),
    ):
        done = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_QR_PAYLOAD: json.dumps(QR_PAYLOAD), CONF_QR_PIN: "1234"},
        )

    assert done["type"] is FlowResultType.ABORT
    assert done["reason"] == "wrong_account"


async def test_a_bad_payload_on_reconnection_never_reaches_the_school(
    hass: HomeAssistant, qr_entry: MockConfigEntry, no_spacing: None
) -> None:
    """Parsed before anything is sent, as on the enrolment form."""
    result = await qr_entry.start_reauth_flow(hass)

    with patch("custom_components.carnet_scolaire.config_flow._probe") as probe:
        again = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_QR_PAYLOAD: "not json at all", CONF_QR_PIN: "1234"},
        )

    assert again["type"] is FlowResultType.FORM
    assert again["step_id"] == "reauth_qr"
    assert again["errors"] == {CONF_QR_PAYLOAD: "invalid_qr_payload"}
    assert probe.call_count == 0


async def test_a_consumed_qr_code_on_reconnection_says_to_generate_a_new_one(
    hass: HomeAssistant, qr_entry: MockConfigEntry, no_spacing: None
) -> None:
    """The form comes back, because a new QR code is one tap away."""
    result = await qr_entry.start_reauth_flow(hass)

    with patch(
        "custom_components.carnet_scolaire.config_flow._probe",
        side_effect=ProbeQrInvalid(),
    ):
        again = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_QR_PAYLOAD: json.dumps(QR_PAYLOAD), CONF_QR_PIN: "1234"},
        )

    assert again["type"] is FlowResultType.FORM
    assert again["step_id"] == "reauth_qr"
    assert again["errors"] == {"base": "invalid_qr"}
    assert (again["description_placeholders"] or {})["url"] == TRIMMED_URL


async def test_a_qr_reconnection_is_charged_for_the_two_logins_it_performs(
    hass: HomeAssistant, qr_entry: MockConfigEntry, no_spacing: None
) -> None:
    """It really is an enrolment, so it costs what an enrolment costs."""
    guard = login_guard(hass)
    before = guard.calls_today

    result = await qr_entry.start_reauth_flow(hass)
    with patch(
        "custom_components.carnet_scolaire.config_flow._probe",
        return_value=_qr_outcome(),
    ):
        await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_QR_PAYLOAD: json.dumps(QR_PAYLOAD), CONF_QR_PIN: "1234"},
        )

    await hass.async_block_till_done()

    assert guard.calls_today - before == 10


async def test_a_reconnection_does_not_widen_the_children_being_followed(
    hass: HomeAssistant, no_spacing: None
) -> None:
    """The defect ``_async_try_login`` documents, still live on the other path.

    The probe returns ``children`` as ``(id, name)`` label pairs under the very
    key that holds the user's *selection*. The enrolment path excludes it for
    that reason; the reconnection path merged the whole outcome in, so a parent
    following one of two children came back from any reconnection following
    both -- silently, and with the second child's whole request budget.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="PRONOTE",
        data={
            CONF_PRONOTE_URL: TRIMMED_URL,
            CONF_LOGIN_MODE: LoginMode.CREDENTIALS.value,
            "username": "parent-under-test",
            "password": "not-a-real-password",
            CONF_CHILDREN: ["STUDENT-2"],
        },
        unique_id="demo.example.invalid|parent-under-test",
    )
    entry.add_to_hass(hass)

    result = await entry.start_reauth_flow(hass)
    with patch(
        "custom_components.carnet_scolaire.config_flow._probe",
        return_value=_outcome(("STUDENT-1", "Enfant Un"), ("STUDENT-2", "Enfant Deux")),
    ):
        done = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"password": "corrected-not-real"}
        )

    await hass.async_block_till_done()

    assert done["reason"] == "reauth_successful"
    assert entry.data[CONF_CHILDREN] == ["STUDENT-2"]


async def test_a_qr_code_the_server_refuses_is_not_reported_as_bad_credentials(
    hass: HomeAssistant, qr_entry: MockConfigEntry, no_spacing: None
) -> None:
    """The message six real attempts got, and why it was the wrong one.

    ``pronotepy`` raises a plain ``CryptoError`` when the server refuses the
    challenge, and the probe classified that as "the credentials were refused"
    -- which on a QR enrolment names things that cannot be the cause. There is
    no password. The account's two-factor PIN does not enter the key either:
    ``ClientBase._login`` derives it as ``username + SHA256(alea + password)``
    from the payload's own login and token, and uses the PIN only in
    ``_do_2fa``, after a challenge that has already succeeded.

    So the refusal is about the QR code, and it now says so. Upstream compounds
    the confusion by appending "probably the qr code has expired" on nothing
    more than ``login_mode == "qr_code"`` -- a guess, not a finding, and one
    this integration must not relay as an answer.
    """
    result = await qr_entry.start_reauth_flow(hass)

    with patch(
        "custom_components.carnet_scolaire.config_flow._probe",
        side_effect=ProbeQrRefused("challenge refused"),
    ):
        again = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_QR_PAYLOAD: json.dumps(QR_PAYLOAD), CONF_QR_PIN: "1234"},
        )

    assert again["type"] is FlowResultType.FORM
    assert again["step_id"] == "reauth_qr"
    assert again["errors"] == {"base": "qr_refused"}


async def test_the_two_qr_failures_stay_two_messages(
    hass: HomeAssistant, qr_entry: MockConfigEntry, no_spacing: None
) -> None:
    """The distinction the new message rests on.

    ``qr_refused`` tells the reader their four-digit code was right. That claim
    is only true because a wrong one arrives as ``invalid_qr`` instead -- the
    local AES pass in ``qrcode_login`` fails before a socket is opened. Collapse
    the two and the message asserts something false and sends the reader after
    the wrong field, which is exactly the mistake being corrected here.
    """
    result = await qr_entry.start_reauth_flow(hass)

    with patch(
        "custom_components.carnet_scolaire.config_flow._probe",
        side_effect=ProbeQrInvalid("invalid confirmation code"),
    ):
        again = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_QR_PAYLOAD: json.dumps(QR_PAYLOAD), CONF_QR_PIN: "1234"},
        )

    assert again["errors"] == {"base": "invalid_qr"}


async def test_a_typo_in_the_four_digit_code_does_not_spend_the_rail(
    hass: HomeAssistant, no_spacing: None
) -> None:
    """Three typos must not lock a parent out; three refusals must.

    ``qrcode_login`` decrypts the payload with the four-digit code before it
    opens a socket, so a wrong one never reaches the school. Counting it as a
    refused credential spent the three-attempt rail on nothing -- and that rail
    is the one thing standing between a parent retrying and an address PRONOTE
    stops answering, so spending it on typos is spending it on the wrong thing.

    What is *not* fixed here, and is stated rather than hidden: the daily
    request budget is still charged, because `RateLimiter.login` charges at
    admission, inside the lock, before the request goes out -- deliberately,
    and there is no refund path by design. So a typo costs allowance it did not
    use. The rail is the part that matters for staying logged in, and it is the
    part this corrects.
    """
    guard = login_guard(hass)

    for _attempt in range(3):
        flow_id = await _start_qr(hass)
        with patch(
            "custom_components.carnet_scolaire.config_flow._probe",
            side_effect=ProbeQrInvalid("invalid confirmation code"),
        ):
            await hass.config_entries.flow.async_configure(
                flow_id,
                {CONF_QR_PAYLOAD: json.dumps(QR_PAYLOAD), CONF_QR_PIN: "0000"},
            )

    assert guard.may_login().allowed, "three typos closed the door"


async def test_three_refusals_that_reached_the_server_do_close_the_door(
    hass: HomeAssistant, no_spacing: None
) -> None:
    """The other half, so the exemption above cannot widen unnoticed.

    A challenge the server refused cost a real login, and repeating it is the
    behaviour PRONOTE sanctions at the address. The rail has to see those.
    """
    guard = login_guard(hass)

    for _attempt in range(3):
        flow_id = await _start_qr(hass)
        with patch(
            "custom_components.carnet_scolaire.config_flow._probe",
            side_effect=ProbeQrRefused("challenge refused"),
        ):
            await hass.config_entries.flow.async_configure(
                flow_id,
                {CONF_QR_PAYLOAD: json.dumps(QR_PAYLOAD), CONF_QR_PIN: "1234"},
            )

    assert not guard.may_login().allowed


async def test_a_rejected_payload_is_logged_by_its_shape_and_not_its_content(
    hass: HomeAssistant, no_spacing: None, caplog: pytest.LogCaptureFixture
) -> None:
    """The rejection that never reaches the network, and logged nothing at all.

    A parent's messaging app quietly added characters to the pasted JSON, and
    this happened: the form said "that does not look like the content of a
    PRONOTE QR code" while the log stayed silent, so there was no way to tell
    that case from a payload of the wrong shape.

    The content must never be logged -- it is a working credential, and this
    log is the file users are invited to attach to a public issue (§8.2) -- so
    what is recorded is the length and whether it starts like a JSON object.
    That is enough to separate "pasted the wrong thing entirely" from "pasted
    JSON missing a key".
    """
    caplog.set_level(
        logging.DEBUG, logger="custom_components.carnet_scolaire.config_flow"
    )
    flow_id = await _start_qr(hass)
    pasted = '{"login": "SENTINEL-LOGIN-DO-NOT-LEAK"} trailing junk'

    with patch("custom_components.carnet_scolaire.config_flow._probe") as probe:
        again = await hass.config_entries.flow.async_configure(
            flow_id, {CONF_QR_PAYLOAD: pasted, CONF_QR_PIN: "1234"}
        )

    assert again["errors"] == {CONF_QR_PAYLOAD: "invalid_qr_payload"}
    assert probe.call_count == 0, "nothing may reach the school for a bad payload"

    assert "the pasted QR code was rejected" in caplog.text
    assert f"{len(pasted)} characters" in caplog.text
    assert "starts with an object brace" in caplog.text
    assert "SENTINEL-LOGIN-DO-NOT-LEAK" not in caplog.text


async def test_something_that_is_not_json_at_all_is_described_as_such(
    hass: HomeAssistant, no_spacing: None, caplog: pytest.LogCaptureFixture
) -> None:
    """The distinction the log exists to draw.

    "Not an object" is what a pasted image filename, a share link or a
    truncated read looks like; "starts with an object brace" is what a real
    payload of the wrong shape looks like. Same message on the screen, two
    different things to tell the user, and only the log can separate them.
    """
    caplog.set_level(
        logging.DEBUG, logger="custom_components.carnet_scolaire.config_flow"
    )
    flow_id = await _start_qr(hass)

    await hass.config_entries.flow.async_configure(
        flow_id,
        {CONF_QR_PAYLOAD: "IMG_20260909_qrcode.png", CONF_QR_PIN: "1234"},
    )

    assert "not an object" in caplog.text


async def test_a_password_reconnection_still_reads_as_one(
    hass: HomeAssistant, mock_entry: MockConfigEntry, no_spacing: None
) -> None:
    """A credentials account keeps the message that is correct for it.

    The same ``CryptoError`` means something different here: there *is* a
    password, and a wrong one is exactly what a failed challenge indicates
    (annexe B §3.1). The classification is what carries that difference now,
    rather than a re-wording applied after the fact -- which is why the probe
    decides it and the step does not.
    """
    result = await mock_entry.start_reauth_flow(hass)

    with patch(
        "custom_components.carnet_scolaire.config_flow._probe",
        side_effect=ProbeInvalidCredentials(),
    ):
        again = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"password": "still-wrong-not-real"}
        )

    assert again["errors"] == {"base": "invalid_auth"}


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(ProbeInvalidCredentials("challenge decryption failed"), id="auth"),
        pytest.param(ProbeQrInvalid("invalid confirmation code"), id="qr"),
        pytest.param(ProbeBootstrapFailed("no session page"), id="bootstrap"),
        pytest.param(ProbeMfaRequired("pin required"), id="mfa"),
        pytest.param(TimeoutError("timed out"), id="transport"),
    ],
)
async def test_every_classified_refusal_says_why_on_our_own_logger(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    no_spacing: None,
    caplog: pytest.LogCaptureFixture,
    failure: Exception,
) -> None:
    """A classified failure used to log nothing at all.

    Only the *unexpected* arm wrote a traceback, so "PRONOTE refused these
    credentials" was the whole of what a bug report could contain, and the four
    causes behind it were indistinguishable -- which cost two rounds of guessing
    on a real instance before the cause was found by reading upstream's source
    instead of the log.

    At DEBUG, because that is what a user turns on before reporting a bug, and
    strictly more conservative than the ERROR-level traceback the unexpected arm
    already writes unconditionally.
    """
    caplog.set_level(
        logging.DEBUG, logger="custom_components.carnet_scolaire.config_flow"
    )
    result = await mock_entry.start_reauth_flow(hass)

    with patch(
        "custom_components.carnet_scolaire.config_flow._probe", side_effect=failure
    ):
        await hass.config_entries.flow.async_configure(
            result["flow_id"], {"password": "still-wrong-not-real"}
        )

    assert "the login was refused" in caplog.text
    assert type(failure).__name__ in caplog.text


async def test_the_refusal_log_names_the_upstream_cause_and_not_only_ours(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    no_spacing: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Our four categories are the part that was never in doubt.

    The whole point of the line is the exception it was raised *from*: the
    category says "refused", and only ``pronotepy``'s own message distinguishes
    a stale token from a wrong two-factor PIN. `probe_account` already chains
    them with ``raise ... from error``, so the cause is there to be read.
    """
    caplog.set_level(
        logging.DEBUG, logger="custom_components.carnet_scolaire.config_flow"
    )
    cause = ValueError("challenge decryption failed")
    classified = ProbeInvalidCredentials("refused")
    classified.__cause__ = cause

    result = await mock_entry.start_reauth_flow(hass)
    with patch(
        "custom_components.carnet_scolaire.config_flow._probe", side_effect=classified
    ):
        await hass.config_entries.flow.async_configure(
            result["flow_id"], {"password": "still-wrong-not-real"}
        )

    assert "challenge decryption failed" in caplog.text
    assert "ValueError" in caplog.text


@pytest.mark.parametrize(
    "chained",
    [
        pytest.param(False, id="direct"),
        pytest.param(True, id="as-the-cause-of-a-refusal"),
    ],
)
async def test_the_refusal_log_never_quotes_a_transport_error(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    no_spacing: None,
    caplog: pytest.LogCaptureFixture,
    chained: bool,
) -> None:
    """The refusal line printed the message of a transport failure verbatim.

    A ``requests`` message names the establishment's host and the page path it
    failed on, parameters included, and this DEBUG line is what a user turns on
    before attaching the log to an issue. A static upstream message is still
    printed -- that half is the line's purpose -- but a transport one is reduced
    to class names and a category, whether it is the error or its cause.
    """
    caplog.set_level(
        logging.DEBUG, logger="custom_components.carnet_scolaire.config_flow"
    )
    leaky = requests.ConnectionError(
        "HTTPSConnectionPool(host='ecole.demo.example.invalid', port=443): "
        "Max retries exceeded with url: "
        "/pronote/parent.html?identifiant=NOT-A-REAL-SESSION"
    )
    failure: Exception = leaky
    if chained:
        failure = ProbeBootstrapFailed("bootstrap failed")
        failure.__cause__ = leaky

    result = await mock_entry.start_reauth_flow(hass)
    with patch(
        "custom_components.carnet_scolaire.config_flow._probe", side_effect=failure
    ):
        await hass.config_entries.flow.async_configure(
            result["flow_id"], {"password": "still-wrong-not-real"}
        )

    assert "the login was refused" in caplog.text
    assert "ConnectionError (connection)" in caplog.text
    assert "ecole.demo.example.invalid" not in caplog.text
    assert "NOT-A-REAL-SESSION" not in caplog.text


def test_the_flow_never_imports_pronotepy_to_be_added(
    hass: HomeAssistant,
) -> None:
    """`_probe` imports inside the function, and that is load-bearing twice.

    Not at module scope, because a broken or missing dependency would then turn
    "the integration cannot log in" into "the integration cannot be added at
    all" -- and the config flow is the only screen that could ever explain the
    problem.

    Not inside the coroutine either: Home Assistant instruments `importlib` and
    reports an import performed on the event loop as a blocking call. So it
    happens here, on the worker thread, immediately before the login it belongs
    to. Neither half of that is visible from reading `_probe`'s two lines,
    which is why it is asserted.
    """
    from custom_components.carnet_scolaire import config_flow

    assert not any(
        name == "pronotepy" or name.startswith("pronotepy.")
        for name in vars(config_flow)
    ), "pronotepy is bound in the flow's namespace, so it was imported at module scope"

    with patch(
        "custom_components.carnet_scolaire.flow_login.probe_account",
        return_value={"account_id": "a", "children": [], "title": "t"},
    ) as probe_account:
        assert config_flow._probe({"any": "data"})["account_id"] == "a"

    assert probe_account.call_args.args == ({"any": "data"},)


# ---------------------------------------------------------------------------
# What the payload check has to catch before the network
#
# `_parse_qr_payload` used to verify that three keys were *present*. Presence is
# not the contract: `qrcode_login` runs `bytes.fromhex` on `login` and on
# `jeton` before the `try` that turns a decryption failure into
# `QRCodeDecryptError`, so a mangled hex string raises a bare `ValueError` from
# inside upstream -- not a `PronoteAPIError`, so past every classified arm, into
# the last-resort `except Exception`.
#
# Two things went wrong there, and only one of them was visible. The screen said
# "unexpected error" for a copy-paste problem; the limiter was told `TRANSPORT`,
# which opens an exponential backoff hold on the *instance-wide* guard and
# blocks every other config flow -- for an attempt that never opened a socket.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("payload", "why"),
    [
        pytest.param(
            {**QR_PAYLOAD, "login": "0badc0de0badc0d"},
            "an odd number of hex digits, which is what a truncated paste gives",
            id="login-truncated",
        ),
        pytest.param(
            {**QR_PAYLOAD, "jeton": "0bad 0bad 12:07 0bad"},
            "a messaging app inserting a timestamp into the pasted text",
            id="jeton-with-inserted-text",
        ),
        pytest.param(
            {**QR_PAYLOAD, "login": 1234},
            "a number where a hex string belongs",
            id="login-not-a-string",
        ),
        pytest.param(
            {**QR_PAYLOAD, "url": ""},
            "an empty address, which `urlparse` accepts and nothing can use",
            id="url-empty",
        ),
    ],
)
async def test_a_payload_of_the_right_shape_but_wrong_content_costs_nothing(
    hass: HomeAssistant,
    no_spacing: None,
    payload: dict[str, object],
    why: str,
) -> None:
    """The three keys were there, and the payload was still unusable.

    Each case is something a real paste produces, and each one used to reach
    ``bytes.fromhex`` inside pronotepy and come back as "unexpected error"
    *plus* a backoff hold on the guard every other config flow shares. Nothing
    here has any business touching the network, so the assertions are that the
    probe was never called and the budget never moved -- not merely that the
    message improved.
    """
    guard = login_guard(hass)
    before = guard.calls_today
    flow_id = await _start_qr(hass)

    with patch("custom_components.carnet_scolaire.config_flow._probe") as probe:
        again = await hass.config_entries.flow.async_configure(
            flow_id, {CONF_QR_PAYLOAD: json.dumps(payload), CONF_QR_PIN: "1234"}
        )

    assert again["errors"] == {CONF_QR_PAYLOAD: "invalid_qr_payload"}, why
    assert probe.call_count == 0, "nothing may reach the school for a bad payload"
    assert guard.calls_today == before, "and nothing may be charged for it"


async def test_a_wrong_four_digit_code_settles_the_charge_it_incurred(
    hass: HomeAssistant, qr_entry: MockConfigEntry, no_spacing: None
) -> None:
    """The charge cannot be refunded, so it must at least be *closed*.

    ``RateLimiter.login`` commits at admission -- inside the lock, before the
    call, and with no refund path -- and records the cost in ``_login_charged``
    for ``note_login`` to reconcile. The arm that handles a wrong four-digit
    code called no ``note_login`` at all, deliberately, on the belief that a
    purely local failure was not charged. It was: the flag stayed armed.

    An armed flag is not inert. ``note_login`` reads it as "this attempt was
    already paid for", so the next caller to report an outcome without having
    gone through ``login()`` -- which the limiter documents as the honest
    accounting for anything logging in outside the gate -- billed itself
    nothing. This test spends that second report and checks the budget moves.
    """
    result = await qr_entry.start_reauth_flow(hass)
    with patch(
        "custom_components.carnet_scolaire.config_flow._probe",
        side_effect=ProbeQrInvalid("invalid confirmation code"),
    ):
        await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_QR_PAYLOAD: json.dumps(QR_PAYLOAD), CONF_QR_PIN: "1234"},
        )

    guard = login_guard(hass)
    before = guard.calls_today
    guard.note_login(LoginOutcome.SUCCESS, requests_used=REQUESTS_PER_LOGIN)

    assert guard.calls_today - before == REQUESTS_PER_LOGIN


async def test_three_wrong_four_digit_codes_still_leave_the_door_open(
    hass: HomeAssistant, qr_entry: MockConfigEntry, no_spacing: None
) -> None:
    """The half of the exemption that must survive the fix above.

    Settling the charge must not turn a typo into a failed *login*: the
    three-attempt rail exists for credentials PRONOTE actually refused, and a
    four-digit code that fails locally never left the house. Three of them in a
    row must therefore still leave a fourth attempt possible -- which is the
    difference between a parent mistyping and a parent locked out of their own
    school account.
    """
    guard = login_guard(hass)
    for _ in range(3):
        result = await qr_entry.start_reauth_flow(hass)
        with patch(
            "custom_components.carnet_scolaire.config_flow._probe",
            side_effect=ProbeQrInvalid("invalid confirmation code"),
        ):
            await hass.config_entries.flow.async_configure(
                result["flow_id"],
                {CONF_QR_PAYLOAD: json.dumps(QR_PAYLOAD), CONF_QR_PIN: "1234"},
            )

    assert guard.may_login().allowed, "a typo is not a refused credential"


async def test_an_unknown_ent_provider_is_reported_on_the_provider_field(
    hass: HomeAssistant, no_spacing: None
) -> None:
    """The failure that used to send an ENT password to the wrong server.

    The provider selector accepts a typed value, and an unresolvable name
    returned ``None`` -- which reads as a graceful fallback and is the opposite
    of one: ``build_client`` then runs in direct mode and sends the *ENT
    portal's* username and password to the PRONOTE server. It cannot succeed, so
    the user was shown "invalid credentials" for a typo in a provider name, and
    one of the three slots on the IP guard was spent proving it.

    The error belongs on ``CONF_ENT`` and not on ``base``, because that is the
    single box to correct; the address, the username and the password are all
    fine.
    """
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "pronote"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "ent"}
    )

    guard = login_guard(hass)
    with patch(
        "custom_components.carnet_scolaire.config_flow._probe",
        side_effect=ProbeEntUnknown("ac_nowhere"),
    ):
        refused = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_PRONOTE_URL: PASTED_URL,
                "username": "parent-under-test",
                "password": "not-a-real-password",
                CONF_ENT: "ac_nowhere",
            },
        )

    assert refused["type"] is FlowResultType.FORM
    assert refused["step_id"] == "ent"
    assert refused["errors"] == {CONF_ENT: "unknown_ent"}
    # The credentials were never submitted to anybody, so the rail that guards
    # the address against refused logins must not have moved.
    assert guard.may_login().allowed


# ---------------------------------------------------------------------------
# Recovering from an error, one mode at a time
# ---------------------------------------------------------------------------
#
# Every other failure test above stops at the re-displayed form. That proves
# the error is reported and says nothing about the one thing the user does
# next: correct the field and submit again. A form that re-displays with the
# right message but loses a piece of the flow's state on the way -- the mode,
# the provider, the device UUID, the EcoleDirecte source -- fails on the
# *second* submit, which is exactly the one no test was making.


async def test_a_refused_password_can_be_corrected_and_the_entry_is_created(
    hass: HomeAssistant, no_spacing: None
) -> None:
    """The credentials form survives its own error and still creates the entry.

    Bronze ``config-flow-test-coverage`` asks for this path by name: the flow
    must recover from an error, not merely report it.
    """
    flow_id = await _start(hass)

    with patch(
        "custom_components.carnet_scolaire.config_flow._probe",
        side_effect=ProbeInvalidCredentials(),
    ):
        refused = await _submit_credentials(hass, flow_id)
    assert refused["type"] is FlowResultType.FORM
    assert refused["step_id"] == "credentials"
    assert refused["errors"] == {"base": "invalid_auth"}

    with patch(
        "custom_components.carnet_scolaire.config_flow._probe",
        return_value=_outcome(("STUDENT-1", "Enfant Un")),
    ):
        created = await _submit_credentials(hass, flow_id)

    assert created["type"] is FlowResultType.CREATE_ENTRY
    assert created["data"][CONF_LOGIN_MODE] == LoginMode.CREDENTIALS.value
    assert created["data"][CONF_PRONOTE_URL] == TRIMMED_URL
    assert created["data"][CONF_CHILDREN] == ["STUDENT-1"]


async def test_a_refused_ent_login_can_be_retried_and_keeps_its_provider(
    hass: HomeAssistant, no_spacing: None
) -> None:
    """The retry after an ENT refusal is still an ENT login, with its provider.

    ``test_a_failed_ent_login_comes_back_to_the_ent_form`` proves the right
    form comes back; this one proves the entry that follows is an ENT entry,
    which is what a lost provider would break -- silently, as a direct login.
    """
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "pronote"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "ent"}
    )
    form = {
        CONF_PRONOTE_URL: PASTED_URL,
        "username": "parent-under-test",
        "password": "not-a-real-password",
        CONF_ENT: "ac_reunion",
    }

    with patch(
        "custom_components.carnet_scolaire.config_flow._probe",
        side_effect=ProbeInvalidCredentials("the identity provider refused"),
    ):
        refused = await hass.config_entries.flow.async_configure(
            result["flow_id"], form
        )
    assert refused["errors"] == {"base": "invalid_auth"}

    with patch(
        "custom_components.carnet_scolaire.config_flow._probe",
        return_value=_outcome(("STUDENT-1", "Enfant Un")),
    ) as probe:
        created = await hass.config_entries.flow.async_configure(
            result["flow_id"], form
        )

    assert created["type"] is FlowResultType.CREATE_ENTRY
    assert created["data"][CONF_LOGIN_MODE] == LoginMode.ENT.value
    assert created["data"][CONF_ENT] == "ac_reunion"
    assert probe.call_args.args[0][CONF_ENT] == "ac_reunion"


async def test_a_refused_qr_code_can_be_replaced_and_the_entry_is_created(
    hass: HomeAssistant, no_spacing: None
) -> None:
    """A second QR code on the same form enrols, and still keeps no secret.

    The first QR code is spent either way, so a new one on the same form is
    the ordinary recovery the step's own text prescribes. The retry must not
    carry the first attempt's payload or four-digit code into the entry.
    """
    flow_id = await _start_qr(hass)
    submitted = {CONF_QR_PAYLOAD: json.dumps(QR_PAYLOAD), CONF_QR_PIN: "1234"}

    with patch(
        "custom_components.carnet_scolaire.config_flow._probe",
        side_effect=ProbeQrInvalid(),
    ):
        refused = await hass.config_entries.flow.async_configure(flow_id, submitted)
    assert refused["type"] is FlowResultType.FORM
    assert refused["step_id"] == "qr_code"
    assert refused["errors"] == {"base": "invalid_qr"}

    with patch(
        "custom_components.carnet_scolaire.config_flow._probe",
        return_value=_outcome(("STUDENT-1", "Enfant Un")),
    ):
        created = await hass.config_entries.flow.async_configure(flow_id, submitted)

    assert created["type"] is FlowResultType.CREATE_ENTRY
    data = created["data"]
    assert data[CONF_LOGIN_MODE] == LoginMode.QR_CODE.value
    assert data[CONF_UUID]
    assert CONF_QR_PAYLOAD not in data
    assert CONF_QR_PIN not in data


async def test_a_refused_ecoledirecte_login_can_be_corrected_and_is_created(
    hass: HomeAssistant, no_spacing: None
) -> None:
    """EcoleDirecte re-displays its own form, and the retry creates its entry.

    ``_async_try_ecoledirecte`` rebuilds the form itself on failure, apart from
    the PRONOTE re-display path, so it needs its own recovery test: an entry
    created without ``source: ecoledirecte`` would be set up as PRONOTE.
    """
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "ecoledirecte"}
    )
    form = {"username": "demo.example.invalid", "password": "not-a-real-password"}

    with patch(
        "custom_components.carnet_scolaire.config_flow._probe_ecoledirecte",
        side_effect=ConnectorCredentialsError(),
    ):
        refused = await hass.config_entries.flow.async_configure(
            result["flow_id"], form
        )
    assert refused["type"] is FlowResultType.FORM
    assert refused["step_id"] == "ecoledirecte"
    assert refused["errors"] == {"base": "ed_invalid_auth"}

    with patch(
        "custom_components.carnet_scolaire.config_flow._probe_ecoledirecte",
        return_value={"students": (("1", "Enfant Un"),)},
    ):
        created = await hass.config_entries.flow.async_configure(
            result["flow_id"], form
        )

    assert created["type"] is FlowResultType.CREATE_ENTRY
    assert created["data"][CONF_SOURCE] == "ecoledirecte"
    assert created["result"].unique_id == "ecoledirecte:demo.example.invalid"


async def test_the_same_ecoledirecte_account_cannot_be_added_twice(
    hass: HomeAssistant, no_spacing: None
) -> None:
    """EcoleDirecte has its own ``unique_id``, so it needs its own duplicate test.

    ``test_the_same_account_cannot_be_added_twice`` covers the PRONOTE
    identifier. EcoleDirecte's is built differently -- the source and the
    username -- and a duplicate here would double every entity and every
    automation just the same.
    """
    MockConfigEntry(
        domain=DOMAIN,
        unique_id="ecoledirecte:demo.example.invalid",
        data={
            CONF_SOURCE: "ecoledirecte",
            "username": "demo.example.invalid",
            "password": "not-a-real-password",
        },
    ).add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "ecoledirecte"}
    )
    with patch(
        "custom_components.carnet_scolaire.config_flow._probe_ecoledirecte",
        return_value={"students": (("1", "Enfant Un"),)},
    ):
        aborted = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"username": "demo.example.invalid", "password": "not-a-real-password"},
        )

    assert aborted["type"] is FlowResultType.ABORT
    assert aborted["reason"] == "already_configured"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


# ---------------------------------------------------------------------------
# Reconfiguration
#
# Removing and re-adding the entry used to be the only way to correct an
# address, move to the ENT or to a QR code -- and it took the devices, the
# entity ids, the automations written against them, the options and the history
# with it. Reconfiguration keeps the entry and swaps only how it connects, which
# makes two failures possible that the add flow cannot have: applying a login
# that opened *another* account, and losing on the way something the entry
# already held. Every test below is about one of those two.
# ---------------------------------------------------------------------------

#: The address as a mistaken first set-up stored it: the mobile page, which a
#: parent copies from a link on the phone. Same host and same account as
#: `TRIMMED_URL` -- the correction reconfiguration exists for.
MISTAKEN_URL = "https://demo.example.invalid/pronote/mobile.parent.html"

RECONFIGURE_OPTIONS: dict[str, Any] = {
    OPT_WRITE_OPERATIONS_ENABLED: False,
    "tier_interval_marks": 7200,
}


@pytest.fixture(name="ent_entry")
def ent_entry_fixture(hass: HomeAssistant) -> MockConfigEntry:
    """An ENT entry, set up against the mistaken address, following one child."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="PRONOTE",
        data={
            CONF_PRONOTE_URL: MISTAKEN_URL,
            CONF_LOGIN_MODE: LoginMode.ENT.value,
            "username": "portal-user-under-test",
            "password": "not-a-real-portal-password",
            CONF_ENT: "ac_reunion",
            CONF_CHILDREN: ["STUDENT-2"],
            CONF_CHILD_KEYS: [
                {"key": "child-2", "resource_id": "STUDENT-2", "name": "Enfant Deux"}
            ],
        },
        options=dict(RECONFIGURE_OPTIONS),
        unique_id=f"{MISTAKEN_URL}::46#not-a-real-signature",
    )
    entry.add_to_hass(hass)
    return entry


def _reconfigured_outcome(
    account_id: str = f"{TRIMMED_URL}::46#a-rotated-signature",
    children: list[tuple[str, str]] | None = None,
    account_name: str = "Parent Fictif",
) -> dict[str, Any]:
    """What the probe hands back after a reconfigured login, children included.

    Two children, although the entry follows one: a finish that merged these
    would widen the selection, which is what the assertions on `children` catch.
    """
    return {
        "account_id": account_id,
        "account_name": account_name,
        "children": (
            children
            if children is not None
            else [("STUDENT-1", "Enfant Un"), ("STUDENT-2", "Enfant Deux")]
        ),
        "title": "PRONOTE",
        "username": "parent-under-test",
        "password": "not-a-real-password",
        CONF_CLIENT_IDENTIFIER: "not-a-real-fresh-client-id",
    }


def _suggested(result: Any, key: str) -> Any:
    """The value a form pre-fills in ``key``, or ``None``."""
    for marker in result["data_schema"].schema:
        if str(marker) == key:
            return (marker.description or {}).get("suggested_value")
    return None


async def _reconfigure_to(
    hass: HomeAssistant, entry: MockConfigEntry, method: str
) -> dict[str, Any]:
    """Open a PRONOTE reconfiguration and pick one of the three methods."""
    result = await entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "reconfigure"
    return dict(
        await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": method}
        )
    )


async def test_reconfiguring_offers_the_three_methods_and_names_the_address(
    hass: HomeAssistant, ent_entry: MockConfigEntry
) -> None:
    """The menu is the add flow's, and it says which account is being changed.

    The address is the trimmed form, because a placeholder is rendered on
    screen and a stored query string is exactly what must never be shown.
    """
    result = await ent_entry.start_reconfigure_flow(hass)

    assert result["type"] is FlowResultType.MENU
    assert set(result["menu_options"]) == {"qr_code", "credentials", "ent"}
    assert (result["description_placeholders"] or {})["url"] == MISTAKEN_URL


async def test_a_reconfigured_address_is_trimmed_and_nothing_else_is_lost(
    hass: HomeAssistant, ent_entry: MockConfigEntry, no_spacing: None
) -> None:
    """The ordinary case: same method, corrected address, entry kept whole.

    Checks the promises at once, because each of them fails silently: the
    pasted query string does not reach the entry, the options -- writes off, a
    tuned tier -- are the user's and not the flow's, and the followed child
    stays the only one even though the probe announces two.
    """
    form = await _reconfigure_to(hass, ent_entry, "ent")
    assert form["step_id"] == "ent"
    # Pre-filled from the entry, except the password, which is never sent
    # back to a browser.
    assert _suggested(form, CONF_PRONOTE_URL) == MISTAKEN_URL
    assert _suggested(form, "username") == "portal-user-under-test"
    assert _suggested(form, CONF_ENT) == "ac_reunion"
    assert _suggested(form, "password") is None

    with patch(
        "custom_components.carnet_scolaire.config_flow._probe",
        return_value=_reconfigured_outcome(),
    ) as probe:
        done = await hass.config_entries.flow.async_configure(
            form["flow_id"],
            {
                CONF_PRONOTE_URL: PASTED_URL,
                "username": "portal-user-under-test",
                "password": "not-a-real-portal-password",
                CONF_ENT: "ac_reunion",
            },
        )
        await hass.async_block_till_done()

    assert done["type"] is FlowResultType.ABORT
    assert done["reason"] == "reconfigure_successful"
    assert probe.call_count == 1
    assert probe.call_args.args[0][CONF_PRONOTE_URL] == TRIMMED_URL
    assert ent_entry.data[CONF_PRONOTE_URL] == TRIMMED_URL
    assert ent_entry.data[CONF_ENT] == "ac_reunion"
    assert ent_entry.data[CONF_CLIENT_IDENTIFIER] == "not-a-real-fresh-client-id"
    assert ent_entry.data[CONF_CHILDREN] == ["STUDENT-2"]
    assert ent_entry.data[CONF_CHILD_KEYS][0]["key"] == "child-2"
    assert dict(ent_entry.options) == RECONFIGURE_OPTIONS
    # The stored identity is left alone, as re-authentication leaves it.
    assert ent_entry.unique_id == f"{MISTAKEN_URL}::46#not-a-real-signature"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


async def test_moving_off_the_ent_drops_the_portal_it_logged_in_through(
    hass: HomeAssistant, ent_entry: MockConfigEntry, no_spacing: None
) -> None:
    """A mode change must not leave the old mode's keys behind.

    `flow_login` picks the ENT whenever the entry names one, so an entry moved
    to direct credentials that kept its `ent` would go on sending the new
    PRONOTE password to the regional portal -- reported as a refused login, at
    every restart, with the right password in hand. And the PIN given for
    this one login is not kept (§8.1).
    """
    form = await _reconfigure_to(hass, ent_entry, "credentials")
    assert form["step_id"] == "credentials"
    assert _suggested(form, CONF_PRONOTE_URL) == MISTAKEN_URL

    with patch(
        "custom_components.carnet_scolaire.config_flow._probe",
        return_value=_reconfigured_outcome(),
    ) as probe:
        done = await hass.config_entries.flow.async_configure(
            form["flow_id"],
            {
                CONF_PRONOTE_URL: TRIMMED_URL,
                "username": "parent-under-test",
                "password": "not-a-real-password",
                CONF_ACCOUNT_PIN: "0000",
            },
        )
        await hass.async_block_till_done()

    assert done["reason"] == "reconfigure_successful"
    assert probe.call_args.args[0][CONF_ACCOUNT_PIN] == "0000"
    assert ent_entry.data[CONF_LOGIN_MODE] == LoginMode.CREDENTIALS.value
    assert CONF_ENT not in ent_entry.data
    assert CONF_ACCOUNT_PIN not in ent_entry.data
    assert "account_id" not in ent_entry.data
    assert "account_name" not in ent_entry.data
    assert ent_entry.data["password"] == "not-a-real-password"


async def test_a_refused_reconfiguration_says_why_and_changes_nothing_until_it_works(
    hass: HomeAssistant, ent_entry: MockConfigEntry, no_spacing: None
) -> None:
    """The probe comes first, and a refusal re-displays the form, pre-filled.

    Saving first and discovering the mistake at the reload would leave a
    working entry broken by a typo -- the opposite of what the form is for.
    One attempt per submission, as everywhere in this flow.
    """
    before = dict(ent_entry.data)
    form = await _reconfigure_to(hass, ent_entry, "credentials")

    with patch(
        "custom_components.carnet_scolaire.config_flow._probe",
        side_effect=[ProbeInvalidCredentials(), _reconfigured_outcome()],
    ) as probe:
        refused = await hass.config_entries.flow.async_configure(
            form["flow_id"],
            {
                CONF_PRONOTE_URL: TRIMMED_URL,
                "username": "parent-under-test",
                "password": "a-typo-not-real",
            },
        )
        assert refused["type"] is FlowResultType.FORM
        assert refused["step_id"] == "credentials"
        assert refused["errors"] == {"base": "invalid_auth"}
        assert _suggested(refused, "username") == "portal-user-under-test"
        assert dict(ent_entry.data) == before

        done = await hass.config_entries.flow.async_configure(
            refused["flow_id"],
            {
                CONF_PRONOTE_URL: TRIMMED_URL,
                "username": "parent-under-test",
                "password": "not-a-real-password",
            },
        )
        await hass.async_block_till_done()

    assert probe.call_count == 2
    assert done["reason"] == "reconfigure_successful"
    assert ent_entry.data["password"] == "not-a-real-password"


async def test_a_refused_ent_reconfiguration_comes_back_to_the_ent_form(
    hass: HomeAssistant, ent_entry: MockConfigEntry, no_spacing: None
) -> None:
    """The re-displayed form is the one the user was on, still pre-filled."""
    form = await _reconfigure_to(hass, ent_entry, "ent")

    with patch(
        "custom_components.carnet_scolaire.config_flow._probe",
        side_effect=ProbeInvalidCredentials(),
    ):
        refused = await hass.config_entries.flow.async_configure(
            form["flow_id"],
            {
                CONF_PRONOTE_URL: TRIMMED_URL,
                "username": "portal-user-under-test",
                "password": "a-typo-not-real",
                CONF_ENT: "ac_reunion",
            },
        )

    assert refused["step_id"] == "ent"
    assert refused["errors"] == {"base": "invalid_auth"}
    assert _suggested(refused, CONF_ENT) == "ac_reunion"


@pytest.mark.parametrize(
    ("account_id", "children"),
    [
        # Another family at the same establishment. The account identifier
        # reads the same -- same host, and the `46#` prefix every child
        # carries -- so only the children tell the accounts apart.
        (
            f"{TRIMMED_URL}::46#another-family-signature",
            [("OTHER-1", "Autre Enfant")],
        ),
        # This entry's child's name, but at another establishment.
        (
            "https://other.example.invalid/pronote/parent.html::46#not-a-real-sig",
            [("STUDENT-2", "Enfant Deux")],
        ),
    ],
)
async def test_a_reconfiguration_that_opens_another_account_is_refused(
    hass: HomeAssistant,
    ent_entry: MockConfigEntry,
    no_spacing: None,
    account_id: str,
    children: list[tuple[str, str]],
) -> None:
    """The failure reconfiguration makes possible, and must not commit.

    Two parents of one school each hold working credentials, and a parent with
    children in two schools holds two sets. Typing the other set succeeds as a
    login, and applying it would keep this entry's devices, entity ids and
    history while quietly following somebody else's child. So the login must
    reach the entry's host *and* announce a child the entry already records,
    and nothing is written when it does not.
    """
    before = dict(ent_entry.data)
    form = await _reconfigure_to(hass, ent_entry, "credentials")

    with patch(
        "custom_components.carnet_scolaire.config_flow._probe",
        return_value=_reconfigured_outcome(account_id=account_id, children=children),
    ):
        done = await hass.config_entries.flow.async_configure(
            form["flow_id"],
            {
                CONF_PRONOTE_URL: TRIMMED_URL,
                "username": "parent-under-test",
                "password": "not-a-real-password",
            },
        )
        await hass.async_block_till_done()

    assert done["type"] is FlowResultType.ABORT
    assert done["reason"] == "reconfigure_wrong_account"
    assert dict(ent_entry.data) == before
    assert dict(ent_entry.options) == RECONFIGURE_OPTIONS


async def test_a_qr_reconfiguration_keeps_the_device_and_no_single_use_secret(
    hass: HomeAssistant, qr_entry: MockConfigEntry, no_spacing: None
) -> None:
    """A fresh QR code on an enrolled entry: same device, new token, no payload.

    The UUID is reused because upstream requires it not to change between
    logins (`async_step_reauth_qr` explains); the payload, its four-digit code
    and the account PIN are single-use and §8.1 promises they are not kept.
    """
    form = await _reconfigure_to(hass, qr_entry, "qr_code")
    assert form["step_id"] == "qr_code"

    with patch(
        "custom_components.carnet_scolaire.config_flow._probe",
        return_value=_qr_outcome(),
    ) as probe:
        done = await hass.config_entries.flow.async_configure(
            form["flow_id"],
            {
                CONF_QR_PAYLOAD: json.dumps(QR_PAYLOAD),
                CONF_QR_PIN: "1234",
                CONF_ACCOUNT_PIN: "0000",
            },
        )
        await hass.async_block_till_done()

    assert done["reason"] == "reconfigure_successful"
    assert probe.call_args.args[0][CONF_UUID] == QR_ENTRY_DATA[CONF_UUID]
    assert qr_entry.data[CONF_UUID] == QR_ENTRY_DATA[CONF_UUID]
    assert qr_entry.data["password"] == "not-a-real-fresh-token"
    for secret in (CONF_QR_PAYLOAD, CONF_QR_PIN, CONF_ACCOUNT_PIN, "account_id"):
        assert secret not in qr_entry.data
    assert qr_entry.data[CONF_CHILDREN] == QR_ENTRY_DATA[CONF_CHILDREN]


async def test_moving_a_password_entry_to_a_qr_code_enrols_a_new_device(
    hass: HomeAssistant, ent_entry: MockConfigEntry, no_spacing: None
) -> None:
    """An entry with no device identity gets one, as the add flow gives it.

    The other half of the UUID rule: reuse one if the entry has it, never send
    an empty one -- `qrcode_login` would enrol a device with no identifier.
    """
    form = await _reconfigure_to(hass, ent_entry, "qr_code")

    with patch(
        "custom_components.carnet_scolaire.config_flow._probe",
        return_value=_qr_outcome(account_id=f"{TRIMMED_URL}::46#not-a-real-signature"),
    ) as probe:
        done = await hass.config_entries.flow.async_configure(
            form["flow_id"],
            {CONF_QR_PAYLOAD: json.dumps(QR_PAYLOAD), CONF_QR_PIN: "1234"},
        )
        await hass.async_block_till_done()

    assert done["reason"] == "reconfigure_successful"
    assert probe.call_args.args[0][CONF_UUID]
    assert ent_entry.data[CONF_LOGIN_MODE] == LoginMode.QR_CODE.value
    assert CONF_ENT not in ent_entry.data


async def test_a_qr_entry_moved_to_a_password_is_not_offered_its_enrolment_login(
    hass: HomeAssistant, qr_entry: MockConfigEntry
) -> None:
    """On a QR entry the stored username is the enrolment's, not the person's.

    Suggesting it on the password form would invite submitting a login the
    PRONOTE page has never heard of -- and spending an attempt on the guard
    rail doing it.
    """
    form = await _reconfigure_to(hass, qr_entry, "credentials")

    assert _suggested(form, "username") is None
    assert _suggested(form, CONF_PRONOTE_URL) == TRIMMED_URL


async def test_a_successful_reconfiguration_lifts_the_hold_of_the_old_details(
    hass: HomeAssistant, ent_entry: MockConfigEntry, no_spacing: None
) -> None:
    """The typical entry being reconfigured is one that stopped logging in.

    Its saved limiter state carries the hold those failures earned, and the
    reload that applies the correction reads it back: left in place, it refuses
    the proven new details until it expires -- or, for the MFA hold, for good.
    """
    limiter_state_store(hass)[ent_entry.entry_id] = {"held": "by-the-old-details"}
    form = await _reconfigure_to(hass, ent_entry, "credentials")

    with patch(
        "custom_components.carnet_scolaire.config_flow._probe",
        return_value=_reconfigured_outcome(),
    ):
        await hass.config_entries.flow.async_configure(
            form["flow_id"],
            {
                CONF_PRONOTE_URL: TRIMMED_URL,
                "username": "parent-under-test",
                "password": "not-a-real-password",
            },
        )
        await hass.async_block_till_done()

    assert ent_entry.entry_id not in limiter_state_store(hass)


@pytest.fixture(name="ed_entry")
def ed_entry_fixture(hass: HomeAssistant) -> MockConfigEntry:
    """An EcoleDirecte entry with a remembered QCM answer and tuned options."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="EcoleDirecte",
        unique_id="ecoledirecte:demo.example.invalid",
        data={
            CONF_SOURCE: "ecoledirecte",
            "username": "demo.example.invalid",
            "password": "old-password-not-real",
            "qcm_json": {"Couleur préférée ?": "Bleu"},
            CONF_CHILD_KEYS: [
                {"key": "child-1", "resource_id": "1", "name": "Enfant Un"}
            ],
        },
        options={"tier_interval_marks": 7200},
    )
    entry.add_to_hass(hass)
    return entry


async def test_an_ecoledirecte_entry_is_reconfigured_through_its_own_form(
    hass: HomeAssistant, ed_entry: MockConfigEntry, no_spacing: None
) -> None:
    """One way in, so no menu: the add flow's form, pre-filled, then the entry.

    The remembered QCM answer goes with the attempt, as at every collection, so
    a question already answered is not asked again; and the PRONOTE probe is
    never reached, because an EcoleDirecte entry has nothing to say to it.
    """
    form = await ed_entry.start_reconfigure_flow(hass)
    assert form["type"] is FlowResultType.FORM
    assert form["step_id"] == "ecoledirecte"
    assert _suggested(form, "username") == "demo.example.invalid"
    assert _suggested(form, "password") is None

    with (
        patch(
            "custom_components.carnet_scolaire.config_flow._probe",
            side_effect=AssertionError("no PRONOTE probe for an ED entry"),
        ),
        patch(
            "custom_components.carnet_scolaire.config_flow._probe_ecoledirecte",
            return_value={"students": (("1", "Enfant Un"),)},
        ) as probe,
    ):
        done = await hass.config_entries.flow.async_configure(
            form["flow_id"],
            {"username": "demo.example.invalid", "password": "not-a-real-password"},
        )
        await hass.async_block_till_done()

    assert done["type"] is FlowResultType.ABORT
    assert done["reason"] == "reconfigure_successful"
    assert probe.call_args.args[3] == {"Couleur préférée ?": "Bleu"}
    assert ed_entry.data["password"] == "not-a-real-password"
    assert ed_entry.data[CONF_CHILD_KEYS][0]["key"] == "child-1"
    assert dict(ed_entry.options) == {"tier_interval_marks": 7200}


async def test_another_ecoledirecte_identifier_is_refused_before_any_login(
    hass: HomeAssistant, ed_entry: MockConfigEntry, no_spacing: None
) -> None:
    """The identifier *is* the identity, so a different one is known in advance.

    Checked with Home Assistant's own unique-id mismatch rule, and before the
    probe rather than after it: nothing needs to be sent to learn the answer,
    and a login spent learning it would draw on the daily allowance.
    """
    before = dict(ed_entry.data)
    form = await ed_entry.start_reconfigure_flow(hass)

    with patch(
        "custom_components.carnet_scolaire.config_flow._probe_ecoledirecte",
        return_value={"students": (("1", "Enfant Un"),)},
    ) as probe:
        done = await hass.config_entries.flow.async_configure(
            form["flow_id"],
            {"username": "other.example.invalid", "password": "not-a-real-password"},
        )

    assert done["type"] is FlowResultType.ABORT
    assert done["reason"] == "reconfigure_wrong_account"
    assert probe.call_count == 0
    assert dict(ed_entry.data) == before


async def test_an_ecoledirecte_reconfiguration_can_pass_through_the_qcm(
    hass: HomeAssistant, ed_entry: MockConfigEntry, no_spacing: None
) -> None:
    """A new question on the way is answered in the flow, then saved with it.

    And the entry is not touched while the question is pending: the answer is
    only kept once the login it belongs to has succeeded.
    """
    before = dict(ed_entry.data)
    form = await ed_entry.start_reconfigure_flow(hass)
    with patch(
        "custom_components.carnet_scolaire.config_flow._probe_ecoledirecte",
        side_effect=ConnectorChallengeRequired(
            ChallengeKind.QCM,
            question="Ville de naissance ?",
            propositions=("Lyon", "Nantes"),
        ),
    ):
        challenged = await hass.config_entries.flow.async_configure(
            form["flow_id"],
            {"username": "demo.example.invalid", "password": "not-a-real-password"},
        )
    assert challenged["step_id"] == "ecoledirecte_qcm"
    assert dict(ed_entry.data) == before

    with patch(
        "custom_components.carnet_scolaire.config_flow._probe_ecoledirecte",
        return_value={"students": (("1", "Enfant Un"),)},
    ):
        done = await hass.config_entries.flow.async_configure(
            challenged["flow_id"], {"choice": "Nantes"}
        )
        await hass.async_block_till_done()

    assert done["reason"] == "reconfigure_successful"
    assert ed_entry.data["qcm_json"] == {"Ville de naissance ?": "Nantes"}
    assert ed_entry.data["password"] == "not-a-real-password"


async def test_a_refused_ecoledirecte_reconfiguration_keeps_the_entry_as_it_was(
    hass: HomeAssistant, ed_entry: MockConfigEntry, no_spacing: None
) -> None:
    """A wrong password re-displays the pre-filled form and writes nothing."""
    before = dict(ed_entry.data)
    form = await ed_entry.start_reconfigure_flow(hass)
    with patch(
        "custom_components.carnet_scolaire.config_flow._probe_ecoledirecte",
        side_effect=ConnectorCredentialsError(),
    ):
        refused = await hass.config_entries.flow.async_configure(
            form["flow_id"],
            {"username": "demo.example.invalid", "password": "a-typo-not-real"},
        )

    assert refused["type"] is FlowResultType.FORM
    assert refused["errors"] == {"base": "ed_invalid_auth"}
    assert _suggested(refused, "username") == "demo.example.invalid"
    assert dict(ed_entry.data) == before


# ---------------------------------------------------------------------------
# Who the account behind a login belongs to
#
# The account identifier cannot say it. `flow_login._account_id` ends in
# PRONOTE's `N`, written `46#<signature>`: the signature changes at every
# session, and the `46` is the same for every child of every account -- a
# resource type, not a number of its own. Two families at one school therefore
# read the same, so the reconnection paths also require a child the entry
# already records. The tests below are that rule, and its fallback.
# ---------------------------------------------------------------------------

#: The establishment-local form every child of every account shares.
SHARED_PREFIX_ID = f"{TRIMMED_URL}::46#not-a-real-signature"


def _named_entry(
    hass: HomeAssistant, names: list[str], *, mode: str = LoginMode.CREDENTIALS.value
) -> MockConfigEntry:
    """A PRONOTE entry whose key table records these children's names."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="PRONOTE",
        data={
            CONF_PRONOTE_URL: TRIMMED_URL,
            CONF_LOGIN_MODE: mode,
            "username": "parent-under-test",
            "password": "not-a-real-password",
            CONF_CHILDREN: [f"STUDENT-{index}" for index, _ in enumerate(names, 1)],
            CONF_CHILD_KEYS: [
                {
                    "key": f"child-{index}",
                    "resource_id": f"STUDENT-{index}",
                    "name": name,
                }
                for index, name in enumerate(names, 1)
            ],
        },
        unique_id=SHARED_PREFIX_ID,
    )
    entry.add_to_hass(hass)
    return entry


async def test_a_password_reconnection_to_another_family_is_refused(
    hass: HomeAssistant, no_spacing: None, caplog: pytest.LogCaptureFixture
) -> None:
    """The address and the username are the entry's; the account need not be.

    A pupil's login handed down to a sibling, a parent space the school
    reassigned: the password form can reach another family with nothing typed
    wrong. Same host, same `46#` prefix, none of the entry's children -- so it
    is refused, nothing is written, and the log says so without a single name.
    """
    caplog.set_level(logging.DEBUG, logger="custom_components.carnet_scolaire")
    entry = _named_entry(hass, ["Enfant Un"])
    before = dict(entry.data)
    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"

    with patch(
        "custom_components.carnet_scolaire.config_flow._probe",
        return_value={
            **_outcome(("OTHER-1", "Autre Enfant")),
            "account_id": f"{TRIMMED_URL}::46#another-family-signature",
        },
    ):
        done = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"password": "corrected-not-real"}
        )

    assert done["type"] is FlowResultType.ABORT
    assert done["reason"] == "wrong_account"
    assert dict(entry.data) == before
    assert "reached none of the children" in caplog.text
    for name in ("Enfant Un", "Autre Enfant"):
        assert name not in caplog.text


async def test_a_reconnection_sharing_one_child_is_the_same_account(
    hass: HomeAssistant, no_spacing: None
) -> None:
    """One child in common is enough, and the comparison forgives layout only.

    A parent who gained a second child since set-up is still the same parent,
    so the new login may announce more children than the entry records. And
    PRONOTE's rendering of a name may differ in case or spacing from the one
    recorded months ago -- which is all the normalisation absorbs.
    """
    entry = _named_entry(hass, ["Enfant  Deux"])
    result = await entry.start_reauth_flow(hass)

    with patch(
        "custom_components.carnet_scolaire.config_flow._probe",
        return_value={
            **_outcome(("NEW-1", "Enfant Un"), ("NEW-2", "ENFANT DEUX ")),
            "account_id": f"{TRIMMED_URL}::46#a-rotated-signature",
        },
    ):
        done = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"password": "corrected-not-real"}
        )
        await hass.async_block_till_done()

    assert done["reason"] == "reauth_successful"
    assert entry.data["password"] == "not-a-real-password"


@pytest.mark.parametrize(
    ("announced", "reason"),
    [
        ("Enfant Un", "reconfigure_successful"),
        (" enfant   UN", "reconfigure_successful"),
        ("Autre Enfant", "reconfigure_wrong_account"),
    ],
)
async def test_a_pupil_account_is_recognised_by_the_pupil_s_own_name(
    hass: HomeAssistant, no_spacing: None, announced: str, reason: str
) -> None:
    """A pupil's login announces no child, only the account holder.

    So the name on the account must be the one child the entry follows. A
    classmate's login at the same school reads the same in the account
    identifier, and must not take this pupil's history over.
    """
    entry = _named_entry(hass, ["Enfant Un"])
    before = dict(entry.data)
    form = await _reconfigure_to(hass, entry, "credentials")

    with patch(
        "custom_components.carnet_scolaire.config_flow._probe",
        return_value=_reconfigured_outcome(
            account_id=SHARED_PREFIX_ID, children=[], account_name=announced
        ),
    ):
        done = await hass.config_entries.flow.async_configure(
            form["flow_id"],
            {
                CONF_PRONOTE_URL: TRIMMED_URL,
                "username": "pupil-under-test",
                "password": "not-a-real-password",
            },
        )
        await hass.async_block_till_done()

    assert done["reason"] == reason
    if reason == "reconfigure_wrong_account":
        assert dict(entry.data) == before
    else:
        assert "account_name" not in entry.data


async def test_a_pupil_login_does_not_pass_for_a_parent_entry(
    hass: HomeAssistant, no_spacing: None
) -> None:
    """A parent entry follows several children; a pupil's login is one of them.

    Accepting it would silently turn a family's entry into one child's, losing
    the others. The rule asks for *the* one followed child, so it is refused.
    """
    entry = _named_entry(hass, ["Enfant Un", "Enfant Deux"])
    form = await _reconfigure_to(hass, entry, "credentials")

    with patch(
        "custom_components.carnet_scolaire.config_flow._probe",
        return_value=_reconfigured_outcome(
            account_id=SHARED_PREFIX_ID, children=[], account_name="Enfant Un"
        ),
    ):
        done = await hass.config_entries.flow.async_configure(
            form["flow_id"],
            {
                CONF_PRONOTE_URL: TRIMMED_URL,
                "username": "pupil-under-test",
                "password": "not-a-real-password",
            },
        )

    assert done["reason"] == "reconfigure_wrong_account"


async def test_an_entry_without_a_recorded_name_falls_back_to_the_address(
    hass: HomeAssistant, no_spacing: None
) -> None:
    """An entry with nothing to compare is not refused for it.

    An entry created before the key table existed, and never set up since,
    records no name. Refusing every reconnection on it would leave its owner
    one exit -- deleting it -- which is the outcome the check exists to
    prevent. So it keeps the older, weaker test: the establishment alone. This
    test pins that fallback, knowingly weaker, rather than letting it drift.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="PRONOTE",
        data={
            CONF_PRONOTE_URL: TRIMMED_URL,
            CONF_LOGIN_MODE: LoginMode.CREDENTIALS.value,
            "username": "parent-under-test",
            "password": "not-a-real-password",
            CONF_CHILDREN: ["STUDENT-1"],
        },
        unique_id=SHARED_PREFIX_ID,
    )
    entry.add_to_hass(hass)
    form = await _reconfigure_to(hass, entry, "credentials")

    with patch(
        "custom_components.carnet_scolaire.config_flow._probe",
        return_value=_reconfigured_outcome(
            account_id=f"{TRIMMED_URL}::46#a-rotated-signature",
            children=[("OTHER-1", "Autre Enfant")],
        ),
    ):
        done = await hass.config_entries.flow.async_configure(
            form["flow_id"],
            {
                CONF_PRONOTE_URL: TRIMMED_URL,
                "username": "parent-under-test",
                "password": "not-a-real-password",
            },
        )
        await hass.async_block_till_done()

    assert done["reason"] == "reconfigure_successful"
