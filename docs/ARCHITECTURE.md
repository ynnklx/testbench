# Architecture and design decisions

This document explains how the software is built and why. The history of how
these decisions were made is in [DEVLOG.md](DEVLOG.md).

## Goal

The goal of the project is a measurement process that is **traceable and
documented**. This is more important than the highest possible precision.
Reproducibility, clean data handover and an honest error analysis come first.

V2 is a new structure of the software. The measurement chain itself (load cell,
current sensor, firmware timing) is the same as in V1. It was checked in the
validation campaign V1–V6 (see DEVLOG).

## Pipeline

```
Acquisition  ->  Analysis of one run  ->  Comparison of runs  ->  Plots
  RAW.csv          ANALYZED.csv            COMPARISON.csv         *.png
```

Every stage has exactly one input and one output. Every stage can be started
alone. Every result file carries its origin in the header.

| Stage | Code | Command |
|---|---|---|
| Acquisition | `acquisition/recorder.py`, `acquisition/session.py` | `testbench.py record` / `session` |
| Analysis | `analysis/analyzer.py` (+ `points.py`, `validation.py`, `stats.py`) | `testbench.py analyze` |
| Comparison | `comparison/compare.py` | `testbench.py compare` |
| Plots | `comparison/plotting.py` | `testbench.py plot` |

Shared helpers are in `core/` (file names, CSV headers, units, calibration).

## Design decisions

1. **The firmware sends only raw values.** ADC counts, no grams. Calibration
   happens on the host. The constants are in `config/calibration.json` and are
   written into every CSV header. Reason: a data set must stay convertible when
   the calibration changes, without measuring again.

2. **RAW files are immutable.** After closing, the recorder sets the file to
   read-only. Every later stage opens it only with `"r"`.

3. **The wizard gives documentation, never input for calculation.** Everything
   the analyzer needs is in the RAW file or in the two config files.
   `testbench.py record` without any argument creates a file that can be fully
   analyzed. The wizard only adds what no machine can know: propeller, motor,
   supply voltage, room temperature, comment, repetition number.

4. **One measurement point per throttle stage.** See "Measurement points".

5. **Nothing is dropped silently.** INVALID points stay in the ANALYZED file.
   Excluded points are listed in the COMPARISON file with a reason. Outliers are
   marked, never removed automatically.

6. **No extrapolation.** Interpolation only happens between neighbouring usable
   points, inside the range that all runs really measured. There is no
   interpolation across an INVALID point. WARNING points are full support points.

7. **Limits are in `config/validation.json`, not in the code.** A rule with a
   `null` threshold is reported and skipped. It never runs with a hidden
   default. A finding code in the config that the code does not know is an
   error, not an ignored entry.

8. **Provenance in every result file:** name and SHA-256 of every source file,
   the calibration constants (`counts_per_gram`, `raw_zero`) in plain text plus
   hash, name and hash of `validation.json`, tool version, time of analysis.
   The constants are written in plain text, not only as a hash. So a data set
   can be recalculated without finding the old config file.

9. **`plotting.py` does not calculate, `compare.py` does not draw.** The
   separation is the import direction: `compare.py` never imports `matplotlib`.

10. **Firmware and tools:** PlatformIO, own HX711 driver (no library, see
    "Known pitfalls"), every sample has a timestamp (the nominal 10 SPS of the
    HX711 is never used as time base), WiFi and Bluetooth off during
    measurement. All host tools run without hardware (`--simulate`).

### Dependency boundaries

- `pyserial` only in `acquisition/link.py`.
- `rich`, `textual`, `textual-plotext` only in `acquisition/session.py`.
- `matplotlib` only in `comparison/plotting.py`.
- `analysis/` and `comparison/compare.py` use only the standard library, so they
  can be tested without installing anything.

## Measurement points

An ANALYZED row is **one measurement window**, not one raw data row.

- **sweep:** exactly one point per throttle stage. The device drives the stages
  up and then down again. Up and down windows are cut, calculated and checked
  separately, and then combined.
- **static:** one point is one time window of fixed width (`bin_seconds`).

### Combined value

**Two-step estimator: median inside each direction, then the arithmetic mean
of the two medians.** Not the pooled median over all samples. Reasons:

- The pooled median is weighted by sample count. With 30 against 24 samples the
  result moves towards the up direction without anyone seeing it.
- If up and down are different, the pooled distribution has two peaks. The
  median jumps into the bigger peak instead of lying between them.
- Both visits of a stage are roughly symmetric in time around the middle of the
  run. Their mean cancels a linear drift.
- Outlier protection is kept: a single HX711 spike is already removed by the
  median inside its direction.

