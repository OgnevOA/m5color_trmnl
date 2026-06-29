/*
 * e1004_trmnl - Seeed reTerminal E1004 firmware client (SKELETON)
 * ---------------------------------------------------------------
 * Minimal device-side firmware that validates the E1004 wire format
 * end-to-end against the same backend the M5 PaperColor client talks to:
 *
 *   wake -> connect WiFi -> POST status -> handle action
 *        -> (download packed frame + draw if needed) -> deep sleep
 *
 * Unlike the M5 client, the server sends a RAW pre-dithered framebuffer
 * (NOT a PNG): exactly app.render.e1004.render_e1004_frame() output --
 *   1200 x 1600, 4bpp, 600 bytes/row, 960000 bytes total,
 *   high nibble = even x, low nibble = odd x,
 *   nibble = GxEPD2 color7 index (0=Black 1=White 2=Green 3=Blue 4=Red 5=Yellow).
 * The device does no dithering/decoding: it copies the bytes into the panel
 * framebuffer via writeNative() and triggers one full refresh (~30-40 s).
 *
 * Hardware (https://wiki.seeedstudio.com/getting_started_with_reterminal_e1004/):
 *   - ESP32-S3 (XIAO ESP32-S3), 8MB OPI PSRAM  (PSRAM REQUIRED for the 937KB FB)
 *   - 13.3" E Ink Spectra 6, 1200x1600, T133A01 dual-chip controller
 *   - PCF8563 RTC (I2C 0x51, GPIO19/20, CR1220 backup), 5000mAh battery
 *   - KEY0 wake button = GPIO4
 *
 * SETUP (one-time):
 *   1. Arduino IDE board: "XIAO_ESP32S3";  Tools > PSRAM: "OPI PSRAM".
 *   2. Install libraries: Adafruit GFX, and the Seeed fork of GxEPD2
 *      (https://github.com/Seeed-Projects/Seeed_GxEPD2).
 *   3. Copy the panel driver pair from that repo's example into THIS folder:
 *        examples/GxEPD2_reTerminal_E1004/GxEPD2_T133A01_1200x1600.h
 *        examples/GxEPD2_reTerminal_E1004/GxEPD2_T133A01_1200x1600.cpp
 *      (GxEPD2 ships them inside the example, not under src/.)
 *   4. Fill in the WiFi / server defines below (or move them to secrets.h).
 *
 * This is a SKELETON: it focuses on the wake/POST/draw/sleep loop and the new
 * raw-frame transport. Telemetry timings, health alerts, LED/buzzer feedback,
 * and static-IP fast connect are intentionally left as TODOs to port from the
 * M5 client once the panel pipeline is confirmed.
 */

#include <Arduino.h>
#include <SPI.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <esp_sleep.h>
#include <esp_heap_caps.h>

#include <GxEPD2_7C.h>
#include "GxEPD2_T133A01_1200x1600.h"

// ===========================================================================
// Configuration  (TODO: move secrets into a secrets.h like the M5 client)
// ===========================================================================
#define FIRMWARE_VERSION   "e1004-0.1.0"

#define WIFI_SSID          "your-ssid"
#define WIFI_PASSWORD      "your-pass"

// Backend base URL, e.g. "http://192.168.1.50:17555". The device id is part of
// the status path, matching the M5 client's /api/device/{id}/status contract.
#define SERVER_BASE_URL    "http://192.168.1.50:17555"
#define DEVICE_ID          "reterminal-e1004-01"
#define DEVICE_TOKEN       "change-me-device-token"

#define WIFI_CONNECT_TIMEOUT_MS   15000
#define HTTP_TIMEOUT_MS           20000

// Local fallbacks when the backend is unreachable (server is otherwise the
// single scheduling authority via next_wake_seconds).
#define FALLBACK_UNREACHABLE_SECONDS  900
#define MAX_SLEEP_SECONDS             14400   // 4 h RTC timer ceiling

// ===========================================================================
// Display: 13.3" 6-color 1200x1600, dual-chip T133A01 (pins from Seeed Setup523)
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

static constexpr int32_t  DISPLAY_W = 1200;
static constexpr int32_t  DISPLAY_H = 1600;
static constexpr uint32_t FRAME_BYTES = (uint32_t)DISPLAY_W * DISPLAY_H / 2; // 960000

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
// Persistent across deep sleep. Standard ESP32-S3 deep sleep keeps the RTC
// domain powered (~14 uA), so RTC_DATA_ATTR survives -- no NVS dance needed
// (unlike the M5 client, which does a full M5PM1 power-off).
// ===========================================================================
RTC_DATA_ATTR static uint32_t g_boot_count = 0;
RTC_DATA_ATTR static char     g_last_image_id[40] = {0};

