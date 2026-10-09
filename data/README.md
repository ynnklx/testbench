# Measurement data

Real data from the test stand. Each folder follows the pipeline:

| Folder | Content |
|---|---|
| `raw/` | RAW files from the recorder (read-only, never changed) |
| `processed/` | ANALYZED files, one per RAW file |
| `comparisons/` | COMPARISON files, one per configuration |
| `plots/` | PNG plots made from the COMPARISON files |

## Series

**`Hauptmessreihe/` – main series (2026-09-17).**
3 motors (1800 kV, 2450 kV 2207, 2750 kV) × 3 voltages (12, 15, 18 V) ×
2 repetitions. Same propeller (3.65″) and same setup for all runs.

**`Demo/` – demo run (2026-10-08).**
One sweep with the 2450 kV 2306 motor at 12 V. It was recorded during the
video for the project documentation.

## Notes

- Every file has its provenance in the header: source file names and SHA-256,
  calibration constants, `validation.json` hash, tool version.
- All ANALYZED, COMPARISON and plot files were created again on 2026-10-09
  with the current `config/validation.json` (see
  [DEVLOG](../docs/DEVLOG.md)). The RAW files were not changed. You can repeat
  this with `python testbench.py analyze <RAW file>`.
- New recordings are written into the top level of each folder and are not
  published (see `.gitignore`).
