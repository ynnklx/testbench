#include "ina226.h"

#include <Arduino.h>
#include <Wire.h>

namespace {
// Default address with A0/A1 tied to GND. No conflict with the display
// (0x27/0x3F).
const uint8_t INA226_ADDR = 0x40;

const uint8_t REG_CONFIG = 0x00;
const uint8_t REG_SHUNT_VOLTAGE = 0x01;
const uint8_t REG_BUS_VOLTAGE = 0x02;
const uint8_t REG_MASK_ENABLE = 0x06;

// Averaging = 16, 1.1 ms conversion time each for shunt and bus, continuous
// shunt+bus mode. Set explicitly rather than relying on the power-on default.
// Averaging happens in the chip (real hardware averaging, not just polling
// less often); without it a 2.5 s throttle step produced ~869 current samples,
// far more than a stationary value needs, and made up ~90 % of every CSV.
const uint16_t CONFIG_VALUE = 0x4527;

// One averaged shunt+bus cycle takes 16 * 2.2 ms = ~35 ms. Polling the
// conversion-ready bit faster than that is pure I2C bus load without new data,
// and blocking I2C in loop() is what once overflowed the serial input buffer.
const uint32_t POLL_INTERVAL_US = 10000;

bool writeRegister(uint8_t reg, uint16_t value) {
  Wire.beginTransmission(INA226_ADDR);
  Wire.write(reg);
  Wire.write((uint8_t)(value >> 8));
  Wire.write((uint8_t)(value & 0xFF));
  return Wire.endTransmission() == 0;
}

bool readRegister(uint8_t reg, uint16_t* out) {
  Wire.beginTransmission(INA226_ADDR);
  Wire.write(reg);
  if (Wire.endTransmission(false) != 0) {
    return false;
  }
  if (Wire.requestFrom((int)INA226_ADDR, 2) != 2) {
    return false;
  }
  uint16_t msb = Wire.read();
  uint16_t lsb = Wire.read();
  *out = (uint16_t)((msb << 8) | lsb);
  return true;
}
}  // namespace

void Ina226Driver::begin() {
  Wire.begin(21, 22);

  Wire.beginTransmission(INA226_ADDR);
  if (Wire.endTransmission() != 0) {
    // Report, do not swallow. The load cell stream keeps running regardless.
    Serial.println("# INA226 not found (I2C 0x40 does not respond)");
    found_ = false;
    return;
  }

  found_ = writeRegister(REG_CONFIG, CONFIG_VALUE);
  if (!found_) {
    Serial.println("# INA226 config write failed");
  }
}

bool Ina226Driver::isReady() {
  if (!found_) {
    return false;
  }

  uint32_t now = micros();
  if (now - lastPollUs_ < POLL_INTERVAL_US) {
    return false;
  }
  lastPollUs_ = now;

  uint16_t maskEnable = 0;
  if (!readRegister(REG_MASK_ENABLE, &maskEnable)) {
    return false;
  }
  // Bit 3 = conversion ready flag. Reading this register clears the bit as a
  // side effect (datasheet) - so read it exactly once per call, never once to
  // check and again for the data.
  return (maskEnable & (1 << 3)) != 0;
}

int32_t Ina226Driver::readShuntRaw() const {
  uint16_t raw = 0;
  if (!readRegister(REG_SHUNT_VOLTAGE, &raw)) {
    return 0;
  }
  // Signed 16-bit register (current can flow either way), sign-extended.
  return (int32_t)(int16_t)raw;
}

int32_t Ina226Driver::readBusRaw() const {
  uint16_t raw = 0;
  if (!readRegister(REG_BUS_VOLTAGE, &raw)) {
    return 0;
  }
  return (int32_t)raw;
}
