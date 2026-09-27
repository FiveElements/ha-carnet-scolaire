"""Every path that reloads a *running* entry, counted, and none of them deprecated.

Three things reload an account that is up: saving the options page, a
re-authentication, and a reconfiguration. A fourth thing writes to the entry
while it runs and must **not** reload it -- the account persisting a rotated
token or a newly paired child into ``entry.data``.

Home Assistant 2026.9 reports ``async_update_reload_and_abort`` on an entry that
carries an update listener as a mistake, with ``breaks_in_ha_version=
"2026.12.0"``. The finishers of the re-authentication and reconfiguration flows
call exactly that, and the integration used to register an update listener to
reload on an options save -- so every reconnection of a *loaded* entry logged
the deprecation, and was scheduled to break at 2026.12. The config-flow suite
could not see it: its autouse ``no_setup`` patches ``async_setup_entry`` away,
so no entry there ever registered a listener. These tests run against the real
set-up, through the ``account`` fixture, which is the only state in which the
warning fires.

The reload count is asserted alongside, because the obvious ways to silence the
warning each break it in one direction: keep the listener and let it reload on a
data write, and the account restarts every time it saves a rotated token; drop
the reload from the finisher, and a corrected password is stored and never
used.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.data_entry_flow import FlowResultType
import pytest

from custom_components.carnet_scolaire.const import (
    CONF_PRONOTE_URL,
    OPT_MASTER_TICK,
)

from .conftest import CHILDREN, REQUIRES_HASS
from .test_options_flow import _form_defaults, _open

if TYPE_CHECKING:
    from collections.abc import Iterator
    from unittest.mock import MagicMock

    from homeassistant.core import HomeAssistant

    from custom_components.carnet_scolaire.account import PronoteAccount

pytestmark = REQUIRES_HASS

#: The phrase Home Assistant's ``report_usage`` logs for this deprecation.
DEPRECATION = "has an update listener"

ESTABLISHMENT = "https://demo.example.invalid/pronote/parent.html"


@pytest.fixture(name="reloads")
def reloads_fixture(hass: HomeAssistant) -> Iterator[MagicMock]:
    """Count every reload of any entry, whoever asks for it.

    Wrapped on the instance, because every route to a reload -- a flow's
    ``async_schedule_reload``, an options flow's automatic reload, an update
    listener -- ends in ``hass.config_entries.async_reload``.
    """
    with patch.object(
        hass.config_entries,
        "async_reload",
        wraps=hass.config_entries.async_reload,
    ) as spy:
        yield spy


def _probe_outcome(account_id: str) -> dict[str, Any]:
    """What the login probe hands back, with the fixture's two children."""
    return {
        "account_id": account_id,
        "children": list(CHILDREN),
        "title": "PRONOTE",
        "username": "parent-under-test",
        "password": "not-a-real-rotated-password",
    }


def _assert_reloaded_once(
    account: PronoteAccount, reloads: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    entry = account.entry
    assert DEPRECATION not in caplog.text
    assert reloads.call_count == 1
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data is not account, "the entry was not rebuilt"


async def test_a_reauthentication_of_a_running_entry_reloads_once_without_deprecation(
    hass: HomeAssistant,
    account: PronoteAccount,
    reloads: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The reconnection a live instance actually performs.

    A token refused mid-run starts the re-authentication while the entry is
    still loaded, so this is the ordinary case and not a corner of it.
    """
    result = await account.entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"

    with patch(
        "custom_components.carnet_scolaire.config_flow._probe",
        return_value=_probe_outcome("demo.example.invalid|parent-under-test"),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"password": "corrected-not-real"}
        )
        # Drained inside the patch: the finisher only *schedules* the reload.
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    _assert_reloaded_once(account, reloads, caplog)


async def test_a_reconfiguration_of_a_running_entry_reloads_once_without_deprecation(
    hass: HomeAssistant,
    account: PronoteAccount,
    reloads: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Reconfiguring is offered on an existing entry, usually a working one.

    Which is the one state the config-flow suite never reaches: there, set-up
    is patched away and no listener is ever registered.
    """
    entry = account.entry
    # The identity shape `flow_login._account_id` writes, which the
    # reconfiguration's same-account check reads; the fixture's is older.
    hass.config_entries.async_update_entry(
        entry, unique_id=f"{ESTABLISHMENT}::46#a-signature-from-set-up"
    )
    await hass.async_block_till_done()
    reloads.reset_mock()

    result = await entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.MENU
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "credentials"}
    )
    with patch(
        "custom_components.carnet_scolaire.config_flow._probe",
        return_value=_probe_outcome(f"{ESTABLISHMENT}::46#a-rotated-signature"),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_PRONOTE_URL: ESTABLISHMENT,
                "username": "parent-under-test",
                "password": "not-a-real-password",
            },
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    _assert_reloaded_once(account, reloads, caplog)


async def test_saving_the_options_of_a_running_entry_reloads_it_once(
    hass: HomeAssistant,
    account: PronoteAccount,
    reloads: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """§7.3: an options change applies without a restart, and exactly once."""
    form = await _open(hass, account.entry, "general")
    tick = account.entry.options.get(OPT_MASTER_TICK)

    result = await hass.config_entries.options.async_configure(
        form["flow_id"],
        {**_form_defaults(form["data_schema"]), OPT_MASTER_TICK: (tick or 5) + 1},
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    _assert_reloaded_once(account, reloads, caplog)


async def test_saving_unchanged_options_does_not_reload(
    hass: HomeAssistant, account: PronoteAccount, reloads: MagicMock
) -> None:
    """A visit to the page that changes nothing must not cost a login.

    Saved twice: the first save writes the page's defaults for every key the
    fixture's options leave out, which *is* a change and reloads; the second,
    identical, is the visit this test is about.
    """
    entry = account.entry
    form = await _open(hass, entry, "general")
    await hass.config_entries.options.async_configure(
        form["flow_id"], _form_defaults(form["data_schema"])
    )
    await hass.async_block_till_done()
    reloads.reset_mock()
    settled = entry.runtime_data

    form = await _open(hass, entry, "general")
    await hass.config_entries.options.async_configure(
        form["flow_id"], _form_defaults(form["data_schema"])
    )
    await hass.async_block_till_done()

    assert reloads.call_count == 0
    assert entry.runtime_data is settled


async def test_the_account_writing_its_own_data_does_not_reload_it(
    hass: HomeAssistant, account: PronoteAccount, reloads: MagicMock
) -> None:
    """The write the old listener had to guard against, now with no listener at all.

    The account persists a rotated token at every login and the key table when
    it pairs a child mid-tick. Reloading on either would drop every snapshot it
    holds -- and, for the child, undo the very adoption the write records.
    """
    entry = account.entry
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, "password": "not-a-real-rotated-token"}
    )
    await hass.async_block_till_done()

    assert reloads.call_count == 0
    assert entry.runtime_data is account
