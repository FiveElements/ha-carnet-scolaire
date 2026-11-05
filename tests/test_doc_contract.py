"""Facts the documentation repeats, pinned to the one place that knows them.

A list that lives in three files diverges in two of them. This suite exists
because that already happened and was paid for: ``LESSON_EVENT_TYPES`` grew
``lesson_restored`` and ``lesson_status_changed``, and the reference tables
went on announcing four types for long enough that a code comment, an annexe
row and an architecture note all disagreed at once. The argument in each was
still right, which is what makes the divergence expensive -- a reader
reconciles a true claim with a stale list and concludes the list.

The rule these tests encode is narrow on purpose. They do not check prose, and
they do not check that the documentation is *good*; they check that a set the
code owns is reproduced exactly where the documentation reproduces it. Adding a
seventh event type must fail here, in the same commit, rather than being
noticed on a dashboard months later.
"""

from pathlib import Path
import re

import pytest

from custom_components.carnet_scolaire import const

from .conftest import REQUIRES_HASS

#: Every event type the integration can fire, by constant name. Collected from
#: the module rather than typed out, so a new ``EVENT_`` constant joins the
#: vocabulary automatically and cannot be silently excluded from the check.
ALL_EVENT_TYPES = frozenset(
    value
    for name, value in vars(const).items()
    if name.startswith("EVENT_") and isinstance(value, str)
)

DOCS = Path(__file__).resolve().parents[1] / "docs"

#: The rows that reproduce the lesson-change vocabulary, and the marker that
#: identifies each. Two files, because a table for automation authors (the
#: guide) and a reference table (the annexe) are read by different people and
#: drifted independently.
LESSON_ROWS = (
    pytest.param(DOCS / "annexe-a-entites.md", "cours_modifie", id="annexe A §4"),
    pytest.param(DOCS / "GUIDE-UTILISATEUR.md", "Cours modifié", id="guide §4.10"),
)


def _event_types_named_in(line: str) -> frozenset[str]:
    """The event types a documentation row names, ignoring everything else.

    Backticked tokens are matched against the vocabulary rather than parsed by
    column, so the attribute names that share the row are ignored without this
    test having to know the table's shape -- which is the documentation's to
    change, not ours.
    """
    return frozenset(re.findall(r"`([a-z_]+)`", line)) & ALL_EVENT_TYPES


@pytest.mark.parametrize(("path", "marker"), LESSON_ROWS)
def test_the_documented_lesson_event_types_are_exactly_the_declared_ones(
    path: Path, marker: str
) -> None:
    """Both directions, because both have been wrong.

    Missing a type sends an automation author looking for a trigger that
    exists -- ``lesson_restored`` was undocumented while it was precisely the
    one that stops "no first period, sleep in" firing on the morning a lesson
    comes back. An extra type promises a trigger that never fires, which is
    worse: nothing in the trace says why.
    """
    rows = [
        line for line in path.read_text(encoding="utf-8").splitlines() if marker in line
    ]
    documented: set[str] = set()
    for row in rows:
        documented |= _event_types_named_in(row)

    assert rows, f"no row mentioning {marker!r} in {path.name}"
    assert documented == set(const.LESSON_EVENT_TYPES)


def test_every_event_type_the_code_can_fire_is_documented_somewhere() -> None:
    """An event nobody documents is an event nobody triggers on.

    The entity exists, the bus carries it, and the only route to discovering it
    is reading the source -- which §1's design objective rules out: an ordinary
    school automation must be writable from the documentation.
    """
    prose = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(DOCS.glob("*.md"))
    )
    named = frozenset(re.findall(r"`([a-z_]+)`", prose)) & ALL_EVENT_TYPES

    assert ALL_EVENT_TYPES - named == frozenset()


@REQUIRES_HASS
def test_every_entity_appears_in_the_annexe_a_catalogue() -> None:
    """An entity nobody documented is an entity nobody can find.

    Annexe A is the catalogue a reader consults to learn what exists, and
    until this test nothing checked that its identifiers were real. They were
    not: **eleven** of them named entities that do not exist -- a
    ``_moyennes`` that is ``_moyennes_par_matiere``, an ``_etat_limiteur``
    that is ``_etat_du_limiteur``, a ``bride`` that is ``collectes_bridees``
    and moved device on top. That defect is invisible in the worst way. Home
    Assistant does not reject an unknown entity id in an automation: the
    trigger simply never fires, and a reader who copied the catalogue
    concludes the integration is broken.

    The check runs over the **whole** ``entity`` map of ``fr.json``, not over
    ``sensor.py``'s two tuples: the translations are already keyed by domain,
    so buttons, binary sensors and the account's own entities cost nothing
    extra to cover -- and five of the eleven lived precisely outside those
    tuples.

    Names are looked up in French and transliterated by Home Assistant's own
    ``slugify`` rather than by a local re-implementation, because the accent
    handling is exactly where this project's two repositories have already
    disagreed once. Re-implementing it here would test the guess.

    Only the presence of the suffix in backticks is asserted -- not the prose,
    not the attribute columns. The reverse direction is deliberately not
    checked: the annexe legitimately mentions per-period variants and other
    non-translated identifiers, and an heuristic for those would become a list
    of exceptions.
    """
    import json

    from homeassistant.util import slugify

    root = Path(__file__).resolve().parents[1]
    entities = json.loads(
        (root / "custom_components/carnet_scolaire/translations/fr.json").read_text(
            encoding="utf-8"
        )
    )["entity"]
    catalogue = (DOCS / "annexe-a-entites.md").read_text(encoding="utf-8")

    missing = sorted(
        f"{domain}.<é>_{slugify(entry['name'])} ({domain}/{key})"
        for domain, keys in entities.items()
        for key, entry in keys.items()
        if "name" in entry
        # A name carrying a placeholder is a template, not a name. The
        # per-period sensors are spelled "Notes ({period})" and Home Assistant
        # substitutes the establishment's own label for the period -- "Notes
        # (Trimestre 1)" -- so the entity suffix depends on data no test can
        # see, and there is no fixed string for this check to look for. Skipped
        # rather than guessed: asserting `_p<n>` here would pin a shorthand the
        # instance does not actually produce.
        and "{" not in entry["name"]
        and f"_{slugify(entry['name'])}`" not in catalogue
    )

    # The skip above must not be able to swallow the check. If a future rename
    # gave every entity a placeholder, the comprehension would find nothing
    # missing and this test would pass having verified nothing at all.
    checked = sum(
        1
        for keys in entities.values()
        for entry in keys.values()
        if "name" in entry and "{" not in entry["name"]
    )
    assert checked > 50, f"only {checked} entities were checked; the filter is wrong"

    assert not missing, (
        "these entities exist and annexe A does not list them: " + ", ".join(missing)
    )
