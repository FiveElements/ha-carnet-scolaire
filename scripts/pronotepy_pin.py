#!/usr/bin/env python3
"""Where the ``pronotepy`` pin is declared, and whether upstream moved past it.

This module exists because of an outage that lasted a morning. Every login
against one school started failing with a ``CryptoError`` that upstream labels
"probably the qr code has expired" -- a message that sends the reader after a QR
code which was, in fact, perfectly valid. The real cause was on the server
side: the establishment's PRONOTE had been upgraded to 26.2.5, and the pinned
``pronotepy==2.15.6`` predates the three-line rewrite of the challenge exchange
in ``ClientBase._login`` that upstream shipped as ``2.15.7`` under the commit
subject *"fix compatibility with PRONOTE 2026.2.5.6"*.

That release had existed for six days. Nothing in this repository said so, and
the version number alone would not have helped: what identified the cause was
the **commit subject**. So the job that uses this module reports the new
version, its date *and* the subjects between the two tags, which is the piece
of information that actually ends the investigation.

It reports them on a **pull request that raises the pin**, not on an issue.
The difference is what the maintainer gets to read: an issue says a newer
release exists, a pull request says whether it *works* -- because opening one
runs `validate.yml`, which exercises the whole suite and `mypy --strict`
against the new library on both socles. On the day of the outage that evidence
would have been available within minutes of upstream publishing, instead of
requiring somebody to reproduce the install by hand. What the pull request
cannot do is discharge the rule in `CONTRIBUTING.md` §5 -- every divergence
documented in ``hardened_client.py`` has to be re-read against the new version
by a human -- so nothing here merges anything.

Three design points worth keeping:

* the pinned version is **read**, never restated. It is already written twice
  --- ``requirements_test.txt`` is what CI tests against, ``manifest.json`` is
  what Home Assistant installs on a user's instance --- and a third copy inside
  a workflow would be a third thing to forget on the day the pin moves;
* those two copies are checked **against each other**. They can diverge in
  silence, and a divergence means the suite is not exercising the library users
  actually run. ``scripts/check_manifest.py`` makes that assertion on every
  push; this module holds the parser it uses;
* the bump itself lives here rather than in the workflow, as a text
  substitution over the two files, so it is unit-tested instead of being a
  ``sed`` line in YAML that nothing ever runs until the day it matters.

Standard library only, and every network call injectable: the unit tests in
``tests/test_pronotepy_pin.py`` run inside a suite that blocks sockets.

Usage::

    python scripts/pronotepy_pin.py                     # print the agreed pin
    python scripts/pronotepy_pin.py --check-upstream    # compare against PyPI
    python scripts/pronotepy_pin.py --bump 2.15.7       # move both declarations
"""

import argparse
from collections.abc import Callable
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import sys
from typing import Any
import urllib.request

PACKAGE = "pronotepy"
UPSTREAM_REPO = "bain3/pronotepy"

REQUIREMENTS = Path("requirements_test.txt")
MANIFEST = Path("custom_components/carnet_scolaire/manifest.json")
#: The same two paths as text, for messages. A ``Path`` formats with the
#: separator of the machine it runs on, and an issue body that says
#: ``custom_components\carnet_scolaire`` because the report happened to be produced
#: on Windows names a file nobody can click on.
REQUIREMENTS_NAME = REQUIREMENTS.as_posix()
MANIFEST_NAME = MANIFEST.as_posix()

#: One word that identifies everything this mechanism produces.
MARKER = "pronotepy-upstream"
#: The branch a bump is proposed on, one per upstream version. This is what
#: makes the job idempotent, and it is deliberately not a label: a label can be
#: absent in a fork or renamed by anyone with write access, and the job would
#: then propose the same version again every week.
BRANCH_PREFIX = "chore/pronotepy-"
#: Used only by the fallback issue, for the day a pull request cannot be
#: opened at all. That issue is found again by this prefix rather than by a
#: label, for the same reason: the marker has to be one the job cannot lose.
ISSUE_TITLE_PREFIX = f"[{MARKER}]"

