#include <SPI.h>
#include <WiFi.h>
#include <esp_now.h>
#include <esp_wifi.h>
#include <esp_ota_ops.h>
#include <Update.h>
#include <Adafruit_GFX.h>
#include <Adafruit_ST7735.h>
#include "USB.h"
#include "USBCDC.h"
#include "esp32_dash_protocol.h"
#include "volgas_link.h"
#include "volgas_logo.h"

// ==========================================
//   ESP32-S3 WHEEL (dash + buttons, 2026-11-13)
// ==========================================
// This S3 Mini lives on the wheel (power through the slip ring) and talks
// to the hub (hub_s3) over ESP-NOW - see volgas_link.h:
//   - dash data: Volgas -> hub over USB -> here by radio, fed into the same
//     frame parser as this board's own USB port (which still works, so the
//     dash can also run straight off USB, as it did before the hub);
//   - buttons: 12 in a diode matrix (see BUTTONS below), sent to the hub,
//     which is the game controller the PC sees.
// It used to be the dash on the PC's USB with the Nano wheel's buttons
// coming in over nRF24 and going out as its own USB gamepad; that radio and
// the gamepad are gone.
//
// The ST7735 shows a Volgas boot splash, then a Standby screen (day/time
// from the PC + hub link status) whenever no telemetry is actively arriving,
// switching to the live Classic screen (gear/speed/RPM bar) while it is -
// see esp32_dash_protocol.h and DashParser. Telemetry going quiet for a bit
// (TELEMETRY_TIMEOUT_MS) drops back to Standby automatically, rather than
// freezing on the last frame.
//
// CDC is composited EXPLICITLY here (our own `USBCDC USBSerial` global
// below) - do NOT set CDCOnBoot=cdc; keep the board menu / FQBN at its
// defaults, same as the original known-stable HID-only build. Verified by
// reading the core source (main.cpp / USB.cpp / esp32-hal-tinyusb.c)
// directly: CDCOnBoot=cdc makes app_main() call Serial.begin() AND
// USB.begin() before setup() even runs - ESPUSB::begin() is one-shot
// (`if (!_started)`), so the sketch's own USB.begin() and the
// USB.productName()/manufacturerName() calls were silently ignored, and
// USB started at the earliest instant of cold boot, outside our control.
// That build was the one that failed to bring up CDC on cold boot
// (2026-07-16). With CDCOnBoot left Disabled, nothing in the core touches
// USB before setup(): interfaces register in global constructors (both
// USBCDC's and USBHID's constructors call tinyusb_enable_interface - it
// hard-fails after init, so ALL interfaces must exist before the one-shot
// tinyusb_init), and the single USB.begin() in setup() below builds the
// composite descriptor with both CDC and HID and starts the stack once,
// in an order we fully control. Note `Serial` itself maps to Serial0
// (UART0 pins) in this configuration - useless over the USB cable; all
// USB serial I/O here uses USBSerial explicitly.

// ==========================================
//   BUTTONS (4x3 matrix, not wired yet as of 2026-11-13)
// ==========================================
// 12 buttons in 4 rows x 3 columns on free pins (GPIO7 is the display
// backlight):
//   rows    -> GPIO4, GPIO5, GPIO6, GPIO1
//   columns -> GPIO13, GPIO2, GPIO3
// Each button connects its row to its column through a diode (e.g. 1N4148),
// band (cathode) towards the ROW - without diodes, holding three buttons
// that form a rectangle makes a fourth look pressed.
// Numbering: button = row * 3 + column + 1, so row 0 is buttons 1-3, row 1
// is 4-6 ... row 3 is 10-12 (columns in the order listed above).
// Scanning: one row at a time is driven LOW (the others float), and the
// columns, pulled up inside the chip, read LOW where a button is held.
const uint8_t ROW_PINS[] = {4, 5, 6, 1};
const uint8_t COL_PINS[] = {13, 2, 3};
#define ROW_COUNT (sizeof(ROW_PINS) / sizeof(ROW_PINS[0]))
#define COL_COUNT (sizeof(COL_PINS) / sizeof(COL_PINS[0]))
#define BUTTON_COUNT (ROW_COUNT * COL_COUNT)
#define BUTTON_DEBOUNCE_READS 3   // a change counts once it reads the same this many loops in a row (~6 ms)

// ST7735 display wiring (own bus, HSPI/default global SPI):
//   VCC -> 3.3V, GND -> GND, LED (backlight) -> GPIO7
//   SCK -> GPIO12, MOSI/SDA -> GPIO11, CS -> GPIO10, DC -> GPIO8, RST -> GPIO9
// The backlight used to be on 3.3V, so it lit the moment the board got power
// and showed the panel's white "not set up yet" screen until the firmware
// had initialised it. Now it stays off until the boot logo is on the
// screen (showSplashScreen). The module's own resistor limits the current.
// Matches the pin plan already decided before this board arrived (see
// CLAUDE.md's original dash section) - unchanged since the display was
// never the half of this that needed separating.
#define TFT_SCK  12
#define TFT_MOSI 11
#define TFT_CS   10
#define TFT_DC   8
#define TFT_RST  9
#define TFT_LED  7

// GPIOs to avoid on this chip (already learned the hard way on other
// boards in this project - see CLAUDE.md toolchain gotchas): GPIO0 (boot
// button), 19/20 (native USB), 43/44 (UART), 45/46 (boot strapping).
// Confirm the pins above are actually broken out on your specific
// "ESP32-S3 Mini" board before wiring - generic "S3 Mini" covers several
// different vendor boards with different physical pin breakouts.

uint32_t wheelButtons = 0;   // debounced, bit N = button N+1
bool hubLinkOk = false;      // for the Standby screen

enum DashScreen { SCREEN_STANDBY, SCREEN_CLASSIC };
DashScreen currentScreen = SCREEN_STANDBY;

USBCDC USBSerial;   // explicit CDC interface - see header comment; constructor registers it with TinyUSB
Adafruit_ST7735 tft = Adafruit_ST7735(TFT_CS, TFT_DC, TFT_RST);

#define SCREEN_W 128
#define SCREEN_H 128

