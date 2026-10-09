"""Combines several ANALYZED sweep files (same configuration, typically
repeat runs) into one COMPARISON file - the "Comparison of runs" stage
(docs/ARCHITECTURE.md, "Pipeline"). Never imports matplotlib (docs/ARCHITECTURE.md decision 9) -
this module only computes, plotting.py only draws. acquisition/session.py's
Session Summary screen is the other consumer - it renders write_comparison()'s
CompareResult directly and computes nothing itself.

A throttle stage is included in the comparison only if every contributing
run has a usable (VALID or WARNING) point there - docs/ARCHITECTURE.md decision 5:
nothing is silently discarded, so a stage missing or INVALID in even one
run is excluded from the aggregate, with a reason naming which run and why,
rather than patched over by interpolating a substitute value. A stage one
run could not reach (e.g. an early voltage-sag reversal) or flagged INVALID
is itself a sign that stage may be compromised for a fair comparison across
the others too.

static ANALYZED files are out of scope here - there is no meaningful shared
x-axis (bin_start_s) to compare two independent static runs against. A
static-specific comparison is a separate, later piece of work.
"""
import argparse
import csv
import dataclasses
import datetime
import json
import pathlib

from analysis import stats, validation
from core import csvio, naming

DEFAULT_OUT_DIR = pathlib.Path(__file__).resolve().parent.parent / "data" / "comparisons"

# (field prefix, gate this metric's CV% by the thrust noise floor?) - current_a
# has no such floor; thrust_g and g_per_w both become unreliable percentages
# near-zero thrust for the same underlying reason (docs/ARCHITECTURE.md, the V1 2687%
# incident), so both share the same gate, driven by thrust_g's own value.
_METRICS = [("thrust_g", True), ("current_a", False), ("power_w", False), ("g_per_w", True)]

CSV_COLUMNS = ["stage_promille", "n_runs_total", "n_runs_usable", "included", "exclude_reason"]
for _metric, _ in _METRICS:
    CSV_COLUMNS += [f"{_metric}_median", f"{_metric}_sd", f"{_metric}_cv_pct"]
CSV_COLUMNS += ["stage_deviation_pct"]


@dataclasses.dataclass
class SessionSummary:
    """Everything the Session Summary screen's KPI cards and quality block
    need - computed once here, only rendered in acquisition/session.py.
    *_status on the three usable-range KPIs is "VALID" or "WARNING": the UI
    marks a KPI sourced from a WARNING point subtly rather than silently.
    typical_run_deviation_pct/
    max_run_deviation_pct/median_direction_mismatch_pct are None with fewer
    than 2 runs - there is no cross-run comparison to make yet."""
    n_runs: int
    n_points_valid: int
    n_points_warning: int
    n_points_invalid: int
    max_usable_thrust_g: float | None
    max_usable_thrust_stage_promille: int | None
    max_usable_thrust_status: str | None
    peak_usable_power_w: float | None
    peak_usable_power_stage_promille: int | None
    peak_usable_power_status: str | None
    best_g_per_w: float | None
    best_g_per_w_thrust_g: float | None  # thrust at that same point, for "6.4 g/W @ 312 g"
    best_g_per_w_stage_promille: int | None
    best_g_per_w_status: str | None
    typical_run_deviation_pct: float | None
    max_run_deviation_pct: float | None
    median_direction_mismatch_pct: float | None


@dataclasses.dataclass
class CompareResult:
    out_path: pathlib.Path | None  # None for compare_in_memory() - nothing written
    rows: list  # CSV_COLUMNS-shaped dicts - the file's own content
    per_run: list  # [{"path": Path, "by_stage": {stage_promille: row_dict}}, ...]
    summary: SessionSummary


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("analyzed_paths", nargs="+", type=pathlib.Path,
                         help="Two or more ANALYZED sweep files to compare (same "
                              "configuration - this script does not check that).")
    parser.add_argument("--label", required=True,
                         help="Short label for the output file name, e.g. 'prop365-16v'.")
    parser.add_argument("--out-dir", type=pathlib.Path, default=None)
    return parser.parse_args()