_PYPI_URL = "https://pypi.org/pypi/{package}/json"
_COMPARE_URL = "https://api.github.com/repos/{repo}/compare/v{old}...v{new}"
_TIMEOUT = 20.0
_USER_AGENT = "ha-carnet-scolaire-pin-watch"

#: ``pronotepy==X.Y.Z`` in a requirements file, whatever version it names.
_REQUIREMENT = re.compile(
    rf"^{re.escape(PACKAGE)}==(?P<version>[^\s#;]+)", re.MULTILINE
)
#: A version, tolerating a leading ``v`` and a PEP 440 suffix.
_VERSION = re.compile(r"^v?(?P<release>\d+(?:\.\d+)*)(?P<suffix>[0-9A-Za-z.\-_+]*)$")

#: Fetches a URL and returns the decoded JSON. Injected in tests.
JsonFetcher = Callable[[str], Any]


class PinError(RuntimeError):
    """The repository's declarations of the pin are absent, or disagree.

    A hard failure on purpose: both callers -- the manifest gate and the weekly
    watch -- are worthless if they cannot say which version is pinned, and a
    watch that guesses would report against a version nobody ships.
    """


# ---------------------------------------------------------------------------
# Reading the pin out of the repository
# ---------------------------------------------------------------------------


def requirement_pin(text: str) -> str:
    """The version ``pronotepy`` is pinned to in a requirements file."""
    match = _REQUIREMENT.search(text)
    if match is None:
        message = (
            f"no `{PACKAGE}==` line found in {REQUIREMENTS_NAME}: the library must be "
            f"pinned exactly, not floating"
        )
        raise PinError(message)
    return match.group("version")


def manifest_pin(manifest: dict[str, Any]) -> str:
    """The version ``pronotepy`` is pinned to in ``manifest.json``.

    This is the one Home Assistant installs at runtime, so it is the one users
    actually run.
    """
    requirements = manifest.get("requirements") or []
    for entry in requirements:
        match = _REQUIREMENT.search(str(entry))
        if match is not None:
            return match.group("version")
    message = (
        f"manifest.json declares no pinned `{PACKAGE}`; its requirements are "
        f"{list(requirements)!r}"
    )
    raise PinError(message)


def pinned_version(root: Path | None = None) -> str:
    """The pin, read from both declarations, which must agree.

    ``manifest.json`` is installed on the user's instance;
    ``requirements_test.txt`` is what the suite runs against. When they drift,
    CI passes against a library nobody ships -- and the difference shows up as
    a bug report, since the divergences ``hardened_client.py`` corrects are
    version-specific by nature.
    """
    base = Path() if root is None else root
    from_requirements = requirement_pin(
        (base / REQUIREMENTS).read_text(encoding="utf-8")
    )
    manifest: dict[str, Any] = json.loads((base / MANIFEST).read_text(encoding="utf-8"))
    from_manifest = manifest_pin(manifest)

    if from_requirements != from_manifest:
        message = (
            f"the two declarations of the {PACKAGE} pin disagree: "
            f"{REQUIREMENTS_NAME} says {from_requirements}, {MANIFEST_NAME} says "
            f"{from_manifest}. manifest.json is what Home Assistant installs "
            f"and requirements_test.txt is what the suite tests, so a "
            f"divergence means the tests do not exercise what users run."
        )
        raise PinError(message)

    return from_requirements


# ---------------------------------------------------------------------------
# Comparing versions
# ---------------------------------------------------------------------------


def version_key(version: str) -> tuple[tuple[int, ...], int, str]:
    """An orderable key for a version string.

    Enough of PEP 440 to be right about this package, and no more: the release
    segment as integers with trailing zeroes dropped (so ``2.15`` and
    ``2.15.0`` compare equal), then a rank placing a pre-release below the
    matching final release, then the suffix itself as a tie-break.

    ``packaging`` would do this properly, but it is not in the standard
    library, and making the weekly watch install the Home Assistant test stack
    to compare two dotted numbers would be a poor trade.
    """
    match = _VERSION.match(version.strip())
    if match is None:
        message = f"cannot read {version!r} as a version"
        raise PinError(message)

    release = [int(part) for part in match.group("release").split(".")]
    while len(release) > 1 and release[-1] == 0:
        release.pop()

    suffix = match.group("suffix").lstrip(".-_").lower()
    if not suffix:
        rank = 0
    elif suffix.startswith(("post", "rev", "+")):
        rank = 1
    else:
        rank = -1

    return (tuple(release), rank, suffix)


