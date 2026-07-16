/*
 * e1004_trmnl - Seeed reTerminal E1004 firmware client
 * ----------------------------------------------------
 * Device-side firmware for the TRMNL-like e-ink system. It mirrors the M5
 * PaperColor client's wake cycle, adapted to the E1004's ESP32-S3 + GxEPD2
 * panel and its raw-framebuffer transport:
 *
 *   wake -> connect WiFi -> read battery -> POST status -> handle action
 *        -> (download packed frame + draw if needed) -> deep sleep
 *
 * The backend is the single scheduling authority: the device just obeys the
 * "next_wake_seconds" it gets back from /api/device/{id}/status. Night mode and
 * intervals are computed server-side; the device only applies a few local
 * fallbacks when the backend is unreachable.
 *
 * Unlike the M5 client, the server sends a RAW pre-dithered framebuffer (NOT a
 * PNG): exactly app.render.e1004.render_e1004_frame() output --
 *   1200 x 1600, 4bpp, 600 bytes/row, 960000 bytes total,
 *   high nibble = even x, low nibble = odd x,
 *   nibble = GxEPD2 color7 index (0=Black 1=White 2=Green 3=Blue 4=Red 5=Yellow).
 * The device does no dithering/decoding: it copies the bytes into the panel
 * framebuffer via writeNative() and triggers one full refresh (~20-40 s).
 *
 * Hardware (https://wiki.seeedstudio.com/getting_started_with_reterminal_e1004/):
 *   - ESP32-S3, 8MB PSRAM  (PSRAM REQUIRED for the 937KB framebuffer)
 *   - 13.3" E Ink Spectra 6, 1200x1600, T133A01 dual-chip controller
 *   - Battery: ADC on GPIO1, enable rail on GPIO21, onboard /2 divider, 5000mAh
 *   - Green status LED on GPIO48 (active-low); buzzer on GPIO45
 *   - Front buttons KEY0=GPIO4 (wake) / KEY1=GPIO3 / KEY2=GPIO5, active-low
 *   - PCF8563 RTC (I2C 0x51, CR1220 backup)
 *
 * SETUP (one-time):
 *   1. Arduino IDE board: "XIAO_ESP32S3" (or a generic ESP32-S3 dev board);
 *      Tools > PSRAM: "OPI PSRAM".  Enable "USB CDC On Boot" for the serial log.
 *   2. Install libraries: Adafruit GFX, and the Seeed fork of GxEPD2
 *      (https://github.com/Seeed-Projects/Seeed_GxEPD2).
 *   3. Copy the panel driver pair from that repo's example into THIS folder:
 *        examples/GxEPD2_reTerminal_E1004/GxEPD2_T133A01_1200x1600.h
 *        examples/GxEPD2_reTerminal_E1004/GxEPD2_T133A01_1200x1600.cpp
 *      (GxEPD2 ships them inside the example, not under src/.)
 *   4. Copy secrets.h.example -> secrets.h and fill in WiFi / backend / device.
 *
 * Standard ESP32-S3 deep sleep keeps the RTC domain powered (~14 uA), so
 * RTC_DATA_ATTR survives between wakes -- no NVS dance needed (unlike the M5
 * client, which does a full M5PM1 power-off and must persist to NVS).
 */

#include <Arduino.h>
#include <SPI.h>
#include <WiFi.h>
#include <WiFiClient.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <esp_sleep.h>
#include <esp_wifi.h>
#include <esp_heap_caps.h>
#include <driver/rtc_io.h>

#include <GxEPD2_7C.h>
#include "GxEPD2_T133A01_1200x1600.h"

// Custom types live in a header so the Arduino auto-generated prototypes (which
// are inserted above the sketch body) can see them. See e1004_types.h.
#include "e1004_types.h"

// WiFi credentials, backend URL/device identity, and static-IP settings live in
// secrets.h (copy secrets.h.example -> secrets.h). It is gitignored so the
// sketch itself can be shared without leaking credentials.
#include "secrets.h"

