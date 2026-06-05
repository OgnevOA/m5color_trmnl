/*
 * m5color_trmnl - M5Stack PaperColor (SKU C151) firmware client
 * -------------------------------------------------------------
 * Device-side firmware for the TRMNL-like e-ink system. It mirrors the
 * reference logic in client.py:
 *
 *   wake -> connect WiFi -> read battery -> POST status -> handle action
 *        -> (download + draw PNG if needed) -> set wake timer -> deep sleep
 *
 * The backend is the single scheduling authority: the device simply obeys the
 * "next_wake_seconds" value returned from /api/device/{id}/status. Night mode
 * and normal intervals are computed server-side. The device only applies a few
 * local fallbacks when the backend is unreachable.
 *
 * Hardware (from https://docs.m5stack.com/en/core/PaperColor):
 *   - ESP32-S3R8, 16MB flash, 8MB PSRAM, 2.4GHz WiFi
 *   - 4" E Ink Spectra 6 full-color e-paper, 400x600 (EL040EF1)
 *   - 3 user buttons: BtnA=GPIO10, BtnB=GPIO9, BtnC=GPIO1
 *   - RX8130CE RTC, M5PM1 power management, 1250mAh battery
 *
 * Libraries (install via Library Manager or PlatformIO):
 *   - M5Unified  (https://github.com/m5stack/M5Unified)
 *   - M5GFX      (https://github.com/m5stack/M5GFX)
 *   - ArduinoJson
 *
 * Arduino IDE board: "M5PaperColor" (or generic ESP32-S3, PSRAM enabled).
 * Enable "USB CDC On Boot" so the serial debug log appears over USB.
 * PlatformIO env: see the board's docs page (env:m5stack-papercolor).
 */

#include <M5Unified.h>
#include <WiFi.h>
#include <WiFiClient.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <esp_sleep.h>
#include <esp_wifi.h>
#include <esp_system.h>
#include <esp_chip_info.h>
#include <driver/rtc_io.h>

// Custom types live in a header so the Arduino auto-generated prototypes (which
// are inserted above the sketch body) can see them. See trmnl_types.h.
#include "trmnl_types.h"

// ===========================================================================
// Debug logging
// ===========================================================================
// Set DEBUG_SERIAL to 0 to silence all serial output (e.g. for production /
// lowest power). When enabled, every line is prefixed with a millisecond
// timestamp so you can see how long each phase of the wake cycle takes.
#define DEBUG_SERIAL 0
#define SERIAL_BAUD  115200

#if DEBUG_SERIAL
  #define LOGF(fmt, ...) Serial.printf("[%8lu ms] " fmt, (unsigned long)millis(), ##__VA_ARGS__)
  #define LOGLN(msg)     Serial.printf("[%8lu ms] %s\n", (unsigned long)millis(), msg)
#else
  #define LOGF(fmt, ...) do {} while (0)
  #define LOGLN(msg)     do {} while (0)
#endif

// ===========================================================================
// Configuration
// ===========================================================================
// WiFi credentials, backend URL/device identity, and the static-IP settings
// live in secrets.h (copy secrets.h.example -> secrets.h). It is gitignored so
// the sketch itself can be shared without leaking credentials.
#include "secrets.h"

static const char *FIRMWARE_VERSION = "0.2.0";

// ===========================================================================
// Tunables
// ===========================================================================
static const int      DISPLAY_W = 400;
static const int      DISPLAY_H = 600;

static const uint32_t WIFI_CONNECT_TIMEOUT_MS = 20000;
static const uint32_t HTTP_TIMEOUT_MS         = 20000;
static const uint32_t SERIAL_READY_TIMEOUT_MS = 1500;  // wait for USB-CDC host

// Local fallbacks (only used when the server cannot be reached).
static const uint64_t FALLBACK_UNREACHABLE_SECONDS  = 30ULL * 60ULL;     // 30 min
static const uint64_t FALLBACK_LOW_BATTERY_SECONDS  = 6ULL * 3600ULL;    // 6 h
static const float    CRITICAL_BATTERY_PERCENT      = 5.0f;

// User button GPIOs (active-low) used as deep-sleep wake sources.
static const gpio_num_t BTN_A = GPIO_NUM_10;
static const gpio_num_t BTN_B = GPIO_NUM_9;
static const gpio_num_t BTN_C = GPIO_NUM_1;
static const uint64_t   BUTTON_WAKE_MASK =
    (1ULL << BTN_A) | (1ULL << BTN_B) | (1ULL << BTN_C);

