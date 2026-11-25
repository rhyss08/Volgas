#include <Adafruit_TinyUSB.h>
#include <HX711.h>
#include <IWatchdog.h>
#include "stm32f4xx_hal.h"
#include "pedal_protocol.h"

// ==========================================
//   PEDAL SET - STM32F411 "Black Pill" (2026-11-13)
// ==========================================
// The Blue Pill's pedal_set.ino ported to the Black Pill: same axes, filters,
// calibration struct and PC protocol (pedal_protocol.h, an identical copy -
// pc_app/tests checks), so Volgas doesn't change. What's different:
//   - USB is Adafruit TinyUSB (HID joystick + CDC serial as one composite
//     device); USBComposite only exists for the F1 family. The core has no
//     built-in TinyUSB support, so it's started by hand (TinyUSB_Device_Init)
//     and serviced from loop().
//   - Calibration lives in two flash sectors written by hand (see FLASH
//     PERSISTENCE), not the EEPROM library.
//   - 12-bit ADC is requested explicitly (this core defaults to 10 bits).
//
// Board: STMicroelectronics:stm32:GenF4:pnum=BLACKPILL_F411CE,usb=none,
// xserial=generic (no extra build flags). Flash over USB with STM32CubeProgrammer
// (DFU): open the pedals' serial port at 1200 baud and close it to restart
// into the bootloader (RESTART INTO THE ROM BOOTLOADER below), or hold BOOT0
// and tap NRST. Erase all first or the verify fails.
//
// Firmware 1.0 (2026-11-24) adds, on top of that port:
//   - an adaptive ("1 euro") filter for both pedals: heavy smoothing while a
//     pedal is still, almost none while it moves fast, instead of one fixed
//     smoothing that is a compromise between the two;
//   - the brake read at the HX711's 80 samples a second (the module's RATE pad
//     is tied high; at 10 SPS the same code just sees fewer samples);
//   - fault detection (throttle sensor at the ADC's rails, HX711 silent or
//     stuck at its maximum) that holds the affected axis at 0 and reports it;
//   - a hardware watchdog that restarts a frozen program;
//   - a firmware version (MSG_INFO) so Volgas can show it and update it.
//
// Planned: this board is wired to the ESP32-S3 hub over USART2 (PA2/PA3) and
// updated through it - not written yet.

// ==========================================
//   PIN ASSIGNMENTS
// ==========================================
#define HALL_PIN    PA1   // throttle position, analog (PA0 is the board's KEY button)
#define HX711_DT    PB6   // brake load cell amp, data
#define HX711_SCK   PB7   // brake load cell amp, clock
#define LED_PIN     PC13  // active-low

// Debug output on USART1 (PA10 = RX, PA9 = TX, the core's Serial1); nothing
// needs to be attached.
#define DEBUG_SERIAL Serial1

// Firmware version: major << 8 | minor, like the wheel's (0x0100 = 1.0). Bump it
// on every release - Volgas compares it with the published firmware.
#define PEDAL_FW_VERSION 0x0101

#define USB_VID 0x1EAF
#define USB_PID 0x0004   // the Blue Pill pedals were 0x0003; a new id so Windows never mixes their descriptors

// ==========================================
//   CALIBRATION
// ==========================================
// Compiled-in defaults, used when flash holds no valid saved calibration.
CalibrationData cal = { 2180, 3000, -2000, -350000L, {0, 44, 66, 84, 100}, {0, 25, 50, 75, 100} };

const uint8_t HALL_OVERSAMPLE    = 8;      // ADC reads averaged per loop iteration
const uint8_t HALL_MEDIAN_WINDOW = 3;      // rejects a single-sample spike

// Adaptive filter (Casiez et al.'s "1 euro" filter): a low-pass filter whose
// cutoff rises with how fast the pedal is moving. minCutoff is the smoothing
// at rest (Hz, lower = steadier), beta how quickly the cutoff opens up with
// speed (Hz per "full travels per second"), dCutoff smooths the speed estimate.
// Speed is measured in fractions of the calibrated travel, so one setting suits
// both pedals whatever their raw range. A full stab (a travel in ~0.2 s = 5
// travels/s) opens the filter to about minCutoff + 5 * beta.
const float HALL_MIN_CUTOFF  = 1.5f, HALL_BETA  = 8.0f, HALL_D_CUTOFF  = 8.0f;
const float LOAD_MIN_CUTOFF  = 1.0f, LOAD_BETA  = 6.0f, LOAD_D_CUTOFF  = 8.0f;

