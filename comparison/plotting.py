"""Draws PNG plots from one or more COMPARISON files - the last pipeline
stage (docs/ARCHITECTURE.md, "Pipeline" - plots). Never computes anything
(docs/ARCHITECTURE.md decision 9) - only reads what compare.py already wrote. The only
file in this project allowed to import matplotlib (docs/ARCHITECTURE.md dependency
boundaries).

Several COMPARISON files overlay onto one chart (one series per file, colour
distinguishes them, label taken from each file's own header) - e.g. one
motor across its three supply voltages, or three motors at one voltage.
plotting.py does not care which combination is passed in; that choice stays
with whoever calls it, same as compare.py does not check that the ANALYZED
files it is given share a configuration.
"""
import argparse
import csv
import pathlib

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from core import csvio, naming

DEFAULT_OUT_DIR = pathlib.Path(__file__).resolve().parent.parent / "data" / "plots"


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("comparison_paths", type=pathlib.Path, nargs="+",
                         help="One COMPARISON file, or several to overlay in one chart.")
    parser.add_argument("--kind", choices=["thrust", "power_vs_thrust", "g_per_w_vs_thrust"],
                         default="thrust",
                         help="thrust: thrust vs. throttle (default). "
                              "power_vs_thrust / g_per_w_vs_thrust: power or efficiency vs. "
                              "thrust, throttle dropped as the x-axis.")
    parser.add_argument("--out-dir", type=pathlib.Path, default=None)
    return parser.parse_args()


def _read_rows(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(line for line in f if not line.startswith("#")))


def _read_series(comparison_path: pathlib.Path):
    # Fails fast with a clear message if this isn't actually a COMPARISON
    # file name, before touching matplotlib at all.
    naming.parse_comparison_filename(comparison_path.name)

    header = csvio.read_header(comparison_path)
    rows = _read_rows(comparison_path)

    stages_pct = [int(r["stage_promille"]) / 10 for r in rows]
    # float("nan"), not 0 or a carried-forward value - matplotlib breaks the
    # line at a NaN y-value instead of connecting across it (docs/ARCHITECTURE.md
    # decision 6: no interpolation over an excluded/untrustworthy stage;
    # WARNING-only stages stay full support points and are never excluded
    # here in the first place, see compare.py).
    thrust = [float(r["thrust_g_median"]) if r["included"] == "yes" else float("nan") for r in rows]
    thrust_sd = [float(r["thrust_g_sd"]) if r["included"] == "yes" and r["thrust_g_sd"] else 0.0
                 for r in rows]
    excluded_pct = [p for p, r in zip(stages_pct, rows) if r["included"] != "yes"]
    label = header.get("label", comparison_path.stem)
    return label, stages_pct, thrust, thrust_sd, excluded_pct


def plot_thrust(comparison_paths, out_dir: pathlib.Path = DEFAULT_OUT_DIR) -> pathlib.Path:
    # Accept either one path (existing single-file callers, tests included)
    # or a list of paths to overlay - same function, no separate API.
    if isinstance(comparison_paths, (str, pathlib.Path)):
        comparison_paths = [pathlib.Path(comparison_paths)]
    else:
        comparison_paths = [pathlib.Path(p) for p in comparison_paths]

    color_cycle = plt.rcParams["axes.prop_cycle"].by_key()["color"]
    fig, ax = plt.subplots(figsize=(8, 5))
    labels = []
    any_excluded = False
    for i, path in enumerate(comparison_paths):
        label, stages_pct, thrust, thrust_sd, excluded_pct = _read_series(path)
        labels.append(label)
        color = color_cycle[i % len(color_cycle)]
        ax.plot(stages_pct, thrust, marker="o", color=color, label=label)
        lower = [t - s for t, s in zip(thrust, thrust_sd)]
        upper = [t + s for t, s in zip(thrust, thrust_sd)]
        ax.fill_between(stages_pct, lower, upper, color=color, alpha=0.15)
        for p in excluded_pct:
            ax.axvline(p, color=color, linestyle=":", alpha=0.4)
        any_excluded = any_excluded or bool(excluded_pct)
    if any_excluded:
        # One neutral proxy entry for the legend - which colour an excluded
        # stage belongs to is already visible from the vertical line itself.
        ax.plot([], [], color="0.4", linestyle=":", label="excluded stage")

    ax.set_xlabel("Throttle (%)")
    ax.set_ylabel("Thrust (g)")
    title = f"Thrust vs. throttle - {labels[0]}" if len(labels) == 1 else "Thrust vs. throttle"
    ax.set_title(title)
    ax.legend()
    ax.grid(True, alpha=0.3)

    if len(comparison_paths) == 1:
        stem = comparison_paths[0].stem
        suffix = "_COMPARISON"
        out_name = (stem[:-len(suffix)] if stem.endswith(suffix) else stem) + "_thrust.png"
    else:
        out_name = "_vs_".join(labels) + "_thrust.png"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / out_name
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"Written: {out_path}")
    return out_path


