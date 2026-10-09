"""Loads config/calibration.json and converts raw values to grams/newtons.

The firmware is unaffected by this - it always emits raw values only. The
conversion is purely a host concern and can be applied to already recorded raw
CSV files without re-measuring.
"""
import json
import pathlib

from core.units import grams_to_newtons

CONFIG_PATH = pathlib.Path(__file__).resolve().parent.parent / "config" / "calibration.json"


class Calibration:
    def __init__(self, data: dict):
        self.raw_zero = data["fit"]["raw_zero"]
        self.counts_per_gram = data["fit"]["counts_per_gram"]
        self.provisional = data.get("provisional", False)
        self.calibrated_at = data.get("calibrated_at", "")

    def raw_to_grams(self, raw: float) -> float:
        return (raw - self.raw_zero) / self.counts_per_gram

    def raw_to_newtons(self, raw: float) -> float:
        return grams_to_newtons(self.raw_to_grams(raw))


def load(path: pathlib.Path = CONFIG_PATH) -> Calibration:
    with open(path) as f:
        data = json.load(f)
    return Calibration(data)


def try_load(path: pathlib.Path = CONFIG_PATH):
    """Like load(), but returns None instead of raising when no calibration
    file exists yet - raw values then simply stay unconverted."""
    if not path.exists():
        return None
    return load(path)