// Faults (see pedal_protocol.h): what counts as one and how long it must last.
const int  HALL_RAIL_LOW  = 150;           // 12-bit counts; a working sensor never goes near 0 or 4095
const int  HALL_RAIL_HIGH = 3950;
const unsigned long HALL_FAULT_MS = 50;
const unsigned long LOAD_SILENT_MS = 500;  // no HX711 sample for this long = fault
const unsigned long LOAD_SATURATED_MS = 500;
const unsigned long WATCHDOG_US = 2000000; // restart if the main loop stalls for 2 s (a flash save takes up to ~1 s)

const unsigned long STATUS_INTERVAL_MS = 10;   // 100 Hz (50 Hz in firmware 1.0), only while the PC has the port open

// HID joystick: X = throttle, Y = brake, 0..1023, two 16-bit fields.
uint8_t const hidReportDescriptor[] = {
  0x05, 0x01, 0x09, 0x04, 0xA1, 0x01,   // Generic Desktop, Joystick, Collection
  0x09, 0x30, 0x09, 0x31,               // X, Y
  0x15, 0x00, 0x26, 0xFF, 0x03,         // logical 0..1023
  0x75, 0x10, 0x95, 0x02, 0x81, 0x02,   // 2 x 16 bit, input
  0xC0
};

Adafruit_USBD_HID usbHid;
HX711 scale;

struct AdaptiveFilter {
  float minCutoff, beta, dCutoff;
  float x = 0, dx = 0;
  bool primed = false;

  static float alpha(float cutoff, float dt) {
    float tau = 1.0f / (6.2831853f * cutoff);
    return dt / (dt + tau);
  }
  void reset(float v) { x = v; dx = 0; primed = true; }
  // v: new sample, dt: seconds since the last one, span: the pedal's calibrated travel in the same units.
  float update(float v, float dt, float span) {
    if (!primed || dt <= 0) { reset(v); return x; }
    if (dt > 0.1f) dt = 0.1f;
    dx += alpha(dCutoff, dt) * ((v - x) / dt - dx);
    float cutoff = minCutoff + beta * fabsf(dx) / span;
    x += alpha(cutoff, dt) * (v - x);
    return x;
  }
};

AdaptiveFilter hallFilter = {HALL_MIN_CUTOFF, HALL_BETA, HALL_D_CUTOFF};
AdaptiveFilter loadFilter = {LOAD_MIN_CUTOFF, LOAD_BETA, LOAD_D_CUTOFF};

float hallFiltered = 0;
long loadRaw = 0;   // the filtered brake reading, relative to the start-up tare; holds between HX711 samples
unsigned long lastStatusSent = 0;
unsigned long lastInfoSent = 0;
unsigned long lastLoadSampleMs = 0;   // when the HX711 last delivered a sample
unsigned long lastLoadSampleUs = 0;
unsigned long loadSaturatedSince = 0; // 0 = the HX711's last sample wasn't at its limit
unsigned long hallBadSince = 0;       // 0 = the hall sensor reads inside the ADC's range
uint8_t activeFaults = 0;             // PEDAL_FAULT_* bits that hold an axis at 0 right now
bool watchdogReset = false;           // the last restart came from the watchdog

// ==========================================
//   FLASH PERSISTENCE
// ==========================================
// Two 'slots' in flash sectors 3 (16 KB, 0x0800C000) and 4 (64 KB, 0x08010000),
// each holding one record: magic+version, a sequence number, the calibration
// and a CRC32. A save erases the slot that does NOT hold the newest valid
// record and writes the new one there, so power loss at any moment leaves
// the previous calibration intact (a half-written record fails its CRC).
// The program must stay below 0x0800C000 (48 KB) until a bootloader layout
// moves things - the build size line tells.
#define SLOT0_ADDR   0x0800C000UL
#define SLOT1_ADDR   0x08010000UL
#define SLOT0_SECTOR FLASH_SECTOR_3
#define SLOT1_SECTOR FLASH_SECTOR_4
const uint16_t CAL_MAGIC = 0xCA1B;

