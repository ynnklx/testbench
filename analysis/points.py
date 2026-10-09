"""Builds merged measurement points from one RAW file - the "Measurement points" step (docs/ARCHITECTURE.md). No validation here (see validation.py):
this module only builds points and the raw diagnostic material validation.py
needs (per-direction sample lists, MAD, settle ratio - everything except
config/validation.json-dependent parameters like mad_k, which validation.py
applies itself).

sweep: one Point per throttle stage, up/down episodes cut and computed
separately, then merged with the two-stage estimator (median within each
direction, arithmetic mean over the two direction medians - docs/ARCHITECTURE.md).
Loads the whole file into memory (two logical passes over it, like V1's
analyse/stage_summary.py) - runs are bounded in length, this is fine.

static: one Point per bin_seconds-wide time window, single-pass streaming
(V1's analyse/drift_check.py) - a static run may span minutes to hours
(docs/ARCHITECTURE.md: ESC drift / INA226 long-run validation), so memory use must
depend only on bin_seconds, never on how long the run was.

Both also expose the post-run capture window's own stats (never a Point -
docs/ARCHITECTURE.md: an ANALYZED row is always a real measurement point) for the
run-level zero_reference_drift check in validation.py.
"""
import csv
import dataclasses

from analysis import stats
from core import units


@dataclasses.dataclass
class DirectionStats:
    """Everything about one direction of one stage (sweep) or one time
    window (static), hold-phase samples only. Sample lists are kept, not
    just their median/MAD, because unstable_measurement's outlier count
    needs config/validation.json's mad_k - a parameter this module
    deliberately does not know about."""
    n_thrust: int
    thrust_g_samples: list
    thrust_g_median: float | None
    thrust_g_mad: float | None
    thrust_g_settle_ratio: float | None
    n_current: int
    current_a_samples: list
    current_a_median: float | None
    voltage_v_median: float | None
    n_rpm: int
    rpm_median: float | None
    temp_c_median: float | None


@dataclasses.dataclass
class Point:
    """One ANALYZED row - one real measurement point. sweep and static
    share this one shape; fields the other test type has no meaning for
    stay None (one CSV schema, not two)."""
    stage_promille: int
    bin_start_s: float | None  # static only
    directions_used: str | None  # sweep only: "up+down"/"up"/"down"
    n_up: int | None  # sweep only
    n_down: int | None  # sweep only
    n_thrust: int
    n_current: int
    thrust_g: float | None
    thrust_g_delta: float | None  # sweep only (down - up)
    thrust_g_delta_pct: float | None  # sweep only, relative to the mean
    thrust_g_sd_within: float | None  # sweep only
    current_a: float | None
    current_a_delta: float | None  # sweep only
    current_a_delta_pct: float | None  # sweep only
    current_a_sd_within: float | None  # sweep only
    voltage_v: float | None
    rpm: float | None
    temp_c_up: float | None  # sweep: upward leg; static: the window's own reading
    temp_c_down: float | None  # sweep only
    dT: float | None  # sweep only (down - up)
    # Not a CSV column - validation.py's raw material: {"up":..., "down":...}
    # for sweep (either may be None), {"window": ...} for static.
    directions: dict
    # power_w/g_per_w are set right after construction (build_points()'s
    # _add_power_fields()), from the merged voltage_v/current_a/thrust_g
    # above - not computed inline in _make_sweep_point()/_make_static_point()
    # so both share one formula instead of two copies.
    power_w: float | None = None
    g_per_w: float | None = None
    status: str = "VALID"
    findings: list = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class BuildResult:
    points: list  # list[Point]
    raw_zero_session: float | None
    countdown_voltage_v: float | None  # CURRENT_LIMIT_ACTIVE's reference, see _countdown_stats()
    post_run_window: DirectionStats | None  # for zero_reference_drift; never a Point


