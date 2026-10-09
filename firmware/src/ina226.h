#pragma once

#include <stdint.h>

// Minimal INA226 driver: reads the raw shunt and bus voltage registers.
// No calibration register, no on-chip conversion to current/power - scaling
// stays a host concern, same as for the load cell (raw values only).
class Ina226Driver {
 public:
  void begin();

  // Non-blocking, rate limited internally (see .cpp) - call from loop(),
  // same usage as Hx711::isReady().
  bool isReady();

  int32_t readShuntRaw() const;
  int32_t readBusRaw() const;

 private:
  bool found_ = false;
  uint32_t lastPollUs_ = 0;
};
