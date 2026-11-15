# AGENTS.md

Loaded into every agent session, so it is kept short: each rule in one or two
lines. The reasoning and the incidents behind them are in
**`AGENTS-REFERENCE.md`** — read the section named in brackets before
changing the code it covers. `CONTRIBUTING.md` is the human-facing version.

## Working style

- **Think before coding.** State assumptions; if several readings exist, say
  so; if something is unclear, stop and ask. Push back when warranted.
- **Simplicity first.** Minimum code that solves the problem — no speculative
  features, abstractions or configurability.
- **Surgical changes.** Every changed line traces to the request. Match the
  existing style; mention unrelated dead code, don't delete it; remove only the
  orphans your own change created.
- **Goal-driven.** Turn the task into a verifiable check (a failing test, a
  gate) and loop until it passes.

## Project

Home Assistant custom integration for PRONOTE and EcoleDirecte. Domain
`carnet_scolaire`, repo `ha-carnet-scolaire`, name "Carnet scolaire"
(`pronote_ng` / "Pronote NG" until 0.0.32; no domain migration exists, an old
entry is removed and re-added). `docs/SPECIFICATION.md` and
`docs/annexe-b-rate-limit.md` are cited by number (`§5.1`, `annexe B §2.4`)
throughout the source — follow the reference rather than guessing.

## Three rules that override convenience [reference: The three rules]

1. **No real credential anywhere** — username, password, PIN, token, QR
   content, iCal URL (`icalsecurise=`), student or establishment name, real
   `N`. Examples are visibly fictional (`demo.example.invalid`, `Enfant Un`,
   `STUDENT-1`). Fixtures are hand-written; captures go in the gitignored
   `tests/fixtures/_raw/`. An iCal URL is a password.
2. **Never enable the `pronotepy` DEBUG logger** — it leaks credentials
   (`pronoteAPI`) and decoded personal data (`dataClasses`), two independent
   loggers. No `loggers` key in `manifest.json` (`scripts/check_manifest.py`).
   Debug with `custom_components.carnet_scolaire: debug` only; never suggest
   `pronotepy` to a user.
3. **Never commit or push to `main`.** One branch per subject, then a PR;
   `main` moves only by merging a PR with its workflows green. "Commit and
   push" on `main` means: branch, commit, push the branch, open the PR.

## Graft before grep

`graft/` (generated, gitignored; `graft build` once after cloning) indexes
every symbol and call edge. Use `graft ask "<task>" --source`, `graft grep`,
`graft skeleton <file>`, `graft callers <sym> --depth all` (blast radius before
any change) before reading source. Docs cite a **symbol**, never a line range
(`scripts/check_doc_citations.py`).

## Commands

The gates, exactly what `.github/workflows/validate.yml` runs:

```bash
ruff check . && ruff format --check .
mypy --strict custom_components/carnet_scolaire
python scripts/check_manifest.py
pytest tests -n auto --cov=custom_components/carnet_scolaire --cov-branch --cov-report=xml:coverage.xml
python scripts/check_coverage.py coverage.xml
python scripts/check_doc_citations.py --since=<base-ref>   # one argument; host only (needs git)
mkdocs build --strict                                      # needs requirements_docs.txt
```

**Windows** [reference: Running tests on Windows]: only `ruff` runs natively;
`pytest` and `mypy` run in the floor Docker image:

```bash
MSYS_NO_PATHCONV=1 docker run --rm -v "C:/project/ai-project/carnet-scolaire/ha-carnet-scolaire:/work" -w /work \
  ha-pronote-test-2026.11.0 python -m pytest tests -n auto -q --no-header --show-capture=no
```

Never add `-p no:logging` (removes `caplog`). `-p no:pytest_homeassistant` runs the
HA-free half but also drops the network guard.

**Generated files — never hand-edit:** `strings.json`, `translations/*.json`,
`services.yaml`, `icons.json` come from `scripts/build_translations.py` (a test
compares them). No URL in a translation string (use
`description_placeholders`); `manifest.json` keys ordered `domain`, `name`,
then alphabetical.

