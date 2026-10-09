"""analysis/analyzer.py against synthetic RAW files with realistic sample
density and chronological interleaving - acquisition/link.py's
SimulatedLink deliberately compresses its timing to replay quickly (see its
class docstring), which makes insufficient_samples fire on every stage when
checked against the real window_ms, and never exercises the static path's
real-time bin bucketing correctly either; it is the wrong tool for testing
analysis/ correctness. These fixtures are built directly, matching
V1/recorder.py's raw-value conventions, so the numbers here are controlled
precisely.
"""
import csv
import json

from analysis import analyzer, validation
from core import calibration, csvio

_HEADER_COMMON = [
    "schema_version=1", "recorded_at=2026-01-01T00:00:00",
    "tool_version=2.0.0", "device_id=OK ID test fw=1.0.0-test rate=10",
    "calibration_file=config/calibration.json",
    "calibration_date=2026-09-02", "calibration_provisional=no",
    "propeller=", "motor=", "supply_voltage_v=16", "ambient_temp_c=22",
    "comment=", "repetition=",
]

_COLUMNS = ["type", "seq", "t_esp_us", "t_host_iso", "raw", "raw_shunt", "raw_bus",
            "rpm", "erpm", "temp_c", "stage_promille", "phase"]


def _grams_to_raw(cal, grams):
    return round(cal.raw_zero + grams * cal.counts_per_gram)


def _amps_to_raw_shunt(amps):
    return round(amps / (2.5e-6 / 0.002))


def _volts_to_raw_bus(volts):
    return round(volts / (1.25 / 1000.0))


class _RowBuilder:
    """Builds RAW rows in real chronological order - D/I/E interleaved by
    their own real timestamp, not three separate sequential blocks. That
    interleaving does not matter to points.py's sweep path (episodes bucket
    by stage/phase, not by elapsed time) but is essential for the static
    path's real-time bin bucketing (analysis/points.py's _BinAccumulator)."""

    def __init__(self):
        self.t = 0
        self.rows = []

    def _add_at(self, type_, t_us, **kw):
        row = dict.fromkeys(_COLUMNS, "")
        row.update(type=type_, seq=0, t_esp_us=t_us, stage_promille=0, phase="")
        row.update(kw)
        self.rows.append(row)

    def window(self, cal, stage, phase, duration_s, grams, amps, volts=16.0,
               rpm=5000, temp_c=30, d_period_us=100_000, i_period_us=20_000,
               e_period_us=30_000):
        """Emits D/I/E samples across duration_s starting at self.t, merged
        into real chronological order. d_period_us=100_000 (10 Hz) matches
        the nominal HX711 rate insufficient_samples is calibrated against."""
        start = self.t
        end = start + int(duration_s * 1_000_000)
        events = []
        for period, type_ in ((d_period_us, "D"), (i_period_us, "I"), (e_period_us, "E")):
            t = start
            while t < end:
                events.append((t, type_))
                t += period
        events.sort(key=lambda e: e[0])
        for t_us, type_ in events:
            if type_ == "D":
                self._add_at("D", t_us, raw=_grams_to_raw(cal, grams), stage_promille=stage, phase=phase)
            elif type_ == "I":
                self._add_at("I", t_us, raw_shunt=_amps_to_raw_shunt(amps),
                              raw_bus=_volts_to_raw_bus(volts), stage_promille=stage, phase=phase)
            else:
                self._add_at("E", t_us, rpm=rpm, erpm=rpm * 7, temp_c=temp_c,
                              stage_promille=stage, phase=phase)
        self.t = end

    def countdown(self, cal, duration_s=3.0):
        self.window(cal, 0, "", duration_s, grams=0.0, amps=0.0, volts=16.0)

    def post_run(self, cal, grams=0.02, duration_s=2.0):
        self.window(cal, 0, "hold", duration_s, grams=grams, amps=0.0, volts=16.0)

    def write(self, path, extra_header):
        with open(path, "w", newline="") as f:
            csvio.write_header(f, [*extra_header, *_HEADER_COMMON])
            writer = csv.DictWriter(f, fieldnames=_COLUMNS)
            writer.writeheader()
            writer.writerows(self.rows)


def _write_sweep_raw(path, cal, stage_grams, stage_amps, volts=16.0, hold_s=2.5):
    """stage_grams/stage_amps: {promille: value} for the upward leg (the
    downward leg reuses the same values, minus the top stage, which is only
    visited once - docs/ARCHITECTURE.md)."""
    b = _RowBuilder()
    b.countdown(cal)
    stages = sorted(stage_grams)
    for stage in stages:
        b.window(cal, stage, "hold", hold_s, stage_grams[stage], stage_amps[stage], volts=volts)
    for stage in reversed(stages[:-1]):
        b.window(cal, stage, "hold", hold_s, stage_grams[stage], stage_amps[stage], volts=volts)
    b.post_run(cal)
    b.write(path, [
        "mode=sweep",
        "params=start=150 step=25 settle_ms=1000 hold_ms=2500 rpm_limit=25000 "
        "temp_max_c=80 sag_fraction=0.90 pole_pairs=7",
    ])


