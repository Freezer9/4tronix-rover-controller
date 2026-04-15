# 4tronix M.A.R.S. Rover

## Hardware Setup

1. Assemble the 4tronix M.A.R.S Rover as per the instructions on the 4tronix website.
2. Install the Raspberry Pi operating system for the P Zero W with the Raspberry Pi Imager (Legacy Edition), with SSH enabled, with your WIFI network configured, with your own choice of hostname, user and password.

## Software Overview

This repository contains the control software for a 4tronix M.A.R.S. Rover on Raspberry Pi.
It provides:

- a rover hardware library for GPIO, servos, motors, LEDs, sonar, and EEPROM calibration data
- an interactive servo calibration tool
- a web-based rover controller in `app.py`
- PlayStation controller support over Bluetooth that auto-pairs when the controller is seen on the Pi

The current goal is manual rover control first, with a clean base for later autonomy work.

## Repository layout

```text
app.py                 Web UI and HTTP API for rover control
modules/roverlib.py    Hardware pin mapping and low-level rover control
modules/servo_calibrate.py
					   Interactive servo calibration utility
pscontroller/          Bluetooth PlayStation controller support
requirements.txt       Python dependencies
```

See [modules/README.md](modules/README.md) for module details and [pscontroller/README.md](pscontroller/README.md) for the controller folder.

Service and manual Bluetooth pairing guide: [RUN_AS_SERVICE.md](RUN_AS_SERVICE.md)

## What each part does

`app.py` starts the rover control server and serves the browser UI.

`modules/roverlib.py` contains the Rover hardware setup and helper functions:

- motor pin configuration
- wheel servo control
- mast servo control
- LED control
- sonar support
- EEPROM-backed servo offsets

`modules/servo_calibrate.py` is the calibration utility. It lets you tune each servo midpoint and both end stops, then saves the offsets back to EEPROM.

`pscontroller/` contains the Bluetooth PlayStation controller integration. It scans for a known controller, pairs and trusts it through `bluetoothctl`, then reads Linux input events through `evdev`.

## Installation

1. Assemble the rover following the 4tronix build guide.
2. Install Raspberry Pi OS on the Pi Zero W or compatible Pi.
3. Enable SSH, Wi-Fi, and your preferred hostname, user, and password during imaging.
4. Boot the Pi, update packages, and enable SPI and I2C in `raspi-config`.
5. Copy this repository to the Pi.
6. Create and activate a virtual environment.
7. Install dependencies from `requirements.txt`.

Example commands:

```bash
python -m venv env
source env/bin/activate
pip install -r requirements.txt
```

## First run

1. Calibrate servos before attaching wheels.
2. Run the calibration tool:

```bash
python3 modules/servo_calibrate.py
```

3. Follow the prompts to select each servo and tune midpoint, right, and left positions.
4. Save the calibration data when finished.
5. Attach the wheels after calibration.
6. Start the rover web app:

```bash
python3 app.py
```

## Notes

- `app.py` can run in mock mode if rover hardware is not present.
- Camera support is optional and depends on `picamera2`.
- Bluetooth controller support is handled by the `pscontroller/` package and can run in web mode or controller-only mode.

## Future work

- Autonomous drive logic
