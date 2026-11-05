"""Fail when the documentation cites a source location that cannot be trusted.

The documentation cites source locations by the hundred -- ``scheduler.py:236``,
``gateway.py:1193-1264`` -- and until now nothing checked a single one of them.
They are load-bearing: the project's own convention is that a claim about
behaviour points at the code that implements it, so a reader who distrusts a
sentence can go and read it. A citation that lands on unrelated code is worse
than an absent one, because the reader then tries to reconcile the wrong code
with a claim that is still true, and concludes against the claim.

Two checks, because two different things go wrong.

**The location exists.** The file resolves and the range lies inside it, which
catches a file renamed, a file deleted and a range past the end -- the damage a
refactor does, in bulk, at exactly the moment nobody is reading the
documentation. Always blocking: there is nothing to weigh.

**The range has not drifted.** Bounds alone miss the common and most
misleading case: a range that slid a few lines and still lands on real code.
Measured on ``ARCHITECTURE.md``, two citations out of three sampled were wrong
this way -- ``const.py:338-343`` given for ``EVENT_LESSON_RESTORED``, which is
at line 385 and whose cited lines carry the end of another enum; ``todo.py:71``
given for ``UPDATE_TODO_ITEM``, which is at 79 while line 71 reads
``student: Student,``. Both are well inside the file, so the first check sees
nothing, and a reader has no reason to doubt the citation rather than the
sentence.

Drift is read from history rather than content. For each citation, take a
**baseline** -- a commit at which the citation was plausibly true -- and ask
whether the *cited* file has changed at or above the end of the range since.
If it has, the range may have moved.

**The baseline is per citation, not per document**, and that distinction is the
whole accuracy of the check. Using the document's last commit means any edit
anywhere in the file silences every report against it: 145 of this project's
146 citations in one document predate its last commit, so a one-line typo fix
rebased all of them and the check went quiet having verified nothing. So the
baseline is the commit that last wrote *the line carrying the citation*, from
``git blame``. Touching the document elsewhere no longer resets anything, and
rewriting the citation's own line means somebody did re-open it.

**Blocking and reporting are separated, deliberately.** On the blame baseline
this repository has a backlog of some seventy suspect citations. Failing a
change on seventy lines it did not touch is not a gate, it is a gate that gets
switched off -- so what blocks is narrower than what is reported:

* **Blocking** -- the range may have moved *because of the commits under
  test*. That is the drift check run with ``--since=<ref>``: the author of
  those commits is the one who can act, and they are being told they just
  invalidated a citation.
* **Reporting** -- everything else the blame baseline finds. Printed as notes,
  exit code untouched, a backlog to work off rather than a wall.

**Limits, written here rather than left to a reviewer, because they decide what
this is worth.** "Not drifted" means "nothing moved above it since the
baseline", *not* "verified": a range that slid before its baseline and was
never re-opened is invisible to both checks. Re-wording the paragraph a
citation sits in resets that citation's baseline without anyone re-opening the
range -- far narrower than "touching the document", but not nothing. Neither
check judges whether the cited code is *relevant* to the claim. And nobody
should read either as licence to cite without opening the file.

Neither failure is reachable by review: a citation is correct when it is
committed, and a later commit to a *different* file is what invalidates it.

Usage::

    python scripts/check_doc_citations.py
    python scripts/check_doc_citations.py --since=origin/main
    python scripts/check_doc_citations.py --no-drift
"""

import ast
from functools import cache
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]

#: Where a cited ``<name>.py`` may live. Ordered, so the integration wins a
#: name it shares with a test -- ``__init__.py`` is the only such collision
#: today, and all its citations mean the integration's module.
SEARCH_ROOTS = (
    ROOT / "custom_components" / "carnet_scolaire",
    ROOT / "scripts",
    ROOT / "tests",
    ROOT / "tests" / "fixtures",
    ROOT,
)

#: ``module.py:12`` or ``module.py:12-34``. Deliberately not matching a bare
#: ``module.py``: naming a file is prose, and this checks *locations*.
CITATION = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*\.py):(\d+)(?:-(\d+))?\b")