// Runs once at boot, after tft.initR() has already happened in setup()'s
// proof-of-life step. The old tab-variant color-cycle diagnostic (144GREEN
// / BLACK / GREEN / RED) is gone - INITR_144GREENTAB is confirmed correct
// for this panel (see CLAUDE.md), so there's nothing left to diagnose.
void showSplashScreen() {
  tft.fillScreen(ST7735_BLACK);
  // drawRGBBitmap() already wraps itself in startWrite()/endWrite() -
  // Adafruit_SPITFT's version of those isn't nesting-safe (no reentrancy
  // counter, just an unconditional SPI beginTransaction/endTransaction), so
  // adding another pair around this call double-begins the transaction and
  // hangs the bus - that's what produced a fully black display 2026-07-25.
  tft.drawRGBBitmap((SCREEN_W - VOLGAS_LOGO_W) / 2, (SCREEN_H - VOLGAS_LOGO_H) / 2, volgas_logo, VOLGAS_LOGO_W, VOLGAS_LOGO_H);
  digitalWrite(TFT_LED, HIGH);   // backlight on: the first thing shown is the logo
  delay(3000);
}

// ==========================================
//   CLASSIC SCREEN (gear / speed / RPM bar)
// ==========================================
// Same layout and damage-tracking discipline as the earlier Nano version
// of this (only touch pixels whose value actually changed) - ported
// rather than reinvented, see docs/wireless_telemetry_relay_attempt.md
// for that history.
#define RPM_BAR_H 14
#define CLASSIC_FOOTER_H 46   // lap time / sector split area at the bottom - see CLASSIC SCREEN FOOTER below (bumped from 30 2026-07-25 for bigger footer text)
#define GEAR_TEXT_SIZE 7    // char cell 6*7=42w x 8*7=56h - big, still fits a single R/N/digit in the 64px-wide left column
#define SPEED_TEXT_SIZE 3   // char cell 6*3=18w x 8*3=24h - up to 3 digits (54w) fits the 64px-wide right column

int8_t  lastGear = -128;          // sentinel outside int8_t telemetry range - forces first draw
int16_t lastSpeedShown = -1;
uint8_t lastBarHalfWidth = 255;   // sentinel - forces first draw
uint16_t lastBarColor = 0;

// Footer (lap time + delta-to-best) state - declared here rather than down
// by its own functions since classicScreenInit() below needs it, and
// Arduino only auto-forward-declares functions, not globals.
uint32_t currentLapTimeMs = 0;
bool currentLapTimeValid = true;
uint32_t lastLapTimeMs = 0xFFFFFFFF;   // sentinel - forces first draw
bool lastLapTimeValid = true;          // separately tracked so a validity flip alone (same ms value) still forces a redraw
int32_t lastDrawnDeltaMs = 0;
bool lastDeltaValid = false;           // live validity, not a one-way latch (2026-07-25) - the PC side now explicitly sends valid=0
                                        // on a car reset/pit visit so this can properly blank instead of freezing on a stale value

// Reactive footer height (2026-07-25, per request) - gear/speed use the
// full screen height below the RPM bar when there's no lap-time data
// flowing at all (e.g. a future game bridge that never sends it, or just
// not in a session yet), and only shrink to make room for the footer once
// MSG_LAP_TIME messages are actually arriving. Continuously reactive, not
// a one-way latch: if lap-time messages stop for FOOTER_DATA_TIMEOUT_MS
// (loop(), below), gear/speed expand back down. The footer's own two lines
// (lap time, delta) still independently show/blank via their own valid
// flags - this only controls whether the footer's 46px band is reserved
// from the gear/speed area at all.
#define FOOTER_DATA_TIMEOUT_MS 1500
bool footerActive = false;
uint8_t footerReservedH = 0;   // 0 = gear/speed use the full height; CLASSIC_FOOTER_H once footerActive
unsigned long lastLapTimeMsgMs = 0;

void setFooterActive(bool active) {
  if (active == footerActive) return;
  footerActive = active;
  footerReservedH = active ? CLASSIC_FOOTER_H : 0;
  // Gear/speed's vertical centering depends on footerReservedH, so a full
  // repaint (and damage-tracking reset) is needed whichever direction this
  // just changed - same reasoning as classicScreenInit()'s reset below.
  tft.fillScreen(ST7735_BLACK);
  lastGear = -128;
  lastSpeedShown = -1;
  lastBarHalfWidth = 255;
  lastBarColor = 0;
  lastLapTimeMs = 0xFFFFFFFF;
  lastDrawnDeltaMs = 0;
  lastDeltaValid = false;
}

void classicScreenInit() {
  tft.fillScreen(ST7735_BLACK);
  lastGear = -128;
  lastSpeedShown = -1;
  lastBarHalfWidth = 255;
  lastBarColor = 0;
  lastLapTimeMs = 0xFFFFFFFF;   // sentinel - forces first footer draw
  lastLapTimeValid = true;
  lastDeltaValid = false;
  footerActive = false;         // start collapsed - reactivates on the first MSG_LAP_TIME, per setFooterActive()
  footerReservedH = 0;
}

uint16_t rpmBarColor(uint16_t rpm, uint16_t rpmMax) {
  if (rpmMax == 0) return ST7735_GREEN;
  float frac = (float)rpm / (float)rpmMax;
  if (frac > 0.92) return ST7735_RED;
  if (frac > 0.75) return ST7735_YELLOW;
  return ST7735_GREEN;
}

// Gear (left half) and speed (right half) share the space below the RPM
// bar, each centered within its own half - not just the old
// left-aligned-box/right-aligned-text positioning.
//
// Both draw at a FIXED field width/position and use opaque text
// (setTextColor(fg, bg)) instead of clear-then-redraw: an opaque print
// paints every pixel in the character cell (glyph AND background) in one
// pass, so the new digit(s) fully overwrite the old ones with no separate
// black-fill step - eliminating the flash a clear-then-redraw two-step
// produces on every rapid update, even when that clear was already shrunk
// to just the previous glyph's bounding box (2026-07-25: that box-clear
// approach still visibly flashed on speed, which redraws almost every
// frame while accelerating/decelerating).
void drawGear(int8_t gear) {
  if (gear == lastGear) return;
  const int16_t colX = 0, colW = SCREEN_W / 2, colY = RPM_BAR_H, colH = SCREEN_H - RPM_BAR_H - footerReservedH;
  char c;
  if (gear < 0) c = 'R';
  else if (gear == 0) c = 'N';
  else c = (char)('0' + gear);   // single-digit gears only (R/N/1-9) - true of every sim car gearbox in practice
  const int16_t textW = 6 * GEAR_TEXT_SIZE;   // fixed: always exactly 1 character
  const int16_t textH = 8 * GEAR_TEXT_SIZE;
  const int16_t x = colX + (colW - textW) / 2;
  const int16_t y = colY + (colH - textH) / 2;

  tft.setTextColor(ST7735_WHITE, ST7735_BLACK);
  tft.setTextSize(GEAR_TEXT_SIZE);
  tft.setCursor(x, y);
  tft.print(c);
  lastGear = gear;
}