// ===========================================================================
// Debug logging
// ===========================================================================
// Set DEBUG_SERIAL to 0 to silence all serial output (lowest power / final
// deployment). When enabled, each line is prefixed with a millisecond timestamp
// so you can see how long each phase of the wake cycle takes.
#define DEBUG_SERIAL 1
#define SERIAL_BAUD  115200

#if DEBUG_SERIAL
  #define LOGF(fmt, ...) Serial.printf("[%8lu ms] " fmt, (unsigned long)millis(), ##__VA_ARGS__)
  #define LOGLN(msg)     Serial.printf("[%8lu ms] %s\n", (unsigned long)millis(), msg)
#else
  #define LOGF(fmt, ...) do {} while (0)
  #define LOGLN(msg)     do {} while (0)
#endif

// ===========================================================================
// Firmware + tunables
// ===========================================================================
#define FIRMWARE_VERSION   "e1004-0.2.0"

static const uint32_t WIFI_CONNECT_TIMEOUT_MS = 20000;
static const uint32_t HTTP_TIMEOUT_MS         = 20000;
static const uint32_t SERIAL_READY_TIMEOUT_MS = 1500;  // wait for USB-CDC host

// Local fallbacks (only used when the server cannot be reached).
static const uint64_t FALLBACK_UNREACHABLE_SECONDS = 30ULL * 60ULL;   // 30 min
static const uint64_t FALLBACK_LOW_BATTERY_SECONDS = 6ULL * 3600ULL;  // 6 h
static const float    CRITICAL_BATTERY_PERCENT     = 5.0f;

// Cap a single sleep at 4 h. next_wake for a long night can exceed the timer we
// want to commit to; the device wakes once mid-night, re-checks in (~2-4 s), and
// the server re-issues a sleep. Negligible cost at ~14 uA RTC standby.
static const uint64_t MAX_SLEEP_SECONDS = 14400;  // 4 h

// ===========================================================================
// Onboard peripherals (reTerminal E1004 pin map, from the Seeed wiki cookbook)
// ===========================================================================
#define BATTERY_MONITOR_ENABLED 1
#define BATTERY_ADC_PIN     1    // GPIO1  - battery voltage ADC
#define BATTERY_ENABLE_PIN  21   // GPIO21 - drive HIGH to enable the divider
#define BATTERY_DIVIDER     2.0f // onboard /2 resistor divider

#define STATUS_LED_PIN      48   // GPIO48 - green user LED (active-low)
#define BUZZER_ENABLED      1
#define BUZZER_PIN          45   // GPIO45 - buzzer
#define WAKE_BUTTON_PIN     4    // GPIO4  - KEY0, front right button (active-low)

// ===========================================================================
// Display: 13.3" 6-color 1200x1600, dual-chip T133A01 (Seeed GxEPD2 example pins)
// ===========================================================================
#define EPD_SCK_PIN     7
#define EPD_MISO_PIN    8
#define EPD_MOSI_PIN    9
#define EPD_CS_PIN      10
#define EPD_DC_PIN      11
#define EPD_CS1_PIN     2
#define EPD_RES_PIN     38
#define EPD_BUSY_PIN    13
#define EPD_ENABLE_PIN  12

static constexpr int32_t  DISPLAY_W   = 1200;
static constexpr int32_t  DISPLAY_H   = 1600;
static constexpr uint32_t FRAME_BYTES = (uint32_t)DISPLAY_W * DISPLAY_H / 2; // 960000
static constexpr uint8_t  WHITE_BYTE  = 0x11;  // both nibbles = white (index 1)

SPIClass hspi(HSPI);

// We only ever push our own native frame, so the GxEPD2_7C paged buffer can be
// tiny; we call display.epd2.writeNative()/refresh() directly.
#define MAX_DISPLAY_BUFFER_SIZE 24000u
#define MAX_HEIGHT(EPD) \
  (EPD::HEIGHT <= (MAX_DISPLAY_BUFFER_SIZE) / (EPD::WIDTH / 2) \
       ? EPD::HEIGHT : (MAX_DISPLAY_BUFFER_SIZE) / (EPD::WIDTH / 2))

