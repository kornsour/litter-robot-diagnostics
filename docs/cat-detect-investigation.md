# LR4 false cat-detect investigation

Running record of an investigation into a Litter-Robot 4 (in service since
2023-03-16) that repeatedly refuses to clean, reporting a cat that is not there.

**Status as of 2026-07-28: unresolved.** Scale recalibration was tried on
07-27 and is awaiting enough post-intervention cycles to judge.

The current causal theory is stated separately in
[hypothesis.md](hypothesis.md): progressive load-cell drift in the base.

Everything below is derived from `logs/lr4-diagnostics.sqlite` and reproducible
with:

```bash
uv run lr4-diagnostics analyze
```

---

## Unit under test

| | |
|---|---|
| Model | `LR4-0301-00-US` |
| Power input | 15V ⎓ 20000mA |
| Serial prefix | `LR4C` (determines the laser board variant — **not** `LR4S`) |
| In service since | 2023-03-16 (`setupDateTime`) |
| Surface | Tile (`surfaceType`) |
| Cats | 2 — recorded `catWeight` values 15.32 lb and 10.81 lb |

Firmware and hardware revisions:

| Component | Version |
|---|---|
| ESP32 | `espFirmware` 1.1.84 |
| PIC (main board) | `picFirmwareVersion` 10500.3072.2.93 (`2904.C00.25D`, Production) |
| Laser board | `laserBoardFirmwareVersion` 5.0.2.1 (`5.0.21`, Production) |
| Main board hardware | `mbHardware` 10500, `mbBom` 3072, `mbRevision` 93 |
| Laser board hardware | `lbHardware` 5, `lbBom` 0, `lbRevision` 1 |

