"""Builds and parses the project's file names (docs/ARCHITECTURE.md, "File names").

    <datum>_<zeit>_<testtyp>[<prozent>][_<n>of<m>]_<STUFE>.csv

    2026-09-11_13-42-18_sweep_1of3_RAW.csv
    2026-09-11_13-42-18_sweep_1of3_ANALYZED.csv
    2026-09-11_14-20-10_static70_1of3_RAW.csv
    2026-09-11_16-08-55_sweep_RAW.csv              # Recorder allein
    2026-09-11_15-02-44_prop365-16v_COMPARISON.csv

RAW/ANALYZED file names (build_run_filename/parse_run_filename) share one
shape: a timestamp, a test type with an optional stage percentage, an
optional repetition tag, then the pipeline stage. COMPARISON file names
(build_comparison_filename/parse_comparison_filename) are a different, looser
shape - a timestamp plus a free-text label, no test type or repetition - so
they get their own pair of functions instead of bolting more optional fields
onto the first.
"""
import dataclasses
import datetime
import re

_DATE_FMT = "%Y-%m-%d"
_TIME_FMT = "%H-%M-%S"
_TIMESTAMP_RE = r"\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}"

RUN_TEST_TYPES = ("sweep", "static")
RUN_FILE_STAGES = ("RAW", "ANALYZED")

_RUN_RE = re.compile(
    rf"^(?P<timestamp>{_TIMESTAMP_RE})_"
    rf"(?P<test_type>{'|'.join(RUN_TEST_TYPES)})(?P<percent>\d+(?:\.\d)?)?"
    rf"(?:_(?P<rep_n>\d+)of(?P<rep_m>\d+))?_"
    rf"(?P<file_stage>{'|'.join(RUN_FILE_STAGES)})\.csv$"
)

_COMPARISON_RE = re.compile(
    rf"^(?P<timestamp>{_TIMESTAMP_RE})_(?P<label>.+)_COMPARISON\.csv$"
)


def promille_to_percent_str(promille: int) -> str:
    """700 -> '70', 675 -> '67.5' - exact because promille is an integer, so
    promille/10 has at most one decimal digit."""
    return f"{promille / 10:g}"


def percent_str_to_promille(percent_str: str) -> int:
    """Inverse of promille_to_percent_str(). Integer arithmetic on the
    string, not float parsing - avoids binary-fraction rounding for values
    like 67.5."""
    whole, sep, frac = percent_str.partition(".")
    if sep and len(frac) != 1:
        raise ValueError(f"Unexpected percent format: {percent_str!r}")
    promille = int(whole) * 10
    if frac:
        promille += int(frac)
    return promille


def parse_mode_value(value: str) -> tuple[str, int | None]:
    """Parses the value of the firmware's "# MODE" line (docs/ARCHITECTURE.md, "# MODE and
    # PARAMS") with the "# MODE " prefix already stripped: "sweep" ->
    ("sweep", None), "static 700" -> ("static", 700) - promille, not
    percent, matching "# STAGE"/"# HOLD" and the file-naming convention
    ("Promille in den Daten"). Shared by acquisition/recorder.py (parses the
    live line) and analysis/points.py-adjacent code (parses the RAW header's
    "mode" field, which holds this same value verbatim) - one place decides
    what a MODE value means, not two."""
    fields = value.split()
    if fields == ["sweep"]:
        return "sweep", None
    if len(fields) == 2 and fields[0] == "static":
        try:
            return "static", int(fields[1])
        except ValueError:
            pass
    raise ValueError(f"Malformed MODE value: {value!r}")


@dataclasses.dataclass(frozen=True)
class RunFilenameParts:
    timestamp: datetime.datetime
    test_type: str  # "sweep" or "static"
    stage_promille: int | None = None  # required for static, must be None for sweep
    repetition: tuple[int, int] | None = None  # (n, m), optional
    file_stage: str = "RAW"  # "RAW" or "ANALYZED"


@dataclasses.dataclass(frozen=True)
class ComparisonFilenameParts:
    timestamp: datetime.datetime
    label: str


def build_run_filename(parts: RunFilenameParts) -> str:
    if parts.test_type not in RUN_TEST_TYPES:
        raise ValueError(f"Unknown test_type: {parts.test_type!r}")
    if parts.file_stage not in RUN_FILE_STAGES:
        raise ValueError(f"Unknown file_stage: {parts.file_stage!r}")
    if parts.test_type == "static" and parts.stage_promille is None:
        raise ValueError("static needs stage_promille")
    if parts.test_type == "sweep" and parts.stage_promille is not None:
        raise ValueError("sweep must not carry stage_promille")

    ts = parts.timestamp.strftime(f"{_DATE_FMT}_{_TIME_FMT}")
    type_part = parts.test_type
    if parts.stage_promille is not None:
        type_part += promille_to_percent_str(parts.stage_promille)
    rep_part = ""
    if parts.repetition is not None:
        n, m = parts.repetition
        rep_part = f"_{n}of{m}"
    return f"{ts}_{type_part}{rep_part}_{parts.file_stage}.csv"


def parse_run_filename(name: str) -> RunFilenameParts:
    m = _RUN_RE.match(name)
    if not m:
        raise ValueError(f"Not a RAW/ANALYZED file name: {name!r}")
    timestamp = datetime.datetime.strptime(m["timestamp"], f"{_DATE_FMT}_{_TIME_FMT}")
    stage_promille = percent_str_to_promille(m["percent"]) if m["percent"] else None
    repetition = (int(m["rep_n"]), int(m["rep_m"])) if m["rep_n"] else None
    return RunFilenameParts(
        timestamp=timestamp,
        test_type=m["test_type"],
        stage_promille=stage_promille,
        repetition=repetition,
        file_stage=m["file_stage"],
    )


def build_comparison_filename(parts: ComparisonFilenameParts) -> str:
    ts = parts.timestamp.strftime(f"{_DATE_FMT}_{_TIME_FMT}")
    return f"{ts}_{parts.label}_COMPARISON.csv"


def parse_comparison_filename(name: str) -> ComparisonFilenameParts:
    m = _COMPARISON_RE.match(name)
    if not m:
        raise ValueError(f"Not a COMPARISON file name: {name!r}")
    timestamp = datetime.datetime.strptime(m["timestamp"], f"{_DATE_FMT}_{_TIME_FMT}")
    return ComparisonFilenameParts(timestamp=timestamp, label=m["label"])
