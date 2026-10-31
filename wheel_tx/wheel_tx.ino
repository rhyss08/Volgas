#include <SPI.h>
#include <RF24.h>

// ==========================================
//   WHEEL TRANSMITTER (Arduino Nano)
//   12 buttons (4x3 matrix) -> nRF24L01 -> ESP32-S3 dash
// ==========================================
// The temporary ST7735 display wiring that used to live on this board
// (2026-07-15) has moved to the ESP32-S3 dash now that it's arrived - see
// esp32_dash/esp32_dash.ino. This board is back to being a pure wireless
// button transmitter, same role it always had.
//
// Receiver also moved: this used to transmit to the pedals' Blue Pill,
// which merged buttons into its own HID report. Now it transmits to the
// ESP32-S3, which reports the buttons as its own separate USB HID device.
// Same "WHEEL" pipe address and packet format either way - only the
// receiver changed.

// nRF24L01 wiring (Nano):
//   VCC  -> 3.3V   (NOT 5V - kills the module!)
//   GND  -> GND
//   CE   -> D9
//   CSN  -> D10
//   SCK  -> D13
//   MOSI -> D11
//   MISO -> D12
// Solder/hold a 10-100uF cap across VCC/GND at the module - the Nano's
// 3.3V rail is weak and the nRF24 browns out without it.
#define NRF_CE   9
#define NRF_CSN  10

// Button matrix: 4 rows x 3 cols = all 12 buttons on 7 pins. Rows are
// driven OUTPUT (idle HIGH, one pulled LOW per scan step); cols are
// INPUT_PULLUP (LOW = that row/col intersection is pressed).
//   ROW1..4 -> D2, D3, D4, D5
//   COL1..3 -> D6, D7, D8
// bitIndex = row * 3 + col, so bits 0-11 map onto HID gamepad buttons
// 1-12 (0,1 = paddle positions; layout can be rearranged freely as long
// as bit numbering here matches wiring).
//
// NOT WIRED WITH DIODES. Without a diode per switch, pressing 3 buttons
// that form an "L" across 2 rows/2 cols simultaneously can "ghost" a 4th,
// unpressed button as pressed. Fine for single presses and most
// paddle+button combos; if a false-positive button ever shows up during
// a real 3+ button combo, that's ghosting - fix is a 1N4148 diode in
// series with each switch (cathode toward the column).
const uint8_t MATRIX_ROWS = 4;
const uint8_t MATRIX_COLS = 3;
const uint8_t rowPins[MATRIX_ROWS] = {2, 3, 4, 5};
const uint8_t colPins[MATRIX_COLS] = {6, 7, 8};

RF24 radio(NRF_CE, NRF_CSN);
const byte wheelAddress[6] = "WHEEL";

bool radioOk = false;
uint16_t lastState = 0xFFFF;   // force initial send
unsigned long lastSend = 0;
unsigned long lastReport = 0;
unsigned long sendOk = 0, sendFail = 0;

uint16_t readButtons() {
  uint16_t state = 0;
  for (uint8_t r = 0; r < MATRIX_ROWS; r++) {
    for (uint8_t rr = 0; rr < MATRIX_ROWS; rr++) {
      digitalWrite(rowPins[rr], rr == r ? LOW : HIGH);
    }
    delayMicroseconds(20);   // let the driven line settle before reading
    for (uint8_t c = 0; c < MATRIX_COLS; c++) {
      if (digitalRead(colPins[c]) == LOW) {   // pressed
        state |= (1UL << (r * MATRIX_COLS + c));
      }
    }
  }
  for (uint8_t rr = 0; rr < MATRIX_ROWS; rr++) digitalWrite(rowPins[rr], HIGH);  // idle
  return state;
}

void setup() {
  Serial.begin(115200);

  for (uint8_t r = 0; r < MATRIX_ROWS; r++) {
    pinMode(rowPins[r], OUTPUT);
    digitalWrite(rowPins[r], HIGH);   // idle
  }
  for (uint8_t c = 0; c < MATRIX_COLS; c++) {
    pinMode(colPins[c], INPUT_PULLUP);
  }

  radioOk = radio.begin();
  if (radioOk) {
    radio.setPALevel(RF24_PA_LOW);      // plenty for wheel->rig distance
    radio.setDataRate(RF24_250KBPS);    // slowest = most reliable link
    radio.setRetries(3, 5);             // auto-retry: 5 tries, 1ms apart
    radio.openWritingPipe(wheelAddress);
    radio.stopListening();              // transmit-only
    Serial.println("RADIO OK - transmitting");
  } else {
    Serial.println("RADIO DEAD - no SPI response from nRF24");
  }
}

void loop() {
  if (!radioOk) {
    Serial.println("RADIO DEAD");
    delay(1000);
    return;
  }

  uint16_t state = readButtons();
  unsigned long now = millis();

  // Send immediately on any change; otherwise heartbeat every 50ms so the
  // receiver can detect a dead link and release stuck buttons.
  if (state != lastState || now - lastSend >= 50) {
    // write() returns true when the receiver ACKed the packet
    if (radio.write(&state, sizeof(state))) sendOk++; else sendFail++;
    lastState = state;
    lastSend = now;
  }

  // Once a second: link quality + current button state
  if (now - lastReport >= 1000) {
    Serial.print("buttons=");
    Serial.print(state, BIN);
    Serial.print(" acked=");
    Serial.print(sendOk);
    Serial.print(" failed=");
    Serial.println(sendFail);
    sendOk = sendFail = 0;
    lastReport = now;
  }

  delay(2);   // ~500Hz scan - far faster than any human shift
}
