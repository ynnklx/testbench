"""Guided front-end for one measurement session: a short setup wizard, a live
Textual dashboard while the device's autonomous run executes, and the
ANALYZED/COMPARISON results right after each cycle - all previously separate
manual steps (recorder.py, then reading the terminal, then
analysis/analyzer.py and comparison/compare.py by hand).

This module imports acquisition/recorder.py, analysis/analyzer.py and
comparison/compare.py as libraries and calls their existing functions - it
does not reimplement recording, analysis or comparison, and none needed a
rewrite for this: recorder.py's run_one_cycle() already had
on_sample/quiet/on_disarm (ported from V1) and gained one V2 addition,
on_mode(test_type, stage_promille, params_text), needed because V2 supports
static mode too, which V1's session.py never had to. analyzer.py's analyze()
gained a quiet flag the same way. compare.py gained compare_in_memory() and
its SessionSummary/CompareResult dataclasses specifically so this module has
somewhere to read pre-computed statistics from instead of calculating any of
its own (design rule: statistics are never recomputed in the UI).

Deliberately still an acquisition/ file, not a move of analysis/comparison
into acquisition/: it only calls out to them, the same way a person running
the tools back to back by hand would.

Per docs/ARCHITECTURE.md decision 3, the wizard below asks only for documentation
recorder.py cannot know on its own (propeller, motor, supply voltage,
ambient temperature, comment, repetition count) - never the test type
(sweep/static), which is a property of the device's own button state and
only becomes known via on_mode() once a cycle actually starts.

Three screens, switched via a ContentSwitcher:
LIVE VIEW while a cycle runs, RUN DETAIL for one completed run (browsable
with Left/Right across every run so far, not just the latest), SESSION
SUMMARY with cross-run KPIs/comparison table once at least one run is done
and no test is currently armed. SESSION SUMMARY is PARTIAL - n/m until the
last repetition, then FINAL automatically. It stays out of reach entirely
for a static session (decision 6: no comparison forced onto a test type
that has no shared x-axis to compare against).

RUN DETAIL and SESSION SUMMARY show numbers only, no plotext curve
(block-character curves are fine as a live
in-progress preview - LiveCurve below stays for that - but not as a
finished result; tables/KPI text carry the same numbers instead). The
plotted result is comparison/plotting.py's matplotlib PNG export, called
once right after the FINAL COMPARISON file is written (power vs. thrust and
efficiency vs. thrust) - session.py only passes it the file, it draws
nothing itself and never imports matplotlib directly.

rich/textual/textual-plotext are approved for this file only (docs/ARCHITECTURE.md
dependency boundaries) - analysis/ and comparison/ must stay installable
without them.
"""
import argparse
import math
import pathlib
import re
import sys
import time

from rich.console import Console
from rich.prompt import IntPrompt, Prompt
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.widgets import Collapsible, ContentSwitcher, DataTable, Footer, Static
from textual_plotext import PlotextPlot

from acquisition import link, recorder
from analysis import analyzer, stats, validation
from comparison import compare, plotting
from core import calibration


class SessionPlan:
    """Answers from the setup wizard - constant for the whole session, all
    repetitions share them."""

    def __init__(self, repetitions, propeller, motor, supply_voltage_v, ambient_temp_c, comment):
        self.repetitions = repetitions
        self.propeller = propeller
        self.motor = motor
        self.supply_voltage_v = supply_voltage_v
        self.ambient_temp_c = ambient_temp_c
        self.comment = comment


def run_setup_wizard() -> SessionPlan:
    """Plain rich prompts before the Textual app takes the screen - six
    linear questions don't earn a Textual form screen of their own. No
    test-type question - docs/ARCHITECTURE.md decision 3, see module docstring."""
    console = Console()
    console.rule("[bold]New session[/]")
    repetitions = IntPrompt.ask("Planned repetitions", default=1)
    propeller = Prompt.ask("Propeller", default="")
    motor = Prompt.ask("Motor", default="")
    supply_voltage_v = Prompt.ask("Supply voltage (V)", default="")
    ambient_temp_c = Prompt.ask("Ambient temperature (C)", default="")
    comment = Prompt.ask("Comment", default="")
    return SessionPlan(repetitions, propeller, motor, supply_voltage_v, ambient_temp_c, comment)