def classify(pinned: str, upstream: str) -> str:
    """``"outdated"``, ``"current"`` or ``"ahead"``.

    ``"ahead"`` is not hypothetical: the pin is raised in this repository
    before it is anything else, and a watch that reported "a new release
    exists" while the pin was already past it would be noise.
    """
    ours, theirs = version_key(pinned), version_key(upstream)
    if theirs > ours:
        return "outdated"
    if theirs == ours:
        return "current"
    return "ahead"


# ---------------------------------------------------------------------------
# Asking upstream
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Release:
    """An upstream release, as PyPI describes it."""

    version: str
    #: ISO-8601 upload time, or ``""`` when PyPI did not say.
    released: str


def fetch_json(url: str) -> Any:
    """Fetch and decode JSON, authenticating to GitHub when a token is around.

    The compare endpoint is public, but an unauthenticated runner shares its
    rate limit with every other job on the same egress address; the workflow
    token raises that limit and costs nothing.
    """
    headers = {"Accept": "application/json", "User-Agent": _USER_AGENT}
    token = os.environ.get("GITHUB_TOKEN", "")
    if token and url.startswith("https://api.github.com/"):
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)  # noqa: S310
    with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:  # noqa: S310
        payload: Any = json.loads(response.read().decode("utf-8"))
    return payload


def latest_release(
    fetch: JsonFetcher = fetch_json, package: str = PACKAGE
) -> Release | None:
    """The newest release PyPI publishes, or ``None`` when it cannot be read.

    ``None`` rather than an exception, because this runs on a schedule: a PyPI
    outage must produce a report and a clean exit. A job that goes red every
    time a third party hiccups teaches its audience to ignore it, which costs
    more than the check is worth.
    """
    try:
        payload = fetch(_PYPI_URL.format(package=package))
        version = str(payload["info"]["version"])
        released = ""
        for file_entry in payload.get("urls") or []:
            released = str(file_entry.get("upload_time_iso_8601") or "")
            if released:
                break
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"PyPI unreadable ({error!r}); nothing reported this run.")
        return None
    return Release(version=version, released=released)


def upstream_subjects(
    old: str,
    new: str,
    fetch: JsonFetcher = fetch_json,
    repo: str = UPSTREAM_REPO,
) -> list[str] | None:
    """Commit subjects between the two tags, or ``None`` if they do not resolve.

    This is the payload of the whole exercise. The outage in the module
    docstring was ended by reading one subject line; a report that made a
    maintainer go and find it by hand would have saved nobody.

    ``None``, not ``[]``: upstream's tag naming is a convention
    (``v2.15.7`` today) and not a promise, so "no commits between the tags" and
    "the tags do not exist" must read differently in the issue.
    """
    try:
        payload = fetch(_COMPARE_URL.format(repo=repo, old=old, new=new))
        commits = payload["commits"]
        subjects = [
            str(commit["commit"]["message"]).strip().splitlines()[0].strip()
            for commit in commits
            if str(commit.get("commit", {}).get("message", "")).strip()
        ]
    except (OSError, ValueError, KeyError, TypeError, IndexError) as error:
        print(f"tag comparison unavailable ({error!r}); reporting PyPI alone.")
        return None
    return subjects


# ---------------------------------------------------------------------------
# Moving the pin
# ---------------------------------------------------------------------------


def bump(root: Path, new_version: str) -> list[str]:
    """Move both declarations of the pin to ``new_version``.

    Text substitution, not a JSON round-trip: ``manifest.json`` has a key order
    hassfest enforces and a two-space indentation, and re-serialising it would
    rewrite lines this change has no business touching -- which is also what
    makes the resulting diff readable, two lines and nothing else.

    The version being replaced is read from the files themselves rather than
    passed in, so a caller cannot bump the wrong number, and the line endings
    are left exactly as they were found.
    """
    old = pinned_version(root)
    changed: list[str] = []
    for relative in (REQUIREMENTS, MANIFEST):
        path = root / relative
        text = path.read_text(encoding="utf-8", newline="")
        replaced = text.replace(f"{PACKAGE}=={old}", f"{PACKAGE}=={new_version}")
        if replaced != text:
            path.write_text(replaced, encoding="utf-8", newline="")
            changed.append(relative.as_posix())
    return changed