def _raw_to_grams(raw, cal, raw_zero_session):
    if raw_zero_session is not None:
        return (raw - raw_zero_session) / cal.counts_per_gram
    return cal.raw_to_grams(raw)


def _direction_stats_from_raw(raw_d, raw_shunt, raw_bus, rpm_list, temp_list, cal, raw_zero_session):
    thrust_g = [_raw_to_grams(r, cal, raw_zero_session) for r in raw_d] if cal is not None else []
    current_a = [units.raw_shunt_to_amps(r) for r in raw_shunt]
    voltage_v = [units.raw_bus_to_volts(r) for r in raw_bus]
    return DirectionStats(
        n_thrust=len(raw_d), thrust_g_samples=thrust_g,
        thrust_g_median=stats.median(thrust_g), thrust_g_mad=stats.mad(thrust_g),
        thrust_g_settle_ratio=stats.settle_ratio(thrust_g),
        n_current=len(raw_shunt), current_a_samples=current_a,
        current_a_median=stats.median(current_a),
        voltage_v_median=stats.median(voltage_v),
        n_rpm=len(rpm_list), rpm_median=stats.median(rpm_list),
        temp_c_median=stats.median(temp_list),
    )


def _append_row(buf, row):
    if row["type"] == "D":
        buf["raw"].append(int(row["raw"]))
    elif row["type"] == "I":
        buf["raw_shunt"].append(int(row["raw_shunt"]))
        buf["raw_bus"].append(int(row["raw_bus"]))
    elif row["type"] == "E":
        buf["rpm"].append(int(row["rpm"]))
        buf["temp_c"].append(int(row["temp_c"]))


def _empty_buf():
    return {"raw": [], "raw_shunt": [], "raw_bus": [], "rpm": [], "temp_c": []}


def _merge_field(up, down, field):
    u = getattr(up, field) if up is not None else None
    d = getattr(down, field) if down is not None else None
    if u is not None and d is not None:
        return (u + d) / 2
    return u if u is not None else d


def _sd_within(up_samples, down_samples):
    """sqrt((sd_up^2 + sd_down^2) / 2) - spread *within* a window, kept
    deliberately separate from delta, the spread *between* the two
    directions (docs/ARCHITECTURE.md)."""
    sd_up, sd_down = stats.sd(up_samples), stats.sd(down_samples)
    if sd_up is None or sd_down is None:
        return None
    return ((sd_up ** 2 + sd_down ** 2) / 2) ** 0.5


