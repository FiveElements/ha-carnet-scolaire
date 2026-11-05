"""The declared Home Assistant floor, and the four places that repeat it.

A version floor is a promise made to somebody at install time: HACS reads
``hacs.json`` and refuses the integration on an older instance. The promise is
only worth something if the number is *exercised*, which is why
``requirements_test.txt`` pins the harness release carrying exactly that Home
Assistant version rather than a merely recent one -- and why raising the floor
has twice been done by running the target socle instead of editing the number.
Both times the run found a product defect no gate could see: ``via_device``,
removed in favour of ``via_device_id``, which killed entity loading on six
platforms out of seven; and a tenfold amplification of refresh requests.

The problem this module addresses is the other half of that: the number is
written down in **twenty-one** files. ``hacs.json`` declares it, the pin encodes
it, ``validate.yml``'s gated row repeats it, and eighteen blueprints each carry
a ``min_version``. Nothing made them agree. A blueprint left at the old floor
imports happily onto an instance the integration cannot run on, and the
failure surfaces as an automation whose entities do not exist -- which reads as
a broken blueprint rather than an unsupported Home Assistant.

Deliberately offline and free of any Home Assistant import: this file is about
what the repository *claims*, so it must run everywhere the suite runs,
including on the Windows half that has no test harness. What the repository
claims and what CI actually installs are tied together in ``validate.yml``
instead, by a step that reads ``homeassistant.const.__version__`` back after
the pinned install.
"""

from __future__ import annotations

import json
from pathlib import Path
import re

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
BLUEPRINTS = ROOT / "blueprints" / "automation" / "carnet_scolaire"

#: The pin line in ``requirements_test.txt``, whatever version it names -- a
#: beta (``2026.11.0b6``) included, for the weeks the harness of a new floor
#: exists only as one.
_PIN = re.compile(
    r"^pytest-homeassistant==(?P<version>[0-9.]+(?:b[0-9]+)?)$", re.MULTILINE
)


class _BlueprintLoader(yaml.SafeLoader):
    """``yaml.safe_load`` with Home Assistant's ``!input`` tolerated.

    A blueprint is not plain YAML: every substitutable value is written
    ``!input some_name``, and the safe loader refuses an unknown tag outright.
    Home Assistant has its own loader for this, and importing it would drag the
    whole framework into a module whose entire point is to need none of it -- so
    the tag is resolved to its own name here, which is enough for a file this
    module only reads metadata out of.
    """


def _unknown_tag(loader: yaml.Loader, _suffix: str, node: yaml.Node) -> object:
    """Return the scalar behind any tag this loader does not know.

    Three parameters, not two: a *multi*-constructor is handed the part of the
    tag after the prefix it was registered for, between the loader and the node.
    """
    return loader.construct_scalar(node)  # type: ignore[arg-type]


_BlueprintLoader.add_multi_constructor("!", _unknown_tag)


def _declared_floor() -> str:
    """The floor HACS publishes to users, from ``hacs.json``."""
    payload = json.loads((ROOT / "hacs.json").read_text(encoding="utf-8"))
    floor: str = payload["homeassistant"]
    return floor


def _pinned_plugin() -> str:
    """The ``pytest-homeassistant`` release the suite pins."""
    match = _PIN.search((ROOT / "requirements_test.txt").read_text(encoding="utf-8"))
    assert match is not None, "the test stack is no longer pinned to one release"
    return match.group("version")


def _gated_matrix_row() -> dict[str, object]:
    """The matrix row that carries the coverage gate."""
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "validate.yml").read_text(encoding="utf-8")
    )
    rows = workflow["jobs"]["test"]["strategy"]["matrix"]["include"]
    gated = [row for row in rows if row.get("gate")]
    assert len(gated) == 1, f"expected exactly one gated row, found {len(gated)}"
    return dict(gated[0])


def test_the_gated_matrix_row_installs_the_pin_the_repository_declares() -> None:
    """Otherwise the gate runs against a socle the repository does not claim.

    The row exists to be the floor. Pinning it separately from
    ``requirements_test.txt`` means two numbers that must agree and nothing
    that makes them -- and the failure is silent: the coverage gate still
    passes, against a Home Assistant nobody promised.
    """
    row = _gated_matrix_row()

    assert row["pytest-ha"] == _pinned_plugin()


def test_the_gated_matrix_row_is_labelled_with_the_floor_it_tests() -> None:
    """The label is what a reader of a failed CI run sees first.

    Only the major and minor are compared: the label is a human one
    (``2026.9``) and the floor is a full version (``2026.9.0``), and there is
    no reason to make the label carry a patch number it does not need.
    """
    major_minor = ".".join(_declared_floor().split(".")[:2])

    assert str(_gated_matrix_row()["ha-version"]) == major_minor


@pytest.mark.parametrize(
    "blueprint",
    sorted(BLUEPRINTS.rglob("*.yaml")),
    ids=lambda path: f"{path.parent.name}/{path.stem}",
)
def test_every_blueprint_requires_the_floor_and_not_an_older_one(
    blueprint: Path,
) -> None:
    """A blueprint that imports onto an unsupported instance is worse than one
    that refuses to.

    Home Assistant enforces ``min_version`` at import time, which is the only
    moment a person is looking. Left behind at an old floor, a blueprint
    imports, builds an automation referring to entities that will never exist,
    and reads as a broken blueprint rather than as an instance too old for the
    integration.
    """
    document = yaml.load(
        blueprint.read_text(encoding="utf-8"),
        Loader=_BlueprintLoader,  # noqa: S506 -- a SafeLoader subclass, see above
    )
    metadata = document["blueprint"]["homeassistant"]

    assert metadata["min_version"] == _declared_floor()


def test_the_blueprints_are_the_eighteen_the_repository_ships() -> None:
    """Guards the parametrisation above against silently covering nothing.

    Nine blueprints in two languages. A glob that stopped matching -- a moved
    directory, a renamed extension -- would turn every assertion above into
    zero assertions, and the suite would go green having checked nothing.
    """
    found = sorted(path.stem for path in BLUEPRINTS.rglob("*.yaml"))

    assert len(found) == 18, found
    assert sorted(set(found)) == [
        "absence_alert",
        "canteen_menu",
        "end_of_day",
        "homework_reminder",
        "lesson_canceled",
        "midday_return",
        "new_grade",
        "new_message",
        "wake_up_alarm",
    ]
