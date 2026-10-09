"""Applies config/validation.json to a run's points - VALID/WARNING/INVALID
per point (docs/ARCHITECTURE.md, "Validation"), plus whole-run findings that do not
belong to any one point (ZERO_REFERENCE_DRIFT, RUN_DRIFT - stored as
run_findings/run_status in the ANALYZED header, not a CSV row).

A rule whose threshold is still null is skipped, never silently run against
a default (docs/ARCHITECTURE.md decision 7) - report_unconfigured() prints each once,
up front, so a run's stderr says exactly which rules did not fire and why.
Only four codes may ever reach LEVEL_INVALID: SENSOR_ERROR, OUT_OF_RANGE,
CURRENT_LIMIT_EXCEEDED, INSUFFICIENT_SAMPLES (severe form) - every other
rule's 'invalid_*' field in validation.json is expected to stay null
forever, by design, not because nobody got around to deriving it yet;
_finding() asserts this so a mistake here fails loudly instead of quietly
producing an INVALID a rule was never supposed to raise.
"""
import dataclasses
import enum
import json
import pathlib
import sys

from analysis import stats

DEFAULT_CONFIG_PATH = pathlib.Path(__file__).resolve().parent.parent / "config" / "validation.json"

# Nominal HX711 rate (docs/ARCHITECTURE.md: "Die Nennrate des HX711 (10 SPS) wird nie
# als Zeitbasis angenommen" - true for timestamps, but the insufficient_
# samples rule needs *some* documented baseline for what "expected" means,
# and validation.json's own reason field already names this convention.
NOMINAL_HX711_SPS = 10

LEVEL_WARNING = "warning"
LEVEL_INVALID = "invalid"

STATUS_VALID = "VALID"
STATUS_WARNING = "WARNING"
STATUS_INVALID = "INVALID"

_STATUS_RANK = {STATUS_VALID: 0, STATUS_WARNING: 1, STATUS_INVALID: 2}
_LEVEL_TO_STATUS = {LEVEL_WARNING: STATUS_WARNING, LEVEL_INVALID: STATUS_INVALID}


class FindingCode(str, enum.Enum):
    SENSOR_ERROR = "SENSOR_ERROR"
    OUT_OF_RANGE = "OUT_OF_RANGE"
    CURRENT_LIMIT_EXCEEDED = "CURRENT_LIMIT_EXCEEDED"
    CURRENT_LIMIT_ACTIVE = "CURRENT_LIMIT_ACTIVE"
    TEMPERATURE_UNAVAILABLE = "TEMPERATURE_UNAVAILABLE"
    INSUFFICIENT_SAMPLES = "INSUFFICIENT_SAMPLES"
    HIGH_VARIANCE = "HIGH_VARIANCE"
    UNSTABLE_MEASUREMENT = "UNSTABLE_MEASUREMENT"
    SETTLING_INCOMPLETE = "SETTLING_INCOMPLETE"
    TEMPERATURE_DRIFT = "TEMPERATURE_DRIFT"
    ZERO_REFERENCE_DRIFT = "ZERO_REFERENCE_DRIFT"  # run-level
    DIRECTION_MISMATCH = "DIRECTION_MISMATCH"  # sweep
    SINGLE_DIRECTION_ONLY = "SINGLE_DIRECTION_ONLY"  # sweep
    BELOW_NOISE_FLOOR = "BELOW_NOISE_FLOOR"  # sweep
    NON_MONOTONIC = "NON_MONOTONIC"  # sweep
    RUN_DRIFT = "RUN_DRIFT"  # static, run-level


_INVALID_CAPABLE = {
    FindingCode.SENSOR_ERROR, FindingCode.OUT_OF_RANGE,
    FindingCode.CURRENT_LIMIT_EXCEEDED, FindingCode.INSUFFICIENT_SAMPLES,
}

_COMMON_RULES = {
    "current_limit_exceeded", "current_limit_active", "sensor_error",
    "temperature_unavailable", "out_of_range", "insufficient_samples",
    "high_variance", "unstable_measurement", "settling_incomplete",
    "temperature_drift", "thrust_relative_floor_g", "zero_reference_drift",
}
_SWEEP_RULES = {"insufficient_samples", "direction_mismatch",
                "single_direction_only", "below_noise_floor", "non_monotonic"}
_STATIC_RULES = {"insufficient_samples", "bin_seconds", "run_drift"}


@dataclasses.dataclass
class Finding:
    code: str
    level: str
    value: float | int | str | None
    threshold: float | int | str | None
    text: str


