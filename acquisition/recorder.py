"""Passive logger for the device's self-contained measurement run - one RAW
CSV file per arm cycle (docs/ARCHITECTURE.md, "Pipeline"). Ported from V1's
host/test_run.py, adapted for the V2 file/header format and the firmware's
"# MODE"/"# PARAMS" lines (firmware/src/esc.cpp).

This script never sends SET. The sequence runs entirely in the firmware,
triggered by the physical arm button - countdown, then throttle steps (sweep)
or a single held throttle (static), each with a settle phase (discarded) and
a hold phase (the actual measurement window). It listens for "# ARMED
(button)", "# MODE ...", "# PARAMS ...", "# STAGE <promille>",
"# HOLD <promille>" and "# DISARMED (...)" and tags every CSV row with the
current step and phase.

Per docs/ARCHITECTURE.md decision 3, this module alone - no wizard - must produce a
fully evaluable RAW file: every argument below is optional documentation
only (propeller/motor/supply_voltage_v/ambient_temp_c/comment), never
something the analyzer needs. The repetition tag in the file name
(naming.py) is likewise not a CLI flag here - it is only ever set by a future
caller (acquisition/session.py, which knows its own measurement plan) via
run_one_cycle()'s repetition parameter.

run_one_cycle() takes the same optional hooks V1's test_run.py grew for
host/session.py - on_sample(stage, phase, values), quiet=True (suppresses
this module's own dashboard/print()s), and on_disarm(reason) - plus one V2
addition, on_mode(test_type, stage_promille), needed because V2 supports
static mode too (see acquisition/session.py).
"""
import argparse
import csv
import datetime
import pathlib
import statistics
import sys
import time

from acquisition import link
from core import calibration, csvio, naming, units

DATA_DIR = pathlib.Path(__file__).resolve().parent.parent / "data" / "raw"

# How long to wait for "# MODE"/"# PARAMS" after "# ARMED (button)" before
# giving up. Both are printed synchronously by the firmware right after the
# ARMED line (esc.cpp's startMeasurementRun()) - this is generous headroom,
# not an expected wait.
MODE_PARAMS_TIMEOUT_S = 5.0

# Redraw rate of the live display - fast enough to look fluid, but not on
# every single I sample (those arrive far faster).
DASHBOARD_INTERVAL_S = 0.15

# Extra logging window after "# DISARMED": the ESP32 keeps streaming D/I/E
# regardless of arm state, so this data was already arriving, just
# previously discarded. Tagged stage_promille=0 like the pre-run countdown,
# but phase="settle" (motor still coasting down mechanically) then
# phase="hold" (rig at rest) - gives a post-run load cell baseline for a
# later drift comparison. Cut short immediately if the next
# "# ARMED (button)" arrives first (a fresh cycle takes priority).
POST_RUN_COASTDOWN_S = 2.0
POST_RUN_HOLD_S = 2.0

CSV_COLUMNS = [
    "type", "seq", "t_esp_us", "t_host_iso",
    "raw", "raw_shunt", "raw_bus", "rpm", "erpm", "temp_c",
    "stage_promille", "phase",
]


class RecorderError(Exception):
    pass


def compute_idle_offset(t_us_list, raw_list):
    """Median of the countdown raw values from the middle second, or
    (None, 0) when too little countdown data was collected."""
    if not t_us_list:
        return None, 0
    t_start, t_end = min(t_us_list) + units.IDLE_TRIM_US, max(t_us_list) - units.IDLE_TRIM_US
    trimmed = [r for t, r in zip(t_us_list, raw_list) if t_start <= t <= t_end]
    if not trimmed:
        return None, 0
    return statistics.median(trimmed), len(trimmed)