def branch_name(version: str) -> str:
    """The one branch a given version is ever proposed on.

    Stable and derived from the version, which is what makes the job
    idempotent: a second run for the same release finds the branch and the
    pull request already there and refreshes them, while a *newer* release
    gets a branch of its own rather than silently overwriting the proposal a
    maintainer may already be reading.
    """
    return f"{BRANCH_PREFIX}{version}"


def pull_request_title(release: Release) -> str:
    """A conventional-commit subject, because it becomes the commit subject."""
    return f"chore(deps): raise the {PACKAGE} pin to {release.version}"


def commit_message(release: Release, pinned: str, subjects: list[str] | None) -> str:
    """The commit the branch carries: what moved, and why it might matter."""
    date = release.released.split("T")[0] if release.released else "unknown date"
    body = [
        pull_request_title(release),
        "",
        (
            f"{PACKAGE} {release.version} was published on {date}; this repository "
            f"pinned {pinned}."
        ),
        "",
        f"Upstream commits between v{pinned} and v{release.version}:",
        "",
    ]
    if subjects is None:
        body.append("  (the two tags did not resolve upstream)")
    elif not subjects:
        body.append("  (no commits between the two tags)")
    else:
        body += [f"  - {subject}" for subject in subjects]
    body += [
        "",
        (
            "Opened by .github/workflows/pronotepy-watch.yml. Raising this pin "
            "requires re-reading every divergence documented in "
            "hardened_client.py (CONTRIBUTING.md §5), which is a human's job, so "
            "this is a proposal and not a merge."
        ),
    ]
    return "\n".join(body) + "\n"


# ---------------------------------------------------------------------------
# The report a maintainer reads
# ---------------------------------------------------------------------------


def compare_url(pinned: str, upstream: str, repo: str = UPSTREAM_REPO) -> str:
    """The human-readable diff between the two tags."""
    return f"https://github.com/{repo}/compare/v{pinned}...v{upstream}"


def _evidence(
    release: Release, pinned: str, subjects: list[str] | None, repo: str
) -> list[str]:
    """The four facts, in the order they get read.

    Pinned version, new version, its date, and the upstream commit subjects.
    The fourth is the one that was missing the morning this mechanism comes
    from: the cause was named in a commit subject, and a report that made
    somebody go and find it would have saved nobody.
    """
    date = release.released.split("T")[0] if release.released else "date inconnue"
    lines = [
        "| | |",
        "| --- | --- |",
        f"| Épinglé | `{pinned}` — `{REQUIREMENTS_NAME}` et `{MANIFEST_NAME}` |",
        f"| Amont | `{release.version}`, publié le {date} |",
        f"| Comparaison | {compare_url(pinned, release.version, repo)} |",
        "",
        f"## Ce que l'amont a changé entre `v{pinned}` et `v{release.version}`",
        "",
    ]
    if subjects is None:
        lines.append(
            "Les deux tags ne se résolvent pas chez l'amont — la convention de "
            "nommage a peut-être changé. Les sujets de commit sont à lire "
            "directement dans la comparaison ci-dessus ; le reste de ce rapport "
            "ne dépend que de PyPI."
        )
    elif not subjects:
        lines.append(
            "Aucun commit entre les deux tags, ce qui est en soi une "
            "information : la publication ne porte rien de fonctionnel."
        )
    else:
        lines += [f"- {subject}" for subject in subjects]
    return lines


_REREAD_RULE = (
    "`hardened_client.py` corrige des comportements précis de l'amont, et "
    "chaque divergence y est documentée avec la version qui la motive. "
    "Relever l'épingle veut dire **relire chacune** de ces divergences : un "
    "correctif amont peut avoir rendu l'une inutile, ou l'avoir rendue fausse. "
    "C'est la règle du §5 de `CONTRIBUTING.md`, et elle ne se délègue pas à la "
    "CI, qui voit qu'un contournement ne casse rien, pas qu'il est devenu "
    "superflu."
)


