# Current hypothesis: progressive load-cell drift in the base

**Stated 2026-07-28. Confidence: high as of 2026-08-28** — the predicted
decay-and-relapse of the 07-27 re-zero has now been observed (see
"Decay-and-relapse confirmed" below), which is the first case in this
investigation of a *prediction* being checked against new data rather than
supporting evidence assembled after the fact.

For the underlying evidence and methodology see
[cat-detect-investigation.md](cat-detect-investigation.md). This document states
the current causal theory, what supports it, what does not, and what would
falsify it.

A caution that belongs at the top: the scale has been wrongly cleared **twice**
in this investigation, once on a constant `weightSensor` and once on a
mid-cycle-frozen `litterLevel`. Both were cases of reading a held value as a
healthy signal. That history argues for stating this hypothesis with explicit
confidence rather than as settled fact.

---

## The hypothesis

**The weight scale in the base has a progressive fault — drifting load cells, or
their amplifier/tare reference — that produces spurious load readings. One fault
produces both observed symptoms.**

### Mechanism, idle latch

The LR4 detects cat entry and exit by weight. A phantom load above the
cat-detect threshold makes the unit believe a cat is present. It refuses to
cycle, since rotating the globe with a cat inside is exactly what the interlock
exists to prevent.

After 30 minutes the firmware raises the cat-sensor fault and the light bar goes
blue with partial yellow flashing — Whisker's documented "scale has been
triggered for more than 30 minutes".

Drift is not a fixed offset, so the phantom load eventually wanders back below
threshold and the latch clears on its own. That accounts for the observed
0.8h–8.2h durations and for every latch self-resolving without intervention.

### Mechanism, mid-cycle abort

As the globe rotates from home into the dump position, its mass redistributes
across the load cells. A drifting or noisy cell turns that redistribution into a
transient excursion large enough to read as a cat entering, which aborts the
cycle.

Because redistribution is a function of **globe angle**, the trip lands at the
same point every time. That matches the tight abort clustering: 37 of 39 aborted
cycles trip within 60s of dump start, median 22s.

This half of the mechanism is **less well established** than the idle latch. See
"What this does not explain" below.

---

## Supporting evidence

Ordered by strength.

1. **Whisker's firmware names the condition.** `historyDownload` contains
   `CatDetectStuckWeight` at 2026-06-30T20:38:15Z. Not an inference — the
   manufacturer's own event name identifies stuck *weight*.

2. **Recorded max weight escalates past physical possibility — and now relapses
   right on schedule with the predicted decay.**

   | Week | maxWeight | minWeight | Note |
   |---|---|---|---|
   | 07-01 .. 07-07 | 11.50 | 8.09 | baseline |
   | 07-08 .. 07-14 | 11.61 | 8.09 | baseline |
   | 07-15 .. 07-21 | 19.48 | 7.87 | drift emerging |
   | 07-22 .. 07-28 | **22.65** | 7.63 | drift, pre-re-zero |
   | 08-01 .. 08-07 | 11.86 | 7.42 | post-re-zero, normal |
   | 08-08 .. 08-14 | 12.02 | 5.70 | post-re-zero, normal |
   | 08-15 .. 08-21 | 12.38 | 5.76 | post-re-zero, normal |
   | 08-22 .. 08-28 | **21.02** | 7.57 | **drift relapsed** |

   SmartWeight has never attributed more than 11.53 lb to a pet profile, and
   both cats at once caps near 19.49 lb. Both the 07-22 .. 07-28 and 08-22 ..
   08-28 weeks exceed any load the household can produce. A climbing maximum
   against a flat minimum is drift, not heavier cats. Note `minWeight` also
   dipped to 5.70-5.76 lb through the normal weeks, below the 7.4-8.1 lb range
   seen elsewhere — plausibly the same drift wandering negative rather than a
   third signal, but not otherwise explained.

3. **Individual readings fall outside both cats, in both directions.** 4.9 lb on
   2026-06-30 (31 minutes before the stuck-weight event, matching the 30-minute
   threshold) and 15.32 lb on 2026-07-25, which SmartWeight never assigned to
   either pet.