GxEPD2_7C<GxEPD2_T133A01_1200x1600, MAX_HEIGHT(GxEPD2_T133A01_1200x1600)>
    display(GxEPD2_T133A01_1200x1600(EPD_CS_PIN, EPD_DC_PIN, EPD_RES_PIN,
                                     EPD_BUSY_PIN, EPD_CS1_PIN, EPD_ENABLE_PIN));

// ===========================================================================
// Persistent across deep sleep (RTC domain stays powered on a plain ESP32-S3
// timer/ext1 wake, so RTC_DATA_ATTR survives -- no NVS needed).
// ===========================================================================
RTC_DATA_ATTR static uint32_t g_boot_count           = 0;
RTC_DATA_ATTR static char     g_last_image_id[40]    = {0};
RTC_DATA_ATTR static char     g_pending_error[48]    = {0};
RTC_DATA_ATTR static Metrics  g_metrics;

// Scratch timings for the *current* cycle, flushed into g_metrics before sleep.
static uint32_t g_wifi_ms     = 0;
static uint32_t g_post_ms     = 0;
static uint32_t g_download_ms = 0;
static uint32_t g_draw_ms     = 0;

// ===========================================================================
// Status LED (single green, active-low) + buzzer
// ===========================================================================
static void ledInit() {
  pinMode(STATUS_LED_PIN, OUTPUT);
  digitalWrite(STATUS_LED_PIN, HIGH);  // off
}
static inline void ledOn()  { digitalWrite(STATUS_LED_PIN, LOW); }
static inline void ledOff() { digitalWrite(STATUS_LED_PIN, HIGH); }

static void beep(uint16_t freq, uint16_t ms) {
#if BUZZER_ENABLED
  tone(BUZZER_PIN, freq, ms);
  delay(ms);
  noTone(BUZZER_PIN);
#endif
}
static void beepError() { beep(400, 120); delay(60); beep(400, 120); }

// ===========================================================================
// Battery (GPIO21 enable, GPIO1 ADC, /2 divider). Percent via a LiPo discharge
// curve (Seeed's calibrate_linear points). Returns pct<0 / mv=0 when disabled.
// ===========================================================================
static float batteryPercentFromVolts(float v) {
  // volts -> percent lookup, descending. Linear interpolation between points.
  static const float lut[][2] = {
      {4.15f, 100.0f}, {3.96f, 90.0f}, {3.91f, 80.0f}, {3.85f, 70.0f},
      {3.80f, 60.0f},  {3.75f, 50.0f}, {3.68f, 40.0f}, {3.58f, 30.0f},
      {3.49f, 20.0f},  {3.41f, 10.0f}, {3.30f, 5.0f},  {3.27f, 0.0f}};
  const int n = sizeof(lut) / sizeof(lut[0]);
  if (v >= lut[0][0]) return 100.0f;
  if (v <= lut[n - 1][0]) return 0.0f;
  for (int i = 0; i < n - 1; ++i) {
    float vh = lut[i][0], ph = lut[i][1];
    float vl = lut[i + 1][0], pl = lut[i + 1][1];
    if (v <= vh && v >= vl) {
      float t = (v - vl) / (vh - vl);
      return pl + t * (ph - pl);
    }
  }
  return -1.0f;
}

static void readBattery(float *outPercent, int *outMv) {
  *outPercent = -1.0f;
  *outMv = 0;
#if BATTERY_MONITOR_ENABLED
  pinMode(BATTERY_ENABLE_PIN, OUTPUT);
  digitalWrite(BATTERY_ENABLE_PIN, HIGH);  // enable the divider
  analogReadResolution(12);
  analogSetPinAttenuation(BATTERY_ADC_PIN, ADC_11db);  // full ~0..3.1V range
  delay(10);  // wiki: settle >=10 ms before analogRead for a precise value

  const int samples = 8;
  uint32_t acc = 0;
  for (int i = 0; i < samples; ++i) {
    acc += analogReadMilliVolts(BATTERY_ADC_PIN);
    delay(2);
  }
  digitalWrite(BATTERY_ENABLE_PIN, LOW);  // cut the divider (saves standby draw)

  int pinMv = (int)(acc / samples);
  int battMv = (int)(pinMv * BATTERY_DIVIDER);
  *outMv = battMv;
  *outPercent = batteryPercentFromVolts(battMv / 1000.0f);
  LOGF("[battery] pin=%d mV -> batt=%d mV (%.0f%%)\n", pinMv, battMv, *outPercent);
#else
  LOGLN("[battery] monitoring disabled");
#endif
}

