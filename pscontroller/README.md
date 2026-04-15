# PS Controller

This package handles PlayStation controller support over Bluetooth.

## What it does

- scans for a controller on the Raspberry Pi
- pairs, trusts, and connects to the controller through `bluetoothctl`
- reads controller input through Linux `evdev`
- translates controller input into rover drive commands
- exposes controller status to the web UI

## Files

- `main.py` contains the controller service (`PSControllerService`) and can also run standalone.
- `__init__.py` exposes `PSControllerService` for imports from other modules.

## Behavior

When the rover web app starts, the controller service keeps looking for a known PlayStation controller name.
If it finds one, it tries to pair and connect automatically and then shows the connection state in the GUI.

You can also run controller-only mode directly:

```bash
python pscontroller/main.py
```