void drawSpeed(uint16_t speedKph) {
  if ((int16_t)speedKph == lastSpeedShown) return;
  const int16_t colX = SCREEN_W / 2, colW = SCREEN_W / 2, colY = RPM_BAR_H, colH = SCREEN_H - RPM_BAR_H - footerReservedH;
  char buf[4];
  snprintf(buf, sizeof(buf), "%3u", (unsigned)min(speedKph, (uint16_t)999));   // fixed 3-char field, space-padded
  const int16_t textW = 3 * 6 * SPEED_TEXT_SIZE;   // fixed: always exactly 3 characters
  const int16_t textH = 8 * SPEED_TEXT_SIZE;
  const int16_t x = colX + (colW - textW) / 2;
  const int16_t y = colY + (colH - textH) / 2;

  tft.setTextColor(ST7735_WHITE, ST7735_BLACK);
  tft.setTextSize(SPEED_TEXT_SIZE);
  tft.setCursor(x, y);
  tft.print(buf);
  lastSpeedShown = speedKph;
}

void drawRpmBar(uint16_t rpm, uint16_t rpmMax) {
  uint16_t color = rpmBarColor(rpm, rpmMax);
  uint8_t halfWidth = 0;
  if (rpmMax > 0) {
    long w = ((long)rpm * (SCREEN_W / 2)) / rpmMax;
    halfWidth = (uint8_t)constrain(w, 0, SCREEN_W / 2);
  }
  if (halfWidth == lastBarHalfWidth && color == lastBarColor) return;

  int16_t mid = SCREEN_W / 2;
  if (color != lastBarColor) {
    // Color changed (crossed a green/yellow/red threshold) - rare, so a
    // full clear-and-redraw here doesn't produce visible flicker in
    // practice, unlike doing it on every width change (see below).
    tft.fillRect(0, 0, SCREEN_W, RPM_BAR_H, ST7735_BLACK);
    if (halfWidth > 0) tft.fillRect(mid - halfWidth, 0, halfWidth * 2, RPM_BAR_H, color);
  } else if (halfWidth > lastBarHalfWidth) {
    // Bar grew - paint only the newly-exposed edge strips; nothing needs
    // blacking out first. Painting the whole bar black then color on every
    // width change (the old behavior) produced a visible flash on every
    // RPM update (2026-07-25) since this redraws constantly while driving.
    uint8_t grow = halfWidth - lastBarHalfWidth;
    tft.fillRect(mid - halfWidth, 0, grow, RPM_BAR_H, color);
    tft.fillRect(mid + lastBarHalfWidth, 0, grow, RPM_BAR_H, color);
  } else {
    // Bar shrank - only black out the strips no longer covered.
    uint8_t shrink = lastBarHalfWidth - halfWidth;
    tft.fillRect(mid - lastBarHalfWidth, 0, shrink, RPM_BAR_H, ST7735_BLACK);
    tft.fillRect(mid + halfWidth, 0, shrink, RPM_BAR_H, ST7735_BLACK);
  }
  lastBarHalfWidth = halfWidth;
  lastBarColor = color;
}

void classicScreenUpdate(const DashTelemetry& t) {
  drawGear(t.gear);
  drawSpeed(t.speedKph);
  drawRpmBar(t.rpm, t.rpmMax);
}

// ==========================================
//   CLASSIC SCREEN FOOTER (lap time + delta-to-best, 2026-07-25)
// ==========================================
// Two lines, both always visible: the running lap time (top) and a
// persistent delta-to-best-lap reading (below), updated whenever new data
// arrives and held until the next update. This replaces the original
// design (a temporary sector-split overlay that took over the whole footer
// for a few seconds) - removed per request ("didn't work, remove it"; see
// CLAUDE.md for the mSector-indexing uncertainty that was the likely
// cause). Both readings compare against the player's best lap this session
// (see volgas_rf2_dash.py's compute_lap_delta()). State (currentLapTimeMs
// etc.) declared earlier, alongside the other Classic-screen globals - see
// comment there.

void footerClear() {
  tft.fillRect(0, SCREEN_H - CLASSIC_FOOTER_H, SCREEN_W, CLASSIC_FOOTER_H, ST7735_BLACK);
}

// mm:ss.d - deciseconds is plenty for a clock that ticks continuously (keeps
// redraws to 10/sec instead of 1000/sec for millisecond precision nobody
// could read anyway at this size). Minutes are a FIXED 2 digits (not "m:ss.d"
// with however many digits minutes needs) so this is always exactly 7
// characters - required for the opaque-text flicker fix below, which only
// works if the field width never changes between redraws.
void formatLapTime(uint32_t ms, char* buf, size_t bufSize) {
  uint32_t totalDs = ms / 100;
  uint32_t m = min((uint32_t)99, totalDs / 600);
  uint32_t s = (totalDs / 10) % 60;
  uint32_t d = totalDs % 10;
  snprintf(buf, bufSize, "%02u:%02u.%u", (unsigned)m, (unsigned)s, (unsigned)d);
}

// +SS.ddd / -SS.ddd - seconds zero-padded to 2 digits (same fixed-7-char
// reasoning as formatLapTime above) rather than "+/-s.ddd" with however many
// digits the seconds need.
void formatDeltaTime(int32_t ms, char* buf, size_t bufSize) {
  bool neg = ms < 0;
  uint32_t absMs = (uint32_t)(neg ? -ms : ms);
  uint32_t s = min((uint32_t)99, absMs / 1000);
  uint32_t msec = absMs % 1000;
  snprintf(buf, bufSize, "%c%02u.%03u", neg ? '-' : '+', (unsigned)s, (unsigned)msec);
}

const int16_t FOOTER_LINE1_Y = SCREEN_H - CLASSIC_FOOTER_H + 3;
const int16_t FOOTER_LINE2_Y = FOOTER_LINE1_Y + 16 + 3;   // 16 = size-2 text height

