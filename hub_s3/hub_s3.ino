#include <WiFi.h>
#include <esp_now.h>
#include <esp_wifi.h>
#include "USB.h"
#include "USBCDC.h"
#include "USBHIDGamepad.h"
#include "volgas_link.h"

// ==========================================
//   ESP32-S3 HUB (N16R8 DevKitC-1, 2026-11-13)
// ==========================================
// The rig's USB device on the PC side (see CLAUDE.md "Planned architecture:
// one-device hub"). This first version does the wireless half:
//   - Volgas talks to it over USB serial exactly as it talked to the dash
//     (same VID:PID, same esp32_dash_protocol.h frames); each complete
//     frame is forwarded to the wheel over ESP-NOW (volgas_link.h).
//   - The wheel's buttons arrive over ESP-NOW and go to the PC as this
//     board's USB game controller. If the wheel goes quiet, every button is
//     released.
//   - Wireless wheel updates: the PC sends update commands over the same USB
//     serial (types 0x40+ below, same framing as the dash frames); the hub
//     turns each into one LINK_OTA_* packet and passes the wheel's answer
//     back. The PC (pc_app/volgas_wheel_ota.py) drives the transfer and the
//     retries; the hub keeps no update state.
// Pedals (UART), FFB (CAN to the ODESC) and the 64-button wheel descriptor
// come later.
//
// USB: use the board's native USB-C port (labelled USB / OTG), not the one
// on the USB-to-serial chip (COM / UART). CDC is composited explicitly, the
// same way and for the same reasons as esp32_dash.ino - keep CDCOnBoot at
// its default (Disabled). Debug messages go to Serial (UART0), which on
// this board is the other USB-C port: Volgas holds the native port and
// never reads it, so printing there could stall the loop.

USBHIDGamepad Gamepad;
USBCDC USBSerial;

const uint8_t BROADCAST[6] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};

struct RxPacket {
  uint8_t mac[6];
  int8_t rssi;
  uint8_t len;
  uint8_t data[250];
};
QueueHandle_t rxQueue;

// USB serial framing, shared with Volgas: AA 55 type len payload checksum
// (8-bit sum of type, len and payload) - see esp32_dash_protocol.h. Types
// below 0x40 are dash data for the wheel; these are for the hub itself.
#define USB_SYNC0 0xAA
#define USB_SYNC1 0x55
#define USB_OTA_BEGIN 0x40   // PC -> hub: uint32 size, char md5[32]
#define USB_OTA_DATA  0x41   // PC -> hub: uint32 offset, data
#define USB_OTA_END   0x42   // PC -> hub
#define USB_OTA_ABORT 0x43   // PC -> hub
#define USB_WHEEL_INFO 0x44  // PC -> hub: asks for USB_WHEEL_STATUS
#define USB_OTA_ACK   0x50   // hub -> PC: uint8 status, uint32 next (the wheel's LINK_OTA_ACK)
#define USB_WHEEL_STATUS 0x51  // hub -> PC: uint8 linked, uint8 model, uint16 fw, uint8 buttons, int8 rssi

uint8_t wheelMac[6];
bool wheelKnown = false;
LinkHello lastHello = {};
bool helloSeen = false;
unsigned long lastWheelMs = 0;
uint32_t wheelButtons = 0;
int8_t lastRssi = 0;
uint32_t packetsIn = 0, packetsOut = 0, sendErrors = 0;

// Runs in the Wi-Fi task: only copy the packet out; loop() does the work.
void onReceive(const esp_now_recv_info_t* info, const uint8_t* data, int len) {
  if (!linkValid(data, len) || len > 250) return;
  RxPacket p;
  memcpy(p.mac, info->src_addr, 6);
  p.rssi = info->rx_ctrl ? info->rx_ctrl->rssi : 0;
  p.len = len;
  memcpy(p.data, data, len);
  xQueueSend(rxQueue, &p, 0);
}

void ensurePeer(const uint8_t* mac) {
  if (esp_now_is_peer_exist(mac)) return;
  esp_now_peer_info_t peer = {};
  memcpy(peer.peer_addr, mac, 6);
  peer.channel = LINK_CHANNEL;
  peer.ifidx = WIFI_IF_STA;
  peer.encrypt = false;
  esp_now_add_peer(&peer);
}

void sendToWheel(const void* data, size_t len) {
  if (!wheelKnown) return;
  if (esp_now_send(wheelMac, (const uint8_t*)data, len) == ESP_OK) packetsOut++;
  else sendErrors++;
}