// ===========================================================================
// Persistent state across deep sleep (kept in RTC slow memory)
// ===========================================================================
RTC_DATA_ATTR char rtc_last_image_id[32] = {0};
RTC_DATA_ATTR char rtc_pending_error[48] = {0};
RTC_DATA_ATTR uint32_t rtc_boot_count = 0;

// Per-cycle timing telemetry. The download/draw/awake values are only known
// after the status POST that triggered them, so each cycle's metrics are stashed
// here and reported on the *next* wake's POST (one-cycle lag, by design).
RTC_DATA_ATTR struct {
  uint32_t wifi_ms;
  uint32_t post_ms;
  uint32_t download_ms;
  uint32_t draw_ms;
  uint32_t awake_ms;
  bool     valid;
} rtc_metrics = {0, 0, 0, 0, 0, false};

// Scratch timings for the *current* cycle, filled in as each phase completes
// and flushed into rtc_metrics just before deep sleep.
static uint32_t g_wifi_ms     = 0;
static uint32_t g_post_ms     = 0;
static uint32_t g_download_ms = 0;
static uint32_t g_draw_ms     = 0;

// ===========================================================================
// Debug helpers
// ===========================================================================
// Give the USB-CDC serial port a moment to be opened by the host so the first
// lines of the log are not lost right after a reset.
static void waitForSerial() {
#if DEBUG_SERIAL
  uint32_t start = millis();
  while (!Serial && (millis() - start) < SERIAL_READY_TIMEOUT_MS) {
    delay(10);
  }
  delay(50);
#endif
}

static const char *resetReasonString(esp_reset_reason_t r) {
  switch (r) {
    case ESP_RST_POWERON:   return "power-on";
    case ESP_RST_EXT:       return "external";
    case ESP_RST_SW:        return "software";
    case ESP_RST_PANIC:     return "panic";
    case ESP_RST_INT_WDT:   return "int-wdt";
    case ESP_RST_TASK_WDT:  return "task-wdt";
    case ESP_RST_WDT:       return "other-wdt";
    case ESP_RST_DEEPSLEEP: return "deep-sleep";
    case ESP_RST_BROWNOUT:  return "brownout";
    case ESP_RST_SDIO:      return "sdio";
    default:                return "unknown";
  }
}

static const char *wakeCauseString(esp_sleep_wakeup_cause_t c) {
  switch (c) {
    case ESP_SLEEP_WAKEUP_TIMER: return "timer";
    case ESP_SLEEP_WAKEUP_EXT0:  return "ext0";
    case ESP_SLEEP_WAKEUP_EXT1:  return "ext1(button)";
    case ESP_SLEEP_WAKEUP_GPIO:  return "gpio";
    case ESP_SLEEP_WAKEUP_UNDEFINED: return "undefined(cold-boot)";
    default: return "other";
  }
}

// Print a one-shot banner with chip / memory / wake diagnostics.
static void printBootInfo(const char *wakeReason) {
#if DEBUG_SERIAL
  esp_chip_info_t chip;
  esp_chip_info(&chip);
  uint64_t mac = ESP.getEfuseMac();

  LOGLN("=====================================================");
  LOGF("  m5color_trmnl firmware v%s\n", FIRMWARE_VERSION);
  LOGF("  boot #%u  reset=%s  wake=%s (reason=%s)\n",
       rtc_boot_count,
       resetReasonString(esp_reset_reason()),
       wakeCauseString(esp_sleep_get_wakeup_cause()),
       wakeReason);
  LOGF("  chip: ESP32-S3 rev%d, %d core(s), %lu MHz\n",
       chip.revision, chip.cores, (unsigned long)getCpuFrequencyMhz());
  LOGF("  MAC: %02X:%02X:%02X:%02X:%02X:%02X\n",
       (uint8_t)(mac >> 40), (uint8_t)(mac >> 32), (uint8_t)(mac >> 24),
       (uint8_t)(mac >> 16), (uint8_t)(mac >> 8), (uint8_t)mac);
  LOGF("  flash: %lu KB   sketch: %lu/%lu KB\n",
       (unsigned long)(ESP.getFlashChipSize() / 1024),
       (unsigned long)(ESP.getSketchSize() / 1024),
       (unsigned long)((ESP.getSketchSize() + ESP.getFreeSketchSpace()) / 1024));
  LOGF("  heap free: %lu KB   PSRAM free: %lu/%lu KB\n",
       (unsigned long)(ESP.getFreeHeap() / 1024),
       (unsigned long)(ESP.getFreePsram() / 1024),
       (unsigned long)(ESP.getPsramSize() / 1024));
  LOGF("  RTC state: last_image='%s' pending_error='%s'\n",
       rtc_last_image_id[0] ? rtc_last_image_id : "(none)",
       rtc_pending_error[0] ? rtc_pending_error : "(none)");
  LOGF("  backend: %s  device: %s\n", BACKEND_URL, DEVICE_ID);
  LOGLN("=====================================================");
#endif
}