class Dashboard:
    """Live status block in the terminal during a cycle - display only,
    never part of the CSV. Overwrites itself in place using ANSI escapes;
    freeze() leaves the current block standing so events like step changes
    stay in the scrollback."""

    def __init__(self):
        self._active = False
        self._height = 0

    def update(self, lines):
        if self._active:
            sys.stdout.write(f"\033[{self._height}A")
        for line in lines:
            sys.stdout.write("\r\033[K" + line + "\n")
        sys.stdout.flush()
        self._active = True
        self._height = len(lines)

    def freeze(self):
        self._active = False


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    link.add_link_args(parser)
    parser.add_argument(
        "--propeller", default="",
        help="Propeller, for documentation only, e.g. 'T-Motor 10x4.5 CF'",
    )
    parser.add_argument("--motor", default="", help="Motor, for documentation only")
    parser.add_argument(
        "--supply-voltage-v", default="",
        help="Lab supply voltage setting, for documentation only, e.g. 16",
    )
    parser.add_argument("--comment", default="")
    parser.add_argument("--ambient-temp-c", default="")
    parser.add_argument(
        "--data-dir", type=pathlib.Path, default=DATA_DIR,
        help=f"Target directory for RAW files (default: {DATA_DIR})",
    )
    return parser.parse_args()


def _parse_mode_line(line: str):
    """"# MODE sweep" -> ("sweep", None); "# MODE static 700" -> ("static", 700).
    Delegates the value parsing to core.naming.parse_mode_value() - shared
    with analysis/ code that parses the same value back out of an ANALYZED
    header's "mode" field."""
    if not line.startswith("# MODE "):
        raise RecorderError(f"Expected a '# MODE' line, got: {line!r}")
    try:
        return naming.parse_mode_value(line[len("# MODE "):].strip())
    except ValueError as e:
        raise RecorderError(f"Malformed MODE line: {line!r}") from e


def _collect_mode_params(conn, armed_line):
    """Buffers every line received after "# ARMED (button)" until both
    "# MODE" and "# PARAMS" have arrived. The firmware prints them
    synchronously right after ARMED, but not necessarily as the literal next
    two lines on the wire - D/I/E samples keep streaming independently of
    arming. The RAW file can only be opened once MODE is known (it decides
    the file name, see naming.py), so nothing may be dropped while waiting -
    every line seen here is returned and gets replayed through the same
    per-line handling once the file is open (see run_one_cycle()), in
    original order, so the delayed file creation loses nothing."""
    pending = [armed_line]
    mode_line = None
    params_line = None
    deadline = time.monotonic() + MODE_PARAMS_TIMEOUT_S
    while mode_line is None or params_line is None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RecorderError(
                "Timeout waiting for '# MODE'/'# PARAMS' after "
                "'# ARMED (button)' - check the firmware version on the device."
            )
        line = conn.readline(timeout=min(0.3, remaining))
        if line is None:
            continue
        pending.append(line)
        if line.startswith("# MODE "):
            mode_line = line
        elif line.startswith("# PARAMS "):
            params_line = line
    return pending, mode_line, params_line


def _calibration_header_fields(cal) -> dict:
    if cal is None:
        return {
            "calibration_file": "none (not created yet)",
            "calibration_sha256": "",
            "calibration_date": "",
            "calibration_provisional": "",
        }
    return {
        "calibration_file": "config/calibration.json",
        "calibration_sha256": csvio.sha256_file(calibration.CONFIG_PATH),
        "calibration_date": cal.calibrated_at,
        "calibration_provisional": "yes" if cal.provisional else "no",
    }


