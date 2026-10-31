#include <HX711.h>
#include <USBComposite.h>
#include <EEPROM.h>
#include "pedal_protocol.h"

// ==========================================
//   PIN ASSIGNMENTS
// ==========================================
#define HALL_PIN    PA0   // throttle position, analog (ADC1)
#define HX711_DT    PB6   // brake load cell amp, data
#define HX711_SCK   PB7   // brake load cell amp, clock

// Debug output over USART1 (same pins as the CP2102 used for flashing:
// PA9=TX, PA10=RX). Fine to reuse at runtime - USB HID and the UART
// are independent peripherals.
#define DEBUG_SERIAL  Serial1

// ==========================================
//   CALIBRATION
// ==========================================
// Defaults used when flash holds no saved calibration (fresh chip, or
// magic/version mismatch). Values as of the 2026-07-14 calibration:
// throttle rest ~2050 / full ~2580; brake reads negative after final
// mounting, sustained press ~-350k, stabs to -590k.
//
// throttleCurve approximates the old gamma=0.6 default (counteracts the
// hall sensor's steepening response as the magnet nears the sensor - end
// of travel far more sensitive than the start) as 5 output-percentage
// points sampled at 0/25/50/75/100% input. brakeCurve is linear (the old
// gamma=1.0 default). Both adjustable from the PC app's Pedals tab.
CalibrationData cal = { 2180, 3000, -2000, -350000L, {0, 44, 66, 84, 100}, {0, 25, 50, 75, 100} };

// EMA factor - lower = smoother/slower. Bumped 0.2->0.45 (2026-08, "throttle
// feels slow to respond"): this is a per-LOOP-ITERATION decay factor, not a
// time constant, so it only translates to a fixed real-world lag combined
// with a fixed loop rate. The loop rate itself was the bigger hidden cost
// here (see the DEBUG_SERIAL/delay(5) changes below) - this bump is a
// secondary, smaller tightening on top of that.
const float HALL_SMOOTHING = 0.45;

// Pre-filtering stage, ahead of the EMA above. The EMA alone lets a single
// noisy/glitched ADC sample drag the output away for several loop iterations
// before it decays back out; these two catch it before it ever reaches the EMA.
const uint8_t HALL_OVERSAMPLE    = 8;  // ADC reads averaged per loop iteration
// Trimmed 5->3 (2026-08, alongside HALL_SMOOTHING above) - a median filter's
// own inherent group delay is ~(windowSize-1)/2 samples, so this trades a
// little of the 5-window's glitch rejection margin for less built-in lag.
// Still rejects a single-sample spike (needs 2 of 3 to agree).
const uint8_t HALL_MEDIAN_WINDOW = 3;

// Calibration/live-monitor link runs over USB CDC serial on the same
// cable as the joystick (composite device). STATUS packets go out at
// 50Hz, only while the PC app actually has the port open (DTR up) -
// writing into an unopened CDC port would fill the buffer and stall.
const unsigned long STATUS_INTERVAL_MS = 20;

USBHID HID;
USBCompositeSerial CompositeSerial;
HIDJoystick Joystick(HID);
HX711 scale;

float hallFiltered = 0;
long loadRaw = 0;   // persists between HX711 samples (~10Hz) so the brake holds its value
unsigned long lastStatusSent = 0;

// ==========================================
//   FLASH PERSISTENCE (STM32 EEPROM emulation)
// ==========================================
// Layout in emulated EEPROM (16-bit words): [0]=magic, [1]=version,
// [2..]=CalibrationData bytes packed two per word.
const uint16 CAL_MAGIC = 0xCA1B;
const int CAL_WORDS = (sizeof(CalibrationData) + 1) / 2;

// Custom EEPROM page placement (2026-07-25) - the bundled EEPROM library
// (Arduino_STM32/STM32F1/libraries/EEPROM/EEPROM.h) hardcodes
// `#define MCU_STM32F103RB`, assuming a 128KB-flash chip when computing its
// default page addresses, regardless of which variant is actually selected
// in the IDE. If this board's real chip is a genuine 64KB F103C8 (not one
// of the common mislabeled-as-64KB-but-actually-128KB clones), that default
// address falls outside the chip's real flash - every write silently fails,
// EEPROM.init() finds no valid page on the next boot, and loadCalibration()
// always fails its magic-number check, silently reverting to the compiled
// `cal` default below. This matched an observed symptom exactly (saved
// calibration "doesn't save, reverts back to the code's values").
// Fix: place the two EEPROM pages explicitly in the last 2KB of a
// conservative 64KB assumption instead of trusting the library's
// 128KB-based default - safe regardless of the chip's true size, and
// confirmed nowhere near the program's own flash usage by reading this
// board's actual linker script (generic_stm32f103c/ld/flash_c8.ld: program
// is confined to the first 44KB).
#define PEDAL_EEPROM_PAGE_SIZE  ((uint16)0x400)                                          // 1KB - correct for all F103 medium-density parts (64-128KB), this wasn't the bug
#define PEDAL_EEPROM_PAGE0      ((uint32)(0x08000000 + 64 * 1024 - 2 * PEDAL_EEPROM_PAGE_SIZE))
#define PEDAL_EEPROM_PAGE1      ((uint32)(PEDAL_EEPROM_PAGE0 + PEDAL_EEPROM_PAGE_SIZE))