static void logHeap(const char *tag) {
  LOGF("[mem] %s: heap=%lu KB  psram=%lu KB\n", tag,
       (unsigned long)(ESP.getFreeHeap() / 1024),
       (unsigned long)(ESP.getFreePsram() / 1024));
}

// ===========================================================================
// Helpers
// ===========================================================================
static const char *wakeReasonString() {
  switch (esp_sleep_get_wakeup_cause()) {
    case ESP_SLEEP_WAKEUP_TIMER: return "timer";
    case ESP_SLEEP_WAKEUP_EXT0:
    case ESP_SLEEP_WAKEUP_EXT1:
    case ESP_SLEEP_WAKEUP_GPIO:  return "button";
    default:
      // First power-on / reset / brown-out: treat as manual on first boot.
      return (rtc_boot_count == 0) ? "manual" : "unknown";
  }
}

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
  wl_status_t last = WL_IDLE_STATUS;
  while (WiFi.status() != WL_CONNECTED) {
    wl_status_t now = WiFi.status();
    if (now != last) {
      LOGF("[wifi] status=%d (elapsed %lu ms)\n", (int)now,
           (unsigned long)(millis() - start));
      last = now;
    }
    if (millis() - start > WIFI_CONNECT_TIMEOUT_MS) {
      LOGF("[wifi] connect TIMEOUT after %lu ms (status=%d)\n",
           (unsigned long)(millis() - start), (int)WiFi.status());
      return false;
    }
    delay(200);
  }
  g_wifi_ms = millis() - start;
  LOGF("[wifi] connected in %lu ms\n", (unsigned long)g_wifi_ms);
  LOGF("[wifi] IP=%s  GW=%s  mask=%s\n",
       WiFi.localIP().toString().c_str(),
       WiFi.gatewayIP().toString().c_str(),
       WiFi.subnetMask().toString().c_str());
  LOGF("[wifi] DNS=%s  ch=%d  RSSI=%d dBm\n",
       WiFi.dnsIP().toString().c_str(), WiFi.channel(), WiFi.RSSI());
  return true;
}

static void shutdownWiFi() {
  LOGLN("[wifi] shutting down radio");
  WiFi.disconnect(true, false);
  WiFi.mode(WIFI_OFF);
  esp_wifi_stop();
}

// Begin an HTTPClient request supporting both http and https URLs.
// The client object must outlive the HTTPClient usage.
static bool httpBegin(HTTPClient &http, WiFiClient &plain,
                      WiFiClientSecure &secure, const String &url) {
  if (url.startsWith("https://")) {
    LOGLN("[http] using TLS (insecure / no cert check)");
    secure.setInsecure();  // LAN/self-signed friendly; tighten for production
    return http.begin(secure, url);
  }
  return http.begin(plain, url);
}

// ===========================================================================
// Display
// ===========================================================================
static void displayBlank() {
  LOGLN("[display] rendering blank white frame");
  uint32_t t0 = millis();
  M5.Display.startWrite();
  M5.Display.fillScreen(TFT_WHITE);
  M5.Display.endWrite();
  LOGF("[display] blank done in %lu ms\n", (unsigned long)(millis() - t0));
}

static bool drawPngBuffer(const uint8_t *data, size_t len) {
  if (data == nullptr || len == 0) {
    LOGLN("[display] drawPng skipped: empty buffer");
    return false;
  }
  LOGF("[display] drawPng start: %u bytes\n", (unsigned)len);
  uint32_t t0 = millis();
  M5.Display.startWrite();
  bool ok = M5.Display.drawPng(data, len, 0, 0, DISPLAY_W, DISPLAY_H);
  M5.Display.endWrite();
  g_draw_ms = millis() - t0;
  LOGF("[display] drawPng -> %s in %lu ms\n",
       ok ? "OK" : "FAILED", (unsigned long)g_draw_ms);
  return ok;
}

