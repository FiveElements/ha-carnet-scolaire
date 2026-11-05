#!/usr/bin/env python3
"""Where ``ECOLEDIRECTE_API_VERSION`` is declared, and whether upstream moved.

This module exists because a 517 from ``api.ecoledirecte.com`` is not a
transient error: it means the ``v=`` query parameter this integration sends
is no longer accepted. The constant lives in ``ed_client.py`` and is copied
from ``APIVERSION`` in ``ecoledirecte_api`` 0.3.0 -- the handshake we
reimplemented rather than imported, because that package's ``backoff`` would
re-login outside our limiter and its DEBUG logger leaks the token
(spécification connecteurs §7.1).

Nothing here installs the ``ecoledirecte`` PyPI package. The weekly job reads
``APIVERSION`` out of the published sdist or wheel -- text in an archive --
and compares it to the constant in this repository. If they differ, the job
**fails** (a red weekly run, not a log line) **and** opens a pull request that
rewrites the constant. Validate is asked to run on that branch. Nothing
merges itself.

A PyPI or GitHub outage reports ``unavailable`` and exits 0. A weekly job that
goes red when a third party hiccups is a weekly job people stop reading, and
the one run that would have said "517 is coming" would be ignored with the
rest.

Standard library only, and every network call injectable.

Usage::

    python scripts/ecoledirecte_api_version.py                  # print ours
    python scripts/ecoledirecte_api_version.py --check-upstream # compare
    python scripts/ecoledirecte_api_version.py --bump 4.102.0   # rewrite
"""

import argparse
from collections.abc import Callable
from dataclasses import dataclass
import io
import json
import os
from pathlib import Path
import re
import sys
import tarfile
from typing import Any
import urllib.error
import urllib.request
import zipfile

PACKAGE = "ecoledirecte"
UPSTREAM_REPO = "hacf-fr/ecoledirecte_api"
#: Raw file that declares ``APIVERSION`` on the default branch. Used only
#: when the PyPI archive cannot be read -- the constant, not a second guess
#: at what the version *should* be.
_GITHUB_CONST = (
    f"https://raw.githubusercontent.com/{UPSTREAM_REPO}/main/"
    "src/ecoledirecte_api/const.py"
)

ED_CLIENT = Path(
    "custom_components/carnet_scolaire/connectors/ecoledirecte/ed_client.py"
)
ED_CLIENT_NAME = ED_CLIENT.as_posix()

MARKER = "ecoledirecte-upstream"
BRANCH_PREFIX = "chore/ecoledirecte-"
ISSUE_TITLE_PREFIX = f"[{MARKER}]"

_PYPI_URL = "https://pypi.org/pypi/{package}/json"
_TIMEOUT = 20.0
_USER_AGENT = "ha-carnet-scolaire-ed-watch"

#: The constant this repository ships, whatever version it names.
_OURS = re.compile(
    r'^ECOLEDIRECTE_API_VERSION\s*=\s*"(?P<version>[^"]+)"',
    re.MULTILINE,
)
#: Upstream's name for the same number, in a ``.py`` member of an archive
#: or in the GitHub file. Single or double quotes; no interpolation.
_THEIRS = re.compile(
    r'^APIVERSION\s*=\s*[\'"](?P<version>[^\'"]+)[\'"]',
    re.MULTILINE,
)

#: Fetches a URL and returns the decoded JSON. Injected in tests.
JsonFetcher = Callable[[str], Any]
#: Fetches a URL and returns the raw bytes. Injected in tests.
BytesFetcher = Callable[[str], bytes]


class VersionError(RuntimeError):
    """The repository's constant is absent, or upstream could not be parsed.

    A hard failure on purpose: a watch that guesses which version we ship
    would report against a number nobody sends on the wire.
    """


# ---------------------------------------------------------------------------
# Reading the constant out of the repository
# ---------------------------------------------------------------------------