def _make_sweep_point(stage, up, down):
    # A direction "counts" only if it actually contributed a thrust reading
    # - an episode can exist (a STAGE line arrived) with zero hold-phase
    # samples if the run aborted immediately after, and that must not read
    # as a usable direction (validation.py's SINGLE_DIRECTION_ONLY relies
    # on this).
    parts = [d for d, v in (("up", up), ("down", down)) if v is not None and v.thrust_g_median is not None]
    directions_used = "+".join(parts) if parts else None

    thrust_delta = thrust_delta_pct = thrust_sd_within = None
    current_delta = current_delta_pct = current_sd_within = None
    if up is not None and down is not None:
        if up.thrust_g_median is not None and down.thrust_g_median is not None:
            thrust_delta = down.thrust_g_median - up.thrust_g_median
            mean_v = (up.thrust_g_median + down.thrust_g_median) / 2
            thrust_delta_pct = (thrust_delta / mean_v * 100) if mean_v else None
            thrust_sd_within = _sd_within(up.thrust_g_samples, down.thrust_g_samples)
        if up.current_a_median is not None and down.current_a_median is not None:
            current_delta = down.current_a_median - up.current_a_median
            mean_v = (up.current_a_median + down.current_a_median) / 2
            current_delta_pct = (current_delta / mean_v * 100) if mean_v else None
            current_sd_within = _sd_within(up.current_a_samples, down.current_a_samples)
    else:
        # Single direction (the peak stage, by design - docs/ARCHITECTURE.md - or a
        # partner leg that yielded no usable reading): no delta/delta_pct,
        # there is nothing to compare against, but *_sd_within still has a
        # real meaning here - the one available direction's own spread.
        # Without this fallback, validation.py's NON_MONOTONIC could never
        # fire at the peak stage, exactly where it was calibrated to catch
        # a real, reproducible finding (docs/DEVLOG.md, first real V2
        # session: a CC-driven thrust drop at 97.5%->100%) - found via a real session, not
        # anticipated when this module was first written.
        only = up if up is not None else down
        if only is not None:
            thrust_sd_within = stats.sd(only.thrust_g_samples)
            current_sd_within = stats.sd(only.current_a_samples)

    temp_c_up = up.temp_c_median if up is not None else None
    temp_c_down = down.temp_c_median if down is not None else None
    dT = (temp_c_down - temp_c_up) if (temp_c_up is not None and temp_c_down is not None) else None

    return Point(
        stage_promille=stage, bin_start_s=None, directions_used=directions_used,
        n_up=up.n_thrust if up is not None else None,
        n_down=down.n_thrust if down is not None else None,
        n_thrust=(up.n_thrust if up else 0) + (down.n_thrust if down else 0),
        n_current=(up.n_current if up else 0) + (down.n_current if down else 0),
        thrust_g=_merge_field(up, down, "thrust_g_median"),
        thrust_g_delta=thrust_delta, thrust_g_delta_pct=thrust_delta_pct,
        thrust_g_sd_within=thrust_sd_within,
        current_a=_merge_field(up, down, "current_a_median"),
        current_a_delta=current_delta, current_a_delta_pct=current_delta_pct,
        current_a_sd_within=current_sd_within,
        voltage_v=_merge_field(up, down, "voltage_v_median"),
        rpm=_merge_field(up, down, "rpm_median"),
        temp_c_up=temp_c_up, temp_c_down=temp_c_down, dT=dT,
        directions={"up": up, "down": down},
    )


def _make_static_point(static_promille, bin_start_s, window):
    return Point(
        stage_promille=static_promille, bin_start_s=bin_start_s, directions_used=None,
        n_up=None, n_down=None,
        n_thrust=window.n_thrust, n_current=window.n_current,
        thrust_g=window.thrust_g_median, thrust_g_delta=None, thrust_g_delta_pct=None,
        thrust_g_sd_within=None,
        current_a=window.current_a_median, current_a_delta=None, current_a_delta_pct=None,
        current_a_sd_within=None,
        voltage_v=window.voltage_v_median, rpm=window.rpm_median,
        temp_c_up=window.temp_c_median, temp_c_down=None, dT=None,
        directions={"window": window},
    )


def _read_rows(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(row for row in f if not row.startswith("#")))


def _countdown_stats(rows):
    """From the pre-run countdown (stage_promille=0, phase=""): the
    D-channel session zero point (median raw value, middle second only -
    docs/ARCHITECTURE.md, known pitfalls: raw_zero from config/calibration.json drifts
    with re-rigging, this does not; None without enough countdown data)
    and the I-channel reference bus voltage (median raw_bus, no trim
    needed - unlike the load cell, bus voltage has no mechanical settling
    to wait out). The reference voltage is validation.py's
    CURRENT_LIMIT_ACTIVE baseline: docs/ARCHITECTURE.md, known pitfalls - V1 used the
    highest voltage observed anywhere in the run as a stand-in for the
    unloaded voltage, which never detects CC if the run is already in CC
    from its very first stage (a real, circular bug); V2 uses the
    countdown voltage at a stopped motor instead, the same reference the
    firmware itself freezes for its own voltage-sag check."""
    d_t_us, d_raw, bus_raw = [], [], []
    for row in rows:
        if int(row["stage_promille"]) != 0 or row["phase"] != "":
            continue
        if row["type"] == "D":
            d_t_us.append(int(row["t_esp_us"]))
            d_raw.append(int(row["raw"]))
        elif row["type"] == "I":
            bus_raw.append(int(row["raw_bus"]))

    raw_zero_session = None
    if d_t_us:
        t_start, t_end = min(d_t_us) + units.IDLE_TRIM_US, max(d_t_us) - units.IDLE_TRIM_US
        trimmed = [r for t, r in zip(d_t_us, d_raw) if t_start <= t <= t_end]
        raw_zero_session = stats.median(trimmed) if trimmed else None

    countdown_voltage_v = None
    if bus_raw:
        countdown_voltage_v = stats.median([units.raw_bus_to_volts(r) for r in bus_raw])

    return raw_zero_session, countdown_voltage_v