def pull_request_body(
    release: Release,
    pinned: str,
    subjects: list[str] | None,
    *,
    repo: str = UPSTREAM_REPO,
) -> str:
    """The proposal: the evidence, then what stops it being merged on its own."""
    lines = [
        f"<!-- {MARKER} -->",
        "",
        (
            f"Cette *pull request* porte l'épingle `{PACKAGE}` de **{pinned}** à "
            f"**{release.version}**, dans les deux déclarations qui doivent bouger "
            f"ensemble : `{REQUIREMENTS_NAME}` (ce que la suite teste) et "
            f"`{MANIFEST_NAME}` (ce que Home Assistant installe chez "
            "l'utilisateur). Elle existe pour que la question « est-ce que ça "
            "marche ? » soit répondue par `Validate` plutôt que par quelqu'un qui "
            "reproduit l'installation à la main."
        ),
        "",
        *_evidence(release, pinned, subjects, repo),
        "",
        "## À faire avant de fusionner",
        "",
        (
            "- [ ] relire **chaque** divergence documentée dans "
            "`custom_components/carnet_scolaire/hardened_client.py` : laquelle cette "
            "version rend inutile, laquelle elle rend fausse (`CONTRIBUTING.md` §5)"
        ),
        (
            "- [ ] lire les sujets de commit ci-dessus, et pas seulement le numéro "
            "de version"
        ),
        (
            "- [ ] `Validate` au vert sur les deux socles — le plancher épinglé et "
            "la ligne flottante"
        ),
        "- [ ] mettre à jour `docs/` si une divergence documentée disparaît",
        "",
        (
            "**Rien ici ne se fusionne tout seul.** Aucune fusion automatique n'est "
            "activée, et ce n'est pas une précaution de forme : la relecture des "
            "divergences ci-dessus est la seule chose qui distingue une montée de "
            "version d'une régression silencieuse, et aucun portail ne peut la "
            "faire à la place d'un humain."
        ),
        "",
        (
            "> Les vérifications ne démarrent pas d'elles-mêmes sur une *pull "
            "request* ouverte par `GITHUB_TOKEN` : GitHub ne déclenche aucun "
            "workflow à partir des évènements d'un jeton d'Actions, pour éviter "
            "les boucles. Le job demande donc `Validate` explicitement sur la "
            "branche (`workflow_dispatch`). Si aucun résultat n'apparaît, fermer "
            "puis réouvrir cette *pull request* relance les portails."
        ),
        "",
        "---",
        "",
        (
            "Ouverte automatiquement par `.github/workflows/pronotepy-watch.yml`, "
            "qui existe parce qu'une panne totale de connexion a coûté une matinée "
            "alors que le correctif amont était publié depuis six jours, et que ce "
            "qui a permis de conclure était un sujet de commit."
        ),
    ]
    return "\n".join(lines) + "\n"


def fallback_issue_title(release: Release, pinned: str) -> str:
    """Stable prefix, then the two versions -- what a notification shows."""
    return (
        f"{ISSUE_TITLE_PREFIX} {PACKAGE} {release.version} est disponible "
        f"(épingle : {pinned})"
    )


def fallback_issue_body(
    release: Release,
    pinned: str,
    subjects: list[str] | None,
    *,
    repo: str = UPSTREAM_REPO,
) -> str:
    """The same evidence, for the day the pull request cannot be opened.

    A repository can forbid Actions from creating pull requests, a branch can
    be protected, a push can be refused. None of that is a reason for the
    information to be lost -- which was the entire failure being corrected
    here -- so the report still arrives, as an issue, saying why it is one.
    """
    lines = [
        f"<!-- {MARKER} -->",
        "",
        (
            f"`{PACKAGE}` **{release.version}** est publié ; ce dépôt épingle "
            f"**{pinned}**."
        ),
        "",
        (
            "La *pull request* qui aurait porté l'épingle n'a pas pu être créée — "
            "Actions n'a peut-être pas le droit d'ouvrir une *pull request* dans ce "
            "dépôt, ou la poussée de branche a été refusée. Le rapport arrive donc "
            "sous cette forme, et la montée de version est à faire à la main."
        ),
        "",
        *_evidence(release, pinned, subjects, repo),
        "",
        "## Avant de relever l'épingle",
        "",
        _REREAD_RULE,
        "",
        (
            f"Les deux déclarations bougent ensemble : `{REQUIREMENTS_NAME}` et "
            f"`{MANIFEST_NAME}`."
        ),
        "",
        "---",
        "",
        "Ouverte automatiquement par `.github/workflows/pronotepy-watch.yml`.",
    ]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------