// ===========================================================================
// Backend communication
// ===========================================================================
// StatusResponse is declared in trmnl_types.h (included above).

static StatusResponse postStatus(float batteryPercent, int batteryMv,
                                  const char *wakeReason) {
  StatusResponse out;

  String url = String(BACKEND_URL) + "/api/device/" + DEVICE_ID + "/status";

  // Build the JSON request body.
  StaticJsonDocument<512> doc;
  if (batteryPercent >= 0)        doc["battery_percent"] = batteryPercent;
  else                            doc["battery_percent"] = nullptr;
  if (batteryMv > 0)              doc["battery_mv"]       = batteryMv;
  doc["wake_reason"]      = wakeReason;
  if (rtc_last_image_id[0]) doc["last_image_id"] = rtc_last_image_id;
  else                      doc["last_image_id"] = nullptr;
  doc["firmware_version"] = FIRMWARE_VERSION;
  doc["wifi_rssi"]        = WiFi.RSSI();
  if (rtc_pending_error[0]) doc["last_error"] = rtc_pending_error;

  // Report the previous cycle's timings (this cycle's download/draw/awake are
  // not known until after this POST returns; see rtc_metrics).
  if (rtc_metrics.valid) {
    doc["wifi_ms"]     = rtc_metrics.wifi_ms;
    doc["post_ms"]     = rtc_metrics.post_ms;
    doc["download_ms"] = rtc_metrics.download_ms;
    doc["draw_ms"]     = rtc_metrics.draw_ms;
    doc["awake_ms"]    = rtc_metrics.awake_ms;
  }

  String body;
  serializeJson(doc, body);

  LOGF("[http] POST %s\n", url.c_str());
  LOGF("[http] request body: %s\n", body.c_str());

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
  LOGF("[http] POST status -> HTTP %d (%lu ms)\n", code,
       (unsigned long)g_post_ms);
  if (code <= 0) {
    LOGF("[http] transport error: %s\n", http.errorToString(code).c_str());
    http.end();
    return out;
  }
  if (code != HTTP_CODE_OK) {
    LOGF("[http] unexpected status %d; body: %s\n", code,
         http.getString().c_str());
    http.end();
    return out;
  }

  String payload = http.getString();
  http.end();
  LOGF("[http] response body (%u bytes): %s\n",
       (unsigned)payload.length(), payload.c_str());

  StaticJsonDocument<512> resp;
  DeserializationError err = deserializeJson(resp, payload);
  if (err) {
    LOGF("[http] JSON parse error: %s\n", err.c_str());
    return out;
  }

  out.valid             = true;
  out.action            = resp["action"]  | "noop";
  out.image_id          = resp["image_id"] | "";
  out.image_url         = resp["image_url"] | "";
  out.next_wake_seconds = resp["next_wake_seconds"] | FALLBACK_UNREACHABLE_SECONDS;
  out.message           = resp["message"] | "";
  LOGF("[http] parsed: action=%s image_id=%s next_wake=%llus\n",
       out.action.c_str(), out.image_id.c_str(), out.next_wake_seconds);
  LOGF("[http] image_url=%s\n",
       out.image_url.length() ? out.image_url.c_str() : "(none)");
  return out;
}

