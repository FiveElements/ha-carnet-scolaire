"""The translation catalogues, and the file Home Assistant refuses silently.

§10.10 makes this suite blocking in CI, and the reason is that every failure
mode here is invisible until a user hits it:

* a ``translation_key`` with no entry shows the raw key in the interface --
  ``nickname`` where a name should be;
* a key present in ``strings.json`` and missing from ``fr.json`` is invisible to
  ``hassfest``, which validates structure, not coverage;
* and ``services.yaml`` is validated by Home Assistant at load time, which on
  failure logs **one** line and serves ``{}`` -- so the services appear in the
  UI with no fields at all, and nothing in the log says why. That is how eighteen
  selectors sat two spaces too far left for as long as they did.

So the catalogues are checked three ways: against Home Assistant's own schema,
against the code that references them, and against each other.
"""

from __future__ import annotations

import ast
import importlib.util
import json
from pathlib import Path
import re
import sys
from typing import TYPE_CHECKING, Any

from homeassistant.helpers import entity_registry as er
import pytest
import voluptuous as vol
import yaml

from custom_components.carnet_scolaire import const
from custom_components.carnet_scolaire.device_action import ACTION_TYPES
from custom_components.carnet_scolaire.device_condition import CONDITION_MAP
from custom_components.carnet_scolaire.device_trigger import TRIGGER_TYPES
from custom_components.carnet_scolaire.services import _SERVICES

from .conftest import REQUIRES_HASS

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.carnet_scolaire.account import PronoteAccount

COMPONENT = Path(const.__file__).parent
ROOT = COMPONENT.parents[1]
LANGUAGES = ("strings.json", "translations/en.json", "translations/fr.json")

#: ``{placeholder}`` occurrences, ignoring `{{` escapes.
_PLACEHOLDER = re.compile(r"(?<!\{)\{([a-z_]+)\}")


def _catalogue(relative: str) -> dict[str, Any]:
    """One catalogue, parsed."""
    payload: dict[str, Any] = json.loads(
        (COMPONENT / relative).read_text(encoding="utf-8")
    )
    return payload