def _finding(code: FindingCode, level: str, value, threshold, text: str) -> Finding:
    assert level != LEVEL_INVALID or code in _INVALID_CAPABLE, (
        f"{code} must never reach LEVEL_INVALID (docs/ARCHITECTURE.md decision 7)"
    )
    return Finding(code=code.value, level=level, value=value, threshold=threshold, text=text)


def _status_from_findings(findings) -> str:
    status = STATUS_VALID
    for f in findings:
        candidate = _LEVEL_TO_STATUS[f.level]
        if _STATUS_RANK[candidate] > _STATUS_RANK[status]:
            status = candidate
    return status


def check_known_rules(cfg: dict) -> None:
    """docs/ARCHITECTURE.md decision 7: a rule key in validation.json this code does
    not know is a bug - either a typo in the config or this file falling
    behind it - not a silently ignored entry."""
    unknown = {
        "common": set(cfg.get("common", {})) - _COMMON_RULES,
        "sweep": set(cfg.get("sweep", {})) - _SWEEP_RULES,
        "static": set(cfg.get("static", {})) - _STATIC_RULES,
    }
    if any(unknown.values()):
        raise ValueError(f"config/validation.json has unrecognised rule keys: {unknown}")


def load_config(path=DEFAULT_CONFIG_PATH) -> dict:
    with open(path) as f:
        cfg = json.load(f)
    check_known_rules(cfg)
    return cfg


def report_unconfigured(cfg: dict, test_type: str) -> None:
    """One stderr line per still-null threshold relevant to test_type -
    docs/ARCHITECTURE.md decision 7. Call once per analyzer.py run, not once per
    point - the same rule would otherwise print dozens of times."""
    common = cfg["common"]
    checks = [
        ("common.out_of_range.voltage_min_v", common["out_of_range"]["voltage_min_v"]),
        ("common.out_of_range.thrust_max_g", common["out_of_range"]["thrust_max_g"]),
        ("common.out_of_range.temp_min_c", common["out_of_range"]["temp_min_c"]),
        ("common.out_of_range.temp_max_c", common["out_of_range"]["temp_max_c"]),
        ("common.insufficient_samples.warning_below_fraction",
         common["insufficient_samples"]["warning_below_fraction"]),
        ("common.insufficient_samples.invalid_below_fraction",
         common["insufficient_samples"]["invalid_below_fraction"]),
        ("common.high_variance.warning_above_rel", common["high_variance"]["warning_above_rel"]),
        ("common.high_variance.warning_above_abs_g", common["high_variance"]["warning_above_abs_g"]),
        ("common.unstable_measurement.mad_k", common["unstable_measurement"]["mad_k"]),
        ("common.unstable_measurement.warning_above_count",
         common["unstable_measurement"]["warning_above_count"]),
        ("common.unstable_measurement.warning_above_consecutive",
         common["unstable_measurement"]["warning_above_consecutive"]),
        ("common.settling_incomplete.warning_above_mad_multiple",
         common["settling_incomplete"]["warning_above_mad_multiple"]),
        ("common.temperature_drift.warning_above_delta_c",
         common["temperature_drift"]["warning_above_delta_c"]),
        ("common.temperature_drift.warning_above_abs_c",
         common["temperature_drift"]["warning_above_abs_c"]),
        ("common.thrust_relative_floor_g", common["thrust_relative_floor_g"]),
        ("common.zero_reference_drift.warning_above_g", common["zero_reference_drift"]["warning_above_g"]),
    ]
    if test_type == "sweep":
        sw = cfg["sweep"]
        checks += [
            ("sweep.direction_mismatch.warning_above_pct", sw["direction_mismatch"]["warning_above_pct"]),
            ("sweep.non_monotonic.warning_above_sd_multiple", sw["non_monotonic"]["warning_above_sd_multiple"]),
        ]
    else:
        checks.append(("static.run_drift.warning_above_pct_per_min",
                        cfg["static"]["run_drift"]["warning_above_pct_per_min"]))
    for name, value in checks:
        if value is None:
            print(f"validation: {name} is unconfigured (null) - rule skipped", file=sys.stderr)