void learnWheel(const uint8_t* mac) {
  if (!wheelKnown || memcmp(mac, wheelMac, 6) != 0) {
    memcpy(wheelMac, mac, 6);
    wheelKnown = true;
    ensurePeer(wheelMac);
    Serial.printf("wheel %02X:%02X:%02X:%02X:%02X:%02X\n", mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
  }
  lastWheelMs = millis();
}

void usbSendFrame(uint8_t type, const uint8_t* payload, uint8_t len) {
  uint8_t frame[4 + 255 + 1];
  frame[0] = USB_SYNC0;
  frame[1] = USB_SYNC1;
  frame[2] = type;
  frame[3] = len;
  uint8_t sum = type + len;
  for (uint8_t i = 0; i < len; i++) {
    frame[4 + i] = payload[i];
    sum += payload[i];
  }
  frame[4 + len] = sum;
  USBSerial.write(frame, 5 + len);
}

void handlePacket(const RxPacket& p) {
  packetsIn++;
  lastRssi = p.rssi;
  const LinkHeader* h = (const LinkHeader*)p.data;
  if (h->type == LINK_HELLO && p.len >= sizeof(LinkHello)) {
    learnWheel(p.mac);
    memcpy(&lastHello, p.data, sizeof(LinkHello));
    helloSeen = true;
    LinkHeader ack = linkHeader(LINK_HELLO_ACK);
    sendToWheel(&ack, sizeof(ack));
  } else if (h->type == LINK_BUTTONS && p.len >= sizeof(LinkButtons)) {
    learnWheel(p.mac);
    wheelButtons = ((const LinkButtons*)p.data)->buttons;
  } else if (h->type == LINK_OTA_ACK && p.len >= sizeof(LinkOtaAck)) {
    learnWheel(p.mac);
    const LinkOtaAck* a = (const LinkOtaAck*)p.data;
    uint8_t payload[5];
    payload[0] = a->status;
    memcpy(payload + 1, &a->next, 4);
    usbSendFrame(USB_OTA_ACK, payload, sizeof(payload));
  }
}

// ---------- USB serial from the PC ----------
// Complete dash frames collect here and go to the wheel once per loop.
LinkDash pendingDash;

void flushDash() {
  if (pendingDash.len == 0) return;
  pendingDash.h = linkHeader(LINK_DASH);
  sendToWheel(&pendingDash, sizeof(LinkHeader) + 1 + pendingDash.len);
  pendingDash.len = 0;
}

void handleUsbFrame(uint8_t type, const uint8_t* payload, uint8_t len) {
  if (type < 0x40) {
    if (pendingDash.len + 5 + len > LINK_DASH_MAX) flushDash();
    uint8_t* out = pendingDash.data + pendingDash.len;
    out[0] = USB_SYNC0;
    out[1] = USB_SYNC1;
    out[2] = type;
    out[3] = len;
    uint8_t sum = type + len;
    for (uint8_t i = 0; i < len; i++) {
      out[4 + i] = payload[i];
      sum += payload[i];
    }
    out[4 + len] = sum;
    pendingDash.len += 5 + len;
  } else if (type == USB_OTA_BEGIN && len == 36) {
    LinkOtaBegin b;
    b.h = linkHeader(LINK_OTA_BEGIN);
    memcpy(&b.size, payload, 4);
    memcpy(b.md5, payload + 4, 32);
    sendToWheel(&b, sizeof(b));
  } else if (type == USB_OTA_DATA && len > 4 && len - 4 <= LINK_OTA_CHUNK) {
    LinkOtaData d;
    d.h = linkHeader(LINK_OTA_DATA);
    memcpy(&d.offset, payload, 4);
    d.len = len - 4;
    memcpy(d.data, payload + 4, d.len);
    sendToWheel(&d, sizeof(LinkHeader) + 5 + d.len);
  } else if (type == USB_OTA_END || type == USB_OTA_ABORT) {
    LinkHeader h = linkHeader(type == USB_OTA_END ? LINK_OTA_END : LINK_OTA_ABORT);
    sendToWheel(&h, sizeof(h));
  } else if (type == USB_WHEEL_INFO) {
    bool linked = wheelKnown && millis() - lastWheelMs < LINK_WHEEL_TIMEOUT_MS;
    uint8_t s[6] = {(uint8_t)linked, helloSeen ? lastHello.modelId : (uint8_t)0, 0, 0,
                    helloSeen ? lastHello.buttonCount : (uint8_t)0, (uint8_t)lastRssi};
    uint16_t fw = helloSeen ? lastHello.fwVersion : 0;
    memcpy(s + 2, &fw, 2);
    usbSendFrame(USB_WHEEL_STATUS, s, sizeof(s));
  }
}

void pollUsb() {
  static enum { WAIT_SYNC0, WAIT_SYNC1, READ_TYPE, READ_LEN, READ_PAYLOAD, READ_CHECKSUM } state = WAIT_SYNC0;
  static uint8_t type, len, idx, sum;
  static uint8_t payload[255];
  static unsigned long lastByteMs = 0;
  if (state != WAIT_SYNC0 && millis() - lastByteMs > 100) state = WAIT_SYNC0;
  while (USBSerial.available()) {
    uint8_t b = USBSerial.read();
    lastByteMs = millis();
    switch (state) {
      case WAIT_SYNC0: if (b == USB_SYNC0) state = WAIT_SYNC1; break;
      case WAIT_SYNC1: state = (b == USB_SYNC1) ? READ_TYPE : WAIT_SYNC0; break;
      case READ_TYPE: type = b; sum = b; state = READ_LEN; break;
      case READ_LEN:
        len = b; sum += b; idx = 0;
        state = len == 0 ? READ_CHECKSUM : READ_PAYLOAD;
        break;
      case READ_PAYLOAD:
        payload[idx++] = b; sum += b;
        if (idx >= len) state = READ_CHECKSUM;
        break;
      case READ_CHECKSUM:
        if (b == sum) handleUsbFrame(type, payload, len);
        state = WAIT_SYNC0;
        break;
    }
  }
  flushDash();
}

void setup() {
  Serial.begin(115200);
  rxQueue = xQueueCreate(16, sizeof(RxPacket));

  WiFi.mode(WIFI_STA);
  WiFi.disconnect();
  esp_wifi_set_channel(LINK_CHANNEL, WIFI_SECOND_CHAN_NONE);
  if (esp_now_init() != ESP_OK) Serial.println("ESP-NOW failed to start");
  esp_now_register_recv_cb(onReceive);
  Serial.printf("hub %s on channel %d\n", WiFi.macAddress().c_str(), LINK_CHANNEL);

  USB.manufacturerName("Sim Rig");
  USB.productName("Volgas Hub");
  Gamepad.begin();
  USBSerial.begin(115200);
  USBSerial.enableReboot(true);   // 1200 bps touch -> bootloader, for arduino-cli UploadMode=cdc
  USB.begin();
}

void loop() {
  unsigned long now = millis();

  RxPacket p;
  while (xQueueReceive(rxQueue, &p, 0) == pdTRUE) handlePacket(p);

  bool wheelLinked = wheelKnown && now - lastWheelMs < LINK_WHEEL_TIMEOUT_MS;
  if (!wheelLinked) wheelButtons = 0;   // never leave a button held on a lost link

  static uint32_t sentButtons = 0xFFFFFFFF;
  if (wheelButtons != sentButtons) {
    Gamepad.send(0, 0, 0, 0, 0, 0, HAT_CENTER, wheelButtons);
    sentButtons = wheelButtons;
  }

  // Volgas -> hub: dash frames go on to the wheel, update commands are relayed.
  uint32_t outBefore = packetsOut;
  pollUsb();
  static unsigned long lastToWheelMs = 0;
  if (packetsOut != outBefore) lastToWheelMs = now;
  if (wheelKnown && now - lastToWheelMs >= LINK_HEARTBEAT_MS) {
    LinkHeader hb = linkHeader(LINK_HEARTBEAT);
    sendToWheel(&hb, sizeof(hb));
    lastToWheelMs = now;
  }

  static unsigned long lastReportMs = 0;
  if (now - lastReportMs >= 2000) {
    Serial.printf("wheel %s  rssi %d dBm  in %lu  out %lu  send errors %lu  buttons %08lX\n",
                  wheelLinked ? "linked" : "lost", lastRssi, (unsigned long)packetsIn, (unsigned long)packetsOut,
                  (unsigned long)sendErrors, (unsigned long)wheelButtons);
    packetsIn = packetsOut = sendErrors = 0;
    lastReportMs = now;
  }

  delay(1);
}
