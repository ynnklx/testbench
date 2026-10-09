#include "esc.h"

#include <Arduino.h>
#include <ESP32Servo.h>
#include <math.h>

namespace {
const uint16_t MIN_PULSE_US = 1000;
const uint16_t MAX_PULSE_US = 2000;

// The raw level must be stable this long before a change counts as a real
// edge. A plain minimum distance between accepted edges proved insufficient:
// an unstable contact produced 17 state changes in ~300 ms without anyone
// touching the button.
const uint32_t DEBOUNCE_MS = 50;

// Normal ramp rate for any throttle change, ~100 percentage points per second -
// prevents abrupt jumps such as a direct SET from 0 to 40 %.
const float RAMP_PROMILLE_PER_MS = 1.0f;

// Slower rate used only for ramping down to 0 % after a run ends. The motor
// repeatedly twitched after a run; the suspected cause is regenerative braking
// energy that the lab supply cannot absorb (it is not a sink). Not verified as
// the root cause, but braking less abruptly is the obvious first countermeasure.
// Explicitly does not apply to ramping up, step changes or SET.
const float SHUTDOWN_RAMP_PROMILLE_PER_MS = 0.5f;

// Run sequence timings.
const uint32_t ARMED_FLASH_MS = 2000;  // "!!! ARMED !!!" before the countdown
const uint32_t COUNTDOWN_MS = 3000;
const uint32_t END_MESSAGE_MS = 2000;  // end-of-run message, see endRun()

// Throttle steps. Below 15 % the motor does not run cleanly on this rig.
const uint16_t STAGE_START_PROMILLE = 150;
const uint16_t STAGE_STEP_PROMILLE = 25;

// Discarded settle time before each measurement window. Raised from 620 to
// 1000 ms (2026-09-07, validation step V6a) after a settle-time analysis found the
// low-throttle steps (15-22.5%) needing up to 924 ms to settle across 5
// independent runs - 620 ms was cutting it close there specifically, not
// across the board. The current path settles much faster (~100-230 ms), but
// one common window for both channels is simpler than two separate ones.
const uint32_t STAGE_SETTLE_MS = 1000;

// Measurement window per step. Comparison runs at 1/2/4 s showed the median
// per step is independent of this duration, but a window shorter than ~2 s
// systematically under-reports the standard deviation - it only catches part
// of a slower oscillation. 2.5 s is the chosen compromise between data quality
// and total run time.
const uint32_t STAGE_HOLD_MS = 2500;

// Safety ceiling for the loaded rig (propeller fitted).
const uint32_t RPM_LIMIT = 25000;

// ESC over-temperature safety cutoff. Chosen by judgement after a real
// static-mode run at 70% reached 71 degC within 90 s and was still rising
// (fitted asymptote ~78 degC) with no software protection at all - not
// derived from a manufacturer rating for the T-Hobby F35A, which was not
// available. Raised 80->85 on 2026-09-17 (docs/DEVLOG.md),
// again by judgement, not a rating - the 2750KV/18V main series run was
// clipping the sweep's downward leg right at 80.
const int8_t ESC_TEMP_MAX_C = 85;
const uint32_t TEMP_LIMIT_DEBOUNCE_MS = 200;  // same shape as the voltage sag check below

// Voltage sag detection - not a safety function (see esc.h). Threshold and
// window were chosen by judgement, not derived from a measurement series.
const float VOLTAGE_SAG_THRESHOLD = 0.90f;  // >10 % below baseline counts as a sag
const uint32_t VOLTAGE_SAG_DEBOUNCE_MS = 200;

// INA226 bus register LSB. Each module converts its own raw values rather than
// sharing a constant across modules.
const float BUS_MV_PER_COUNT = 1.25f;

// Telemetry arrives every ~30 ms, so 500 ms is more than 15 intervals: late or
// individually dropped frames cannot trigger it, a real dropout is caught
// quickly.
const uint32_t TELEMETRY_TIMEOUT_MS = 500;

Servo servo;
}  // namespace

