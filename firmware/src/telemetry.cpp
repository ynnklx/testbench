#include "telemetry.h"

#include <Arduino.h>

namespace {
// Second hardware UART (the first one carries the host protocol on Serial),
// remapped to GPIO18 in begin().
HardwareSerial telemSerial(2);

// AM32 sends a frame every 30 ms at the configured interval; one frame takes
// about 870 us at 115200 baud 8N1. A gap of more than 2 ms between bytes can
// therefore only be a frame boundary, never a pause inside a frame - used to
// resynchronise if the buffer ever gets out of step.
const uint32_t FRAME_GAP_US = 2000;

// Identical to update_crc8()/get_crc8() in AM32 Src/functions.c (poly 0x07,
// init 0) - taken from the firmware sources, not guessed.
uint8_t crc8Update(uint8_t crc, uint8_t byte) {
  uint8_t crcU = byte ^ crc;
  for (uint8_t i = 0; i < 8; i++) {
    crcU = (crcU & 0x80) ? (uint8_t)(0x7 ^ (crcU << 1)) : (uint8_t)(crcU << 1);
  }
  return crcU;
}

uint8_t crc8(const uint8_t* buf, uint8_t len) {
  uint8_t crc = 0;
  for (uint8_t i = 0; i < len; i++) {
    crc = crc8Update(crc, buf[i]);
  }
  return crc;
}
}  // namespace

void EscTelemetryReader::begin(uint8_t rxPin) {
  telemSerial.begin(115200, SERIAL_8N1, rxPin, -1);
}

bool EscTelemetryReader::poll() {
  bool gotFrame = false;

  while (telemSerial.available()) {
    uint32_t nowUs = micros();
    if (bufLen_ > 0 && (nowUs - lastByteUs_) > FRAME_GAP_US) {
      // Gap too large for "still the same frame" - the remainder was
      // incomplete, a new frame starts here.
      bufLen_ = 0;
    }
    buf_[bufLen_++] = (uint8_t)telemSerial.read();
    lastByteUs_ = nowUs;
    bytesSeen_++;

    if (bufLen_ == sizeof(buf_)) {
      bufLen_ = 0;
      if (crc8(buf_, 9) != buf_[9]) {
        framesCrcFail_++;
        continue;  // Frame dropped, the next gap resynchronises.
      }

      temperatureC_ = (int8_t)buf_[0];
      voltageCentivolt_ = ((uint16_t)buf_[1] << 8) | buf_[2];
      currentCentiamp_ = ((uint16_t)buf_[3] << 8) | buf_[4];
      consumptionMah_ = ((uint16_t)buf_[5] << 8) | buf_[6];
      uint16_t erpmRaw = ((uint16_t)buf_[7] << 8) | buf_[8];
      erpm_ = (uint32_t)erpmRaw * 100;
      rpm_ = erpm_ / kMotorPolePairs;
      gotFrame = true;
    }
  }

  return gotFrame;
}
