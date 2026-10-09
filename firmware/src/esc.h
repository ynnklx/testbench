#pragma once

#include <stdint.h>

#include "display.h"

// ESC control via standard servo PWM (50 Hz, 1000-2000 us). DShot is
// deliberately not implemented - the F35A/AM32 supports it, but it is far more
// effort than a servo signal and buys nothing for this measurement.
//
// Arming happens exclusively through the physical button on GPIO26 (toggle:
// one press arms, another disarms). There is no serial ARM/DISARM and no
// command watchdog - a consequence of that is documented in docs/ARCHITECTURE.md: if the
// host dies mid-run, the motor keeps its last setpoint until someone presses
// the button.
//
// Every arm press also starts a self-contained measurement run, independent of
// the host: armed flash, countdown, then an up/down throttle sweep, then an
// automatic shutdown and self-disarm. Each step has a settle phase (discarded)
// followed by a hold phase (the measurement window); both are announced on the
// serial link as "# STAGE <promille>" and "# HOLD <promille>" so a listening
// host can tag its rows without steering anything - the protocol carries no
// direction marker, a host that cares can infer up/down purely from whether
// consecutive STAGE values increase or decrease (see analyse/stage_summary.py).
//
// Up/down sweep (added 2026-09-05, replacing a single upward run): from
// STAGE_START_PROMILLE to full throttle in STAGE_STEP_PROMILLE steps
// (sweepGoingUp_ true), then back down the same steps to STAGE_START_PROMILLE
// (sweepGoingUp_ false) without a stop at the top - a real run showed a
// temperature-correlated drift in thrust/current (see docs/DEVLOG.md), and pairing
// each stage's upward and downward reading (host-side, see
// analysis/points.py) lets a later/hotter reading be compared against
// an earlier/cooler one at the very same throttle instead of just one number
// per stage. Reaching full throttle reverses direction instead of ending the
// run; the peak stage itself is measured once (upward only) - reversing
// straight back down to remeasure it a few ms later would add nothing.
//
// A run ends in one of five ways: the downward leg reaching
// STAGE_START_PROMILLE (regular completion), telemetry lost, an ESC
// over-temperature, the button pressed again, or - only if triggered again
// on the downward leg, having already reversed once - the RPM limit. On the
// upward leg the RPM limit instead reverses the sweep, letting the running
// step finish its full hold window first, then reversing from the stage
// below it (the last one actually completed) - see startDownwardLeg().
//
// A voltage sag works the same way on the upward leg (finish the running
// step, then reverse one stage below it), but - unlike the RPM limit - never
// ends the run: it is not a safety function, and the supply sitting in its
// own constant-current mode past the peak is not itself a problem, so once
// the downward leg has started a sag is not even checked for any more (see
// stoppingAtVoltageSag_/voltageSagStartMs_). Its only purpose is finding the
// upper end of the range this supply can hold voltage for, exactly once per
// run. Every throttle change - steps, host SET commands, shutdown - goes
// through the same ramp limiter.
//
// The ESC over-temperature cutoff (ESC_TEMP_MAX_C, esc.cpp) is a genuine
// safety limit, unlike the voltage sag check above - the power supply's own
// current limit does nothing to protect against thermal damage. Added
// 2026-09-05 after a real static-mode run reached 71 degC within 90 s and
// was still climbing with no software protection at all up to
// that point. It aborts immediately once the threshold is reached, rather
// than waiting for the current step's hold window like RPM_LIMIT does, and
// unlike RPM_LIMIT/voltage sag it never reverses the sweep - only ends the
// run - on either leg.
//
// A long press on the (separate) tara button toggles "static mode" -
// selected via LocalDisplay::staticModeSelected(), with the target throttle
// itself from LocalDisplay::staticModeThrottlePromille() (adjustable in 10 %
// steps via the same button, see display.h). Both are read once per run in
// startMeasurementRun(). It reuses the exact same armed flash, countdown,
// ramp limiter and safety cutoffs (RPM limit, telemetry loss) - the voltage
// sag check is skipped entirely (see above, it only ever reverses a sweep,
// and static mode never sweeps) - only the step sequence itself is replaced
// by the single chosen throttle held until the arm button disarms it - for
// ESC drift and INA226 long-run validation, see docs/ARCHITECTURE.md.
//
// Two lines follow "# ARMED (button)" on the serial link before the
// countdown even starts: "# MODE sweep" or "# MODE static <promille>" (the
// latter is the only way the host learns the static throttle at all - it is
// set on the device, never sent over serial), then "# PARAMS ..." with every
// constant above that shapes the run (start/step/settle_ms/hold_ms/
// rpm_limit/temp_max_c/sag_fraction/pole_pairs). V1 kept copies of these same
// numbers by hand in up to seven places across the host code plus a
// free-text CSV header comment - the free-text version stayed wrong from
// 2026-09-07 to 2026-09-11 after STAGE_SETTLE_MS changed and nobody updated
// it. This line is the one source; the host reads
// it instead of hard-coding anything.
class EscDriver {
 public:
  // polePairs is not used for anything EscDriver itself computes - it is
  // only carried through into the "# PARAMS" line (see startMeasurementRun())
  // so the RAW file documents the assumption telemetry.cpp's RPM decoding
  // makes about the fitted motor, without EscDriver depending on
  // EscTelemetryReader for it.
  void begin(uint8_t signalPin, uint8_t armButtonPin, uint8_t statusLedPin,
             LocalDisplay* display, uint8_t polePairs);