def open_csv(mode_line, params_line, device_line, cal, args, repetition=None,
             data_dir=DATA_DIR):
    """Only the calibration *reference* (file/hash/date/provisional) goes
    into a RAW header, never the fit constants - RAW stores raw counts only
    (docs/ARCHITECTURE.md decision 1) and never converts anything. The constants
    actually used for a conversion belong in the ANALYZED file that does
    the converting."""
    test_type, stage_promille = _parse_mode_line(mode_line)
    mode_value = mode_line[len("# MODE "):].strip()
    params_value = params_line[len("# PARAMS "):].strip()

    timestamp = datetime.datetime.now()
    filename = naming.build_run_filename(naming.RunFilenameParts(
        timestamp=timestamp, test_type=test_type, stage_promille=stage_promille,
        repetition=repetition, file_stage="RAW",
    ))
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / filename

    header = [
        "schema_version=1",
        f"recorded_at={timestamp.isoformat()}",
        f"tool_version={csvio.TOOL_VERSION}",
        f"device_id={device_line}",
        # Passed through verbatim from the firmware (docs/ARCHITECTURE.md, "# MODE and
        # # PARAMS") - this is now the one source for the run's throttle
        # program, replacing V1's hand-copied, occasionally-stale
        # step_sequence free text.
        f"mode={mode_value}",
        f"params={params_value}",
    ]
    header += [f"{k}={v}" for k, v in _calibration_header_fields(cal).items()]
    header += [
        f"propeller={args.propeller}",
        f"motor={args.motor}",
        f"supply_voltage_v={args.supply_voltage_v}",
        f"ambient_temp_c={args.ambient_temp_c}",
        f"comment={args.comment}",
        f"repetition={f'{repetition[0]}of{repetition[1]}' if repetition else ''}",
    ]

    f = open(path, "w", newline="")
    csvio.write_header(f, header)
    writer = csv.writer(f)
    writer.writerow(CSV_COLUMNS)
    return path, f, writer


