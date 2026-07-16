#pragma once

// Custom types used by the E1004 firmware.
//
// These live in a header (not directly in the .ino) on purpose: the Arduino IDE
// auto-generates function prototypes and inserts them near the top of the
// sketch, before any struct defined in the .ino body. A function that returns
// or accepts such a struct would then reference an unknown type
// ("'ActionResponse' does not name a type"). Types declared in an included
// header are known before those generated prototypes, avoiding the error.

#include <Arduino.h>

// Parsed result of POST /api/device/{id}/status.
struct ActionResponse {
  bool     valid             = false;
  String   action            = "noop";  // "draw" | "noop" | "sleep" | "blank"
  String   image_id;
  String   frame_url;                    // URL of the packed .bin frame
  uint64_t next_wake_seconds = 1800ULL;  // 30 min fallback default
  String   message;
};

// Per-cycle timing telemetry. Reported on the *next* wake's POST (one-cycle
// lag, by design) since this cycle's download/draw/awake times are only known
// after the POST that triggered them. Plain PODs so the whole struct can live
// in RTC_DATA_ATTR across deep sleep (no String / heap members).
struct Metrics {
  uint32_t wifi_ms     = 0;
  uint32_t post_ms     = 0;
  uint32_t download_ms = 0;
  uint32_t draw_ms     = 0;
  uint32_t awake_ms    = 0;
  bool     valid       = false;
};