struct CalRecord {            // 9 words, written one word at a time
  uint16_t magic;
  uint16_t version;
  uint32_t seq;
  uint8_t  data[24];          // CalibrationData, padded
  uint32_t crc;               // over everything above
};
static_assert(sizeof(CalibrationData) <= 24, "CalRecord.data too small");
static_assert(sizeof(CalRecord) % 4 == 0, "CalRecord must be whole words");

uint32_t crc32(const uint8_t* p, size_t n) {
  uint32_t c = 0xFFFFFFFF;
  while (n--) {
    c ^= *p++;
    for (int i = 0; i < 8; i++) c = (c >> 1) ^ (0xEDB88320UL & -(c & 1));
  }
  return ~c;
}

bool recordValid(const CalRecord* r) {
  return r->magic == CAL_MAGIC && r->version == PEDAL_PROTOCOL_VERSION &&
         r->crc == crc32((const uint8_t*)r, sizeof(CalRecord) - 4);
}

uint32_t currentSeq = 0;
int currentSlot = -1;   // -1 = nothing valid in flash

bool loadCalibration() {
  const CalRecord* slots[2] = {(const CalRecord*)SLOT0_ADDR, (const CalRecord*)SLOT1_ADDR};
  currentSlot = -1;
  for (int i = 0; i < 2; i++) {
    if (recordValid(slots[i]) && (currentSlot < 0 || slots[i]->seq > currentSeq)) {
      currentSlot = i;
      currentSeq = slots[i]->seq;
    }
  }
  if (currentSlot < 0) return false;
  memcpy(&cal, slots[currentSlot]->data, sizeof(cal));
  return true;
}

bool saveCalibration() {
  int target = currentSlot == 0 ? 1 : 0;
  uint32_t addr = target == 0 ? SLOT0_ADDR : SLOT1_ADDR;

  CalRecord rec;
  memset(&rec, 0xFF, sizeof(rec));
  rec.magic = CAL_MAGIC;
  rec.version = PEDAL_PROTOCOL_VERSION;
  rec.seq = currentSeq + 1;
  memcpy(rec.data, &cal, sizeof(cal));
  rec.crc = crc32((const uint8_t*)&rec, sizeof(rec) - 4);

  IWatchdog.reload();   // an erase can take most of a second
  HAL_FLASH_Unlock();
  __HAL_FLASH_CLEAR_FLAG(FLASH_FLAG_EOP | FLASH_FLAG_OPERR | FLASH_FLAG_WRPERR | FLASH_FLAG_PGAERR |
                         FLASH_FLAG_PGPERR | FLASH_FLAG_PGSERR);
  FLASH_EraseInitTypeDef erase = {};
  erase.TypeErase = FLASH_TYPEERASE_SECTORS;
  erase.Sector = target == 0 ? SLOT0_SECTOR : SLOT1_SECTOR;
  erase.NbSectors = 1;
  erase.VoltageRange = FLASH_VOLTAGE_RANGE_3;   // 2.7-3.6 V: word programming
  uint32_t sectorError = 0;
  bool ok = HAL_FLASHEx_Erase(&erase, &sectorError) == HAL_OK;
  const uint32_t* words = (const uint32_t*)&rec;
  for (size_t i = 0; ok && i < sizeof(rec) / 4; i++) {
    ok = HAL_FLASH_Program(FLASH_TYPEPROGRAM_WORD, addr + i * 4, words[i]) == HAL_OK;
  }
  HAL_FLASH_Lock();
  IWatchdog.reload();

  ok = ok && recordValid((const CalRecord*)addr);
  if (ok) {
    currentSlot = target;
    currentSeq = rec.seq;
  }
  return ok;
}