**Version floor = one number in 21 files:** `hacs.json` ⇄ the
`requirements_test.txt` pin ⇄ the gated row of `validate.yml` ⇄ 18 blueprint
`min_version`. Currently **2026.11.0** /
`pytest-homeassistant==2026.11.0b6` (official harness; never install it beside
`pytest-homeassistant-custom-component`). While the floor's harness is a beta,
CI installs the released `homeassistant` over it — drop that line once
`pytest-homeassistant==2026.11.0` exists (look it up with `--pre`: the newest
final under that name is an unrelated `0.1.3`). `tests/test_version_floor.py`
holds all 21 and CI checks the installed HA equals `hacs.json`. Raise it by
running the new socle green first, then moving the declaration.

## Architecture [reference: Architecture]

One path to the network, auditable:
`scheduler.py` → `account.py` → `session.py` (one lock + one worker per entry)
→ `ratelimit.py` → `hardened_client.py` → `gateway.py` (sole `pronotepy`
importer) → `models.py` (frozen DTOs; nothing from `pronotepy` passes).

Invariants that break silently:

- **Costs are declared, not inferred** — `GatewayResult.calls` is the real
  request count; a lazy-property read is an uncounted request.
- **Charged at admission, under the lock, before the request** — never on
  return, and no refund path.
- **The DTO boundary is not aesthetic** — a `pronotepy` object escaping to an
  entity is a network call from the event loop.
- **Stale beats unavailable**, asked as two questions: `available` checks
  `snapshot is not None`; `FetchScheduler.is_stale` answers only about age and
  is `False` for a tier never collected. No attributes on an `unavailable`
  entity — read `diagnostics.py`, not the state.
- **A restart refills during quiet hours, on purpose** — never advise
  postponing a restart. `CRITICAL` (first collection, ≤3 attempts) and
  `GESTURE` (services, buttons, to-do tick) are the only quiet-hours
  exemptions (`QUIET_HOURS_EXEMPT`); neither escapes the daily caps.
- **§1 objective:** any ordinary school automation is writable without a Jinja
  template — facts live in an entity **state** (timestamp, number, boolean);
  "something new" is an `event` entity, one event per changed aspect.
- **Secrets by construction:** iCal URL, identity block, PDF link are
  `SupportsResponse.ONLY` service results — never state or attributes.
  Writes to PRONOTE: on for a new entry, off for an entry lacking the key.
- **Config flow:** `flow_login.py` is synchronous (building a client *is* a
  login) and runs in the executor; `config_flow.py` never imports `pronotepy`
  at module scope; one attempt per gesture; the 2FA PIN is never persisted
  (`_NEVER_PERSISTED` on every path).
- `pronotepy==2.15.7` is pinned; raising it means re-reading every divergence
  documented in `hardened_client.py`.

## Coverage gate [reference: Coverage gate]

`scripts/check_coverage.py`, measured as `min(line, branch)`: **95 %** on every
module (no exception list), **100 %** on `ratelimit.py`, `scheduler.py`,
`gateway.py`, `delta.py` and `connectors/ecoledirecte/ed_*.py`, **80 %**
global.

## Tests [reference: Test conventions]

- A test name is a sentence; its docstring says which defect it prevents.
- One seam is doubled: `session.build_client` (`account` fixture); protocol
  fixtures are raw dictionaries decoded by the real `pronotepy`. A new
  `FakeClient` method counts requests like the real library.
- A test that triggers a reload drains it with
  `await hass.async_block_till_done()` **inside** the patch suppressing set-up.
- A limiter's sleeper is real time unless patched (`no_spacing` for PRONOTE,
  `ed_limiter._sleep` for EcoleDirecte) — an unpatched one costs seconds per
  test.
- **Never derive an `entity_id` from a translation key**: slugify the
  translated name (`homeassistant.util.slugify`); the suite uses English slugs.
  `tests/test_doc_contract.py` checks names against `docs/annexe-a-entites.md`.

## Releasing

The tag is the only source of version: `manifest.json` and `pyproject.toml`
stay `0.0.0`, no release PR. Merge to `main` with `Validate`, `Hassfest`,
`HACS` and `Docs` green, then `git tag -a vX.Y.Z && git push origin vX.Y.Z`;
`release.yml` stamps the manifest, re-checks it, zips `carnet_scolaire.zip`
and publishes. HACS installs that zip.