#define LOGF(...)  Serial.printf(__VA_ARGS__)
#define LOGLN(s)   Serial.println(F(s))

struct ActionResponse {
  bool     valid = false;
  String   action;            // "draw" | "noop" | "sleep" | "blank"
  String   image_id;
  String   frame_url;         // absolute URL of the packed .bin frame
  uint64_t next_wake_seconds = FALLBACK_UNREACHABLE_SECONDS;
};

// ===========================================================================
// WiFi
// ===========================================================================
static bool connectWiFi() {
  WiFi.persistent(false);
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  uint32_t start = millis();
  while (WiFi.status() != WL_CONNECTED) {
    if (millis() - start > WIFI_CONNECT_TIMEOUT_MS) {
      LOGF("[wifi] connect TIMEOUT (status=%d)\n", (int)WiFi.status());
      return false;
    }
    delay(50);
  }
  LOGF("[wifi] connected in %lu ms, IP=%s\n",
       (unsigned long)(millis() - start), WiFi.localIP().toString().c_str());
  return true;
}

static void shutdownWiFi() {
  WiFi.disconnect(true);
  WiFi.mode(WIFI_OFF);
}

// ===========================================================================
// Status POST -> action
// ===========================================================================
static ActionResponse postStatus(float batteryPercent, int batteryMv) {
  ActionResponse out;

  String url = String(SERVER_BASE_URL) + "/api/device/" + DEVICE_ID + "/status";
  HTTPClient http;
  http.setTimeout(HTTP_TIMEOUT_MS);
  if (!http.begin(url)) {
    LOGLN("[http] begin failed");
    return out;
  }
  http.addHeader("Content-Type", "application/json");
  http.addHeader("Authorization", String("Bearer ") + DEVICE_TOKEN);

  JsonDocument doc;
  doc["firmware_version"] = FIRMWARE_VERSION;
  doc["battery_percent"]  = batteryPercent;
  doc["battery_mv"]       = batteryMv;
  doc["wifi_rssi"]        = WiFi.RSSI();
  doc["boot_count"]       = g_boot_count;
  doc["last_image_id"]    = g_last_image_id;
  doc["width"]            = DISPLAY_W;
  doc["height"]           = DISPLAY_H;
  doc["pixel_format"]     = "gxepd2_4bpp";  // tells the server to pack a frame

  String body;
  serializeJson(doc, body);

  int code = http.POST(body);
  if (code != HTTP_CODE_OK) {
    LOGF("[http] status POST -> HTTP %d\n", code);
    http.end();
    return out;
  }

  JsonDocument resp;
  DeserializationError err = deserializeJson(resp, http.getString());
  http.end();
  if (err) {
    LOGF("[http] JSON parse error: %s\n", err.c_str());
    return out;
  }

  out.action            = (const char*)(resp["action"] | "noop");
  out.image_id          = (const char*)(resp["image_id"] | "");
  out.frame_url         = (const char*)(resp["frame_url"] | resp["image_url"] | "");
  out.next_wake_seconds = resp["next_wake_seconds"] | FALLBACK_UNREACHABLE_SECONDS;
  out.valid             = true;
  LOGF("[http] action=%s image_id=%s next_wake=%llus\n",
       out.action.c_str(), out.image_id.c_str(), out.next_wake_seconds);
  return out;
}