def declared_version(text: str) -> str:
    """The ``ECOLEDIRECTE_API_VERSION`` a source file declares."""
    match = _OURS.search(text)
    if match is None:
        message = (
            f"no `ECOLEDIRECTE_API_VERSION =` line found in {ED_CLIENT_NAME}: "
            "the API version must be a named constant, not inlined in URLs"
        )
        raise VersionError(message)
    return match.group("version")


def pinned_version(root: Path | None = None) -> str:
    """The constant, read from ``ed_client.py`` and never restated here."""
    base = Path() if root is None else root
    return declared_version((base / ED_CLIENT).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Asking upstream
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Release:
    """Upstream's ``APIVERSION``, and where it was read from."""

    apiversion: str
    #: PyPI package version (``0.3.0``), or ``""`` when only GitHub answered.
    package_version: str
    #: ISO-8601 upload time, or ``""`` when nobody said.
    released: str
    #: ``"pypi"`` or ``"github"`` -- so the report says which file was read.
    source: str


def fetch_json(url: str) -> Any:
    """Fetch and decode JSON, authenticating to GitHub when a token is around."""
    headers = {"Accept": "application/json", "User-Agent": _USER_AGENT}
    token = os.environ.get("GITHUB_TOKEN", "")
    if token and url.startswith("https://api.github.com/"):
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)  # noqa: S310
    with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:  # noqa: S310
        payload: Any = json.loads(response.read().decode("utf-8"))
    return payload


def fetch_bytes(url: str) -> bytes:
    """Fetch raw bytes (sdist, wheel, or a GitHub file)."""
    headers = {"User-Agent": _USER_AGENT}
    token = os.environ.get("GITHUB_TOKEN", "")
    if token and url.startswith("https://raw.githubusercontent.com/"):
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)  # noqa: S310
    with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:  # noqa: S310
        return bytes(response.read())


def _apiversion_in_text(text: str) -> str | None:
    """The first ``APIVERSION =`` assignment, or ``None``."""
    match = _THEIRS.search(text)
    if match is None:
        return None
    return match.group("version")


def _python_members_from_zip(payload: bytes) -> list[tuple[str, str]]:
    """``(name, APIVERSION)`` pairs found in a wheel or zip sdist."""
    found: list[tuple[str, str]] = []
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        for name in archive.namelist():
            if not name.endswith(".py"):
                continue
            version = _apiversion_in_text(
                archive.read(name).decode("utf-8", errors="replace")
            )
            if version is not None:
                found.append((name, version))
    return found


def _python_members_from_tar(payload: bytes, filename: str) -> list[tuple[str, str]]:
    """``(name, APIVERSION)`` pairs found in a tarball sdist."""
    mode = "r:gz" if filename.endswith((".tar.gz", ".tgz")) else "r:"
    found: list[tuple[str, str]] = []
    with tarfile.open(fileobj=io.BytesIO(payload), mode=mode) as archive:
        for member in archive.getmembers():
            if not member.isfile() or not member.name.endswith(".py"):
                continue
            handle = archive.extractfile(member)
            if handle is None:
                continue
            version = _apiversion_in_text(
                handle.read().decode("utf-8", errors="replace")
            )
            if version is not None:
                found.append((member.name, version))
    return found


def _unique_apiversion(members: list[tuple[str, str]]) -> str | None:
    """One ``APIVERSION``, preferring ``const.py``, or ``None`` if they disagree."""
    if not members:
        return None
    preferred = [
        item for item in members if item[0].replace("\\", "/").endswith("const.py")
    ]
    versions = {version for _name, version in (preferred or members)}
    if len(versions) != 1:
        return None
    return next(iter(versions))