#: Cited names that belong to ``pronotepy``, not to this repository. The
#: contradictory review cites them on purpose, and "absent from the repo" is
#: not a citation error there -- so this must be an explicit list rather than
#: a silent "not found, never mind", which would excuse a real typo too.
#: Nothing here can be checked at all: the ranges are ``pronotepy`` 2.15.6
#: while the pin is 2.15.7, and that code is not versioned in this repository.
EXEMPT = frozenset({"pronoteAPI.py", "clients.py", "dataClasses.py"})

#: A hunk header from ``git diff -U0``; group 1 is the first line changed on
#: the old side. ``-U0`` matters: with context lines the header starts early
#: and the check reports drift where there is none.
HUNK = re.compile(r"^@@ -(\d+)", re.MULTILINE)

#: A ``git blame --porcelain`` line header: commit, original line, final line.
#: Porcelain repeats it for every line, so this alone maps line to commit.
BLAME = re.compile(r"^([0-9a-f]{40}) \d+ (\d+)", re.MULTILINE)

#: Blame's sha for a line that is not committed yet. Its citation cannot have
#: drifted -- whoever is writing the line is looking at the code now.
UNCOMMITTED = "0" * 40


class HistoryUnavailableError(RuntimeError):
    """Git could not answer, so the drift check cannot run.

    Raised rather than skipped, and that is the whole point.
    ``actions/checkout`` clones one commit deep by default, which makes every
    history question return nothing -- and a check that reads "no answer" as
    "no problem" reports success for a gate that never ran. This project has
    been bitten by exactly that shape twice already: a mkdocs warning that
    only fails under ``--strict``, and a test image whose pinned library was
    not the pinned library. Opting out has to be visible, hence ``--no-drift``.
    """


def _git(*args: str) -> str:
    """One git command, or a refusal to guess when git will not answer."""
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["git", "-C", str(ROOT), *args],  # noqa: S607 - git resolved from PATH
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or "no output"
        raise HistoryUnavailableError(f"git {' '.join(args)} failed: {detail}")
    return result.stdout


def _require_full_history() -> None:
    """Refuse to run the drift check on a clone that cannot answer."""
    if _git("rev-parse", "--is-shallow-repository").strip() == "true":
        raise HistoryUnavailableError(
            "this is a shallow clone, where every history question returns "
            "nothing. Use fetch-depth: 0, or pass --no-drift to skip the "
            "drift check deliberately"
        )


def _resolve_ref(ref: str) -> str:
    """A commit for ``ref``, or a refusal -- never a silent report-only run."""
    return _git("rev-parse", "--verify", f"{ref}^{{commit}}").strip()


def _resolve(name: str) -> Path | None:
    """Where a cited module lives, or ``None`` if it is nowhere."""
    for root in SEARCH_ROOTS:
        candidate = root / name
        if candidate.is_file():
            return candidate
    return None


def _blame(document: Path) -> dict[int, str]:
    """Which commit last wrote each line of a document."""
    porcelain = _git("blame", "--porcelain", "--", str(document))
    return {int(match.group(2)): match.group(1) for match in BLAME.finditer(porcelain)}


class _Drift:
    """Answers "has this file moved since that commit", once per pair."""

    def __init__(self) -> None:
        self._seen: dict[tuple[str, Path], int | None] = {}

    def first_change(self, baseline: str, target: Path) -> int | None:
        """The lowest line of ``target`` touched since ``baseline``, if any."""
        key = (baseline, target)
        if key not in self._seen:
            diff = _git("diff", "-U0", f"{baseline}..HEAD", "--", str(target))
            starts = [int(match.group(1)) for match in HUNK.finditer(diff)]
            self._seen[key] = min(starts) if starts else None
        return self._seen[key]

    def moved_above(self, baseline: str, target: Path, end: int) -> int | None:
        """The first changed line, when it sits at or above ``end``."""
        first = self.first_change(baseline, target)
        if first is None or first > end:
            return None
        return first


#: One citation: the text as written, its line in the document, the resolved
#: file, and the range.
Citation = tuple[str, int, Path | None, int, int]


