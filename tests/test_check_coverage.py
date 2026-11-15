"""The coverage gate must not disarm itself when two files share a basename.

``scripts/check_coverage.py`` used to key modules by ``Path(filename).name``.
A second ``ratelimit.py`` (an Ecoledirecte limiter under the same package)
would overwrite the Pronote row: the 100 % bar would measure the new file,
Pronote's limiter could drop in silence, and CI would stay green. That is the
same family of defect as a ``--since`` argument the citation gate used to
ignore.
"""

import importlib.util
from pathlib import Path
from typing import TYPE_CHECKING
from xml.etree.ElementTree import Element, ElementTree, SubElement, tostring

if TYPE_CHECKING:
    import pytest

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "check_coverage", ROOT / "scripts" / "check_coverage.py"
)
assert _SPEC is not None
assert _SPEC.loader is not None
check_coverage = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(check_coverage)


def _class(parent: Element, filename: str, hits: str) -> None:
    """One coverage class with a single line of known hits."""
    item = SubElement(parent, "class", filename=filename)
    lines = SubElement(item, "lines")
    SubElement(lines, "line", number="1", hits=hits)


def _report(*files: tuple[str, str], source: str | None = None) -> Element:
    root = Element("coverage", attrib={"line-rate": "1.0", "branch-rate": "1.0"})
    if source is not None:
        sources = SubElement(root, "sources")
        element = SubElement(sources, "source")
        element.text = source
    packages = SubElement(root, "packages")
    package = SubElement(packages, "package")
    classes = SubElement(package, "classes")
    for filename, hits in files:
        _class(classes, filename, hits)
    return root


def test_a_second_ratelimit_py_does_not_replace_the_pronote_row() -> None:
    """The Pronote limiter stays the gated file when an ED limiter shares the name."""
    root = _report(
        ("custom_components/carnet_scolaire/ratelimit.py", "1"),
        ("custom_components/carnet_scolaire/connectors/ecoledirecte/ratelimit.py", "0"),
    )
    per_module = check_coverage._module_coverage(root)
    assert (
        check_coverage.coverage_for(per_module, "carnet_scolaire/ratelimit.py") == 100.0
    )
    ed = check_coverage.coverage_for(
        per_module, "carnet_scolaire/connectors/ecoledirecte/ratelimit.py"
    )
    assert ed == 0.0


def test_the_gate_finds_pronote_modules_under_the_package_prefix() -> None:
    """coverage.xml stores a prefix; the required key is the package suffix."""
    root = _report(("custom_components/carnet_scolaire/gateway.py", "1"))
    per_module = check_coverage._module_coverage(root)
    assert (
        check_coverage.coverage_for(per_module, "carnet_scolaire/gateway.py") == 100.0
    )


def test_the_three_ecoledirecte_protocol_modules_are_critical() -> None:
    """An invisible ED admission or decode defect must fail the coverage gate."""
    required = check_coverage.CRITICAL_MODULES
    assert required["carnet_scolaire/connectors/ecoledirecte/ed_limiter.py"] == 100.0
    assert required["carnet_scolaire/connectors/ecoledirecte/ed_client.py"] == 100.0
    assert required["carnet_scolaire/connectors/ecoledirecte/ed_mapping.py"] == 100.0


def test_a_package_relative_cobertura_report_joins_source_to_find_pronote() -> None:
    """CI writes basenames; joining <source> is what makes the suffix exist."""
    root = _report(
        ("ratelimit.py", "1"),
        ("connectors/ecoledirecte/ratelimit.py", "0"),
        source="/home/runner/work/ha-carnet-scolaire/ha-carnet-scolaire/custom_components/carnet_scolaire",
    )
    per_module = check_coverage._module_coverage(root)
    assert (
        check_coverage.coverage_for(per_module, "carnet_scolaire/ratelimit.py") == 100.0
    )
    ed = check_coverage.coverage_for(
        per_module, "carnet_scolaire/connectors/ecoledirecte/ratelimit.py"
    )
    assert ed == 0.0


def test_an_ambiguous_suffix_is_missing_so_the_gate_fails_closed() -> None:
    """Two files ending with the same required path must not pick a winner."""
    root = _report(
        ("a/carnet_scolaire/delta.py", "1"),
        ("b/carnet_scolaire/delta.py", "0"),
    )
    per_module = check_coverage._module_coverage(root)
    assert check_coverage.coverage_for(per_module, "carnet_scolaire/delta.py") is None


def test_tostring_keeps_the_fixture_well_formed() -> None:
    """Guard the helper: a broken fixture would make the lookups pass vacuously."""
    xml = tostring(_report(("carnet_scolaire/scheduler.py", "1")), encoding="unicode")
    assert "scheduler.py" in xml


