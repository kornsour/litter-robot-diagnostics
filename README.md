# Litter-Robot Diagnostics

A local diagnostic recorder for Whisker Litter-Robot 4 devices. It keeps the
detailed cloud state that the Whisker app normally compresses into messages such
as “cycle interrupted” or “scale active too long.”

Capture and analysis are strictly read-only. One opt-in component, the
[autoreset watchdog](#autoreset-watchdog), does send commands — it is the single
deliberate exception, it is off unless you invoke it, and it dispatches nothing
unless you also pass `--arm`.

The recorder uses the unofficial, reverse-engineered
[`pylitterbot`](https://github.com/natekspencer/pylitterbot) client for
authentication and live state, plus read-only GraphQL queries for activity,
connectivity lifecycle, SmartWeight pet visits, historical summaries, firmware,
and the LR4 unit-diagnostics record.

> This project is not affiliated with or endorsed by Whisker. Whisker does not
> publish a supported API, so endpoints and fields may change.

## Safety and privacy

- `capture`, `summary`, and `analyze` send no device-control commands and
  contain no GraphQL mutations. Only `autoreset --arm` writes to the device.
- Passwords and refresh tokens are stored in the operating-system keyring.
- Passwords, tokens, authorization headers, and clear device serials are never
  written to the diagnostic database.
- Device and account identifiers are redacted before persistence. Serial
  numbers become stable one-way pseudonyms so events can still be correlated.
- The SQLite database is local and ignored by Git.

## What it captures

Live state includes:

- `robotStatus`, `robotCycleStatus`, `robotCycleState`, and `displayCode`
- `catDetect`, `weightSensor`, `isCatDetectPending`, and `isDebugModeActive`
- pinch, USB, globe motor, and retract motor faults
- raw litter/drawer measurements, laser-dirty status, Wi-Fi RSSI, and firmware
- the complete raw state payload for future fields

Raw activity records retain `originalHex`, `valueString`, `stateString`,
`actionValue`, and `commandSource`. They arrive through both a live activity
subscription and periodic history polling; the database deduplicates the two
paths. Lifecycle records retain disconnect reasons and trace IDs.

Additional read-only evidence includes:

- direct state snapshots every 30 seconds, supplementing websocket updates
- SmartWeight visits with stable pseudonymous pet IDs, weight, reassignment
  status, and the originating pseudonymous LR4
- the longer curated `historyDownload` backfill, which can include events such
  as `CatDetectStuckWeight` that are absent from the short raw activity window
- installed-versus-current ESP, PIC, and laser-board firmware
- weekly summaries and longer-range cycle/cat-detection insights
- sanitized GraphQL warning paths showing exactly which diagnostic fields the
  owner account was denied, without retaining authorization metadata

When the owner account is authorized to read it, the unit-diagnostics record
also includes scale readiness, three ToF distances and slopes, sensor readings,
motor current/speed, per-phase cycle timers, and captured motor-fault
measurements.

## Setup

Requirements:

- Python 3.12+
- [`uv`](https://docs.astral.sh/uv/)
- A Whisker account that owns an LR4

```bash
make setup
```

Store the Whisker password in your OS keyring:

```bash
uv run lr4-diagnostics auth store --username you@example.com
```

The password prompt does not echo. The username can alternatively be supplied
through `WHISKER_USERNAME`; `WHISKER_PASSWORD` is supported for ephemeral
automation but is not recommended for everyday use. The stored account becomes
the default for later commands, so `--username` can be omitted without putting
the account identifier in scripts or shell history again.

## Capture

Start a live recording:

```bash
uv run lr4-diagnostics capture \
  --database logs/lr4-diagnostics.sqlite
```

Stop with `Ctrl-C`. Defaults are intentionally conservative:

- live state through Whisker’s websocket subscription
- direct read-only state refresh every 30 seconds
- raw activity through a websocket subscription, backed by a 60-second poll
- activity refresh every 60 seconds
- unit diagnostics every 60 seconds
- connectivity lifecycle refresh every 5 minutes
- SmartWeight pet history every 5 minutes
- longer history, firmware, summaries, and insights every hour

Intervals are configurable:

```bash
uv run lr4-diagnostics capture \
  --database logs/lr4-diagnostics.sqlite \
  --state-refresh-interval 30 \
  --diagnostics-interval 30 \
  --activity-interval 60 \
  --lifecycle-interval 300 \
  --pet-interval 300 \
  --extended-interval 3600
```

Review capture counts and the most recent records:

```bash
uv run lr4-diagnostics summary --database logs/lr4-diagnostics.sqlite
```

## Autoreset watchdog

`autoreset` watches for the states that leave the cats without a usable box and
clears them the same way you would at the unit, using the `shortResetPress` and
`cleanCycle` commands.

It detects:

| State | Signal | Default patience |
|---|---|---|
| Cycle stall | a cycle frozen on one `robotCycleStatus`/`robotCycleState` | 5m |
| Cycle overrun | any cycle running too long overall | 10m |
| Idle latch | `DC_CAT_DETECT_30M` / `ROBOT_CAT_DETECT_DELAY` | 15m past the unit's own 30m threshold |

The overrun ceiling exists because this unit's cycles retry rather than freeze:
`robotCycleState` keeps flipping between `PROCESS`, `CAT_DETECT`, and `WAIT_ON`,
which resets the stall timer forever. A cycle that never finishes is stuck even
while its internals churn — the median cycle here is 3.4m even when aborting.

### The two modes are not equally urgent, and get different recoveries

A **cycle stall** has parked the globe away from home, so the box is unusable
and the cats go elsewhere in the house. It is the urgent mode, and its patience
is deliberately the shortest of the three: 5m on one unchanged state, against a
longest-normal-phase of 57s on this unit's own diagnostics timers. Recovery
presses Reset, then *watches where the globe actually goes* and only presses
Cycle if Reset did not bring it home by itself — see below.

An **idle latch** leaves the unit at home, where the cats can still use it; it is
just refusing to clean. That is the less urgent mode and the riskier one to act
on, since a cat could genuinely be inside a globe at home. It gets the longer
grace, the ToF clearance check, and — once the phantom detection is cleared —
the Cycle press, because the cleaning it owed is exactly what was missing.

In practice `--quiet-period`, not `--stall-grace`, usually sets how fast a stall
is answered: the abort that caused it emits a `robotCycleStateCatDetect` row, so
the ten-minute silence requirement effectively starts at the last abort. Response
lands around 10m after the stall begins rather than 5m. Lowering `--stall-grace`
further will not change that; only relaxing the quiet period would, and that is
the gate standing between a reset and a cat.

### Reset is a pause/resume toggle, not a "go home" command

**Measured 2026-07-30 by `probe-reset`**, then corrected by what the owner
observed pressing the buttons by hand:

| State | `shortResetPress` | `cleanCycle` |
|---|---|---|
| Idle at home | recalibrates (double-press) | starts a cycle |
| Cycle running | **pauses it where it stands** | — |
| Cycle paused | **resumes it, runs out to home** | ignored entirely |

Full yellow across all five buttons is the paused state (`DC_USER_PAUSE` /
`CYCLE_STATE_PAUSE`). Blue with partial yellow is the idle latch
(`DC_CAT_DETECT_30M`). They need different commands:

| Light bar | State | Recovery |
|---|---|---|
| All five yellow | paused mid-cycle | **Reset** — resumes and runs to home |
| Blue + partial yellow | idle latch | **Reset, then Cycle** — clear the phantom detect, then do the cleaning it owed |
| — | stalled mid-cycle | **Reset, observe, Reset again** — first press breaks the stall into a pause, second resumes it |

So recovery is a loop that observes between presses, not a fixed sequence.
Firing Reset twice blindly would pause a cycle that had already resumed;
firing Reset-then-Cycle leaves a paused globe untouched, because `cleanCycle`
does nothing to a pause. `next_recovery_command` holds the decision table and
Reset is capped at two presses per attempt.

Two traps worth stating plainly:

- **A paused cycle keeps reporting the phase it stopped in.**
  `robotCycleStatus` still reads `CYCLE_DUMP`, so `is_cycling` is true even
  though nothing is moving. `Observation.is_paused` tells them apart.
  `is_cycling` still counts a paused cycle so the stall timers catch it — the
  pause changes the remedy, not the detection.
- **`cleanCycle` is inert on a paused unit.** Dispatched and ignored for a full
  five minutes in the first probe run, which stranded the globe until someone
  pressed Reset by hand.

To re-run the experiment, or to check the behaviour again after a firmware
change:

```bash
uv run lr4-diagnostics probe-reset --database logs/lr4-diagnostics.sqlite --arm
```

This starts a cycle, waits for the globe to leave home, presses Reset and
nothing else, and reports whether the globe came home on its own. If it did not,
it presses Cycle so the unit is not left stalled. **The globe must be empty and
you must be at the unit.** It refuses to run unless armed, the bonnet is on, the
unit is idle at home, and the ToF floor is clear, and it records the whole
timeline under source `intervention` with kind `probe`.

### Read this before arming

A reset can start the globe turning. The watchdog triggers on a sensor this unit
is already known to misreport, so it can never *prove* the globe is empty — it
can only make an occupied globe very unlikely. Six gates stand between a
detection and a command, and any one of them holds it off:

1. **Bonnet closed.** No command while `isBonnetRemoved` is set, or while the
   unit is not reporting that field at all. This unit reports
   `ROBOT_CAT_DETECT_DELAY` for the entire time the bonnet is off, so a cleaning
   session is indistinguishable from an idle latch on status alone — without
   this gate a session running past `--latch-grace` would earn a reset with the
   owner's hands in the globe. Reseating the bonnet also restarts the stuck
   timers, so time spent open cannot ripen into an immediate reset.
2. **Persistence.** The state must outlast any plausible cat visit
   (`--latch-grace`, `--stall-grace`).
3. **Quiet period.** No `catWeight` or cat-detect row on the activity stream for
   `--quiet-period`. A cat that walks in mid-countdown backs the watchdog off.
4. **Clearance.** For idle latches, the top-centre ToF distance must read at or
   above `--tof-clear-floor` (400mm; nominal litter is 451-454mm, a full globe
   about 441mm). An unavailable reading blocks rather than defaults open. The
   check is skipped for cycle stalls because `litterLevel` is frozen mid-cycle
   and a stale value would prove nothing.
5. **Rate limits.** `--cooldown` between attempts and `--max-per-hour` overall.
   `validate()` rejects a `--max-per-hour` that the cooldown alone already
   satisfies, since that is not a second gate at all.
6. **Escalation.** After `--max-consecutive-failures` failed recoveries the
   watchdog stands down entirely and waits for a human — and stays down until
   the DynamoDB `STATE` item is cleared by hand, so wire up the alarm below.

A recovery is **not** judged by whether the unit reached home during the
attempt. It always will: a Reset drives the globe home and a cycle finishes in
about three minutes. A drifting scale then re-forms the fault minutes later.
Success is therefore judged from later observations — the fault coming back is a
failed recovery, and only staying healthy through `--recovery-hold` counts as a
real one. Without this the failure counter cleared on every attempt, escalation
was unreachable, and a persistent latch drew a reset every ~16 minutes for as
long as it lasted.

Run it unarmed first — that is the default. It performs every assessment, logs
every decision, and records both, without sending a single command:

```bash
uv run lr4-diagnostics autoreset --database logs/lr4-diagnostics.sqlite
```

Once the decisions look right, add `--arm`:

```bash
uv run lr4-diagnostics autoreset --database logs/lr4-diagnostics.sqlite --arm
```

### It changes the evidence

Latch durations are load-bearing for the scale-drift hypothesis and for the
$50-laser-board-versus-$449-base decision. An armed watchdog truncates them.
Every assessment and attempt is written to the same database under source
`intervention`, so those windows stay identifiable:

```bash
uv run lr4-diagnostics summary --database logs/lr4-diagnostics.sqlite | grep -A5 intervention
```

`analyze` reads only `activity` rows and is unaffected, but any conclusion drawn
from a period the watchdog was armed for needs those interventions accounted for.

### Running it continuously

The watchdog is only useful if it is awake at 3am, so it ships as a container.
[compose.yaml](compose.yaml) runs it as a sidecar next to the capture service,
sharing one SQLite volume so interventions and readings land in one timeline:

```bash
cp .env.example .env && docker compose up -d --build
```

Containers have no OS keyring, so credentials come from the gitignored `.env` as
`WHISKER_USERNAME` and `WHISKER_PASSWORD`. Refreshed tokens are not persisted;
the container re-authenticates on restart. Drop `--arm` from the `autoreset`
service in `compose.yaml` to run detection-only.

### Scheduled AWS watchdog

The versioned [OpenTofu configuration](infra/) deploys a Lambda every minute
with durable watchdog state and redacted intervention records in DynamoDB. It
is detection-only unless `armed = true` is set explicitly. Each invocation
restores the prior timers, reads recent activity for the quiet-period gate, and
fails closed if that activity check is unavailable.

Every wait in that routine pass has a ceiling, because a run that outlives its
own schedule slot holds the DynamoDB lease while the invocations behind it skip
— one 175-second run cost three schedules of monitoring. Whisker requests are
capped at 20 seconds each (aiohttp's own default is 300), DynamoDB and Secrets
Manager at three attempts of 3s connect plus 5s read, and the pass as a whole at
45 seconds. Exceeding the budget logs `WATCHDOG_TIMEOUT` and fails the
invocation so the next schedule gets a clean run one minute later. A *dispatched
recovery* is exempt: it drives the globe and then waits up to `verify_timeout`
(five minutes) for it to park, which is what the 600-second Lambda timeout and
the 11-minute lease are sized for.

The same invocation also retains a low-cost diagnostic timeline: an append-only
state snapshot plus newly seen activity records. Per-robot DynamoDB cursors
track the newest activity timestamp, with a five-minute overlap so late cloud
records and retries are safe. First use and a missed schedule trigger a bounded
35-day `historyDownload` backfill. This is a one-minute polling record, not a
replacement for the local capture process's WebSocket stream or 30-second
sampling. Captured rows are redacted before persistence and expire after 90
days; the watchdog's durable state does not expire.

The Whisker credential is an AWS Secrets Manager secret, deliberately outside
OpenTofu state. Create or update it interactively (no secret is printed):

```bash
./scripts/bootstrap-watchdog-secret.sh lr4-whisker
```

Build the Linux Lambda bundle, set the returned secret ARN in a local
`infra/terraform.tfvars`, then review and apply the complete deployment:

```bash
./scripts/package-lambda.sh
cd infra
tofu init
tofu plan
tofu apply
```

Keep the first deployment unarmed and inspect CloudWatch/DynamoDB intervention
records before arming. OpenTofu owns all AWS resources; do not edit the Lambda,
schedule, IAM policy, or table in the console.

#### What CI reads, and how to arm

The deploy workflow runs on pushes to `main` that touch `src/`, `infra/`,
`scripts/package-lambda.sh`, `pyproject.toml` or `uv.lock`, and on
`workflow_dispatch`. Settings that live outside the tree change nothing in
`git`, so the manual trigger is how you apply them:

```bash
gh workflow run deploy-watchdog.yml --repo kornsour/litter-robot-diagnostics
```

| Setting | Where | Unset behaviour |
|---|---|---|
| `TF_VAR_WHISKER_SECRET_ARN` | repo **secret** | apply fails |
| `TF_VAR_ALARM_EMAIL` | repo **secret** | topic deploys with no subscriber; alarms fire into nothing |
| `WATCHDOG_ARMED` | repo **variable** | `false` |

`WATCHDOG_ARMED` is a variable rather than a secret on purpose: whether a
machine that physically moves is permitted to move should be readable at a
glance, not hidden. Set it to exactly `true` or `false` — anything else fails
the apply rather than being guessed at.

It has to be plumbed through CI at all because CI is the last writer. Arming via
a local `tofu apply` would be silently reverted to `false` by the next merge
touching `src/` or `infra/`. That direction is fail-safe, but discovering your
watchdog quietly stopped acting is not how you want to find out. To arm:

```bash
gh variable set WATCHDOG_ARMED --body true --repo kornsour/litter-robot-diagnostics
gh workflow run deploy-watchdog.yml --repo kornsour/litter-robot-diagnostics
```

Confirm what actually shipped, rather than trusting the variable:

```bash
aws lambda get-function-configuration --function-name lr4-watchdog --region us-west-2 --query 'Environment.Variables.WATCHDOG_ARMED'
```

#### Alerting

Set `alarm_email` in `infra/terraform.tfvars` and confirm the subscription mail
AWS sends. Five alarms publish to one SNS topic:

| Alarm | Fires when | Why it matters |
|---|---|---|
| `lr4-watchdog-stuck` | the unit is assessed as stuck | This is the notification worth having even unarmed: it cuts time-to-discovery from hours to minutes, so you can go press Reset yourself. |
| `lr4-watchdog-drawer-full` | the waste drawer needs emptying | Whisker only announces this with an app push — no email, no webhook, no public API. See below. |
| `lr4-watchdog-motor-fault` | the unit reports a latched hardware fault | Nothing else can see it: the unit returns to an idle display while the flag stays set, so both the app and `assess` show a healthy unit. See below. |
| `lr4-watchdog-escalated` | the watchdog stands down | Escalation is a one-way latch. It will not act again until the DynamoDB `STATE` item is deleted by hand. |
| `lr4-watchdog-errors` | ≥3 invocation errors over two 5-minute periods | The handler fails closed, so sustained errors mean the unit is unmonitored. The threshold rides over single expired-token blips. A run that blows its budget lands here too, tagged `WATCHDOG_TIMEOUT` in the log. |

##### Waste drawer

`drawer.py` folds `DFILevelPercent` into a pure state machine and emits
`WATCHDOG_DRAWER_FULL` once the level holds. Two gates keep it to one mail per
fill rather than one per check:

- **Persistence** — `drawer_consecutive_samples` (default 5) at-rest readings at
  or above `drawer_warn_percent` (default 85). The check runs every minute, so a
  single spike must not send mail. Mid-cycle and missing readings hold the streak
  rather than counting either way: the sensor is re-read during the DFI phase, so
  a mid-cycle percentage is a measurement in progress, not a level.
- **Hysteresis** — the warning is only released below `drawer_clear_percent`
  (default 60), which only an emptied drawer reaches. Releasing on the first dip
  would flap the alarm OK→ALARM and re-send the mail on every swing back up.

Together those make it **exactly one mail per fill**, however long the drawer
stays full: alarm actions fire on state transitions only, and the marker line
repeating every check is what holds the alarm in ALARM and so *suppresses*
further mail. There is deliberately no `ok_actions` — notifying on the clear
would double the volume to tell the owner something they just did themselves.

`isDFIFull` bypasses the persistence gate — it is a boolean rather than a noisy
distance, and it is the signal the app push itself fires on, so waiting would
only deliver the mail later than the phone notification. It is also why the
percentage threshold is low-risk: it can only warn *early*, never miss.

Sizing the band: in the 2026-07-25/28 capture the drawer percentage was stable
to 0.0 within every hour but one, and swung 7 points (18→25) in the hour a cycle
ran — including in the at-rest samples either side of it. A 25-point band leaves
comfortable margin over that. The capture never reached a full drawer, though,
so the 85 threshold itself is unvalidated against this unit; re-check it against
`dfi_level_pct` once a real fill has been recorded.

##### Latched hardware faults

The LR4 latches fault flags — `globeMotorFaultStatus`, `retractMotorFaultStatus`,
`pinchStatus`, `USBFaultStatus` — and keeps reporting them until the firmware
clears them, while the unit itself goes back to a normal idle display. Neither
the app nor the watchdog notices: `Observation` carries status, display code,
cycle status/state, litter level and bonnet, so a unit parked at home with a
latched motor fault assesses as `is_healthy`.

`faults.py` folds those flags into a pure state machine and emits
`WATCHDOG_MOTOR_FAULT`. One gate keeps it honest — `fault_consecutive_samples`
(default 3) readings of the same value. That is deliberately lower than the
drawer's five: a fault status is a categorical flag rather than a noisy ToF
distance, so persistence buys nothing beyond riding over a single garbled
payload. There is no hysteresis band for the same reason; the latch releases
when the unit reports a clear value and on nothing else. A field the payload
omits entirely holds the latch rather than releasing it — silence is not
recovery. An unrecognised fault code alarms rather than being guessed clear.

Replayed against the 22,634 state samples retained on 2026-08-14, this sends
exactly one mail across the whole record: the `FAULT_TIMEOUT` that latched at
14:44 and sat unreported for four hours behind a blue light and a healthy
assessment.

**It detects only.** The flag clears at the *start* of the next cycle rather
than on a successful one, so a recovery that dispatched a cycle and then read
the flag would report success unconditionally. See
[docs/cat-detect-investigation.md](docs/cat-detect-investigation.md) for the
measurement and what a real recovery would have to judge instead.

Clear an escalation once you have dealt with the unit:

```bash
aws dynamodb delete-item --table-name lr4-watchdog --region us-west-2 --key '{"robot_id":{"S":"<pseudonym>"},"recorded_at":{"S":"STATE"}}'
```

### Repository configuration

This repository is public, so no AWS account ID, role ARN, state-bucket name, or
Identity Center user name is committed. None of those are credentials, but an
account ID beside a role name is targeting surface, and a portal URL beside a
valid user name is most of an SSO consent-phishing setup. They are supplied at
deploy time instead.

The deploy workflow needs these set on the repository (or inherited from the
organization):

| Name | Kind | Value |
|---|---|---|
| `AWS_DEPLOY_ROLE_ARN` | secret | `arn:aws:iam::<ACCOUNT_ID>:role/lr4-github-deploy` |
| `TF_STATE_BUCKET` | secret | `lr4-watchdog-tofu-state-<ACCOUNT_ID>` |
| `TF_VAR_WHISKER_SECRET_ARN` | secret | Secrets Manager ARN of the Whisker login |
| `TF_VAR_ALARM_EMAIL` | secret | Address to notify; unset leaves the topic unsubscribed |
| `TF_LOCK_TABLE` | variable | Optional. Defaults to `lr4-watchdog-tofu-lock` |
| `AWS_REGION` | variable | Optional. Defaults to `us-west-2` |
| `WATCHDOG_ARMED` | variable | `true` or `false`. A variable, not a secret, on purpose |

Two of those are needed because a `backend "s3"` block cannot reference
variables — an OpenTofu limitation, not a style choice — so the bucket and lock
table are passed to `tofu init -backend-config=` instead. Applying by hand takes
the same two:

```bash
cd infra
export TF_STATE_BUCKET=lr4-watchdog-tofu-state-<ACCOUNT_ID>
export TF_VAR_tf_state_bucket="$TF_STATE_BUCKET"
export TF_VAR_github_repository=<OWNER>/<REPO>
tofu init \
  -backend-config="bucket=$TF_STATE_BUCKET" \
  -backend-config="dynamodb_table=${TF_LOCK_TABLE:-lr4-watchdog-tofu-lock}"
tofu apply
```

`github_repository` pins the deploy role's OIDC trust policy to one repository.
CI passes `github.repository` from context, so a rename or transfer updates it
on the next deploy rather than silently leaving the role trusting the old path.
**Moving this repository means the role's trust policy has to be re-applied
before the first deploy from the new location will authenticate.**

## Analyze

> Findings from the ongoing investigation on this unit — two distinct failure
> modes, field semantics, what has been ruled out, and parts/warranty notes —
> live in [docs/cat-detect-investigation.md](docs/cat-detect-investigation.md).
> The current causal theory, its supporting and contrary evidence, and the
> tests that would falsify it are in [docs/hypothesis.md](docs/hypothesis.md).

Reconstruct clean cycles and attribute false cat-detect aborts to a subsystem:

```bash
uv run lr4-diagnostics analyze --database logs/lr4-diagnostics.sqlite
```

Add `--json` for machine-readable output. On first run the command backfills
`sensor_samples` from already-recorded events.

The LR4 reports two different "cat detected" signals, and separating them is
what makes the fault legible:

| Activity marker | Meaning | Driven by |
|---|---|---|
| `robotStatusCatDetect` | Cat detected while idle; starts the wait timer | Scale (load cells) |
| `robotCycleStateCatDetect` | Cycle aborted mid-rotation | ToF laser curtain |

A visit that never starts a cycle is a third signal: the at-rest detection has
latched, and `analyze` reports those separately as stalled visits.

The globe is moving during a cycle, so the scale cannot be used as the
mid-cycle interlock — that job belongs to the three-sensor laser curtain on the
laser board. This gives a clean discriminator:

- A **real cat** re-entering produces aborts scattered across the cycle.
- A **failing laser curtain** produces aborts clustered at the same offset from
  dump start, because the beam breaks at a fixed globe rotation angle.

`analyze` reports that offset distribution, then cross-checks each sensor:

- **Middle ToF** — `litterLevel` is the millimetre distance to the top-centre
  ToF sensor, so it works as a live readout of the centre curtain sensor even
  while Whisker withholds `ToFSensorDistanceMiddle`. A steady value clears the
  centre sensor and points at the left/right sensors.
- **Scale** — reported as unassessable when `weightSensor` never varies. On the
  unit studied here it holds one value even while `robotStatus` passes through
  cat-detect, making it a tare constant rather than a live load reading, so it
  can neither incriminate nor clear the scale.
- **Litter depth** — graded against the firmware scale (~441 full, ~451
  nominal, ~461 low), since overfilled litter can break the laser plane on its
  own.
- **DFI drawer ToF** — a second optical sensor; noise on both suggests a shared
  cause such as litter dust.

Remove locally stored Whisker credentials:

```bash
uv run lr4-diagnostics auth clear --username you@example.com
```

## Database

The `events` table stores:

| Column | Meaning |
|---|---|
| `observed_at` | When this recorder saw the record |
| `source` | Evidence stream such as `state`, `activity`, `diagnostics`, `pet_weight`, `history`, `firmware`, `summary`, `insights`, `lifecycle`, or `api_warning` |
| `robot_id` | Stable pseudonymous LR4 identifier |
| `source_timestamp` | Device/cloud timestamp when available |
| `payload_json` | Redacted complete payload |
| `payload_hash` | Used to suppress exact duplicate records |

The `sensor_samples` table stores a flat time series of ToF, scale, and cycle
scalars pulled from `state` and `diagnostics` payloads. Unlike `events`, it is
deliberately **not** deduplicated: an unchanged reading at a later time is
itself evidence, and the `events` dedupe index would discard it.

SQLite is opened in WAL mode so a long-running capture can be inspected safely
from another process.

## Known limitations

- `getUnitDiagnosticsBySerial` is present in Whisker’s GraphQL schema, but some
  accounts or devices may return no record unless Whisker has enabled its hidden
  debug/operations-audit mode. The recorder reports this without failing the
  rest of the capture.
- The activity subscription is an unofficial AppSync interface and may change.
  If it disconnects or Whisker rejects it, the recorder continues using the
  periodic raw-activity query and reconnects with backoff.
- SmartWeight history is useful corroboration, not proof that no cat was
  present: Whisker can omit, delay, or decline to assign a partial visit.
- `historyDownload`, summaries, and insights are curated and can lag behind the
  raw activity stream. Their timestamps are retained so the analysis does not
  mistake them for live observations.
- Cloud state can reveal which subsystem interrupted a cycle, but it is not a
  replacement for the ESP32 serial console when raw PIC register traffic is
  required.
- `ToFSensorDistanceLeft`/`Middle`/`Right` are withheld by Whisker's
  field-level GraphQL authorization on at least some owner accounts. Per-sensor
  attribution therefore stays indirect, inferred from `litterLevel` and abort
  timing rather than read directly. `analyze` says so explicitly rather than
  implying more certainty than the data supports.
- This tool currently supports LR4 only.

## Development

```bash
make check
```

The project retains the Python template’s Ruff, Pyright, Pytest, pre-commit,
Dependabot, and centralized CI setup.

## License

MIT — see [LICENSE](LICENSE).