def _parse_params(params_text: str) -> dict:
    """"start=150 step=25 ... sag_fraction=0.90 ..." -> {"start": "150", ...} -
    read live from the current run's own "# PARAMS" line (see recorder.py's
    on_mode hook) instead of hand-copying a firmware constant a second time,
    the exact duplication the firmware's # PARAMS line eliminated."""
    result = {}
    for pair in params_text.split():
        key, _, value = pair.partition("=")
        if key:
            result[key] = value
    return result


def _slugify(text: str) -> str:
    """"1800 kv, brushless" -> "1800kv,brushless" -> "1800kvbrushless" - safe
    as one component of an auto-generated comparison label."""
    return re.sub(r"[,.\s]+", "", text)


def _auto_label(plan: SessionPlan) -> str:
    """E.g. "1800kv-prop365-16v" - built from the wizard's own configuration
    answers, no separate wizard question. An optional manual override can be added later; for now this is the
    only source, and "session" is the fallback when none of the three fields
    were filled in."""
    parts = []
    if plan.motor:
        parts.append(_slugify(plan.motor))
    if plan.propeller:
        parts.append(f"prop{_slugify(plan.propeller)}")
    if plan.supply_voltage_v:
        parts.append(f"{_slugify(plan.supply_voltage_v)}v")
    return "-".join(parts) if parts else "session"


def _status_markup(status: str) -> str:
    color = {validation.STATUS_VALID: "green", validation.STATUS_WARNING: "yellow",
             validation.STATUS_INVALID: "red"}.get(status, "white")
    return f"[{color}]{status}[/{color}]"


def _fmt_findings(findings) -> str:
    return ", ".join(f.code for f in findings) if findings else ""


# Cosmetic traffic-light thresholds for the ESC-temp bar in the live panel
# only - unrelated to any firmware constant.
_TEMP_GREEN_MAX_C = 50
_TEMP_YELLOW_MAX_C = 70
_TEMP_BAR_MIN_C = 20
_TEMP_BAR_MAX_C = 80
_TEMP_BAR_CHARS = "▂▄▆█"

_GAS_GAUGE_WIDTH = 24

# Clean, fixed x-axis ticks for any plot keyed by throttle stage (0-100%) -
# plotext's own tick generator just splits whatever xlim() range is set into
# equal parts regardless of whether that lands on round numbers.
_X_TICKS = [0, 25, 50, 75, 100]

_VOLTAGE_SAG_HOLDOFF_S = 0.2


def _nice_step(raw_step: float) -> float:
    """Smallest of 1/2/2.5/5/10 x 10^n that is >= raw_step - so axis ticks
    land on round values instead of plotext's default even split."""
    if raw_step <= 0:
        return 1.0
    magnitude = 10 ** math.floor(math.log10(raw_step))
    for m in (1, 2, 2.5, 5, 10):
        step = m * magnitude
        if step >= raw_step - 1e-9:
            return step
    return 10 * magnitude


def _gas_gauge(stage_promille: int) -> str:
    pos = max(0, min(_GAS_GAUGE_WIDTH, round(stage_promille / 1000 * _GAS_GAUGE_WIDTH)))
    return f"0%|{'-' * pos}|{'-' * (_GAS_GAUGE_WIDTH - pos)}|100%"


def _temp_bar(temp_c) -> str:
    if temp_c is None:
        return f"[dim]{_TEMP_BAR_CHARS}[/dim]"
    if temp_c < _TEMP_GREEN_MAX_C:
        color = "green"
    elif temp_c < _TEMP_YELLOW_MAX_C:
        color = "yellow"
    else:
        color = "red"
    fraction = (temp_c - _TEMP_BAR_MIN_C) / (_TEMP_BAR_MAX_C - _TEMP_BAR_MIN_C)
    lit = min(4, max(0, math.ceil(fraction * 4)))
    lit_part, dim_part = _TEMP_BAR_CHARS[:lit], _TEMP_BAR_CHARS[lit:]
    text = f"[{color}]{lit_part}[/{color}]" if lit_part else ""
    if dim_part:
        text += f"[dim]{dim_part}[/dim]"
    return text


def _fw_version(device_line: str) -> str:
    m = re.search(r"fw=(\S+)", device_line)
    return m.group(1) if m else "?"


def _fmt_num(value, width: int, digits: int) -> str:
    return f"{value:{width}.{digits}f}" if value is not None else f"{'--':>{width}}"


def _fmt_int(value, width: int) -> str:
    return f"{value:{width}d}" if value is not None else f"{'--':>{width}}"


def _round_rpm(rpm, step=100):
    """Rounded to the nearest 100 rpm for display - the raw value jitters
    sample to sample far more than is useful to read at a glance; used both
    by the live panel and the post-cycle results tables."""
    return round(rpm / step) * step if rpm is not None else None