void EscDriver::begin(uint8_t signalPin, uint8_t armButtonPin,
                       uint8_t statusLedPin, LocalDisplay* display,
                       uint8_t polePairs) {
  armButtonPin_ = armButtonPin;
  pinMode(armButtonPin_, INPUT_PULLUP);
  polePairs_ = polePairs;

  statusLedPin_ = statusLedPin;
  pinMode(statusLedPin_, OUTPUT);
  digitalWrite(statusLedPin_, LOW);  // disarmed = off

  display_ = display;

  // The ESC needs a valid idle signal from its own power-on to complete its
  // internal arming sequence - so the pulse starts immediately, regardless of
  // the button.
  servo.setPeriodHertz(50);
  servo.attach(signalPin, MIN_PULSE_US, MAX_PULSE_US);
  servo.writeMicroseconds(MIN_PULSE_US);
  armed_ = false;
  lastRampMs_ = millis();
}

void EscDriver::writePulseUs(uint16_t us) {
  servo.writeMicroseconds(us);
}

void EscDriver::pollButton() {
  bool rawPressed = (digitalRead(armButtonPin_) == LOW);

  if (rawPressed != lastRawState_) {
    // Raw level changed - restart the stability window, do not treat this as a
    // real edge yet (could be bounce or interference).
    lastRawState_ = rawPressed;
    lastEdgeMs_ = millis();
    return;
  }
  if (millis() - lastEdgeMs_ < DEBOUNCE_MS) {
    return;  // Not stable long enough yet.
  }
  if (rawPressed == buttonPressed_) {
    return;  // Confirmed state already matches the known one.
  }
  buttonPressed_ = rawPressed;

  if (!buttonPressed_) {
    return;  // Toggle on the confirmed press, not on release.
  }

  armed_ = !armed_;
  digitalWrite(statusLedPin_, armed_ ? HIGH : LOW);
  Serial.println(armed_ ? "# ARMED (button)" : "# DISARMED (button)");

  if (armed_) {
    startMeasurementRun();
  } else {
    abortRunFromButton();
  }
}

void EscDriver::startMeasurementRun() {
  phase_ = MeasurementPhase::kArmedFlash;
  phaseStartMs_ = millis();
  lastCountdownShown_ = -1;
  // Idle target during flash and countdown - never inherit an old SET value
  // from the previous arming.
  targetPromille_ = 0;
  inHoldPhase_ = false;
  stoppingAtRpmLimit_ = false;
  stoppingAtVoltageSag_ = false;
  sweepGoingUp_ = true;
  // Generous grace period for the telemetry timeout check: if no frame has
  // ever arrived since boot, lastTelemetryMs_ would be 0 and the first check
  // would fire immediately.
  lastTelemetryMs_ = millis();
  // Reference for the voltage sag check: bus voltage while still unloaded.
  voltageBaselineV_ = lastBusVoltageV_;
  voltageSagStartMs_ = 0;
  tempLimitStartMs_ = 0;
  // Drop a still-running end message from the previous run - its timeout would
  // otherwise clear the fresh "!!! ARMED !!!" display.
  statusMessageUntilMs_ = 0;
  // Slow shutdown ramp applies only to the ramp down; back to normal here.
  slowShutdownRamp_ = false;
  // Latched once per run - a toggle/adjustment mid-run must not change what's
  // running. Held indefinitely once reached (see kStepping below), so
  // RPM_LIMIT stays the only ceiling - it is unaffected by this value.
  runIsStaticMode_ = display_ && display_->staticModeSelected();
  runStaticPromille_ = display_ ? display_->staticModeThrottlePromille() : 0;

  // Immediately after "# ARMED (button)" (see pollButton(), which calls this
  // function right after printing that line) - see class comment for why.
  if (runIsStaticMode_) {
    Serial.printf("# MODE static %u\n", runStaticPromille_);
  } else {
    Serial.println("# MODE sweep");
  }
  Serial.printf(
      "# PARAMS start=%u step=%u settle_ms=%lu hold_ms=%lu rpm_limit=%lu "
      "temp_max_c=%d sag_fraction=%.2f pole_pairs=%u\n",
      STAGE_START_PROMILLE, STAGE_STEP_PROMILLE,
      (unsigned long)STAGE_SETTLE_MS, (unsigned long)STAGE_HOLD_MS,
      (unsigned long)RPM_LIMIT, (int)ESC_TEMP_MAX_C, VOLTAGE_SAG_THRESHOLD,
      (unsigned)polePairs_);

  if (display_) {
    display_->setOverrideCentered("!!! ARMED !!!", "");
  }
}