def _citations_in(document: Path) -> list[Citation]:
    """Every source location cited by one document, in order of appearance."""
    text = document.read_text(encoding="utf-8")
    found: list[Citation] = []
    for match in CITATION.finditer(text):
        name = match.group(1)
        if name in EXEMPT:
            continue
        start = int(match.group(2))
        end = int(match.group(3)) if match.group(3) else start
        line = text.count("\n", 0, match.start()) + 1
        found.append((match.group(0), line, _resolve(name), start, end))
    return found


def _unfollowable(where: str, target: Path | None, start: int, end: int) -> str | None:
    """Why this citation cannot be followed at all, or ``None`` if it can."""
    if target is None:
        return f"{where}: no such file in this repository"
    if end < start:
        return f"{where}: the range ends before it starts"
    lines = len(target.read_text(encoding="utf-8").splitlines())
    if end > lines:
        return f"{where}: {target.relative_to(ROOT)} has only {lines} lines"
    return None


def _scope(line: str, column: int) -> tuple[int, int, bool] | None:
    """The span of one line a citation's symbols share, and how tight it is.

    The convention writes them together: ``(`RateLimiter.check`,
    `ratelimit.py`)`` in prose, or one cell of a table. Bounding the search
    that way is what keeps the check quiet -- the platforms table row lists
    ``sensor``, ``binary_sensor`` and five more that are domains rather than
    symbols of ``__init__.py``, and they sit outside the parentheses.

    Returns ``None`` for a file named in running prose, which is deliberate:
    ``(`session.py`, donc `note_login` ...)`` reads as a citation to any
    proximity rule, yet ``note_login`` lives in ``ratelimit.py`` and
    ``session.py`` merely calls it. Prose puts symbols near files for every
    reason there is, so nothing there can be checked without inventing
    intent. Never crosses a newline either: unbalanced brackets are ordinary
    in prose, and a runaway span would swallow half a section.

    The flag says whether the span came from parentheses, which are exact, or
    from a table cell, which is looser and needs the adjacency rule.
    """
    opened = line.rfind("(", 0, column)
    if opened != -1 and ")" not in line[opened:column]:
        closed = line.find(")", column)
        if closed != -1:
            return opened, closed, True
    if line.lstrip().startswith("|"):
        left = line.rfind("|", 0, column)
        right = line.find("|", column)
        if left != -1 and right != -1:
            return left, right, False
    return None


@cache
def _definitions(target: Path) -> frozenset[str]:
    """Every name the cited module defines, at any depth.

    Parsed rather than pattern-matched: a regex for ``def`` misses a name
    bound by assignment, and the names most cited here are module constants.
    Deliberately a flat set with no notion of scope -- the question is "does
    this symbol still exist under this name", not "is it reachable from the
    module root", so a method counts and so does a nested class.
    """
    found: set[str] = set()
    tree = ast.parse(target.read_text(encoding="utf-8"), filename=str(target))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            found.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            found.add(node.id)
        elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store):
            found.add(node.attr)
        elif isinstance(node, ast.alias):
            found.add((node.asname or node.name).split(".")[0])
    return frozenset(found)


#: A backticked token that could name a Python symbol, possibly dotted.
SYMBOL = re.compile(r"`([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)`")

#: Suffixes that make a backticked token a filename rather than a symbol.
#: ``services.yaml`` beside ``services.py`` is a sibling file, not a member.
NOT_SYMBOLS = (".py", ".yaml", ".yml", ".json", ".md", ".txt", ".toml", ".cfg")

#: A lint rule code -- ``SLF001`` sits beside ``gateway.py`` because the file
#: silences it, not because it defines it. Narrow on purpose: a real constant
#: such as ``FUNC_NEWS`` carries an underscore and no digits.
RULE_CODE = re.compile(r"^[A-Z]{1,5}[0-9]{3,4}$")