// ===========================================================================
// WiFi
// ===========================================================================
static bool connectWiFi() {
  LOGF("[wifi] connecting to SSID '%s' ...\n", WIFI_SSID);
  WiFi.persistent(false);
  WiFi.mode(WIFI_STA);
#if STATIC_IP_ENABLED
  if (WiFi.config(STATIC_IP, STATIC_GATEWAY, STATIC_SUBNET,
                  STATIC_DNS1, STATIC_DNS2)) {
    LOGF("[wifi] static IP %s\n", STATIC_IP.toString().c_str());
  } else {
    LOGLN("[wifi] static IP config FAILED; using DHCP");
  }
#else
  LOGLN("[wifi] using DHCP");
#endif
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  uint32_t start = millis();
  while (WiFi.status() != WL_CONNECTED) {
    if (millis() - start > WIFI_CONNECT_TIMEOUT_MS) {
      LOGF("[wifi] connect TIMEOUT after %lu ms (status=%d)\n",
           (unsigned long)(millis() - start), (int)WiFi.status());
      return false;
    }
    delay(100);
  }
  g_wifi_ms = millis() - start;
  LOGF("[wifi] connected in %lu ms, IP=%s, RSSI=%d dBm\n",
       (unsigned long)g_wifi_ms, WiFi.localIP().toString().c_str(), WiFi.RSSI());
  return true;
}

static void shutdownWiFi() {
  WiFi.disconnect(true, false);
  WiFi.mode(WIFI_OFF);
  esp_wifi_stop();
}

// Begin an HTTPClient request supporting both http and https URLs. The client
// objects must outlive the HTTPClient usage.
static bool httpBegin(HTTPClient &http, WiFiClient &plain,
                      WiFiClientSecure &secure, const String &url) {
  if (url.startsWith("https://")) {
    secure.setInsecure();  // LAN / self-signed friendly; tighten for production
    return http.begin(secure, url);
  }
  return http.begin(plain, url);
}

// Resolve a possibly-relative URL from the server ("/api/...") to an absolute
// one against BACKEND_URL.
static String absoluteUrl(const String &url) {
  if (url.startsWith("/")) return String(BACKEND_URL) + url;
  return url;
}

