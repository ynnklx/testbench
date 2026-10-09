"""Serial connection to the test stand.

Two implementations behind the same narrow interface (send/readline/close):
a real serial link, and a simulation that needs no hardware (--simulate), so
the host tools can be developed and exercised without the rig attached.
"""
import argparse
import random
import time


def wait_for_line(conn, predicate, timeout_s=None):
    """Reads lines until predicate(line) is true, then returns that line.
    timeout_s=None waits indefinitely. Shared by test_run.py and calibrate.py."""
    deadline = time.monotonic() + timeout_s if timeout_s else None
    while True:
        if deadline and time.monotonic() > deadline:
            return None
        line = conn.readline(timeout=0.3)
        if line is None:
            continue
        if predicate(line):
            return line


class Link:
    def send(self, cmd: str) -> None:
        raise NotImplementedError

    def readline(self, timeout: float = 1.0):
        """Returns one received line (without \\n), or None on timeout."""
        raise NotImplementedError

    def close(self) -> None:
        pass


class LinkError(Exception):
    pass


class SerialLink(Link):
    # After opening the port (and especially after a fresh flash) the USB
    # serial adapter sometimes needs several hundred ms to seconds before it
    # answers cleanly - root cause never fully established. Hence an active
    # ID? probe instead of trusting a fixed delay.
    _HANDSHAKE_ATTEMPTS = 8
    _HANDSHAKE_RETRY_DELAY_S = 0.4
    # If a measurement run is already in progress (button pressed before the
    # host connected) or the stream is active, D/I/E lines arrive every few ms
    # and the "OK ID" reply sits behind a backlog rather than being next in
    # line. So scan several lines per attempt instead of just one.
    _HANDSHAKE_DRAIN_READS = 20

    def __init__(self, port: str, baud: int):
        import serial

        self._ser = serial.Serial(port, baud, timeout=0.3)
        # Opening the port resets the ESP32 via DTR/RTS - give it time to boot
        # before sending commands.
        time.sleep(1.5)
        self._ser.reset_input_buffer()

        for _ in range(self._HANDSHAKE_ATTEMPTS):
            self.send("ID?")
            for _ in range(self._HANDSHAKE_DRAIN_READS):
                line = self.readline(timeout=0.3)
                if line and line.startswith("OK ID"):
                    return
            time.sleep(self._HANDSHAKE_RETRY_DELAY_S)
        raise LinkError(
            f"No clean handshake with the device on {port} after "
            f"{self._HANDSHAKE_ATTEMPTS} attempts."
        )

    def send(self, cmd: str) -> None:
        self._ser.write((cmd + "\n").encode())

    def readline(self, timeout: float = 1.0):
        self._ser.timeout = timeout
        line = self._ser.readline().decode(errors="replace").strip()
        return line or None

    def close(self) -> None:
        self._ser.close()


