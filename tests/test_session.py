"""acquisition/session.py against Textual's headless test harness
(App.run_test()) driven by acquisition/link.py's SimulatedLink - slow (one
full simulated sweep cycle, like test_recorder.py) but this is the only
place that exercises the worker-thread callback wiring (on_mode/on_sample/
on_disarm all fire off the asyncio loop and must marshal back via
call_from_thread) end to end. Plain asyncio.run() rather than an async test
function - avoids depending on which async pytest plugin (if any) is wired
up.
"""
import argparse
import asyncio

from acquisition import link, session
from core import calibration


def _run(coro):
    return asyncio.run(coro)


async def _wait_until(pilot, predicate, timeout_s=40.0, step_s=0.2):
    """Polls in small steps instead of one long pause() - the gap between a
    run finishing and the next one re-arming (SimulatedLink's
    _SIM_REARM_DELAY_S) is only ~2s, too easy to blow past with a single
    coarse pause() and land mid-way into the next run instead."""
    waited = 0.0
    while waited < timeout_s:
        if predicate():
            return
        await pilot.pause(step_s)
        waited += step_s
    raise AssertionError(f"condition not met within {timeout_s}s")


def _make_app(tmp_path, repetitions, conn, device_line, cal):
    plan = session.SessionPlan(repetitions, "testprop", "testmotor", "16", "22", "pytest")
    run_args = argparse.Namespace(
        propeller=plan.propeller, motor=plan.motor,
        supply_voltage_v=plan.supply_voltage_v,
        ambient_temp_c=plan.ambient_temp_c, comment=plan.comment,
    )
    return session.SessionApp(
        conn, device_line, cal, plan, run_args,
        raw_dir=tmp_path / "raw", analyzed_dir=tmp_path / "processed",
        comparisons_dir=tmp_path / "comparisons", plots_dir=tmp_path / "plots",
    ), plan


def test_session_single_run_opens_final_summary(tmp_path):
    """One planned repetition: the only run is also the last one, so the
    session must land directly on a FINAL Session Summary,
    with a RUN DETAIL still fully populated underneath it."""
    async def body():
        conn = link.SimulatedLink()
        conn.send("ID?")
        device_line = link.wait_for_line(conn, lambda l: l.startswith("OK ID"), timeout_s=3.0)
        assert device_line is not None
        conn.send("START")
        link.wait_for_line(conn, lambda l: l == "OK START", timeout_s=2.0)

        cal = calibration.try_load()
        app, plan = _make_app(tmp_path, 1, conn, device_line, cal)

        async with app.run_test() as pilot:
            await pilot.pause(4.0)
            assert app.test_type == "sweep"
            assert app.sag_fraction == 0.90  # read live from PARAMS, not hardcoded
            assert app.label == "testmotor-proptestprop-16v"

            # One full simulated sweep cycle: ~20s of compressed stage
            # transitions plus the fixed 4s post-run capture window
            # (recorder.py) - generous margin above that.
            await pilot.pause(30.0)
            assert app.run_index == 1
            assert not app.armed
            assert len(app.run_results) == 1

            # Landed on FINAL Session Summary automatically.
            assert app.view_mode == "session_summary"
            assert app.session_result is not None
            assert app.session_result.out_path is not None
            assert app.session_result.out_path.exists()
            assert app.session_result.summary.n_runs == 1
            # Power/efficiency-vs-thrust PNGs drawn right after the FINAL
            # COMPARISON file.
            assert app.plot_error is None
            assert [p.name for p in app.plot_paths] == [
                app.session_result.out_path.stem.removesuffix("_COMPARISON") + "_power_vs_thrust.png",
                app.session_result.out_path.stem.removesuffix("_COMPARISON") + "_efficiency_vs_thrust.png",
            ]
            assert all(p.exists() and p.parent == tmp_path / "plots" for p in app.plot_paths)
            # Cross-run KPIs are "-" with a single run - no comparison to make.
            assert app.session_result.summary.typical_run_deviation_pct is None

            summary_table = app.query_one("#summary-table")
            assert summary_table.row_count == 35  # (1000-150)/25 + 1 stages

            # RUN DETAIL underneath is fully populated even though not shown.
            assert app.viewed_run_index == 0
            detail_table = app.query_one("#detail-table")
            assert detail_table.row_count == 35

            app.exit()

        conn.close()

    _run(body())