// Download a PNG into a PSRAM buffer. Caller must free() the returned buffer.
static uint8_t *downloadImage(const String &imageUrl, size_t *outLen) {
  *outLen = 0;
  String url = imageUrl;
  if (url.startsWith("/")) url = String(BACKEND_URL) + imageUrl;

  LOGF("[img] GET %s\n", url.c_str());
  logHeap("before download");

  WiFiClient plain;
  WiFiClientSecure secure;
  HTTPClient http;
  http.setTimeout(HTTP_TIMEOUT_MS);
  if (!httpBegin(http, plain, secure, url)) {
    LOGLN("[img] begin() failed");
    return nullptr;
  }
  http.addHeader("Authorization", String("Bearer ") + DEVICE_TOKEN);

  uint32_t t0 = millis();
  int code = http.GET();
  LOGF("[img] GET -> HTTP %d (%lu ms)\n", code,
       (unsigned long)(millis() - t0));
  if (code != HTTP_CODE_OK) {
    if (code <= 0) {
      LOGF("[img] transport error: %s\n", http.errorToString(code).c_str());
    }
    http.end();
    return nullptr;
  }

  int len = http.getSize();  // may be -1 if chunked
  LOGF("[img] content-length: %d bytes\n", len);
  size_t cap = (len > 0) ? (size_t)len : 200000;  // 200KB fallback cap
  // Prefer PSRAM for the image buffer.
  uint8_t *buf = (uint8_t *)ps_malloc(cap);
  bool inPsram = (buf != nullptr);
  if (buf == nullptr) buf = (uint8_t *)malloc(cap);
  if (buf == nullptr) {
    LOGF("[img] buffer alloc FAILED (%u bytes)\n", (unsigned)cap);
    http.end();
    return nullptr;
  }
  LOGF("[img] allocated %u bytes in %s\n", (unsigned)cap,
       inPsram ? "PSRAM" : "heap");

  WiFiClient *stream = http.getStreamPtr();
  size_t received = 0;
  size_t nextLogMark = 16384;  // log progress every 16 KB
  uint32_t lastData = millis();
  while (http.connected() && (len < 0 || received < (size_t)len)) {
    size_t avail = stream->available();
    if (avail) {
      if (received + avail > cap) {
        // Grow the buffer if the fallback cap was too small.
        size_t newCap = cap * 2;
        LOGF("[img] growing buffer %u -> %u bytes\n",
             (unsigned)cap, (unsigned)newCap);
        uint8_t *grown = (uint8_t *)ps_realloc(buf, newCap);
        if (grown == nullptr) grown = (uint8_t *)realloc(buf, newCap);
        if (grown == nullptr) { LOGLN("[img] grow FAILED"); break; }
        buf = grown;
        cap = newCap;
      }
      int r = stream->readBytes(buf + received, avail);
      received += r;
      lastData = millis();
      if (received >= nextLogMark) {
        LOGF("[img] received %u bytes...\n", (unsigned)received);
        nextLogMark += 16384;
      }
    } else {
      if (millis() - lastData > HTTP_TIMEOUT_MS) {
        LOGLN("[img] stream stalled; aborting");
        break;
      }
      delay(5);
    }
  }
  http.end();

  if (received == 0) {
    LOGLN("[img] download produced 0 bytes");
    free(buf);
    return nullptr;
  }
  *outLen = received;
  g_download_ms = millis() - t0;
  LOGF("[img] download complete: %u bytes in %lu ms\n",
       (unsigned)received, (unsigned long)g_download_ms);
  logHeap("after download");
  return buf;
}

// ===========================================================================
// Action handling
// ===========================================================================
static void handleAction(const StatusResponse &r) {
  LOGF("[action] handling '%s'\n", r.action.c_str());
  if (r.action == "draw" || (r.action == "blank" && r.image_url.length())) {
    size_t len = 0;
    uint8_t *png = downloadImage(r.image_url, &len);
    if (png == nullptr) {
      // Keep current content; report the failure on the next status post.
      snprintf(rtc_pending_error, sizeof(rtc_pending_error),
               "image_download_failed:%s", r.image_id.c_str());
      LOGLN("[action] image download FAILED; keeping current display");
      return;
    }
    if (drawPngBuffer(png, len)) {
      strncpy(rtc_last_image_id, r.image_id.c_str(), sizeof(rtc_last_image_id) - 1);
      rtc_last_image_id[sizeof(rtc_last_image_id) - 1] = '\0';
      rtc_pending_error[0] = '\0';
      LOGF("[action] displayed image_id=%s\n", rtc_last_image_id);
    } else {
      snprintf(rtc_pending_error, sizeof(rtc_pending_error),
               "draw_failed:%s", r.image_id.c_str());
      LOGLN("[action] drawPng FAILED");
    }
    free(png);
  } else if (r.action == "blank") {
    displayBlank();
    rtc_last_image_id[0] = '\0';
  } else {
    // "sleep" or "noop": keep the current display untouched.
    LOGF("[action] %s: keeping current display untouched\n", r.action.c_str());
  }

  if (r.message.length()) {
    LOGF("[server] message: %s\n", r.message.c_str());
  }
}