def apiversion_from_archive(payload: bytes, filename: str) -> str | None:
    """Read ``APIVERSION`` out of a sdist or wheel, without installing it.

    Prefers a member named ``const.py`` when several files declare the
    name, and refuses to guess if they disagree. An archive we cannot
    open, or that carries no assignment, is ``None`` -- the caller tries
    the next source rather than inventing a number.
    """
    try:
        if filename.endswith((".whl", ".zip")):
            members = _python_members_from_zip(payload)
        elif filename.endswith((".tar.gz", ".tgz", ".tar")):
            members = _python_members_from_tar(payload, filename)
        else:
            return None
    except OSError, tarfile.TarError, zipfile.BadZipFile, UnicodeError:
        return None
    return _unique_apiversion(members)


def _pypi_artifact(payload: dict[str, Any]) -> tuple[str, str, str] | None:
    """Filename, download URL and upload time of the preferred artifact.

    Sdist first -- that is where ``const.py`` lives as source -- then
    wheel. The version is ``info.version``, not a second number we chose.
    """
    version = str(payload.get("info", {}).get("version") or "")
    if not version:
        return None
    urls = list(payload.get("urls") or [])
    ordered = [entry for entry in urls if entry.get("packagetype") == "sdist"]
    ordered += [entry for entry in urls if entry.get("packagetype") == "bdist_wheel"]
    for entry in ordered:
        filename = str(entry.get("filename") or "")
        url = str(entry.get("url") or "")
        if filename and url:
            released = str(entry.get("upload_time_iso_8601") or "")
            return filename, url, released
    return None


def latest_release(
    fetch: JsonFetcher = fetch_json,
    download: BytesFetcher = fetch_bytes,
) -> Release | None:
    """Upstream ``APIVERSION``, or ``None`` when nobody can be read.

    ``None`` rather than an exception: this runs on a schedule. PyPI is
    tried first (sdist/wheel of ``ecoledirecte``); GitHub
    ``hacf-fr/ecoledirecte_api`` is the fallback named by the spec. Both
    failing is an outage, not a red run.
    """
    package_version = ""
    released = ""
    try:
        payload = fetch(_PYPI_URL.format(package=PACKAGE))
        artifact = _pypi_artifact(payload)
        if artifact is not None:
            filename, url, released = artifact
            package_version = str(payload["info"]["version"])
            found = apiversion_from_archive(download(url), filename)
            if found:
                return Release(
                    apiversion=found,
                    package_version=package_version,
                    released=released,
                    source="pypi",
                )
    except (OSError, ValueError, KeyError, TypeError, urllib.error.URLError) as error:
        print(f"PyPI unreadable ({error!r}); trying the upstream repository.")

    try:
        text = download(_GITHUB_CONST).decode("utf-8")
        found = _apiversion_in_text(text)
        if found:
            return Release(
                apiversion=found,
                package_version=package_version,
                released=released,
                source="github",
            )
    except (OSError, ValueError, UnicodeError, urllib.error.URLError) as error:
        print(f"GitHub unreadable ({error!r}); nothing reported this run.")
        return None

    print("APIVERSION not found in PyPI archive or upstream const.py.")
    return None


def classify(ours: str, theirs: str) -> str:
    """``"outdated"`` or ``"current"``.

    Equality only. A 517 is "the string Aplim no longer accepts", not a
    dotted-number order, and proposing a downgrade when upstream moved the
    constant *down* is still the report this job exists to deliver.
    """
    if ours == theirs:
        return "current"
    return "outdated"


# ---------------------------------------------------------------------------
# Moving the constant
# ---------------------------------------------------------------------------


def bump(root: Path, new_version: str) -> list[str]:
    """Rewrite ``ECOLEDIRECTE_API_VERSION`` to ``new_version``.

    Text substitution, so line endings and the rest of the file stay
    exactly as they were found. The version being replaced is read from
    the file itself rather than passed in.
    """
    old = pinned_version(root)
    path = root / ED_CLIENT
    text = path.read_text(encoding="utf-8", newline="")
    replaced = text.replace(
        f'ECOLEDIRECTE_API_VERSION = "{old}"',
        f'ECOLEDIRECTE_API_VERSION = "{new_version}"',
        1,
    )
    if replaced == text:
        return []
    path.write_text(replaced, encoding="utf-8", newline="")
    return [ED_CLIENT_NAME]


