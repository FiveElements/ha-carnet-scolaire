"""Device triggers, conditions and actions -- the surface §2.3 is about.

This is what the project is *for*: "when a lesson is cancelled, for this child"
has to be one card in the automation editor, not a Jinja template over an event
bus. Which makes the failure mode of this layer particularly nasty -- a trigger
that never attaches, or a condition that is never offered, produces no error
anywhere. The automation simply stops firing, and the user concludes the
integration is broken without a single line in the log.

Two of those had actually happened, and each has a test here:

* every condition whose key contains an underscore was silently absent from
  the editor, because the entity key was extracted with ``rpartition("_")`` --
  which answers ``day`` for ``school_day``. Eight of the ten conditions were
  invisible, and the two that survived were the two single-word keys;
* the write actions are gated on the option, but a hand-written automation must
  still be refused by the service rather than by the editor's filtering.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components import automation
from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.setup import async_setup_component
import pytest

from custom_components.carnet_scolaire.const import (
    DOMAIN,
    EVENT_GRADE_ADDED,
    EVENT_LESSON_CANCELED,
    OPT_WRITE_OPERATIONS_ENABLED,
    Tier,
)
from custom_components.carnet_scolaire.device_action import (
    ACTION_MARK_HOMEWORK_DONE,
    ACTION_TYPES,
    WRITE_ACTIONS,
    async_get_actions,
)
from custom_components.carnet_scolaire.device_condition import (
    CONDITION_MAP,
    CONDITION_SCHEMA,
    async_condition_from_config,
    async_get_conditions,
)
from custom_components.carnet_scolaire.device_trigger import (
    TRIGGER_TYPES,
    async_attach_trigger,
    async_get_triggers,
)

from .conftest import CHILDREN, REQUIRES_HASS, child_key
from .fixtures import protocol
from .keys import key_of

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.carnet_scolaire.account import PronoteAccount

pytestmark = REQUIRES_HASS

STUDENT_ONE, STUDENT_TWO = (child_id for child_id, _name in CHILDREN)


def _device_id(hass: HomeAssistant, entry_id: str, identifier: str) -> str:
    """A registry device id, by our own identifier, within its entry.

    The entry is required rather than incidental: from HA 2026.9 identifiers
    are unique only within a config entry, and the entry-less lookup is
    deprecated for being ambiguous.
    """
    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, identifier), entry_id
    )
    assert device is not None, f"no device for {identifier}"
    return device.id


def _child(hass: HomeAssistant, entry: MockConfigEntry, student: str) -> str:
    return _device_id(
        hass, entry.entry_id, f"{entry.entry_id}_{child_key(entry, student)}"
    )


def _account_device(hass: HomeAssistant, entry: MockConfigEntry) -> str:
    # The account device's identifier *is* the bare entry id, so the entry is
    # passed twice on purpose: once as the owner, once as the identifier.
    return _device_id(hass, entry.entry_id, entry.entry_id)


async def _fire(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    *,
    student_id: str,
    event_type: str,
    entity_key: str = "lesson_changed",
) -> None:
    """Fire one delta on the bus, the way the account does."""
    from custom_components.carnet_scolaire.account import SIGNAL_DELTA

    hass.bus.async_fire(
        SIGNAL_DELTA,
        {
            "entry_id": entry.entry_id,
            "student_id": student_id,
            "entity_key": entity_key,
            "event_type": event_type,
            "attributes": {"subject": "Mathématiques"},
        },
    )
    await hass.async_block_till_done()


# ---------------------------------------------------------------------------
# Triggers
# ---------------------------------------------------------------------------


async def test_every_change_type_is_offered_on_a_child(
    hass: HomeAssistant, mock_entry: MockConfigEntry, account: PronoteAccount
) -> None:
    """One trigger per event type, not per entity.

    A user thinking "a room changed" should not have to know that four
    different changes share one ``event`` entity (annexe A §4).
    """
    triggers = await async_get_triggers(hass, _child(hass, mock_entry, STUDENT_ONE))

    assert {trigger["type"] for trigger in triggers} == set(TRIGGER_TYPES)
    assert all(trigger["domain"] == DOMAIN for trigger in triggers)


async def test_the_account_device_offers_no_change_triggers(
    hass: HomeAssistant, mock_entry: MockConfigEntry, account: PronoteAccount
) -> None:
    """ "A grade arrived" does not happen to an account; it happens to a child.

    The account device carries the budget diagnostics, and offering it a
    "new grade" trigger would produce a card that can never fire.
    """
    assert await async_get_triggers(hass, _account_device(hass, mock_entry)) == []


async def test_a_trigger_fires_for_its_own_child_only(
    hass: HomeAssistant, mock_entry: MockConfigEntry, account: PronoteAccount
) -> None:
    """The defect a one-child test cannot see, on the automation side.

    Both children share an entry, a session and an event bus, so the trigger's
    event data has to match on the student as well as on the entry and the
    type. Matching on the type alone made every automation fire for every
    child: "wake me when Enfant Un's first lesson is cancelled" also fired for
    Enfant Deux.
    """
    assert await async_setup_component(
        hass,
        automation.DOMAIN,
        {
            automation.DOMAIN: [
                {
                    "alias": "child one",
                    "trigger": {
                        "platform": "device",
                        "domain": DOMAIN,
                        "device_id": _child(hass, mock_entry, STUDENT_ONE),
                        "type": EVENT_LESSON_CANCELED,
                    },
                    "action": {
                        "event": "test_fired",
                        "event_data": {"who": "one"},
                    },
                }
            ]
        },
    )
    fired: list[Any] = []
    hass.bus.async_listen("test_fired", lambda event: fired.append(event.data))

    await _fire(
        hass, mock_entry, student_id=STUDENT_TWO, event_type=EVENT_LESSON_CANCELED
    )
    assert fired == []

    await _fire(
        hass, mock_entry, student_id=STUDENT_ONE, event_type=EVENT_LESSON_CANCELED
    )
    assert fired == [{"who": "one"}]

    # And the type has to match too: a grade is not a cancellation.
    await _fire(hass, mock_entry, student_id=STUDENT_ONE, event_type=EVENT_GRADE_ADDED)
    assert len(fired) == 1


async def test_a_trigger_on_a_device_that_no_longer_resolves_does_not_attach(
    hass: HomeAssistant, mock_entry: MockConfigEntry, account: PronoteAccount
) -> None:
    """The quietest failure this integration has produced.

    A device left behind by an earlier child identity still resolves to a
    device id, so the automation editor accepts it and Home Assistant attaches
    the trigger. But the child cannot be resolved, the event filter is built
    with ``student_id: None``, and no real event -- all of which carry a
    student -- can ever match it. There is no trace, no log line and no
    ``unavailable`` entity: the automation is indistinguishable from one whose
    condition simply has not occurred.

    That is not hypothetical. On the live instance a child's PRONOTE resource
    signature rotated, an automation kept pointing at the stranded device, and
    the household noticed the missing announcement rather than the integration
    noticing the broken trigger. Minting our own key stops the identifier
    moving; it cannot resurrect a device the account no longer announces, so
    for those the only useful answer is to say so.
    """
    del account
    devices = dr.async_get(hass)
    stranded = devices.async_get_or_create(
        config_entry_id=mock_entry.entry_id,
        identifiers={(DOMAIN, f"{mock_entry.entry_id}_child-does-not-exist")},
        name="Un Enfant Parti",
    )

    with pytest.raises(HomeAssistantError):
        await async_attach_trigger(
            hass,
            {
                "platform": "device",
                "domain": DOMAIN,
                "device_id": stranded.id,
                "type": EVENT_LESSON_CANCELED,
            },
            lambda *_args, **_kwargs: None,
            {},  # type: ignore[arg-type]
        )


# ---------------------------------------------------------------------------
# Conditions
# ---------------------------------------------------------------------------


async def test_every_condition_is_offered(
    hass: HomeAssistant, mock_entry: MockConfigEntry, account: PronoteAccount
) -> None:
    """The regression that hid eight of the ten conditions.

    The entity key was recovered from the unique id with ``rpartition("_")``,
    which keeps only the last segment: ``day`` for ``school_day``, ``class``
    for ``in_class``, ``progress`` for ``absence_in_progress``. Only the two
    single-word keys matched, so the automation editor offered a child exactly
    one condition -- "Holidays" -- and the module's own documented example,
    "only if it is a school day", could not be written at all.
    """
    conditions = await async_get_conditions(hass, _child(hass, mock_entry, STUDENT_ONE))

    assert {condition["type"] for condition in conditions} == set(CONDITION_MAP)
    for condition in conditions:
        assert condition["entity_id"]
        assert condition["domain"] == DOMAIN


async def test_a_condition_reads_the_binary_sensor_it_names(
    hass: HomeAssistant, mock_entry: MockConfigEntry, account: PronoteAccount
) -> None:
    """Both polarities, and both actually evaluated.

    Offering ``is_not_school_day`` as well as ``is_school_day`` is one fewer
    place for a mistake than negating a condition in the editor -- but only if
    the two really do read the same entity and answer opposite questions.
    """
    conditions = {
        item["type"]: item
        for item in await async_get_conditions(
            hass, _child(hass, mock_entry, STUDENT_ONE)
        )
    }
    entity_id = conditions["is_school_day"]["entity_id"]

    # Through the module's own entry point, with the schema applied first --
    # which is how `device_automation` reaches it. Going via
    # `homeassistant.helpers.condition.async_from_config` would test Home
    # Assistant's dispatch rather than this integration's mapping.
    positive = async_condition_from_config(
        hass, CONDITION_SCHEMA(conditions["is_school_day"])
    )
    negative = async_condition_from_config(
        hass, CONDITION_SCHEMA(conditions["is_not_school_day"])
    )

    hass.states.async_set(entity_id, STATE_ON)
    assert positive(hass, {}) is True
    assert negative(hass, {}) is False

    hass.states.async_set(entity_id, STATE_OFF)
    assert positive(hass, {}) is False
    assert negative(hass, {}) is True


async def test_a_condition_is_not_offered_without_its_entity(
    hass: HomeAssistant, mock_entry: MockConfigEntry, account: PronoteAccount
) -> None:
    """An establishment that publishes nothing has no entity to read.

    Offering a condition that can never be true would be worse than offering
    nothing (§2.5), so the account device -- which has no child binary sensors
    at all -- gets an empty list rather than ten dead cards.
    """
    assert await async_get_conditions(hass, _account_device(hass, mock_entry)) == []


async def test_a_hand_written_condition_resolves_its_own_entity(
    hass: HomeAssistant, mock_entry: MockConfigEntry, account: PronoteAccount
) -> None:
    """A blueprint carries the device and the type, and nothing else.

    ``entity_id`` is optional in the schema and the editor always fills it in,
    so every path anybody exercised had it -- while a condition written by hand
    or delivered in a blueprint raised ``KeyError`` inside the automation. What
    the user sees is "this automation is broken", with no indication of which
    line, and the condition can never be satisfied.
    """
    config = CONDITION_SCHEMA(
        {
            "condition": "device",
            "domain": DOMAIN,
            "device_id": _child(hass, mock_entry, STUDENT_ONE),
            "type": "is_school_day",
        }
    )
    checker = async_condition_from_config(hass, config)

    resolved = {
        item["type"]: item["entity_id"]
        for item in await async_get_conditions(
            hass, _child(hass, mock_entry, STUDENT_ONE)
        )
    }
    hass.states.async_set(resolved["is_school_day"], STATE_ON)

    assert checker(hass, {}) is True


async def test_a_condition_whose_entity_does_not_exist_says_so(
    hass: HomeAssistant, mock_entry: MockConfigEntry, account: PronoteAccount
) -> None:
    """Rather than answering "false" for ever.

    A condition that quietly evaluates to false disarms every automation
    containing it, silently. An error at least appears in the trace.
    """
    config = CONDITION_SCHEMA(
        {
            "condition": "device",
            "domain": DOMAIN,
            "device_id": _account_device(hass, mock_entry),
            "type": "is_school_day",
        }
    )

    with pytest.raises(HomeAssistantError, match="school_day"):
        async_condition_from_config(hass, config)


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


async def test_the_write_actions_are_hidden_until_writes_are_enabled(
    hass: HomeAssistant, mock_entry: MockConfigEntry, account: PronoteAccount
) -> None:
    """Hidden in the editor, and -- separately -- refused by the service.

    Filtering the list is a courtesy, not the guard: an automation written by
    hand, or imported from a blueprint, still reaches
    ``async_call_action_from_config``. The refusal lives in the service
    (§8.3), which is what ``test_services.py`` pins.
    """
    device_id = _child(hass, mock_entry, STUDENT_ONE)

    offered = {action["type"] for action in await async_get_actions(hass, device_id)}
    assert offered == set(ACTION_TYPES) - set(WRITE_ACTIONS)

    hass.config_entries.async_update_entry(
        mock_entry,
        options={**mock_entry.options, OPT_WRITE_OPERATIONS_ENABLED: True},
    )
    await hass.async_block_till_done()

    offered = {action["type"] for action in await async_get_actions(hass, device_id)}
    # A parent account: PRONOTE records nothing its session ticks, so the tick
    # is not offered even with writes on.
    assert offered == set(ACTION_TYPES) - {ACTION_MARK_HOMEWORK_DONE}


@pytest.mark.parametrize(
    "account_client", ["student"], indirect=True, ids=["student-account"]
)
async def test_a_student_account_is_offered_the_homework_tick(
    hass: HomeAssistant, mock_entry: MockConfigEntry, account: PronoteAccount
) -> None:
    """The one shape of account whose ticks PRONOTE records gets the action."""
    hass.config_entries.async_update_entry(
        mock_entry,
        options={**mock_entry.options, OPT_WRITE_OPERATIONS_ENABLED: True},
    )
    await hass.async_block_till_done()

    device_id = _child(hass, mock_entry, protocol.STUDENT_ID)
    offered = {action["type"] for action in await async_get_actions(hass, device_id)}
    assert offered == set(ACTION_TYPES)


async def test_an_unknown_device_offers_nothing_at_all(
    hass: HomeAssistant, account: PronoteAccount
) -> None:
    """Rather than raising inside the automation editor."""
    assert await async_get_actions(hass, "not-a-device") == []
    assert await async_get_conditions(hass, "not-a-device") == []
    assert await async_get_triggers(hass, "not-a-device") == []


@pytest.mark.parametrize(
    ("action_type", "expected_service", "extra"),
    [
        pytest.param("refresh", "refresh", {}, id="refresh"),
        pytest.param("refresh_marks", "refresh", {}, id="refresh-marks"),
    ],
)
async def test_a_refresh_action_calls_the_matching_service(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    account: PronoteAccount,
    action_type: str,
    expected_service: str,
    extra: dict[str, Any],
) -> None:
    """Delegated to the service, so the limiter and the scheduler still decide.

    A device action that reached the gateway directly would be a second path to
    the network, and annexe B §6 gives the limiter a monopoly on that.
    """
    from custom_components.carnet_scolaire.device_action import (
        async_call_action_from_config,
    )

    calls: list[Any] = []
    hass.bus.async_listen(
        "call_service",
        lambda event: (
            calls.append(event.data) if event.data.get("domain") == DOMAIN else None
        ),
    )

    await async_call_action_from_config(
        hass,
        {
            "domain": DOMAIN,
            # A student account: the only one on which the tick lands.
            "device_id": _child(hass, mock_entry, protocol.STUDENT_ID),
            "type": action_type,
            **extra,
        },
        {},
        None,
    )
    await hass.async_block_till_done()

    assert [call["service"] for call in calls] == [expected_service]


@pytest.mark.parametrize(
    ("action_type", "required"),
    [
        pytest.param("refresh", set(), id="refresh"),
        pytest.param("refresh_marks", set(), id="refresh-marks"),
        pytest.param(
            "mark_homework_done", {"homework_id", "done"}, id="mark-homework-done"
        ),
        pytest.param(
            "mark_information_read", {"information_id"}, id="mark-information-read"
        ),
    ],
)
async def test_the_editor_asks_only_for_the_fields_an_action_needs(
    hass: HomeAssistant, action_type: str, required: set[str]
) -> None:
    """An extra field in the editor is a question the user cannot answer.

    A refresh needs nothing beyond the device, so asking for an identifier
    there would make the commonest action look harder than it is. Conversely a
    write action whose identifier was *not* asked for would be offered,
    accepted, and then refused by the service at run time -- a failure the
    automation editor had every chance to prevent.
    """
    from custom_components.carnet_scolaire.device_action import (
        async_get_action_capabilities,
    )

    capabilities = await async_get_action_capabilities(
        hass, {"domain": DOMAIN, "device_id": "unused", "type": action_type}
    )

    schema = capabilities["extra_fields"].schema
    assert {str(key) for key in schema} == required


@pytest.mark.parametrize(
    ("action_type", "expected_service", "extra", "expected_data"),
    [
        pytest.param(
            "mark_homework_done",
            "mark_homework_done",
            {"homework_id": "HOMEWORK-1"},
            {"homework_id": "HOMEWORK-1", "done": True},
            id="homework-defaults-to-done",
        ),
        pytest.param(
            "mark_homework_done",
            "mark_homework_done",
            {"homework_id": "HOMEWORK-1", "done": False},
            {"homework_id": "HOMEWORK-1", "done": False},
            id="homework-can-be-un-done",
        ),
        pytest.param(
            "mark_information_read",
            "mark_information_read",
            {"information_id": "INFORMATION-1"},
            {"information_id": "INFORMATION-1"},
            id="information-read",
        ),
    ],
)
@pytest.mark.parametrize(
    "account_client", ["student"], indirect=True, ids=["student-account"]
)
async def test_a_write_action_carries_its_identifier_into_the_service_call(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    account: PronoteAccount,
    action_type: str,
    expected_service: str,
    extra: dict[str, Any],
    expected_data: dict[str, Any],
) -> None:
    """The action is a wrapper, so the trace must show the real service call.

    ``done`` defaulting to ``True`` is the part worth pinning: an automation
    that ticks a homework item off omits the field entirely, and a wrapper that
    dropped it instead of defaulting would send ``done`` absent -- which the
    service reads as a validation error on the commonest write there is.
    """
    from custom_components.carnet_scolaire.device_action import (
        async_call_action_from_config,
    )

    # Parametrised with PRONOTE's `N`, which is what a fixture carries; what an
    # automation holds, and what the service looks up, is the minted key.
    def keyed(data: dict[str, Any]) -> dict[str, Any]:
        tiers = {"homework_id": Tier.HOMEWORK, "information_id": Tier.NEWS}
        return {
            field: key_of(account, value, tiers[field]) if field in tiers else value
            for field, value in data.items()
        }

    extra, expected_data = keyed(extra), keyed(expected_data)

    hass.config_entries.async_update_entry(
        mock_entry,
        options={**mock_entry.options, OPT_WRITE_OPERATIONS_ENABLED: True},
    )
    await hass.async_block_till_done()

    calls: list[Any] = []
    hass.bus.async_listen(
        "call_service",
        lambda event: (
            calls.append(event.data) if event.data.get("domain") == DOMAIN else None
        ),
    )

    await async_call_action_from_config(
        hass,
        {
            "domain": DOMAIN,
            "device_id": _child(hass, mock_entry, STUDENT_ONE),
            "type": action_type,
            **extra,
        },
        {},
        None,
    )
    await hass.async_block_till_done()

    assert [call["service"] for call in calls] == [expected_service]
    sent = calls[0]["service_data"]
    assert {key: sent[key] for key in expected_data} == expected_data


async def test_a_device_of_another_integration_resolves_to_no_account(
    hass: HomeAssistant, mock_entry: MockConfigEntry, account: PronoteAccount
) -> None:
    """Not the same case as a device id that does not exist at all.

    Home Assistant hands every integration's ``async_get_actions`` the device
    the user clicked, whichever integration owns it. A resolver that looked
    only for "no such device" would walk the identifiers of somebody else's
    device and, since the loop's exit is the interesting branch, return
    whatever the last iteration happened to leave behind.
    """
    other = dr.async_get(hass).async_get_or_create(
        config_entry_id=mock_entry.entry_id,
        identifiers={("some_other_integration", "a-device-we-do-not-own")},
        name="Not ours",
    )

    assert await async_get_actions(hass, other.id) == []


async def test_a_change_trigger_asks_the_editor_for_no_extra_field(
    hass: HomeAssistant, account: PronoteAccount
) -> None:
    """A threshold belongs in the automation's condition, not in the trigger.

    Baking "only grades under 10" into the trigger would make the decision to
    fire invisible in the automation trace, which is the one place a user
    looks when an automation did not run. The empty schema is the design, so
    it is worth pinning rather than leaving to whatever the default happens to
    be.
    """
    from custom_components.carnet_scolaire.device_trigger import (
        async_get_trigger_capabilities,
    )

    capabilities = await async_get_trigger_capabilities(
        hass, {"domain": DOMAIN, "device_id": "unused", "type": EVENT_GRADE_ADDED}
    )

    assert capabilities["extra_fields"].schema == {}


async def test_a_device_this_integration_does_not_own_resolves_to_no_entry(
    hass: HomeAssistant, mock_entry: MockConfigEntry, account: PronoteAccount
) -> None:
    """Home Assistant offers every integration the device the user clicked.

    A device belonging to someone else carries no identifier of ours, and both
    resolvers must answer ``None`` rather than partition a string that is not
    there -- ``"".partition("_")`` returns empty strings quite happily, and an
    entry id of ``""`` looks up as a missing account instead of as a bug.
    """
    from custom_components.carnet_scolaire.device_trigger import _entry_id, _student_id

    other = dr.async_get(hass).async_get_or_create(
        config_entry_id=mock_entry.entry_id,
        identifiers={("some_other_integration", "not-ours")},
        name="Not ours",
    )

    assert _entry_id(hass, other.id) is None
    assert _student_id(hass, other.id) is None


async def test_a_child_device_of_an_unloaded_entry_still_reads_as_a_child(
    hass: HomeAssistant, mock_entry: MockConfigEntry, account: PronoteAccount
) -> None:
    """The automation editor must still list triggers for a disabled entry.

    With the entry unloaded there is no account to translate the minted key
    into the identifier PRONOTE announces this session, and returning ``None``
    there would make the child device look like the account device -- so its
    per-child triggers would vanish from the editor while the entry is simply
    switched off. The untranslated key is enough to answer "this is a child",
    which is all ``async_get_triggers`` asks; nothing can fire meanwhile.
    """
    from custom_components.carnet_scolaire.device_trigger import _student_id

    device_id = _child(hass, mock_entry, STUDENT_ONE)
    resolved = _student_id(hass, device_id)
    assert resolved == STUDENT_ONE

    await hass.config_entries.async_unload(mock_entry.entry_id)
    await hass.async_block_till_done()

    key = _student_id(hass, device_id)
    assert key is not None
    assert key != STUDENT_ONE
    assert await async_get_triggers(hass, device_id) != []