def _candidates(line: str, span: tuple[int, int, bool], column: int) -> list[str]:
    """The symbols a file token is plausibly cited alongside.

    Inside parentheses, everything in the group counts: the convention puts
    exactly the citation there. In a table cell it is only the token
    immediately before or after the file, because a cell legitimately lists
    several unrelated things -- a milestone row naming ``manifest``, two
    protocol tabs and two modules is a list, not five citations.
    """
    left, right, parenthesised = span
    tokens = [
        (match.start(), match.group(1))
        for match in SYMBOL.finditer(line)
        if left < match.start() < right
    ]
    usable = [
        (at, token)
        for at, token in tokens
        if not token.endswith(NOT_SYMBOLS) and not RULE_CODE.match(token)
    ]
    if parenthesised:
        return [token for _, token in usable]
    anchors = [index for index, (at, _) in enumerate(tokens) if at == column]
    if not anchors:
        return []
    neighbours = {anchors[0] - 1, anchors[0] + 1}
    wanted = {token for index, (_, token) in enumerate(tokens) if index in neighbours}
    return [token for _, token in usable if token in wanted]


def _symbol_citations_in(document: Path) -> list[tuple[str, str, Path, list[str]]]:
    """Every ``(`symbol`, `file.py`)`` pairing, grouped by the file cited.

    Grouped rather than flat because the verdict is per group: a cell or a
    parenthesis where *nothing* resolves is a citation gone stale, while one
    where some resolve and some do not is a list that happens to sit beside a
    file. The two deserve different force.
    """
    found: list[tuple[str, str, Path, list[str]]] = []
    for number, line in enumerate(
        document.read_text(encoding="utf-8").splitlines(), start=1
    ):
        for anchor in SYMBOL.finditer(line):
            name = anchor.group(1)
            if not name.endswith(".py") or name in EXEMPT:
                continue
            target = _resolve(name)
            if target is None:
                continue
            span = _scope(line, anchor.start())
            if span is None:
                continue
            tokens = _candidates(line, span, anchor.start())
            if tokens:
                found.append((f"{document.name}:{number}", name, target, tokens))
    return found


class Report:
    """What one run found, kept apart because the halves differ in force."""

    def __init__(self) -> None:
        self.checked = 0
        self.symbols = 0
        self.unfollowable: list[str] = []
        self.blocking: list[str] = []
        self.backlog: list[str] = []

    @property
    def failed(self) -> bool:
        """Whether this run should fail the build."""
        return bool(self.unfollowable or self.blocking)


def _touched_here(since: str | None, drift: _Drift | None, target: Path) -> bool:
    """Whether the commits under test changed the cited file at all."""
    if since is None or drift is None:
        return False
    return drift.first_change(since, target) is not None


def _check(
    document: Path, since: str | None, drift: _Drift | None, into: Report
) -> None:
    """Check one document, accumulating into ``into``."""
    for where, name, target, tokens in _symbol_citations_in(document):
        into.symbols += len(tokens)
        defined = _definitions(target)
        # The last component only. `account.async_request_tick` names an
        # attribute on an instance, `RateLimiter.check` a method on a class;
        # in both the leading part is a holder this file need not define.
        missing = [t for t in tokens if t.rsplit(".", 1)[-1] not in defined]
        if not missing:
            continue
        listed = ", ".join(f"`{t}`" for t in missing)
        if len(missing) < len(tokens):
            into.backlog.append(
                f"{where}: {name} defines no {listed}, though other symbols "
                f"cited beside it do resolve -- probably a list, not a citation"
            )
        elif _touched_here(since, drift, target):
            # Blocking on the same principle as drift: these commits changed
            # the file, and a symbol the documentation names against it is
            # gone. That is the refactor case, and the author can act on it.
            into.blocking.append(
                f"{where}: {name} defines no {listed}, and these commits "
                f"changed that file -- rename it in the documentation too"
            )
        else:
            # Opening a new check as a wall fails changes that did not cause
            # the problem, and a gate like that gets switched off. Notes now,
            # blocking once the backlog is empty.
            into.backlog.append(f"{where}: {name} defines no {listed}")

    citations = _citations_in(document)
    into.checked += len(citations)
    blame = _blame(document) if drift is not None else {}

    for cited, line, target, start, end in citations:
        where = f"{document.relative_to(ROOT)} -> {cited}"
        broken = _unfollowable(where, target, start, end)
        if broken is not None:
            into.unfollowable.append(broken)
            continue
        if drift is None or target is None:
            continue

        # Whether the range drifted at all is settled once, against the
        # citation's own baseline. Asking the tested range that question
        # instead flags a citation somebody has already re-opened *because*
        # the file moved -- which is what happened the first time this ran:
        # `device_trigger.py:58-73` had been re-cited as 60-75 in the very
        # commit that fixed it, and the check demanded it be looked at again.
        # A gate that fires on work already done is a gate people learn to
        # ignore.
        baseline = blame.get(line)
        if baseline is None or baseline == UNCOMMITTED:
            continue
        moved = drift.moved_above(baseline, target, end)
        if moved is None:
            continue

        # It did drift. Blocking only if the commits under test are what moved
        # it -- their author is the one who can act, and is being told they
        # just invalidated a citation.
        caused_here = (
            since is not None and drift.moved_above(since, target, end) is not None
        )
        if caused_here:
            into.blocking.append(
                f"{where}: {target.relative_to(ROOT)} changed from line "
                f"{moved} in the commits under test, so this range may have "
                f"moved -- re-open it"
            )
        else:
            into.backlog.append(
                f"{where}: {target.relative_to(ROOT)} changed from line "
                f"{moved} since {baseline[:9]}, which last wrote this line"
            )


