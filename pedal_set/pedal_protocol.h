#pragma once
#include <stdint.h>

// ==========================================
//   PEDAL SET WIRE PROTOCOL (Blue Pill <-> PC calibration app)
// ==========================================
// Carried over USB CDC serial on the same cable as the HID joystick -
// USBComposite_stm32f1 enumerates both as one composite device, so the
// PC app just opens the "Pedal Set" COM port. The old Serial1 debug UART
// stays as-is for bring-up prints. Mirrors the framing used by the
// dash's binary protocol: two-byte sync anchor + length-prefixed payload
// + checksum, so a corrupted/partial frame is dropped rather than
// misparsed, and the parser can always resync on the next 0xAA 0x55.
//
// [0]  SYNC0 = 0xAA
// [1]  SYNC1 = 0x55
// [2]  MSG_TYPE
// [3]  LEN            (payload length)
// [4..4+LEN-1]  PAYLOAD
// [4+LEN]  CHECKSUM   (8-bit sum of MSG_TYPE + LEN + payload, mod 256)

#define PEDAL_SYNC0 0xAA
#define PEDAL_SYNC1 0x55
// Bumped 1->2 (2026-08) when CalibrationData's layout changed (gamma floats
// -> curve byte arrays, same total field count but different byte offsets).
// Without this bump, a chip with a valid saved v1 calibration would pass
// loadCalibration()'s magic+version check and memcpy old-format bytes
// straight into the new struct - throttleGamma/brakeGamma's raw float
// bytes (plus a couple of never-written EEPROM words past the old 20-byte
// struct's end) reinterpreted as 10 curve-point bytes, i.e. garbage curve
// data (sanitizeCalibration() clamps anything >100 but can't recover
// anything meaningful from it). Bumping the version makes that case fail
// the check cleanly and fall back to the new compiled-in defaults instead.
#define PEDAL_PROTOCOL_VERSION 2
#define PEDAL_MAX_PAYLOAD 32

#define MSG_STATUS           0x01  // device -> PC, periodic live pedal values
#define MSG_SET_CALIBRATION  0x02  // PC -> device, apply to RAM only (not persisted)
#define MSG_SAVE             0x03  // PC -> device, persist current RAM calibration to flash
#define MSG_GET_CALIBRATION  0x04  // PC -> device, request current calibration
#define MSG_CALIBRATION      0x84  // device -> PC, current calibration (reply to GET/SET, and once at boot)
#define MSG_SAVE_ACK         0x83  // device -> PC, 1-byte payload: 1 = flash write ok, 0 = failed

// PC -> device (MSG_SET_CALIBRATION payload) and device -> PC
// (MSG_CALIBRATION payload). Same struct both directions so the PC app's
// "current calibration" view always reflects exactly what the firmware
// is using - it never has to guess or keep a shadow copy.
//
// throttleCurve/brakeCurve replace the old single-gamma exponent (2026-08)
// with 5 explicit response-curve points, each an output percentage (0-100)
// at a fixed input breakpoint (0/25/50/75/100% of pedal travel) - the PC
// app's Pedals tab exposes these as 5 sliders per pedal plus preset
// buttons (linear/S-curve/exponential/reverse exponential) that just set
// the 5 values. Firmware linearly interpolates between them.
struct __attribute__((packed)) CalibrationData {
  int16_t hallMin;
  int16_t hallMax;
  int32_t loadMin;
  int32_t loadMax;
  uint8_t throttleCurve[5];
  uint8_t brakeCurve[5];
};

// device -> PC (MSG_STATUS payload), sent periodically for the live monitor.
// No wheel/radio fields here - the Blue Pill no longer touches the wheel's
// wireless link at all (moved to the ESP32-S3 dash, 2026-07-16). This board
// is pedals-only: throttle/brake axes in, calibration in/out over USB.
struct __attribute__((packed)) StatusPacket {
  int16_t  hallRaw;
  int16_t  hallFiltered;
  int32_t  loadRaw;
  uint16_t throttleAxis;   // 0-1023, the same value sent to the HID joystick
  uint16_t brakeAxis;      // 0-1023
};