Only the serial *prefix* is recorded here. The full serial and the MAC address
are direct identifiers and are deliberately kept out of version control — see
[Identifiers and redaction](#identifiers-and-redaction).

---

## Two distinct failure modes

The Whisker app collapses both into "Cat Detected" / "Cycle Interrupted", which
is why they went unseparated for so long. They behave very differently.

| | Mid-cycle abort | Idle latch |
|---|---|---|
| Activity marker | `robotCycleStateCatDetect` | `robotStatusCatDetect` with no following cycle |
| pylitterbot status | `CAT_SENSOR_INTERRUPTED` | `CAT_SENSOR_TIMING` |
| Display code | `DC_CAT_DETECT` | `DC_CAT_DETECT_30M` |
| Robot status | `ROBOT_CLEAN` | `ROBOT_CAT_DETECT_DELAY` |
| When | During globe rotation | Sitting at home position |
| Duration | Seconds; retries and finishes | Hours; no cycle runs at all |
| Observed | 105 aborts across 59 cycles | 17 stalls across 60 cat visits |

### Mid-cycle aborts

Capture window 2026-07-20 to 2026-07-27 (7.6 days):

- 59 cycles, 105 aborts, only 19 cycles abort-free
- **37 of 39 aborted cycles trip within 60s of dump start**, median 22s
- Retries re-trip a median of 53s apart
- Median cycle 3.4m vs 2.2m for abort-free cycles

Aborts occur exclusively while `robotCycleStatus` is `CYCLE_DUMP`, never during
`CYCLE_LEVEL`, `CYCLE_DFI`, or `CYCLE_HOME`. Clustering at a fixed offset means
the trigger is locked to a globe **rotation angle**, not to elapsed time.

### Idle latches

The unit registers a cat, then never sees it leave, so the clean-cycle wait
timer never elapses. Stall durations ranged 0.8h to 8.2h, roughly 2.2/day.

**Every latch so far has self-resolved without intervention.** On 2026-07-27 a
7.3h latch cleared on its own with `odometerPowerCycles` unchanged at 52.
Self-recovery is the fault's normal lifecycle, *not* evidence of repair — the
cycle that ended that latch still aborted once at 30s.

---

## Decisive evidence that the detections are false

On 2026-07-26 at 22:44 and 22:50 UTC, two **manually triggered** cycles ran on an
**empty** box (the cats had been moved to a separate conventional litter box),
immediately after a full clean and bonnet reseat. Both aborted — at 13s, 32s, and
18s, matching the long-run median.

No cat present, no cat-triggered cycle, freshly cleaned. The aborts are false
positives, confirmed rather than inferred.

---

## Field semantics discovered

Non-obvious meanings, several of which were initially misread.

| Field | Meaning |
|---|---|
| `litterLevel` | Millimetre distance to the **top-centre ToF sensor**: ~441 full, ~451 nominal, ~461 low, ~471 very low. Live **only at rest** — frozen during a cycle (see below). |
| `weightSensor` | **A constant, not a live load reading.** See correction below. |
| `catWeight` | Last *recorded* pet weight, not current. Two cats here: 15.32 lb and 10.81 lb. |
| `bonnetRemovedYes` | Activity marker for a cleaning/reseating session; used to split before/after. |
| `odometerPowerCycles` | Increments on power cycle; a free intervention marker. |
| `ToFSensorDistance{Left,Middle,Right}` | Withheld by Whisker field-level GraphQL authorization. Always null. |
| `catDetectStuckLaser` | Maps to `CAT_SENSOR_FAULT`. Never observed on this unit. |

### `litterLevel` is frozen during a cycle

The firmware holds the last at-rest reading while the globe turns rather than
streaming the ToF distance; pylitterbot's `calculate_litter_level` mirrors this
(`if is_cleaning: ...` keeps the previous level). Raw captures confirm it —
`litterLevel` sat at exactly 453.0 through entire cycles including
`CYCLE_STATE_CAT_DETECT` samples, then moved only after the cycle ended.

So correlating `litterLevel` against abort moments proves nothing: the field was
not updating. `sensor_findings` now computes the middle-ToF spread from at-rest
samples only and labels the verdict `AT REST ONLY`.

### Correction: the scale was wrongly exonerated

An earlier conclusion held that a stable `weightSensor` cleared the scale. That
was wrong. `weightSensor` reads **2.70 across all 71 samples**, including three
where `robotStatus` was `ROBOT_CAT_DETECT` — a cat physically standing on it. A
field that does not move under load is a tare/calibration constant; its
stillness proves nothing either way. pylitterbot exposes no `weightSensor`
property, only `catWeight`, consistent with that reading.

`analyze` now reports `SCALE UNASSESSABLE`, and `_varies_with_status` prevents
any constant series from being reported as healthy.

This mattered: Whisker's own documentation attributes the >30-minute cat sensor
fault primarily to **extra weight seen by the scale** — the exact component the
retracted verdict had waved through.

---

## Hardware architecture

Whisker calls the LR4 detection system **OmniSense**:

- **Three laser "curtain" sensors** in the bezel, driven by the laser board
  (`laserBoardFirmwareVersion` 5.0.2.1, surfaced by pylitterbot as "TOF")
- **A weight scale** in the base

Other processors: ESP32 (`espFirmware` 1.1.84) and a PIC
(`picFirmwareVersion` 10500.3072.2.93). Cat-detect logic lives on the PIC and
laser board, not the ESP32.

The LR5 uses the **same architecture** — bezel curtain sensor plus SmartScale in
the base — with refinements (2 lb vs 3 lb activation, waste ID, camera). It has
the same documented false-detection failure mode, and pylitterbot's LR5 module
carries the same `isLaserDirty` and `CAT_SENSOR_INTERRUPTED` fields. Newer
hardware is **not** a structural fix for this class of problem.

---

## Ruled out

- **A real cat.** The empty-box manual test above.
- **Litter depth.** `litterLevel` 451-454mm is nominal. Do not add litter —
  overfilling moves toward the 441 "full" mark and can break the laser plane.
  Note this is a ToF distance and would not reveal scale drift.
- **Loose plastic in the globe.** A clear piece that came off the insert was
  discarded long ago, so it is not rattling around inside.
- **Connectivity.** WiFi −40 dBm, `wifiModeStatus` `ROUTER_CONNECTED`; MQTT
  lifecycle disconnects are routine.
- **Motor/mechanical faults.** `globeMotorFaultStatus`,
  `globeMotorRetractFaultStatus`, `pinchStatus`, `USBFaultStatus` all clear
  throughout.

## Not ruled out

- **Scale drift or miscalibration. Now the leading candidate for both modes.**
  Whisker's own light-code mapping identifies blue-with-partial-yellow-flashing
  as "the scale has been triggered for more than 30 minutes", which is the idle
  latch exactly. Load redistribution during early dump rotation could produce
  the aborts too. See "Light bar patterns" below.
- **Laser curtain, any of the three sensors.** The centre sensor reads steady
  at rest (451-454mm, stdev 1.0) via `litterLevel`, but that field is frozen
  mid-cycle, so the centre sensor is *not* cleared for the rotation window
  where the aborts happen.

---

## Interventions and outcomes

| Date (UTC) | Action | Outcome |
|---|---|---|
| before 2026-07-20 | Laser bezel wiped with dry cotton swab | No improvement |
| 2026-07-26 15:29-15:31 | Full wipe-down, bonnet/globe reseat (`bonnetRemovedYes` ×3) | Aborts continued; empty-box test still failed |
| 2026-07-27 ~18:30 | **Scale recalibration: double-press Reset from home** | Pending |
| 2026-08-14 14:44 | Scheduled watchdog recovery (`reset` + `cleanCycle`) on a `DC_CAT_DETECT_30M` latch, stuck 960s | Cycle completed (odometer 4207→4208) and the latch cleared, but the same cycle raised the first `globeMotorFaultStatus = FAULT_TIMEOUT` in the record, and `litterLevel` stepped 471→439mm |
| 2026-08-14 19:17 | Supervised one-shot `cleanCycle`, owner at the unit, to test whether a cycle clears the motor fault | Flag cleared 15s in, **during `CYCLE_DUMP`** — at cycle start, not on completion. Cycle finished normally (odometer 4209, home, no fault re-raised); `litterLevel` stayed at 440mm |

### The motor fault clears at cycle start, so clearing it proves nothing

Measured 2026-08-14 by a supervised one-shot `cleanCycle` with the owner at the
unit. `globeMotorFaultStatus` went `FAULT_TIMEOUT` → `FAULT_CLEAR` fifteen
seconds into the cycle, while still in `CYCLE_DUMP` — the firmware releases the
latch when a new cycle *begins*, not when one *succeeds*.

This is load-bearing for any future recovery automation. A watchdog that
dispatched a cycle and then read the flag would report success unconditionally,
including for a globe that was about to time out again — marking its own
homework, the same failure `--recovery-hold` exists to prevent for the idle
latch. Success has to be judged from whether a *later* cycle re-raises the
fault, and that hold cannot be sized until the fault has recurred on its own a
few times. `faults.py` therefore detects and alarms only; nothing dispatches on
this signal.

The 471→439mm step at 14:44 initially looked like a globe parked short of home,
with the top-centre ToF reading a different part of the bed. Two complete cycles
later it still reads ~440mm, so that reading does not hold: the bed geometry
genuinely changed during the faulted cycle and has persisted since. The timeout
stands on its own as the only hard evidence.

Whisker's documented recalibration is: from home position, **press Reset
twice**; if stuck mid-cycle, long-press Reset for 3 seconds.

### What the remote commands actually do

Measured 2026-07-30 with `lr4-diagnostics probe-reset` on this unit. This was
run because the automated recovery had been written on the assumption that a
Reset returns the globe home. It does not.

| Offset | Command / observation |
|---|---|
| +0s | `ROBOT_IDLE` / `CYCLE_IDLE`, globe home |
| +10.5s | `cleanCycle` dispatched → `CYCLE_DUMP`, globe moving |
| +46.3s | `shortResetPress` → **`DC_USER_PAUSE` / `CYCLE_STATE_PAUSE`** |
| +331s | `cleanCycle` dispatched again |
| +631s | still `DC_USER_PAUSE` / `CYCLE_STATE_PAUSE`, unchanged |

The owner then pressed **Reset** by hand at 15:41, and the unit recovered:

| Time (UTC) | robotStatus | displayCode | robotCycleStatus | odometerCleanCycles |
|---|---|---|---|---|
| 15:40:48 | `ROBOT_CLEAN` | `DC_USER_PAUSE` | `CYCLE_DUMP` | 4151 |
| 15:41:48 | `ROBOT_CLEAN` | `DC_MODE_CYCLE` | `CYCLE_LEVEL` | 4151 |
| 15:42:48 | `ROBOT_IDLE` | `DC_MODE_IDLE` | `CYCLE_IDLE` | 4151 |

The cycle **resumed** — `CYCLE_DUMP` → `CYCLE_LEVEL` → home — and
`odometerCleanCycles` did not move, so it continued the interrupted cycle
rather than starting a new one.

So `shortResetPress` is a **pause/resume toggle** mid-cycle:

| State | `shortResetPress` | `cleanCycle` |
|---|---|---|
| Idle at home | recalibrates (double-press) | starts a cycle |
| Cycle running | pauses it where it stands | — |
| Cycle paused | resumes it, runs out to home | **ignored entirely** |

The earlier reading of this probe — that `cleanCycle` being inert meant no
remote recovery existed — was wrong, and wrong in an expensive direction: it
tested the wrong command. `cleanCycle` is simply not what clears a pause;
Reset is. The correction came from the owner's hands-on knowledge, which had
already been right once before about Reset not homing the globe.

Consequence: recovering a stalled cycle can need **two** Reset presses, one to
break the stall into a pause and one to resume it, and the watchdog must
observe between presses rather than firing a fixed sequence — a third press
would re-pause the cycle the second had just resumed. `next_recovery_command`
in `autoreset.py` holds that decision table.

Still unverified: what a Reset does to a *genuinely stalled* cycle rather than
a healthy one, and whether the API `shortResetPress` resumes a pause the way
the physical button does. Only the button has been observed doing it.

### Automated recovery exists for the latch, and truncates the evidence

`lr4-diagnostics autoreset` (see the README) can clear the idle latch
unattended: short Reset press, then Cycle press. It is off by default and sends
nothing without `--arm`.

While it is armed, **latch durations stop being a natural measurement.** The
33%-of-captured-hours figure and the 0.8h-8.2h range above were recorded with no
automated intervention; they should not be recomputed across an armed window
without excluding it. Every assessment and attempt lands in the same database
under source `intervention`, so armed windows are identifiable:

```sql
SELECT observed_at, json_extract(payload_json, '$.kind'),
       json_extract(payload_json, '$.assessment.reason')
FROM events WHERE source = 'intervention' ORDER BY id;
```

Recovery attempts also bump `odometerCleanCycles`, so cycle counts and the
detections-per-week figures in the weekly summaries inflate under an armed
watchdog. The `maxWeight` drift signal is unaffected.

`compare_around_maintenance` deliberately withholds a verdict until 5 cycles
have run on each side. Even then, treat small samples with suspicion: a 3-cycle
clean streak after the reseat looked like a fix, but the full 52-cycle
pre-maintenance baseline contains its own 3-streak, and with P(clean)=0.29 one
is expected roughly every 50 cycles.

---

## Parts, warranty, economics

Unit is ~3.4 years old. LR4 ships with a 1-year WhiskerCare warranty, extendable
to 3 years maximum, so it is **out of warranty**. Whisker does not offer
out-of-warranty repair but does sell parts.

| Part | Price | Relevance |
|---|---|---|
| Laser board | from $50 | Drives the three bezel curtain sensors |
| Bezel | from $50 | Houses the curtain sensors |
| Base | from $449 | **Houses the scale** |
| Cat presence sensor | $15 | Function unverified |

The laser board is serial-dependent. This unit's serial starts with **`LR4C`**,
so select the **LR4C** variant from the dropdown at purchase, not LR4S.

Economics matter here: if the fault is the **laser board**, a $50 part is an
easy call. If it is the **scale**, the base is $449, close enough to a new unit
that replacement or an LR5 becomes competitive. Establishing which one it is,
before spending, is the point of the recalibration test.

Links: [parts index](https://www.litter-robot.com/litter-robot/parts.html) ·
[laser board selector](https://www.litter-robot.com/litter-robot-4-laser-boards.html) ·
[base](https://www.litter-robot.com/litter-robot-4-base.html) ·
[LR4 cat sensor fault](https://www.litter-robot.com/support/article/litter-robot-4-flashing-red-cat-sensor-fault/) ·
[laser board install guide](https://www.litter-robot.com/support/article/litter-robot-4-laser-board-installation-guide/)

### On custom firmware

Considered and not recommended. Firmware cannot make a faulty sensor report
correct values; it could only ignore them, and that signal is the interlock that
stops the globe rotating with a cat inside. The mature open-source work
(`litter-eater`, OpenLitter, elttam's teardown) targets the **LR3** and replaces
or reflashes the ESP32, whereas LR4 cat-detect runs on the PIC and laser board.
It also forecloses the cheaper parts path.

---

## Light bar patterns

`displayCode` maps onto the front light bar, which is what Whisker support asks
about. Codes observed on this unit:

| `displayCode` | Light bar | Meaning |
|---|---|---|
| `DC_MODE_IDLE` | Solid blue | Normal idle |
| `DC_MODE_CYCLE` | Blue, cycling | Clean cycle running |
| `DC_CAT_DETECT` | Blue, cat-detect indication | Cat present |
| `DC_CAT_DETECT_30M` | **Blue with partial yellow flashing** | **Scale triggered >30 min** |
| `DC_BONNET_OFF` | Yellow flashing | Bonnet removed (seen during maintenance) |
| `DC_USER_PAUSE` | — | Cycle paused |
| `DCX_LAMP_TEST`, `DCX_REFRESH` | — | Power-up self-test / refresh |

### The blue + partial yellow pattern points at the scale

Whisker's support article for blue-with-partial-yellow-flashing is titled
*"excess weight detected"* and states the pattern means **the scale has been
triggered for more than 30 minutes**. It attributes the condition to the weight
scale sensors specifically, *not* the laser sensor, and its escalation path ends
at **replace the base**.

This is the same condition as `DC_CAT_DETECT_30M` and `ROBOT_CAT_DETECT_DELAY`,
so the idle latch is — by Whisker's own diagnostic mapping — a scale fault.

**Time spent in this state: 59.3 of 182.4 captured hours, or 33%.** 17 episodes,
mean 3.5h. Derived from the continuous activity log rather than the state
snapshots, which are too sparse and dedupe-biased to estimate frequency.

### One cause may explain both failure modes

A scale reading persistent excess weight would latch at rest *and* trip during
the dump, when the globe tips and load redistributes across the load cells —
which would be rotation-angle-locked in exactly the way the abort offsets show.
That is a single-fault explanation for both symptoms, and it displaces the
earlier assumption that the two modes needed two separate causes.

Economically this matters: the scale lives in the **base ($449)**, not the
**laser board ($50)**.

## Scale drift is measurable in the weekly summaries

`getLitterRobot4Summary` records a max and min recorded weight per week. The two
cats are stable at roughly **11.5 lb** and **8 lb** — `minWeight` barely moves.
`maxWeight` does not:

| Week | maxWeight | minWeight | detections | cycles |
|---|---|---|---|---|
| 2026-07-01 .. 07-07 | 11.50 | 8.09 | 23 | 23 |
| 2026-07-08 .. 07-14 | 11.61 | 8.09 | 11 | 11 |
| 2026-07-15 .. 07-21 | **19.48** | 7.87 | 24 | 23 |
| 2026-07-22 .. 07-28 | **22.65** | 7.63 | 48 | 46 |

SmartWeight never attributed anything above **11.53 lb** to a pet profile, and
both cats on the scale simultaneously would cap at about **19.49 lb**. The
22.65 lb week therefore exceeds any physically possible load, and the escalating
trend with a flat `minWeight` is the signature of a scale reading progressively
high rather than of heavier cats.

Caveat: 19.48 is within a whisker of the both-cats-at-once ceiling, so that week
alone is not conclusive. 22.65 is not explainable that way.

Detections and cycles also roughly double in the final week (48/46 against a
23-ish baseline), consistent with false triggers driving retries.

### `CatDetectStuckWeight`

The curated `historyDownload` stream carries an event the short activity window
does not: **`CatDetectStuckWeight`** at 2026-06-30T20:38:15Z. Whisker's own
firmware names the condition, and it is the scale.

It is preceded at 20:07:36 by a `catWeight` reading of **4.9 lb** — far below
either cat. A partial load registering and never clearing, then the stuck-weight
event 31 minutes later, matches the 30-minute threshold exactly.

Spurious readings appear in both directions: 4.9 lb (2026-06-30) and 15.32 lb
(2026-07-25, unassigned to any pet).

### Firmware is already current

`litterRobot4CompareFirmwareVersion` reports no update available for any
component — ESP 1.1.84, PIC 10500.3072.2.93, laser board 5.0.2.1 all match the
latest offered. "Update the firmware" is not an available remedy.

## Identifiers and redaction

Model numbers, power ratings, firmware versions, and hardware revisions are
identical across every unit of a SKU. They carry no personal information and are
recorded above because they are genuinely useful for diagnosis and for ordering
the right part.

Full serial numbers and MAC addresses are different: they identify one specific
unit and link it to an owner account. They are kept out of version control.

- `CLAUDE.md` already forbids clear device serials, and `redact.py` hashes every
  serial to `lr4-<12 hex>` before it reaches the database.
- The pseudonym is `sha256(serial)[:12]`, so publishing the clear serial
  alongside a redacted capture would let anyone confirm the hash and undo the
  redaction. That matters because redacted database snapshots get backed up to
  cloud storage.
- Manufacturer support lines often treat a serial as weak proof of ownership for
  warranty and parts claims.
- The MAC address appears in **no** captured payload — Whisker's API does not
  expose it and nothing in the analysis uses it, so recording it would add a
  permanent identifier for no analytical gain.

Only the serial prefix (`LR4S` / `LR4C`) is recorded, because that is the entire
operational requirement: it selects the laser board variant.

Keep the full serial and MAC in `logs/device-info.local.md`. The `logs/`
directory is already gitignored, and `*.local.md` is ignored as a second
safeguard.

Note that git history is effectively permanent — removing a committed identifier
means rewriting history and force-pushing. Keeping it out is far cheaper than
taking it back out, and private repositories can later be made public or forked.

## Method notes for future sessions

- **Capture at least weekly.** Whisker serves roughly a 7-day activity window,
  capped by row count. The old `limit=100` default reached back only ~26 hours;
  `--history-limit` now defaults to 500 (the server holds ~417 rows).
- **`events` is deduplicated, `sensor_samples` is not.** An unchanged reading at
  a later time is evidence, so sensor readings bypass the dedupe index. This is
  also how you tell "robot silent" from "robot reporting unchanged values".
- **Ongoing stalls need a later reference than the activity log.** A stalled
  visit is the newest activity row by definition, so it must be measured against
  a sensor sample from this recorder's clock or it computes as zero.
- **Distrust constant series.** A field that never varies is probably not being
  measured. That mistake cost several days of chasing the wrong component, and
  `litterLevel` mid-cycle turned out to be the same trap a second time. Before
  reading meaning into stability, confirm the field actually moves when the
  thing it measures moves.
