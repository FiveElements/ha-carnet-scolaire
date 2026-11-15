"""The quality-scale inventory must name every rule, with a usable status.

Home Assistant grades Core integrations against a fixed list of rules
(developers.home-assistant.io, integration quality scale). This repository is
a custom integration, so it cannot display a bronze/silver/gold/platinum
badge, but ``quality_scale.yaml`` is how a reviewer — or a future Core
submission — sees what is done, exempt, or still open.

A file that silently drops a rule, or that marks an exemption without saying
why, is how an inventory goes stale. This test needs no Home Assistant: it
reads YAML.
"""

from pathlib import Path
import re
from typing import Any, Final

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCALE = ROOT / "custom_components" / "carnet_scolaire" / "quality_scale.yaml"

#: Every rule hassfest knows about, in declaration order. A rule added
#: upstream and missing here fails this test on the next review, not months
#: later when someone opens the YAML.
SCALE_RULES: Final[tuple[str, ...]] = (
    # Bronze
    "action-setup",
    "appropriate-polling",
    "brands",
    "common-modules",
    "config-flow",
    "config-flow-test-coverage",
    "dependency-transparency",
    "docs-actions",
    "docs-conditions",
    "docs-high-level-description",
    "docs-installation-instructions",
    "docs-removal-instructions",
    "docs-triggers",
    "entity-event-setup",
    "entity-unique-id",
    "has-entity-name",
    "runtime-data",
    "test-before-configure",
    "test-before-setup",
    "unique-config-entry",
    # Silver
    "action-exceptions",
    "config-entry-unloading",
    "docs-configuration-parameters",
    "docs-installation-parameters",
    "entity-unavailable",
    "integration-owner",
    "log-when-unavailable",
    "parallel-updates",
    "reauthentication-flow",
    "test-coverage",
    # Gold
    "devices",
    "diagnostics",
    "discovery",
    "discovery-update-info",
    "docs-data-update",
    "docs-examples",
    "docs-known-limitations",
    "docs-supported-devices",
    "docs-supported-functions",
    "docs-troubleshooting",
    "docs-use-cases",
    "dynamic-devices",
    "entity-category",
    "entity-device-class",
    "entity-disabled-by-default",
    "entity-translations",
    "exception-translations",
    "icon-translations",
    "reconfiguration-flow",
    "repair-issues",
    "stale-devices",
    # Platinum
    "async-dependency",
    "inject-websession",
    "strict-typing",
)

STATUSES: Final = frozenset({"done", "exempt", "todo"})


def _rules() -> dict[str, Any]:
    payload = yaml.safe_load(SCALE.read_text(encoding="utf-8"))
    rules: dict[str, Any] = payload["rules"]
    return rules


def _status(value: Any) -> str:
    if isinstance(value, str):
        return value
    status: str = value["status"]
    return status


def test_every_quality_scale_rule_is_inventoried() -> None:
    """A missing rule is a rule nobody will implement."""
    assert SCALE.is_file()
    named = frozenset(_rules())
    assert named == frozenset(SCALE_RULES)


def test_every_quality_scale_status_is_done_exempt_or_todo() -> None:
    """hassfest rejects anything else; so does this test, before CI does."""
    unknown = {
        name: _status(value)
        for name, value in _rules().items()
        if _status(value) not in STATUSES
    }
    assert unknown == {}


def test_an_exemption_or_todo_names_the_reason() -> None:
    """A status without a comment is an inventory that cannot be reviewed."""
    silent = [
        name
        for name, value in _rules().items()
        if _status(value) in {"exempt", "todo"}
        and not (isinstance(value, dict) and str(value.get("comment") or "").strip())
    ]
    assert silent == []


def test_log_when_unavailable_is_marked_done() -> None:
    """The Silver gap this change closed; flipping it back is a regression."""
    assert _status(_rules()["log-when-unavailable"]) == "done"


def test_the_integration_package_declares_itself_fully_typed() -> None:
    """Platinum ``strict-typing``: a PEP-561 marker, or mypy treats us as untyped.

    ``mypy --strict`` on our own files is necessary and not sufficient. Without
    ``py.typed``, another checker that imports ``custom_components.carnet_scolaire``
    ignores every annotation we wrote. That is the gap the quality-scale rule
    names, and it is why Core hassfest looks for this file on requirements.
    """
    assert (SCALE.parent / "py.typed").is_file()


def test_runtime_data_is_typed_through_pronote_config_entry() -> None:
    """The Platinum warning: the custom ConfigEntry type must be used throughout.

    ``PronoteConfigEntry = ConfigEntry[PronoteAccount]`` is a no-op if
    ``account.py`` and the coordinators still take a bare ``ConfigEntry``.
    Config-flow helpers stay on the generic type: they run before
    ``runtime_data`` exists, and Home Assistant types those callbacks that way.
    """
    pattern = re.compile(r"entry: ConfigEntry\b(?!\[)")
    offenders = sorted(
        path.name
        for path in SCALE.parent.glob("*.py")
        if path.name != "config_flow.py"
        and pattern.search(path.read_text(encoding="utf-8"))
    )
    assert offenders == []


def test_the_pinned_pronotepy_ships_py_typed() -> None:
    """Hassfest's other half of ``strict-typing``: every requirement is PEP-561.

    ``pronotepy`` already ships ``py.typed``. The remaining mypy override
    (``follow_imports = silent``) is about noisy upstream annotations
    (``autoslot``), not about a missing marker. A pin bump that dropped
    ``py.typed`` would silently undo Platinum.
    """
    pytest.importorskip("pronotepy")
    from importlib import metadata

    distribution = metadata.distribution("pronotepy")
    files = distribution.files or []
    assert any(path.name == "py.typed" for path in files)
