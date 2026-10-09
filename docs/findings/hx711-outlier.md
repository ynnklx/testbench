# Finding: single corrupted HX711 values

Sweep run from 2026-09-03, 12:47:28 (V1 software, firmware 0.1.0).
Settings at that time: start 15 %, 2.5 % steps, 620 ms settle, 2.5 s hold.
Calibration: 5-point fit from 2026-09-02, session zero point +21.9 g.

## What was seen

In one run, the thrust standard deviation (SD) jumped at two stages:

| Stage | n | Thrust median | Thrust SD |
|---|---|---|---|
| 65.0 % | 29 | 357.2 g | 0.6 g |
| **67.5 %** | 29 | 378.6 g | **49.0 g** |
| **70.0 %** | 29 | 419.6 g | **48.8 g** |
| 72.5 % | 29 | 415.7 g | 0.8 g |

All other 24 stages of the run had an SD below 1 g.

## Cause

Each of the two stages contains exactly **one** wrong sample out of 29 in the
hold phase. The samples right before and after are normal. There is no ramp
and no oscillation, so it is not a physical event.

| Stage | seq | Time in hold | Raw value | Neighbours | Thrust |
|---|---|---|---|---|---|
| 67.5 % | 970 | 616 ms | −810 282 | −547 569 / −548 623 | 642.7 g instead of ~379 g |
| 70.0 % | 1023 | 2113 ms | −849 220 | −587 529 / −586 857 | 681.9 g instead of ~418 g |

Both jumps are about 262 000 counts, which is very close to 2¹⁸ (262 144).
This fits one flipped bit when the 24-bit value is read from the HX711, not a
real force.

## Effect on the result

| Stage | Median with outlier (n=29) | Median without outlier (n=28) | Difference |
|---|---|---|---|
| 67.5 % | 378.6 g | 378.6 g | −0.1 g |
| 70.0 % | 419.6 g | 419.4 g | −0.2 g |

The median moves by at most 0.2 g. The arithmetic mean would move by about 9 g.

## Consequences for the software

- The measurement value per window is the **median**, not the mean
  (see [ARCHITECTURE.md](../ARCHITECTURE.md), "Combined value").
- The SD is still calculated from the mean on purpose. It should make such
  outliers visible, not hide them.
- In V2, single outliers are counted with a MAD rule
  (`unstable_measurement` in `config/validation.json`). They are marked, never
  removed (decision 5).