void footerDrawLapTime(uint32_t lapTimeMs, bool valid) {
  // While invalid the displayed content is always "--:--,-" regardless of
  // the underlying ms value (which keeps ticking - whether it should
  // freeze/reset on an invalid lap is still an open question, see
  // CLAUDE.md), so only the validity flip matters there; only compare
  // lapTimeMs when valid, or this redraws needlessly every frame while
  // sitting on an invalid lap.
  if (valid == lastLapTimeValid && (!valid || lapTimeMs == lastLapTimeMs)) return;
  char buf[10];
  if (valid) {
    formatLapTime(lapTimeMs, buf, sizeof(buf));
  } else {
    // Dash placeholder instead of a strikethrough line (2026-07-25) - same
    // 7-character width as formatLapTime's real output ("mm:ss.d"), just
    // digits swapped for dashes and a comma in place of the decimal point
    // per request, so the fixed-width opaque-redraw approach still applies.
    snprintf(buf, sizeof(buf), "--:--,-");
  }
  const uint8_t textSize = 2;
  const int16_t textW = (int16_t)strlen(buf) * 6 * textSize;   // fixed: always exactly 7 characters, valid or not
  const int16_t x = (SCREEN_W - textW) / 2;

  // Opaque fixed-width text instead of clear-then-redraw (2026-07-25) - the
  // same flicker fix already applied to gear/speed (see drawGear's comment
  // above) that this footer was missed for initially. The lap timer
  // redraws every decisecond, so the clear-then-redraw flash was very
  // visible here.
  tft.setTextColor(ST7735_WHITE, ST7735_BLACK);
  tft.setTextSize(textSize);
  tft.setCursor(x, FOOTER_LINE1_Y);
  tft.print(buf);

  lastLapTimeMs = lapTimeMs;
  lastLapTimeValid = valid;
}

void footerDrawDelta(int32_t deltaMs, bool valid) {
  if (valid == lastDeltaValid && (!valid || deltaMs == lastDrawnDeltaMs)) return;

  char buf[10];
  uint16_t color;
  if (!valid) {
    // Blank whenever the PC side has nothing to show right now - not just
    // "hasn't sent one yet this session" but also a live car reset/pit
    // visit (2026-07-25: this used to be a one-way latch that never
    // blanked again once first shown, which froze on a stale value after a
    // reset instead of clearing). 7 spaces keeps the same fixed width as
    // formatDeltaTime's output, drawn in black-on-black (i.e. just clears
    // the row).
    snprintf(buf, sizeof(buf), "       ");
    color = ST7735_BLACK;
  } else {
    formatDeltaTime(deltaMs, buf, sizeof(buf));
    color = deltaMs <= 0 ? ST7735_GREEN : ST7735_RED;   // negative = faster/ahead
  }

  const uint8_t textSize = 2;
  const int16_t textW = 7 * 6 * textSize;   // fixed width - formatDeltaTime always produces exactly 7 characters
  const int16_t x = (SCREEN_W - textW) / 2;

  tft.setTextColor(color, ST7735_BLACK);
  tft.setTextSize(textSize);
  tft.setCursor(x, FOOTER_LINE2_Y);
  tft.print(buf);

  lastDrawnDeltaMs = deltaMs;
  lastDeltaValid = valid;
}

// ==========================================
//   STANDBY SCREEN (day/time + app/wheel link status)
// ==========================================
// Shown at boot and whenever telemetry isn't actively arriving. Time comes
// from the PC's periodic TimeSync messages (see esp32_dash_protocol.h) -
// there's no RTC or WiFi/NTP on this board.
//
// hasTimeSync is a ONE-WAY LATCH (2026-07-25) - once the first sync ever
// arrives it stays true until reboot; it does NOT get reset back to false
// if syncs stop. Between syncs (and after the PC app disconnects entirely)
// the clock keeps running on its own, computed from the last synced
// timestamp plus elapsed millis() (computeCurrentTime() below) rather than
// freezing or disappearing - the previous behavior (hasTimeSync reverting
// to false after TIME_SYNC_TIMEOUT_MS, dropping back to wheel-status-only)
// made the clock vanish every time the PC app closed. Still true that no
// time is shown at all until the FIRST sync ever arrives (nothing to
// compute from) - see appConnected below for a separate "is the app
// currently connected" indicator, since that's now a different question
// from "do we know the time".
const char* dayNames[7] = { "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday" };

bool hasTimeSync = false;   // one-way latch - true forever once the first sync arrives
uint8_t syncedDayOfWeek = 0, syncedHour = 0, syncedMinute = 0, syncedSecond = 0;
unsigned long lastTimeSyncMs = 0;   // millis() when the above was captured - anchor for computeCurrentTime(), and for appConnected's recency check
const unsigned long APP_TIMEOUT_MS = 15000;   // PC resyncs every ~5s while connected (volgas_rf2_dash.py TIME_SYNC_PERIOD_S) - 3x margin before calling the app disconnected

// Current best-known wall time: the last synced timestamp plus however much
// time has passed since (millis() delta from lastTimeSyncMs). Keeps the
// clock advancing smoothly on its own between syncs and after disconnects;
// drifts slowly against real time between resyncs (ESP32's internal clock
// isn't crystal-accurate) but gets corrected back whenever a new sync lands.
void computeCurrentTime(uint8_t& dayOfWeek, uint8_t& hour, uint8_t& minute) {
  unsigned long elapsedSec = (millis() - lastTimeSyncMs) / 1000;
  unsigned long totalSec = (unsigned long)syncedSecond + elapsedSec;
  unsigned long totalMin = (unsigned long)syncedMinute + totalSec / 60;
  unsigned long totalHour = (unsigned long)syncedHour + totalMin / 60;
  unsigned long dayRollover = totalHour / 24;
  dayOfWeek = (syncedDayOfWeek + dayRollover) % 7;
  hour = totalHour % 24;
  minute = totalMin % 60;
}

int16_t standbyLastMinute = -1;
int16_t standbyLastDay = -1;
int8_t  standbyLastWheelOk = -1;      // -1 = never drawn, 0 = lost, 1 = ok
int8_t  standbyLastAppConnected = -1; // -1 = never drawn, 0 = disconnected, 1 = connected
bool    standbyLastHasTime = false;
uint32_t standbyLastButtons = 0xFFFFFFFF; // sentinel - forces first draw

void standbyScreenInit() {
  tft.fillScreen(ST7735_BLACK);
  standbyLastMinute = -1;
  standbyLastDay = -1;
  standbyLastWheelOk = -1;
  standbyLastAppConnected = -1;
  standbyLastButtons = 0xFFFFFFFF;
  standbyLastHasTime = false;   // forces the first update to lay out for whatever hasTimeSync currently is
}