def _read_thrust_xy_series(comparison_path: pathlib.Path, y_field: str):
    naming.parse_comparison_filename(comparison_path.name)

    header = csvio.read_header(comparison_path)
    rows = _read_rows(comparison_path)

    # Same NaN-breaks-the-line handling as _read_series above (docs/ARCHITECTURE.md
    # decision 6) - an excluded stage is missing from both axes here, not
    # just flagged on one of them.
    thrust = [float(r["thrust_g_median"]) if r["included"] == "yes" else float("nan") for r in rows]
    y_vals = [float(r[y_field]) if r["included"] == "yes" else float("nan") for r in rows]
    label = header.get("label", comparison_path.stem)
    return label, thrust, y_vals


def _plot_vs_thrust(comparison_paths, y_field: str, y_label: str, out_suffix: str,
                     out_dir: pathlib.Path, quiet: bool = False) -> pathlib.Path:
    """Shared by plot_power_vs_thrust and plot_g_per_w_vs_thrust - both are
    "some per-stage metric against thrust instead of throttle", differing
    only in which COMPARISON column and axis label to use."""
    if isinstance(comparison_paths, (str, pathlib.Path)):
        comparison_paths = [pathlib.Path(comparison_paths)]
    else:
        comparison_paths = [pathlib.Path(p) for p in comparison_paths]

    color_cycle = plt.rcParams["axes.prop_cycle"].by_key()["color"]
    fig, ax = plt.subplots(figsize=(8, 5))
    labels = []
    for i, path in enumerate(comparison_paths):
        label, thrust, y_vals = _read_thrust_xy_series(path, y_field)
        labels.append(label)
        color = color_cycle[i % len(color_cycle)]
        ax.plot(thrust, y_vals, marker="o", color=color, label=label)

    ax.set_xlabel("Thrust (g)")
    ax.set_ylabel(y_label)
    title_stem = f"{y_label} vs. thrust"
    title = f"{title_stem} - {labels[0]}" if len(labels) == 1 else title_stem
    ax.set_title(title)
    ax.legend()
    ax.grid(True, alpha=0.3)

    if len(comparison_paths) == 1:
        stem = comparison_paths[0].stem
        suffix = "_COMPARISON"
        out_name = (stem[:-len(suffix)] if stem.endswith(suffix) else stem) + out_suffix
    else:
        out_name = "_vs_".join(labels) + out_suffix
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / out_name
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    if not quiet:
        print(f"Written: {out_path}")
    return out_path


# quiet=True suppresses the "Written:" line - needed by
# acquisition/session.py, which calls these from inside its Textual UI and
# shows the paths there instead (same reason analyzer.analyze() has quiet).
def plot_power_vs_thrust(comparison_paths, out_dir: pathlib.Path = DEFAULT_OUT_DIR,
                          quiet: bool = False) -> pathlib.Path:
    return _plot_vs_thrust(comparison_paths, "power_w_median", "Power (W)",
                            "_power_vs_thrust.png", out_dir, quiet)


def plot_g_per_w_vs_thrust(comparison_paths, out_dir: pathlib.Path = DEFAULT_OUT_DIR,
                            quiet: bool = False) -> pathlib.Path:
    return _plot_vs_thrust(comparison_paths, "g_per_w_median", "Efficiency (g/W)",
                            "_efficiency_vs_thrust.png", out_dir, quiet)


def main():
    args = parse_args()
    out_dir = args.out_dir if args.out_dir is not None else DEFAULT_OUT_DIR
    if args.kind == "power_vs_thrust":
        plot_power_vs_thrust(args.comparison_paths, out_dir)
    elif args.kind == "g_per_w_vs_thrust":
        plot_g_per_w_vs_thrust(args.comparison_paths, out_dir)
    else:
        plot_thrust(args.comparison_paths, out_dir)


if __name__ == "__main__":
    main()