  // Call from loop() - services the arm button, advances the measurement run
  // and moves the actual pulse towards the target.
  void poll();

  bool isArmed() const { return armed_; }

  // throttlePromille: 0 (idle) to 1000 (full). Sets the target only - the
  // pulse approaches it through the ramp limiter. Returns false when not armed
  // or out of range; the target is then left unchanged.
  bool setThrottle(uint16_t throttlePromille);

  // Call on every ESC telemetry frame - tracks the current RPM (to detect the
  // RPM limit) and the arrival time (to detect telemetry loss).
  void updateRpm(uint32_t rpm);

  // Call on every ESC telemetry frame - tracks the ESC temperature for the
  // over-temperature safety cutoff. Staleness is covered by updateRpm()'s
  // telemetry timeout, so this needs no timestamp of its own.
  void updateTemperature(int8_t tempC);

  // Call on every INA226 sample - tracks the bus voltage for the voltage sag
  // check only. Never used as measurement data; that stays the host's job.
  void updateBusVoltage(int32_t rawBus);

 private:
  enum class MeasurementPhase { kIdle, kArmedFlash, kCountdown, kStepping };

  void writePulseUs(uint16_t us);
  void pollButton();
  // State machine of the measurement run (armed flash -> countdown -> steps).
  // Reads and writes targetPromille_, but never writes the pulse directly -
  // that is pollRamp()'s job alone.
  void pollMeasurementRun();
  // Moves outputPromille_ towards targetPromille_ (ramp limiting) and writes
  // the actual servo pulse - regardless of whether the target came from the
  // measurement run, a SET command or a shutdown.
  void pollRamp();

  void startMeasurementRun();
  // Moves to a new stage: sets targetPromille_, resets the settle/hold timer,
  // announces "# STAGE <promille>". Shared by the upward and downward leg.
  void goToStage(uint16_t promille);
  // Reverses from an upward into a downward leg, one step below fromPromille
  // (the just-completed upward stage - see class comment for why RPM_LIMIT,
  // reaching full throttle and a voltage sag all use exactly this same
  // formula despite triggering at different points). Returns false without
  // side effects when there is no lower stage left (fromPromille was already
  // at/near STAGE_START_PROMILLE) - the caller then ends the run instead.
  bool startDownwardLeg(uint16_t fromPromille);
  // Button abort. Does not touch armed_/LED/message - pollButton() has already
  // handled those before calling.
  void abortRunFromButton();
  // Shared teardown for all four automatic endings below: reason goes into the
  // "# DISARMED (...)" line, the two display lines are shown briefly before
  // pollMeasurementRun() clears them again.
  void endRun(const char* reason, const char* displayLine0,
              const char* displayLine1);
  void endRunComplete();
  void endRunRpmLimit();
  void endRunTelemetryLost();
  void endRunTempLimit();