def _summarise(report: Report, since: str | None, *, drift: bool) -> None:
    """Print what was found, and say plainly what the green means."""
    for problem in [*report.unfollowable, *report.blocking]:
        print(f"error: {problem}")
    for problem in report.backlog:
        print(f"note: {problem}")

    if not drift:
        scope = "drift check skipped"
    elif since is None:
        scope = "nothing blocking: no --since, so every drift is a note"
    else:
        scope = f"blocking against {since[:9]}"
    print(
        f"{report.symbols} symbol citations and {report.checked} line "
        f"citations checked, {len(report.unfollowable)} unfollowable, "
        f"{len(report.blocking)} drifted here, "
        f"{len(report.backlog)} drifted earlier ({scope})."
    )
    if report.unfollowable:
        print(
            "A citation that cannot be followed sends the reader to the wrong "
            "code, which costs more than no citation at all."
        )
    if report.blocking:
        print(
            "Re-open each blocking range above and re-cite it. This check "
            "knows the file moved, not where the range should now be -- and "
            "it cannot tell a range that slid from one that is still right."
        )
    if report.backlog:
        print(
            f"The {len(report.backlog)} note(s) are a pre-existing backlog, "
            "not caused here, and do not fail this run. They are still wrong: "
            "two of three sampled were pointing at unrelated code."
        )


def main() -> int:
    """Report every citation that cannot be followed, or may have moved."""
    argv = sys.argv[1:]
    # Spelled out rather than left to argparse because the failure mode being
    # avoided is a silently ignored argument: `--since <ref>` in two words, or
    # a typo in the flag, would leave the run report-only while the workflow
    # step went green -- the shallow-clone trap again, from another door.
    unknown = [
        arg for arg in argv if arg != "--no-drift" and not arg.startswith("--since=")
    ]
    if unknown:
        print(
            f"error: unrecognised argument(s) {' '.join(unknown)}. Usage: "
            "check_doc_citations.py [--since=<ref>] [--no-drift]. Note the "
            "`=`: --since takes it, and a separate word would be ignored."
        )
        return 1
    wanted = "--no-drift" not in argv
    since_ref = next(
        (arg.split("=", 1)[1] for arg in argv if arg.startswith("--since=")),
        None,
    )

    report = Report()
    documents = [*sorted((ROOT / "docs").rglob("*.md")), ROOT / "README.md"]

    try:
        drift = None
        since = None
        if wanted:
            _require_full_history()
            drift = _Drift()
            # An unresolvable --since is an error, not a quiet fall back to
            # report-only: that would be the shallow-clone trap wearing a hat.
            since = _resolve_ref(since_ref) if since_ref else None
        for document in documents:
            if document.is_file():
                _check(document, since, drift, report)
    except HistoryUnavailableError as error:
        print(f"error: {error}")
        return 1

    _summarise(report, since, drift=wanted)
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())