void EscDriver::abortRunFromButton() {
  phase_ = MeasurementPhase::kIdle;
  // No jump to idle - goes through the ramp limiter, at the slower shutdown
  // rate. A disarm is therefore a controlled ramp down, not an instant stop.
  targetPromille_ = 0;
  slowShutdownRamp_ = true;
  if (display_) {
    display_->clearOverride();
  }
}

void EscDriver::endRun(const char* reason, const char* displayLine0,
                        const char* displayLine1) {
  phase_ = MeasurementPhase::kIdle;
  targetPromille_ = 0;
  slowShutdownRamp_ = true;
  armed_ = false;
  digitalWrite(statusLedPin_, LOW);
  Serial.printf("# DISARMED (%s)\n", reason);
  if (display_) {
    display_->setOverride(displayLine0, displayLine1);
  }
  statusMessageUntilMs_ = millis() + END_MESSAGE_MS;
}

void EscDriver::endRunComplete() {
  // The downward leg reached STAGE_START_PROMILLE (or, in the pathological
  // case of startDownwardLeg() finding no lower stage to reverse into, the
  // upward leg ended right where it began). Self-disarms so the next button
  // press starts a fresh run instead of holding the last step indefinitely.
  endRun("measurement complete", "COMPLETE", "all stages done");
}

void EscDriver::endRunRpmLimit() {
  // Only reached on the downward leg (see class comment) - on the upward leg
  // this reverses the sweep instead, see startDownwardLeg(). Only called
  // after the running step has had its full measurement window (see
  // stoppingAtRpmLimit_), so this is a complete ending, not an abort.
  endRun("RPM limit reached", "COMPLETE", "RPM limit hit");
}

void EscDriver::endRunTelemetryLost() {
  // Without RPM feedback the RPM limit can no longer be enforced, so the run
  // is stopped as a precaution instead of continuing blind. This one is a real
  // abort.
  endRun("telemetry lost", "ABORTED", "telemetry lost");
}

void EscDriver::endRunTempLimit() {
  // Genuine safety cutoff (see class comment) - a real abort, not a
  // controlled ending like endRunComplete()/endRunRpmLimit().
  endRun("ESC temp limit reached", "ABORTED", "ESC temp limit");
}

void EscDriver::goToStage(uint16_t promille) {
  targetPromille_ = promille;
  phaseStartMs_ = millis();
  inHoldPhase_ = false;
  Serial.printf("# STAGE %u\n", promille);
}

bool EscDriver::startDownwardLeg(uint16_t fromPromille) {
  if (fromPromille < STAGE_START_PROMILLE + STAGE_STEP_PROMILLE) {
    return false;  // fromPromille was already the lowest stage - nothing below it
  }
  sweepGoingUp_ = false;
  // A fresh downward leg starts clean - stale trigger state from the upward
  // leg must not immediately re-fire here. stoppingAtVoltageSag_ is reset for
  // the same reason, even though the check above already gates on
  // sweepGoingUp_ and so will never set it again on this leg.
  stoppingAtRpmLimit_ = false;
  stoppingAtVoltageSag_ = false;
  voltageSagStartMs_ = 0;
  goToStage((uint16_t)(fromPromille - STAGE_STEP_PROMILLE));
  return true;
}

void EscDriver::updateBusVoltage(int32_t rawBus) {
  lastBusVoltageV_ = (float)rawBus * BUS_MV_PER_COUNT / 1000.0f;
}

void EscDriver::updateTemperature(int8_t tempC) {
  lastTempC_ = tempC;
}