def run_one_cycle(conn, device_line, cal, args, repetition=None, on_sample=None,
                   quiet=False, on_disarm=None, on_mode=None, armed_line=None,
                   data_dir=DATA_DIR):
    """Records one arm cycle into a fresh RAW file and returns
    (path, rearmed_line). rearmed_line is the already-consumed
    "# ARMED (button)" line when a new cycle started during this one's
    post-run capture window (see the "# DISARMED" handling below) - pass it
    straight back in as armed_line on the next call instead of waiting for
    an ARMED line that will never come again (it already happened); None
    otherwise, in which case the next call should wait as usual.

    on_mode(test_type, stage_promille, params_text), if given, fires once per
    cycle right after "# MODE"/"# PARAMS" are known - before file creation,
    let alone the first sample - so a caller (acquisition/session.py) can
    shape its live view (e.g. a sweep-only up/down curve, or a voltage-sag
    hint read from params_text's own sag_fraction instead of a second
    hand-copied constant) for this cycle without waiting for the first
    on_sample callback. params_text is the verbatim "# PARAMS " value, same
    as ends up in the RAW header's "params" field.
    """
    if armed_line is None:
        armed_line = link.wait_for_line(
            conn, lambda l: l == "# ARMED (button)", timeout_s=None)

    pending, mode_line, params_line = _collect_mode_params(conn, armed_line)
    if on_mode is not None:
        test_type, stage_promille = naming.parse_mode_value(mode_line[len("# MODE "):].strip())
        on_mode(test_type, stage_promille, params_line[len("# PARAMS "):].strip())
    path, f, writer = open_csv(mode_line, params_line, device_line, cal, args,
                                repetition, data_dir)

    n_weight_samples = 0
    n_current_samples = 0
    n_rpm_samples = 0
    current_stage = 0
    current_phase = ""  # empty during the countdown, before the first step
    finished = False
    start = time.monotonic()
    phase_start = start

    last_real_stage = 0  # current_stage is reset to 0 for the post-run capture below
    post_run_start = None  # set once DISARMED arrives, see module docstring
    rearmed_line = None
    disarm_line = None  # the raw "# DISARMED (...)" line, for the summary below

    last_raw = None
    last_amps = None
    last_volts = None
    last_rpm = None
    last_temp_c = None

    idle_t_us = []
    idle_raw = []
    raw_zero_session = None

    def raw_to_grams_session(raw):
        if cal is None:
            return None
        if raw_zero_session is not None:
            return (raw - raw_zero_session) / cal.counts_per_gram
        return cal.raw_to_grams(raw)

    dash = Dashboard()
    last_dash_update = 0.0
    phase_labels = {"": "countdown", "settle": "settle", "hold": "hold"}

    def render_dashboard(force=False):
        nonlocal last_dash_update
        now_mono = time.monotonic()
        if not force and now_mono - last_dash_update < DASHBOARD_INTERVAL_S:
            return
        last_dash_update = now_mono
        elapsed = now_mono - phase_start

        if cal is not None and last_raw is not None:
            weight_str = f"{raw_to_grams_session(last_raw):8.1f} g"
        elif last_raw is not None:
            weight_str = f"{last_raw:+d} (uncalibrated)"
        else:
            weight_str = "--"
        current_str = f"{last_amps:5.2f} A" if last_amps is not None else "--"
        voltage_str = f"{last_volts:5.2f} V" if last_volts is not None else "--"
        rpm_str = f"{last_rpm} rpm" if last_rpm is not None else "--"
        temp_str = f"{last_temp_c} C" if last_temp_c is not None else "--"

        if not quiet:
            dash.update([
                f"Step {current_stage / 10:5.1f}%  [{phase_labels[current_phase]:9s}]  {elapsed:5.1f}s",
                f"  Thrust:  {weight_str}",
                f"  Current: {current_str}   Voltage: {voltage_str}",
                f"  RPM:     {rpm_str}   ESC temp: {temp_str}",
                f"  Samples: D={n_weight_samples}  I={n_current_samples}  E={n_rpm_samples}",
            ])

        if on_sample is not None:
            grams = raw_to_grams_session(last_raw) if (cal is not None and last_raw is not None) else None
            on_sample(current_stage, current_phase, {
                "grams": grams, "current_a": last_amps, "voltage_v": last_volts,
                "rpm": last_rpm, "temp_c": last_temp_c, "elapsed_s": elapsed,
            })

    def check_post_run_deadline():
        """Advances settle->hold and reports whether the post-run capture
        window is over. No-op while post_run_start is still None (normal
        run, not yet disarmed)."""
        nonlocal current_phase
        if post_run_start is None:
            return False
        elapsed_post = time.monotonic() - post_run_start
        if elapsed_post >= POST_RUN_COASTDOWN_S and current_phase != "hold":
            current_phase = "hold"
            render_dashboard(force=True)
        return elapsed_post >= POST_RUN_COASTDOWN_S + POST_RUN_HOLD_S

    def handle_line(line) -> bool:
        """Processes one protocol line - used both to replay the lines
        buffered before the file was open and for every line arriving
        afterwards, so a sample makes it into the CSV exactly the same way
        regardless of which side of that boundary it arrived on. Returns
        True when the caller should stop reading (a new cycle already
        started, see rearmed_line above)."""
        nonlocal current_stage, current_phase, phase_start, last_real_stage
        nonlocal post_run_start, rearmed_line, disarm_line, finished
        nonlocal last_raw, last_amps, last_volts, last_rpm, last_temp_c
        nonlocal raw_zero_session
        nonlocal n_weight_samples, n_current_samples, n_rpm_samples

        if line == "# ARMED (button)":
            if post_run_start is not None:
                # A fresh cycle already started while still capturing the
                # post-run window - stop now instead of discarding this
                # line; the caller passes it straight into the next
                # run_one_cycle() call as armed_line.
                rearmed_line = line
                return True
            return False  # our own cycle's ARMED line, replayed from pending[0]

        if line.startswith("# MODE ") or line.startswith("# PARAMS "):
            # Already consumed by open_csv() to build the header - nothing
            # further to do on replay.
            return False

        t_host_iso = datetime.datetime.now().isoformat()

        if line.startswith("# STAGE "):
            if current_stage == 0 and cal is not None:
                raw_zero_session, n_idle = compute_idle_offset(idle_t_us, idle_raw)
                if not quiet:
                    if raw_zero_session is not None:
                        offset_delta_g = (raw_zero_session - cal.raw_zero) / cal.counts_per_gram
                        print(f"Zero offset: {offset_delta_g:+.1f} g")
                    else:
                        print("Zero offset: n/a (not enough countdown data) - "
                              "using raw_zero from config/calibration.json.")
            dash.freeze()
            current_stage = int(line.split(" ")[-1])
            current_phase = "settle"
            phase_start = time.monotonic()
            if not quiet:
                print(f"--- Step {current_stage / 10:.1f}% ---")
            render_dashboard(force=True)
            return False

        if line.startswith("# HOLD "):
            current_phase = "hold"
            phase_start = time.monotonic()
            render_dashboard(force=True)
            return False

        if line.startswith("# DISARMED"):
            finished = (
                "button" not in line
                and "telemetry lost" not in line
                and "temp limit" not in line
            )
            disarm_line = line
            last_real_stage = current_stage
            dash.freeze()
            if on_disarm is not None:
                on_disarm(line.split("(", 1)[1].rstrip(")"))
            post_run_start = time.monotonic()
            current_stage = 0
            current_phase = "settle"
            phase_start = post_run_start
            if not quiet:
                print("--- Post-run capture (coastdown, then at rest) ---")
            render_dashboard(force=True)
            return False

        if line.startswith("D,"):
            _, seq, t_us, raw = line.split(",")
            last_raw = int(raw)
            if current_stage == 0 and current_phase == "":
                idle_t_us.append(int(t_us))
                idle_raw.append(last_raw)
            writer.writerow(["D", seq, t_us, t_host_iso, raw, "", "", "", "", "",
                              current_stage, current_phase])
            n_weight_samples += 1
        elif line.startswith("I,"):
            _, seq, t_us, raw_shunt, raw_bus = line.split(",")
            last_amps = units.raw_shunt_to_amps(int(raw_shunt))
            last_volts = units.raw_bus_to_volts(int(raw_bus))
            writer.writerow(["I", seq, t_us, t_host_iso, "", raw_shunt, raw_bus, "", "", "",
                              current_stage, current_phase])
            n_current_samples += 1
        elif line.startswith("E,"):
            _, seq, t_us, rpm, erpm, temp_c, _voltage_cv, _current_ca, _consumption_mah = line.split(",")
            last_rpm = int(rpm)
            last_temp_c = int(temp_c)
            writer.writerow(["E", seq, t_us, t_host_iso, "", "", "", rpm, erpm, temp_c,
                              current_stage, current_phase])
            n_rpm_samples += 1
        render_dashboard()
        return False

    try:
        for buffered_line in pending:
            handle_line(buffered_line)
        while True:
            line = conn.readline(timeout=0.3)
            if line is None:
                if check_post_run_deadline():
                    break
                render_dashboard()
                continue
            if handle_line(line):
                break
            if check_post_run_deadline():
                break
    finally:
        f.close()
    csvio.make_readonly(path)

    duration_s = time.monotonic() - start
    if not quiet:
        print("\n=== Summary ===")
        print(f"Duration: {duration_s:.1f}s, last step: {last_real_stage / 10:.1f}%")
        print(f"Samples: {n_weight_samples} thrust, {n_current_samples} current, {n_rpm_samples} rpm")
        if not finished:
            print(f"Cycle ended early: {disarm_line}")
        print(f"Written: {path}\n")
    return path, rearmed_line


def main():
    args = parse_args()
    conn = link.open_link(args)

    conn.send("ID?")
    device_line = link.wait_for_line(
        conn, lambda l: l.startswith("OK ID") or l.startswith("ERR"), timeout_s=3.0
    )
    if not device_line or not device_line.startswith("OK ID"):
        print(f"No contact with the device: {device_line!r}", file=sys.stderr)
        sys.exit(1)
    print(f"Connected: {device_line}")

    conn.send("START")
    link.wait_for_line(conn, lambda l: l == "OK START", timeout_s=2.0)

    cal = calibration.try_load()

    print("Ready. Runs continuously - Ctrl+C to stop.")
    try:
        armed_line = None
        while True:
            if armed_line is None:
                print("\nWaiting for the next cycle (press the button) ...")
            else:
                print("Armed already - continuing straight into the next cycle.\n")
            _, armed_line = run_one_cycle(conn, device_line, cal, args, armed_line=armed_line,
                                           data_dir=args.data_dir)
    except KeyboardInterrupt:
        print("\nStopped (Ctrl+C).")
    finally:
        conn.send("STOP")
        link.wait_for_line(conn, lambda l: l == "OK STOP", timeout_s=1.0)
        conn.close()


if __name__ == "__main__":
    main()