bool loadCalibration() {
  if (EEPROM.read(0) != CAL_MAGIC) return false;
  if (EEPROM.read(1) != PEDAL_PROTOCOL_VERSION) return false;
  uint16 words[CAL_WORDS];
  for (int i = 0; i < CAL_WORDS; i++) words[i] = EEPROM.read(2 + i);
  memcpy(&cal, words, sizeof(cal));
  return true;
}

bool saveCalibration() {
  uint16 words[CAL_WORDS];
  memcpy(words, &cal, sizeof(cal));
  if (EEPROM.write(0, CAL_MAGIC) != EEPROM_OK) return false;
  if (EEPROM.write(1, PEDAL_PROTOCOL_VERSION) != EEPROM_OK) return false;
  for (int i = 0; i < CAL_WORDS; i++) {
    if (EEPROM.write(2 + i, words[i]) != EEPROM_OK) return false;
  }
  return true;
}

// Curve points are already uint8_t (0-255 wire range), just clamp to the
// meaningful 0-100 percentage range in case a buggy/garbled command sent
// something past it.
void sanitizeCalibration() {
  for (uint8_t i = 0; i < 5; i++) {
    if (cal.throttleCurve[i] > 100) cal.throttleCurve[i] = 100;
    if (cal.brakeCurve[i]    > 100) cal.brakeCurve[i]    = 100;
  }
}

// ==========================================
//   FRAME I/O (see pedal_protocol.h for the format)
// ==========================================
void sendFrame(uint8_t type, const void* payload, uint8_t len) {
  if (!CompositeSerial.isConnected()) return;
  const uint8_t* p = (const uint8_t*)payload;
  uint8_t checksum = type + len;
  CompositeSerial.write(PEDAL_SYNC0);
  CompositeSerial.write(PEDAL_SYNC1);
  CompositeSerial.write(type);
  CompositeSerial.write(len);
  for (uint8_t i = 0; i < len; i++) {
    CompositeSerial.write(p[i]);
    checksum += p[i];
  }
  CompositeSerial.write(checksum);
}

void handleFrame(uint8_t type, const uint8_t* payload, uint8_t len) {
  switch (type) {
    case MSG_SET_CALIBRATION:
      if (len == sizeof(CalibrationData)) {
        memcpy(&cal, payload, sizeof(cal));
        sanitizeCalibration();
        sendFrame(MSG_CALIBRATION, &cal, sizeof(cal));  // echo back what's now in effect
        DEBUG_SERIAL.println("calibration updated (RAM)");
      }
      break;
    case MSG_GET_CALIBRATION:
      sendFrame(MSG_CALIBRATION, &cal, sizeof(cal));
      break;
    case MSG_SAVE: {
      uint8_t ok = saveCalibration() ? 1 : 0;
      sendFrame(MSG_SAVE_ACK, &ok, 1);
      DEBUG_SERIAL.println(ok ? "calibration saved to flash" : "flash save FAILED");
      break;
    }
  }
}

// Explicit resync state machine, same hang-proofing idiom as elsewhere in
// this codebase: any bad byte drops back to WAIT_SYNC0 (which re-scans
// every byte for the sync pair), and 100ms of mid-frame silence resets it
// so a truncated frame can never leave the parser stuck.
void pollCommands() {
  static enum { WAIT_SYNC0, WAIT_SYNC1, READ_TYPE, READ_LEN, READ_PAYLOAD, READ_CHECKSUM } state = WAIT_SYNC0;
  static uint8_t msgType, msgLen, payloadIdx, checksum;
  static uint8_t payload[PEDAL_MAX_PAYLOAD];
  static unsigned long lastByteMs = 0;

  if (state != WAIT_SYNC0 && millis() - lastByteMs > 100) state = WAIT_SYNC0;

  while (CompositeSerial.available()) {
    uint8_t b = CompositeSerial.read();
    lastByteMs = millis();
    switch (state) {
      case WAIT_SYNC0:
        if (b == PEDAL_SYNC0) state = WAIT_SYNC1;
        break;
      case WAIT_SYNC1:
        state = (b == PEDAL_SYNC1) ? READ_TYPE : WAIT_SYNC0;
        break;
      case READ_TYPE:
        msgType = b;
        checksum = b;
        state = READ_LEN;
        break;
      case READ_LEN:
        if (b > PEDAL_MAX_PAYLOAD) { state = WAIT_SYNC0; break; }
        msgLen = b;
        checksum += b;
        payloadIdx = 0;
        state = (msgLen == 0) ? READ_CHECKSUM : READ_PAYLOAD;
        break;
      case READ_PAYLOAD:
        payload[payloadIdx++] = b;
        checksum += b;
        if (payloadIdx >= msgLen) state = READ_CHECKSUM;
        break;
      case READ_CHECKSUM:
        if (b == checksum) handleFrame(msgType, payload, msgLen);
        state = WAIT_SYNC0;
        break;
    }
  }
}

