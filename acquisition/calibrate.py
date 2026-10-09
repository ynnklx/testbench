"""Multi-point load cell calibration: measure known reference masses and write
a linear fit (raw -> grams) to config/calibration.json.

Per reference point: place the mass by hand (0 g = empty rig), confirm with
Enter, then send TARE <n> to the device. It averages n raw values and reports
mean and standard deviation, but does not apply anything itself - the fit
(least squares through all points) happens here on the host, keeping the
firmware raw-values-only.

At least 2 reference masses are required. From 3 points on, the residual per
point is printed as a simple linearity check. That check matters: a single
badly seated mass distorts the whole fit while still looking fine in the
resulting slope - it happened once, with a 14 % residual at one point while all
others stayed below 2 %.

Placing and removing masses stays manual, so the script waits for Enter at
every point, including in --simulate mode.
"""
import argparse
import datetime
import json
import pathlib
import sys

from acquisition import link

CONFIG_PATH = pathlib.Path(__file__).resolve().parent.parent / "config" / "calibration.json"


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    link.add_link_args(parser)
    parser.add_argument(
        "--weights-g", required=True,
        help="Comma separated reference masses in grams, e.g. 0,200,400 "
             "(at least 2; 0 g means the empty rig)",
    )
    parser.add_argument(
        "--tare-n", type=int, default=200,
        help="Number of raw samples per reference point (default: 200)",
    )
    parser.add_argument("--comment", default="")
    return parser.parse_args()


def read_reference_point(conn, mass_g, n):
    input(f"Place reference mass {mass_g:g} g (0 g = empty rig), then press Enter ...")
    conn.send(f"TARE {n}")
    line = link.wait_for_line(
        conn, lambda l: l.startswith("OK TARE") or l.startswith("ERR"), timeout_s=30.0
    )
    if not line or not line.startswith("OK TARE"):
        print(f"TARE failed at {mass_g:g} g: {line!r}", file=sys.stderr)
        sys.exit(1)
    _, _, mean_s, sd_s, n_s = line.split(" ")
    return {"mass_g": mass_g, "raw_mean": float(mean_s), "raw_sd": float(sd_s), "n": int(n_s)}


def fit_linear(points):
    """Least squares: raw = raw_zero + counts_per_gram * mass. Plain Python is
    enough for a single 1D fit - no numpy dependency needed."""
    n = len(points)
    xs = [p["mass_g"] for p in points]
    ys = [p["raw_mean"] for p in points]
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    num = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    den = sum((x - mean_x) ** 2 for x in xs)
    if den == 0:
        print("All reference masses identical - no fit possible.", file=sys.stderr)
        sys.exit(1)
    counts_per_gram = num / den
    raw_zero = mean_y - counts_per_gram * mean_x
    return raw_zero, counts_per_gram


def main():
    args = parse_args()
    try:
        masses = [float(m) for m in args.weights_g.split(",")]
    except ValueError:
        print(f"Invalid --weights-g: {args.weights_g!r}", file=sys.stderr)
        sys.exit(1)
    if len(masses) < 2:
        print("At least 2 reference masses required.", file=sys.stderr)
        sys.exit(1)

    conn = link.open_link(args)
    conn.send("ID?")
    device_line = link.wait_for_line(
        conn, lambda l: l.startswith("OK ID") or l.startswith("ERR"), timeout_s=3.0
    )
    if not device_line or not device_line.startswith("OK ID"):
        print(f"No contact with the device: {device_line!r}", file=sys.stderr)
        sys.exit(1)
    print(f"Connected: {device_line}\n")

    points = []
    for mass_g in masses:
        point = read_reference_point(conn, mass_g, args.tare_n)
        print(f"  {mass_g:g} g -> raw_mean={point['raw_mean']:.1f}, "
              f"raw_sd={point['raw_sd']:.2f}, n={point['n']}\n")
        points.append(point)
    conn.close()

    raw_zero, counts_per_gram = fit_linear(points)

    print("=== Linearity check (residuals) ===")
    for p in points:
        predicted = raw_zero + counts_per_gram * p["mass_g"]
        residual_raw = p["raw_mean"] - predicted
        residual_g = residual_raw / counts_per_gram
        print(f"  {p['mass_g']:g} g: residual {residual_raw:+.1f} counts "
              f"({residual_g:+.2f} g)")

    now = datetime.datetime.now()
    data = {
        "schema_version": 2,
        "calibrated_at": now.strftime("%Y-%m-%d"),
        "provisional": False,
        "comment": (
            f"{len(points)}-point calibration via host/calibrate.py"
            + (f" - {args.comment}" if args.comment else "")
        ),
        "unit": "grams",
        "reference_points": points,
        "fit": {
            "method": "least-squares-linear",
            "raw_zero": raw_zero,
            "counts_per_gram": counts_per_gram,
        },
    }

    with open(CONFIG_PATH, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")

    print(f"\nWritten: {CONFIG_PATH}")
    print(f"fit: raw_zero={raw_zero:.3f}, counts_per_gram={counts_per_gram:.5f}")


if __name__ == "__main__":
    main()