// ===========================================================================
// Status POST -> action
// ===========================================================================
static ActionResponse postStatus(float batteryPercent, int batteryMv,
                                  const char *wakeReason) {
  ActionResponse out;

  String url = String(BACKEND_URL) + "/api/device/" + DEVICE_ID + "/status";

  JsonDocument doc;
  if (batteryPercent >= 0) doc["battery_percent"] = batteryPercent;
  else                     doc["battery_percent"] = nullptr;
  if (batteryMv > 0)       doc["battery_mv"]       = batteryMv;
  doc["wake_reason"]      = wakeReason;
  doc["firmware_version"] = FIRMWARE_VERSION;
  doc["wifi_rssi"]        = WiFi.RSSI();
  doc["boot_count"]       = g_boot_count;
  doc["width"]            = DISPLAY_W;
  doc["height"]           = DISPLAY_H;
  doc["pixel_format"]     = "gxepd2_4bpp";  // hint: server should pack a frame
  if (g_last_image_id[0]) doc["last_image_id"] = g_last_image_id;
  else                    doc["last_image_id"] = nullptr;
  if (g_pending_error[0]) doc["last_error"] = g_pending_error;

  // Report the previous cycle's timings (this cycle's are not known yet).
  if (g_metrics.valid) {
    doc["wifi_ms"]     = g_metrics.wifi_ms;
    doc["post_ms"]     = g_metrics.post_ms;
    doc["download_ms"] = g_metrics.download_ms;
    doc["draw_ms"]     = g_metrics.draw_ms;
    doc["awake_ms"]    = g_metrics.awake_ms;
  }

  String body;
  serializeJson(doc, body);
  LOGF("[http] POST %s\n[http] body: %s\n", url.c_str(), body.c_str());

  WiFiClient plain;
  WiFiClientSecure secure;
  HTTPClient http;
  http.setTimeout(HTTP_TIMEOUT_MS);
  if (!httpBegin(http, plain, secure, url)) {
    LOGLN("[http] begin() failed");
    return out;
  }
  http.addHeader("Content-Type", "application/json");
  http.addHeader("Authorization", String("Bearer ") + DEVICE_TOKEN);

  uint32_t t0 = millis();
  int code = http.POST(body);
  g_post_ms = millis() - t0;
  LOGF("[http] status POST -> HTTP %d (%lu ms)\n", code, (unsigned long)g_post_ms);
  if (code != HTTP_CODE_OK) {
    if (code <= 0) LOGF("[http] transport error: %s\n",
                        http.errorToString(code).c_str());
    http.end();
    return out;
  }

  String payload = http.getString();
  http.end();

  JsonDocument resp;
  DeserializationError err = deserializeJson(resp, payload);
  if (err) {
    LOGF("[http] JSON parse error: %s\n", err.c_str());
    return out;
  }

  out.action            = (const char *)(resp["action"] | "noop");
  out.image_id          = (const char *)(resp["image_id"] | "");
  out.frame_url         = (const char *)(resp["frame_url"] | resp["image_url"] | "");
  out.next_wake_seconds = resp["next_wake_seconds"] | FALLBACK_UNREACHABLE_SECONDS;
  out.message           = (const char *)(resp["message"] | "");
  out.valid             = true;
  LOGF("[http] action=%s image_id=%s frame_url=%s next_wake=%llus\n",
       out.action.c_str(), out.image_id.c_str(),
       out.frame_url.length() ? out.frame_url.c_str() : "(none)",
       out.next_wake_seconds);
  return out;
}

