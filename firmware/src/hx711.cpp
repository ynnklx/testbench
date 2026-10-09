#include "hx711.h"

#include <Arduino.h>

void Hx711::begin(uint8_t doutPin, uint8_t sckPin) {
  dout_ = doutPin;
  sck_ = sckPin;
  pinMode(dout_, INPUT);
  pinMode(sck_, OUTPUT);
  digitalWrite(sck_, LOW);
}

bool Hx711::isReady() const {
  // The HX711 pulls DOUT low once a conversion is ready.
  return digitalRead(dout_) == LOW;
}

bool Hx711::waitReady(uint32_t timeoutMs) const {
  uint32_t start = millis();
  while (!isReady()) {
    if (millis() - start >= timeoutMs) {
      return false;
    }
  }
  return true;
}

int32_t Hx711::readRaw() const {
  uint32_t value = 0;

  // The 25 clock pulses must not be interrupted: if SCK stays high for more
  // than 60 us the HX711 enters power-down and returns plausible-looking but
  // wrong values afterwards - no visible error. Hence a critical section
  // around the whole pulse train, not around individual edges.
  portDISABLE_INTERRUPTS();

  // 24 data bits, MSB first.
  for (int i = 0; i < 24; i++) {
    digitalWrite(sck_, HIGH);
    value = (value << 1) | digitalRead(dout_);
    digitalWrite(sck_, LOW);
  }

  // 25th pulse: selects channel A / gain 128 for the *next* conversion.
  digitalWrite(sck_, HIGH);
  digitalWrite(sck_, LOW);

  portENABLE_INTERRUPTS();

  // Sign-extend the 24-bit two's complement value to int32.
  if (value & 0x800000) {
    value |= 0xFF000000;
  }
  return (int32_t)value;
}