def branch_name(version: str) -> str:
    """The one branch a given ``APIVERSION`` is ever proposed on."""
    return f"{BRANCH_PREFIX}{version}"


def pull_request_title(release: Release) -> str:
    """A conventional-commit subject, because it becomes the commit subject."""
    return f"chore(deps): raise ECOLEDIRECTE_API_VERSION to {release.apiversion}"


def commit_message(release: Release, pinned: str) -> str:
    """The commit the branch carries: what moved, and why a 517 would follow."""
    date = release.released.split("T")[0] if release.released else "unknown date"
    origin = (
        f"{PACKAGE} {release.package_version} on PyPI ({date})"
        if release.source == "pypi" and release.package_version
        else f"{UPSTREAM_REPO} ({release.source})"
    )
    return (
        f"{pull_request_title(release)}\n"
        "\n"
        f"Upstream APIVERSION is {release.apiversion} in {origin}; this "
        f"repository still sends v={pinned}. A 517 from "
        "api.ecoledirecte.com means this constant is late, not that the "
        "request should be retried.\n"
        "\n"
        "Opened by .github/workflows/ecoledirecte-watch.yml. The "
        "ecoledirecte PyPI package is not a dependency -- only APIVERSION "
        "is being copied -- so this is a proposal and not a merge.\n"
    )


# ---------------------------------------------------------------------------
# The report a maintainer reads
# ---------------------------------------------------------------------------


def _evidence(release: Release, pinned: str) -> list[str]:
    """The facts, in the order they get read."""
    date = release.released.split("T")[0] if release.released else "date inconnue"
    origin = (
        f"`{PACKAGE}=={release.package_version}` sur PyPI"
        if release.source == "pypi" and release.package_version
        else f"`{UPSTREAM_REPO}` (fichier brut)"
    )
    return [
        "| | |",
        "| --- | --- |",
        f"| Constante | `{pinned}` — `{ED_CLIENT_NAME}` |",
        f"| `APIVERSION` amont | `{release.apiversion}`, lu le {date} |",
        f"| Source | {origin} |",
        "",
        (
            "Cette intégration **n'installe pas** le paquet `ecoledirecte`. "
            "Le handshake est le nôtre (spécification connecteurs §7.1) ; on "
            "ne copie que le numéro `v=` que l'amont a bougé."
        ),
    ]


def pull_request_body(release: Release, pinned: str) -> str:
    """The proposal: the evidence, then what stops it being merged on its own."""
    lines = [
        f"<!-- {MARKER} -->",
        "",
        (
            f"Cette *pull request* porte `ECOLEDIRECTE_API_VERSION` de "
            f"**{pinned}** à **{release.apiversion}** dans `{ED_CLIENT_NAME}`. "
            "Elle existe parce qu'un 517 Aplim n'est pas un incident transitoire "
            ": c'est cette constante en retard."
        ),
        "",
        *_evidence(release, pinned),
        "",
        "## À faire avant de fusionner",
        "",
        (
            "- [ ] relire le handshake dans "
            "`custom_components/carnet_scolaire/connectors/ecoledirecte/ed_client.py` "
            ": une nouvelle `APIVERSION` peut accompagner un changement de "
            "GTK, de corps de login ou de codes (250 / 520 / 525 / 517)"
        ),
        (
            "- [ ] `Validate` au vert — le job le demande explicitement, parce "
            "qu'une *pull request* ouverte par `GITHUB_TOKEN` n'a pas de "
            "contrôles d'elle-même"
        ),
        (
            "- [ ] ne pas ajouter le paquet PyPI `ecoledirecte` aux "
            "dépendances (son `backoff` relog hors limiteur ; son DEBUG fuit "
            "le jeton)"
        ),
        "",
        (
            "**Rien ici ne se fusionne tout seul.** Aucune fusion automatique "
            "n'est activée."
        ),
        "",
        (
            "> Les vérifications ne démarrent pas d'elles-mêmes sur une *pull "
            "request* ouverte par `GITHUB_TOKEN`. Le job demande `Validate` "
            "explicitement (`workflow_dispatch`). Si aucun résultat "
            "n'apparaît, fermer puis réouvrir cette *pull request* relance "
            "les portails."
        ),
        "",
        "---",
        "",
        (
            "Ouverte automatiquement par "
            "`.github/workflows/ecoledirecte-watch.yml` (spécification "
            "connecteurs §7.1)."
        ),
    ]
    return "\n".join(lines) + "\n"


