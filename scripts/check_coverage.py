#!/usr/bin/env python3
"""Enforce the coverage thresholds of SPECIFICATION.md §11.

A threshold that does not fail is not a threshold: this script exits non-zero
when any module of the package drops below 95 %, when the global line+branch
coverage drops below 80 %, or when any of the seven modules where a mistake is
invisible at runtime drops below 100 %.

Keys are path *suffixes* (``carnet_scolaire/ratelimit.py``), never basenames.
Indexing by ``Path(filename).name`` would let a second ``ratelimit.py``
(an Ecoledirecte limiter, say) overwrite the Pronote row and disarm the
gate while CI stayed green.
"""

from pathlib import Path
import sys
import xml.etree.ElementTree as ET

GLOBAL_MINIMUM = 80.0

#: Every module of the package, with no exception list. The Home Assistant
#: quality scale asks for this at Silver (`test-coverage`), and the reason it
#: is a *per-module* floor rather than a global one is that a global average
#: hides exactly what matters: this package carries four modules above five
#: hundred statements, so a module of forty can fall to nothing without moving
#: the global figure by a point. The global floor stays as a second, weaker
#: guard.
MODULE_MINIMUM = 95.0

# The four modules where an error produces no visible symptom: a rate limiter
# that lets a call through, a scheduler that skips a tier, a gateway that
# silently mis-decodes, a delta detector that misses a change.
#
# Suffixes under the package, not basenames: see the module docstring.
CRITICAL_MODULES = {
    "carnet_scolaire/connectors/ecoledirecte/ed_client.py": 100.0,
    "carnet_scolaire/connectors/ecoledirecte/ed_limiter.py": 100.0,
    "carnet_scolaire/connectors/ecoledirecte/ed_mapping.py": 100.0,
    "carnet_scolaire/ratelimit.py": 100.0,
    "carnet_scolaire/scheduler.py": 100.0,
    "carnet_scolaire/gateway.py": 100.0,
    "carnet_scolaire/delta.py": 100.0,
}


def _posix(filename: str) -> str:
    """Normalise a coverage.xml path so Windows and POSIX reports compare."""
    return Path(filename).as_posix().replace("\\", "/")


def _report_paths(root: ET.Element, filename: str) -> tuple[str, ...]:
    """Filenames as written, plus each ``<source>`` joined in front.

    ``pytest --cov=custom_components/carnet_scolaire`` writes Cobertura basenames
    (``ratelimit.py``) and puts the package directory in ``<source>``. A
    suffix lookup of ``carnet_scolaire/ratelimit.py`` only works after that join.
    A second ``ratelimit.py`` under ``connectors/ecoledirecte/`` stays a
    different path: it does not end with ``/carnet_scolaire/ratelimit.py``.
    """
    filename = _posix(filename)
    paths = [filename]
    for source in root.iter("source"):
        text = (source.text or "").strip()
        if text:
            paths.append(_posix(f"{text.rstrip('/')}/{filename}"))
    return tuple(paths)


def _class_coverage(class_element: ET.Element) -> float:
    """``min(line, branch)`` for one file, as one figure.

    Lines and branches go into a single denominator rather than being
    compared: a module whose lines are all executed but whose conditions are
    only half taken reads as half covered, which is what a branch nobody
    exercised deserves to look like.
    """
    covered = total = 0
    for line in class_element.iter("line"):
        total += 1
        covered += 1 if line.get("hits", "0") != "0" else 0
        condition = line.get("condition-coverage")
        if condition and "(" in condition:
            fraction = condition.split("(", 1)[1].rstrip(")")
            taken, possible = (int(part) for part in fraction.split("/"))
            total += possible
            covered += taken
    return 100.0 * covered / total if total else 100.0


def _module_coverage(root: ET.Element) -> dict[str, float]:
    """Return per-file coverage keyed by every path the report can name."""
    result: dict[str, float] = {}
    for class_element in root.iter("class"):
        filename = _posix(class_element.get("filename", ""))
        percent = _class_coverage(class_element)
        for path in _report_paths(root, filename):
            result[path] = percent
    return result


def _package_modules(root: ET.Element) -> dict[str, float]:
    """Every module of the package, keyed by its path under ``carnet_scolaire/``.

    Built from the report rather than from a list in this file, so a module
    added tomorrow is held to the floor without anybody remembering to
    register it. A list would have been the quieter failure: the gate would
    stay green over a new module with no tests at all.
    """
    marker = "carnet_scolaire/"
    result: dict[str, float] = {}
    for class_element in root.iter("class"):
        filename = _posix(class_element.get("filename", ""))
        for path in _report_paths(root, filename):
            if marker not in path:
                continue
            result[path[path.index(marker) :]] = _class_coverage(class_element)
            break
    return result


