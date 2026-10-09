"""comparison/compare.py against synthetic ANALYZED files, built via
analysis/analyzer.py on the same synthetic RAW fixtures test_analyzer.py
uses (reused directly rather than duplicated - real sample density matters
here too, same reasoning as there)."""
import csv

import pytest

from analysis import analyzer
from comparison import compare
from core import calibration, csvio
from tests.test_analyzer import _write_sweep_raw, _write_static_raw


def _make_analyzed(tmp_path, name, cal, stage_grams, stage_amps):
    raw_dir = tmp_path / name
    raw_dir.mkdir()
    raw_path = raw_dir / "2026-09-11_10-00-00_sweep_RAW.csv"
    _write_sweep_raw(raw_path, cal, stage_grams=stage_grams, stage_amps=stage_amps)
    return analyzer.analyze(raw_path, out_dir=raw_dir, quiet=True).out_path


def _rows(path):
    with open(path) as f:
        return list(csv.DictReader(line for line in f if not line.startswith("#")))


def test_compare_two_matching_runs_all_included(tmp_path):
    cal = calibration.try_load()
    a = _make_analyzed(tmp_path, "run_a", cal,
                        stage_grams={150: 20.0, 175: 25.0, 200: 30.0},
                        stage_amps={150: 1.0, 175: 1.2, 200: 1.4})
    b = _make_analyzed(tmp_path, "run_b", cal,
                        stage_grams={150: 20.5, 175: 24.5, 200: 30.5},
                        stage_amps={150: 1.05, 175: 1.15, 200: 1.45})

    out_path = compare.write_comparison([a, b], label="testlabel", out_dir=tmp_path / "comparisons").out_path
    assert out_path.name.endswith("_testlabel_COMPARISON.csv")

    header = csvio.read_header(out_path)
    assert header["source_count"] == "2"
    assert header["source_1_file"] == a.name
    assert header["label"] == "testlabel"

    rows = _rows(out_path)
    assert len(rows) == 3
    for row in rows:
        assert row["included"] == "yes"
        assert row["exclude_reason"] == ""
        assert row["n_runs_usable"] == "2"
        assert float(row["thrust_g_median"]) > 0
    row_150 = next(r for r in rows if r["stage_promille"] == "150")
    assert float(row_150["thrust_g_median"]) == pytest.approx(20.25, abs=0.05)


def test_compare_excludes_stage_missing_in_one_run(tmp_path):
    cal = calibration.try_load()
    a = _make_analyzed(tmp_path, "run_a", cal,
                        stage_grams={150: 20.0, 175: 25.0, 200: 30.0},
                        stage_amps={150: 1.0, 175: 1.2, 200: 1.4})
    # run_b never reached 200 (e.g. an early reversal) - only 150/175.
    b = _make_analyzed(tmp_path, "run_b", cal,
                        stage_grams={150: 20.5, 175: 24.5},
                        stage_amps={150: 1.05, 175: 1.15})

    out_path = compare.write_comparison([a, b], label="testlabel", out_dir=tmp_path / "comparisons").out_path
    rows = {r["stage_promille"]: r for r in _rows(out_path)}

    assert rows["150"]["included"] == "yes"
    assert rows["175"]["included"] == "yes"
    assert rows["200"]["included"] == "no"
    assert "stage not reached" in rows["200"]["exclude_reason"]
    assert b.name in rows["200"]["exclude_reason"]
    assert rows["200"]["thrust_g_median"] == ""


def test_compare_excludes_stage_invalid_in_one_run(tmp_path):
    cal = calibration.try_load()
    a = _make_analyzed(tmp_path, "run_a", cal,
                        stage_grams={150: 20.0, 175: 25.0},
                        stage_amps={150: 1.0, 175: 1.2})
    # run_b's 175 stage draws way over the current limit -> INVALID there.
    b = _make_analyzed(tmp_path, "run_b", cal,
                        stage_grams={150: 20.5, 175: 24.5},
                        stage_amps={150: 1.05, 175: 12.0})

    out_path = compare.write_comparison([a, b], label="testlabel", out_dir=tmp_path / "comparisons").out_path
    rows = {r["stage_promille"]: r for r in _rows(out_path)}

    assert rows["150"]["included"] == "yes"
    assert rows["175"]["included"] == "no"
    assert "INVALID" in rows["175"]["exclude_reason"]
    assert "CURRENT_LIMIT_EXCEEDED" in rows["175"]["exclude_reason"]


def test_compare_rejects_static_input(tmp_path):
    cal = calibration.try_load()
    raw_dir = tmp_path / "static_run"
    raw_dir.mkdir()
    raw_path = raw_dir / "2026-09-11_10-00-00_static70_RAW.csv"
    _write_static_raw(raw_path, cal, promille=700, grams=15.0, amps=0.8)
    static_analyzed = analyzer.analyze(raw_path, out_dir=raw_dir, quiet=True).out_path

    sweep_analyzed = _make_analyzed(tmp_path, "run_a", cal,
                                     stage_grams={150: 20.0}, stage_amps={150: 1.0})

    with pytest.raises(ValueError, match="only compares sweep"):
        compare.write_comparison([static_analyzed, sweep_analyzed], label="mix",
                                  out_dir=tmp_path / "comparisons")