def validate_point(point, cfg, test_type, countdown_voltage_v=None,
                    is_peak=False, prev_point=None) -> None:
    """Evaluates every per-point rule, setting point.status/point.findings.
    prev_point (the point at the next-lower stage_promille) drives
    NON_MONOTONIC (sweep only); without one, that check just does not fire."""
    findings = []
    common = cfg["common"]

    # SENSOR_ERROR (invalid) - no threshold, an all-or-nothing check.
    if point.n_thrust == 0:
        findings.append(_finding(FindingCode.SENSOR_ERROR, LEVEL_INVALID,
                                  0, 1, "No thrust sample in the window."))
    if point.n_current == 0:
        findings.append(_finding(FindingCode.SENSOR_ERROR, LEVEL_INVALID,
                                  0, 1, "No current sample in the window."))

    # OUT_OF_RANGE (invalid) - RPM is never checked (docs/ARCHITECTURE.md).
    oor = common["out_of_range"]
    if point.voltage_v is not None and oor["voltage_min_v"] is not None \
            and point.voltage_v < oor["voltage_min_v"]:
        findings.append(_finding(FindingCode.OUT_OF_RANGE, LEVEL_INVALID,
                                  point.voltage_v, oor["voltage_min_v"],
                                  "Bus voltage below plausible minimum - V+/V- likely not measuring."))
    if point.thrust_g is not None and oor["thrust_max_g"] is not None \
            and point.thrust_g > oor["thrust_max_g"]:
        findings.append(_finding(FindingCode.OUT_OF_RANGE, LEVEL_INVALID,
                                  point.thrust_g, oor["thrust_max_g"],
                                  "Thrust above the load cell's rated capacity."))
    for label, temp in (("up", point.temp_c_up), ("down", point.temp_c_down)):
        if temp is None:
            continue
        if oor["temp_min_c"] is not None and temp < oor["temp_min_c"]:
            findings.append(_finding(FindingCode.OUT_OF_RANGE, LEVEL_INVALID,
                                      temp, oor["temp_min_c"],
                                      f"ESC temperature ({label}) below a physically plausible minimum."))
        if oor["temp_max_c"] is not None and temp > oor["temp_max_c"]:
            findings.append(_finding(FindingCode.OUT_OF_RANGE, LEVEL_INVALID,
                                      temp, oor["temp_max_c"],
                                      f"ESC temperature ({label}) at/above the firmware's own abort threshold."))

    cle = common["current_limit_exceeded"]
    cla = common["current_limit_active"]
    ins_common = common["insufficient_samples"]
    ins_scope = (cfg["sweep"] if test_type == "sweep" else cfg["static"])["insufficient_samples"]
    expected_thrust = ins_scope["window_ms"] / 1000.0 * NOMINAL_HX711_SPS

    for label, d in point.directions.items():
        if d is None:
            continue

        # CURRENT_LIMIT_EXCEEDED (invalid) / CURRENT_LIMIT_ACTIVE (warning) -
        # CC reference is the countdown voltage at a stopped motor, not the
        # run's own max (docs/ARCHITECTURE.md, known pitfalls - V1's version was circular).
        if d.current_a_median is not None and cle["current_invalid_above_a"] is not None \
                and d.current_a_median > cle["current_invalid_above_a"]:
            findings.append(_finding(FindingCode.CURRENT_LIMIT_EXCEEDED, LEVEL_INVALID,
                                      d.current_a_median, cle["current_invalid_above_a"],
                                      f"Current ({label}) above the invalidation limit."))
        if cla["cc_voltage_fraction"] is not None and d.voltage_v_median is not None and countdown_voltage_v:
            ref = countdown_voltage_v * cla["cc_voltage_fraction"]
            if d.voltage_v_median < ref:
                findings.append(_finding(FindingCode.CURRENT_LIMIT_ACTIVE, LEVEL_WARNING,
                                          d.voltage_v_median, ref,
                                          f"Voltage ({label}) below the constant-current fraction of the "
                                          "countdown reference voltage."))

        # TEMPERATURE_UNAVAILABLE (warning) - missing E line costs the
        # temperature, not the measurement (docs/ARCHITECTURE.md).
        if d.n_rpm == 0:
            findings.append(_finding(FindingCode.TEMPERATURE_UNAVAILABLE, LEVEL_WARNING,
                                      0, 1, f"No E line ({label}) - temperature unavailable."))

        # INSUFFICIENT_SAMPLES (severe form: invalid)
        if expected_thrust:
            frac = d.n_thrust / expected_thrust
            inv_frac = ins_common["invalid_below_fraction"]
            warn_frac = ins_common["warning_below_fraction"]
            if inv_frac is not None and frac < inv_frac:
                findings.append(_finding(FindingCode.INSUFFICIENT_SAMPLES, LEVEL_INVALID,
                                          d.n_thrust, round(expected_thrust * inv_frac),
                                          f"Thrust sample count ({label}) far below the expected count."))
            elif warn_frac is not None and frac < warn_frac:
                findings.append(_finding(FindingCode.INSUFFICIENT_SAMPLES, LEVEL_WARNING,
                                          d.n_thrust, round(expected_thrust * warn_frac),
                                          f"Thrust sample count ({label}) below the expected count."))

        # HIGH_VARIANCE (warning only - never one of the four invalidating codes)
        hv = common["high_variance"]
        floor = common["thrust_relative_floor_g"]
        if d.thrust_g_median is not None and floor is not None:
            if abs(d.thrust_g_median) >= floor:
                cv = stats.robust_cv_pct(d.thrust_g_samples)
                thr = hv["warning_above_rel"]
                if cv is not None and thr is not None and abs(cv) > thr:
                    findings.append(_finding(FindingCode.HIGH_VARIANCE, LEVEL_WARNING,
                                              cv, thr, f"Thrust CV ({label}) above the relative threshold."))
            else:
                thr = hv["warning_above_abs_g"]
                if thr is not None and d.thrust_g_mad is not None and d.thrust_g_mad > thr:
                    findings.append(_finding(FindingCode.HIGH_VARIANCE, LEVEL_WARNING,
                                              d.thrust_g_mad, thr,
                                              f"Thrust MAD ({label}) above the absolute threshold near "
                                              "the noise floor."))

        # UNSTABLE_MEASUREMENT (warning only)
        um = common["unstable_measurement"]
        if um["mad_k"] is not None:
            count, consecutive = stats.count_mad_outliers(d.thrust_g_samples, um["mad_k"])
            warn_count = um["warning_above_count"]
            warn_consec = um["warning_above_consecutive"]
            if warn_count is not None and count > warn_count:
                findings.append(_finding(FindingCode.UNSTABLE_MEASUREMENT, LEVEL_WARNING,
                                          count, warn_count,
                                          f"Outlier sample count ({label}) above threshold."))
            elif warn_consec is not None and consecutive > warn_consec:
                findings.append(_finding(FindingCode.UNSTABLE_MEASUREMENT, LEVEL_WARNING,
                                          consecutive, warn_consec,
                                          f"Consecutive outlier run ({label}) above threshold."))

        # SETTLING_INCOMPLETE (warning only)
        si = common["settling_incomplete"]
        if si["warning_above_mad_multiple"] is not None and d.thrust_g_settle_ratio is not None:
            thr = si["warning_above_mad_multiple"]
            if d.thrust_g_settle_ratio > thr:
                findings.append(_finding(FindingCode.SETTLING_INCOMPLETE, LEVEL_WARNING,
                                          d.thrust_g_settle_ratio, thr,
                                          f"First/last third mismatch ({label}) suggests incomplete settling."))

    # TEMPERATURE_DRIFT (warning only) - relevant to interpretation, never a
    # reason to discard a reading.
    td = common["temperature_drift"]
    if point.dT is not None and td["warning_above_delta_c"] is not None \
            and abs(point.dT) > td["warning_above_delta_c"]:
        findings.append(_finding(FindingCode.TEMPERATURE_DRIFT, LEVEL_WARNING,
                                  point.dT, td["warning_above_delta_c"],
                                  "Temperature delta (down-up) above threshold."))
    for label, temp in (("up", point.temp_c_up), ("down", point.temp_c_down)):
        if temp is not None and td["warning_above_abs_c"] is not None and temp > td["warning_above_abs_c"]:
            findings.append(_finding(FindingCode.TEMPERATURE_DRIFT, LEVEL_WARNING,
                                      temp, td["warning_above_abs_c"],
                                      f"ESC temperature ({label}) above the absolute threshold."))

    if test_type == "sweep":
        sw = cfg["sweep"]

        # DIRECTION_MISMATCH - quality indicator, must never become invalid
        # (invalid_above_pct stays null structurally, by design).
        # warning_min_thrust_g gates the percentage check itself: at very low
        # but real thrust (the lowest sweep stage, not near-zero enough for
        # thrust_relative_floor_g to apply) a tiny, unremarkable absolute
        # delta still produces a large percentage - confirmed reproducible
        # across five independent runs, not a data quality problem at that
        # one stage. Below the gate, delta_pct simply
        # is not evaluated - mirrors V1's own PLAUSIBLE_THRUST_MIN_G, which
        # excluded exactly this kind of pair from its comparison the same way.
        dm = sw["direction_mismatch"]
        min_thrust = dm["warning_min_thrust_g"]
        above_min_thrust = min_thrust is None or (point.thrust_g is not None and abs(point.thrust_g) >= min_thrust)
        if above_min_thrust and dm["warning_above_pct"] is not None and point.thrust_g_delta_pct is not None \
                and abs(point.thrust_g_delta_pct) > dm["warning_above_pct"]:
            findings.append(_finding(FindingCode.DIRECTION_MISMATCH, LEVEL_WARNING,
                                      point.thrust_g_delta_pct, dm["warning_above_pct"],
                                      "Up/down thrust spread above threshold."))

        # SINGLE_DIRECTION_ONLY - not raised for the peak stage, visited
        # once by design.
        if not is_peak and point.directions_used in ("up", "down"):
            findings.append(_finding(FindingCode.SINGLE_DIRECTION_ONLY, LEVEL_WARNING,
                                      None, None,
                                      f"Only the {point.directions_used} leg produced a usable "
                                      "reading at this stage."))

        # BELOW_NOISE_FLOOR - not invalid, a decoupled validation run is
        # supposed to read zero.
        floor = common["thrust_relative_floor_g"]
        if floor is not None and point.thrust_g is not None and abs(point.thrust_g) < floor:
            findings.append(_finding(FindingCode.BELOW_NOISE_FLOOR, LEVEL_WARNING,
                                      point.thrust_g, floor, "Thrust below the noise floor."))

        # NON_MONOTONIC - thrust falling against the lower neighbouring
        # stage beyond its own spread.
        nm = sw["non_monotonic"]
        thr = nm["warning_above_sd_multiple"]
        if thr is not None and prev_point is not None and point.thrust_g is not None \
                and prev_point.thrust_g is not None and point.thrust_g_sd_within:
            if point.thrust_g < prev_point.thrust_g:
                sd_multiple = (prev_point.thrust_g - point.thrust_g) / point.thrust_g_sd_within
                if sd_multiple > thr:
                    findings.append(_finding(FindingCode.NON_MONOTONIC, LEVEL_WARNING,
                                              sd_multiple, thr,
                                              "Thrust fell against the lower neighbouring stage."))

    point.findings = findings
    point.status = _status_from_findings(findings)