def _read_analyzed(path):
    header = csvio.read_header(path)
    test_type, _ = naming.parse_mode_value(header["raw_mode"])
    if test_type != "sweep":
        raise ValueError(f"{path.name}: compare.py only compares sweep runs, got {test_type!r}")
    with open(path, newline="") as f:
        rows = list(csv.DictReader(line for line in f if not line.startswith("#")))
    return {int(r["stage_promille"]): r for r in rows}


def _read_all(analyzed_paths):
    return [_read_analyzed(p) for p in analyzed_paths]


def _usable(row):
    return row["status"] in (validation.STATUS_VALID, validation.STATUS_WARNING)


def _invalid_reason(row):
    findings = json.loads(row["findings"]) if row["findings"] else []
    codes = [f["code"] for f in findings if f["level"] == validation.LEVEL_INVALID]
    return "+".join(codes) if codes else row["status"]


def _numeric(row, field):
    value = row.get(field)
    return float(value) if value else None


def _above_floor(value, floor):
    """floor=None means the rule is unconfigured - docs/ARCHITECTURE.md decision 7:
    nothing gets silently gated on a null threshold, so nothing is excluded
    either; everything counts as "above" it."""
    return floor is None or (value is not None and abs(value) >= floor)


def _build_rows(analyzed_paths, per_file, thrust_relative_floor_g=None):
    """Returns a list of CSV_COLUMNS-shaped dicts, one per stage_promille
    across the union of all input files' stages."""
    n_runs = len(per_file)
    all_stages = sorted(set(s for by_stage in per_file for s in by_stage))

    out_rows = []
    for stage in all_stages:
        usable_values = {metric: [] for metric, _ in _METRICS}
        n_usable = 0
        exclude_parts = []
        for path, by_stage in zip(analyzed_paths, per_file):
            row = by_stage.get(stage)
            if row is None:
                exclude_parts.append(f"{path.name}: stage not reached")
                continue
            if not _usable(row):
                exclude_parts.append(f"{path.name}: {row['status']} ({_invalid_reason(row)})")
                continue
            n_usable += 1
            for metric, _ in _METRICS:
                v = _numeric(row, metric)
                if v is not None:
                    usable_values[metric].append(v)

        included = n_usable == n_runs
        out_row = {
            "stage_promille": stage, "n_runs_total": n_runs, "n_runs_usable": n_usable,
            "included": "yes" if included else "no",
            "exclude_reason": "" if included else "; ".join(exclude_parts),
        }
        for metric, _ in _METRICS:
            out_row[f"{metric}_median"] = out_row[f"{metric}_sd"] = out_row[f"{metric}_cv_pct"] = ""
        out_row["stage_deviation_pct"] = ""  # filled in by _add_stage_deviation()

        if included:
            thrust_median = stats.median(usable_values["thrust_g"]) if usable_values["thrust_g"] else None
            above_floor = _above_floor(thrust_median, thrust_relative_floor_g)
            for metric, gate_by_floor in _METRICS:
                values = usable_values[metric]
                if not values:
                    continue
                median = stats.median(values)
                sd = stats.sd(values)
                out_row[f"{metric}_median"] = median
                out_row[f"{metric}_sd"] = "" if sd is None else sd
                # CV% stays blank near the noise floor - a small absolute
                # spread divided by a near-zero median explodes into a
                # meaningless percentage (docs/ARCHITECTURE.md - the V1 2687% incident).
                if median and sd is not None and (not gate_by_floor or above_floor):
                    out_row[f"{metric}_cv_pct"] = sd / abs(median) * 100
        out_rows.append(out_row)

    return out_rows


def build_comparison(analyzed_paths, thrust_relative_floor_g=None):
    """Public convenience wrapper around _build_rows() - reads the files
    itself. write_comparison()/compare_in_memory() read once and reuse the
    result for both the rows and the session summary instead of calling
    this. Every CSV_COLUMNS key is always present (even "" when not
    computed) so callers can hand these rows straight to a
    csv.DictWriter(fieldnames=CSV_COLUMNS) - hence the stage_deviation_pct
    pass here too, not just in _compute()."""
    per_file = _read_all(analyzed_paths)
    rows = _build_rows(analyzed_paths, per_file, thrust_relative_floor_g)
    _add_stage_deviation(rows, per_file, thrust_relative_floor_g)
    return rows