// Layout: relative offsets from the top of a vertically-centered content
// block, rather than fixed top-anchored positions (2026-07-25) - restores
// the original impression of the wheel status "moving to the middle" of the
// screen when it's the only thing shown (no time yet), now extended to the
// whole block (time+day+wheel+app) centering together whenever time is shown.
const int16_t STANDBY_TIME_OFFSET              = 0;
const int16_t STANDBY_DAY_OFFSET               = 30;
// With the clock shown, only the time and day are centred (block height 40:
// they stay in the middle of the screen whatever the status rows below do);
// the hub / app / button rows sit in fixed slots under them (y = 88, 100,
// 112), and are blank unless there's something to say.
const int16_t STANDBY_WHEEL_OFFSET_WITH_TIME   = 44;
const int16_t STANDBY_APP_OFFSET_WITH_TIME     = 56;
const int16_t STANDBY_BUTTON_OFFSET_WITH_TIME  = 68;
const int16_t STANDBY_BLOCK_HEIGHT_WITH_TIME   = 40;

const int16_t STANDBY_WHEEL_OFFSET_NO_TIME  = 0;
const int16_t STANDBY_APP_OFFSET_NO_TIME    = 16;
const int16_t STANDBY_BUTTON_OFFSET_NO_TIME = 32;
const int16_t STANDBY_BLOCK_HEIGHT_NO_TIME  = 42;

int16_t standbyBlockTop() {
  int16_t blockHeight = hasTimeSync ? STANDBY_BLOCK_HEIGHT_WITH_TIME : STANDBY_BLOCK_HEIGHT_NO_TIME;
  return (SCREEN_H - blockHeight) / 2;
}

void standbyDrawTime(uint8_t dayOfWeek, uint8_t hour, uint8_t minute) {
  int16_t top = standbyBlockTop();

  char timebuf[6];
  snprintf(timebuf, sizeof(timebuf), "%02u:%02u", hour, minute);
  int16_t tx = (SCREEN_W - (int16_t)strlen(timebuf) * 18) / 2;   // ~18px per size-3 char
  int16_t ty = top + STANDBY_TIME_OFFSET;
  tft.fillRect(0, ty, SCREEN_W, 26, ST7735_BLACK);
  tft.setTextSize(3);
  tft.setTextColor(ST7735_WHITE);
  tft.setCursor(tx, ty + 2);
  tft.print(timebuf);

  const char* day = dayNames[dayOfWeek % 7];
  int16_t dx = (SCREEN_W - (int16_t)strlen(day) * 6) / 2;   // ~6px per size-1 char
  int16_t dy = top + STANDBY_DAY_OFFSET;
  tft.fillRect(0, dy, SCREEN_W, 10, ST7735_BLACK);
  tft.setTextSize(1);
  tft.setTextColor(ST7735_WHITE);
  tft.setCursor(dx, dy);
  tft.print(day);
}

// The radio link to the hub (the row kept its old "wheel" position names).
// Only shown while the link is down - blank (just cleared) when it's fine,
// like the app status line below.
void standbyDrawWheelStatus(bool linkOk) {
  int16_t y = standbyBlockTop() + (hasTimeSync ? STANDBY_WHEEL_OFFSET_WITH_TIME : STANDBY_WHEEL_OFFSET_NO_TIME);
  tft.fillRect(0, y, SCREEN_W, 10, ST7735_BLACK);
  if (linkOk) return;
  const char* buf = "hub: lost";
  int16_t x = (SCREEN_W - (int16_t)strlen(buf) * 6) / 2;
  tft.setCursor(x, y);
  tft.setTextColor(ST7735_WHITE);
  tft.print("hub: ");
  tft.setTextColor(ST7735_RED);
  tft.print("lost");
}

void standbyDrawAppStatus(bool connected) {
  int16_t y = standbyBlockTop() + (hasTimeSync ? STANDBY_APP_OFFSET_WITH_TIME : STANDBY_APP_OFFSET_NO_TIME);
  tft.fillRect(0, y, SCREEN_W, 10, ST7735_BLACK);
  if (connected) return;   // only shown when disconnected - blank (just cleared above) otherwise
  const char* buf = "app: disconnected";
  int16_t x = (SCREEN_W - (int16_t)strlen(buf) * 6) / 2;
  tft.setCursor(x, y);
  tft.setTextColor(ST7735_RED);
  tft.print(buf);
}

// Shows which button(s) are currently held, for testing the wiring without
// needing a game running - blank when nothing's pressed, same "only show
// when there's something to say" idiom as the app status line above.
void standbyDrawButtons(uint32_t buttons) {
  int16_t y = standbyBlockTop() + (hasTimeSync ? STANDBY_BUTTON_OFFSET_WITH_TIME : STANDBY_BUTTON_OFFSET_NO_TIME);
  tft.fillRect(0, y, SCREEN_W, 10, ST7735_BLACK);
  if (buttons == 0) return;   // blank when nothing's pressed

  char buf[40] = "btn: ";
  size_t len = strlen(buf);
  bool first = true;
  for (uint8_t bit = 0; bit < BUTTON_COUNT && len < sizeof(buf) - 4; bit++) {
    if (!(buttons & (1u << bit))) continue;
    if (!first) buf[len++] = ',';
    len += snprintf(buf + len, sizeof(buf) - len, "%u", bit + 1);   // bit N = button N+1
    first = false;
  }
  int16_t x = (SCREEN_W - (int16_t)strlen(buf) * 6) / 2;
  tft.setCursor(max((int16_t)0, x), y);
  tft.setTextColor(ST7735_YELLOW);
  tft.print(buf);
}

void standbyScreenUpdate(bool wheelOk) {
  if (hasTimeSync != standbyLastHasTime) {
    // Layout differs with/without the clock - full repaint so nothing from
    // the other layout is left on screen.
    tft.fillScreen(ST7735_BLACK);
    standbyLastHasTime = hasTimeSync;
    standbyLastMinute = -1;
    standbyLastDay = -1;
    standbyLastWheelOk = -1;
    standbyLastAppConnected = -1;
    standbyLastButtons = 0xFFFFFFFF;
  }

  if (hasTimeSync) {
    uint8_t dow, hr, mn;
    computeCurrentTime(dow, hr, mn);
    if (mn != standbyLastMinute || dow != standbyLastDay) {
      standbyDrawTime(dow, hr, mn);
      standbyLastMinute = mn;
      standbyLastDay = dow;
    }
  }

  bool appConnected = hasTimeSync && (millis() - lastTimeSyncMs < APP_TIMEOUT_MS);
  int8_t appConnectedVal = appConnected ? 1 : 0;
  if (appConnectedVal != standbyLastAppConnected) {
    standbyDrawAppStatus(appConnected);
    standbyLastAppConnected = appConnectedVal;
  }

  int8_t wheelOkVal = wheelOk ? 1 : 0;
  if (wheelOkVal != standbyLastWheelOk) {
    standbyDrawWheelStatus(wheelOk);
    standbyLastWheelOk = wheelOkVal;
  }

  if (wheelButtons != standbyLastButtons) {
    standbyDrawButtons(wheelButtons);
    standbyLastButtons = wheelButtons;
  }
}