def _fmt_cell(value, digits=1):
    return "n/a" if value is None else f"{value:.{digits}f}"


class LiveCurve(PlotextPlot):
    """sweep: thrust vs. throttle stage, up/down as two coloured series -
    ported from V1's session.py ThrustCurve, including its fixed/clean axis
    handling (see the class's original comment in V1: plotext's own
    autoscale produces unreadable tick spacing) and marker="hd" choice
    (braille has no glyphs in some terminal fonts; "sd" reads as a row of
    bricks on steep sections - "hd" is the confirmed-legible middle ground).

    static: thrust vs. elapsed seconds into the hold phase, one series,
    left to plotext's own autoscale - a static run's duration is open-ended
    (unlike a sweep's fixed 0-100% range), so there is no fixed range to
    pin ticks to. Simpler and less polished than the sweep case by design;
    an acceptable trade-off.

    LIVE VIEW only - block-character curves read fine as an in-progress
    preview but not as a finished result. RUN
    DETAIL and SESSION SUMMARY carry the same numbers as tables/KPI text
    instead; a proper plotted result stays comparison/plotting.py's
    matplotlib PNG export, not this widget."""

    def on_mount(self) -> None:
        super().on_mount()
        self.plt.title("Thrust")

    def set_sweep_points(self, up_points: dict, down_points: dict) -> None:
        self.plt.clear_data()
        if up_points:
            stages = sorted(up_points)
            self.plt.plot([s / 10 for s in stages], [up_points[s] for s in stages],
                          marker="hd", color="cyan", label="up")
        if down_points:
            stages = sorted(down_points)
            self.plt.plot([s / 10 for s in stages], [down_points[s] for s in stages],
                          marker="hd", color="orange", label="down")
        self.plt.xlim(0, 100)
        self.plt.xticks(_X_TICKS, [f"{t:g}%" for t in _X_TICKS])
        max_grams = max((*up_points.values(), *down_points.values(), 100))
        step = _nice_step(max_grams / 4)
        ceiling = step * 4
        y_ticks = [round(i * step, 6) for i in range(5)]
        self.plt.ylim(0, ceiling)
        self.plt.yticks(y_ticks, [f"{v:g}g" for v in y_ticks])
        self.refresh()

    def set_static_points(self, elapsed_s: list, thrust_g: list) -> None:
        self.plt.clear_data()
        if elapsed_s:
            self.plt.plot(elapsed_s, thrust_g, marker="hd", color="cyan")
        self.refresh()


