#include "display.h"

#include <Arduino.h>
#include <LiquidCrystal_I2C.h>
#include <Preferences.h>
#include <Wire.h>
#include <string.h>

namespace {
// Calibration slope, copied by hand from the host-side calibration
// (config/calibration.json, fit.counts_per_gram). Duplicated on the device on
// purpose so the convenience display does not drift from the later host
// evaluation - re-sync it manually after every recalibration.
// The zero point deliberately stays local and button-set instead: unlike the
// slope, it shifts whenever the rig is re-assembled, so a fixed constant would
// be wrong there.
const float COUNTS_PER_GRAM = -993.9631926845303f;

const uint32_t DEBOUNCE_MS = 30;
const uint32_t STATUS_MSG_MS = 1500;

// Held at least this long counts as a long press (static mode toggle) rather
// than a tare. Same threshold and mechanism this button used before the
// 2026-09-03 rework (back then short=tare/long=calibrate against a reference
// weight; now short=tare/long=mode toggle).
const uint32_t LONG_PRESS_MS = 800;

// Static mode target throttle step size (10 %) and how long a short press
// inside the adjustment window still counts as adjusting rather than taring
// - extended on every press within it, so holding the window open only
// requires pressing again before it lapses.
const uint16_t STATIC_PROMILLE_STEP = 100;
const uint32_t STATIC_ADJUST_WINDOW_MS = 3000;

// The I2C redraw is blocking and far more expensive than one HX711 sample
// (~10/s). Redrawing on every sample stalls loop() long enough to overflow the
// serial input buffer under a fast command sequence - observed in phase 1c.
// A 16x2 display needs nothing close to that rate. The button is still polled
// on every sample (cheap, no I2C).
const uint32_t RENDER_INTERVAL_MS = 300;

// INA226 shunt LSB (2.5 uV/count) across the fitted 2 mOhm shunt = 1.25
// mA/count. Display conversion only - the shunt value is a known constant, so
// no calibration is involved, and the raw serial stream is unaffected.
const float CURRENT_MA_PER_COUNT = 1.25f;

LiquidCrystal_I2C* lcd = nullptr;
Preferences prefs;

uint8_t probeI2cAddress() {
  const uint8_t candidates[] = {0x27, 0x3F};
  for (uint8_t addr : candidates) {
    Wire.beginTransmission(addr);
    if (Wire.endTransmission() == 0) {
      return addr;
    }
  }
  return 0;
}

void printPadded(const char* text) {
  char line[17];
  snprintf(line, sizeof(line), "%-16s", text);
  lcd->print(line);
}

// Like printPadded(), but centred across the 16 columns - left-aligned short
// status messages look unsettled on an otherwise empty line.
void printCentered(const char* text) {
  char line[17];
  memset(line, ' ', 16);
  line[16] = '\0';
  size_t len = strlen(text);
  if (len > 16) {
    len = 16;
  }
  size_t start = (16 - len) / 2;
  memcpy(line + start, text, len);
  lcd->print(line);
}
}  // namespace

void LocalDisplay::begin(uint8_t buttonPin) {
  buttonPin_ = buttonPin;
  pinMode(buttonPin_, INPUT_PULLUP);

  Wire.begin(21, 22);
  uint8_t addr = probeI2cAddress();
  if (addr == 0) {
    // Report, do not swallow. Measurement over serial keeps running regardless.
    Serial.println("# display not found (I2C 0x27/0x3F does not respond)");
    displayFound_ = false;
  } else {
    lcd = new LiquidCrystal_I2C(addr, 16, 2);
    lcd->init();
    lcd->backlight();
    displayFound_ = true;
  }

  loadCalibration();
}

void LocalDisplay::loadCalibration() {
  prefs.begin("waage", /*readOnly=*/true);
  rawZero_ = prefs.getInt("zero", 0);
  zeroSet_ = prefs.getBool("zeroSet", false);
  prefs.end();
}

void LocalDisplay::saveCalibration() {
  prefs.begin("waage", false);
  prefs.putInt("zero", rawZero_);
  prefs.putBool("zeroSet", zeroSet_);
  prefs.end();
}

void LocalDisplay::doTare(int32_t raw) {
  rawZero_ = raw;
  zeroSet_ = true;
  saveCalibration();
  statusMsg_ = "tared";
  statusMsgUntilMs_ = millis() + STATUS_MSG_MS;
}

void LocalDisplay::pollButton(int32_t raw) {
  bool pressedNow = (digitalRead(buttonPin_) == LOW);
  if (pressedNow == buttonPressed_) {
    return;
  }
  if (millis() - lastEdgeMs_ < DEBOUNCE_MS) {
    return;  // Bouncing
  }
  lastEdgeMs_ = millis();
  buttonPressed_ = pressedNow;

  if (pressedNow) {
    pressStartMs_ = lastEdgeMs_;
    return;
  }

  uint32_t heldMs = lastEdgeMs_ - pressStartMs_;
  if (heldMs >= LONG_PRESS_MS) {
    staticModeSelected_ = !staticModeSelected_;
    Serial.println(staticModeSelected_ ? "# MODE static (tara button)"
                                        : "# MODE steps (tara button)");
    if (staticModeSelected_) {
      // Opens the adjustment window instead of a fixed status message - the
      // live percentage display below serves as the "mode is on" feedback.
      adjustWindowUntilMs_ = millis() + STATIC_ADJUST_WINDOW_MS;
    } else {
      adjustWindowUntilMs_ = 0;
      statusMsg_ = "STATIC MODE OFF";
      statusMsgUntilMs_ = millis() + STATUS_MSG_MS;
    }
  } else if (millis() < adjustWindowUntilMs_) {
    // Short press inside the adjustment window: step instead of taring.
    // Wraps at both ends - the only way to reach every value with one
    // button, since only this one direction is available.
    staticPromille_ = (uint16_t)((staticPromille_ + STATIC_PROMILLE_STEP) % 1100);
    adjustWindowUntilMs_ = millis() + STATIC_ADJUST_WINDOW_MS;
  } else {
    doTare(raw);
  }
}