4. **The light bar agrees.** Blue with partial yellow flashing is documented by
   Whisker as the scale being triggered over 30 minutes, attributed to the
   weight sensors *specifically and not the laser sensor*, with an escalation
   path ending at replace the base. The unit spent 33% of a 7.6-day window in
   that state.

5. **Optical remedies produced no durable change.** Bezel cleaned with a swab,
   then a full wipe-down with bonnet and globe reseated. Manually triggered
   cycles on an empty box still aborted at 13s, 32s, and 18s afterward.

6. **Laser-specific fault codes never fired.** `catDetectStuckLaser` — which
   pylitterbot maps to `CAT_SENSOR_FAULT` — has never appeared, and
   `isLaserDirty` has never been set, across the entire capture.

### Degradation timeline

| Date | Event |
|---|---|
| 2026-06-30 | First known `CatDetectStuckWeight`; isolated |
| 07-01 .. 07-14 | Weekly maxWeight normal (11.50, 11.61) |
| 07-15 onward | maxWeight anomalous (19.48, then 22.65) |
| 07-20 onward | Detailed capture: 17 idle latches, 105 aborts across 59 cycles |
| 07-27 ~18:30 | Scale recalibration: double-press Reset from home |
| 08-01 .. 08-21 | Weekly maxWeight normal again (11.86, 12.02, 12.38) |
| 08-22 .. 08-28 | **maxWeight relapses to 21.02**; worst single cycle of the whole capture (8 aborts) lands 08-28 07:09, inside the relapsed week |

Consistent with a component degrading over weeks rather than failing outright,
and now with a re-zero whose relief measurably decays rather than holding.

---

## Decay-and-relapse confirmed (2026-08-28)