def _stage_deviation(stage_median, run_values):
    """Median absolute relative deviation of each run's own value from the
    stage median - a more legible, robust repeatability figure than the
    pooled CV: not affected by within-run
    sample-count weighting, one number per stage regardless of how many
    samples backed each run's own median."""
    if not run_values or not stage_median:
        return None
    devs = [abs(v - stage_median) / abs(stage_median) * 100 for v in run_values]
    return stats.median(devs)


def _add_stage_deviation(rows, per_file, thrust_relative_floor_g) -> None:
    """Fills in each included row's stage_deviation_pct in place - one
    robust number per stage (see _stage_deviation()), persisted in the file
    (feeds the Session Summary's Repeatability plot and quality block in
    acquisition/session.py, not just a throwaway) rather than only living
    inside summarize()'s aggregate. Needs at least 2 runs; stays "" with
    only one (nothing to deviate from)."""
    if len(per_file) < 2:
        return
    for row in rows:
        if row["included"] != "yes":
            continue
        stage_median = row.get("thrust_g_median")
        if stage_median in (None, ""):
            continue
        stage_median = float(stage_median)
        if not _above_floor(stage_median, thrust_relative_floor_g):
            continue
        run_values = []
        for by_stage in per_file:
            r = by_stage.get(row["stage_promille"])
            if r is None:
                continue
            t = _numeric(r, "thrust_g")
            if t is not None:
                run_values.append(t)
        dev = _stage_deviation(stage_median, run_values)
        if dev is not None:
            row["stage_deviation_pct"] = dev


def summarize(analyzed_paths, per_file, rows, thrust_relative_floor_g) -> SessionSummary:
    n_runs = len(analyzed_paths)
    n_valid = n_warning = n_invalid = 0
    max_thrust = peak_power = best_eff = None  # (value, [thrust_g,] stage, status)
    per_run_direction_medians = []

    for by_stage in per_file:
        run_abs_deltas = []
        for stage, row in by_stage.items():
            status = row["status"]
            if status == validation.STATUS_VALID:
                n_valid += 1
            elif status == validation.STATUS_WARNING:
                n_warning += 1
            else:
                n_invalid += 1
                continue  # INVALID never counts toward the usable-range KPIs below

            thrust = _numeric(row, "thrust_g")
            power = _numeric(row, "power_w")
            eff = _numeric(row, "g_per_w")

            if thrust is not None and (max_thrust is None or thrust > max_thrust[0]):
                max_thrust = (thrust, stage, status)
            if power is not None and (peak_power is None or power > peak_power[0]):
                peak_power = (power, stage, status)
            if (eff is not None and thrust is not None
                    and _above_floor(thrust, thrust_relative_floor_g)
                    and (best_eff is None or eff > best_eff[0])):
                best_eff = (eff, thrust, stage, status)

            delta_pct = _numeric(row, "thrust_g_delta_pct")
            if delta_pct is not None and _above_floor(thrust, thrust_relative_floor_g):
                run_abs_deltas.append(abs(delta_pct))

        run_median = stats.median(run_abs_deltas)
        if run_median is not None:
            per_run_direction_medians.append(run_median)

    median_direction_mismatch_pct = stats.median(per_run_direction_medians)

    # stage_deviation_pct is filled in by _add_stage_deviation() (called from
    # _compute(), before summarize() runs) - reused here rather than
    # recomputed, so the file's own column and this aggregate can never
    # disagree. Empty with fewer than 2 runs - no cross-run comparison to
    # make at all - stays None, not a trivial 0%.
    stage_deviations = [row["stage_deviation_pct"] for row in rows
                         if row["included"] == "yes" and row["stage_deviation_pct"] != ""]
    typical_dev = stats.median(stage_deviations) if n_runs >= 2 else None
    max_dev = max(stage_deviations) if (n_runs >= 2 and stage_deviations) else None

    return SessionSummary(
        n_runs=n_runs,
        n_points_valid=n_valid, n_points_warning=n_warning, n_points_invalid=n_invalid,
        max_usable_thrust_g=max_thrust[0] if max_thrust else None,
        max_usable_thrust_stage_promille=max_thrust[1] if max_thrust else None,
        max_usable_thrust_status=max_thrust[2] if max_thrust else None,
        peak_usable_power_w=peak_power[0] if peak_power else None,
        peak_usable_power_stage_promille=peak_power[1] if peak_power else None,
        peak_usable_power_status=peak_power[2] if peak_power else None,
        best_g_per_w=best_eff[0] if best_eff else None,
        best_g_per_w_thrust_g=best_eff[1] if best_eff else None,
        best_g_per_w_stage_promille=best_eff[2] if best_eff else None,
        best_g_per_w_status=best_eff[3] if best_eff else None,
        typical_run_deviation_pct=typical_dev,
        max_run_deviation_pct=max_dev,
        median_direction_mismatch_pct=median_direction_mismatch_pct,
    )


