import argparse

from acquisition import link, recorder
from core import csvio, naming


def _make_args():
    return argparse.Namespace(
        propeller="", motor="", supply_voltage_v="", ambient_temp_c="", comment="",
    )


def test_record_one_cycle_simulated(tmp_path):
    """End-to-end smoke test against --simulate's SimulatedLink: one full
    sweep cycle (~20s wall clock, SimulatedLink's compressed but real-time
    pacing) plus the fixed 4s post-run capture window. Slow by unit-test
    standards, but this is the one place that actually exercises the
    ARMED -> buffered MODE/PARAMS -> file-open sequence end to end."""
    conn = link.SimulatedLink()
    conn.send("ID?")
    device_line = link.wait_for_line(conn, lambda l: l.startswith("OK ID"), timeout_s=3.0)
    assert device_line is not None
    conn.send("START")
    link.wait_for_line(conn, lambda l: l == "OK START", timeout_s=2.0)

    path, _rearmed_line = recorder.run_one_cycle(
        conn, device_line, cal=None, args=_make_args(),
        quiet=True, data_dir=tmp_path,
    )

    assert path.parent == tmp_path
    assert path.exists()

    parts = naming.parse_run_filename(path.name)
    assert parts.test_type == "sweep"
    assert parts.stage_promille is None
    assert parts.file_stage == "RAW"

    header = csvio.read_header(path)
    assert header["mode"] == "sweep"
    assert header["params"].startswith("start=150 step=25")
    assert header["device_id"] == device_line
    assert header["calibration_file"] == "none (not created yet)"
    assert header["tool_version"] == csvio.TOOL_VERSION

    # RAW files are locked read-only after close (docs/ARCHITECTURE.md decision 2).
    assert not (path.stat().st_mode & 0o222)

    with open(path) as f:
        lines = f.readlines()
    data_lines = [l for l in lines if not l.startswith("#")]
    # Header row plus at least one D/I/E sample row.
    assert len(data_lines) > 1
    assert data_lines[0].strip() == ",".join(recorder.CSV_COLUMNS)