def test_summary_kpis_two_runs(tmp_path):
    """Hand-computable scenario: run B is run A scaled up by 10% (thrust
    *and* current) at both stages - g_per_w stays comparable but not
    identical (current scaled slightly differently), giving a clean,
    predictable best-efficiency pick and a single, exact cross-run
    deviation figure at both stages."""
    cal = calibration.try_load()
    a = _make_analyzed(tmp_path, "run_a", cal,
                        stage_grams={150: 20.0, 500: 100.0},
                        stage_amps={150: 1.0, 500: 2.0})
    b = _make_analyzed(tmp_path, "run_b", cal,
                        stage_grams={150: 22.0, 500: 110.0},
                        stage_amps={150: 1.05, 500: 2.3})

    result = compare.write_comparison([a, b], label="testlabel", out_dir=tmp_path / "comparisons")
    s = result.summary

    assert s.n_runs == 2
    assert s.n_points_valid == 4  # 2 stages x 2 runs, nothing flagged
    assert s.n_points_warning == 0
    assert s.n_points_invalid == 0

    # power_w = voltage_v * current_a (16.0 V): A@500=32.0W, B@500=36.8W
    assert s.max_usable_thrust_g == pytest.approx(110.0, abs=0.01)  # run_b @ 500
    assert s.max_usable_thrust_stage_promille == 500
    assert s.max_usable_thrust_status == "VALID"
    assert s.peak_usable_power_w == pytest.approx(36.8, abs=0.01)  # run_b @ 500

    # g_per_w: A@150=20/16=1.25, B@150=22/16.8=1.310, A@500=100/32=3.125,
    # B@500=110/36.8=2.989 -> best is A@500.
    assert s.best_g_per_w == pytest.approx(3.125, abs=0.001)
    assert s.best_g_per_w_thrust_g == pytest.approx(100.0, abs=0.01)
    assert s.best_g_per_w_stage_promille == 500

    # Both stages: run_b is exactly +10% over run_a -> median=1.05x run_a,
    # both runs deviate by the same 1/21 = 4.7619% from that median.
    assert s.typical_run_deviation_pct == pytest.approx(4.7619, abs=0.01)
    assert s.max_run_deviation_pct == pytest.approx(4.7619, abs=0.01)

    # Both legs of both runs hold the same grams/amps (see _write_sweep_raw)
    # -> thrust_g_delta_pct is 0 everywhere, so this is 0, not None - still
    # confirms the per-run-then-median-of-medians pipeline runs cleanly.
    assert s.median_direction_mismatch_pct == pytest.approx(0.0, abs=0.01)

    # stage_deviation_pct persisted in the file itself, not just the
    # aggregate - the Session Summary's Repeatability plot reads this
    # directly (docs/ARCHITECTURE.md: no recomputation in the UI).
    rows_by_stage = {r["stage_promille"]: r for r in result.rows}
    assert rows_by_stage[150]["stage_deviation_pct"] == pytest.approx(4.7619, abs=0.01)
    assert rows_by_stage[500]["stage_deviation_pct"] == pytest.approx(4.7619, abs=0.01)


def test_summary_cross_run_deviation_none_with_one_run(tmp_path):
    cal = calibration.try_load()
    a = _make_analyzed(tmp_path, "run_a", cal,
                        stage_grams={150: 20.0, 500: 100.0},
                        stage_amps={150: 1.0, 500: 2.0})

    result = compare.write_comparison([a], label="single", out_dir=tmp_path / "comparisons")
    s = result.summary

    assert s.n_runs == 1
    assert s.max_usable_thrust_g == pytest.approx(100.0, abs=0.01)  # still meaningful with 1 run
    assert s.typical_run_deviation_pct is None
    assert s.max_run_deviation_pct is None


def test_summary_efficiency_respects_noise_floor(tmp_path):
    """A stage right at the noise floor must not win "best efficiency" even
    if its g/W ratio looks good on paper - docs/ARCHITECTURE.md's thrust_relative_floor_g
    gate, reused here for the efficiency KPI."""
    cal = calibration.try_load()
    # thrust_relative_floor_g is 0.3 g (config/validation.json); 0.05 g at
    # a tiny current gives a huge, meaningless g/W that must be excluded.
    a = _make_analyzed(tmp_path, "run_a", cal,
                        stage_grams={150: 0.05, 500: 100.0},
                        stage_amps={150: 0.01, 500: 2.0})
    b = _make_analyzed(tmp_path, "run_b", cal,
                        stage_grams={150: 0.05, 500: 100.0},
                        stage_amps={150: 0.01, 500: 2.0})

    result = compare.write_comparison([a, b], label="floorcheck", out_dir=tmp_path / "comparisons")
    s = result.summary
    assert s.best_g_per_w_stage_promille == 500


def test_compare_in_memory_writes_nothing(tmp_path):
    """PARTIAL session summaries (acquisition/session.py, 's' before the
    last repetition) must not litter data/comparisons/ with a file per
    keypress - compare_in_memory() computes the same numbers without
    writing anything."""
    cal = calibration.try_load()
    a = _make_analyzed(tmp_path, "run_a", cal,
                        stage_grams={150: 20.0, 500: 100.0},
                        stage_amps={150: 1.0, 500: 2.0})
    b = _make_analyzed(tmp_path, "run_b", cal,
                        stage_grams={150: 22.0, 500: 110.0},
                        stage_amps={150: 1.05, 500: 2.3})

    comparisons_dir = tmp_path / "comparisons"
    result = compare.compare_in_memory([a, b])

    assert result.out_path is None
    assert not comparisons_dir.exists()  # nothing written anywhere
    assert result.summary.n_runs == 2
    assert result.summary.max_usable_thrust_g == pytest.approx(110.0, abs=0.01)