def _compute(analyzed_paths):
    """Shared by write_comparison() and compare_in_memory(): reads every
    file once, builds the per-stage rows and the session summary."""
    val_cfg = validation.load_config()
    floor = val_cfg["common"]["thrust_relative_floor_g"]
    per_file = _read_all(analyzed_paths)
    rows = _build_rows(analyzed_paths, per_file, thrust_relative_floor_g=floor)
    _add_stage_deviation(rows, per_file, thrust_relative_floor_g=floor)
    summary = summarize(analyzed_paths, per_file, rows, thrust_relative_floor_g=floor)
    per_run = [{"path": p, "by_stage": bs} for p, bs in zip(analyzed_paths, per_file)]
    return rows, per_run, summary


def compare_in_memory(analyzed_paths) -> CompareResult:
    """Like write_comparison() but writes nothing - for a PARTIAL look at an
    in-progress session (acquisition/session.py's Session Summary screen
    before the last repetition is done). Calling this repeatedly as more
    runs complete must not litter data/comparisons/ with a new timestamped
    file every time the user just wants to check progress; the FINAL
    summary, once every repetition is done, goes through write_comparison()
    instead for one real, traceable file."""
    rows, per_run, summary = _compute(analyzed_paths)
    return CompareResult(out_path=None, rows=rows, per_run=per_run, summary=summary)


def write_comparison(analyzed_paths, label, out_dir=DEFAULT_OUT_DIR) -> CompareResult:
    rows, per_run, summary = _compute(analyzed_paths)

    timestamp = datetime.datetime.now()
    filename = naming.build_comparison_filename(
        naming.ComparisonFilenameParts(timestamp=timestamp, label=label))
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / filename

    header = [
        "schema_version=1",
        f"compared_at={timestamp.isoformat()}",
        f"tool_version={csvio.TOOL_VERSION}",
        f"label={label}",
        f"source_count={len(analyzed_paths)}",
    ]
    for i, path in enumerate(analyzed_paths, start=1):
        header.append(f"source_{i}_file={path.name}")
        header.append(f"source_{i}_sha256={csvio.sha256_file(path)}")

    with open(out_path, "w", newline="") as f:
        csvio.write_header(f, header)
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)

    n_included = sum(1 for r in rows if r["included"] == "yes")
    print(f"{len(rows)} stages: {n_included} included, {len(rows) - n_included} excluded")
    print(f"Max usable thrust: {summary.max_usable_thrust_g} g, "
          f"peak usable power: {summary.peak_usable_power_w} W")
    print(f"Written: {out_path}")

    return CompareResult(out_path=out_path, rows=rows, per_run=per_run, summary=summary)


def main():
    args = parse_args()
    if len(args.analyzed_paths) < 2:
        raise SystemExit("Need at least two ANALYZED files to compare.")
    out_dir = args.out_dir if args.out_dir is not None else DEFAULT_OUT_DIR
    write_comparison(args.analyzed_paths, args.label, out_dir)


if __name__ == "__main__":
    main()
