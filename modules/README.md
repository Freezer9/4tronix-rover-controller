# Modules

This folder holds the low-level rover code used by the web app and tools.

## roverlib.py

`roverlib.py` is the hardware layer. It defines the rover pin map and control helpers for:

- wheel motor control
- wheel servo positioning
- mast servo positioning
- LED control
- sonar distance reading
- EEPROM storage for servo calibration offsets

This file is the main place to look when you want to understand how the rover hardware is wired.

## servo_calibrate.py

`servo_calibrate.py` is the interactive calibration utility.

It lets you:

- select each servo in turn
- adjust midpoint, right stop, and left stop positions
- save offsets to EEPROM for later use by `roverlib.py`

Run it from the repository root with:

```bash
python3 modules/servo_calibrate.py
```

## Layout note

If you add more hardware helpers later, keep them in this folder and document them here so the split between hardware code and user-facing control code stays clear.
