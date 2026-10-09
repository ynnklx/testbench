# Testbench – motor and propeller test stand

A test stand to measure small brushless motors with propellers: **thrust,
current, voltage, RPM and ESC temperature**. It runs on a lab power supply.

The main goal is not the most precise number. The goal is a measurement
process that is **traceable, reproducible and honest about its errors**:
every result file says where it comes from, raw data is never changed, and
every point that looks wrong is marked, not deleted.

![Efficiency vs. thrust, three motors at 18 V](data/plots/Hauptmessreihe/1800kv-prop365-18v_vs_2450kv2207-prop365-18v_vs_2750kv-prop365-18v_efficiency_vs_thrust.png)

*Efficiency (g/W) over thrust for three motors with the same propeller at
18 V. Each curve is the combination of two repeated runs.*

## How it works

```
ESP32 firmware  ->  Acquisition  ->  Analysis  ->  Comparison  ->  Plots
 raw ADC counts      RAW.csv        ANALYZED.csv   COMPARISON.csv   *.png
```

- The **firmware** (ESP32, C++, PlatformIO) reads the load cell (HX711), the
  current/voltage sensor (INA226) and the ESC telemetry. It sends only raw
  values with a timestamp per sample. It drives the throttle sequence by
  itself: up to full throttle in 2.5 % steps and down again.
- The **host software** (Python) records the data, makes one measurement point
  per throttle stage, checks each point against rules (VALID / WARNING /
  INVALID), compares repeated runs and draws plots.
- Calibration happens on the host. So old data can be recalculated when the
  calibration changes.

Details and reasons for each decision: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Validation

Before the main series, the measurement chain was checked in six steps
(details in [docs/DEVLOG.md](docs/DEVLOG.md)):

| Check | Result |
|---|---|
| Load cell calibration (5 weights) | residuals 0.6–2.5 % |
| Zero drift at rest | < 0.15 g in a typical 5 min run |
| Interference from motor and vibration | 0.0 g with decoupled load cell |
| INA226 vs. multimeter | voltage < 0.3 %, current 1.5–1.7 % |
| Settle time per stage | max 924 ms measured → 1000 ms used |
| Repeatability | SD < 1.2 % for thrust, current and RPM |

## Quick start (no hardware needed)

Python 3.10 or newer.

```bash
pip install pyserial numpy pandas matplotlib rich textual textual-plotext pytest

# Record with the built-in simulator instead of the real device
# (one RAW file per cycle, stop with Ctrl+C)
python testbench.py record --simulate

# Analyze, compare and plot
python testbench.py analyze data/raw/<file>_RAW.csv
python testbench.py compare --label demo data/processed/<file>_ANALYZED.csv ...
python testbench.py plot data/comparisons/<file>_COMPARISON.csv

# Or use the real data in this repository
python testbench.py plot --kind g_per_w_vs_thrust data/comparisons/Hauptmessreihe/*18v_COMPARISON.csv

# Guided session with live view in the terminal
python testbench.py session --simulate

# Tests
python -m pytest
```

Run `python testbench.py <command> --help` for all options.

Firmware: `cd firmware && pio run`.

## Repository structure

| Path | Content |
|---|---|
| `firmware/` | ESP32 firmware: HX711 driver, INA226, ESC control and telemetry, serial protocol |
| `acquisition/` | Serial link (with simulator), recorder, guided session, calibration |
| `analysis/` | RAW → ANALYZED: measurement points, statistics, validation rules |
| `comparison/` | Run-to-run comparison and plots |
| `core/` | File names, CSV headers, units, calibration |
| `config/` | `calibration.json` (load cell fit), `validation.json` (all thresholds, each with its source) |
| `data/` | Real measurement data, see [data/README.md](data/README.md) |
| `tests/` | Automated tests (pytest) |
| `docs/` | Architecture and development log |

## Hardware

ESP32 WROOM-32, 2 kg load cell with HX711, INA226 current sensor
(0.002 Ω shunt), T-Hobby F35A ESC (AM32 firmware), lab power supply 10 A.
Full list and pin assignment in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#hardware).

## Development with AI support

The firmware and the Python tools were implemented with the help of Claude
Code. I defined the test concept, requirements, test logic, interfaces and
validation strategy myself. The resulting measurement chain was then checked
with independent measuring equipment.
