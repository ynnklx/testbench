// Test stand firmware: load cell (HX711), current/voltage (INA226), ESC
// control with its own measurement run, and ESC telemetry (RPM).
//
// The serial protocol emits raw values only - ADC counts, never grams or amps.
// Calibration is a host concern, so recorded data sets stay convertible when
// the calibration changes. The local display is the one exception and is
// deliberately isolated from the data path (see display.h).
#include <Arduino.h>
#include <WiFi.h>

#include "display.h"
#include "esc.h"
#include "hx711.h"
#include "ina226.h"
#include "telemetry.h"

// Pin assignment is fixed project-wide, see docs/ARCHITECTURE.md.
static const uint8_t HX711_DOUT_PIN = 16;
static const uint8_t HX711_SCK_PIN = 17;
static const uint8_t TARE_BUTTON_PIN = 27;
static const uint8_t ESC_SIGNAL_PIN = 25;
static const uint8_t ESC_ARM_BUTTON_PIN = 26;
static const uint8_t ESC_STATUS_LED_PIN = 4;
static const uint8_t ESC_TELEM_RX_PIN = 18;

static const char* DEVICE_ID = "pruefstand";
// Matches core/csvio.py's TOOL_VERSION - both track the V2 project version,
// bumped by hand together.
static const char* FW_VERSION = "2.0.0";
// Nominal rate, reported by ID? for information only - NEVER used as a time
// base. The HX711 RATE pin is unconnected, so the real rate is closer to
// 9-11 SPS and temperature dependent. Hence every sample carries its own
// timestamp instead.
static const uint32_t NOMINAL_RATE_SPS = 10;

static const uint32_t HX711_TIMEOUT_MS = 500;

static Hx711 hx711;
static LocalDisplay localDisplay;
static EscDriver esc;
static Ina226Driver ina226;
static EscTelemetryReader escTelemetry;
static bool streaming = false;
// One sequence counter per line type (D/I/E), all reset by START - lets the
// host detect dropped lines per channel.
static uint32_t weightSeq = 0;
static uint32_t currentSeq = 0;
static uint32_t telemetrySeq = 0;

// Reads from the serial buffer character by character and returns a complete
// line once '\n' arrives, empty string otherwise. Non-blocking so the data
// stream keeps running meanwhile.
static String readCommandLine() {
  static String buf;
  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c == '\n') {
      String line = buf;
      buf = "";
      line.trim();
      return line;
    }
    buf += c;
  }
  return String();
}

static void handleCommand(const String& line) {
  if (line.length() == 0) {
    return;
  }

  if (line == "ID?") {
    Serial.printf("OK ID %s fw=%s rate=%lu\n", DEVICE_ID, FW_VERSION,
                   (unsigned long)NOMINAL_RATE_SPS);

  } else if (line == "PING") {
    Serial.printf("OK PONG %lu\n", (unsigned long)micros());

  } else if (line == "START") {
    streaming = true;
    weightSeq = 0;
    currentSeq = 0;
    telemetrySeq = 0;
    Serial.println("OK START");

  } else if (line == "STOP") {
    streaming = false;
    Serial.println("OK STOP");

  } else if (line.startsWith("TARE ")) {
    // strtol plus "whole string consumed" instead of String::toInt(), which
    // would silently accept "TARE 5abc" as 5.
    String arg = line.substring(5);
    char* end = nullptr;
    long parsed = strtol(arg.c_str(), &end, 10);
    bool validNumber = (end != arg.c_str()) && (*end == '\0');
    if (!validNumber || parsed <= 0) {
      Serial.printf("ERR %s unknown\n", line.c_str());
      return;
    }
    int n = (int)parsed;

    // Averages n raw values and reports mean plus standard deviation. Does NOT
    // apply the value - that decision belongs to the host.
    double sum = 0.0;
    double sumSq = 0.0;
    for (int i = 0; i < n; i++) {
      if (!hx711.waitReady(HX711_TIMEOUT_MS)) {
        // Not a protocol-defined case, but follows the same ERR convention as
        // unknown commands rather than failing silently.
        Serial.println("ERR TARE timeout");
        return;
      }
      int32_t sample = hx711.readRaw();
      sum += sample;
      sumSq += (double)sample * (double)sample;
    }
    double mean = sum / n;
    double variance = (sumSq / n) - (mean * mean);
    if (variance < 0.0) {
      variance = 0.0;  // Guard against rounding error at very low spread
    }
    double sd = sqrt(variance);
    Serial.printf("OK TARE %.3f %.3f %d\n", mean, sd, n);

  } else if (line.startsWith("SET ")) {
    String arg = line.substring(4);
    char* end = nullptr;
    long promille = strtol(arg.c_str(), &end, 10);
    bool validNumber = (end != arg.c_str()) && (*end == '\0');
    if (!validNumber || promille < 0 || promille > 1000) {
      Serial.printf("ERR %s unknown\n", line.c_str());
      return;
    }
    if (!esc.setThrottle((uint16_t)promille)) {
      // Arming comes exclusively from the physical button - there is no serial
      // ARM command.
      Serial.println("ERR SET not armed");
      return;
    }
    Serial.printf("OK SET %ld\n", promille);

  } else {
    Serial.printf("ERR %s unknown\n", line.c_str());
  }
}