[hypothesis.md's own prediction #2](#predictions), stated 2026-07-28, was:

> The Reset re-zero of 2026-07-27 gives temporary relief that decays within
> days, because re-taring corrects an offset but not ongoing drift.

A capture gap means the actual data cannot distinguish "days" from "weeks" —
nothing was recorded between 07-30 and 08-21 — but the shape is exactly what
was predicted: three clean weeks (08-01 .. 08-21, maxWeight 11.86-12.38, in
the same range as the pre-drift baseline) followed by a relapse to 21.02 lb
in the week of 08-22 .. 08-28, landing in the same physically-impossible
territory as the pre-re-zero peak (22.65). The relapsed week also produced
the single worst cycle in the entire capture — 8 aborts in one cycle, on
08-28 at 07:09 — consistent with abort severity tracking weight-drift
magnitude (prediction #4).

This is the first time a *stated prediction* has been checked against data
collected after the fact, rather than a piece of evidence assembled in
support of the hypothesis after the fact. It does not distinguish this
hypothesis from "a mechanical tare/offset that re-drifts over time" in
general — it does not, on its own, rule out the competing "mechanical or
placement" explanation (§ below) — but a one-time miscalibration that simply
needed correcting would not be expected to relapse on its own weeks later.
Confidence is raised accordingly, from moderate-to-high to high.

**Practical corollary**: the re-zero is a real but temporary mitigation, on
roughly a 3-4 week relief cycle so far (one data point; not yet enough to
call it a fixed interval). See "Practical consequence" below for what that
implies for repeating it versus replacing the base.

## What this does not explain

Recorded so the hypothesis is not overstated.

- **Abort clustering is tighter than the mechanism obviously predicts.** Whether
  load redistribution during rotation is repeatable enough to trip at a median
  22s with that consistency is plausible but unverified. A fixed optical
  obstruction would produce the same signature more naturally.
- **The live load is unreadable.** `weightSensor` is a tare constant and the
  per-sensor ToF distances are withheld by GraphQL authorization, so the
  hypothesis rests on derived quantities (weekly aggregates, firmware events)
  rather than direct observation of the failing signal.
- **The 19.48 lb week is not conclusive on its own** — it sits just under the
  both-cats-at-once ceiling. Only 22.65 lb is unambiguous.
- **No `CatDetectStuckWeight` inside the detailed capture window.** The one
  instance predates it. Its absence since may be a retention artifact of the
  `historyDownload` window rather than meaningful.

---

## Competing hypotheses

**Laser curtain fault (left or right sensor).** Explains rotation-locked aborts
more naturally than load redistribution does. Does *not* explain the stuck-weight
event, the maxWeight escalation, or the light-bar code. Would be expected to set
`isLaserDirty` or `catDetectStuckLaser` eventually; neither has occurred.
**Downgraded to secondary**, but not eliminated — it remains the better
explanation for the aborts specifically.

**Both.** A drifting scale causing the latches and an independent optical fault
causing the aborts. Less parsimonious, but two symptoms with different
signatures do not have to share a cause. Cannot currently be excluded.

**Mechanical or placement.** Whisker attributes excess-weight faults partly to
unstable surfaces, debris in the feet, or overfilled litter. Litter is nominal
(451-454mm against a 441 "full" mark), the unit is on tile, and cleaning did not
help — but the feet have not been explicitly inspected.

---

## Predictions

If the hypothesis holds:

1. Weekly `maxWeight` keeps climbing beyond 22.65 lb. **Not confirmed as
   stated** — it reset to baseline after the 07-27 re-zero rather than
   continuing to climb, then relapsed to 21.02 (see prediction 2). Climbing
   without bound was the wrong shape; decay-then-relapse was the right one.
2. **CONFIRMED 2026-08-28.** The Reset re-zero of 2026-07-27 gives temporary
   relief that decays, because re-taring corrects an offset but not ongoing
   drift. Observed: relief held for three full weeks (08-01 .. 08-21,
   maxWeight 11.86-12.38) before relapsing to 21.02 in the week of 08-22 ..
   08-28. The capture gap 07-30..08-21 means the true decay window could be
   anywhere from days to ~3 weeks — the original "within days" guess was
   likely too fast, but the decay-then-relapse shape was right. See
   "Decay-and-relapse confirmed" above.
3. A deliberate weight-step test reproduces the latch on demand: place a known
   ~5 lb load, remove it, and the unit fails to release. **Still not run.**
4. Latch frequency tracks drift magnitude, so both worsen together. **Weakly
   consistent**: the relapsed week (08-22 .. 08-28) produced the single worst
   cycle in the whole capture (8 aborts), though this tracks abort severity
   more directly than latch frequency, which was not separately recomputed
   for this window.

If instead the aborts are optical, replacing the laser board clears the aborts
while the latches continue unchanged.

---

## What would change the conclusion

- Weekly `maxWeight` returning to ~11.5 lb and staying there → drift resolved or
  never real.
- A weight-step test where load applies and releases cleanly → scale responding
  correctly; suspicion returns to the laser.
- `catDetectStuckLaser` or `isLaserDirty` appearing → optical fault confirmed as
  at least a contributor.
- Latches stopping after the Reset re-zero and staying stopped → simple tare
  error rather than progressive drift. **Already decided against**: relief
  held for ~3 weeks then relapsed (08-22 .. 08-28, maxWeight 21.02), so this
  reads as progressive drift, not a one-time tare error.

---

## Practical consequence

The scale is in the **base**, a ~$449 part. The laser board is ~$50. Given the
cost gap, establishing which component is at fault before purchasing matters
more than usual, and the free weight-step test is the highest-value next action.

The re-zero (double-press Reset from home) is a free, ~10-second action that
now has one confirmed data point: it holds for roughly 3-4 weeks before the
drift relapses. Until the base is replaced or the weight-step test points
elsewhere, repeating the re-zero on that rough cadence is a reasonable
stopgap — it is not a fix and the relapse pattern is expected to continue.

The unit is roughly 3.4 years old and out of warranty (1 year standard,
extendable to 3), and Whisker does not offer out-of-warranty repair, so the
realistic options are a purchased part or replacement.
