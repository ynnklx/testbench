import datetime

import pytest

from core import naming


def test_percent_promille_round_trip():
    for promille in (0, 150, 250, 675, 700, 1000):
        percent = naming.promille_to_percent_str(promille)
        assert naming.percent_str_to_promille(percent) == promille


def test_percent_str_examples():
    assert naming.promille_to_percent_str(700) == "70"
    assert naming.promille_to_percent_str(675) == "67.5"


@pytest.mark.parametrize(
    "parts",
    [
        naming.RunFilenameParts(
            timestamp=datetime.datetime(2026, 9, 11, 13, 42, 18),
            test_type="sweep",
            repetition=(1, 3),
            file_stage="RAW",
        ),
        naming.RunFilenameParts(
            timestamp=datetime.datetime(2026, 9, 11, 13, 42, 18),
            test_type="sweep",
            repetition=(1, 3),
            file_stage="ANALYZED",
        ),
        naming.RunFilenameParts(
            timestamp=datetime.datetime(2026, 9, 11, 14, 20, 10),
            test_type="static",
            stage_promille=700,
            repetition=(1, 3),
            file_stage="RAW",
        ),
        naming.RunFilenameParts(
            timestamp=datetime.datetime(2026, 9, 11, 14, 20, 10),
            test_type="static",
            stage_promille=675,
            file_stage="RAW",
        ),
        naming.RunFilenameParts(
            timestamp=datetime.datetime(2026, 9, 11, 16, 8, 55),
            test_type="sweep",
            file_stage="RAW",
        ),
    ],
)
def test_run_filename_round_trip(parts):
    name = naming.build_run_filename(parts)
    assert naming.parse_run_filename(name) == parts


def test_run_filename_examples_from_architecture_doc():
    assert naming.build_run_filename(naming.RunFilenameParts(
        timestamp=datetime.datetime(2026, 9, 11, 13, 42, 18),
        test_type="sweep", repetition=(1, 3), file_stage="RAW",
    )) == "2026-09-11_13-42-18_sweep_1of3_RAW.csv"
    assert naming.build_run_filename(naming.RunFilenameParts(
        timestamp=datetime.datetime(2026, 9, 11, 13, 42, 18),
        test_type="sweep", repetition=(1, 3), file_stage="ANALYZED",
    )) == "2026-09-11_13-42-18_sweep_1of3_ANALYZED.csv"
    assert naming.build_run_filename(naming.RunFilenameParts(
        timestamp=datetime.datetime(2026, 9, 11, 14, 20, 10),
        test_type="static", stage_promille=700, repetition=(1, 3), file_stage="RAW",
    )) == "2026-09-11_14-20-10_static70_1of3_RAW.csv"
    assert naming.build_run_filename(naming.RunFilenameParts(
        timestamp=datetime.datetime(2026, 9, 11, 16, 8, 55),
        test_type="sweep", file_stage="RAW",
    )) == "2026-09-11_16-08-55_sweep_RAW.csv"


@pytest.mark.parametrize(
    "name",
    [
        "2026-09-11_13-42-18_sweep_1of3_RAW.csv",
        "2026-09-11_13-42-18_sweep_1of3_ANALYZED.csv",
        "2026-09-11_14-20-10_static70_1of3_RAW.csv",
        "2026-09-11_16-08-55_sweep_RAW.csv",
    ],
)
def test_parse_run_filename_examples_round_trip(name):
    assert naming.build_run_filename(naming.parse_run_filename(name)) == name


def test_run_filename_rejects_inconsistent_parts():
    with pytest.raises(ValueError):
        naming.build_run_filename(naming.RunFilenameParts(
            timestamp=datetime.datetime(2026, 9, 11, 0, 0, 0),
            test_type="sweep", stage_promille=700, file_stage="RAW",
        ))
    with pytest.raises(ValueError):
        naming.build_run_filename(naming.RunFilenameParts(
            timestamp=datetime.datetime(2026, 9, 11, 0, 0, 0),
            test_type="static", file_stage="RAW",
        ))
    with pytest.raises(ValueError):
        naming.parse_run_filename("not_a_valid_name.csv")


def test_comparison_filename_round_trip():
    parts = naming.ComparisonFilenameParts(
        timestamp=datetime.datetime(2026, 9, 11, 15, 2, 44),
        label="prop365-16v",
    )
    name = naming.build_comparison_filename(parts)
    assert name == "2026-09-11_15-02-44_prop365-16v_COMPARISON.csv"
    assert naming.parse_comparison_filename(name) == parts


def test_parse_comparison_filename_rejects_run_name():
    with pytest.raises(ValueError):
        naming.parse_comparison_filename("2026-09-11_13-42-18_sweep_1of3_RAW.csv")