def _write_report(path: Path, root: Element) -> None:
    ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)


def test_main_exits_nonzero_when_the_coverage_report_is_missing() -> None:
    """A threshold that does not fail is not a threshold."""
    code = check_coverage.main(["check_coverage.py", "no-such-coverage.xml"])
    assert code == 2


def test_main_names_an_ambiguous_suffix_instead_of_calling_it_absent(
    tmp_path: Path, capsys
) -> None:
    """Fail-closed that says 'absent' sends a CI reader looking for a missing file."""
    report = tmp_path / "coverage.xml"
    _write_report(
        report,
        _report(
            ("a/carnet_scolaire/delta.py", "1"),
            ("b/carnet_scolaire/delta.py", "0"),
            ("custom_components/carnet_scolaire/ratelimit.py", "1"),
            ("custom_components/carnet_scolaire/scheduler.py", "1"),
            ("custom_components/carnet_scolaire/gateway.py", "1"),
        ),
    )
    code = check_coverage.main(["check_coverage.py", str(report)])
    captured = capsys.readouterr()
    assert code == 1
    assert (
        "carnet_scolaire/delta.py matches more than one file in the coverage report"
        in (captured.err)
    )
    assert (
        "carnet_scolaire/delta.py is absent from the coverage report"
        not in captured.err
    )


def test_main_says_absent_when_a_critical_module_is_missing(
    tmp_path: Path, capsys
) -> None:
    """The two failure modes must stay distinguishable."""
    report = tmp_path / "coverage.xml"
    _write_report(
        report,
        _report(
            ("custom_components/carnet_scolaire/ratelimit.py", "1"),
            ("custom_components/carnet_scolaire/scheduler.py", "1"),
            ("custom_components/carnet_scolaire/gateway.py", "1"),
        ),
    )
    code = check_coverage.main(["check_coverage.py", str(report)])
    captured = capsys.readouterr()
    assert code == 1
    assert "carnet_scolaire/delta.py is absent from the coverage report" in captured.err
    assert "more than one file" not in captured.err


def _all_critical_at_100() -> tuple[tuple[str, str], ...]:
    """Every gated module fully covered, so only the new floor can fail."""
    return tuple(
        (f"custom_components/{module}", "1")
        for module in check_coverage.CRITICAL_MODULES
    )


def test_a_module_below_the_per_module_floor_fails_the_gate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Silver `test-coverage` asks for 95 % on *every* module, not on average.

    A global floor hides exactly what matters here: this package carries four
    modules above five hundred statements, so a module of forty can fall to
    nothing without moving the global figure by a point. The global rate in
    this fixture is a perfect 1.0 and the gate must still fail.
    """
    report = tmp_path / "coverage.xml"
    _write_report(
        report,
        _report(
            *_all_critical_at_100(),
            ("custom_components/carnet_scolaire/image.py", "0"),
        ),
    )

    code = check_coverage.main(["check_coverage.py", str(report)])
    captured = capsys.readouterr()

    assert code == 1
    assert (
        "carnet_scolaire/image.py coverage 0.00% is below the required 95%"
        in captured.err
    )


def test_a_module_nobody_registered_is_still_held_to_the_floor(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The floor is read off the report, never off a list in the script.

    A list would have been the quieter failure of the two: a module added
    tomorrow with no tests at all would sit outside it, and the gate would
    stay green over the one file most likely to be wrong.
    """
    report = tmp_path / "coverage.xml"
    _write_report(
        report,
        _report(
            *_all_critical_at_100(),
            (
                "custom_components/carnet_scolaire/a_module_invented_for_this_test.py",
                "0",
            ),
        ),
    )

    code = check_coverage.main(["check_coverage.py", str(report)])

    assert code == 1
    assert "a_module_invented_for_this_test.py" in capsys.readouterr().err


def test_a_critical_module_is_reported_once_and_not_twice(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """One regression, one line.

    The seven gated modules are held at 100 %, which is above the 95 % floor,
    so an uncovered one satisfies both rules' failure condition. Printing it
    twice would make a CI reader count two problems and look for a second
    cause that does not exist.
    """
    report = tmp_path / "coverage.xml"
    _write_report(
        report,
        _report(
            *(
                (f"custom_components/{module}", "0" if "delta" in module else "1")
                for module in check_coverage.CRITICAL_MODULES
            ),
        ),
    )

    code = check_coverage.main(["check_coverage.py", str(report)])
    errors = [
        line
        for line in capsys.readouterr().err.splitlines()
        if "carnet_scolaire/delta.py" in line
    ]

    assert code == 1
    assert len(errors) == 1
