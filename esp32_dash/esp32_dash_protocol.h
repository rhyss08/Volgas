#pragma once
#include <stdint.h>

// ==========================================
//   ESP32-S3 DASH TELEMETRY PROTOCOL
// ==========================================
// Minimal protocol for the Classic screen (gear, speed, RPM bar only) -
// same scoped-down approach as the earlier Nano version of this (see
// docs/wireless_telemetry_relay_attempt.md for that history), not the
// full dash plan's TelemetryPacket (docs/dash_multiscreen_plan.md), which
// carries lap times, tire temps, fuel, etc. for all 5 planned screens.
// Extend this (or move to the full TelemetryPacket) if more screens get
// built later.
//
// Carried over the ESP32-S3's native USB CDC serial (the sketch's own
// explicit `USBCDC USBSerial` - NOT CDCOnBoot, see esp32_dash.ino header
// comment for why), composited alongside the USB HID gamepad on the same
// cable. Same framing idiom as the rest of this project: two-byte sync
// anchor + length-prefixed payload + checksum.
//
// [0]  SYNC0 = 0xAA
// [1]  SYNC1 = 0x55
// [2]  MSG_TYPE
// [3]  LEN            (payload length)
// [4..4+LEN-1]  PAYLOAD
// [4+LEN]  CHECKSUM   (8-bit sum of MSG_TYPE + LEN + payload, mod 256)

#define DASH_SYNC0 0xAA
#define DASH_SYNC1 0x55
#define DASH_MAX_PAYLOAD 16

#define MSG_DASH_TELEMETRY 0x01   // PC -> ESP32-S3
#define MSG_TIME_SYNC      0x02   // PC -> ESP32-S3
#define MSG_LAP_TIME       0x03   // PC -> ESP32-S3, current lap running time, sent periodically while on track
#define MSG_LAP_DELTA      0x04   // PC -> ESP32-S3, sent once per sector crossing (was MSG_SECTOR_SPLIT, a temporary overlay - simplified 2026-07-25 into a persistent readout)

struct __attribute__((packed)) DashTelemetry {
  uint16_t speedKph;
  uint16_t rpm;
  uint16_t rpmMax;
  int8_t   gear;   // -1=R, 0=N, 1..N
};

// Current lap's running time, for the Classic screen's footer (small text,
// the default state of that area). Milliseconds rather than a pre-formatted
// string keeps this a fixed-size struct like everything else here - the
// dash formats it for display.
//
// `valid` added 2026-07-25: rFactor2's own HUD draws a line through the lap
// time when the current lap won't count (e.g. a track-limits violation) -
// the dash replicates that instead of just showing a number with no
// indication it won't actually count. Derived PC-side from
// RF2VehicleScoring's mCountLapFlag (recalled ISI/Studio397 plugin
// convention, not independently re-verified: 0 = don't count lap or time,
// 1 = count lap but not time, 2 = count lap and time - valid here means ==2).
struct __attribute__((packed)) LapTime {
  uint32_t lapTimeMs;
  uint8_t  valid;   // 1 = counts for time (normal), 0 = invalid - dash draws a strikethrough
};

// Sent every telemetry frame (20Hz), like LapTime - not a one-shot event
// anymore (2026-07-25 rewrite, see CLAUDE.md for the version history: a
// temporary sector-split overlay, then a sector-crossing-only persistent
// value, now a continuously-interpolated one against a PC-recorded
// distance-vs-time reference curve of the player's best lap this session,
// with a coarser game-data-only fallback for the window before that curve
// exists). Comparison basis is the player's best lap this session (not the
// previous lap).
//
// `valid` added 2026-07-25: sent every frame now instead of only when a
// value happens to be available, specifically so resetting the car (or
// sitting in the pits - mLapDist goes negative either way) can explicitly
// tell the dash "nothing to show right now" - previously the PC side just
// stopped sending anything in that case, which left the dash frozen on
// whatever delta was last computed before the reset instead of blanking.
struct __attribute__((packed)) LapDelta {
  int32_t cumulativeDeltaMs;   // total time faster/slower than the best lap, at this point in the lap - negative=faster/ahead (green), positive=slower/behind (red)
  uint8_t valid;               // 1 = show cumulativeDeltaMs, 0 = blank the line (no reference yet, or car reset/in pits)
};

// Local day/time, for the standby screen when no telemetry is coming in.
// No RTC on this board and no WiFi/NTP - the PC companion app is the only
// time source, sent periodically whenever it's running. dayOfWeek matches
// Python's time.localtime().tm_wday / datetime.weekday(): Monday=0 .. Sunday=6.
//
// `second` added 2026-07-25: the dash now keeps its own running clock
// between syncs (millis() elapsed since the last sync's arrival, added to
// the synced timestamp) instead of just displaying whatever the last sync
// said and going stale/disappearing if syncs stop - second-level precision
// at the anchor point keeps that running clock from drifting a full minute
// off immediately after each resync.
struct __attribute__((packed)) TimeSync {
  uint8_t dayOfWeek;
  uint8_t hour;     // 0-23
  uint8_t minute;   // 0-59
  uint8_t second;   // 0-59
};