long constrainMap(long x, long inMin, long inMax, long outMin, long outMax) {
  long lo = min(inMin, inMax);
  long hi = max(inMin, inMax);
  x = constrain(x, lo, hi);
  return map(x, inMin, inMax, outMin, outMax);
}

// Piecewise-linear interpolation through the 5 curve points (breakpoints
// at 0/25/50/75/100% of the 0-1023 input range). Integer-only - cheap
// enough to run every loop iteration with no pow()/float involved.
int applyCurve(int linear, const uint8_t* curve) {
  linear = constrain(linear, 0, 1023);
  long scaled = (long)linear * 4;            // 0..4092 - 4 segments across 5 points
  int seg = scaled / 1023;
  if (seg > 3) seg = 3;                      // linear==1023 lands exactly on the last point
  long segFrac = scaled - (long)seg * 1023;  // 0..1023 position within this segment
  int y0 = curve[seg];
  int y1 = curve[seg + 1];
  long y = y0 + ((long)(y1 - y0) * segFrac) / 1023;  // 0..100
  return (int)(y * 1023 / 100);
}

int readHallOversampled() {
  long sum = 0;
  for (uint8_t i = 0; i < HALL_OVERSAMPLE; i++) sum += analogRead(HALL_PIN);
  return sum / HALL_OVERSAMPLE;
}

// Generic odd-window median via insertion sort - cheap at these sizes (<=9ish).
int pushMedian(int sample, int* history, uint8_t windowSize, uint8_t* idx, uint8_t* count) {
  history[*idx] = sample;
  *idx = (*idx + 1) % windowSize;
  if (*count < windowSize) (*count)++;
  if (*count < windowSize) return sample;  // still filling on boot, pass through
  int v[9];  // HALL_MEDIAN_WINDOW is currently 3; sized generously
  memcpy(v, history, windowSize * sizeof(int));
  for (uint8_t i = 1; i < windowSize; i++) {
    int key = v[i];
    int j = i;
    while (j > 0 && v[j - 1] > key) { v[j] = v[j - 1]; j--; }
    v[j] = key;
  }
  return v[windowSize / 2];
}

void blinkLED(int times, int ms) {
  for (int i = 0; i < times; i++) {
    digitalWrite(PC13, LOW);   // Blue Pill onboard LED is active-low
    delay(ms);
    digitalWrite(PC13, HIGH);
    delay(ms);
  }
}

void setup() {
  pinMode(PC13, OUTPUT);
  digitalWrite(PC13, HIGH);   // off

  DEBUG_SERIAL.begin(115200);
  blinkLED(2, 400);   // reached here: pins/serial init OK, about to start USB

  EEPROM.init(PEDAL_EEPROM_PAGE0, PEDAL_EEPROM_PAGE1, PEDAL_EEPROM_PAGE_SIZE);
  if (loadCalibration()) {
    sanitizeCalibration();
    DEBUG_SERIAL.println("BOOT: calibration loaded from flash");
  } else {
    DEBUG_SERIAL.println("BOOT: no saved calibration, using defaults");
  }

  // PID bumped 0x0002 -> 0x0003 when CDC serial was added to the
  // descriptor: Windows caches descriptors per VID/PID, and re-plugging
  // a changed device under the old PID can leave a broken cached entry.
  // Games will see this as a new controller once and need a re-bind.
  USBComposite.setProductId(0x0003);
  USBComposite.setProductString("Pedal Set");
  HID.begin(CompositeSerial, HID_JOYSTICK);
  Joystick.setManualReportMode(true);

  blinkLED(6, 100);   // reached here: USB HID init returned, did not hang
  DEBUG_SERIAL.println("BOOT: usb init done (HID + CDC composite)");

  pinMode(HALL_PIN, INPUT_ANALOG);

  DEBUG_SERIAL.println("BOOT: starting HX711 init");
  scale.begin(HX711_DT, HX711_SCK);
  // Pull the data line up so a disconnected HX711 reads as "not ready"
  // (floating pin can fake "ready" and hang the blocking reads below).
  // A connected HX711 drives this line actively, so the pullup is harmless.
  pinMode(HX711_DT, INPUT_PULLUP);

  // Sample DOUT for 1s: a healthy converting HX711 toggles it at ~10Hz,
  // so we expect a mix of lows (ready) and highs (converting).
  int lows = 0, highs = 0;
  for (int i = 0; i < 100; i++) {
    if (digitalRead(HX711_DT) == LOW) lows++; else highs++;
    delay(10);
  }
  DEBUG_SERIAL.print("BOOT: DOUT samples low=");
  DEBUG_SERIAL.print(lows);
  DEBUG_SERIAL.print(" high=");
  DEBUG_SERIAL.println(highs);

  // Tare manually with a bounded wait per read - scale.tare() can block
  // forever if the chip stops converting mid-sequence.
  long tareSum = 0;
  int tareReads = 0;
  for (int i = 0; i < 10; i++) {
    if (scale.wait_ready_timeout(250)) {
      tareSum += scale.read();
      tareReads++;
    }
  }
  if (tareReads > 0) {
    scale.set_offset(tareSum / tareReads);
    DEBUG_SERIAL.print("BOOT: tare ok from ");
    DEBUG_SERIAL.print(tareReads);
    DEBUG_SERIAL.println(" reads");
  } else {
    DEBUG_SERIAL.println("HX711 not responding (check wiring) - skipping tare");
  }

  hallFiltered = analogRead(HALL_PIN);
}