def _read_episodes(rows):
    """Groups hold-phase samples into per-stage-direction episodes -
    contiguous runs of the same stage_promille, direction inferred from
    whether the stage value increased or decreased since the previous
    episode (the protocol carries no direction marker, docs/ARCHITECTURE.md,
    known pitfalls). stage_promille=0 rows (countdown, post-run capture) are
    not episodes and are handled separately by the caller."""
    episodes = []
    current = None
    for row in rows:
        stage = int(row["stage_promille"])
        if stage == 0:
            continue
        if current is None or stage != current["stage"]:
            if current is not None:
                episodes.append(current)
            direction = "up"
            if episodes and stage < episodes[-1]["stage"]:
                direction = "down"
            current = {"stage": stage, "direction": direction, **_empty_buf()}
        if row["phase"] != "hold":
            continue
        _append_row(current, row)
    if current is not None:
        episodes.append(current)
    return episodes


def _post_run_window_from_rows(rows, cal, raw_zero_session):
    buf = _empty_buf()
    for row in rows:
        if int(row["stage_promille"]) != 0 or row["phase"] != "hold":
            continue
        _append_row(buf, row)
    if not buf["raw"] and not buf["raw_shunt"]:
        return None
    return _direction_stats_from_raw(
        buf["raw"], buf["raw_shunt"], buf["raw_bus"], buf["rpm"], buf["temp_c"],
        cal, raw_zero_session,
    )


def _build_sweep(path, cal):
    rows = _read_rows(path)
    raw_zero_session, countdown_voltage_v = _countdown_stats(rows)
    episodes = _read_episodes(rows)

    by_stage = {}
    for ep in episodes:
        ds = _direction_stats_from_raw(
            ep["raw"], ep["raw_shunt"], ep["raw_bus"], ep["rpm"], ep["temp_c"],
            cal, raw_zero_session,
        )
        by_stage.setdefault(ep["stage"], {})[ep["direction"]] = ds

    points = [_make_sweep_point(stage, dirs.get("up"), dirs.get("down"))
              for stage, dirs in sorted(by_stage.items())]
    post_run_window = _post_run_window_from_rows(rows, cal, raw_zero_session)
    return BuildResult(points=points, raw_zero_session=raw_zero_session,
                        countdown_voltage_v=countdown_voltage_v, post_run_window=post_run_window)


