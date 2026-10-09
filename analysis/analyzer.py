"""Turns one RAW file into one ANALYZED file (docs/ARCHITECTURE.md, "Pipeline" -
Erfassung -> Analyse eines Laufs). Opens the RAW file with "r" only (decision
2: RAW is immutable); never touches hardware.

Unlike RAW, an ANALYZED file is not locked read-only after writing - it is a
derived, regenerable artifact of a RAW file plus config/validation.json, not
irreplaceable primary data. Re-running this on the same RAW file (e.g. after
validation.json changes) is expected to overwrite it.
"""
import argparse
import csv
import dataclasses
import datetime
import pathlib

from analysis import points, validation
from core import calibration, csvio, naming

DEFAULT_OUT_DIR = pathlib.Path(__file__).resolve().parent.parent / "data" / "processed"


@dataclasses.dataclass
class AnalyzeResult:
    """Everything a caller could want from one analyze() call - acquisition/
    session.py uses this directly for its own results display instead of
    re-parsing the ANALYZED file it just wrote."""
    out_path: pathlib.Path
    test_type: str
    build_result: points.BuildResult
    run_status: str
    run_findings: list


CSV_COLUMNS = [
    "stage_promille", "bin_start_s", "directions_used", "n_up", "n_down",
    "n_thrust", "n_current",
    "thrust_g", "thrust_g_delta", "thrust_g_delta_pct", "thrust_g_sd_within",
    "current_a", "current_a_delta", "current_a_delta_pct", "current_a_sd_within",
    "voltage_v", "rpm", "power_w", "g_per_w",
    "temp_c_up", "temp_c_down", "dT",
    "status", "findings",
]


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("raw_path", type=pathlib.Path)
    parser.add_argument(
        "--out-dir", type=pathlib.Path, default=None,
        help=f"Target directory for the ANALYZED file (default: {DEFAULT_OUT_DIR})",
    )
    return parser.parse_args()


def _fmt(value):
    return "" if value is None else value


def _build_header(raw_path, raw_header, cal, build_result, run_status, run_findings):
    if cal is not None:
        calibration_lines = [
            f"calibration_counts_per_gram={cal.counts_per_gram}",
            f"calibration_raw_zero={cal.raw_zero}",
            f"calibration_sha256={csvio.sha256_file(calibration.CONFIG_PATH)}",
        ]
    else:
        calibration_lines = [
            "calibration_counts_per_gram=",
            "calibration_raw_zero=",
            "calibration_sha256=",
        ]

    header = [
        "schema_version=1",
        f"evaluated_at={datetime.datetime.now().isoformat()}",
        f"tool_version={csvio.TOOL_VERSION}",
        f"raw_source_file={raw_path.name}",
        f"raw_source_sha256={csvio.sha256_file(raw_path)}",
        *calibration_lines,
        "validation_file=config/validation.json",
        f"validation_sha256={csvio.sha256_file(validation.DEFAULT_CONFIG_PATH)}",
        f"run_status={run_status}",
        f"run_findings={csvio.findings_to_json(run_findings)}",
    ]
    # The RAW header's own fields, verbatim, "raw_"-prefixed so nothing here
    # collides with the ANALYZED-specific fields above (both files have a
    # schema_version/tool_version/calibration_sha256, meaning different
    # things at each stage) - decision 8: a report built from ANALYZED alone
    # must not lose the RAW-stage documentation (mode, params, propeller...).
    for key, value in raw_header.items():
        header.append(f"raw_{key}={value}")
    return header


def _write_point_row(writer, p):
    writer.writerow([
        p.stage_promille, _fmt(p.bin_start_s), p.directions_used or "",
        _fmt(p.n_up), _fmt(p.n_down), p.n_thrust, p.n_current,
        _fmt(p.thrust_g), _fmt(p.thrust_g_delta), _fmt(p.thrust_g_delta_pct), _fmt(p.thrust_g_sd_within),
        _fmt(p.current_a), _fmt(p.current_a_delta), _fmt(p.current_a_delta_pct), _fmt(p.current_a_sd_within),
        _fmt(p.voltage_v), _fmt(p.rpm), _fmt(p.power_w), _fmt(p.g_per_w),
        _fmt(p.temp_c_up), _fmt(p.temp_c_down), _fmt(p.dT),
        p.status, csvio.findings_to_json(p.findings),
    ])


def analyze(raw_path: pathlib.Path, out_dir: pathlib.Path = DEFAULT_OUT_DIR,
            quiet: bool = False) -> AnalyzeResult:
    """quiet=True suppresses the terminal summary - needed by
    acquisition/session.py, which owns the whole terminal via Textual and
    would otherwise have stray print()s corrupt its display (same reason
    recorder.run_one_cycle() has a quiet flag)."""
    raw_header = csvio.read_header(raw_path)
    test_type, static_promille = naming.parse_mode_value(raw_header["mode"])

    val_cfg = validation.load_config()
    validation.report_unconfigured(val_cfg, test_type)

    cal = calibration.try_load()
    bin_seconds = val_cfg["static"]["bin_seconds"] if test_type == "static" else None
    build_result = points.build_points(
        raw_path, cal, test_type, static_promille=static_promille, bin_seconds=bin_seconds,
    )

    validation.validate_points(build_result.points, val_cfg, test_type, build_result.countdown_voltage_v)
    run_status, run_findings = validation.validate_run(build_result, val_cfg, test_type)

    raw_parts = naming.parse_run_filename(raw_path.name)
    analyzed_parts = dataclasses.replace(raw_parts, file_stage="ANALYZED")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / naming.build_run_filename(analyzed_parts)

    header = _build_header(raw_path, raw_header, cal, build_result, run_status, run_findings)
    with open(out_path, "w", newline="") as f:
        csvio.write_header(f, header)
        writer = csv.writer(f)
        writer.writerow(CSV_COLUMNS)
        for p in build_result.points:
            _write_point_row(writer, p)

    if not quiet:
        n_valid = sum(1 for p in build_result.points if p.status == validation.STATUS_VALID)
        n_warning = sum(1 for p in build_result.points if p.status == validation.STATUS_WARNING)
        n_invalid = sum(1 for p in build_result.points if p.status == validation.STATUS_INVALID)
        print(f"{len(build_result.points)} points: {n_valid} valid, {n_warning} warning, "
              f"{n_invalid} invalid - run status {run_status}")
        if run_findings:
            for f in run_findings:
                print(f"  run finding: {f.code} ({f.level}): {f.text}")
        print(f"Written: {out_path}")

    return AnalyzeResult(out_path=out_path, test_type=test_type, build_result=build_result,
                          run_status=run_status, run_findings=run_findings)


def main():
    args = parse_args()
    out_dir = args.out_dir if args.out_dir is not None else DEFAULT_OUT_DIR
    analyze(args.raw_path, out_dir)


if __name__ == "__main__":
    main()