void loop() {
  int hallRaw = analogRead(HALL_PIN);          // single sample, kept only for DEBUG_SERIAL/STATUS transparency
  int hallOversampled = readHallOversampled(); // this iteration's noise-reduced sample
  static int hallHistory[HALL_MEDIAN_WINDOW];
  static uint8_t hallHistIdx = 0, hallHistCount = 0;
  int hallMedian = pushMedian(hallOversampled, hallHistory, HALL_MEDIAN_WINDOW, &hallHistIdx, &hallHistCount);
  hallFiltered += (hallMedian - hallFiltered) * HALL_SMOOTHING;

  if (scale.is_ready()) {
    loadRaw = scale.get_units(1);   // update only when a fresh sample exists
  }

  int throttleLinear = constrainMap((long)hallFiltered, cal.hallMin, cal.hallMax, 0, 1023);
  int throttleAxis   = applyCurve(throttleLinear, cal.throttleCurve);
  int brakeLinear    = constrainMap(loadRaw, cal.loadMin, cal.loadMax, 0, 1023);
  int brakeAxis      = applyCurve(brakeLinear, cal.brakeCurve);

  // send() blocks forever if no USB host is connected/enumerated,
  // so only report when the native USB link is actually up.
  if (USBComposite.isReady()) {
    Joystick.X(throttleAxis);
    Joystick.Y(brakeAxis);
    Joystick.send();
  }

  pollCommands();

  if (millis() - lastStatusSent >= STATUS_INTERVAL_MS) {
    lastStatusSent = millis();
    StatusPacket st;
    st.hallRaw = hallRaw;
    st.hallFiltered = (int16_t)hallFiltered;
    st.loadRaw = loadRaw;
    st.throttleAxis = throttleAxis;
    st.brakeAxis = brakeAxis;
    sendFrame(MSG_STATUS, &st, sizeof(st));
  }

  // Both of these used to run every single iteration and were the real
  // cause of "throttle feels slow to respond" (2026-08) - not the filter
  // constants above. A blocking UART print at 115200 baud costs a few ms
  // per call, and the flat delay(5) added another 5ms on top of that -
  // together capping the loop (and therefore Joystick.send(), i.e. how
  // often the game actually sees a fresh axis value) at ~100Hz. Since
  // HALL_SMOOTHING/HALL_MEDIAN_WINDOW above are per-ITERATION effects, not
  // per-time-unit ones, that low loop rate was silently multiplying
  // whatever filter lag they already added. Throttling the print to 1-in-20
  // iterations (still several times a second - plenty for a wired debug
  // monitor) and dropping the flat delay entirely lets the loop run as
  // fast as the actual work allows, which is the bigger fix of the two.
  static uint8_t debugDivider = 0;
  if (++debugDivider >= 20) {
    debugDivider = 0;
    DEBUG_SERIAL.print("hallRaw=");
    DEBUG_SERIAL.print(hallRaw);
    DEBUG_SERIAL.print(" loadRaw=");
    DEBUG_SERIAL.print(loadRaw);
    DEBUG_SERIAL.print(" -> throttle=");
    DEBUG_SERIAL.print(throttleAxis);
    DEBUG_SERIAL.print(" brake=");
    DEBUG_SERIAL.println(brakeAxis);
  }
}