class SessionApp(App):
    TITLE = "Testbench session"
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "restart", "Restart"),
        Binding("s", "toggle_summary", "Summary"),
        Binding("left", "prev_run", "Prev run", show=False),
        Binding("right", "next_run", "Next run", show=False),
    ]
    ENABLE_COMMAND_PALETTE = False

    CSS = """
    #banner {
        text-align: center;
        text-style: bold;
        color: $accent;
        border-top: heavy $accent;
        border-bottom: heavy $accent;
        margin: 0 2;
        height: 3;
    }
    .panel {
        border: round $panel;
        padding: 1 2;
        margin: 1 2 0 2;
    }
    .panel-accent {
        border: round $accent;
    }
    #status-panel {
        text-align: center;
    }
    #curve-panel { height: 25; }
    #detail-table, #summary-table { height: 16; }
    Collapsible { margin: 1 2 1 2; }
    """

    _VIEW_TO_SCREEN = {
        "live": "screen-live",
        "run_detail": "screen-run-detail",
        "session_summary": "screen-summary",
    }

    def __init__(self, conn, device_line, cal, plan: SessionPlan, run_args: argparse.Namespace,
                 raw_dir=recorder.DATA_DIR, analyzed_dir=analyzer.DEFAULT_OUT_DIR,
                 comparisons_dir=compare.DEFAULT_OUT_DIR, plots_dir=plotting.DEFAULT_OUT_DIR):
        super().__init__()
        self.conn = conn
        self.device_line = device_line
        self.cal = cal
        self.plan = plan
        self.run_args = run_args
        self.raw_dir = raw_dir
        self.analyzed_dir = analyzed_dir
        self.comparisons_dir = comparisons_dir
        self.plots_dir = plots_dir
        self.label = _auto_label(plan)
        self.run_index = 0
        self.armed_line = None  # threaded across cycles, see recorder.run_one_cycle()
        self.test_type = None  # set by on_mode() at the start of each cycle
        self.up_points = {}
        self.down_points = {}
        self.static_elapsed_s = []
        self.static_thrust_g = []
        self.sweep_going_up = True
        self.armed = False
        self.voltage_ref = None
        self.sag_since = None
        self.sag_active = False
        self.sag_fraction = 0.90  # overwritten per-cycle from that cycle's own PARAMS
        self.blink_on = False
        self.last_live = (0, "", {})

        # Per-run retention: every completed run
        # stays reachable, not just the last one.
        self.run_results = []  # list[analyzer.AnalyzeResult], parallel to raw_paths
        self.raw_paths = []
        self.view_mode = "live"
        self.viewed_run_index = 0
        self.session_result = None  # compare.CompareResult | None
        self.session_final_written = False
        self.plot_paths = []  # PNGs written alongside the FINAL COMPARISON
        self.plot_error = None  # shown in SESSION SUMMARY instead of crashing the UI

    def compose(self) -> ComposeResult:
        yield Static(
            f"» T E S T B E N C H «  [dim]fw {_fw_version(self.device_line)}[/dim]",
            id="banner",
        )
        with ContentSwitcher(initial="screen-live", id="screens"):
            with VerticalScroll(id="screen-live"):
                yield Static(id="session-panel", classes="panel")
                yield Static(id="status-panel", classes="panel panel-accent")
                yield Static(id="live-panel", classes="panel")
                yield LiveCurve(id="curve-panel", classes="panel panel-accent")
            with VerticalScroll(id="screen-run-detail"):
                yield Static(id="detail-selector", classes="panel")
                yield DataTable(id="detail-table", classes="panel")
                yield Static(id="detail-summary", classes="panel")
                with Collapsible(title="Details"):
                    yield Static(id="detail-details")
            with VerticalScroll(id="screen-summary"):
                yield Static(id="summary-header", classes="panel panel-accent")
                yield Static(id="summary-kpis", classes="panel")
                yield DataTable(id="summary-table", classes="panel")
                yield Static(id="summary-quality", classes="panel")
                with Collapsible(title="Details"):
                    yield Static(id="summary-details")
        yield Footer()

    def on_mount(self) -> None:
        detail_table = self.query_one("#detail-table", DataTable)
        detail_table.add_columns("Stage/Time", "Thrust g", "Current A", "Voltage V", "RPM",
                                  "Up/Down %", "Status", "Findings")
        detail_table.cursor_type = "none"
        detail_table.zebra_stripes = True

        summary_table = self.query_one("#summary-table", DataTable)
        summary_table.cursor_type = "none"
        summary_table.zebra_stripes = True

        self._render_session_panel()
        self._render_status_panel(False, "waiting for test...")
        self._render_live_panel(0, "", {})
        self.set_interval(0.5, self._toggle_blink)
        self.run_worker(self._run_sessions, thread=True)

    def action_restart(self) -> None:
        if self.run_index < self.plan.repetitions:
            self.bell()
            return
        self.exit("restart")

    def action_toggle_summary(self) -> None:
        if not self.run_results:
            self.bell()
            return
        if self.view_mode == "session_summary":
            self._show_view("run_detail")
            return
        # decision 5: only while no test is currently armed/running.
        # decision 6: never for a session that contains a static run - no
        # comparison to force it into.
        if self.armed or not self._sweep_session():
            self.bell()
            return
        self._enter_session_summary()

    def action_prev_run(self) -> None:
        if self.view_mode != "run_detail" or not self.run_results:
            return
        if self.viewed_run_index > 0:
            self.viewed_run_index -= 1
            self._render_run_detail()

    def action_next_run(self) -> None:
        if self.view_mode != "run_detail" or not self.run_results:
            return
        if self.viewed_run_index < len(self.run_results) - 1:
            self.viewed_run_index += 1
            self._render_run_detail()

    def _sweep_session(self) -> bool:
        return bool(self.run_results) and all(r.test_type == "sweep" for r in self.run_results)

    def _show_view(self, mode: str) -> None:
        self.view_mode = mode
        self.query_one("#screens", ContentSwitcher).current = self._VIEW_TO_SCREEN[mode]

    def _render_session_panel(self) -> None:
        p = self.plan
        current_run = min(self.run_index + 1, p.repetitions)
        self.query_one("#session-panel", Static).update(
            f"Run {current_run}/{p.repetitions}\n"
            f"[b]Motor:[/b] {p.motor or '-'}   [b]Propeller:[/b] {p.propeller or '-'}   "
            f"[b]Supply:[/b] {p.supply_voltage_v or '-'} V   "
            f"[b]Ambient:[/b] {p.ambient_temp_c or '-'} C\n"
            f"[b]Comment:[/b] {p.comment or '-'}"
        )

    def _render_status_panel(self, armed: bool, subtext: str) -> None:
        headline = "[reverse bold] ARMED [/reverse bold]" if armed else "[bold]-- DISARMED --[/bold]"
        self.query_one("#status-panel", Static).update(f"{headline}\n[dim]{subtext}[/dim]")

    def _render_live_panel(self, stage: int, phase: str, values: dict) -> None:
        rpm = values.get("rpm")
        rpm_rounded = _round_rpm(rpm)
        voltage = values.get("voltage_v")
        voltage_str = _fmt_num(voltage, 5, 2)
        if self.sag_active:
            style = "reverse yellow" if self.blink_on else "yellow"
            voltage_line = f"[{style}]Voltage: {voltage_str} V  voltage sag[/{style}]"
        else:
            voltage_line = f"[b]Voltage:[/b] {voltage_str} V"

        phase_str = f"({phase or 'countdown'})"
        mode_str = self.test_type or "?"
        lines = [
            f"[b]Mode:[/b] {mode_str}   [b]Step:[/b] {stage / 10:5.1f}%  {phase_str:<11}  "
            f"{_gas_gauge(stage)}",
            f"[b]Thrust:[/b]  {_fmt_num(values.get('grams'), 7, 1)} g      "
            f"[b]RPM:[/b] {_fmt_int(rpm_rounded, 6)} rpm",
            f"[b]Current:[/b] {_fmt_num(values.get('current_a'), 5, 2)} A      "
            f"{voltage_line}",
            f"[b]ESC temp:[/b] {_fmt_int(values.get('temp_c'), 3)} C  {_temp_bar(values.get('temp_c'))}",
        ]
        self.query_one("#live-panel", Static).update("\n".join(lines))

    def _toggle_blink(self) -> None:
        self.blink_on = not self.blink_on
        if self.sag_active:
            self._render_live_panel(*self.last_live)

    def _update_sag_state(self, voltage) -> None:
        if voltage is None or self.voltage_ref is None:
            return
        now = time.monotonic()
        if voltage < self.voltage_ref * self.sag_fraction:
            if self.sag_since is None:
                self.sag_since = now
            elif now - self.sag_since > _VOLTAGE_SAG_HOLDOFF_S:
                self.sag_active = True
        else:
            self.sag_since = None
            self.sag_active = False

    def _run_sessions(self) -> None:
        """Runs in a worker thread, off the asyncio loop -
        recorder.run_one_cycle() blocks on conn.readline(), which must
        never share a thread with Textual's event loop."""
        while self.run_index < self.plan.repetitions:
            self.call_from_thread(self._start_run)
            path, self.armed_line = recorder.run_one_cycle(
                self.conn, self.device_line, self.cal, self.run_args,
                repetition=(self.run_index + 1, self.plan.repetitions),
                on_mode=self._on_mode, on_sample=self._on_sample,
                on_disarm=self._on_disarm, quiet=True, armed_line=self.armed_line,
                data_dir=self.raw_dir,
            )
            self.run_index += 1
            self.call_from_thread(self._show_results, path)
        self.call_from_thread(self._render_status_panel, False, "all repetitions done")

    def _start_run(self) -> None:
        # Not armed yet - this only prepares fresh state and starts waiting
        # for the physical button. The device itself stays DISARMED until it
        # actually reports "# ARMED (button)" (see _apply_mode() below,
        # which is the only place self.armed becomes True) - claiming ARMED
        # here as well was misleading the live status panel from the very
        # start of every cycle.
        self.armed = False
        self.test_type = None
        self.voltage_ref = None
        self.sag_since = None
        self.sag_active = False
        self._render_session_panel()
        self._render_status_panel(False, "waiting for the button...")
        self.up_points = {}
        self.down_points = {}
        self.static_elapsed_s = []
        self.static_thrust_g = []
        self.sweep_going_up = True
        self.query_one("#curve-panel", LiveCurve).set_sweep_points({}, {})
        # Deliberately does NOT switch the view here: a finished run's RUN
        # DETAIL (or SESSION SUMMARY) stays on screen through the whole
        # "waiting for the button" gap, however long that takes on real
        # hardware - only _apply_mode() below, once the device has actually
        # re-armed and there is live data to show, switches to LIVE VIEW.

    def _on_mode(self, test_type: str, stage_promille, params_text: str) -> None:
        """Called from the worker thread once per cycle, right after
        "# MODE"/"# PARAMS" - before the first on_sample callback, so the
        live view can be shaped for this cycle's test type up front. This is
        also the earliest point at which the device has genuinely armed
        (MODE/PARAMS only ever follow a real "# ARMED (button)")."""
        params = _parse_params(params_text)
        sag_fraction = params.get("sag_fraction")
        self.call_from_thread(self._apply_mode, test_type, sag_fraction)

    def _apply_mode(self, test_type: str, sag_fraction) -> None:
        self.armed = True
        self.test_type = test_type
        if sag_fraction is not None:
            self.sag_fraction = float(sag_fraction)
        self._render_status_panel(True, f"{test_type} - countdown...")
        # Now that the device has genuinely re-armed and live data is about
        # to start arriving, hand the screen back to LIVE VIEW - whatever
        # RUN DETAIL/SESSION SUMMARY the previous run left on screen has had
        # its full "waiting for the button" window to be reviewed.
        self._show_view("live")

    def _on_sample(self, stage: int, phase: str, values: dict) -> None:
        self.call_from_thread(self._update_live, stage, phase, values)

    def _on_disarm(self, reason: str) -> None:
        self.call_from_thread(self._render_status_panel, False, reason)
        self.armed = False

    def _update_live(self, stage: int, phase: str, values: dict) -> None:
        self.last_live = (stage, phase, values)
        voltage = values.get("voltage_v")
        if stage == 0 and phase == "" and voltage is not None and self.voltage_ref is None:
            self.voltage_ref = voltage
        self._update_sag_state(voltage)
        self._render_live_panel(stage, phase, values)
        if self.armed:
            if stage == 0:
                subtext = {"": "countdown...", "settle": "coasting down...", "hold": "at rest"}[phase]
            else:
                subtext = "test running"
            self._render_status_panel(True, subtext)

        grams = values.get("grams")
        if stage == 0 or phase != "hold" or grams is None:
            return

        if self.test_type == "static":
            elapsed = values.get("elapsed_s")
            if elapsed is not None:
                self.static_elapsed_s.append(elapsed)
                self.static_thrust_g.append(max(grams, 0.0))
                self.query_one("#curve-panel", LiveCurve).set_static_points(
                    self.static_elapsed_s, self.static_thrust_g)
        elif self.test_type == "sweep":
            # Direction inferred the same way analysis/points.py does: a
            # stage lower than the previous one means the downward leg has
            # started (esc.h - every stage except the peak is visited
            # twice).
            if self.up_points and stage < max(self.up_points):
                self.sweep_going_up = False
            target = self.up_points if self.sweep_going_up else self.down_points
            # Clamped to >=0 for the curve only (V1: a single noisy
            # near-zero reading landing below the fixed y=0 floor crashes
            # plotext's legend rendering when it is the only point a
            # labelled series has so far).
            target[stage] = max(grams, 0.0)
            self.query_one("#curve-panel", LiveCurve).set_sweep_points(self.up_points, self.down_points)

    def _show_results(self, path: pathlib.Path) -> None:
        result = analyzer.analyze(path, out_dir=self.analyzed_dir, quiet=True)
        self.raw_paths.append(path)
        self.run_results.append(result)

        self.viewed_run_index = len(self.run_results) - 1
        self._render_run_detail()

        is_final = self.run_index >= self.plan.repetitions
        if is_final and self._sweep_session():
            self._enter_session_summary()
        else:
            self._show_view("run_detail")

        self._render_live_panel(0, "", {})
        self._render_session_panel()

    # -- RUN DETAIL ---------------------------------------------------

    def _render_run_detail(self) -> None:
        result = self.run_results[self.viewed_run_index]
        raw_path = self.raw_paths[self.viewed_run_index]
        n = len(self.run_results)
        idx = self.viewed_run_index + 1
        self.query_one("#detail-selector", Static).update(
            f"[b]◀[/b] Run {idx}/{n} [b]▶[/b]   [dim]{result.test_type}[/dim]"
        )

        points = result.build_result.points

        table = self.query_one("#detail-table", DataTable)
        table.clear()
        for p in points:
            label = f"{p.stage_promille / 10:.1f}%" if result.test_type == "sweep" else f"{p.bin_start_s:.0f}s"
            table.add_row(
                label,
                _fmt_cell(p.thrust_g), _fmt_cell(p.current_a), _fmt_cell(p.voltage_v),
                _fmt_cell(_round_rpm(p.rpm), 0), _fmt_cell(p.thrust_g_delta_pct),
                _status_markup(p.status), _fmt_findings(p.findings),
            )

        run_deltas = [abs(p.thrust_g_delta_pct) for p in points if p.thrust_g_delta_pct is not None]
        run_dev = stats.median(run_deltas) if run_deltas else None
        n_valid = sum(1 for p in points if p.status == validation.STATUS_VALID)
        n_warning = sum(1 for p in points if p.status == validation.STATUS_WARNING)
        n_invalid = sum(1 for p in points if p.status == validation.STATUS_INVALID)
        summary_lines = [
            f"{n_valid} valid, {n_warning} warning, {n_invalid} invalid   "
            f"[b]Run status:[/b] {_status_markup(result.run_status)}"
            + (f"   [b]Up/down deviation:[/b] {run_dev:.1f}%" if run_dev is not None else "")
        ]
        for f in result.run_findings:
            summary_lines.append(f"[b]Run finding:[/b] {f.code} ({f.level}): {f.text}")
        self.query_one("#detail-summary", Static).update("\n".join(summary_lines))

        self.query_one("#detail-details", Static).update(
            f"[b]Raw:[/b] {raw_path}\n[b]Analyzed:[/b] {result.out_path}"
        )

    # -- SESSION SUMMARY -----------------------------------------------

    def _enter_session_summary(self) -> None:
        out_paths = [r.out_path for r in self.run_results]
        is_final = self.run_index >= self.plan.repetitions
        if is_final:
            # Written exactly once (repeated 's' toggles after FINAL just
            # redisplay the cached result) - one real, traceable file, not
            # one per keypress.
            if not self.session_final_written:
                self.session_result = compare.write_comparison(
                    out_paths, label=self.label, out_dir=self.comparisons_dir)
                self.session_final_written = True
                self._write_plots(self.session_result.out_path)
        else:
            self.session_result = compare.compare_in_memory(out_paths)
        self._render_session_summary(final=is_final)
        self._show_view("session_summary")

    def _write_plots(self, comparison_path: pathlib.Path) -> None:
        # The measurement data is already safely on disk at this point, so a
        # plotting failure must not take the whole UI down with it - but it
        # is reported in SESSION SUMMARY, not swallowed. The PNGs can always
        # be redrawn later via `testbench.py plot`.
        try:
            self.plot_paths = [
                plotting.plot_power_vs_thrust(comparison_path, out_dir=self.plots_dir, quiet=True),
                plotting.plot_g_per_w_vs_thrust(comparison_path, out_dir=self.plots_dir, quiet=True),
            ]
        except Exception as exc:
            self.plot_error = f"{type(exc).__name__}: {exc}"

    def _render_session_summary(self, final: bool) -> None:
        n = len(self.run_results)
        kind = "FINAL" if final else f"PARTIAL · {n}/{self.plan.repetitions}"
        self.query_one("#summary-header", Static).update(
            f"[b]SESSION SUMMARY[/b]   {kind}   [dim]{self.label}[/dim]"
        )
        self._render_summary_kpis()
        self._render_summary_table()
        self._render_summary_quality()
        self._render_summary_details()

    def _render_summary_kpis(self) -> None:
        s = self.session_result.summary

        def marker(status):
            return " [yellow](from a WARNING point)[/yellow]" if status == validation.STATUS_WARNING else ""

        if s.max_usable_thrust_g is not None:
            thrust_line = (f"[b]Max usable thrust:[/b] {s.max_usable_thrust_g:.1f} g "
                            f"@ {s.max_usable_thrust_stage_promille / 10:.1f}%"
                            f"{marker(s.max_usable_thrust_status)}")
        else:
            thrust_line = "[b]Max usable thrust:[/b] —"

        if s.peak_usable_power_w is not None:
            power_line = (f"[b]Peak usable power:[/b] {s.peak_usable_power_w:.1f} W "
                           f"@ {s.peak_usable_power_stage_promille / 10:.1f}%"
                           f"{marker(s.peak_usable_power_status)}")
        else:
            power_line = "[b]Peak usable power:[/b] —"

        if s.best_g_per_w is not None:
            eff_line = (f"[b]Best efficiency:[/b] {s.best_g_per_w:.2f} g/W "
                        f"@ {s.best_g_per_w_thrust_g:.0f} g"
                        f"{marker(s.best_g_per_w_status)}")
        else:
            eff_line = "[b]Best efficiency:[/b] —"

        if s.typical_run_deviation_pct is not None:
            repeat_line = f"[b]Repeatability (typical run deviation):[/b] {s.typical_run_deviation_pct:.1f}%"
        else:
            repeat_line = "[b]Repeatability:[/b] — (needs at least 2 runs)"

        self.query_one("#summary-kpis", Static).update(
            f"{thrust_line}\n{power_line}\n{eff_line}\n{repeat_line}"
        )

    def _render_summary_table(self) -> None:
        result = self.session_result
        table = self.query_one("#summary-table", DataTable)
        table.clear(columns=True)
        n_runs = len(result.per_run)
        table.add_columns("Stage", *[f"Run {i + 1}" for i in range(n_runs)], "Median", "Deviation", "Status")
        for row in result.rows:
            stage = row["stage_promille"]
            cells = [f"{stage / 10:.1f}%"]
            for run in result.per_run:
                r = run["by_stage"].get(stage)
                if r is None:
                    cells.append("—")
                    continue
                t = r.get("thrust_g")
                if not t:
                    cells.append("n/a")
                    continue
                text = _fmt_cell(float(t))
                if r.get("status") == validation.STATUS_WARNING:
                    text += "*"
                elif r.get("status") == validation.STATUS_INVALID:
                    text = f"[red]{text}[/red]"
                cells.append(text)
            if row["included"] == "yes":
                cells.append(_fmt_cell(row["thrust_g_median"]))
                dev = row["stage_deviation_pct"]
                cells.append(f"{dev:.1f}%" if dev != "" else "—")
                cells.append("[green]OK[/green]")
            else:
                cells.append("—")
                cells.append("—")
                cells.append("[red]excluded[/red]")
            table.add_row(*cells)

    def _render_summary_quality(self) -> None:
        s = self.session_result.summary
        lines = [
            f"[b]Runs completed:[/b] {s.n_runs}/{self.plan.repetitions}",
            f"[b]Points:[/b] {s.n_points_valid} valid, {s.n_points_warning} warning, {s.n_points_invalid} invalid",
        ]
        if s.typical_run_deviation_pct is not None:
            lines.append(f"[b]Cross-run deviation:[/b] median {s.typical_run_deviation_pct:.1f}%, "
                         f"max {s.max_run_deviation_pct:.1f}%")
        else:
            lines.append("[b]Cross-run deviation:[/b] — (needs at least 2 runs)")
        if s.median_direction_mismatch_pct is not None:
            lines.append(f"[b]Typical up/down deviation:[/b] {s.median_direction_mismatch_pct:.1f}%")
        self.query_one("#summary-quality", Static).update("\n".join(lines))

    def _render_summary_details(self) -> None:
        lines = []
        for i, result in enumerate(self.run_results, start=1):
            lines.append(f"[b]Run {i}:[/b] {self.raw_paths[i - 1]}  ->  {result.out_path}")
        if self.session_result.out_path is not None:
            lines.append(f"[b]Comparison:[/b] {self.session_result.out_path}")
        for plot_path in self.plot_paths:
            lines.append(f"[b]Plot:[/b] {plot_path}")
        if self.plot_error is not None:
            lines.append(f"[red][b]Plotting failed:[/b] {self.plot_error}[/red]")
        self.query_one("#summary-details", Static).update("\n".join(lines))


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    link.add_link_args(parser)
    return parser.parse_args()


def main():
    cli_args = parse_args()

    conn = link.open_link(cli_args)
    conn.send("ID?")
    device_line = link.wait_for_line(
        conn, lambda l: l.startswith("OK ID") or l.startswith("ERR"), timeout_s=3.0
    )
    if not device_line or not device_line.startswith("OK ID"):
        print(f"No contact with the device: {device_line!r}", file=sys.stderr)
        sys.exit(1)

    conn.send("START")
    link.wait_for_line(conn, lambda l: l == "OK START", timeout_s=2.0)
    cal = calibration.try_load()

    try:
        while True:
            plan = run_setup_wizard()
            run_args = argparse.Namespace(
                propeller=plan.propeller, motor=plan.motor,
                supply_voltage_v=plan.supply_voltage_v,
                ambient_temp_c=plan.ambient_temp_c, comment=plan.comment,
            )
            result = SessionApp(conn, device_line, cal, plan, run_args).run()
            if result != "restart":
                break
    finally:
        conn.send("STOP")
        link.wait_for_line(conn, lambda l: l == "OK STOP", timeout_s=1.0)
        conn.close()


if __name__ == "__main__":
    main()
