"""Pure statistics helpers shared by points.py and validation.py - no I/O,
no CSV/JSON formats, no knowledge of the project's specific finding codes.
Standard library only, so this stays testable without installing anything
(docs/ARCHITECTURE.md, dependency boundaries).
"""
import statistics


def median(values):
    return statistics.median(values) if values else None


def sd(values):
    """Sample standard deviation, defined around the arithmetic mean - a
    pure spread measure (docs/ARCHITECTURE.md, "Diagnostics that are kept"), not a
    location statistic. None below two samples."""
    if len(values) < 2:
        return None
    return statistics.stdev(values)


def mad(values):
    """Median absolute deviation - robust against the single-corrupted-
    HX711-sample phenomenon (docs/ARCHITECTURE.md, known pitfalls) the way sd() is not:
    one bad sample barely moves a median, unlike a mean-based measure."""
    if not values:
        return None
    m = statistics.median(values)
    return statistics.median([abs(v - m) for v in values])


def robust_cv_pct(values):
    """MAD/median as a percentage - None if there is nothing to divide by
    (empty, or a median of exactly zero)."""
    if not values:
        return None
    m = statistics.median(values)
    if not m:
        return None
    return mad(values) / m * 100


def count_mad_outliers(values, mad_k):
    """(count, max_consecutive_run) of samples further than mad_k raw MAD
    multiples from the median - config/validation.json's
    unstable_measurement.mad_k convention (a raw MAD multiple, not the
    1.4826-scaled/z-score MAD - see the reason field in validation.json for
    the calibration behind the chosen k). (0, 0) when there are too few samples or no
    spread to judge against."""
    if len(values) < 2:
        return 0, 0
    m = statistics.median(values)
    spread = mad(values)
    if not spread:
        return 0, 0
    is_outlier = [abs(v - m) > mad_k * spread for v in values]
    count = sum(is_outlier)
    best = run = 0
    for outlier in is_outlier:
        run = run + 1 if outlier else 0
        best = max(best, run)
    return count, best


def settle_ratio(values):
    """|median(last third) - median(first third)| / MAD - the
    settling_incomplete check (docs/ARCHITECTURE.md): whether the reading was still
    trending within its own hold window. None below 9 samples (need a
    meaningful third at each end) or with no spread to divide by."""
    n = len(values)
    if n < 9:
        return None
    spread = mad(values)
    if not spread:
        return None
    third = n // 3
    return abs(median(values[-third:]) - median(values[:third])) / spread


def linreg(xs, ys):
    """Simple OLS y = intercept + slope*x, ported from V1's
    analyse/stage_summary.py. None for fewer than 2 points or a degenerate
    (all-identical-x) input. residual_sd uses n-2 degrees of freedom (two
    fitted parameters) - None below n=3, where a scatter estimate is not
    meaningful."""
    n = len(xs)
    if n < 2:
        return None
    mean_x, mean_y = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mean_x) ** 2 for x in xs)
    if sxx == 0:
        return None
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    slope = sxy / sxx
    intercept = mean_y - slope * mean_x
    residual_sd = None
    if n > 2:
        sse = sum((y - (intercept + slope * x)) ** 2 for x, y in zip(xs, ys))
        residual_sd = (sse / (n - 2)) ** 0.5
    return {"slope": slope, "intercept": intercept, "residual_sd": residual_sd, "n": n}