class SimulatedLink(Link):
    """Sequence-level simulation for host tools without hardware - not a
    physical model of load cell or INA226, just plausible numbers so the script
    logic can be exercised.

    It also mimics the device's self-contained measurement run: discrete
    throttle steps with a settle and a hold phase each, ramping up from
    _SIM_STAGE_START_PROMILLE to full throttle and back down again (see
    esc.h's class comment for why), until the simulated RPM reaches the limit
    or the downward leg reaches the start again. Settle and hold times are
    heavily compressed so several cycles can be replayed quickly - no physical
    meaning. RPM is assumed proportional to throttle, with no motor or
    propeller model. Does not model a voltage sag or the RPM limit being hit
    on the way up (simulated RPM never reaches _SIM_RPM_MAX within 0-1000
    promille) - those two upward-leg reversals stay untested by --simulate,
    same as before this class gained the up/down sweep.

    Also mimics the firmware's "# MODE"/"# PARAMS" lines right after
    "# ARMED (button)" (firmware/src/esc.cpp) - always sweep, since this
    class never runs a static-mode cycle. _SIM_PARAMS_LINE reports the real
    firmware constants (STAGE_START_PROMILLE etc.), not the compressed sim
    timing below - it documents what a real run would report, the
    compression here is only about replaying events quickly.
    """

    # Mirrors STAGE_START_PROMILLE / STAGE_STEP_PROMILLE / RPM_LIMIT in
    # firmware/src/esc.cpp - kept in sync by hand.
    _SIM_STAGE_START_PROMILLE = 150  # 15 %
    _SIM_STAGE_STEP_PROMILLE = 25  # 2.5 %
    _SIM_RPM_MAX = 25000
    # Compressed compared to the real 1000 ms / 2.5 s.
    _SIM_STAGE_SETTLE_S = 0.1
    _SIM_STAGE_HOLD_S = 0.2
    _SIM_REARM_DELAY_S = 2.0

    # See class comment - the real firmware constants, not the compressed
    # timing above.
    _SIM_PARAMS_LINE = (
        "# PARAMS start=150 step=25 settle_ms=1000 hold_ms=2500 rpm_limit=25000 "
        "temp_max_c=80 sag_fraction=0.90 pole_pairs=7"
    )

    def __init__(self):
        self._seq_d = 0
        self._seq_i = 0
        self._seq_e = 0
        self._streaming = False
        self._armed = False
        self._throttle_promille = 0
        self._stage_phase = "settle"
        self._sweep_going_up = True
        self._start_time = time.monotonic()
        self._next_event_at = time.monotonic() + 2.0
        self._tick = 0
        self._pending = []

    def send(self, cmd: str) -> None:
        if cmd == "ID?":
            self._pending.append("OK ID pruefstand-SIM fw=1.0.0-sim rate=10")
        elif cmd == "PING":
            self._pending.append(f"OK PONG {self._now_us()}")
        elif cmd == "START":
            self._streaming = True
            self._seq_d = 0
            self._seq_i = 0
            self._seq_e = 0
            self._pending.append("OK START")
        elif cmd == "STOP":
            self._streaming = False
            self._pending.append("OK STOP")
        elif cmd.startswith("SET "):
            promille = int(cmd.split(" ", 1)[1])
            if not self._armed:
                self._pending.append("ERR SET not armed")
            else:
                self._throttle_promille = promille
                self._pending.append(f"OK SET {promille}")
        elif cmd.startswith("TARE "):
            n = int(cmd.split(" ", 1)[1])
            # No physical model - the simulation cannot know which reference
            # mass was placed by hand (see calibrate.py). Just a plausible
            # reply so the calibration script logic can be exercised.
            mean = -165000.0 + random.uniform(-500, 500)
            sd = abs(random.gauss(30, 5))
            self._pending.append(f"OK TARE {mean:.3f} {sd:.3f} {n}")
        else:
            self._pending.append(f"ERR {cmd} unknown")

    def readline(self, timeout: float = 1.0):
        if self._pending:
            return self._pending.pop(0)

        now = time.monotonic()
        if not self._armed:
            if now >= self._next_event_at:
                self._armed = True
                self._throttle_promille = self._SIM_STAGE_START_PROMILLE
                self._stage_phase = "settle"
                self._sweep_going_up = True
                self._next_event_at = now + self._SIM_STAGE_SETTLE_S
                self._pending.append("# MODE sweep")
                self._pending.append(self._SIM_PARAMS_LINE)
                self._pending.append(f"# STAGE {self._throttle_promille}")
                return "# ARMED (button)"
        else:
            simulated_rpm = self._throttle_promille * 10
            if simulated_rpm >= self._SIM_RPM_MAX:
                self._armed = False
                self._throttle_promille = 0
                self._next_event_at = now + self._SIM_REARM_DELAY_S
                return "# DISARMED (RPM limit reached)"
            if now >= self._next_event_at:
                if self._stage_phase == "settle":
                    self._stage_phase = "hold"
                    self._next_event_at = now + self._SIM_STAGE_HOLD_S
                    return f"# HOLD {self._throttle_promille}"
                if self._sweep_going_up:
                    next_promille = self._throttle_promille + self._SIM_STAGE_STEP_PROMILLE
                    if next_promille > 1000:
                        # Reached full throttle - reverse instead of ending,
                        # same as esc.cpp's startDownwardLeg(): one step below
                        # the peak stage, not a second reading of the peak
                        # itself.
                        self._sweep_going_up = False
                        next_promille = self._throttle_promille - self._SIM_STAGE_STEP_PROMILLE
                else:
                    next_promille = self._throttle_promille - self._SIM_STAGE_STEP_PROMILLE
                    if next_promille < self._SIM_STAGE_START_PROMILLE:
                        self._armed = False
                        self._throttle_promille = 0
                        self._sweep_going_up = True
                        self._next_event_at = now + self._SIM_REARM_DELAY_S
                        return "# DISARMED (measurement complete)"
                self._throttle_promille = next_promille
                self._stage_phase = "settle"
                self._next_event_at = now + self._SIM_STAGE_SETTLE_S
                return f"# STAGE {self._throttle_promille}"

        if not self._streaming:
            time.sleep(min(timeout, 0.05))
            return None

        time.sleep(0.02)
        self._tick += 1
        if self._tick % 12 == 0:
            # ~one E frame every 30 ms, matching the real AM32 telemetry
            # interval. current_ca/consumption_mah stay placeholders - they are
            # unusable on the real ESC anyway.
            self._seq_e += 1
            rpm = self._throttle_promille * 10
            erpm = rpm * 7
            return (f"E,{self._seq_e - 1},{self._now_us()},{rpm},{erpm},32,"
                    f"1200,0,0")
        elif self._tick % 4 != 0:
            self._seq_i += 1
            raw_shunt = int(10 + self._throttle_promille * 1.2 + random.randint(-2, 2))
            # 16000 counts * 1.25 mV/count = 20 V, a typical test voltage in
            # this project - not a model of a real power supply.
            raw_bus = 16000 + random.randint(-20, 20)
            return f"I,{self._seq_i - 1},{self._now_us()},{raw_shunt},{raw_bus}"
        else:
            self._seq_d += 1
            raw = -165500 - self._throttle_promille * 5 + random.randint(-50, 50)
            return f"D,{self._seq_d - 1},{self._now_us()},{raw}"

    def _now_us(self) -> int:
        return int((time.monotonic() - self._start_time) * 1_000_000)


def add_link_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--simulate", action="store_true",
        help="Run without hardware against the simulated link",
    )
    parser.add_argument("--port", default="/dev/ttyUSB0")
    parser.add_argument("--baud", type=int, default=921600)


def open_link(args) -> Link:
    if args.simulate:
        return SimulatedLink()
    return SerialLink(args.port, args.baud)