def test_session_two_runs_run_detail_then_final_summary(tmp_path):
    """Two planned repetitions: after run 1, the app must show RUN DETAIL
    for that run (not overwrite-in-place, not jump to a summary yet -
    decision 5, only after the last repetition). 's' must open a PARTIAL
    summary while waiting for run 2 (device disarmed between cycles), and
    after run 2 completes the app must auto-switch to a FINAL summary
    covering both runs, with Left/Right able to browse both run details."""
    # SimulatedLink's real re-arm gap is only 2s (_SIM_REARM_DELAY_S) - too
    # tight to reliably observe against this test's own analysis/render
    # overhead, not an app timing issue. Widened for this test only.
    original_rearm_delay = link.SimulatedLink._SIM_REARM_DELAY_S
    link.SimulatedLink._SIM_REARM_DELAY_S = 6.0

    async def body():
        conn = link.SimulatedLink()
        conn.send("ID?")
        device_line = link.wait_for_line(conn, lambda l: l.startswith("OK ID"), timeout_s=3.0)
        assert device_line is not None
        conn.send("START")
        link.wait_for_line(conn, lambda l: l == "OK START", timeout_s=2.0)

        cal = calibration.try_load()
        app, plan = _make_app(tmp_path, 2, conn, device_line, cal)

        async with app.run_test() as pilot:
            await _wait_until(pilot, lambda: len(app.run_results) == 1)
            assert app.run_index == 1
            assert app.view_mode == "run_detail"
            assert app.viewed_run_index == 0
            assert not app.armed  # waiting for the button again between reps

            # PARTIAL summary reachable now, writes nothing to disk.
            await pilot.press("s")
            await pilot.pause()
            assert app.view_mode == "session_summary"
            assert app.session_result.out_path is None
            assert app.session_result.summary.n_runs == 1
            comparisons_dir = tmp_path / "comparisons"
            assert not comparisons_dir.exists()
            assert not (tmp_path / "plots").exists()  # plots only with FINAL

            # Toggle back to RUN DETAIL.
            await pilot.press("s")
            await pilot.pause()
            assert app.view_mode == "run_detail"

            await _wait_until(pilot, lambda: len(app.run_results) == 2)  # -> auto FINAL summary
            assert app.run_index == 2
            assert len(app.run_results) == 2
            assert app.view_mode == "session_summary"
            assert app.session_result.out_path is not None
            assert app.session_result.summary.n_runs == 2
            # SimulatedLink's hold window is compressed to well below any
            # real-hardware sample-count threshold (fast cycling for tests),
            # so every simulated point comes back INVALID/INSUFFICIENT_SAMPLES
            # - a property of the fixture, not of compare.py/session.py (the
            # KPI math itself is covered against realistic data in
            # tests/test_compare.py). What matters here is that the wiring
            # tallies both runs' points without crashing.
            assert app.session_result.summary.n_points_invalid == 70  # 2 runs x 35 points
            first_written = app.session_result.out_path
            first_plots = list(app.plot_paths)
            assert len(first_plots) == 2

            # A second 's'-driven look at FINAL reuses the same file, not a
            # fresh COMPARISON per keypress.
            await pilot.press("s")  # back to run_detail
            await pilot.pause()
            await pilot.press("s")  # forward to session_summary again
            await pilot.pause()
            assert app.session_result.out_path == first_written
            assert app.plot_paths == first_plots

            # Browse both run details.
            await pilot.press("s")  # run_detail (viewing run 2, the latest)
            await pilot.pause()
            assert app.view_mode == "run_detail"
            assert app.viewed_run_index == 1
            await pilot.press("left")
            await pilot.pause()
            assert app.viewed_run_index == 0
            await pilot.press("left")  # clamps at the first run
            await pilot.pause()
            assert app.viewed_run_index == 0
            await pilot.press("right")
            await pilot.pause()
            assert app.viewed_run_index == 1

            app.exit()

        conn.close()

    try:
        _run(body())
    finally:
        link.SimulatedLink._SIM_REARM_DELAY_S = original_rearm_delay