// ==========================================
//   TELEMETRY LINK (see esp32_dash_protocol.h)
// ==========================================
// Same resync state machine idiom used throughout this project: any bad
// byte or stale mid-frame silence drops back to WAIT_SYNC0.
DashTelemetry latestTelemetry = {0, 0, 0, 0};
unsigned long lastTelemetryMs = 0;
const unsigned long TELEMETRY_TIMEOUT_MS = 1000;   // bridge sends at 20Hz - 1s of silence means the game/bridge stopped

void handleDashMessage(uint8_t msgType, const uint8_t* payload, uint8_t msgLen) {
  if (msgType == MSG_DASH_TELEMETRY && msgLen == sizeof(DashTelemetry)) {
    lastTelemetryMs = millis();
    memcpy(&latestTelemetry, payload, sizeof(DashTelemetry));
    if (currentScreen != SCREEN_CLASSIC) {
      currentScreen = SCREEN_CLASSIC;
      classicScreenInit();
    }
    classicScreenUpdate(latestTelemetry);
  } else if (msgType == MSG_TIME_SYNC && msgLen == sizeof(TimeSync)) {
    TimeSync ts;
    memcpy(&ts, payload, sizeof(TimeSync));
    syncedDayOfWeek = ts.dayOfWeek;
    syncedHour = ts.hour;
    syncedMinute = ts.minute;
    syncedSecond = ts.second;
    hasTimeSync = true;   // one-way latch - see STANDBY SCREEN section
    lastTimeSyncMs = millis();
  } else if (msgType == MSG_LAP_TIME && msgLen == sizeof(LapTime)) {
    LapTime lt;
    memcpy(&lt, payload, sizeof(LapTime));
    currentLapTimeMs = lt.lapTimeMs;
    currentLapTimeValid = lt.valid != 0;
    lastLapTimeMsgMs = millis();
    if (currentScreen == SCREEN_CLASSIC) {
      if (!footerActive) setFooterActive(true);
      footerDrawLapTime(currentLapTimeMs, currentLapTimeValid);
    }
  } else if (msgType == MSG_LAP_DELTA && msgLen == sizeof(LapDelta)) {
    LapDelta ld;
    memcpy(&ld, payload, sizeof(LapDelta));
    if (currentScreen == SCREEN_CLASSIC) {
      footerDrawDelta(ld.cumulativeDeltaMs, ld.valid != 0);
    }
  }
}

// One frame parser per byte source (this board's USB port, and the hub's
// radio packets), so bytes from one can never land in the middle of a frame
// from the other.
struct DashParser {
  enum { WAIT_SYNC0, WAIT_SYNC1, READ_TYPE, READ_LEN, READ_PAYLOAD, READ_CHECKSUM } state = WAIT_SYNC0;
  uint8_t msgType = 0, msgLen = 0, payloadIdx = 0, checksum = 0;
  uint8_t payload[DASH_MAX_PAYLOAD];
  unsigned long lastByteMs = 0;

  void feed(uint8_t b) {
    if (state != WAIT_SYNC0 && millis() - lastByteMs > 100) state = WAIT_SYNC0;
    lastByteMs = millis();
    switch (state) {
      case WAIT_SYNC0:
        if (b == DASH_SYNC0) state = WAIT_SYNC1;
        break;
      case WAIT_SYNC1:
        state = (b == DASH_SYNC1) ? READ_TYPE : WAIT_SYNC0;
        break;
      case READ_TYPE:
        msgType = b;
        checksum = b;
        state = READ_LEN;
        break;
      case READ_LEN:
        if (b > DASH_MAX_PAYLOAD) { state = WAIT_SYNC0; break; }
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
        if (b == checksum) handleDashMessage(msgType, payload, msgLen);
        state = WAIT_SYNC0;
        break;
    }
  }
};

DashParser usbParser, radioParser;

void pollTelemetry() {
  while (USBSerial.available()) usbParser.feed(USBSerial.read());
}

// ==========================================
//   RADIO LINK TO THE HUB (see volgas_link.h)
// ==========================================
const uint8_t BROADCAST[6] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};
const uint16_t WHEEL_FW_VERSION = 5;   // bump for every release - the hub and Volgas show it (1 = first radio link, 2 = wireless updates, 3 = backlight on GPIO7, 4 = hub status only shown when lost, 5 = clock centred)

struct RxPacket {
  uint8_t mac[6];
  uint8_t len;
  uint8_t data[250];
};
QueueHandle_t rxQueue;

uint8_t hubMac[6];
bool hubKnown = false;
unsigned long lastHubMs = 0;

