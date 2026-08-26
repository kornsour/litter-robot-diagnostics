# Project conventions

Python project scaffolded from `kornsour/python-template`.

- **Purpose:** read-only capture of owner-authorized Litter-Robot 4 cloud state,
  activity, lifecycle, and diagnostic data. Do not add undocumented mutations or
  device-control commands to `capture.py`, `queries.py`, `analysis.py`,
  `store.py`, `sensors.py`, `drawer.py`, or `subscriptions.py` — those stay
  read-only.
- **Device control is confined to `autoreset.py`.** It is the one deliberate
  exception: an opt-in watchdog that clears stuck cat-sensor states with
  `shortResetPress` + `cleanCycle`. Rules for it:
  - No new command reaches the device without an explicit CLI flag. `--arm`
    gates dispatch; unarmed is the default and must stay the default.
  - A reset can rotate the globe with a cat inside. Every safety gate
    (persistence, quiet period, ToF clearance, rate limit, escalation) is
    load-bearing — do not weaken or bypass one without saying so plainly.
  - Keep the decision logic in `Watchdog` pure and clock-injected so it stays
    testable without a device.
  - Record every assessment and attempt under source `intervention`;
    interventions contaminate the diagnostic record and must stay traceable.
- **Secrets:** Whisker passwords and refresh tokens belong in the OS keyring.
  Never print credentials, authorization headers, raw tokens, or clear device
  serial numbers.
- **Logs:** redact direct identifiers before persistence. Preserve fault codes,
  trace IDs, sensor values, firmware versions, and timestamps needed for
  diagnosis.
- **Env & deps:** `uv`. `make setup` creates the venv and installs `.[dev]`. Add
  runtime deps to `[project.dependencies]`; keep heavy/optional ones under
  `[project.optional-dependencies]` so CI stays light.
- **Layout:** `src/` layout, package under `src/<name>/`, tests under `tests/`.
- **Quality gate:** `make check` (ruff lint + ruff format + pyright + pytest) is
  exactly what CI enforces. Run it before pushing.
- **CI:** `.github/workflows/ci.yml` calls the reusable
  `kornsour/gh-automation/.github/workflows/python-ci.yml`. Don't inline CI logic
  here — change it upstream in `gh-automation` so every repo benefits.
- **Dependencies:** Dependabot opens grouped weekly PRs; patch/minor auto-merge
  when green. Review majors yourself.
- **`main` is protected:** merge via PR; the `ci / Lint, type-check & test` check
  must pass.