void LocalDisplay::render(int32_t raw) {
  if (!displayFound_) {
    return;
  }

  lcd->setCursor(0, 0);
  if (overrideKind_ == OverrideKind::kStatic) {
    if (overrideCentered_) {
      printCentered(overrideLine0_);
    } else {
      printPadded(overrideLine0_);
    }
  } else if (overrideKind_ == OverrideKind::kLive) {
    // Throttle left, RPM right - same 8+8 column split as line 2. rpmValue_
    // stays 0 while no ESC telemetry arrives, which is not an error state.
    char throttleBuf[9];
    snprintf(throttleBuf, sizeof(throttleBuf), "%.1f%%", overrideThrottlePromille_ / 10.0f);
    char rpmBuf[9];
    snprintf(rpmBuf, sizeof(rpmBuf), "%lurpm", (unsigned long)rpmValue_);
    char line0[17];
    snprintf(line0, sizeof(line0), "%-8s%8s", throttleBuf, rpmBuf);
    printPadded(line0);
  } else if (millis() < adjustWindowUntilMs_) {
    printCentered("STATIC MODE");
  } else if (!zeroSet_) {
    printPadded("not tared");
  } else {
    float grams = (float)(raw - rawZero_) / COUNTS_PER_GRAM;
    char line[17];
    snprintf(line, sizeof(line), "%8.1f g", grams);
    printPadded(line);
  }

  lcd->setCursor(0, 1);
  if (overrideKind_ == OverrideKind::kStatic) {
    if (overrideCentered_) {
      printCentered(overrideLine1_);
    } else {
      printPadded(overrideLine1_);
    }
  } else if (overrideKind_ == OverrideKind::kLive) {
    // Current left, thrust right, 8 columns each. Thrust uses the same local
    // convenience calibration as the normal weight view - never tared shows
    // "n/a" rather than a value relative to a meaningless zero.
    char currentBuf[9];
    float milliamps = (float)currentRawShunt_ * CURRENT_MA_PER_COUNT;
    snprintf(currentBuf, sizeof(currentBuf), "%.0fmA", milliamps);

    char gramBuf[9];
    if (!zeroSet_) {
      snprintf(gramBuf, sizeof(gramBuf), "n/a");
    } else {
      float grams = (float)(raw - rawZero_) / COUNTS_PER_GRAM;
      snprintf(gramBuf, sizeof(gramBuf), "%.1fg", grams);
    }

    char line1[17];
    snprintf(line1, sizeof(line1), "%-8s%8s", currentBuf, gramBuf);
    printPadded(line1);
  } else if (millis() < adjustWindowUntilMs_) {
    char buf[17];
    snprintf(buf, sizeof(buf), "%u%%", staticPromille_ / 10);
    printCentered(buf);
  } else if (millis() < statusMsgUntilMs_) {
    printPadded(statusMsg_);
  } else {
    float milliamps = (float)currentRawShunt_ * CURRENT_MA_PER_COUNT;
    char line[17];
    if (staticModeSelected_) {
      // Permanent reminder while idle - the next arm press holds the chosen
      // static throttle instead of running the usual step sequence, easy to
      // forget otherwise.
      char currentBuf[9];
      snprintf(currentBuf, sizeof(currentBuf), "%.0fmA", milliamps);
      char tagBuf[9];
      snprintf(tagBuf, sizeof(tagBuf), "ST %u%%", staticPromille_ / 10);
      snprintf(line, sizeof(line), "%-8s%8s", currentBuf, tagBuf);
    } else {
      snprintf(line, sizeof(line), "%7.0f mA", milliamps);
    }
    printPadded(line);
  }
}

void LocalDisplay::setCurrentRaw(int32_t rawShunt) {
  currentRawShunt_ = rawShunt;
}

void LocalDisplay::setRpm(uint32_t rpm) {
  rpmValue_ = rpm;
}

void LocalDisplay::setOverride(const char* line0, const char* line1) {
  overrideKind_ = OverrideKind::kStatic;
  overrideCentered_ = false;
  snprintf(overrideLine0_, sizeof(overrideLine0_), "%s", line0);
  snprintf(overrideLine1_, sizeof(overrideLine1_), "%s", line1);
}

void LocalDisplay::setOverrideCentered(const char* line0, const char* line1) {
  overrideKind_ = OverrideKind::kStatic;
  overrideCentered_ = true;
  snprintf(overrideLine0_, sizeof(overrideLine0_), "%s", line0);
  snprintf(overrideLine1_, sizeof(overrideLine1_), "%s", line1);
}

void LocalDisplay::setOverrideLive(uint16_t throttlePromille) {
  overrideKind_ = OverrideKind::kLive;
  overrideThrottlePromille_ = throttlePromille;
}

void LocalDisplay::clearOverride() {
  overrideKind_ = OverrideKind::kNone;
}

void LocalDisplay::update(int32_t raw) {
  pollButton(raw);

  uint32_t now = millis();
  if (now - lastRenderMs_ < RENDER_INTERVAL_MS) {
    return;
  }
  lastRenderMs_ = now;
  render(raw);
}
