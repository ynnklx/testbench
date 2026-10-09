# Development log

The most important milestones of the project. The full working log (in German)
is kept outside of this repository. Design decisions and known pitfalls are
explained in [ARCHITECTURE.md](ARCHITECTURE.md).

## Phase 1 – Hardware and firmware (August 2026)

**2026-08-02 – First firmware, own HX711 driver.**
ESP32 firmware with own bit-bang HX711 driver: clock pulses in a critical
section, timeout on DRDY, timestamp at the DRDY edge. First serial protocol
(`ID?`, `PING`, `START`, `STOP`, `TARE`).
A suspected boot loop was in the end caused by the test method (two processes
opening the serial port at the same time), not by firmware or hardware.

**2026-08-03 – Motor, ESC and INA226.**
Motor mounted on the load cell, ESC control with servo PWM, INA226 driver for
current and voltage (raw registers only, conversion on the host).
Found and fixed: a display redraw on every sample blocked the main loop and
corrupted serial answers. Arm button and status LED added, with a correct
stability-window debounce.

**2026-08-03 – Current drop at higher throttle.** The supply voltage dropped
under load. Cause was a "smoke stopper" (bulb in series as protection) in the
supply line, not the measurement chain. Removed, and the drop was gone.

**2026-08-17 – Self-contained test cycle.**
The device runs the throttle stages by itself after the arm button
(countdown, settle phase, hold phase). The host only records. First series
with three propellers, and a re-mount test to check reproducibility.

**2026-08-18/19 – ESC telemetry and fixed ESC configuration.**
RPM and ESC temperature via the ESC telemetry line. The current value from the
ESC telemetry was found to be wrong by a factor of ~3750 and is not used.
Every automatic or adaptive ESC setting was switched off in favour of fixed
values. Reason: determinism is more important than optimisation.

## Phase 2 – Measurement method and validation (September 2026)

**2026-09-02 – Real calibration.**
`calibrate.py` with a least-squares fit over 5 reference weights and residuals
per point. A first attempt showed 14 % residual at one point (weight not placed
cleanly). The repeated calibration has residuals of 0.6–2.5 %.

**2026-09-02 – Spread caused by recirculation.**
The spread grew strongly with throttle. Test with and without a board in the
air flow: with the board the spread was 5–10× higher. Cause was air
recirculation, not settling time. Rule: the air flow area must stay free.

**2026-09-03 – Session zero point.**
The zero point from the calibration file moves with mounting and storage
(+29 g observed). Fix: zero point per run from the middle second of the
countdown. The slope from the calibration stays.

**2026-09-05 – Up/down sweep against temperature drift.**
A long static run showed a real drift of thrust and current with temperature.
The sequence was changed to go up to full throttle and down again. Every stage
is measured twice, so drift becomes visible and can be compensated.
An acoustic RPM check showed that the ESC telemetry RPM under-reads the real
change by a factor of ~3. RPM is therefore used only as diagnostic value.

**2026-09-07/08 – Validation campaign V1–V6.**

| Test | Topic | Result |
|---|---|---|
| V1 | Load cell calibration | 5 points, residuals 0.6–2.5 % |
| V2 | Zero drift, 33.5 min at rest | ~0.5 g over full time, < 0.15 g in a typical 5 min run |
| V3 | Interference (load cell mechanically decoupled, motor running) | 0.0 g on all stages |
| V4 | INA226 vs. multimeter | voltage < 0.3 %, current 1.5–1.7 % |
| V5 | RPM plausibility | pole pair count confirmed acoustically |
| V6a | Settle time per stage | low stages need up to 924 ms → settle time 1000 ms |
| V6 | Repeatability (16 V: 5 runs, 25 V: 4 runs) | SD < 1.2 % for thrust, current, RPM |

**2026-09-11 – Active ESC cooling and 10 A power supply.**
V6 repeated with active cooling: the thrust drift fell from 0.20 %/K to
0.024 %/K (factor ~8). Maximum ESC temperature fell from 65–72 °C to 51–53 °C.

## Phase 3 – Software V2 (September 2026)

**2026-09-11 – V1 frozen, V2 started.**
V1 worked, but the processing chain had grown without a plan: modules imported
each other via `sys.path`, the same constants were copied in up to seven
places, and there was no clear idea of a "measurement point". V2 is a new
structure in four stages (acquisition, analysis, comparison, plots), each with
one input and one output. V1 stays unchanged as fallback.

Steps of the rebuild:

1. `core/` and `acquisition/`: file names, CSV headers, units, calibration,
   serial link with simulation.
2. Firmware lines `# MODE` and `# PARAMS`: the device reports the test type and
   its own parameters. Backwards compatible with V1. Checked on the real device.
3. `recorder.py`: creates a fully usable RAW file without any user input.
4. Thresholds in `validation.json` derived from the V1 campaign data sets. Each
   value names its source in its `reason` field.
5. `analysis/`: RAW → ANALYZED with one point per stage, findings per point
   and per run, full provenance in the header.
6. `session.py`: guided session in the terminal (live view, run detail,
   session summary).
7. `compare.py` and `plotting.py`: run-to-run comparison and plots.

Then `testbench.py` as single entry point, and automated tests with `pytest`.

**2026-09-11 – First real V2 session.**
Found one bug: the `NON_MONOTONIC` check was blind at the peak stage, because
the spread was only calculated when both directions existed. Fixed. Two
thresholds adjusted, both checked against the original reference campaign, not
only against the new runs.

## Phase 4 – Measurement series (September/October 2026)

**2026-09-17 – Main series.**
3 motors (1800 kV, 2450 kV 2207, 2750 kV) × 3 voltages (12/15/18 V) × 2
repetitions, same propeller. Voltage limited to 18 V because the 2750 kV motor
is a 4S motor.

- `temperature_drift` warning thresholds re-derived from all 18 runs. The old
  values were derived from the 1800 kV motor only and marked 88–95 % of the
  points of the other motors.
- ESC temperature limit raised from 80 to 85 °C, so that the 2750 kV / 18 V
  sweep can finish. This is a judgement call; there is no manufacturer value
  for this ESC.
- All 18 runs analyzed again with one common `validation.json`.

**2026-09-20 – Geometry comparison 2207 vs. 2306 (same kV).**
The 2306 motor is up to 8–10 % more efficient (g/W) at low throttle. The
difference becomes smaller than 1 % close to full throttle. Run-to-run spread
was 0.2–1.1 %, so the differences are clearly above the noise. Both motors stop
at the same current limit, so the top end is limited by the setup, not by the
motor.

**2026-10-08 – Automatic plots and warning thresholds.**
`session.py` creates power and efficiency plots at the end of a session.
A re-analysis of all 24 runs (686 points) showed 37 % WARNING. Most of it came
from the temperature difference between up and down direction, which follows
from the sweep order and not from the point quality. Thresholds adjusted
(`temperature_drift` 75 °C / 30 K, `unstable_measurement` from 3 outliers in a
row). Expected result: 14 % WARNING, INVALID unchanged.
**2026-10-09 – Published data analyzed again.**
All published runs were analyzed again with the current `validation.json`.
All measured values and all comparison results stay exactly the same. Only
the status labels change: 101 of 554 points go from WARNING to VALID. The 39
INVALID points stay INVALID.