Limit: the drift compensation needs this time symmetry. If the sweep turns
early (RPM limit, voltage drop), it is less exact for low stages. This affects
accuracy, not validity.

### Diagnostics that are kept

- `delta` = down − up, with sign.
- `delta_pct` = delta / **mean** × 100. Not divided by the up value, which is
  close to the noise floor at low throttle and would make the percentage huge.
- `sd_within` = √((sd_up² + sd_down²)/2): spread *inside* a window, separate
  from `delta`, the spread *between* the directions.
- `n_up`, `n_down`, `directions_used` (`up+down`, `up` or `down`).

**Temperature is not combined.** Only `temp_c_up`, `temp_c_down` and `dT`. The
up direction is clearly colder; a mean would describe neither state.

### Only one direction

The point is still created if the existing direction is usable. Peak stage:
no finding, only the note in `directions_used` (it is visited once by design).
Partner exists but is unusable: `SINGLE_DIRECTION_ONLY` as WARNING.

## Validation

Three states per point, **one** status per point:

- **VALID**: no finding.
- **WARNING**: the value is correct, but its context limits the
  interpretation. It stays usable and is only marked in the comparison.
- **INVALID**: the value is not suitable for a quantitative comparison. It
  stays in the file, but is not used in aggregation and not bridged.

**Careful with INVALID.** Only four findings can cause it: `SENSOR_ERROR`,
`OUT_OF_RANGE`, `CURRENT_LIMIT_EXCEEDED`, and `INSUFFICIENT_SAMPLES` in a
strong form. All others only warn (their `invalid_above` is `null`). Goal:
small problems must not break the measurement curve into pieces.

A finding is `(code, level, value, threshold, text)`. The measured value and the
threshold belong to it, otherwise the result can not be checked. All findings
of a point are stored, not only the worst one.

**Run findings vs. point findings.** An ANALYZED data row is always a real
measurement point. `ZERO_REFERENCE_DRIFT` (both test types) and `RUN_DRIFT`
(static only) are about the whole run. They are written as `run_findings` /
`run_status` in the ANALYZED header, in the same schema as point findings.

**Current.** Two codes with different meaning:

- `CURRENT_LIMIT_EXCEEDED` (INVALID): about the *analysis*. Current above
  `current_invalid_above_a`. If one direction is above, the whole stage is
  INVALID.
- `CURRENT_LIMIT_ACTIVE` (WARNING): about the *power supply*. Constant-current
  mode detected. Diagnosis only. A point can be in CC mode and still be usable.

**Spread near zero load.** One threshold, two regimes, bound to
`thrust_relative_floor_g`. Above it, spread is rated relative (MAD/median, CV).
Below it, spread is rated absolute in grams. The relative column stays
**empty** below the threshold instead of showing a huge number. V1 once printed
2687 % because a small absolute spread was divided by a median close to zero.
The same rule applies to the CV values in the comparison.

**RPM creates no findings.** RPM is not used for any calculation, so there is
nothing to rate. The column stays as diagnostic value. A missing `E` line
creates `TEMPERATURE_UNAVAILABLE` (WARNING), because of the temperature.

**Thresholds** in `config/validation.json` come from real data sets. Each
`reason` field names its source.

## File names

```
<date>_<time>_<testtype>[<percent>][_<n>of<m>]_<STAGE>.csv

2026-09-11_13-42-18_sweep_1of3_RAW.csv
2026-09-11_13-42-18_sweep_1of3_ANALYZED.csv
2026-09-11_14-20-10_static70_1of3_RAW.csv
2026-09-11_16-08-55_sweep_RAW.csv              # recorder alone, no plan
2026-09-11_15-02-44_prop365-16v_COMPARISON.csv
```

- RAW and ANALYZED share the same stem.
- Throttle as percent in the name, per mille in the data: `700 -> "70"`,
  `675 -> "67.5"`. The header is the reference: `stage_promille=700`.
- The repetition field is optional. A recorder started alone has no plan, and a
  made-up number would be worse than none.

## Serial protocol

Line based, `\n` terminated, 921600 baud, ASCII.

### Host → device

| Command | Answer |
|---|---|
| `ID?` | `OK ID <name> fw=<ver> rate=<sps>` |
| `PING` | `OK PONG <t_us>` |
| `START` / `STOP` | `OK START` / `OK STOP` |
| `TARE <n>` | `OK TARE <mean> <sd> <n>` (averages only, applies nothing) |
| `SET <promille>` | `OK SET <promille>`, only when enabled by the arm button |

### Device → host

```
D,<seq>,<t_us>,<raw>
I,<seq>,<t_us>,<raw_shunt>,<raw_bus>
E,<seq>,<t_us>,<rpm>,<erpm>,<temp_c>,<voltage_cv>,<current_ca>,<consumption_mah>
```

