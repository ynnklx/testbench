"""testbench.py's dispatch - via subprocess (exactly how a user invokes it),
not by importing and mutating sys.argv in-process, which would risk leaking
state into other tests. Only checks that dispatch and argv-rewriting work;
each subcommand's own logic is already covered by that module's own tests
(test_recorder.py, test_analyzer.py, ...)."""
import pathlib
import subprocess
import sys

from core import calibration
from tests.test_analyzer import _write_sweep_raw

TESTBENCH_PY = pathlib.Path(__file__).resolve().parent.parent / "testbench.py"


def _run(*args, timeout=10):
    return subprocess.run([sys.executable, str(TESTBENCH_PY), *args],
                           capture_output=True, text=True, timeout=timeout)


def test_no_command_prints_usage_and_fails():
    result = _run()
    assert result.returncode == 1
    assert "usage: testbench.py" in result.stderr
    assert "record" in result.stderr and "analyze" in result.stderr


def test_top_level_help():
    result = _run("--help")
    assert result.returncode == 0
    assert "usage: testbench.py" in result.stdout


def test_unknown_command_fails_with_usage():
    result = _run("frobnicate")
    assert result.returncode == 1
    assert "Unknown command: 'frobnicate'" in result.stderr
    assert "usage: testbench.py" in result.stderr


def test_delegated_help_shows_the_real_subcommand_parser():
    result = _run("analyze", "--help")
    assert result.returncode == 0
    assert "testbench.py analyze" in result.stdout  # prog name rewritten, not "analyzer.py"
    assert "raw_path" in result.stdout  # analyzer.py's own positional argument
    assert "--out-dir" in result.stdout


def test_analyze_end_to_end_through_dispatch(tmp_path):
    """One real functional check that the dispatch actually runs the target
    module's main(), not just its --help path."""
    cal = calibration.try_load()
    raw_path = tmp_path / "raw" / "2026-09-11_10-00-00_sweep_RAW.csv"
    raw_path.parent.mkdir()
    _write_sweep_raw(raw_path, cal, stage_grams={150: 20.0, 175: 25.0},
                      stage_amps={150: 1.0, 175: 1.2})

    out_dir = tmp_path / "processed"
    result = _run("analyze", str(raw_path), "--out-dir", str(out_dir))
    assert result.returncode == 0, result.stderr
    assert "Written:" in result.stdout

    out_files = list(out_dir.glob("*_ANALYZED.csv"))
    assert len(out_files) == 1
