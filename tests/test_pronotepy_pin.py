"""The ``pronotepy`` pin: where it is declared, and when upstream moves past it.

This file protects a mechanism that exists because of one morning. Every login
against a school failed with a ``CryptoError`` that ``pronotepy`` reports as
"probably the qr code has expired", so the QR code is where the search went.
The cause was that the establishment's PRONOTE server had moved to 26.2.5 and
the pin was one release behind the upstream fix -- a release published six days
earlier, whose commit subject was literally *"fix compatibility with PRONOTE
2026.2.5.6"*. Nothing in the repository was watching PyPI, so nothing said so.

``scripts/pronotepy_pin.py`` is what says so now, by proposing the bump as a
pull request. The defects these tests stop from returning are all silent ones:

* a pin restated instead of read -- the version is already in two files, and a
  watcher carrying its own copy proposes a bump from a version nobody ships;
* the two declarations drifting apart. ``manifest.json`` is what Home Assistant
  installs on a user's instance and ``requirements_test.txt`` is what the suite
  runs against, so a divergence means the corrections in ``hardened_client.py``
  are verified against a library nobody has -- and a bump that moved only one
  of them would be worse than no bump at all;
* a comparison that is wrong in either direction: proposing a release older
  than the pin is noise, and missing a newer one is the outage above;
* a report that arrives without the upstream commit subjects, which is the only
  part of it that ended the investigation;
* a proposal presented as ready to merge. ``CONTRIBUTING.md`` §5 requires every
  divergence in ``hardened_client.py`` to be re-read against the new version,
  and no gate can discharge that.

Offline by construction. Every network call in the module under test is a
callable parameter, and every test here passes a dictionary in its place: the
suite blocks sockets, and a test of a version watcher that needed PyPI to be up
would fail for reasons that have nothing to do with this repository.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import re
import sys
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "pronotepy-watch.yml"


def _module() -> Any:
    """Import ``scripts/pronotepy_pin.py`` as a module.

    By path, because ``scripts/`` is not a package -- the same reasoning as
    ``tests/test_translations.py``: it is a directory of tools the CI runs, and
    making it importable only to satisfy a test would be the test dictating the
    repository layout.
    """
    path = ROOT / "scripts" / "pronotepy_pin.py"
    spec = importlib.util.spec_from_file_location("pronotepy_pin", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


pin = _module()


def _repository(tmp_path: Path, requirement: str, manifest_pin: str) -> Path:
    """A throwaway tree carrying the two declarations, and nothing else."""
    (tmp_path / "requirements_test.txt").write_text(
        f"# prose naming {requirement.replace('==', ' ')}, not a declaration\n"
        f"pytest-homeassistant-custom-component==0.13.363\n"
        f"{requirement}\n"
        f"ruff==0.14.4\n",
        encoding="utf-8",
    )
    component = tmp_path / "custom_components" / "carnet_scolaire"
    component.mkdir(parents=True)
    (component / "manifest.json").write_text(
        json.dumps(
            {
                "domain": "carnet_scolaire",
                "name": "Carnet scolaire",
                "requirements": [manifest_pin],
                "version": "0.0.0",
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return tmp_path


def _pypi(version: str, released: str = "2026-09-03T00:32:14.054846Z") -> Any:
    """A fetcher standing in for ``https://pypi.org/pypi/pronotepy/json``."""

    def fetch(url: str) -> Any:
        assert url == "https://pypi.org/pypi/pronotepy/json", url
        return {
            "info": {"version": version},
            "urls": [
                {"filename": "pronotepy.tar.gz", "upload_time_iso_8601": released}
            ],
        }

    return fetch


def _compare(*subjects: str) -> Any:
    """A fetcher standing in for the GitHub compare endpoint."""

    def fetch(url: str) -> Any:
        assert "compare" in url, url
        return {
            "commits": [
                {"commit": {"message": f"{subject}\n\ncloses #346"}}
                for subject in subjects
            ]
        }

    return fetch


