#pragma once

#include <stdint.h>

// ESC telemetry via the T signal of the T-Hobby F35A (AM32) on GPIO18.
//
// This is the one place where the firmware decodes rather than forwarding raw
// bytes: RPM is computed on the ESP32 from the frame's electrical RPM and the
// motor pole count. Deliberate exception to the raw-values-only rule.
//
// Protocol: KISS ESC telemetry (10-byte frame, 115200 baud 8N1, receive only),
// verified against the AM32 firmware sources (Src/kiss_telemetry.c,
// Src/functions.c). Frames only arrive when "Serial/Interval Telemetry" is
// enabled in the AM32 configurator - otherwise the line stays silent, which is
// not an error state.
class EscTelemetryReader {
 public:
  // Pole pairs of the fitted motor (12N14P = 14 poles). Adjust when swapping
  // motors - a wrong value yields plausible but wrong RPM. Public (not just
  // an implementation constant in telemetry.cpp) so main.cpp can hand it to
  // EscDriver for the "# PARAMS" line - one source instead of a second
  // hand-copied constant, the exact duplication problem V1 had (see
  // docs/ARCHITECTURE.md, "# MODE and # PARAMS").
  static constexpr uint8_t kMotorPolePairs = 7;

  void begin(uint8_t rxPin);

  // Non-blocking, call from loop(). True exactly when a new CRC-checked frame
  // is available (the accessors below are then current). A failed CRC drops
  // the frame and resynchronises on the next one - a single bit error on the
  // line is expected, not a fault.
  bool poll();

  int8_t temperatureC() const { return temperatureC_; }
  uint16_t voltageCentivolt() const { return voltageCentivolt_; }
  uint16_t currentCentiamp() const { return currentCentiamp_; }
  uint16_t consumptionMah() const { return consumptionMah_; }
  // Electrical RPM, straight from the frame (erpm_h/l * 100).
  uint32_t erpm() const { return erpm_; }
  // Mechanical RPM = erpm() / pole pairs. The pole count is a property of the
  // fitted motor (12N14P, 14 poles) - adjust MOTOR_POLE_PAIRS when swapping
  // motors, otherwise this value stays plausible but wrong.
  uint32_t rpm() const { return rpm_; }

  // Diagnostics: distinguishes "no bytes at all" (wiring or ESC config) from
  // "bytes arrive but CRC keeps failing" (framing or baud rate).
  uint32_t bytesSeen() const { return bytesSeen_; }
  uint32_t framesCrcFail() const { return framesCrcFail_; }

 private:
  uint8_t buf_[10] = {0};
  uint8_t bufLen_ = 0;
  uint32_t lastByteUs_ = 0;

  int8_t temperatureC_ = 0;
  uint16_t voltageCentivolt_ = 0;
  uint16_t currentCentiamp_ = 0;
  uint16_t consumptionMah_ = 0;
  uint32_t erpm_ = 0;
  uint32_t rpm_ = 0;

  uint32_t bytesSeen_ = 0;
  uint32_t framesCrcFail_ = 0;
};
