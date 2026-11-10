# Changelog

## Unreleased

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
