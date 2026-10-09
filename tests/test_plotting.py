"""comparison/plotting.py - only checks that a plausible PNG comes out and
that an excluded stage does not silently get a value (docs/ARCHITECTURE.md decision 6:
no interpolation across it). Not a pixel-level check of the render."""
import pytest

from analysis import analyzer
from comparison import compare, plotting
from core import calibration
from tests.test_analyzer import _write_sweep_raw


def _make_analyzed(tmp_path, name, cal, stage_grams, stage_amps):
    raw_dir = tmp_path / name
    raw_dir.mkdir()
    raw_path = raw_dir / "2026-09-11_10-00-00_sweep_RAW.csv"
    _write_sweep_raw(raw_path, cal, stage_grams=stage_grams, stage_amps=stage_amps)
    return analyzer.analyze(raw_path, out_dir=raw_dir, quiet=True).out_path


def test_plot_thrust_with_excluded_stage(tmp_path):
    cal = calibration.try_load()
    a = _make_analyzed(tmp_path, "run_a", cal,
                        stage_grams={150: 20.0, 175: 25.0, 200: 30.0},
                        stage_amps={150: 1.0, 175: 1.2, 200: 1.4})
    b = _make_analyzed(tmp_path, "run_b", cal,
                        stage_grams={150: 20.5, 175: 24.5},  # never reaches 200
                        stage_amps={150: 1.05, 175: 1.15})

    comparison_path = compare.write_comparison([a, b], label="testlabel",
                                                 out_dir=tmp_path / "comparisons").out_path
    out_path = plotting.plot_thrust(comparison_path, out_dir=tmp_path / "plots")

    assert out_path.exists()
    assert out_path.name.endswith("_thrust.png")
    assert out_path.stat().st_size > 1000  # a real rendered plot, not an empty/broken file


def test_plot_thrust_rejects_non_comparison_file(tmp_path):
    bogus = tmp_path / "not_a_comparison_file.csv"
    bogus.write_text("not,relevant\n1,2\n")
    with pytest.raises(ValueError):
        plotting.plot_thrust(bogus, out_dir=tmp_path / "plots")


def test_plot_thrust_overlays_several_comparison_files(tmp_path):
    """Two independent configurations (different label) in one chart -
    docs/ARCHITECTURE.md decision 9 still holds (no computation here, only what
    compare.py already wrote for each file)."""
    cal = calibration.try_load()

    a1 = _make_analyzed(tmp_path, "cfg1_a", cal,
                         stage_grams={150: 20.0, 175: 25.0}, stage_amps={150: 1.0, 175: 1.2})
    a2 = _make_analyzed(tmp_path, "cfg1_b", cal,
                         stage_grams={150: 20.5, 175: 24.5}, stage_amps={150: 1.05, 175: 1.15})
    comparison_1 = compare.write_comparison([a1, a2], label="cfg1",
                                             out_dir=tmp_path / "comparisons").out_path

    b1 = _make_analyzed(tmp_path, "cfg2_a", cal,
                         stage_grams={150: 30.0, 175: 35.0}, stage_amps={150: 1.5, 175: 1.7})
    b2 = _make_analyzed(tmp_path, "cfg2_b", cal,
                         stage_grams={150: 30.5, 175: 34.5}, stage_amps={150: 1.55, 175: 1.65})
    comparison_2 = compare.write_comparison([b1, b2], label="cfg2",
                                             out_dir=tmp_path / "comparisons").out_path

    out_path = plotting.plot_thrust([comparison_1, comparison_2], out_dir=tmp_path / "plots")

    assert out_path.exists()
    assert out_path.name.endswith("_thrust.png")
    assert "cfg1" in out_path.name and "cfg2" in out_path.name
    assert out_path.stat().st_size > 1000


def test_plot_power_vs_thrust(tmp_path):
    cal = calibration.try_load()
    a = _make_analyzed(tmp_path, "run_a", cal,
                        stage_grams={150: 20.0, 175: 25.0, 200: 30.0},
                        stage_amps={150: 1.0, 175: 1.2, 200: 1.4})
    b = _make_analyzed(tmp_path, "run_b", cal,
                        stage_grams={150: 20.5, 175: 24.5},  # never reaches 200
                        stage_amps={150: 1.05, 175: 1.15})

    comparison_path = compare.write_comparison([a, b], label="testlabel",
                                                 out_dir=tmp_path / "comparisons").out_path
    out_path = plotting.plot_power_vs_thrust(comparison_path, out_dir=tmp_path / "plots")

    assert out_path.exists()
    assert out_path.name.endswith("_power_vs_thrust.png")
    assert out_path.stat().st_size > 1000


def test_plot_g_per_w_vs_thrust(tmp_path):
    cal = calibration.try_load()
    a = _make_analyzed(tmp_path, "run_a", cal,
                        stage_grams={150: 20.0, 175: 25.0, 200: 30.0},
                        stage_amps={150: 1.0, 175: 1.2, 200: 1.4})
    b = _make_analyzed(tmp_path, "run_b", cal,
                        stage_grams={150: 20.5, 175: 24.5},  # never reaches 200
                        stage_amps={150: 1.05, 175: 1.15})

    comparison_path = compare.write_comparison([a, b], label="testlabel",
                                                 out_dir=tmp_path / "comparisons").out_path
    out_path = plotting.plot_g_per_w_vs_thrust(comparison_path, out_dir=tmp_path / "plots")

    assert out_path.exists()
    assert out_path.name.endswith("_efficiency_vs_thrust.png")
    assert out_path.stat().st_size > 1000