def fallback_issue_title(release: Release, pinned: str) -> str:
    """Stable prefix, then the two versions -- what a notification shows."""
    return (
        f"{ISSUE_TITLE_PREFIX} APIVERSION {release.apiversion} (constante : {pinned})"
    )


def fallback_issue_body(release: Release, pinned: str) -> str:
    """The same evidence, for the day the pull request cannot be opened."""
    lines = [
        f"<!-- {MARKER} -->",
        "",
        (
            f"L'amont publie `APIVERSION` **{release.apiversion}** ; ce dépôt "
            f"envoie encore **{pinned}**."
        ),
        "",
        (
            "La *pull request* qui aurait porté la constante n'a pas pu être "
            "créée — Actions n'a peut-être pas le droit d'ouvrir une *pull "
            "request* dans ce dépôt, ou la poussée de branche a été refusée. "
            "Le rapport arrive donc sous cette forme, et la mise à jour est à "
            "faire à la main."
        ),
        "",
        *_evidence(release, pinned),
        "",
        "---",
        "",
        "Ouverte automatiquement par `.github/workflows/ecoledirecte-watch.yml`.",
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
    """Compare the constant against upstream. Exit 1 only when they differ."""
    release = latest_release()
    if release is None:
        _emit_outputs({"outcome": "unavailable", "pinned": pinned})
        return 0

    outcome = classify(pinned, release.apiversion)
    date = release.released or "?"
    print(
        f"ours {pinned}, upstream APIVERSION {release.apiversion} "
        f"({release.source}, {date}): {outcome}"
    )

    outputs = {
        "outcome": outcome,
        "pinned": pinned,
        "latest": release.apiversion,
        "released": release.released,
        "branch": branch_name(release.apiversion),
    }
    if outcome != "outdated":
        _emit_outputs(outputs)
        return 0

    documents = {
        "pr_body": pull_request_body(release, pinned),
        "issue_body": fallback_issue_body(release, pinned),
        "commit_message": commit_message(release, pinned),
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
            outputs[f"{name}_path"] = str(path)
        print(f"reports written to {out_dir}")

    _emit_outputs(outputs)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check-upstream",
        action="store_true",
        help="query PyPI/GitHub and report whether APIVERSION moved",
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
        help="rewrite ECOLEDIRECTE_API_VERSION to this value",
    )
    args = parser.parse_args(argv)

    try:
        pinned = pinned_version()
    except (VersionError, OSError) as error:
        print(f"version check failed: {error}", file=sys.stderr)
        return 1

    if args.bump:
        changed = bump(Path(), args.bump)
        print(
            f"ECOLEDIRECTE_API_VERSION {pinned} -> {args.bump} "
            f"in {changed or 'no file'}"
        )
        return 0

    if not args.check_upstream:
        print(f'ECOLEDIRECTE_API_VERSION = "{pinned}" ({ED_CLIENT_NAME})')
        return 0

    return _check_upstream(pinned, args.reports)


if __name__ == "__main__":
    sys.exit(main())
