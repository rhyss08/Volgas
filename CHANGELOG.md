# Changelog

## 0.3.1 (2026-11-13)

### Interface
- Sharp on laptops with display scaling (125%, 150%...): Volgas now handles the scaling itself instead of letting Windows stretch the window, which made everything fuzzy. Text, controls, gauges, curves, the wheel photo and the dash preview are drawn at the screen's real resolution, at the same size as before. At 100% nothing changes. `VOLGAS_UI_SCALE=1.5` (environment variable) previews the scaled layout on any computer, e.g. with `tools/simulate.py`.
- New installer, `VolgasSetup.exe`: installs for your Windows account without admin rights, adds a Start menu entry, and lists Volgas under Settings → Apps with an Uninstall button. The in-app updater keeps working, and the installed-apps entry follows its updates. `Volgas.exe` on its own still works.

### Hub and wheel (firmware: needs flashing over USB once)
- New hub (`hub_s3`, an ESP32-S3 on the PC's USB) and a wheel that talks to it wirelessly (ESP-NOW, `esp32_dash`, the old dash board moved onto the wheel). Volgas sends the dash data to the hub exactly as before; the hub forwards it to the wheel's display. The wheel's buttons go to the hub, which shows up in Windows as the game controller "Volgas Hub" (12 buttons in a 4x3 diode matrix; not wired or tested yet).
- The wheel can be updated over the radio, with no cable: it writes the new firmware into a spare slot, checks it, and goes back to the previous version by itself if the new one doesn't connect to the hub.
- The display backlight is on its own pin (GPIO7) and only switches on once the logo is drawn, which removes the white flash at power-on. The standby screen shows "hub: lost" only while the link is down, and the clock stays centred.
- The old nRF24 receiver and the dash's own USB game controller are gone from the wheel firmware.

### Device updates
- Settings now has two sections, switched at the top: **Volgas** (the app's own update and the support info) and **Device updates**, with a card for the dash, the hub, the pedals and the wheelbase. Each card shows what's installed and the result of its last check, with its own **Check** and **Update** buttons, plus **Check all** at the top. The Settings item in the left bar shows a dot when the app or a device has an update.
- Checks read a `firmware.json` published with the latest GitHub release (version, file and SHA-256 per device); a release without one means no firmware has been published yet.
- The dash updates wirelessly through the hub, from the release or from a .bin file (From file...). Volgas pauses the dash feed for the update and shows the progress on the card. The pedals, hub and wheelbase have their cards and checks; updating them from Volgas comes later.
- Link status only shows while a device is disconnected: the title-bar pills (dash, hub, pedals, wheelbase), the Home card pills and the wheel's radio link pill are hidden when everything is connected. There's a new Hub pill, and Volgas now asks the hub for the wheel's link and firmware version.

### Dash
- Assetto Corsa support: speed, gear, RPM and the rev limit, plus lap time and a live delta to your best lap, read from the game's own shared memory (nothing to set up in the game). A lap with more than two tyres off the track shows as invalid.
- Lap time and delta (rFactor 2 and Assetto Corsa) appear once the game has counted the first completed lap: the out lap isn't shown, and neither is the "lap" timer on maps without a start/finish line (e.g. No Hesi), which only counts time since joining.

### Wheelbase (FFBeast)
- FFBeast support is back: the Wheelbase page is shown again, finds the wheelbase automatically, and the wheel photos turn with the real wheel. A title bar pill shows whether it's connected.
- The wheelbase is the source of truth: on connect Volgas shows the board's own settings instead of pushing remembered ones onto it (a new board would otherwise get the old board's values).
- New Forces switch on the Wheelbase page.
- Home's wheel card shows the steering angle and has Steering Range and Power sliders with 360°/540°/900° presets, kept in step with the Wheelbase page. The explanatory lines under the Home cards are gone.
- The wheel photo is only redrawn when the angle changes, with a cheaper rotation while it moves; a large window no longer makes Volgas lag. Home's Pedals and Dash cards are drawn on one canvas each, so resizing is quicker.
- Every change is read back from the board; a change the board refuses springs back with a notice (on FFBeast RC.26.1.1 in free mode, the three DirectX strengths are locked).
- The page says when there are changes that haven't been saved to the wheelbase yet.
- Tested against an ODESC 4.2 on FFBeast RC.26.1.1.

## 0.3.0 (2026-11-11)

Phases 0 and 1 of the improvement plan (safety net and reliability), plus the first part of the new interface.

### Interface
- New layout: navigation down the left (Home, Pedals, Dash, Settings at the bottom), page name and live status in the title bar.
- New **Home** page: the steering wheel photo fills the left side; the pedals (live throttle and brake) and the dash (live preview, current game) share the right. Every card shows its device's real connection state.
- Title bar pills always show whether the pedals and dash are connected and which game is running.
- Pop-up warnings replaced by non-blocking notices in the window corner. The update confirmation stays a dialog because it needs a decision.
- Pedal curves: point labels no longer clipped at the top or overlapping the axis; a ring shows where each pedal is on its curve right now.
- Calibration wizard rebuilt: "Step 1 of 2", averaged captures, a progress bar, clear reasons when a capture is rejected, Enter and Esc.
- Design tokens (`volgas_theme.py`) and shared components (`volgas_ui.py`): buttons with hover, pressed, focus and disabled states; status pills; notices; the navigation rail. Secondary text contrast raised from 2.9:1 to 4.7:1 or better; cards separate more clearly from the background.
- Wheelbase page hidden until wheelbase support exists; Settings → Support can show it. Saved wheelbase values are kept.
- Splash shortened from 3 s to 1.5 s; the window opens at a roomier default size.
- Settings → Support shows where files are kept and opens the log folder.

### Reliability
- Closing the window now stops the dash bridge and closes the pedal port cleanly.
- Each pedal connection has its own reader thread and stop signal; a fast unplug and replug can no longer leave two readers on one port.
- Volgas no longer sends a curve or calibration change before it has read the pedals' own calibration, which could have overwritten it with defaults.
- Calibration is checked before it's written: too little travel, or a pedal that moved during a capture, is rejected with an explanation.
- Settings moved to the user's data folder (`%APPDATA%\Volgas` on Windows), saved atomically, with damaged files set aside instead of silently replaced. Old settings next to the exe are copied over once and left in place.
- The updater puts the old exe back if installing the new one fails half way.
- The dash bridge logs unexpected errors and reconnects instead of stopping silently.
- Logging to a rotating file, including errors in background threads and interface callbacks.

### Firmware (needs compiling and flashing, not yet tested on hardware)
- Dash: the lap-time footer now collapses 1.5 s after lap-time messages stop, as its own comments describe.
- Pedals: a zero-width calibration range no longer divides by zero.
- Pedals: calibration saves clear the "valid" marker first and set it last, so a save interrupted by a power cut can't load as a mix of old and new values. Same storage layout; existing saved calibrations still load.

### Development
- `tools/simulate.py` runs the app with a simulated pedal board, dash and Forza game, on Windows, macOS or Linux.
- 32 automated tests (`pc_app/tests`): protocol framing and agreement with the firmware header, calibration checks, settings, the updater's rollback, Forza packet decoding.
- `requirements.txt`, `requirements-dev.txt`, and a CI workflow that tests on Windows and Linux and builds `Volgas.exe`.
- UPX compression turned off in `Volgas.spec` to reduce antivirus false alarms.
- Pedal protocol code moved to `volgas_pedal_protocol.py` so it can be tested on its own.