def coverage_match(
    per_module: dict[str, float], required: str
) -> tuple[float | None, str]:
    """Look up ``required`` as an exact key or unique posix suffix.

    Two paths that share a basename do not match a short suffix such as
    ``carnet_scolaire/ratelimit.py``:
    ``carnet_scolaire/connectors/ecoledirecte/ratelimit.py`` does not end with
    that string.

    Returns ``(value, "found")``, ``(None, "absent")``, or
    ``(None, "ambiguous")``. Ambiguous is fail-closed, but it is not the
    same as a missing file: CI must say which.
    """
    required = _posix(required)
    if required in per_module:
        return per_module[required], "found"
    suffix = required if required.startswith("/") else f"/{required}"
    matches = [
        value
        for name, value in per_module.items()
        if _posix(name) == required or _posix(name).endswith(suffix)
    ]
    if len(matches) == 1:
        return matches[0], "found"
    if len(matches) > 1:
        return None, "ambiguous"
    return None, "absent"


def coverage_for(per_module: dict[str, float], required: str) -> float | None:
    """Look up ``required``; ``None`` means absent or ambiguous (fail closed)."""
    value, _reason = coverage_match(per_module, required)
    return value


def _critical_failure(
    module: str, actual: float | None, reason: str, minimum: float
) -> str | None:
    if reason == "absent":
        return f"{module} is absent from the coverage report"
    if reason == "ambiguous":
        return f"{module} matches more than one file in the coverage report"
    if actual is not None and actual + 1e-9 < minimum:
        return f"{module} coverage {actual:.2f}% is below the required {minimum:.0f}%"
    return None


def _shown(value: float | None, reason: str) -> str:
    if reason == "found" and value is not None:
        return f"{value:.2f}%"
    if reason == "ambiguous":
        return "ambiguous"
    return "missing"


def _critical_failures(per_module: dict[str, float]) -> list[str]:
    """Every breach among the modules held at 100 %, in path order."""
    failures = []
    for module, minimum in sorted(CRITICAL_MODULES.items()):
        actual, reason = coverage_match(per_module, module)
        failure = _critical_failure(module, actual, reason, minimum)
        if failure is not None:
            failures.append(failure)
    return failures


def _thin_modules(package: dict[str, float]) -> list[tuple[str, float]]:
    """The modules under the per-module floor, excluding the gated seven.

    Those are held higher elsewhere, and reporting one twice would let a
    single regression print two failures -- which reads as two problems.
    """
    return [
        (module, actual)
        for module, actual in sorted(package.items())
        if module not in CRITICAL_MODULES and actual + 1e-9 < MODULE_MINIMUM
    ]


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: check_coverage.py <coverage.xml>", file=sys.stderr)
        return 2

    report = Path(argv[1])
    if not report.is_file():
        print(f"coverage report not found: {report}", file=sys.stderr)
        return 2

    root = ET.parse(report).getroot()  # noqa: S314 -- our own CI artifact

    failures: list[str] = []

    line_rate = float(root.get("line-rate", "0")) * 100.0
    branch_rate = float(root.get("branch-rate", "0")) * 100.0
    overall = min(line_rate, branch_rate) if branch_rate else line_rate
    if overall < GLOBAL_MINIMUM:
        failures.append(
            f"global coverage {overall:.2f}% is below the {GLOBAL_MINIMUM:.0f}% floor"
        )

    per_module = _module_coverage(root)
    failures.extend(_critical_failures(per_module))

    thin = _thin_modules(_package_modules(root))
    failures.extend(
        f"{module} coverage {actual:.2f}% is below the required {MODULE_MINIMUM:.0f}%"
        for module, actual in thin
    )

    print(f"global: line {line_rate:.2f}% / branch {branch_rate:.2f}%")
    for module in sorted(CRITICAL_MODULES):
        value, reason = coverage_match(per_module, module)
        print(f"  {module}: {_shown(value, reason)}")
    if thin:
        print(f"below the {MODULE_MINIMUM:.0f}% per-module floor:")
        for module, actual in thin:
            print(f"  {module}: {actual:.2f}%")

    if failures:
        print("\nCoverage gate failed:", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1

    print("\nCoverage gate passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