// ===========================================================================
// Download the packed frame into PSRAM and push it to the panel
// ===========================================================================
static bool drawFrameFromUrl(const String& url) {
  if (!url.length()) { LOGLN("[draw] empty frame_url"); return false; }

  uint8_t* frame = (uint8_t*)heap_caps_malloc(FRAME_BYTES,
                                              MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
  if (!frame) { LOGLN("[draw] PSRAM alloc failed"); return false; }

  HTTPClient http;
  http.setTimeout(HTTP_TIMEOUT_MS);
  if (!http.begin(url)) { heap_caps_free(frame); return false; }
  http.addHeader("Authorization", String("Bearer ") + DEVICE_TOKEN);

  int code = http.GET();
  if (code != HTTP_CODE_OK) {
    LOGF("[draw] GET %s -> HTTP %d\n", url.c_str(), code);
    http.end(); heap_caps_free(frame); return false;
  }

  int len = http.getSize();
  if (len > 0 && (uint32_t)len != FRAME_BYTES) {
    LOGF("[draw] WARN: Content-Length %d != expected %u\n", len, (unsigned)FRAME_BYTES);
  }

  WiFiClient* stream = http.getStreamPtr();
  uint32_t got = 0;
  uint32_t t0 = millis();
  while (http.connected() && got < FRAME_BYTES) {
    size_t avail = stream->available();
    if (avail) {
      int r = stream->readBytes(frame + got,
                                min((size_t)(FRAME_BYTES - got), avail));
      if (r <= 0) break;
      got += r;
    } else {
      if (millis() - t0 > HTTP_TIMEOUT_MS) break;
      delay(1);
    }
  }
  http.end();
  LOGF("[draw] downloaded %u/%u bytes in %lu ms\n",
       (unsigned)got, (unsigned)FRAME_BYTES, (unsigned long)(millis() - t0));
  if (got != FRAME_BYTES) { heap_caps_free(frame); return false; }

  // Push our native 4bpp frame straight into the driver's PSRAM framebuffer,
  // then run one full refresh. (data2 is unused on this single-plane panel.)
  uint32_t d0 = millis();
  display.epd2.writeNative(frame, nullptr, 0, 0, DISPLAY_W, DISPLAY_H,
                           /*invert*/ false, /*mirror_y*/ false, /*pgm*/ false);
  display.epd2.refresh(false);   // ~30-40 s full Spectra-6 refresh
  display.epd2.hibernate();
  LOGF("[draw] refresh done in %lu ms\n", (unsigned long)(millis() - d0));

  heap_caps_free(frame);
  return true;
}

static void handleAction(const ActionResponse& r) {
  if (r.action == "draw") {
    if (drawFrameFromUrl(r.frame_url)) {
      strncpy(g_last_image_id, r.image_id.c_str(), sizeof(g_last_image_id) - 1);
      g_last_image_id[sizeof(g_last_image_id) - 1] = '\0';
    }
  } else {
    // "noop" / "sleep" / "blank": e-paper retains its image with no power, so
    // do nothing -- the panel keeps showing whatever it last drew.
    LOGF("[action] %s: keeping current image\n", r.action.c_str());
  }
}

// ===========================================================================
// Deep sleep (plain ESP32-S3 timer wake; RTC domain stays on at ~14 uA)
// ===========================================================================
static void enterDeepSleep(uint64_t seconds) {
  if (seconds < 1) seconds = 1;
  if (seconds > MAX_SLEEP_SECONDS) seconds = MAX_SLEEP_SECONDS;
  shutdownWiFi();
  LOGF("[sleep] next wake in %llu s\n", seconds);
  Serial.flush();

  esp_sleep_enable_timer_wakeup(seconds * 1000000ULL);
  // Optional: also wake on KEY0 (GPIO4, active low). EXT1 needs an RTC GPIO.
  // esp_sleep_enable_ext1_wakeup(1ULL << 4, ESP_EXT1_WAKEUP_ANY_LOW);
  esp_deep_sleep_start();  // never returns
}

// ===========================================================================
// Main wake cycle
// ===========================================================================
void setup() {
  Serial.begin(115200);
  delay(100);
  g_boot_count++;
  LOGF("\n[boot] #%lu  fw=%s  wake_cause=%d\n",
       (unsigned long)g_boot_count, FIRMWARE_VERSION,
       (int)esp_sleep_get_wakeup_cause());

  // Bring up SPI + panel driver (allocates the 937KB PSRAM framebuffer).
  hspi.begin(EPD_SCK_PIN, EPD_MISO_PIN, EPD_MOSI_PIN, -1);
  display.epd2.selectSPI(hspi, SPISettings(10000000, MSBFIRST, SPI_MODE0));
  display.init(115200);

  // TODO: read the real battery via ADC (GPIO6 expansion / onboard divider) and
  // the PCF8563 RTC over I2C (0x51, GPIO19/20). Stubbed for the skeleton.
  float batteryPercent = 100.0f;
  int   batteryMv      = 4200;

  if (!connectWiFi()) {
    enterDeepSleep(FALLBACK_UNREACHABLE_SECONDS);
  }

  ActionResponse r = postStatus(batteryPercent, batteryMv);
  if (!r.valid) {
    enterDeepSleep(FALLBACK_UNREACHABLE_SECONDS);
  }

  handleAction(r);
  enterDeepSleep(r.next_wake_seconds);
}

void loop() {
  // Never reached: setup() always ends in deep sleep.
}
