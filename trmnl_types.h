#pragma once

// Custom types used by the firmware.
//
// This lives in a header (instead of directly in the .ino) on purpose: the
// Arduino IDE auto-generates function prototypes and inserts them near the top
// of the sketch, before any struct defined in the .ino body. Functions that
// return or accept a custom type would then reference an unknown type
// ("'StatusResponse' does not name a type"). Types declared in an included
// header are known before those generated prototypes, which avoids the error.

#include <Arduino.h>

// Parsed result of POST /api/device/{id}/status.
struct StatusResponse {
  bool     valid             = false;
  String   action            = "noop";
  String   image_id;
  String   image_url;
  uint64_t next_wake_seconds = 30ULL * 60ULL;  // 30 min fallback default
  String   message;
  String   epd_mode;  // refresh waveform hint: "" (quality) / "text" / "fast"
};
