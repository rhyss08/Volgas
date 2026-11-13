# Volgas

Volgas is the Windows app and firmware for a DIY sim-racing rig: pedals, a button
box steering wheel and a small dash display.

## What's here

| Folder | What it is |
| --- | --- |
| `pc_app/` | The Volgas Windows app (Python / Tk), built into a single `Volgas.exe` |
| `pedal_set/` | Pedal firmware for an STM32 "Blue Pill": hall-sensor throttle, HX711 load-cell brake, USB joystick + serial |
| `esp32_dash/` | ESP32-S3 dash: ST7735 display, wheel-button receiver (nRF24) and USB gamepad |
| `wheel_tx/` | Arduino Nano in the steering wheel: 12-button matrix sent over nRF24 to the dash |

### The app

- **Home**: the whole rig at a glance - the steering wheel, live throttle and brake, and the dash with the game it's showing. Every card shows whether its device is connected.
- **Pedals**: live readings, a guided calibration wizard and response curves, with a marker showing where each pedal is on its curve right now. The pedals are found automatically over USB.
- **Dash**: a live picture of what the dash screen is showing.
- **Dash bridge**: runs in the background and sends rFactor 2, Assetto Corsa or Forza Horizon 5 telemetry to the dash, whichever game is running.
- **Settings** (bottom of the left bar): the app's version and updates, where Volgas keeps its settings and log files, and a **Device updates** section with firmware for each device (dash, hub, pedals, wheelbase) and a check for each and for all of them. The dash updates wirelessly through the hub; the others come later.
- **Wheelbase**: settings for an FFBeast wheelbase - forces on/off, rotation range, effect strengths, gains and power limit - plus Set Centre and the live wheel angle. Changes apply straight away; **Save & Reboot** stores them on the wheelbase. Settings → Support can hide the page.

The title bar always shows whether the pedals and dash are connected and which game is running.

Settings and logs live in `%APPDATA%\Volgas` (Windows). Settings from older versions, kept next to `Volgas.exe`, are copied over automatically the first time. If something goes wrong, Settings → Support → **Open log folder** has the log to attach to a bug report.

## Download

Get `VolgasSetup.exe` from the [latest release](https://github.com/rhyss08/Volgas/releases/latest)
and run it. It installs Volgas for your Windows account only (no admin rights needed), adds it
to the Start menu, and lists it under Settings → Apps so it can be uninstalled like any other
app. Volgas then updates itself from the Settings page. Uninstalling keeps your settings in
`%APPDATA%\Volgas`.

`Volgas.exe` on its own still works too: put it anywhere and run it.

Neither file is code-signed, so Windows SmartScreen may say "Windows protected your PC".
Click **More info → Run anyway**.

## Building from source

Windows, Python 3.12:

```
cd pc_app
pip install -r requirements-dev.txt
python -m pytest -q tests
python -m PyInstaller Volgas.spec
```

`python volgas.py` also runs it directly. The firmware folders are Arduino sketches.

To build the installer as well, install [Inno Setup 6.3+](https://jrsoftware.org/isinfo.php)
and run, after PyInstaller (use the version in `volgas.py`):

```
iscc /DAppVersion=0.3.0 installer\Volgas.iss
```

It writes `dist\VolgasSetup.exe`.

### Running without the hardware

`python tools/simulate.py` starts Volgas with a simulated pedal board, dash and Forza game, on Windows, macOS or Linux. Options: `--no-pedals`, `--no-dash`, `--no-game`, and `--unplug 20` to pull the pedals out after 20 seconds and plug them back in 10 seconds later. It uses a temporary settings folder unless `--keep-settings` is given. Simulated runs check the app's own logic and screens, not the real hardware's timing.

### Automated checks

Every push runs the tests on Windows and Linux and builds `Volgas.exe` and `VolgasSetup.exe` (see `.github/workflows/ci.yml`); both, plus the exe's SHA-256 file, are attached to the run as an artifact.

## Licence

Volgas is released under the [GNU General Public License v3](LICENSE).

`esp32_dash` and `wheel_tx` use the [RF24](https://github.com/nRF24/RF24) library,
which is licensed separately under GPL v2.

The Dashboard tab's preview uses the 5x7 font from the
[Adafruit GFX Library](https://github.com/adafruit/Adafruit-GFX-Library),
Copyright (c) 2012 Adafruit Industries, under the BSD licence. The full notice is in
[`pc_app/volgas_dash_font.py`](pc_app/volgas_dash_font.py).