def _generator() -> Any:
    """Import ``scripts/build_translations.py`` as a module.

    By path, because ``scripts/`` is not a package: it is a directory of tools
    the CI runs, and making it importable only to satisfy a test would be the
    test dictating the layout.
    """
    path = ROOT / "scripts" / "build_translations.py"
    spec = importlib.util.spec_from_file_location("build_translations", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _flatten(payload: Any, prefix: str = "") -> dict[str, str]:
    """Every leaf string, keyed by its dotted path."""
    if isinstance(payload, dict):
        out: dict[str, str] = {}
        for key, value in payload.items():
            out.update(_flatten(value, f"{prefix}.{key}" if prefix else str(key)))
        return out
    return {prefix: str(payload)}


# ---------------------------------------------------------------------------
# The generated files are the generator's output
# ---------------------------------------------------------------------------


def test_the_catalogues_are_what_the_generator_produces() -> None:
    """Otherwise a hand edit is silently reverted by the next regeneration.

    The four files come from one table, which is what keeps English, French and
    ``services.yaml`` in step. Nothing prevents editing the JSON directly, and
    the edit would survive review and disappear on the next run of the script,
    so the CI has to be the thing that notices.
    """
    module = _generator()
    english = module._catalogue(1)
    french = module._catalogue(2)

    assert _catalogue("strings.json") == english
    assert _catalogue("translations/en.json") == english
    assert _catalogue("translations/fr.json") == french
    assert (COMPONENT / "services.yaml").read_text(
        encoding="utf-8"
    ) == module._services_yaml()
    assert _catalogue("icons.json") == module._icons()


# ---------------------------------------------------------------------------
# icons.json (quality scale `icon-translations`)
# ---------------------------------------------------------------------------


def _icon_values(payload: Any) -> list[str]:
    """Every icon string in ``icons.json``, wherever it sits."""
    return list(_flatten(payload).values())


def test_every_icon_is_a_material_design_icon() -> None:
    """hassfest's ``icon_value_validator``: an ``mdi:`` name and nothing else."""
    icons = _catalogue("icons.json")

    values = _icon_values(icons)
    assert values
    assert all(value.startswith("mdi:") for value in values), values


def test_no_state_icon_repeats_its_default() -> None:
    """hassfest rejects it (``ensure_not_same_as_default``), and it adds nothing.

    Checked for the entity's own states and for its state attributes, since a
    binary sensor's ``off`` and an event type's picture are both states.
    """
    for platform, keys in _catalogue("icons.json")["entity"].items():
        for key, spec in keys.items():
            assert spec.get("default"), f"{platform}.{key} has no default icon"
            for state, icon in (spec.get("state") or {}).items():
                assert icon != spec["default"], f"{platform}.{key}.{state}"
            for attribute, sub in (spec.get("state_attributes") or {}).items():
                default = sub.get("default")
                for state, icon in (sub.get("state") or {}).items():
                    assert icon != default, f"{platform}.{key}.{attribute}.{state}"


def test_the_state_icons_name_only_states_the_entity_can_take() -> None:
    """An icon for a state that never occurs is a typo nobody will see.

    A binary sensor is ``on`` or ``off``; the limiter's states are the
    ``LimiterState`` values, all of them, so a new state cannot appear with the
    generic picture; and the lesson entity's event types are the six it
    declares.
    """
    from custom_components.carnet_scolaire.const import (
        LESSON_EVENT_TYPES,
        LimiterState,
    )

    entity = _catalogue("icons.json")["entity"]

    for key, spec in entity["binary_sensor"].items():
        assert set(spec.get("state") or {}) <= {"on", "off"}, key
    assert set(entity["sensor"]["limiter_state"]["state"]) == {
        str(state) for state in LimiterState
    }
    lesson_types = entity["event"]["lesson_changed"]["state_attributes"]["event_type"]
    assert set(lesson_types["state"]) == set(LESSON_EVENT_TYPES)


def test_every_service_has_an_icon() -> None:
    """The actions list draws a blank square for a service with no icon."""
    services = _catalogue("icons.json")["services"]

    assert set(services) == {key for key, *_rest in _SERVICES}
    assert all(spec.get("service") for spec in services.values())


def test_every_translated_entity_key_has_an_icon() -> None:
    """The static half: a key in the entity catalogue must have an icon.

    ``_icons`` already refuses to build otherwise; this says so in the test
    report rather than as a traceback from the generator.
    """
    names = _catalogue("strings.json")["entity"]
    icons = _catalogue("icons.json")["entity"]

    assert set(names) == set(icons)
    for platform, keys in names.items():
        assert set(keys) == set(icons[platform]), platform


@REQUIRES_HASS
async def test_every_entity_that_was_created_has_an_icon(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    account: PronoteAccount,
) -> None:
    """The dynamic half, against the registry, as for the names above.

    A platform can build an entity whose translation key sits in no table of
    the generator; only setting the integration up shows what was *built*.
    Disabled entities are in the registry too, so the closed-period sensors are
    checked although they have no state.
    """
    icons = _catalogue("icons.json")["entity"]
    registry = er.async_get(hass)
    entries = er.async_entries_for_config_entry(registry, mock_entry.entry_id)
    assert entries

    missing = [
        f"{entry.domain}.{entry.translation_key}"
        for entry in entries
        if not icons.get(entry.domain, {}).get(entry.translation_key or "", {})
    ]
    assert not missing, f"no icon: {sorted(set(missing))}"


# ---------------------------------------------------------------------------
# Raised errors (quality scale `exception-translations`)
# ---------------------------------------------------------------------------

#: The Home Assistant exception family a user can see: on the integrations
#: page, in an automation trace, in the answer to an action.
_USER_FACING_ERRORS = frozenset(
    {
        "HomeAssistantError",
        "ServiceValidationError",
        "ConfigEntryNotReady",
        "ConfigEntryAuthFailed",
        "ConfigEntryError",
        "IntegrationError",
        "InvalidDeviceAutomationConfig",
        "UpdateFailed",
    }
)


def _untranslated_raises(tree: ast.AST) -> list[int]:
    """Lines raising a user-facing error without a ``translation_key``."""
    lines: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if name not in _USER_FACING_ERRORS:
            continue
        if not any(keyword.arg == "translation_key" for keyword in node.keywords):
            lines.append(node.lineno)
    return lines


def test_no_user_facing_error_is_built_from_a_plain_message() -> None:
    """Every ``HomeAssistantError`` of this package names a translation key.

    Scanned over the source rather than listed, because the defect is a new
    ``raise HomeAssistantError(f"...")`` in a file nobody thought to check:
    English on every install, and -- for an f-string over an exception -- text
    the integration does not control, such as an address with its session
    parameters. The set-up helper ``_setup_error`` passes the class as an
    argument and is therefore covered by its own call.
    """
    offenders: dict[str, list[int]] = {}
    for path in sorted(COMPONENT.rglob("*.py")):
        lines = _untranslated_raises(ast.parse(path.read_text(encoding="utf-8")))
        if lines:
            offenders[str(path.relative_to(COMPONENT))] = lines

    assert offenders == {}


def test_the_raise_scanner_sees_a_plain_message() -> None:
    """A scanner that finds nothing must be shown to find something."""
    tree = ast.parse(
        "raise HomeAssistantError('plain')\n"
        "raise exceptions.ConfigEntryNotReady(str(error))\n"
        "raise ServiceValidationError(translation_domain=D, translation_key='k')\n"
    )

    assert _untranslated_raises(tree) == [1, 2]


def test_every_translation_key_the_code_raises_is_in_the_catalogue() -> None:
    """A key with no entry renders as the key itself: ``setup_unreachable``.

    Read from the source: every string literal passed as ``translation_key``
    to an exception, and every key handed to ``_setup_error`` or to the
    gesture helper's ``_fail``, must have a message in both languages.
    """
    raised: set[str] = set()
    for path in sorted(COMPONENT.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if name in _USER_FACING_ERRORS:
                for keyword in node.keywords:
                    if keyword.arg == "translation_key" and isinstance(
                        keyword.value, ast.Constant
                    ):
                        raised.add(str(keyword.value.value))
            elif name in {"_setup_error", "_fail"}:
                for arg in node.args:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        raised.add(arg.value)
    assert {"setup_auth_failed", "condition_entity_missing"} <= raised

    for language in LANGUAGES:
        exceptions = _catalogue(language)["exceptions"]
        missing = sorted(key for key in raised if key not in exceptions)
        assert not missing, f"{language}: {missing}"


# ---------------------------------------------------------------------------
# services.yaml, against Home Assistant's own schema
# ---------------------------------------------------------------------------


def test_services_yaml_passes_home_assistants_own_schema() -> None:
    """The check whose absence let eighteen null selectors through.

    ``helpers/service.py`` applies this schema in ``_load_services_file``; on
    ``vol.Invalid`` it logs one line and returns an empty mapping, so every
    service loses its fields and the automation editor offers a form with
    nothing in it. Importing the private schema is deliberate: it is the exact
    thing that runs at load time, and a re-implementation here would only prove
    that two of my own guesses agree.
    """
    from homeassistant.helpers.service import (
        _SERVICES_SCHEMA,
    )

    payload = yaml.safe_load((COMPONENT / "services.yaml").read_text(encoding="utf-8"))

    validated = _SERVICES_SCHEMA(payload)
    assert set(validated) == {key for key, *_rest in _SERVICES}


def test_every_selector_is_actually_a_selector() -> None:
    """A `selector:` whose body became a sibling key parses as ``None``.

    The schema above accepts that -- ``selector`` is optional -- which is why
    this needs saying separately: a field with no selector is a field the UI
    cannot render.
    """
    payload = yaml.safe_load((COMPONENT / "services.yaml").read_text(encoding="utf-8"))

    for service, definition in payload.items():
        for field, spec in (definition.get("fields") or {}).items():
            selector = spec.get("selector")
            assert isinstance(selector, dict), (
                f"{service}.{field} has no usable selector: {spec!r}"
            )
            assert selector, f"{service}.{field} has an empty selector"


def test_the_services_yaml_fields_match_the_voluptuous_schemas() -> None:
    """The form and the validator have to describe the same service.

    A field in the schema and not in the YAML cannot be filled in from the UI;
    a field in the YAML and not in the schema is rejected the moment somebody
    fills it in.
    """
    payload = yaml.safe_load((COMPONENT / "services.yaml").read_text(encoding="utf-8"))

    for key, _handler, schema, _response in _SERVICES:
        documented = set(payload[key].get("fields") or {})
        declared = {str(marker.schema) for marker in _voluptuous_keys(schema)}
        assert documented == declared, f"{key}: {documented ^ declared}"


def _voluptuous_keys(schema: Any) -> list[Any]:
    """The top-level markers of a schema, through ``vol.All`` wrappers."""
    if isinstance(schema, vol.All):
        for candidate in schema.validators:
            keys = _voluptuous_keys(candidate)
            if keys:
                return keys
        return []
    inner = getattr(schema, "schema", None)
    if isinstance(inner, dict):
        return list(inner)
    return []


# ---------------------------------------------------------------------------
# Everything the code references must exist, in every language
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("language", LANGUAGES)
def test_every_service_is_named_and_described(language: str) -> None:
    """An undescribed service shows its raw name in the actions list."""
    catalogue = _catalogue(language)
    for key, _handler, schema, _response in _SERVICES:
        entry = catalogue["services"].get(key)
        assert entry is not None, f"{key} is not in {language}"
        assert entry["name"], f"{key} has no name in {language}"
        assert entry["description"], f"{key} has no description in {language}"
        for marker in _voluptuous_keys(schema):
            field = str(marker.schema)
            assert field in entry["fields"], f"{key}.{field} is not in {language}"


@pytest.mark.parametrize("language", LANGUAGES)
def test_every_repair_issue_has_a_title_and_a_body(language: str) -> None:
    """A missing entry renders the raw key as the title of the repair.

    Collected from ``const`` rather than from a list written here, so adding an
    ``ISSUE_*`` constant without a catalogue entry fails this test instead of
    reaching a user.
    """
    catalogue = _catalogue(language)
    keys = {
        value
        for name, value in vars(const).items()
        if name.startswith("ISSUE_") and isinstance(value, str)
    }
    assert keys, "no ISSUE_* constants found -- has const.py been renamed?"

    for key in keys:
        entry = catalogue["issues"].get(key)
        assert entry is not None, f"issues.{key} is missing from {language}"
        assert entry["title"], f"issues.{key} has no title in {language}"
        assert entry["description"], f"issues.{key} has no body in {language}"


@pytest.mark.parametrize("language", LANGUAGES)
def test_every_device_automation_type_is_labelled(language: str) -> None:
    """An unlabelled trigger reads as ``lesson_canceled`` in the editor."""
    catalogue = _catalogue(language)["device_automation"]

    assert set(TRIGGER_TYPES) <= set(catalogue["trigger_type"])
    assert set(CONDITION_MAP) <= set(catalogue["condition_type"])
    assert set(ACTION_TYPES) <= set(catalogue["action_type"])


@pytest.mark.parametrize("language", LANGUAGES)
def test_every_selector_translation_key_exists(language: str) -> None:
    """``translation_key: tier`` with no ``selector.tier`` shows raw values."""
    catalogue = _catalogue(language)
    payload = yaml.safe_load((COMPONENT / "services.yaml").read_text(encoding="utf-8"))

    for service, definition in payload.items():
        for field, spec in (definition.get("fields") or {}).items():
            for kind, body in (spec.get("selector") or {}).items():
                if not isinstance(body, dict):
                    continue
                key = body.get("translation_key")
                if key is None:
                    continue
                options = catalogue["selector"].get(key, {}).get("options")
                assert options, f"selector.{key} missing ({service}.{field}, {kind})"
                for value in body.get("options") or []:
                    assert value in options, f"selector.{key}.{value} missing"


# ---------------------------------------------------------------------------
# The two languages have to say the same things
# ---------------------------------------------------------------------------


def test_the_two_languages_have_the_same_keys() -> None:
    """A key in one language and not the other is a raw key for half the users."""
    english = _flatten(_catalogue("translations/en.json"))
    french = _flatten(_catalogue("translations/fr.json"))

    assert set(english) == set(french)


def test_the_two_languages_use_the_same_placeholders() -> None:
    """A French text naming ``{until}`` where English does not renders it raw.

    Home Assistant substitutes only the placeholders the *code* supplies, so an
    extra one in a translation appears verbatim in the interface, braces
    included.
    """
    english = _flatten(_catalogue("translations/en.json"))
    french = _flatten(_catalogue("translations/fr.json"))

    for key, text in english.items():
        assert set(_PLACEHOLDER.findall(text)) == set(
            _PLACEHOLDER.findall(french[key])
        ), key


# ---------------------------------------------------------------------------
# ...and the entities that actually got created
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("language", LANGUAGES)
@REQUIRES_HASS
async def test_every_entity_that_was_created_has_a_name(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    account: PronoteAccount,
    language: str,
) -> None:
    """Checked against the registry rather than against a list of keys.

    A static scan cannot see which entities a platform decides to create -- the
    history sensors carry a period suffix, and several entities exist only for
    establishments that publish the matching tab. Setting the integration up and
    asking the registry is the only way to compare what was *built* against what
    was *translated*.
    """
    catalogue = _catalogue(language)["entity"]
    registry = er.async_get(hass)
    entries = er.async_entries_for_config_entry(registry, mock_entry.entry_id)
    assert entries

    missing = [
        f"{entry.domain}.{entry.translation_key}"
        for entry in entries
        if entry.translation_key
        and not catalogue.get(entry.domain, {})
        .get(entry.translation_key, {})
        .get("name")
    ]
    assert not missing, f"untranslated in {language}: {sorted(set(missing))}"


@pytest.mark.parametrize("language", LANGUAGES)
@REQUIRES_HASS
async def test_every_event_type_an_entity_can_report_is_translated(
    hass: HomeAssistant,
    mock_entry: MockConfigEntry,
    account: PronoteAccount,
    language: str,
) -> None:
    """The ``event`` entities' state is an event *type*, and it is displayed.

    Home Assistant reads these from
    ``entity.event.<key>.state_attributes.event_type.state.<value>``. Without
    them the dashboard shows ``lesson_canceled`` in snake case -- and the same
    fourteen strings are already translated for the device triggers, so it is
    not even a translation cost.
    """
    catalogue = _catalogue(language)["entity"]["event"]
    registry = er.async_get(hass)
    keys = {
        entry.entity_id: entry.translation_key
        for entry in er.async_entries_for_config_entry(registry, mock_entry.entry_id)
    }

    states = hass.states.async_all("event")
    assert states, "no event entities were created"

    for state in states:
        # From the registry, not from the state: `translation_key` is not a
        # state attribute, so reading it there gave "" for every entity and the
        # assertion below would have passed on an empty catalogue.
        key = keys[state.entity_id]
        types = state.attributes.get("event_types") or []
        assert types, f"{state.entity_id} declares no event types"
        translated = (
            catalogue.get(key, {})
            .get("state_attributes", {})
            .get("event_type", {})
            .get("state", {})
        )
        for event_type in types:
            assert event_type in translated, (
                f"event.{key}.state_attributes.event_type.state.{event_type} "
                f"is missing from {language}"
            )
