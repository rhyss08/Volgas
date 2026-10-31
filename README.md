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

- **Main**: live throttle and brake bars, plus the wheel card.
- **Pedals**: live readings, a calibration wizard and response curves. The pedals are found automatically over USB.
- **Wheelbase**: settings sliders. Wheelbase support isn't available yet; it will come with Volgas's own wheelbase firmware.
- **Dash bridge**: runs in the background and sends rFactor 2 or Forza Horizon 5 telemetry to the dash, whichever game is running.
- **Settings**: click the Volgas logo in the title bar. This shows the version and checks for and installs updates.

## Download

Get `Volgas.exe` from the [latest release](https://github.com/rhyss08/Volgas/releases/latest)
and run it. There's no installer, and Volgas updates itself from the Settings page.

The exe isn't code-signed, so Windows SmartScreen may say "Windows protected your PC".
Click **More info → Run anyway**.

## Building from source

Windows, Python 3.12:

```
pip install pyserial pillow certifi pyinstaller
cd pc_app
python -m PyInstaller --onefile --windowed --name Volgas --icon assets/volgas.ico --add-data "assets/volgas_logo.png;assets" --add-data "assets/volgas.ico;assets" --add-data "assets/volgas_wheel.png;assets" volgas.py
```

`python volgas.py` also runs it directly. The firmware folders are Arduino sketches.

## Licence

Volgas is released under the [GNU General Public License v3](LICENSE).

`esp32_dash` and `wheel_tx` use the [RF24](https://github.com/nRF24/RF24) library,
which is licensed separately under GPL v2.