class _BinAccumulator:
    """Buffers exactly one open time bin's raw samples and finalises it
    (DirectionStats) as soon as a row's elapsed time moves past it -
    memory use depends on bin_seconds, never on the total run length.
    Ported from V1's analyse/drift_check.py."""

    def __init__(self, bin_seconds, cal, raw_zero_session):
        self.bin_seconds = bin_seconds
        self.cal = cal
        self.raw_zero_session = raw_zero_session
        self.hold_start_us = None
        self.current_bin = -1
        self.buf = _empty_buf()
        self.finished = []

    def _flush(self):
        if self.current_bin < 0:
            return
        window = _direction_stats_from_raw(
            self.buf["raw"], self.buf["raw_shunt"], self.buf["raw_bus"],
            self.buf["rpm"], self.buf["temp_c"], self.cal, self.raw_zero_session,
        )
        self.finished.append((self.current_bin * self.bin_seconds, window))
        self.buf = _empty_buf()

    def add(self, t_us, row):
        if self.hold_start_us is None:
            self.hold_start_us = t_us
        elapsed_s = (t_us - self.hold_start_us) / 1e6
        bin_idx = int(elapsed_s // self.bin_seconds)
        if bin_idx != self.current_bin:
            self._flush()
            self.current_bin = bin_idx
        _append_row(self.buf, row)

    def close(self):
        self._flush()
        return self.finished


def _build_static(path, cal, static_promille, bin_seconds):
    idle_t_us, idle_raw, idle_bus_raw = [], [], []
    raw_zero_session = None
    countdown_voltage_v = None
    offset_computed = False
    acc = None
    post_run_buf = _empty_buf()

    with open(path, newline="") as f:
        reader = csv.DictReader(row for row in f if not row.startswith("#"))
        for row in reader:
            stage = int(row["stage_promille"])
            phase = row["phase"]

            if stage == 0 and phase == "":
                if row["type"] == "D":
                    idle_t_us.append(int(row["t_esp_us"]))
                    idle_raw.append(int(row["raw"]))
                elif row["type"] == "I":
                    idle_bus_raw.append(int(row["raw_bus"]))
                continue

            if not offset_computed:
                if idle_t_us:
                    t_start = min(idle_t_us) + units.IDLE_TRIM_US
                    t_end = max(idle_t_us) - units.IDLE_TRIM_US
                    trimmed = [r for t, r in zip(idle_t_us, idle_raw) if t_start <= t <= t_end]
                    raw_zero_session = stats.median(trimmed) if trimmed else None
                if idle_bus_raw:
                    countdown_voltage_v = stats.median([units.raw_bus_to_volts(r) for r in idle_bus_raw])
                offset_computed = True

            if stage == 0:
                # Post-run capture - never a Point of its own (docs/ARCHITECTURE.md);
                # only the settled "hold" half feeds zero_reference_drift,
                # same convention as V1's analyse/drift_check.py.
                if phase == "hold":
                    _append_row(post_run_buf, row)
                continue

            if phase != "hold":
                continue
            if acc is None:
                acc = _BinAccumulator(bin_seconds, cal, raw_zero_session)
            acc.add(int(row["t_esp_us"]), row)

    bins = acc.close() if acc is not None else []
    points = [_make_static_point(static_promille, bin_start_s, window) for bin_start_s, window in bins]

    post_run_window = None
    if post_run_buf["raw"] or post_run_buf["raw_shunt"]:
        post_run_window = _direction_stats_from_raw(
            post_run_buf["raw"], post_run_buf["raw_shunt"], post_run_buf["raw_bus"],
            post_run_buf["rpm"], post_run_buf["temp_c"], cal, raw_zero_session,
        )
    return BuildResult(points=points, raw_zero_session=raw_zero_session,
                        countdown_voltage_v=countdown_voltage_v, post_run_window=post_run_window)


def _add_power_fields(point):
    """power_w = voltage_v * current_a, g_per_w = thrust_g / power_w - from
    the already-merged values, computed unconditionally on status (an
    INVALID point's numbers are still worth carrying through; the status
    itself already says not to trust them)."""
    if point.voltage_v is not None and point.current_a is not None:
        point.power_w = point.voltage_v * point.current_a
        if point.power_w and point.thrust_g is not None:
            point.g_per_w = point.thrust_g / point.power_w


def build_points(path, cal, test_type, static_promille=None, bin_seconds=None) -> BuildResult:
    if test_type == "sweep":
        result = _build_sweep(path, cal)
    elif test_type == "static":
        if static_promille is None or bin_seconds is None:
            raise ValueError("static needs both static_promille and bin_seconds")
        result = _build_static(path, cal, static_promille, bin_seconds)
    else:
        raise ValueError(f"Unknown test_type: {test_type!r}")
    for point in result.points:
        _add_power_fields(point)
    return result
