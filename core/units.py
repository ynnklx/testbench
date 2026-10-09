"""Physical/hardware unit conversions shared across the host tools.

These are fixed constants - the INA226 register scaling comes from the shunt
resistor value and the chip's fixed LSB sizes (see docs/ARCHITECTURE.md, "Serial
Protokoll"), not from a calibration run. Load-cell conversion is different in
kind (a per-device linear fit) and stays in core/calibration.py alongside
config/calibration.json.
"""

G_STANDARD = 9.80665  # m/s^2

SHUNT_UV_PER_COUNT = 2.5
SHUNT_OHM = 0.002
BUS_MV_PER_COUNT = 1.25

# Session zero point from the 3 s countdown: discard the first and last
# second (the rig can still be settling right after the button press, and
# the transition into step 1 begins towards the end), take the median of
# the middle second. Shared by acquisition/recorder.py (live display) and
# analysis/points.py (the actual raw-to-grams conversion) - both must use
# exactly the same trim, or the two would disagree on the very value the
# live display is supposed to preview.
IDLE_TRIM_US = 1_000_000


def raw_shunt_to_amps(raw_shunt: int) -> float:
    return raw_shunt * SHUNT_UV_PER_COUNT / 1e6 / SHUNT_OHM


def raw_bus_to_volts(raw_bus: int) -> float:
    return raw_bus * BUS_MV_PER_COUNT / 1000.0


def grams_to_newtons(grams: float) -> float:
    return grams / 1000.0 * G_STANDARD