`t_us` is `micros()` at the DRDY edge or the conversion-ready bit, not at send
time. `raw_shunt`/`raw_bus` are raw INA226 registers; the host converts them
(2.5 µV/count shunt over 0.002 Ω = 1.25 mA/count, 1.25 mV/count bus).
`current_ca`/`consumption_mah` from the ESC are wrong on this ESC (factor ~3750)
and are not used. Only the `I` line counts for current.

Comment lines: `# ARMED (button)`, `# MODE ...`, `# PARAMS ...`,
`# STAGE <promille>`, `# HOLD <promille>`, `# DISARMED (...)`.

### `# MODE` and `# PARAMS`

Two lines right after `# ARMED (button)`:

```
# MODE sweep                       (or: # MODE static 700)
# PARAMS start=150 step=25 settle_ms=1000 hold_ms=2500 rpm_limit=25000 \
         temp_max_c=85 sag_fraction=0.90 pole_pairs=7
```

`# MODE` makes the test type a statement of the device, not a claim of the
operator. It also gives the static throttle value, which is set on the device
by button and is otherwise unknown to the host.

`# PARAMS` fixes a real problem from V1: the same firmware constants were
copied by hand in seven places in the host code. A free-text header was wrong
for four days (it said 620 ms settle time while 1000 ms was active). Now there
is one source, and it is in every RAW file.

The extension is backwards compatible: V1's logger ignores unknown `#` lines.

## Hardware

| Part | Type |
|---|---|
| Controller | ESP32 WROOM-32 |
| Load cell | 2 kg bar, 4-wire |
| ADC | HX711, 3.3 V, 10 SPS |
| Current/voltage | INA226, 0.002 Ω shunt |
| ESC | T-Hobby F35A, AM32, 48 kHz PWM, servo 1000–2000 µs |
| Motors | 12N14P, 7 pole pairs (1800 kV, 2450 kV 2207/2306, 2750 kV) |
| Power supply | Lab supply, 10 A |

| Function | GPIO |
|---|---|
| HX711 DOUT / SCK | 16 / 17 |
| Potentiometer (ADC1) | 34 |
| ESC signal | 25 |
| I²C SDA / SCL | 21 / 22 |
| Arm button | 26 |
| Status LED | 4 |
| Tare button (long press ≥800 ms: static mode) | 27 |
| ESC telemetry | 18 |

GPIO 0, 2, 12, 15 stay free (strapping pins). Every automatic or adaptive ESC
setting is switched off in favour of a fixed, documented value.

## Known pitfalls

These problems were found during the project. The code handles all of them.

**HX711 timing.** If SCK stays high longer than 60 µs, the chip goes into
power-down and then gives wrong numbers that look plausible. On the ESP32 with
FreeRTOS this happens when an interrupt falls into the bit-bang loop. The 25
clock pulses are therefore in a critical section (~100 µs). DOUT polling has a
timeout. The 24-bit value is signed and is extended to int32.

**Blocking I²C in `loop()` breaks the serial protocol.** A display redraw on
every HX711 sample over slow I²C blocked `loop()` so long that the serial input
buffer overflowed (broken `OK` answers). Redraw is limited to ~3 Hz.

**INA226 conversion-ready bit is cleared when read.** Read it exactly once per
cycle.

**Debouncing needs a stability window, not a minimum distance.** The raw level
must be stable for a fixed time; the timer restarts on every change.

**ESC telemetry can stay silent after an ESP32 reset** until the ESC itself is
power-cycled. After every reflash, the `E` lines are checked actively.

**The measurement chain needs time to settle.** The load cell needs up to
~620 ms, the lowest stages up to 924 ms. Settle time is 1000 ms.

**Spread that grows with throttle can be recirculation.** An obstacle in the
propeller airflow increased the spread 5–10 times. The air flow area must stay
free.

**One bad calibration point can distort the whole fit.** A weight that was not
placed cleanly gave 14 % residual at one point. `calibrate.py` shows residuals
per point, not only the fit.

**The calibration zero point drifts** with mounting and storage, while
`counts_per_gram` stays stable. Fix: a session zero point per run from the
middle second of the 3 s countdown.

**The CC reference voltage was circular in V1.** V1 used the highest voltage in
the run. If a run is in CC from the first stage, this reference is already too
low. V2 uses the countdown voltage with the motor stopped.

**Direction is derived only from position.** "Stage lower than previous ⇒ down
direction". The protocol has no direction marker. This is correct for the
current firmware sequence (one way up, one way down). If the firmware sequence
changes, this logic must be checked again.
