import pytest

from analysis import stats


def test_median_and_sd_empty_and_single():
    assert stats.median([]) is None
    assert stats.median([5]) == 5
    assert stats.sd([]) is None
    assert stats.sd([5]) is None
    assert stats.sd([1, 2, 3]) == pytest.approx(1.0)


def test_mad_robust_to_single_outlier():
    values = [10.0] * 9 + [1000.0]  # one corrupted HX711-style sample
    assert stats.median(values) == 10.0
    assert stats.mad(values) == 0.0  # 9/10 values sit exactly on the median


def test_robust_cv_pct_handles_zero_median():
    assert stats.robust_cv_pct([]) is None
    assert stats.robust_cv_pct([-1, 0, 1]) is None  # median 0 - nothing to divide by
    assert stats.robust_cv_pct([10, 10, 10, 12]) == pytest.approx(0.0)


def test_count_mad_outliers_isolated_vs_consecutive():
    # A little realistic jitter around 10.0 - nine identical values would
    # make MAD exactly 0 (an unrealistically clean population no real
    # sensor produces), which the div-by-zero guard correctly refuses to
    # judge outliers against at all.
    base = [9.9, 10.1, 9.8, 10.2, 10.0, 9.9, 10.1, 10.0, 9.8, 10.2]
    values = list(base)
    values[3] = 1000.0  # one isolated corrupted sample
    count, consecutive = stats.count_mad_outliers(values, mad_k=5)
    assert count == 1
    assert consecutive == 1

    values2 = list(base)
    values2[3] = 1000.0
    values2[4] = 1000.0  # two consecutive corrupted samples
    count2, consecutive2 = stats.count_mad_outliers(values2, mad_k=5)
    assert count2 == 2
    assert consecutive2 == 2


def test_count_mad_outliers_no_spread_is_safe():
    assert stats.count_mad_outliers([5, 5, 5], mad_k=3) == (0, 0)
    assert stats.count_mad_outliers([5], mad_k=3) == (0, 0)


def test_settle_ratio_detects_trend():
    # A clear upward trend across the window - first third well below the
    # last third relative to the overall MAD.
    trending = list(range(30))
    ratio = stats.settle_ratio([float(v) for v in trending])
    assert ratio is not None and ratio > 1.0

    stable = [10.0] * 30
    assert stats.settle_ratio(stable) is None  # MAD is 0 - nothing to divide by

    assert stats.settle_ratio([1.0, 2.0, 3.0]) is None  # below the 9-sample minimum


def test_linreg_basic_fit():
    xs = [0, 1, 2, 3, 4]
    ys = [1, 3, 5, 7, 9]  # y = 1 + 2x, exact fit
    fit = stats.linreg(xs, ys)
    assert fit["slope"] == pytest.approx(2.0)
    assert fit["intercept"] == pytest.approx(1.0)
    assert fit["residual_sd"] == pytest.approx(0.0, abs=1e-9)
    assert fit["n"] == 5


def test_linreg_degenerate_cases():
    assert stats.linreg([1], [1]) is None
    assert stats.linreg([2, 2, 2], [1, 2, 3]) is None  # all-identical-x