def _emit_outputs(values: dict[str, str]) -> None:
    """Append ``key=value`` pairs to ``$GITHUB_OUTPUT`` when running in Actions."""
    destination = os.environ.get("GITHUB_OUTPUT", "")
    if not destination:
        return
    with Path(destination).open("a", encoding="utf-8") as handle:
        handle.writelines(f"{key}={value}\n" for key, value in values.items())


def _check_upstream(pinned: str, out_dir: Path | None) -> int:
    """Compare the pin against PyPI and write the reports. Always exits 0."""
    release = latest_release()
    if release is None:
        _emit_outputs({"outcome": "unavailable", "pinned": pinned})
        return 0

    try:
        outcome = classify(pinned, release.version)
    except PinError as error:
        # A version string upstream shapes in a way this cannot order. The pin
        # itself is fine, so this is somebody else's oddity and is reported the
        # same way an outage is: the alternative is a red run every Tuesday
        # until whatever PyPI now returns is understood.
        print(f"cannot compare against PyPI ({error}); nothing proposed.")
        _emit_outputs({"outcome": "unavailable", "pinned": pinned})
        return 0

    date = release.released or "?"
    print(f"pinned {pinned}, PyPI {release.version} ({date}): {outcome}")

    outputs = {
        "outcome": outcome,
        "pinned": pinned,
        "latest": release.version,
        "released": release.released,
        "branch": branch_name(release.version),
    }
    if outcome != "outdated":
        # The quiet outcome, and the common one. Nothing is written, nothing is
        # opened, and the run summary still says what was compared -- a green
        # tick with no explanation is indistinguishable from a job that has
        # stopped working.
        _emit_outputs(outputs)
        return 0

    subjects = upstream_subjects(pinned, release.version)
    for subject in subjects or []:
        print(f"  - {subject}")

    documents = {
        "pr_body": pull_request_body(release, pinned, subjects),
        "issue_body": fallback_issue_body(release, pinned, subjects),
        "commit_message": commit_message(release, pinned, subjects),
    }
    outputs["pr_title"] = pull_request_title(release)
    outputs["issue_title"] = fallback_issue_title(release, pinned)

    if out_dir is None:
        for name, content in documents.items():
            print(f"\n----- {name} -----\n{content}")
    else:
        out_dir.mkdir(parents=True, exist_ok=True)
        for name, content in documents.items():
            path = out_dir / f"{name}.md"
            path.write_text(content, encoding="utf-8")
            # Paths are passed on rather than restated: the workflow needs
            # them, and the ``runner.temp`` they live under is only knowable
            # from inside a step.
            outputs[f"{name}_path"] = str(path)
        print(f"reports written to {out_dir}")

    _emit_outputs(outputs)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check-upstream",
        action="store_true",
        help="query PyPI and report whether a newer release exists",
    )
    parser.add_argument(
        "--reports",
        type=Path,
        metavar="DIR",
        help="write the pull request body, the fallback issue body and the "
        "commit message into this directory instead of standard output",
    )
    parser.add_argument(
        "--bump",
        metavar="VERSION",
        help="rewrite both declarations of the pin to this version",
    )
    args = parser.parse_args(argv)

    try:
        pinned = pinned_version()
    except (PinError, OSError) as error:
        print(f"pin check failed: {error}", file=sys.stderr)
        return 1

    if args.bump:
        changed = bump(Path(), args.bump)
        print(f"{PACKAGE} {pinned} -> {args.bump} in {changed or 'no file'}")
        return 0

    if not args.check_upstream:
        print(f"{PACKAGE}=={pinned} (both declarations agree)")
        return 0

    return _check_upstream(pinned, args.reports)


if __name__ == "__main__":
    sys.exit(main())