void EscDriver::updateRpm(uint32_t rpm) {
  currentRpm_ = rpm;
  lastTelemetryMs_ = millis();
  // No further evaluation here - the current sequence (fixed settle/hold times
  // per throttle step) needs no control loop. currentRpm_ is read only as a
  // safety check against RPM_LIMIT.
}

bool EscDriver::setThrottle(uint16_t throttlePromille) {
  if (!armed_ || throttlePromille > 1000) {
    return false;
  }
  targetPromille_ = throttlePromille;
  return true;
}

void EscDriver::pollMeasurementRun() {
  uint32_t now = millis();

  // Clear an end-of-run message once its display time is up. Runs independently
  // of phase_ (already kIdle by then), hence before the switch.
  if (statusMessageUntilMs_ != 0 && now >= statusMessageUntilMs_) {
    statusMessageUntilMs_ = 0;
    if (display_) {
      display_->clearOverride();
    }
  }

  switch (phase_) {
    case MeasurementPhase::kArmedFlash: {
      if (now - phaseStartMs_ >= ARMED_FLASH_MS) {
        phase_ = MeasurementPhase::kCountdown;
        phaseStartMs_ = now;
        lastCountdownShown_ = -1;
        if (display_) {
          display_->setOverride("STARTING", "in 3s");
        }
      }
      break;
    }
    case MeasurementPhase::kCountdown: {
      uint32_t elapsed = now - phaseStartMs_;
      int secondsLeft = 3 - (int)(elapsed / 1000);
      if (secondsLeft >= 1 && secondsLeft != lastCountdownShown_) {
        lastCountdownShown_ = secondsLeft;
        char line1[17];
        snprintf(line1, sizeof(line1), "in %ds", secondsLeft);
        if (display_) {
          display_->setOverride("STARTING", line1);
        }
      }
      if (elapsed >= COUNTDOWN_MS) {
        phase_ = MeasurementPhase::kStepping;
        phaseStartMs_ = now;
        inHoldPhase_ = false;
        targetPromille_ = runIsStaticMode_ ? runStaticPromille_ : STAGE_START_PROMILLE;
        Serial.printf("# STAGE %u\n", targetPromille_);
      }
      break;
    }
    case MeasurementPhase::kStepping: {
      if (now - lastTelemetryMs_ > TELEMETRY_TIMEOUT_MS) {
        endRunTelemetryLost();
        break;
      }

      // ESC over-temperature - a genuine safety cutoff (see class comment),
      // so it aborts immediately instead of waiting for the running step's
      // hold window like the RPM ceiling below does.
      if (lastTempC_ >= ESC_TEMP_MAX_C) {
        if (tempLimitStartMs_ == 0) {
          tempLimitStartMs_ = now;
        } else if (now - tempLimitStartMs_ >= TEMP_LIMIT_DEBOUNCE_MS) {
          endRunTempLimit();
          break;
        }
      } else {
        tempLimitStartMs_ = 0;
      }

      // Voltage sag detection - see esc.h. Only relevant on the upward leg
      // (voltageBaselineV_ > 0 guards against triggering before any INA226
      // sample has arrived at all); not checked in static mode, which never
      // sweeps, or once the downward leg has started - see class comment.
      if (!runIsStaticMode_ && sweepGoingUp_ && voltageBaselineV_ > 0.0f &&
          lastBusVoltageV_ < voltageBaselineV_ * VOLTAGE_SAG_THRESHOLD) {
        if (voltageSagStartMs_ == 0) {
          voltageSagStartMs_ = now;
        } else if (now - voltageSagStartMs_ >= VOLTAGE_SAG_DEBOUNCE_MS) {
          stoppingAtVoltageSag_ = true;
        }
      } else {
        voltageSagStartMs_ = 0;
      }

      // Same "finish the running step first" pattern as the RPM ceiling
      // below - only then does the device react, always with a reversal
      // (see the elapsed check further down and esc.h).
      if (stoppingAtVoltageSag_ && !inHoldPhase_) {
        inHoldPhase_ = true;
        phaseStartMs_ = now - STAGE_SETTLE_MS;
        Serial.printf("# HOLD %u\n", targetPromille_);
      }

      // RPM ceiling. The running step still gets its full measurement window:
      // if it was still settling, it jumps straight into a fresh hold window;
      // if it was already holding, that window runs out unchanged. Only then
      // does the device react - reverse (upward leg) or disarm (downward leg).
      if (currentRpm_ >= RPM_LIMIT && !stoppingAtRpmLimit_) {
        stoppingAtRpmLimit_ = true;
        if (!inHoldPhase_) {
          inHoldPhase_ = true;
          phaseStartMs_ = now - STAGE_SETTLE_MS;
          Serial.printf("# HOLD %u\n", targetPromille_);
        }
      }

      uint32_t elapsed = now - phaseStartMs_;
      if (!inHoldPhase_) {
        if (elapsed >= STAGE_SETTLE_MS) {
          inHoldPhase_ = true;
          Serial.printf("# HOLD %u\n", targetPromille_);
        }
      } else if (elapsed >= STAGE_SETTLE_MS + STAGE_HOLD_MS) {
        if (stoppingAtRpmLimit_) {
          // The running stage already had its full window (see above), so
          // reversing here starts one step below the just-completed stage,
          // exactly as startDownwardLeg() already does.
          if (!runIsStaticMode_ && sweepGoingUp_ && startDownwardLeg(targetPromille_)) {
            break;
          }
          endRunRpmLimit();
          break;
        }
        if (stoppingAtVoltageSag_) {
          // Always a reversal - only ever set while sweepGoingUp_ (see
          // above) - starting one step below the just-completed stage. Ends
          // the run only in the pathological case of no lower stage to
          // reverse into (mirrors the full-throttle-reached case below).
          if (!startDownwardLeg(targetPromille_)) {
            endRunComplete();
          }
          break;
        }
        if (runIsStaticMode_) {
          // No further stages, no auto-end - hold here until the arm button
          // disarms it (abortRunFromButton()) or a safety cutoff fires above.
          break;
        }
        if (sweepGoingUp_) {
          uint32_t nextPromille = (uint32_t)targetPromille_ + STAGE_STEP_PROMILLE;
          if (nextPromille > 1000) {
            // Reached full throttle - reverse instead of ending. The peak
            // stage was just measured once (upward only); see class comment
            // for why remeasuring it downward immediately would add nothing.
            if (!startDownwardLeg(targetPromille_)) {
              endRunComplete();
            }
            break;
          }
          goToStage((uint16_t)nextPromille);
        } else {
          uint32_t nextPromille = (uint32_t)targetPromille_ - STAGE_STEP_PROMILLE;
          if (nextPromille < STAGE_START_PROMILLE) {
            endRunComplete();
            break;
          }
          goToStage((uint16_t)nextPromille);
        }
      }

      if (display_) {
        display_->setOverrideLive(targetPromille_);
      }
      break;
    }
    case MeasurementPhase::kIdle:
      break;
  }
}

void EscDriver::pollRamp() {
  uint32_t now = millis();
  uint32_t dtMs = now - lastRampMs_;
  lastRampMs_ = now;

  float rate = slowShutdownRamp_ ? SHUTDOWN_RAMP_PROMILLE_PER_MS
                                 : RAMP_PROMILLE_PER_MS;
  float maxStep = rate * (float)dtMs;
  float diff = (float)targetPromille_ - outputPromille_;
  if (diff > maxStep) {
    outputPromille_ += maxStep;
  } else if (diff < -maxStep) {
    outputPromille_ -= maxStep;
  } else {
    outputPromille_ = (float)targetPromille_;
  }

  uint32_t span = MAX_PULSE_US - MIN_PULSE_US;
  uint16_t us = MIN_PULSE_US + (uint16_t)((float)span * outputPromille_ / 1000.0f);
  writePulseUs(us);
}

void EscDriver::poll() {
  pollButton();
  pollMeasurementRun();
  pollRamp();
}