def _write_static_raw(path, cal, promille, grams, amps, duration_s=20.0):
    b = _RowBuilder()
    b.countdown(cal)
    b.window(cal, promille, "hold", duration_s, grams, amps)
    b.post_run(cal)
    b.write(path, [
        f"mode=static {promille}",
        "params=start=150 step=25 settle_ms=1000 hold_ms=2500 rpm_limit=25000 "
        "temp_max_c=80 sag_fraction=0.90 pole_pairs=7",
    ])


def _rows(path):
    with open(path) as f:
        return list(csv.DictReader(line for line in f if not line.startswith("#")))


def test_analyze_synthetic_sweep_all_valid(tmp_path):
    cal = calibration.try_load()
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    raw_path = raw_dir / "2026-09-11_10-00-00_sweep_RAW.csv"
    _write_sweep_raw(raw_path, cal,
                      stage_grams={150: 20.0, 175: 25.0, 200: 30.0},
                      stage_amps={150: 1.0, 175: 1.2, 200: 1.4})

    out_path = analyzer.analyze(raw_path, out_dir=tmp_path / "processed").out_path
    assert out_path.exists()
    assert out_path.name == "2026-09-11_10-00-00_sweep_ANALYZED.csv"

    header = csvio.read_header(out_path)
    assert header["run_status"] == validation.STATUS_VALID
    assert header["raw_source_file"] == raw_path.name
    assert header["raw_mode"] == "sweep"
    assert float(header["calibration_counts_per_gram"]) == cal.counts_per_gram

    rows = {r["stage_promille"]: r for r in _rows(out_path)}
    assert set(rows) == {"150", "175", "200"}
    for stage, row in rows.items():
        assert row["status"] == validation.STATUS_VALID, row["findings"]
    # The peak stage (200) is visited once by design (docs/ARCHITECTURE.md) - no down leg.
    assert rows["150"]["directions_used"] == "up+down"
    assert rows["175"]["directions_used"] == "up+down"
    assert rows["200"]["directions_used"] == "up"
    assert rows["150"]["n_up"] == "25" and rows["150"]["n_down"] == "25"

    thrusts = [float(rows[s]["thrust_g"]) for s in ("150", "175", "200")]
    assert thrusts == sorted(thrusts)


def test_analyze_synthetic_sweep_current_limit_exceeded(tmp_path):
    cal = calibration.try_load()
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    raw_path = raw_dir / "2026-09-11_10-00-00_sweep_RAW.csv"
    _write_sweep_raw(raw_path, cal,
                      stage_grams={150: 20.0, 175: 25.0},
                      stage_amps={150: 1.0, 175: 12.0})  # 175 exceeds the 9.9A limit

    out_path = analyzer.analyze(raw_path, out_dir=tmp_path / "processed").out_path
    rows = {r["stage_promille"]: r for r in _rows(out_path)}
    assert rows["150"]["status"] == validation.STATUS_VALID
    assert rows["175"]["status"] == validation.STATUS_INVALID
    codes = [f["code"] for f in json.loads(rows["175"]["findings"])]
    assert "CURRENT_LIMIT_EXCEEDED" in codes


def test_analyze_synthetic_static_all_valid(tmp_path):
    cal = calibration.try_load()
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    raw_path = raw_dir / "2026-09-11_10-00-00_static70_RAW.csv"
    _write_static_raw(raw_path, cal, promille=700, grams=15.0, amps=0.8)

    out_path = analyzer.analyze(raw_path, out_dir=tmp_path / "processed").out_path
    assert out_path.name == "2026-09-11_10-00-00_static70_ANALYZED.csv"

    header = csvio.read_header(out_path)
    assert header["run_status"] == validation.STATUS_VALID

    rows = _rows(out_path)
    assert len(rows) == 2  # 20s run / bin_seconds=10.0 -> two complete bins
    for row in rows:
        assert row["stage_promille"] == "700"
        assert row["directions_used"] == ""
        assert row["bin_start_s"] != ""
        assert row["status"] == validation.STATUS_VALID, row["findings"]


def test_analyze_synthetic_static_zero_reference_drift_run_finding(tmp_path):
    """The post-run capture window feeds a run-level finding
    (ZERO_REFERENCE_DRIFT), never a Point of its own - not present in the per-point rows at all."""
    cal = calibration.try_load()
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    raw_path = raw_dir / "2026-09-11_10-00-00_static70_RAW.csv"
    b = _RowBuilder()
    b.countdown(cal)
    b.window(cal, 700, "hold", 12.0, grams=15.0, amps=0.8)
    b.post_run(cal, grams=5.0)  # far above the 0.15 g threshold - deliberate drift
    b.write(raw_path, [
        "mode=static 700",
        "params=start=150 step=25 settle_ms=1000 hold_ms=2500 rpm_limit=25000 "
        "temp_max_c=80 sag_fraction=0.90 pole_pairs=7",
    ])

    out_path = analyzer.analyze(raw_path, out_dir=tmp_path / "processed").out_path
    header = csvio.read_header(out_path)
    assert header["run_status"] == validation.STATUS_WARNING
    run_findings = json.loads(header["run_findings"])
    codes = [f["code"] for f in run_findings]
    assert "ZERO_REFERENCE_DRIFT" in codes

    for row in _rows(out_path):
        assert row["stage_promille"] != "0"  # never a pseudo-point