  uint8_t armButtonPin_ = 0;
  uint8_t statusLedPin_ = 0;
  bool armed_ = false;

  // Carried through into "# PARAMS" only, see begin(). Not used for any
  // computation here.
  uint8_t polePairs_ = 0;

  bool buttonPressed_ = false;
  bool lastRawState_ = false;
  uint32_t lastEdgeMs_ = 0;

  // Ramp limiting: outputPromille_ approaches targetPromille_ at a bounded
  // rate - applies to SET, measurement steps and shutdown alike.
  float outputPromille_ = 0.0f;
  uint16_t targetPromille_ = 0;
  uint32_t lastRampMs_ = 0;

  // True while the current ramp movement is the slower shutdown to 0 % after a
  // run ends; false for the normal rate (ramp up, step changes, SET). Reset at
  // the start of every run.
  bool slowShutdownRamp_ = false;

  MeasurementPhase phase_ = MeasurementPhase::kIdle;
  uint32_t phaseStartMs_ = 0;
  int8_t lastCountdownShown_ = -1;

  // Telemetry state, tracked independently of the run.
  uint32_t currentRpm_ = 0;
  uint32_t lastTelemetryMs_ = 0;
  int8_t lastTempC_ = 0;

  // ESC over-temperature detection - a genuine safety cutoff, see class
  // comment. Same debounce shape as the voltage sag check below (a short
  // stable window rather than a single sample) to not abort on one noisy
  // reading. 0 = not currently over the threshold.
  uint32_t tempLimitStartMs_ = 0;

  // Voltage sag detection. Not a safety function - the supply sitting in its
  // own constant-current mode is not itself a problem. Its only purpose is to
  // find the upper end of the range this supply can hold voltage for, so it
  // is only ever checked on the upward leg (see stoppingAtVoltageSag_ below) -
  // a sag once the downward leg is already running has no effect at all.
  // lastBusVoltageV_ updates on every INA226 sample, voltageBaselineV_ is
  // frozen (unloaded) at run start.
  float lastBusVoltageV_ = 0.0f;
  float voltageBaselineV_ = 0.0f;
  uint32_t voltageSagStartMs_ = 0;  // 0 = no sag in progress

  // False = still in the discarded settle time, true = inside the measurement
  // window.
  bool inHoldPhase_ = false;

  // When the RPM limit is hit, the running step still gets its full hold
  // window - only then does the device reverse into the downward leg (or,
  // if already on the downward leg, disarm - see class comment).
  bool stoppingAtRpmLimit_ = false;

  // Same "finish the running step, then reverse" pattern as stoppingAtRpmLimit_
  // above, but only ever set on the upward leg (see voltageSagStartMs_) -
  // reversing always succeeds in the sense that the run never disarms because
  // of this (see class comment), it only ends via startDownwardLeg() finding
  // no lower stage to reverse into (endRunComplete()).
  bool stoppingAtVoltageSag_ = false;

  // True while stepping up (STAGE_START_PROMILLE towards full throttle),
  // false on the downward leg back towards STAGE_START_PROMILLE. Reset to
  // true at the start of every run - irrelevant in static mode, which never
  // steps at all. See class comment for the full up/down sweep.
  bool sweepGoingUp_ = true;

  // Latched from LocalDisplay::staticModeSelected()/staticModeThrottlePromille()
  // at the start of a run - a toggle or adjustment mid-run only takes effect
  // on the next run, not the current one.
  bool runIsStaticMode_ = false;
  uint16_t runStaticPromille_ = 0;

  // End-of-run message currently on the display, 0 = none. Deliberately
  // independent of phase_ (which is already kIdle by then), so "no run active"
  // stays true while the message is still showing.
  uint32_t statusMessageUntilMs_ = 0;

  LocalDisplay* display_ = nullptr;
};
