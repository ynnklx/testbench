"""Generic mechanics shared by every RAW/ANALYZED/COMPARISON file: provenance
header lines and file immutability (docs/ARCHITECTURE.md decisions 2 and 8).

Deciding *which* keys go into a header - source file hashes, calibration
constants, validation.json's hash, tool version, timestamp - is the job of
the module producing that stage (recorder.py, analyzer.py, compare.py). This
module only knows the generic "# key=value" comment-line format, sitting
above the actual CSV data table, plus hashing and read-only locking.
"""
import dataclasses
import hashlib
import json
import pathlib
import stat

# This project's own version, bumped by hand - every result file carries the
# tool version that produced it (docs/ARCHITECTURE.md decision 8). One constant shared
# by every stage that writes a header (recorder.py, analyzer.py, compare.py).
TOOL_VERSION = "2.0.0"


def sha256_file(path) -> str:
    """Hex digest of a file's contents, for provenance lines pointing at
    source files (docs/ARCHITECTURE.md decision 8)."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write_header(f, lines: list[str]) -> None:
    """Writes provenance lines as '# key=value\\n', one per entry. f must
    already be open for text writing; the caller writes the CSV data table
    right after this call with its own csv.writer."""
    for line in lines:
        f.write(f"# {line}\n")


def read_header(path) -> dict[str, str]:
    """Reads the leading '#'-comment block of a CSV file into a dict,
    stopping at the first non-comment line. Values stay raw strings -
    parsing them into numbers or comparing hashes is up to the caller."""
    header = {}
    with open(path, "r") as f:
        for line in f:
            if not line.startswith("#"):
                break
            content = line[1:].strip()
            if "=" not in content:
                continue
            key, _, value = content.partition("=")
            header[key.strip()] = value.strip()
    return header


def findings_to_json(findings) -> str:
    """Serializes a list of findings - analysis/validation.py's Finding
    dataclass or plain dicts, either works - into one JSON string. Shared by
    a point's own 'findings' CSV cell and a run's 'run_findings' header
    line (docs/ARCHITECTURE.md decision 5: every finding is kept, not just the worst),
    so both use exactly the same encoding. Deliberately generic here (no
    import of analysis.validation.Finding) - core/ must not depend on
    analysis/."""
    def as_dict(f):
        if dataclasses.is_dataclass(f) and not isinstance(f, type):
            return dataclasses.asdict(f)
        return dict(f)
    return json.dumps([as_dict(f) for f in findings])


def findings_from_json(value: str) -> list[dict]:
    """Inverse of findings_to_json(). '' (an empty CSV cell/header value,
    meaning no findings) decodes to []."""
    return json.loads(value) if value else []


def make_readonly(path) -> None:
    """Strips write permission after the recorder closes a RAW file
    (docs/ARCHITECTURE.md decision 2). Every later stage opens with 'r' only - this is
    a guard against accidental edits, not a security boundary."""
    p = pathlib.Path(path)
    mode = p.stat().st_mode
    p.chmod(mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