// Curve points are uint8_t on the wire; clamp to the meaningful 0-100.
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
  if (!SerialTinyUSB) return;   // only while the PC has the port open (DTR up)
  uint8_t frame[4 + PEDAL_MAX_PAYLOAD + 1];
  frame[0] = PEDAL_SYNC0;
  frame[1] = PEDAL_SYNC1;
  frame[2] = type;
  frame[3] = len;
  uint8_t checksum = type + len;
  const uint8_t* p = (const uint8_t*)payload;
  for (uint8_t i = 0; i < len; i++) {
    frame[4 + i] = p[i];
    checksum += p[i];
  }
  frame[4 + len] = checksum;
  SerialTinyUSB.write(frame, 5 + len);
}

void sendInfo() {
  PedalInfo info;
  info.fwVersion = PEDAL_FW_VERSION;
  info.faults = activeFaults | (watchdogReset ? PEDAL_FAULT_WATCHDOG_RESET : 0);
  info.boardId = PEDAL_BOARD_BLACKPILL_F411;
  info.protocol = PEDAL_PROTOCOL_VERSION;
  sendFrame(MSG_INFO, &info, sizeof(info));
  lastInfoSent = millis();
}

void handleFrame(uint8_t type, const uint8_t* payload, uint8_t len) {
  switch (type) {
    case MSG_GET_INFO:
      sendInfo();
      break;
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

// Explicit resync state machine: any bad byte drops back to WAIT_SYNC0, and
// 100 ms of mid-frame silence resets it, so a truncated frame can't wedge it.
void pollCommands() {
  static enum { WAIT_SYNC0, WAIT_SYNC1, READ_TYPE, READ_LEN, READ_PAYLOAD, READ_CHECKSUM } state = WAIT_SYNC0;
  static uint8_t msgType, msgLen, payloadIdx, checksum;
  static uint8_t payload[PEDAL_MAX_PAYLOAD];
  static unsigned long lastByteMs = 0;

  if (state != WAIT_SYNC0 && millis() - lastByteMs > 100) state = WAIT_SYNC0;

  while (SerialTinyUSB.available()) {
    uint8_t b = SerialTinyUSB.read();
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
  // A zero-width calibration would divide by zero in map(); report released.
  if (inMin == inMax) return outMin;
  long lo = min(inMin, inMax);
  long hi = max(inMin, inMax);
  x = constrain(x, lo, hi);
  return map(x, inMin, inMax, outMin, outMax);
}

// Piecewise-linear interpolation through the 5 curve points (breakpoints at
// 0/25/50/75/100% of the 0-1023 input range). Integer-only.
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

// Generic odd-window median via insertion sort.
int pushMedian(int sample, int* history, uint8_t windowSize, uint8_t* idx, uint8_t* count) {
  history[*idx] = sample;
  *idx = (*idx + 1) % windowSize;
  if (*count < windowSize) (*count)++;
  if (*count < windowSize) return sample;  // still filling on boot, pass through
  int v[9];
  memcpy(v, history, windowSize * sizeof(int));
  for (uint8_t i = 1; i < windowSize; i++) {
    int key = v[i];
    int j = i;
    while (j > 0 && v[j - 1] > key) { v[j] = v[j - 1]; j--; }
    v[j] = key;
  }
  return v[windowSize / 2];
}

// ==========================================
//   RESTART INTO THE ROM BOOTLOADER (no buttons)
// ==========================================
// The usual "1200 bps touch": opening the USB serial port at 1200 baud and
// closing it again makes the board jump into the chip's built-in USB flasher
// (DFU, system memory at 0x1FFF0000 on the F411), so STM32CubeProgrammer -
// and later Volgas - can flash it without BOOT0 + NRST. The bootloader is in
// ROM, so this can never brick the board; if the jump ever fails, a power
// cycle returns to this firmware and BOOT0 + NRST still works.
// Jumping into the bootloader while this firmware's USB is running left the
// board off the bus (Windows: "device not recognized"), so it's two steps:
// the touch leaves a marker in a backup register and resets the chip, and
// the very start of setup() - before USB exists - sees the marker and jumps.
#define BOOTLOADER_MAGIC 0xB007B007UL

void backupAccessEnable() {
  __HAL_RCC_PWR_CLK_ENABLE();
  HAL_PWR_EnableBkUpAccess();
}

void requestBootloader() {
  USB_OTG_DeviceTypeDef* dev = (USB_OTG_DeviceTypeDef*)((uint32_t)USB_OTG_FS + USB_OTG_DEVICE_BASE);
  dev->DCTL |= USB_OTG_DCTL_SDIS;    // soft-disconnect: the PC sees the pedals leave the bus
  backupAccessEnable();
  RTC->BKP0R = BOOTLOADER_MAGIC;
  delay(100);
  NVIC_SystemReset();
}

void jumpToBootloaderIfRequested() {
  backupAccessEnable();
  if (RTC->BKP0R != BOOTLOADER_MAGIC) return;
  RTC->BKP0R = 0;                    // one shot: the next reset is a normal start
  __disable_irq();
  HAL_RCC_DeInit();
  HAL_DeInit();
  SysTick->CTRL = 0;
  SysTick->LOAD = 0;
  SysTick->VAL = 0;
  for (int i = 0; i < 8; i++) {
    NVIC->ICER[i] = 0xFFFFFFFF;
    NVIC->ICPR[i] = 0xFFFFFFFF;
  }
  __HAL_RCC_SYSCFG_CLK_ENABLE();
  __HAL_SYSCFG_REMAPMEMORY_SYSTEMFLASH();   // system memory at address 0, as the bootloader expects
  __enable_irq();   // the bootloader runs its USB from interrupts; left disabled it never answers the PC
  const uint32_t bootAddr = 0x1FFF0000;
  __set_MSP(*(volatile uint32_t*)bootAddr);
  ((void (*)(void))(*(volatile uint32_t*)(bootAddr + 4)))();
  while (true) {}
}

void checkBootloaderTouch() {
  static bool wasOpen = false;
  bool open = SerialTinyUSB.dtr();
  if (wasOpen && !open && SerialTinyUSB.baud() == 1200) requestBootloader();
  wasOpen = open;
}

void blinkLED(int times, int ms) {
  for (int i = 0; i < times; i++) {
    digitalWrite(LED_PIN, LOW);   // on
    delay(ms);
    digitalWrite(LED_PIN, HIGH);
    delay(ms);
  }
}

void setup() {
  jumpToBootloaderIfRequested();   // first: a 1200 bps touch asked for the bootloader
  pinMode(LED_PIN, OUTPUT);
  digitalWrite(LED_PIN, HIGH);   // off

  watchdogReset = IWatchdog.isReset(true);   // a frozen program was restarted by the watchdog
  DEBUG_SERIAL.begin(115200);
  analogReadResolution(12);      // the calibration defaults are 12-bit counts
  pinMode(HALL_PIN, INPUT_ANALOG);

  if (loadCalibration()) {
    sanitizeCalibration();
    DEBUG_SERIAL.println("BOOT: calibration loaded from flash");
  } else {
    DEBUG_SERIAL.println("BOOT: no saved calibration, using defaults");
  }

  TinyUSB_Device_Init(0);
  TinyUSBDevice.setID(USB_VID, USB_PID);
  TinyUSBDevice.setManufacturerDescriptor("Sim Rig");
  TinyUSBDevice.setProductDescriptor("Pedal Set");
  usbHid.setPollInterval(1);
  usbHid.setReportDescriptor(hidReportDescriptor, sizeof(hidReportDescriptor));
  usbHid.begin();
  SerialTinyUSB.begin(115200);
  blinkLED(3, 100);   // reached here: USB init returned
  DEBUG_SERIAL.println("BOOT: usb init done (HID + CDC composite)");

  scale.begin(HX711_DT, HX711_SCK);
  // Pull the data line up so a disconnected HX711 reads as "not ready"
  // (a floating pin can fake "ready" and hang the blocking reads below).
  pinMode(HX711_DT, INPUT_PULLUP);

  // Tare manually with a bounded wait per read - scale.tare() can block
  // forever if the chip stops converting mid-sequence.
  long tareSum = 0;
  int tareReads = 0;
  for (int i = 0; i < 10; i++) {
    TinyUSB_Device_Task();
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
  hallFilter.reset(hallFiltered);
  lastLoadSampleMs = millis();
  lastLoadSampleUs = micros();
  IWatchdog.begin(WATCHDOG_US);   // from here a stalled loop restarts the board
}

void loop() {
  IWatchdog.reload();
  TinyUSB_Device_Task();
  checkBootloaderTouch();

  static unsigned long lastLoopUs = micros();
  unsigned long nowUs = micros();
  float dt = (nowUs - lastLoopUs) * 1e-6f;
  lastLoopUs = nowUs;
  unsigned long nowMs = millis();

  int hallRaw = analogRead(HALL_PIN);          // single sample, kept for STATUS transparency
  int hallOversampled = readHallOversampled();
  static int hallHistory[HALL_MEDIAN_WINDOW];
  static uint8_t hallHistIdx = 0, hallHistCount = 0;
  int hallMedian = pushMedian(hallOversampled, hallHistory, HALL_MEDIAN_WINDOW, &hallHistIdx, &hallHistCount);
  hallFiltered = hallFilter.update(hallMedian, dt, max(1.0f, fabsf((float)(cal.hallMax - cal.hallMin))));

  // The HX711 only has a sample every 12.5 ms at 80 SPS: filter on those, with
  // the time between them, and hold the value in between.
  if (scale.is_ready()) {
    long raw = scale.read();
    float sampleDt = (nowUs - lastLoadSampleUs) * 1e-6f;
    lastLoadSampleUs = nowUs;
    lastLoadSampleMs = nowMs;
    bool atLimit = raw >= 8388607L || raw <= -8388608L;
    if (!atLimit) loadSaturatedSince = 0;
    else if (loadSaturatedSince == 0) loadSaturatedSince = nowMs;
    float relative = (float)(raw - scale.get_offset());
    float filtered = loadFilter.update(relative, sampleDt, max(1.0f, fabsf((float)(cal.loadMax - cal.loadMin))));
    loadRaw = lroundf(filtered);
  }

  // Faults: hold the affected axis at 0 and report it.
  if (hallOversampled < HALL_RAIL_LOW || hallOversampled > HALL_RAIL_HIGH) {
    if (hallBadSince == 0) hallBadSince = nowMs;
  } else {
    hallBadSince = 0;
  }
  uint8_t faults = 0;
  if (hallBadSince != 0 && nowMs - hallBadSince >= HALL_FAULT_MS) faults |= PEDAL_FAULT_HALL_RANGE;
  if (nowMs - lastLoadSampleMs >= LOAD_SILENT_MS) faults |= PEDAL_FAULT_LOAD_TIMEOUT;
  if (loadSaturatedSince != 0 && nowMs - loadSaturatedSince >= LOAD_SATURATED_MS) faults |= PEDAL_FAULT_LOAD_SATURATED;
  bool faultsChanged = faults != activeFaults;
  activeFaults = faults;

  int throttleLinear = constrainMap((long)hallFiltered, cal.hallMin, cal.hallMax, 0, 1023);
  int throttleAxis   = (faults & PEDAL_FAULT_HALL_RANGE) ? 0 : applyCurve(throttleLinear, cal.throttleCurve);
  int brakeLinear    = constrainMap(loadRaw, cal.loadMin, cal.loadMax, 0, 1023);
  int brakeAxis      = (faults & (PEDAL_FAULT_LOAD_TIMEOUT | PEDAL_FAULT_LOAD_SATURATED)) ? 0
                                                                                      : applyCurve(brakeLinear, cal.brakeCurve);

  if (usbHid.ready()) {
    uint16_t axes[2] = { (uint16_t)throttleAxis, (uint16_t)brakeAxis };
    usbHid.sendReport(0, axes, sizeof(axes));
  }

  pollCommands();

  if (faultsChanged || millis() - lastInfoSent >= 1000) sendInfo();

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
  TinyUSB_Device_FlushCDC();

  // Heartbeat: the LED blinks slowly while the loop runs.
  digitalWrite(LED_PIN, ((millis() / 500) & 1) ? HIGH : LOW);
}