// ===========================================================================
// Frame download + panel refresh
// ===========================================================================
// Download the packed frame into PSRAM. Caller frees the returned buffer.
static uint8_t *downloadFrame(const String &frameUrl) {
  String url = absoluteUrl(frameUrl);
  LOGF("[dl] GET %s\n", url.c_str());

  uint8_t *frame = (uint8_t *)heap_caps_malloc(
      FRAME_BYTES, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
  if (!frame) {
    LOGLN("[dl] PSRAM alloc failed");
    return nullptr;
  }

  WiFiClient plain;
  WiFiClientSecure secure;
  HTTPClient http;
  http.setTimeout(HTTP_TIMEOUT_MS);
  if (!httpBegin(http, plain, secure, url)) {
    heap_caps_free(frame);
    return nullptr;
  }
  http.addHeader("Authorization", String("Bearer ") + DEVICE_TOKEN);

  uint32_t t0 = millis();
  int code = http.GET();
  if (code != HTTP_CODE_OK) {
    LOGF("[dl] GET -> HTTP %d\n", code);
    http.end();
    heap_caps_free(frame);
    return nullptr;
  }

  int len = http.getSize();
  if (len > 0 && (uint32_t)len != FRAME_BYTES) {
    LOGF("[dl] WARN: Content-Length %d != expected %u\n",
         len, (unsigned)FRAME_BYTES);
  }

  WiFiClient *stream = http.getStreamPtr();
  uint32_t got = 0;
  uint32_t lastData = millis();
  while (http.connected() && got < FRAME_BYTES) {
    size_t avail = stream->available();
    if (avail) {
      int r = stream->readBytes(frame + got,
                                min((size_t)(FRAME_BYTES - got), avail));
      if (r <= 0) break;
      got += r;
      lastData = millis();
    } else {
      if (millis() - lastData > HTTP_TIMEOUT_MS) {
        LOGLN("[dl] stream stalled; aborting");
        break;
      }
      delay(2);
    }
  }
  http.end();
  g_download_ms = millis() - t0;
  LOGF("[dl] %u/%u bytes in %lu ms\n",
       (unsigned)got, (unsigned)FRAME_BYTES, (unsigned long)g_download_ms);

  if (got != FRAME_BYTES) {
    heap_caps_free(frame);
    return nullptr;
  }
  return frame;
}

// Push a native 4bpp frame into the driver's PSRAM framebuffer and run one full
// refresh. (data2 is unused on this single-plane panel.)
static void pushFrame(const uint8_t *frame) {
  uint32_t d0 = millis();
  display.epd2.writeNative(frame, nullptr, 0, 0, DISPLAY_W, DISPLAY_H,
                           /*invert*/ false, /*mirror_y*/ false, /*pgm*/ false);
  display.epd2.refresh(false);  // full Spectra-6 refresh (~20-40 s)
  display.epd2.hibernate();
  g_draw_ms = millis() - d0;
  LOGF("[draw] refresh done in %lu ms\n", (unsigned long)g_draw_ms);
}

static bool drawFrameFromUrl(const String &frameUrl) {
  if (!frameUrl.length()) {
    LOGLN("[draw] empty frame_url");
    return false;
  }
  uint8_t *frame = downloadFrame(frameUrl);
  if (!frame) return false;

  // The frame is in PSRAM; the radio is not needed during the long refresh.
  // Power it down now to save ~80-120 mA across the draw.
  shutdownWiFi();
  pushFrame(frame);
  heap_caps_free(frame);
  return true;
}

// Fill the panel white (used for the "blank" action).
static void drawBlank() {
  uint8_t *frame = (uint8_t *)heap_caps_malloc(
      FRAME_BYTES, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
  if (!frame) {
    LOGLN("[blank] PSRAM alloc failed");
    return;
  }
  memset(frame, WHITE_BYTE, FRAME_BYTES);
  shutdownWiFi();
  pushFrame(frame);
  heap_caps_free(frame);
}

// ===========================================================================
// Action handling
// ===========================================================================
static void handleAction(const ActionResponse &r) {
  LOGF("[action] handling '%s'\n", r.action.c_str());
  ledOn();  // solid while drawing

  if (r.action == "draw" || (r.action == "blank" && r.frame_url.length())) {
    if (drawFrameFromUrl(r.frame_url)) {
      strncpy(g_last_image_id, r.image_id.c_str(), sizeof(g_last_image_id) - 1);
      g_last_image_id[sizeof(g_last_image_id) - 1] = '\0';
      g_pending_error[0] = '\0';
      LOGF("[action] displayed image_id=%s\n", g_last_image_id);
    } else {
      snprintf(g_pending_error, sizeof(g_pending_error),
               "frame_failed:%s", r.image_id.c_str());
      LOGLN("[action] frame draw FAILED; keeping current image");
    }
  } else if (r.action == "blank") {
    drawBlank();
    g_last_image_id[0] = '\0';
  } else {
    // "noop" / "sleep": e-paper retains its image with no power, so leave the
    // panel untouched (a full refresh would flash the whole screen).
    LOGF("[action] %s: keeping current image\n", r.action.c_str());
  }

  if (r.message.length()) LOGF("[server] message: %s\n", r.message.c_str());
}

// ===========================================================================
// Deep sleep (ESP32-S3 timer + KEY0 button wake; RTC domain stays on ~14 uA)
// ===========================================================================
static void enterDeepSleep(uint64_t seconds) {
  if (seconds < 1) seconds = 1;
  if (seconds > MAX_SLEEP_SECONDS) {
    LOGF("[sleep] clamping %llu s -> %llu s\n", seconds, MAX_SLEEP_SECONDS);
    seconds = MAX_SLEEP_SECONDS;
  }

  // Snapshot this cycle's timings for the next wake to report (covers every
  // exit path: critical battery, wifi/post failure, normal end).
  g_metrics.wifi_ms     = g_wifi_ms;
  g_metrics.post_ms     = g_post_ms;
  g_metrics.download_ms = g_download_ms;
  g_metrics.draw_ms     = g_draw_ms;
  g_metrics.awake_ms    = millis();
  g_metrics.valid       = true;
  LOGF("[metrics] wifi=%lu post=%lu dl=%lu draw=%lu awake=%lu ms\n",
       (unsigned long)g_metrics.wifi_ms, (unsigned long)g_metrics.post_ms,
       (unsigned long)g_metrics.download_ms, (unsigned long)g_metrics.draw_ms,
       (unsigned long)g_metrics.awake_ms);

  shutdownWiFi();
  ledOff();
  LOGF("[sleep] next wake in %llu s (%.1f min)\n", seconds, seconds / 60.0);
  Serial.flush();

  esp_sleep_enable_timer_wakeup(seconds * 1000000ULL);
  // Also wake on KEY0 (GPIO4, active-low): hold an internal pull-up so the pin
  // idles HIGH and a press pulls it LOW to trigger the ext1 wake.
  rtc_gpio_pullup_en((gpio_num_t)WAKE_BUTTON_PIN);
  rtc_gpio_pulldown_dis((gpio_num_t)WAKE_BUTTON_PIN);
  esp_sleep_enable_ext1_wakeup(1ULL << WAKE_BUTTON_PIN, ESP_EXT1_WAKEUP_ANY_LOW);

  esp_deep_sleep_start();  // never returns
}

// ===========================================================================
// Wake reason
// ===========================================================================
static const char *wakeReasonString() {
  switch (esp_sleep_get_wakeup_cause()) {
    case ESP_SLEEP_WAKEUP_TIMER: return "timer";
    case ESP_SLEEP_WAKEUP_EXT1:  return "button";  // KEY0 pressed
    default:                     return (g_boot_count <= 1) ? "manual" : "unknown";
  }
}

// ===========================================================================
// Main wake cycle
// ===========================================================================
void setup() {
#if DEBUG_SERIAL
  Serial.begin(SERIAL_BAUD);
  uint32_t s0 = millis();
  while (!Serial && (millis() - s0) < SERIAL_READY_TIMEOUT_MS) delay(10);
  delay(50);
#endif

  g_boot_count++;
  ledInit();
  ledOn();  // green: awake / processing

  const char *wakeReason = wakeReasonString();
  LOGF("\n[boot] #%lu  fw=%s  wake=%s\n",
       (unsigned long)g_boot_count, FIRMWARE_VERSION, wakeReason);
  LOGF("[mem] heap=%lu KB  psram=%lu/%lu KB\n",
       (unsigned long)(ESP.getFreeHeap() / 1024),
       (unsigned long)(ESP.getFreePsram() / 1024),
       (unsigned long)(ESP.getPsramSize() / 1024));

  // Bring up SPI + panel driver (allocates the 937KB PSRAM framebuffer).
  hspi.begin(EPD_SCK_PIN, EPD_MISO_PIN, EPD_MOSI_PIN, -1);
  display.epd2.selectSPI(hspi, SPISettings(10000000, MSBFIRST, SPI_MODE0));
  display.init(SERIAL_BAUD);

  // --- Battery ---
  float batteryPercent;
  int   batteryMv;
  readBattery(&batteryPercent, &batteryMv);

  // --- Critical battery: skip everything, sleep long, report next time ---
  if (batteryPercent >= 0 && batteryPercent <= CRITICAL_BATTERY_PERCENT) {
    LOGF("[battery] CRITICAL (<= %.0f%%); skipping update\n",
         CRITICAL_BATTERY_PERCENT);
    beepError();
    snprintf(g_pending_error, sizeof(g_pending_error), "critical_battery");
    enterDeepSleep(FALLBACK_LOW_BATTERY_SECONDS);
  }

  // --- WiFi ---
  if (!connectWiFi()) {
    LOGLN("[net] WiFi unavailable -> fallback sleep");
    beepError();
    enterDeepSleep(FALLBACK_UNREACHABLE_SECONDS);
  }

  // --- Status POST ---
  ActionResponse r = postStatus(batteryPercent, batteryMv, wakeReason);
  if (!r.valid) {
    LOGLN("[net] status request failed -> fallback sleep");
    beepError();
    enterDeepSleep(FALLBACK_UNREACHABLE_SECONDS);
  }

  // --- Apply action + sleep until the server-chosen next wake ---
  handleAction(r);
  LOGF("[cycle] complete; awake %lu ms\n", (unsigned long)millis());
  enterDeepSleep(r.next_wake_seconds);
}

void loop() {
  // Never reached: setup() always ends in deep sleep.
}
