#pragma once
#include <stdint.h>

// ==========================================
//   HUB <-> WHEEL RADIO LINK (ESP-NOW)
// ==========================================
// Shared by hub_s3 (the ESP32-S3 on USB to the PC) and esp32_dash (the
// ESP32-S3 Mini on the wheel). Both sketches carry an identical copy of this
// file - pc_app/tests/test_firmware_link.py checks they match.
//
// Pairing: the wheel broadcasts LINK_HELLO every LINK_HELLO_MS until it hears
// from a hub, then talks to that hub directly (and repeats the hello to it
// every LINK_HELLO_LINKED_MS, so the hub always knows its firmware version). The hub learns the wheel from
// any wheel packet (hello or buttons), so either side can restart and the
// two find each other again within a fraction of a second. Any ESP-NOW
// device sending this magic and version is accepted for now.
//
// Dash data: the hub forwards the frames Volgas sends it over USB unchanged
// (the esp32_dash_protocol.h frames) in LINK_DASH packets; the wheel feeds
// them into the same frame parser it uses for its own USB port.
//
// Buttons: the wheel sends its state on every change and repeats it every
// LINK_BUTTON_REPEAT_MS, so a lost packet corrects itself; the hub releases
// every button if the wheel goes quiet for LINK_WHEEL_TIMEOUT_MS.

#define LINK_MAGIC0  'V'
#define LINK_MAGIC1  'L'
#define LINK_VERSION 1
#define LINK_CHANNEL 1   // fixed Wi-Fi channel both ends use

#define LINK_HELLO      1   // wheel -> broadcast, until a hub answers
#define LINK_HELLO_ACK  2   // hub -> wheel
#define LINK_BUTTONS    3   // wheel -> hub
#define LINK_DASH       4   // hub -> wheel, raw Volgas dash protocol bytes
#define LINK_HEARTBEAT  5   // hub -> wheel, when there's no dash data to send
#define LINK_OTA_BEGIN  6   // hub -> wheel: firmware size and MD5
#define LINK_OTA_DATA   7   // hub -> wheel: one chunk at an offset
#define LINK_OTA_END    8   // hub -> wheel: check the image and switch to it
#define LINK_OTA_ABORT  9   // hub -> wheel
#define LINK_OTA_ACK   10   // wheel -> hub: status + the next offset it wants

// Wireless firmware update: the PC drives it through the hub one chunk at a
// time (pc_app/volgas_wheel_ota.py). The wheel answers every OTA packet with
// LINK_OTA_ACK carrying the offset it expects next, so a lost chunk or a
// lost answer is simply sent again. The new firmware boots on probation and
// is kept only once it links to the hub again (otherwise the wheel goes back
// to the previous one) - see esp32_dash.ino's OTA section.
#define LINK_OTA_CHUNK 200
#define OTA_OK            0
#define OTA_DRIVING       1   // refused: the car is moving
#define OTA_BEGIN_FAILED  2   // e.g. the image doesn't fit
#define OTA_WRITE_FAILED  3
#define OTA_VERIFY_FAILED 4   // MD5 mismatch or not a valid image
#define OTA_NOT_STARTED   5   // data or end without a begin
#define OTA_DONE          6   // verified; the wheel restarts into it

#define LINK_DEVICE_WHEEL 1
#define LINK_CAP_DISPLAY  0x01

#define LINK_HELLO_MS          500
#define LINK_HELLO_LINKED_MS  5000
#define LINK_HEARTBEAT_MS      200
#define LINK_BUTTON_REPEAT_MS   50
#define LINK_WHEEL_TIMEOUT_MS  250    // hub: wheel silent this long -> release all buttons
#define LINK_HUB_TIMEOUT_MS   1000    // wheel: hub silent this long -> link lost, hello again
#define LINK_DASH_MAX          200    // dash bytes per packet (ESP-NOW allows 250 in total)

struct __attribute__((packed)) LinkHeader {
  uint8_t magic0, magic1, version, type;
};

struct __attribute__((packed)) LinkHello {
  LinkHeader h;
  uint8_t  deviceType;    // LINK_DEVICE_*
  uint8_t  modelId;
  uint16_t fwVersion;
  uint8_t  buttonCount;
  uint8_t  caps;          // LINK_CAP_*
};

struct __attribute__((packed)) LinkButtons {
  LinkHeader h;
  uint8_t  seq;
  uint32_t buttons;       // bit N = button N+1, 1 = pressed
};

struct __attribute__((packed)) LinkDash {
  LinkHeader h;
  uint8_t len;
  uint8_t data[LINK_DASH_MAX];
};

struct __attribute__((packed)) LinkOtaBegin {
  LinkHeader h;
  uint32_t size;
  char md5[32];           // lowercase hex
};

struct __attribute__((packed)) LinkOtaData {
  LinkHeader h;
  uint32_t offset;
  uint8_t len;
  uint8_t data[LINK_OTA_CHUNK];
};

struct __attribute__((packed)) LinkOtaAck {
  LinkHeader h;
  uint8_t status;         // OTA_*
  uint32_t next;          // offset the wheel wants next
};

inline LinkHeader linkHeader(uint8_t type) {
  LinkHeader h = {LINK_MAGIC0, LINK_MAGIC1, LINK_VERSION, type};
  return h;
}

inline bool linkValid(const uint8_t* data, int len) {
  return len >= (int)sizeof(LinkHeader) && data[0] == LINK_MAGIC0 && data[1] == LINK_MAGIC1 &&
         data[2] == LINK_VERSION;
}
