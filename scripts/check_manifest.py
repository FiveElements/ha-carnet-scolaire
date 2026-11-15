#!/usr/bin/env python3
"""Manifest invariants that must hold in CI.

Two of them, both from SPECIFICATION.md:

* §8.1 -- ``manifest.json`` must carry no ``loggers`` key. Setting the
  ``pronotepy`` logger to DEBUG makes ``pronoteAPI.py`` write the request
  payload in the clear, credentials included. The requirement is worth nothing
  unless something enforces it, because a ``loggers`` entry is exactly the kind
  of line someone adds while debugging and forgets to remove.
* §11.1 -- on a release, ``version`` must equal the tag. A HACS integration
  whose manifest diverges from its tag installs once and never updates again.

And one that comes from an outage rather than from the specification: the
``pronotepy`` pin is declared twice, here and in ``requirements_test.txt``, and
nothing made the two agree. ``manifest.json`` is what Home Assistant installs
on a user's instance; ``requirements_test.txt`` is what the suite runs against.
A divergence therefore means CI is green against a library nobody ships --
which matters precisely because ``hardened_client.py`` corrects
version-specific upstream behaviour, so "tested" and "installed" being
different versions makes the corrections unverified. The parser lives in
``scripts/pronotepy_pin.py``, shared with the weekly upstream watch, so the pin
is read from the repository and never restated.
"""

import argparse
import json
from pathlib import Path
import sys

# ``scripts/`` is not a package, and this file is only ever run as a script --
# so its own directory is ``sys.path[0]`` and the sibling module imports
# plainly. Making the directory importable any other way would be the check
# dictating the repository layout.
from pronotepy_pin import PinError, pinned_version

MANIFEST = Path("custom_components/carnet_scolaire/manifest.json")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--expect-version",
        help="fail unless manifest.json declares exactly this version",
    )
    args = parser.parse_args()

    if not MANIFEST.is_file():
        print(f"manifest not found: {MANIFEST}", file=sys.stderr)
        return 2

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    failures: list[str] = []

    if "loggers" in manifest:
        failures.append(
            "manifest.json declares a `loggers` key. Enabling the pronotepy "
            "logger at DEBUG writes credentials to the log (SPECIFICATION.md "
            "§8.1). Remove the key."
        )

    if args.expect_version is not None:
        declared = manifest.get("version")
        if declared != args.expect_version:
            failures.append(
                f"manifest version {declared!r} does not equal the release tag "
                f"{args.expect_version!r}"
            )

    failures.extend(
        f"manifest.json is missing the required key {key!r}"
        for key in ("domain", "name", "version", "documentation", "codeowners")
        if key not in manifest
    )

    pin: str | None = None
    try:
        pin = pinned_version()
    except (PinError, OSError) as error:
        failures.append(str(error))

    if failures:
        print("Manifest check failed:", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1

    print(f"manifest.json ok (version {manifest.get('version')}, pronotepy=={pin})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