@pytest.fixture
def release() -> Any:
    """The release the outage was fixed by, as PyPI described it."""
    return pin.Release(version="2.15.7", released="2026-09-03T00:32:14Z")


@pytest.fixture
def outputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A stand-in for ``$GITHUB_OUTPUT``, which the workflow reads back."""
    destination = tmp_path / "github_output"
    destination.write_text("", encoding="utf-8")
    monkeypatch.setenv("GITHUB_OUTPUT", str(destination))
    return destination


def _emitted(destination: Path) -> dict[str, str]:
    """The ``key=value`` pairs a run appended to ``$GITHUB_OUTPUT``."""
    pairs: dict[str, str] = {}
    for line in destination.read_text(encoding="utf-8").splitlines():
        key, _, value = line.partition("=")
        pairs[key] = value
    return pairs


# ---------------------------------------------------------------------------
# Reading the pin out of the repository
# ---------------------------------------------------------------------------


def test_the_pin_is_read_from_the_repository_and_never_restated() -> None:
    """A third copy of the version is a third thing to forget.

    The number lives in ``requirements_test.txt`` and in ``manifest.json``, and
    the watcher must take it from there. This test reads the real files, so it
    also fails the day either declaration changes shape -- a pin turned into a
    range, a requirements entry given a marker -- which is exactly when the
    watcher would otherwise start comparing against nothing. No version is
    written down here on purpose: an assertion naming 2.15.7 would have to be
    edited by the very commit it is supposed to be checking.
    """
    version = pin.pinned_version(ROOT)

    assert version
    from_requirements = pin.requirement_pin(
        (ROOT / "requirements_test.txt").read_text(encoding="utf-8")
    )
    manifest = json.loads(
        (ROOT / "custom_components" / "carnet_scolaire" / "manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert from_requirements == version
    assert pin.manifest_pin(manifest) == version


def test_two_declarations_of_the_pin_that_disagree_are_a_failure(
    tmp_path: Path,
) -> None:
    """Silent drift between the tested version and the installed one.

    ``manifest.json`` is what Home Assistant installs; ``requirements_test.txt``
    is what CI tests. Nothing made them agree before, and the failure is
    invisible: every gate stays green while the suite exercises a library no
    user has -- which matters here more than elsewhere, because
    ``hardened_client.py`` corrects upstream behaviour specific to one version.
    """
    root = _repository(tmp_path, "pronotepy==2.15.7", "pronotepy==2.15.6")

    with pytest.raises(pin.PinError, match="disagree"):
        pin.pinned_version(root)


def test_a_pronotepy_requirement_that_is_not_pinned_is_a_failure(
    tmp_path: Path,
) -> None:
    """A floating requirement makes the whole watcher meaningless.

    There is nothing to compare against a range, and reporting "up to date"
    because no pin could be read would be worse than reporting nothing.
    """
    root = _repository(tmp_path, "pronotepy>=2.15", "pronotepy>=2.15")

    with pytest.raises(pin.PinError):
        pin.pinned_version(root)


def test_the_manifest_pin_is_found_among_other_requirements() -> None:
    """The list is not guaranteed to hold one entry, or to hold ours first."""
    manifest = {"requirements": ["some-other-lib==1.0", "pronotepy==2.15.7"]}

    assert pin.manifest_pin(manifest) == "2.15.7"


# ---------------------------------------------------------------------------
# Comparing versions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("pinned", "upstream", "expected"),
    [
        # The outage itself: the pin behind the release that fixed it.
        ("2.15.6", "2.15.7", "outdated"),
        ("2.15.6", "2.16.0", "outdated"),
        ("2.15.6", "3.0.0", "outdated"),
        ("2.9.0", "2.15.7", "outdated"),
        ("2.15.7", "2.15.7", "current"),
        # Trailing zeroes are not a difference.
        ("2.16", "2.16.0", "current"),
        # The pin is raised here first, in the middle of the incident that
        # motivated it, so upstream being behind is an ordinary state and must
        # not be reported as a release.
        ("2.15.7", "2.15.6", "ahead"),
        ("2.15.7", "2.15.7rc1", "ahead"),
    ],
)
def test_the_pinned_and_upstream_versions_are_ordered_correctly(
    pinned: str, upstream: str, expected: str
) -> None:
    """Both directions of the mistake cost something.

    Missing a newer release is the outage in the module docstring. Proposing
    one that is older -- ``2.9.0`` looking newer than ``2.15.7`` under a string
    comparison, which is what a naive check does -- opens a pull request that
    *downgrades* the library, and a weekly proposal for nothing is how a
    maintainer learns to ignore this job.
    """
    assert pin.classify(pinned, upstream) == expected


def test_a_version_that_cannot_be_read_is_a_failure_not_a_guess() -> None:
    """Guessing an order would propose a downgrade as an upgrade."""
    with pytest.raises(pin.PinError, match="cannot read"):
        pin.classify("2.15.7", "not-a-version")


# ---------------------------------------------------------------------------
# Moving the pin
# ---------------------------------------------------------------------------


def test_a_bump_moves_both_declarations_together(tmp_path: Path) -> None:
    """A pull request that moved one of the two would be worse than none.

    ``manifest.json`` is what a user installs and ``requirements_test.txt`` is
    what CI tests, so a bump touching only one produces a green pipeline
    proving nothing about the library that ends up on the instance. Both files,
    one commit, or neither.
    """
    root = _repository(tmp_path, "pronotepy==2.15.6", "pronotepy==2.15.6")

    changed = pin.bump(root, "2.15.7")

    assert changed == [
        "requirements_test.txt",
        "custom_components/carnet_scolaire/manifest.json",
    ]
    assert pin.pinned_version(root) == "2.15.7"


def test_a_bump_leaves_prose_and_json_formatting_alone(tmp_path: Path) -> None:
    """The diff has to be readable, and the manifest has to stay valid.

    ``manifest.json`` has a key order hassfest enforces and a two-space
    indentation; a JSON round-trip would rewrite lines this change has no
    business touching, and a comment mentioning the old version in prose is
    documentation, not a declaration. Only the two pinned requirements move.
    """
    root = _repository(tmp_path, "pronotepy==2.15.6", "pronotepy==2.15.6")
    before = (
        root / "custom_components" / "carnet_scolaire" / "manifest.json"
    ).read_text(encoding="utf-8")

    pin.bump(root, "2.15.7")

    requirements = (root / "requirements_test.txt").read_text(encoding="utf-8")
    after = (
        root / "custom_components" / "carnet_scolaire" / "manifest.json"
    ).read_text(encoding="utf-8")
    assert "# prose naming pronotepy 2.15.6, not a declaration" in requirements
    assert "pytest-homeassistant-custom-component==0.13.363" in requirements
    assert after == before.replace("2.15.6", "2.15.7")
    assert json.loads(after)["requirements"] == ["pronotepy==2.15.7"]


def test_bumping_to_the_version_already_pinned_changes_nothing(
    tmp_path: Path,
) -> None:
    """A re-run must not produce an empty commit, or any commit.

    The job is scheduled, so it will meet the same version again. Rewriting the
    files with identical content would leave ``git commit`` to fail on an empty
    tree and take the whole run red with it.
    """
    root = _repository(tmp_path, "pronotepy==2.15.7", "pronotepy==2.15.7")

    assert pin.bump(root, "2.15.7") == []


def test_the_branch_a_version_is_proposed_on_is_derived_from_it() -> None:
    """One branch per version is what makes the job idempotent.

    A fixed branch name would have a newer release silently overwrite the
    proposal a maintainer is in the middle of reading; a random one would open
    a fresh pull request every Tuesday.
    """
    assert pin.branch_name("2.15.7") == "chore/pronotepy-2.15.7"
    assert pin.branch_name("2.16.0") != pin.branch_name("2.15.7")


# ---------------------------------------------------------------------------
# Asking upstream
# ---------------------------------------------------------------------------


def test_the_release_pypi_publishes_is_read_with_its_date() -> None:
    """The date is half of the decision.

    "A newer version exists" says nothing about urgency; "published six days
    ago" is what told the maintainer the fix had been sitting there while the
    integration was down.
    """
    found = pin.latest_release(fetch=_pypi("2.15.7"))

    assert found is not None
    assert found.version == "2.15.7"
    assert found.released.startswith("2026-09-03")


@pytest.mark.parametrize(
    "failure",
    [
        OSError("connection reset"),
        ValueError("not json"),
        TimeoutError("timed out"),
    ],
)
def test_an_unreachable_pypi_reports_nothing_and_does_not_raise(
    failure: Exception,
) -> None:
    """A scheduled job that goes red on someone else's outage gets ignored.

    The point of a weekly check is that a red run means something. If a PyPI
    hiccup fails it, the failure becomes background noise and the one run that
    matters looks like all the others.
    """

    def fetch(_url: str) -> Any:
        raise failure

    assert pin.latest_release(fetch=fetch) is None


def test_a_payload_pypi_shapes_differently_reports_nothing() -> None:
    """The JSON API is not a contract this repository can enforce."""
    assert pin.latest_release(fetch=lambda _url: {"info": {}}) is None


def test_the_commit_subjects_between_the_two_tags_are_collected() -> None:
    """The subject is what ended the investigation, so it must arrive by itself.

    A maintainer reading "2.15.7 is available" still has to go and look. One
    reading "fix compatibility with PRONOTE 2026.2.5.6" already knows, and that
    difference was worth a morning. Only the first line of each message is
    kept: the bodies carry issue references and no decision.
    """
    subjects = pin.upstream_subjects(
        "2.15.6",
        "2.15.7",
        fetch=_compare("fix compatibility with PRONOTE 2026.2.5.6", "bump version"),
    )

    assert subjects == ["fix compatibility with PRONOTE 2026.2.5.6", "bump version"]


def test_tags_that_do_not_resolve_fall_back_instead_of_failing() -> None:
    """Upstream's ``v``-prefixed tags are a convention, not a promise.

    If the naming changes, the proposal must still arrive carrying what PyPI
    said. Losing the version *and* the date because the tag comparison could
    not be built would reintroduce exactly the silence this mechanism exists to
    break.
    """

    def fetch(_url: str) -> Any:
        raise OSError("404 Not Found")

    assert pin.upstream_subjects("2.15.6", "2.15.7", fetch=fetch) is None


# ---------------------------------------------------------------------------
# The proposal a maintainer reads
# ---------------------------------------------------------------------------


def test_the_pull_request_body_carries_what_a_maintainer_needs_to_decide(
    release: Any,
) -> None:
    """Four facts, and the fourth is the one that was missing.

    Pinned version, new version, its date, and the upstream commit subjects. A
    proposal that only announced a version number would be read, closed and
    forgotten -- and the outage would have lasted the same morning.
    """
    body = pin.pull_request_body(
        release, "2.15.6", ["fix compatibility with PRONOTE 2026.2.5.6"]
    )

    assert "2.15.6" in body
    assert "2.15.7" in body
    assert "2026-09-03" in body
    assert "fix compatibility with PRONOTE 2026.2.5.6" in body
    assert "compare/v2.15.6...v2.15.7" in body
    # Both declarations are named, because moving one is the failure mode.
    assert "requirements_test.txt" in body
    assert "custom_components/carnet_scolaire/manifest.json" in body


def test_the_pull_request_body_says_that_nothing_merges_by_itself(
    release: Any,
) -> None:
    """§5 is a rule a machine cannot discharge, and the proposal must say so.

    ``hardened_client.py`` exists to correct specific upstream behaviours at a
    specific version: a bump can make one of those corrections unnecessary, or
    wrong. CI sees that a workaround still passes, not that it has become
    superfluous. So the body carries the re-read as an explicit item and states
    that no automatic merge is enabled -- otherwise the green tick reads as
    permission.
    """
    body = pin.pull_request_body(release, "2.15.6", ["bump version"])

    assert "hardened_client.py" in body
    assert "CONTRIBUTING.md` §5" in body
    assert "- [ ]" in body
    assert "fusionne" in body


def test_the_workflow_enables_no_automatic_merge() -> None:
    """The one line that would undo the rule above, and its absence.

    ``gh pr merge --auto`` is a single flag away from where the pull request is
    created, and adding it would hand the §5 re-read to a green tick. Nothing
    else in this repository would notice.
    """
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "--auto" not in workflow
    assert "enable-auto-merge" not in workflow
    assert "pr merge" not in workflow


def test_the_report_says_so_when_the_tags_could_not_be_compared(
    release: Any,
) -> None:
    """An empty comparison and an impossible one must not read the same.

    Reporting an empty list of subjects when the tags simply did not resolve
    would say the release changed nothing, which is a false statement about
    upstream and precisely the kind that sends someone the wrong way.
    """
    undated = pin.Release(version="2.15.7", released="")

    unavailable = pin.pull_request_body(undated, "2.15.6", None)
    nothing_between = pin.pull_request_body(release, "2.15.6", [])

    assert "ne se résolvent pas" in unavailable
    assert "Aucun commit" in nothing_between
    assert "date inconnue" in unavailable


def test_the_commit_message_says_which_versions_moved_and_why(
    release: Any,
) -> None:
    """The commit outlives the pull request, and is what ``git log`` shows.

    A ``chore(deps)`` line with no reason is indistinguishable from a routine
    bump; this one has to say that the release fixed something, because that is
    what a bisecting reader needs six months later.
    """
    message = pin.commit_message(
        release, "2.15.6", ["fix compatibility with PRONOTE 2026.2.5.6"]
    )

    assert message.startswith("chore(deps): raise the pronotepy pin to 2.15.7")
    assert "2.15.6" in message
    assert "fix compatibility with PRONOTE 2026.2.5.6" in message
    assert "hardened_client.py" in message


def test_the_fallback_issue_says_why_it_is_an_issue_and_not_a_pull_request(
    release: Any,
) -> None:
    """A repository can forbid Actions from opening pull requests.

    On that day the report must still arrive, because silence is the failure
    being corrected -- and it has to say that the bump is now a manual job,
    otherwise a reader waits for a proposal that will never come.
    """
    body = pin.fallback_issue_body(release, "2.15.6", ["bump version"])

    assert "n'a pas pu être créée" in body
    assert "à la main" in body
    assert "bump version" in body


def test_the_fallback_issue_title_starts_with_the_marker_the_workflow_searches_for(
    release: Any,
) -> None:
    """The marker is what stops a duplicate issue every week.

    The workflow finds its own issue by this prefix rather than by a label,
    because a label can be missing or renamed and the job would then file a
    fresh issue on every run. Two literals in two languages that cannot share
    one constant, so their agreement is asserted here.
    """
    title = pin.fallback_issue_title(release, "2.15.6")
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert title.startswith(pin.ISSUE_TITLE_PREFIX)
    assert pin.ISSUE_TITLE_PREFIX == "[pronotepy-upstream]"
    assert "2.15.7" in title
    assert "2.15.6" in title
    assert f'startswith("{pin.ISSUE_TITLE_PREFIX}")' in workflow


def test_the_workflow_names_the_branch_the_helper_produces() -> None:
    """The retirement step rebuilds the branch name in shell, and must match.

    It closes the proposal whose branch is the version now pinned. Built from
    a different prefix, it would close nothing and stale proposals would pile
    up -- while the helper went on opening one per release.
    """
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert f'branch="{pin.BRANCH_PREFIX}$PINNED"' in workflow


# ---------------------------------------------------------------------------
# What the workflow reads back
# ---------------------------------------------------------------------------


def test_every_output_the_workflow_reads_is_one_the_helper_emits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outputs: Path
) -> None:
    """A renamed output breaks the job silently, in the branch nobody tests.

    The workflow refers to ``steps.compare.outputs.<name>`` in six places, and
    an expression naming an output that does not exist expands to the empty
    string rather than failing -- so a rename would produce a run that pushes
    a branch with an empty title, or opens nothing at all, and only on the week
    a release happens to be out.
    """
    monkeypatch.setattr(
        pin, "latest_release", lambda: pin.Release("2.15.7", "2026-09-03T00:00:00Z")
    )
    monkeypatch.setattr(pin, "upstream_subjects", lambda *_a, **_k: ["bump version"])

    assert pin._check_upstream("2.15.6", tmp_path / "reports") == 0

    emitted = set(_emitted(outputs))
    referenced = set(
        re.findall(r"steps\.compare\.outputs\.([a-z_]+)", WORKFLOW.read_text("utf-8"))
    )
    assert referenced
    assert referenced <= emitted, sorted(referenced - emitted)


def test_the_three_documents_are_written_where_the_workflow_looks_for_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outputs: Path
) -> None:
    """The bodies travel as files, and the paths as outputs.

    A body passed through an output would be a shell expansion of text taken
    from PyPI, and a body written to a path the workflow guessed would be a
    second copy of that path. Both are avoided by having the helper say where
    it wrote what.
    """
    monkeypatch.setattr(
        pin, "latest_release", lambda: pin.Release("2.15.7", "2026-09-03T00:00:00Z")
    )
    monkeypatch.setattr(pin, "upstream_subjects", lambda *_a, **_k: [])

    pin._check_upstream("2.15.6", tmp_path / "reports")

    emitted = _emitted(outputs)
    for key in ("pr_body_path", "issue_body_path", "commit_message_path"):
        assert Path(emitted[key]).read_text(encoding="utf-8").strip()


def test_a_pin_already_level_with_upstream_proposes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outputs: Path
) -> None:
    """The ordinary week, and the one the job spends most of its life in.

    Nothing is written and nothing is opened, but the verdict is still
    reported: a green run that says nothing is indistinguishable from a job
    that has quietly stopped working. This is also the state right after the
    pin is raised by hand during an incident.
    """
    monkeypatch.setattr(
        pin, "latest_release", lambda: pin.Release("2.15.7", "2026-09-03T00:00:00Z")
    )
    reports = tmp_path / "reports"

    assert pin._check_upstream("2.15.7", reports) == 0

    emitted = _emitted(outputs)
    assert emitted["outcome"] == "current"
    assert emitted["pinned"] == "2.15.7"
    assert "pr_body_path" not in emitted
    assert not reports.exists()


def test_an_upstream_version_that_cannot_be_ordered_is_reported_not_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outputs: Path
) -> None:
    """Upstream's version scheme is upstream's to change.

    The pin is readable and the repository is fine; something on PyPI is
    shaped in a way this cannot order. Letting that raise would turn the job
    red every Tuesday until somebody investigates a third party's release
    metadata -- and a red weekly run is one nobody reads by the third week.
    """
    monkeypatch.setattr(pin, "latest_release", lambda: pin.Release("not-a-version", ""))

    assert pin._check_upstream("2.15.7", tmp_path / "reports") == 0
    assert _emitted(outputs)["outcome"] == "unavailable"


def test_a_pypi_outage_reports_unavailable_and_still_exits_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outputs: Path
) -> None:
    """The graceful-degradation rule, at the level the workflow branches on.

    Every step that could open something is guarded by
    ``outcome == 'outdated'``, so ``unavailable`` has to be a real value and
    the exit code has to be zero -- otherwise a PyPI hiccup turns into a red
    weekly run, and a red weekly run is one nobody reads.
    """
    monkeypatch.setattr(pin, "latest_release", lambda: None)

    assert pin._check_upstream("2.15.7", tmp_path / "reports") == 0
    assert _emitted(outputs)["outcome"] == "unavailable"
