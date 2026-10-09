#pragma once

#include <stdint.h>

// 16x2 I2C display plus tare button. Convenience only: it has its own local
// calibration and does not influence the raw serial protocol in any way.
// The zero point is set by the button and stored in NVS; the slope is a fixed
// constant copied by hand from the host-side calibration (see COUNTS_PER_GRAM
// in display.cpp).
//
// A long press on the same button (LONG_PRESS_MS, display.cpp) toggles
// "static mode" instead of taring - a mode selector only, read by EscDriver
// at the start of the next measurement run (see runIsStaticMode_ in esc.cpp)
// to hold a single fixed throttle until manually disarmed, instead of the
// normal step sequence. Used for ESC drift and INA226 long-run validation.
// Deliberately kept here rather than in EscDriver: the button stays a single
// owner of its pin/debounce state, and EscDriver already reads this class for
// display purposes, so no new dependency is introduced.
//
// Turning the mode on (not off) opens a short adjustment window
// (STATIC_ADJUST_WINDOW_MS): a short press inside it steps the target
// throttle by 10 % instead of taring, wrapping at both ends of 0-100 % - the
// only way to reach every value with a single button. Each press extends the
// window; it ends after STATIC_ADJUST_WINDOW_MS of inactivity, and the
// button reverts to taring. The chosen value stays in RAM (not NVS, like the
// mode selection itself) until changed again or the device reboots.
class LocalDisplay {
 public:
  void begin(uint8_t buttonPin);

  // Call on every new HX711 raw value - refreshes the display and services
  // the tare button (every press tares).
  void update(int32_t raw);

  // Call on every new INA226 raw value - only stores it for the next redraw
  // (shown as current in mA), never triggers I2C itself. Bus voltage is not
  // displayed: the supply voltage is fixed anyway, current is the more
  // informative reading.
  void setCurrentRaw(int32_t rawShunt);

  // Call on every new ESC telemetry frame - stored for the next redraw, same
  // as setCurrentRaw(). Only visible in the live view (line 1, right).
  void setRpm(uint32_t rpm);

  // Replaces the weight/current view with two fixed lines until
  // clearOverride() - used by the measurement run for countdown and status
  // messages. Stores strings only, no I2C of its own.
  void setOverride(const char* line0, const char* line1);

  // Like setOverride(), but horizontally centred - for short status messages
  // on an otherwise empty line.
  void setOverrideCentered(const char* line0, const char* line1);

  // Like setOverride(), but renders live values instead of fixed text:
  // throttle and RPM on line 1, current and thrust on line 2, recomputed from
  // the latest raw values on every redraw. Used during the throttle steps.
  // Throttle carries one decimal - with 2.5 % steps an integer display would
  // make every second step indistinguishable from the previous one.
  void setOverrideLive(uint16_t throttlePromille);

  void clearOverride();

  // True after a long press toggled "static mode 50%" on (see pollButton()) -
  // read by EscDriver at the start of every measurement run to decide which
  // program to run. Resets to false on every boot - deliberately not stored
  // in NVS like the tare zero point, so a stale selection from a previous
  // session can never surprise the next run.
  bool staticModeSelected() const { return staticModeSelected_; }

  // Target throttle for static mode, 0-1000 promille in 100-promille (10 %)
  // steps - see the adjustment window described above. Meaningful only while
  // staticModeSelected() is true; EscDriver reads both together.
  uint16_t staticModeThrottlePromille() const { return staticPromille_; }

 private:
  void pollButton(int32_t raw);
  void doTare(int32_t raw);
  void render(int32_t raw);
  void loadCalibration();
  void saveCalibration();

  uint8_t buttonPin_ = 0;
  bool displayFound_ = false;

  int32_t rawZero_ = 0;
  bool zeroSet_ = false;  // false = never tared, neither this session nor stored

  int32_t currentRawShunt_ = 0;
  uint32_t rpmValue_ = 0;

  bool buttonPressed_ = false;
  uint32_t lastEdgeMs_ = 0;
  uint32_t pressStartMs_ = 0;
  uint32_t lastRenderMs_ = 0;
  bool staticModeSelected_ = false;
  uint16_t staticPromille_ = 500;
  uint32_t adjustWindowUntilMs_ = 0;  // 0 = not in the adjustment window

  const char* statusMsg_ = "ready";
  uint32_t statusMsgUntilMs_ = 0;

  enum class OverrideKind { kNone, kStatic, kLive };
  OverrideKind overrideKind_ = OverrideKind::kNone;
  bool overrideCentered_ = false;  // kStatic only, see setOverrideCentered()
  char overrideLine0_[17] = "";
  char overrideLine1_[17] = "";
  uint16_t overrideThrottlePromille_ = 0;
};