def validate_points(points, cfg, test_type, countdown_voltage_v=None) -> None:
    """Validates every point in place, in ascending stage_promille order -
    the order sorted-by-stage points already come in from points.py -
    needed for NON_MONOTONIC's neighbour comparison and for finding the
    peak stage (SINGLE_DIRECTION_ONLY)."""
    peak_stage = max((p.stage_promille for p in points), default=None) if test_type == "sweep" else None
    prev = None
    for point in points:
        validate_point(point, cfg, test_type, countdown_voltage_v,
                        is_peak=(point.stage_promille == peak_stage), prev_point=prev)
        prev = point


def validate_run(build_result, cfg, test_type) -> tuple:
    """Whole-run findings that do not belong to any one point (docs/ARCHITECTURE.md,
    "Validation"): ZERO_REFERENCE_DRIFT (both test types, from
    the post-run capture window - never a Point of its own) and RUN_DRIFT
    (static only, thrust/current drift across the run's own bin points).
    Returns (run_status, run_findings)."""
    findings = []
    common = cfg["common"]

    zrd = common["zero_reference_drift"]
    window = build_result.post_run_window
    if window is not None and window.thrust_g_median is not None and zrd["warning_above_g"] is not None:
        thr = zrd["warning_above_g"]
        if abs(window.thrust_g_median) > thr:
            findings.append(_finding(FindingCode.ZERO_REFERENCE_DRIFT, LEVEL_WARNING,
                                      window.thrust_g_median, thr,
                                      "Post-run at-rest thrust drifted away from zero."))

    if test_type == "static":
        rd = cfg["static"]["run_drift"]
        thr = rd["warning_above_pct_per_min"]
        if thr is not None:
            for field, label in (("thrust_g", "Thrust"), ("current_a", "Current")):
                paired = [(p.bin_start_s / 60.0, getattr(p, field)) for p in build_result.points
                          if p.bin_start_s is not None and getattr(p, field) is not None]
                if len(paired) < 2:
                    continue
                fit = stats.linreg([x for x, _ in paired], [y for _, y in paired])
                if fit is None:
                    continue
                mean_v = sum(y for _, y in paired) / len(paired)
                pct_per_min = (fit["slope"] / mean_v * 100) if mean_v else None
                if pct_per_min is not None and abs(pct_per_min) > thr:
                    findings.append(_finding(FindingCode.RUN_DRIFT, LEVEL_WARNING,
                                              pct_per_min, thr, f"{label} drift rate above threshold."))

    return _status_from_findings(findings), findings