// Runs in the Wi-Fi task: only copy the packet out; loop() does the work
// (the display must only ever be touched from one task).
void onReceive(const esp_now_recv_info_t* info, const uint8_t* data, int len) {
  if (!linkValid(data, len) || len > 250) return;
  RxPacket p;
  memcpy(p.mac, info->src_addr, 6);
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

void linkBegin() {
  rxQueue = xQueueCreate(16, sizeof(RxPacket));
  WiFi.mode(WIFI_STA);
  WiFi.disconnect();
  esp_wifi_set_channel(LINK_CHANNEL, WIFI_SECOND_CHAN_NONE);
  if (esp_now_init() != ESP_OK) {
    USBSerial.println("BOOT: ESP-NOW failed to start");
    return;
  }
  esp_now_register_recv_cb(onReceive);
  ensurePeer(BROADCAST);
  USBSerial.printf("BOOT: radio up, %s on channel %d\n", WiFi.macAddress().c_str(), LINK_CHANNEL);
}

// ==========================================
//   WIRELESS FIRMWARE UPDATE (see volgas_link.h)
// ==========================================
// Chunks arrive in order through the hub and go straight into the spare
// firmware slot (Update writes the OTA partition that isn't running). Every
// packet is answered with the next offset wanted, so the sender just resends
// whatever didn't arrive. Refused while the car is moving.
//
// Probation: the core is told (verifyRollbackLater) not to accept a new
// firmware on its own. After an update the wheel boots it as "pending";
// it's kept once it links to the hub, and if it crashes, or can't reach the
// hub within OTA_PROBATION_MS, the bootloader goes back to the previous one.
// Firmware flashed over USB isn't pending, so none of this applies to it.
extern "C" bool verifyRollbackLater() { return true; }

const unsigned long OTA_PROBATION_MS = 60000;
bool otaActive = false;
uint32_t otaSize = 0, otaNext = 0;
int8_t otaShownPercent = -1;
bool otaPendingVerify = false;

void otaReply(uint8_t status) {
  LinkOtaAck a = {linkHeader(LINK_OTA_ACK), status, otaNext};
  esp_now_send(hubMac, (const uint8_t*)&a, sizeof(a));
}

void otaDrawProgress(const char* line1, int percent) {
  tft.fillScreen(ST7735_BLACK);
  tft.setTextColor(ST7735_WHITE);
  tft.setTextSize(1);
  tft.setCursor((SCREEN_W - (int16_t)strlen(line1) * 6) / 2, 44);
  tft.print(line1);
  if (percent >= 0) {
    char buf[8];
    snprintf(buf, sizeof(buf), "%d%%", percent);
    tft.setTextSize(3);
    tft.setCursor((SCREEN_W - (int16_t)strlen(buf) * 18) / 2, 62);
    tft.print(buf);
  }
}

bool carMoving() {
  return millis() - lastTelemetryMs < 2000 && latestTelemetry.speedKph > 2;
}

void otaHandle(const uint8_t* data, int len) {
  const LinkHeader* h = (const LinkHeader*)data;
  if (h->type == LINK_OTA_BEGIN && len >= (int)sizeof(LinkOtaBegin)) {
    const LinkOtaBegin* b = (const LinkOtaBegin*)data;
    if (otaActive && b->size == otaSize) { otaReply(OTA_OK); return; }   // our answer got lost
    if (carMoving()) { otaReply(OTA_DRIVING); return; }
    if (otaActive) Update.abort();
    otaNext = 0;
    char md5[33];
    memcpy(md5, b->md5, 32);
    md5[32] = 0;
    otaActive = Update.begin(b->size) && Update.setMD5(md5);
    if (!otaActive) { Update.abort(); otaReply(OTA_BEGIN_FAILED); return; }
    otaSize = b->size;
    otaShownPercent = 0;
    currentScreen = SCREEN_STANDBY;   // back to standby once it's over
    otaDrawProgress("Updating firmware", 0);
    otaReply(OTA_OK);
  } else if (h->type == LINK_OTA_DATA && len >= (int)sizeof(LinkHeader) + 5) {
    if (!otaActive) { otaReply(OTA_NOT_STARTED); return; }
    const LinkOtaData* d = (const LinkOtaData*)data;
    uint8_t n = min((int)d->len, len - (int)sizeof(LinkHeader) - 5);
    if (d->offset == otaNext && n > 0) {
      if (Update.write((uint8_t*)d->data, n) != n) {
        Update.abort();
        otaActive = false;
        otaReply(OTA_WRITE_FAILED);
        return;
      }
      otaNext += n;
      int percent = (int)((uint64_t)otaNext * 100 / otaSize);
      if (percent / 5 != otaShownPercent / 5) {
        otaShownPercent = percent;
        otaDrawProgress("Updating firmware", percent);
      }
    }
    otaReply(OTA_OK);   // a repeat or an out-of-order chunk just gets told where to continue
  } else if (h->type == LINK_OTA_END) {
    if (!otaActive) { otaReply(Update.isFinished() ? OTA_DONE : OTA_NOT_STARTED); return; }
    otaActive = false;
    if (otaNext != otaSize || !Update.end()) {
      USBSerial.printf("OTA: verify failed (%s)\n", Update.errorString());
      Update.abort();
      otaReply(OTA_VERIFY_FAILED);
      otaDrawProgress("Update failed", -1);
      delay(1500);
      standbyScreenInit();
      return;
    }
    otaReply(OTA_DONE);
    otaDrawProgress("Update done", 100);
    delay(500);   // let the answer go out
    ESP.restart();
  } else if (h->type == LINK_OTA_ABORT) {
    if (otaActive) Update.abort();
    otaActive = false;
    otaReply(OTA_OK);
    standbyScreenInit();
  }
}

void otaProbationBegin() {
  esp_ota_img_states_t state;
  otaPendingVerify = esp_ota_get_state_partition(esp_ota_get_running_partition(), &state) == ESP_OK &&
                     state == ESP_OTA_IMG_PENDING_VERIFY;
  if (otaPendingVerify) USBSerial.println("BOOT: new firmware on probation until the hub link is up");
}

void otaProbationService() {
  if (!otaPendingVerify) return;
  if (hubLinkOk) {
    esp_ota_mark_app_valid_cancel_rollback();
    otaPendingVerify = false;
  } else if (millis() > OTA_PROBATION_MS) {
    esp_ota_mark_app_invalid_rollback_and_reboot();
  }
}

void linkService() {
  unsigned long now = millis();
  RxPacket p;
  while (xQueueReceive(rxQueue, &p, 0) == pdTRUE) {
    const LinkHeader* h = (const LinkHeader*)p.data;
    bool fromHub = h->type == LINK_HELLO_ACK || h->type == LINK_DASH || h->type == LINK_HEARTBEAT ||
                   (h->type >= LINK_OTA_BEGIN && h->type <= LINK_OTA_ABORT);
    if (!fromHub) continue;
    if (!hubKnown || memcmp(p.mac, hubMac, 6) != 0) {
      memcpy(hubMac, p.mac, 6);
      hubKnown = true;
      ensurePeer(hubMac);
    }
    lastHubMs = now;
    if (h->type == LINK_DASH && p.len >= sizeof(LinkHeader) + 1 && !otaActive) {
      const LinkDash* d = (const LinkDash*)p.data;
      uint8_t n = min((int)d->len, p.len - (int)sizeof(LinkHeader) - 1);
      for (uint8_t i = 0; i < n; i++) radioParser.feed(d->data[i]);
    } else if (h->type >= LINK_OTA_BEGIN && h->type <= LINK_OTA_ABORT) {
      otaHandle(p.data, p.len);
    }
  }
  hubLinkOk = hubKnown && now - lastHubMs < LINK_HUB_TIMEOUT_MS;
  if (otaActive && !hubLinkOk) {   // the hub went away mid-update
    Update.abort();
    otaActive = false;
    standbyScreenInit();
  }

  // Until a hub answers (or after it goes quiet), say hello to anyone
  // listening; once linked, repeat it to the hub now and then so it always
  // knows this wheel's firmware version.
  static unsigned long lastHelloMs = 0;
  if (now - lastHelloMs >= (hubLinkOk ? LINK_HELLO_LINKED_MS : LINK_HELLO_MS)) {
    LinkHello hello = {linkHeader(LINK_HELLO), LINK_DEVICE_WHEEL, 1, WHEEL_FW_VERSION, BUTTON_COUNT, LINK_CAP_DISPLAY};
    esp_now_send(hubLinkOk ? hubMac : BROADCAST, (const uint8_t*)&hello, sizeof(hello));
    lastHelloMs = now;
  }

  // Buttons: on every change, and repeated so a lost packet corrects itself.
  static uint32_t sentButtons = 0xFFFFFFFF;
  static unsigned long lastButtonsMs = 0;
  static uint8_t seq = 0;
  if (hubKnown && (wheelButtons != sentButtons || now - lastButtonsMs >= LINK_BUTTON_REPEAT_MS)) {
    LinkButtons b = {linkHeader(LINK_BUTTONS), seq++, wheelButtons};
    esp_now_send(hubMac, (const uint8_t*)&b, sizeof(b));
    sentButtons = wheelButtons;
    lastButtonsMs = now;
  }
}

// ==========================================
//   BUTTON READING (matrix, see BUTTONS above)
// ==========================================
void matrixBegin() {
  for (uint8_t r = 0; r < ROW_COUNT; r++) pinMode(ROW_PINS[r], INPUT);   // floating until scanned
  for (uint8_t c = 0; c < COL_COUNT; c++) pinMode(COL_PINS[c], INPUT_PULLUP);
}

uint32_t matrixScan() {
  uint32_t pressed = 0;
  for (uint8_t r = 0; r < ROW_COUNT; r++) {
    pinMode(ROW_PINS[r], OUTPUT);
    digitalWrite(ROW_PINS[r], LOW);
    delayMicroseconds(10);   // let the column lines settle through the pull-ups
    for (uint8_t c = 0; c < COL_COUNT; c++) {
      if (digitalRead(COL_PINS[c]) == LOW) pressed |= 1UL << (r * COL_COUNT + c);
    }
    pinMode(ROW_PINS[r], INPUT);
  }
  return pressed;
}

void readButtons() {
  static uint32_t candidate = 0;
  static uint8_t stableReads = 0;
  uint32_t pins = matrixScan();
  if (pins == candidate) {
    if (stableReads < BUTTON_DEBOUNCE_READS) stableReads++;
  } else {
    candidate = pins;
    stableReads = 1;
  }
  if (stableReads >= BUTTON_DEBOUNCE_READS) wheelButtons = candidate;
}

void setup() {
  pinMode(TFT_LED, OUTPUT);
  digitalWrite(TFT_LED, LOW);                // backlight off until the logo is drawn
  SPI.begin(TFT_SCK, -1, TFT_MOSI);          // display never uses MISO
  matrixBegin();

  // Display proof-of-life BEFORE anything USB-related: the CDC-on-boot
  // build's cold-boot failure left a white (uninitialized) screen with no
  // way to tell how far boot got. Now a white screen can only mean setup()
  // itself never ran (or the panel/wiring died) - anything USB-related
  // failing later leaves this text visible instead.
  tft.initR(INITR_144GREENTAB);
  tft.setRotation(0);
  tft.fillScreen(ST7735_BLACK);
  tft.setTextColor(ST7735_WHITE);
  tft.setTextSize(1);
  tft.setCursor(4, 4);
  tft.println("booting...");

  // Must be set before USB.begin() - and with CDCOnBoot Disabled it CAN be:
  // the CDC-on-boot build started USB in app_main() before setup(), so these
  // calls never took effect.
  USB.manufacturerName("Sim Rig");
  USB.productName("Volgas Wheel");
  USBSerial.begin(115200);
  // Already true by default (USBCDC's constructor), made explicit here since
  // a future PC companion app relies on it: opening this CDC port at 1200
  // baud triggers USBCDC::_onLineCoding() to call usb_persist_restart(),
  // rebooting the board into the ROM bootloader - no physical BOOT+RESET
  // needed. Confirmed working end-to-end 2026-07-25: touch at 1200 baud ->
  // board re-enumerates as the bootloader's USB-Serial/JTAG interface (same
  // VID:PID 303A:1001, but MI_00 serial + MI_02 JTAG instead of the app's
  // own interfaces (HID + CDC until 2026-11-13, CDC only since the gamepad
  // moved to the hub) - that interface-layout difference is how a future
  // app should tell "still the app" from "now the bootloader" apart) ->
  // esptool can flash new firmware there -> esptool's own hard reset at the
  // end boots straight back into the (new) app.
  //
  // A future PC app doesn't need to drive this manually - arduino-cli (and
  // the esp32 core's own upload tooling) already has a generic "1200bps
  // touch + wait for the new port" implementation, gated by the FQBN's
  // UploadMode menu option (boards.txt: esp32s3.menu.UploadMode.cdc sets
  // upload.use_1200bps_touch=true / upload.wait_for_upload_port=true;
  // UploadMode.default - the plain FQBN's implicit choice - leaves both
  // false, which is why upload attempts without this option require the
  // manual BOOT+RESET dance). Confirmed working fully automatically
  // 2026-07-25, board left running normally on its CDC port beforehand,
  // no button presses at all:
  //   arduino-cli compile --fqbn esp32:esp32:esp32s3:PSRAM=opi,FlashSize=8M,PartitionScheme=default_8MB,UploadMode=cdc \
  //     esp32_dash --upload -p <app's current CDC COM port>
  USBSerial.enableReboot(true);
  USB.begin();

  linkBegin();
  otaProbationBegin();
  showSplashScreen();
  standbyScreenInit();
  standbyScreenUpdate(hubLinkOk);
  USBSerial.println("BOOT: display ready, entering main loop");
}

void loop() {
  readButtons();
  linkService();
  otaProbationService();
  if (otaActive) {   // the update's progress screen owns the display
    delay(1);
    return;
  }
  pollTelemetry();

  // Collapse the lap-time footer again once lap-time messages stop (e.g.
  // switching from rFactor 2 to Forza, which sends none). This check was
  // described above setFooterActive() but missing, so the footer stayed
  // reserved until the next return to standby.
  if (currentScreen == SCREEN_CLASSIC && footerActive && millis() - lastLapTimeMsgMs > FOOTER_DATA_TIMEOUT_MS) {
    setFooterActive(false);
  }

  if (currentScreen == SCREEN_CLASSIC && millis() - lastTelemetryMs > TELEMETRY_TIMEOUT_MS) {
    currentScreen = SCREEN_STANDBY;
    standbyScreenInit();
  }
  if (currentScreen == SCREEN_STANDBY) {
    standbyScreenUpdate(hubLinkOk);
  }

  delay(2);
}