// ===========================================================================
// Deep sleep
// ===========================================================================
static void enterDeepSleep(uint64_t seconds) {
  if (seconds < 1) seconds = 1;

  // Snapshot this cycle's timings for the next wake to report. Done here so it
  // covers every exit path (critical battery, wifi/post failure, normal end).
  rtc_metrics.wifi_ms     = g_wifi_ms;
  rtc_metrics.post_ms     = g_post_ms;
  rtc_metrics.download_ms = g_download_ms;
  rtc_metrics.draw_ms     = g_draw_ms;
  rtc_metrics.awake_ms    = millis();
  rtc_metrics.valid       = true;
  LOGF("[metrics] cycle: wifi=%lu post=%lu download=%lu draw=%lu awake=%lu ms\n",
       (unsigned long)rtc_metrics.wifi_ms, (unsigned long)rtc_metrics.post_ms,
       (unsigned long)rtc_metrics.download_ms, (unsigned long)rtc_metrics.draw_ms,
       (unsigned long)rtc_metrics.awake_ms);

  LOGF("[sleep] next wake in %llu s (%.1f min)\n", seconds, seconds / 60.0);

  shutdownWiFi();
  LOGLN("[sleep] powering down e-paper controller");
  M5.Display.sleep();  // power down the panel controller

  // Wake on timer.
  esp_sleep_enable_timer_wakeup(seconds * 1000000ULL);

  // Wake on any user-button press (active-low). EXT1 ANY_LOW on ESP32-S3.
  // Use RTC-GPIO pull-ups so the lines idle high and the config is retained
  // through deep sleep (the buttons are RTC-capable: GPIO1/9/10).
  const gpio_num_t buttons[] = {BTN_A, BTN_B, BTN_C};
  for (gpio_num_t pin : buttons) {
    rtc_gpio_pullup_en(pin);
    rtc_gpio_pulldown_dis(pin);
  }
  esp_sleep_enable_ext1_wakeup(BUTTON_WAKE_MASK, ESP_EXT1_WAKEUP_ANY_LOW);
  LOGF("[sleep] wake sources: timer + buttons (GPIO %d/%d/%d)\n",
       (int)BTN_A, (int)BTN_B, (int)BTN_C);
  logHeap("before sleep");

  LOGLN("[sleep] entering deep sleep now");
  Serial.flush();
  esp_deep_sleep_start();  // never returns
}

// ===========================================================================
// Main wake cycle
// ===========================================================================
void setup() {
#if DEBUG_SERIAL
  Serial.begin(SERIAL_BAUD);
  waitForSerial();
#endif

  auto cfg = M5.config();
  // Do NOT clear the panel on init. E-paper retains its image without power,
  // so on a 'noop'/'sleep' wake we must leave the existing content untouched
  // (clearing would cause a full white refresh/flash every single wake).
  cfg.clear_display = false;
  M5.begin(cfg);            // powers the panel, buttons, RTC, etc.
  delay(50);

  rtc_boot_count++;
  const char *wakeReason = wakeReasonString();
  printBootInfo(wakeReason);

  // --- Battery ---
  float batteryPercent = M5.Power.getBatteryLevel();      // 0..100 (-1 if n/a)
  int   batteryMv      = M5.Power.getBatteryVoltage();     // mV (0 if n/a)
  int   charging       = (int)M5.Power.isCharging();
  LOGF("[battery] level=%.0f%%  voltage=%d mV  charging=%d\n",
       batteryPercent, batteryMv, charging);

  // --- Critical battery: skip rendering, sleep long, report next time ---
  if (batteryPercent >= 0 && batteryPercent <= CRITICAL_BATTERY_PERCENT) {
    LOGF("[battery] CRITICAL (<= %.0f%%); skipping update\n",
         CRITICAL_BATTERY_PERCENT);
    snprintf(rtc_pending_error, sizeof(rtc_pending_error), "critical_battery");
    enterDeepSleep(FALLBACK_LOW_BATTERY_SECONDS);
  }

  // --- WiFi ---
  if (!connectWiFi()) {
    LOGLN("[net] WiFi unavailable; backend unreachable -> fallback sleep");
    enterDeepSleep(FALLBACK_UNREACHABLE_SECONDS);
  }

  // --- Status POST ---
  StatusResponse r = postStatus(batteryPercent, batteryMv, wakeReason);
  if (!r.valid) {
    LOGLN("[net] status request failed -> fallback sleep");
    enterDeepSleep(FALLBACK_UNREACHABLE_SECONDS);
  }

  // --- Apply action ---
  handleAction(r);

  // --- Sleep until the server-chosen next wake ---
  LOGF("[cycle] complete; total awake time so far %lu ms\n",
       (unsigned long)millis());
  enterDeepSleep(r.next_wake_seconds);
}

void loop() {
  // Never reached: setup() always ends in deep sleep.
}
