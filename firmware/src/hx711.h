#pragma once

#include <stdint.h>

// Minimal bit-banged HX711 driver, channel A / gain 128 only.
// Channel B and gain 64 are deliberately unsupported - not needed here.
class Hx711 {
 public:
  void begin(uint8_t doutPin, uint8_t sckPin);

  // True when a conversion is available (DOUT is low).
  bool isReady() const;

  // Blocks until isReady() or timeout. False = timeout, chip not responding.
  bool waitReady(uint32_t timeoutMs) const;

  // Reads one raw value. Expects isReady() to have been true - otherwise the
  // HX711 itself stalls until its next conversion completes.
  int32_t readRaw() const;

 private:
  uint8_t dout_ = 0;
  uint8_t sck_ = 0;
};