void setup() {
  // Radios off during measurement - disabled explicitly, not just left unused.
  WiFi.mode(WIFI_OFF);
  btStop();

  Serial.begin(921600);
  hx711.begin(HX711_DOUT_PIN, HX711_SCK_PIN);
  localDisplay.begin(TARE_BUTTON_PIN);
  esc.begin(ESC_SIGNAL_PIN, ESC_ARM_BUTTON_PIN, ESC_STATUS_LED_PIN,
            &localDisplay, EscTelemetryReader::kMotorPolePairs);
  ina226.begin();
  escTelemetry.begin(ESC_TELEM_RX_PIN);

  Serial.printf("# %s ready t=%lu\n", DEVICE_ID, (unsigned long)millis());
}

void loop() {
  String line = readCommandLine();
  if (line.length() > 0) {
    handleCommand(line);
  }

  esc.poll();

  if (hx711.isReady()) {
    // Timestamp taken at the DRDY edge, not at send time - sending itself
    // takes an unpredictable, variable amount of time.
    uint32_t t_us = micros();
    int32_t raw = hx711.readRaw();

    if (streaming) {
      Serial.printf("D,%lu,%lu,%ld\n", (unsigned long)weightSeq,
                     (unsigned long)t_us, (long)raw);
      weightSeq++;
    }

    // The local display is fully separate from the raw stream above (own local
    // calibration, see display.h).
    localDisplay.update(raw);
  }

  if (ina226.isReady()) {
    uint32_t t_us = micros();
    int32_t rawShunt = ina226.readShuntRaw();
    int32_t rawBus = ina226.readBusRaw();

    localDisplay.setCurrentRaw(rawShunt);

    // Feeds the voltage sag check of the measurement run - independent of
    // armed/streaming, so the baseline is current during countdown and idle.
    esc.updateBusVoltage(rawBus);

    if (streaming) {
      Serial.printf("I,%lu,%lu,%ld,%ld\n", (unsigned long)currentSeq,
                     (unsigned long)t_us, (long)rawShunt, (long)rawBus);
      currentSeq++;
    }
  }

  // ESC telemetry only arrives when "Serial Telemetry" is enabled in the AM32
  // configurator - otherwise poll() stays false forever, which is not an error.
  if (escTelemetry.poll()) {
    uint32_t t_us = micros();

    localDisplay.setRpm(escTelemetry.rpm());

    // Feeds the RPM limit and telemetry timeout checks - independent of
    // armed/streaming, so both stay current during countdown and idle.
    esc.updateRpm(escTelemetry.rpm());
    // Feeds the ESC over-temperature safety cutoff, same reasoning.
    esc.updateTemperature(escTelemetry.temperatureC());

    if (streaming) {
      Serial.printf("E,%lu,%lu,%lu,%lu,%d,%u,%u,%u\n",
                     (unsigned long)telemetrySeq, (unsigned long)t_us,
                     (unsigned long)escTelemetry.rpm(),
                     (unsigned long)escTelemetry.erpm(),
                     (int)escTelemetry.temperatureC(),
                     (unsigned)escTelemetry.voltageCentivolt(),
                     (unsigned)escTelemetry.currentCentiamp(),
                     (unsigned)escTelemetry.consumptionMah());
      telemetrySeq++;
    }
  }
}
