# Run Modes and Services

This guide shows how to run the rover in two separate modes:

- Web mode: `app.py`
- Controller-only mode: `pscontroller/main.py`

Use one mode at a time. Do not run both services together.

## Prerequisites

On Raspberry Pi, from project root:

```bash
cd ~/4tronix-mars-rover
python3 -m venv env
source env/bin/activate
python -m pip install -r requirements.txt
```

## Mode 1: Web App Service (`app.py`)

Create service file:

```bash
sudo tee /etc/systemd/system/rover-web.service > /dev/null <<'EOF'
[Unit]
Description=4tronix Rover Web App
After=network-online.target bluetooth.service
Wants=network-online.target

[Service]
Type=simple
User=rover
WorkingDirectory=/home/rover/4tronix-mars-rover
ExecStart=/home/rover/4tronix-mars-rover/env/bin/python /home/rover/4tronix-mars-rover/app.py
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF
```

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable rover-web.service
sudo systemctl start rover-web.service
```

Check status and logs:

```bash
sudo systemctl status rover-web.service
journalctl -u rover-web.service -f
```

## Mode 2: Controller-Only Service (`pscontroller/main.py`)

Create service file:

```bash
sudo tee /etc/systemd/system/rover-controller.service > /dev/null <<'EOF'
[Unit]
Description=4tronix Rover Bluetooth Controller Mode
After=bluetooth.service network-online.target
Wants=network-online.target

[Service]
Type=simple
User=rover
WorkingDirectory=/home/rover/4tronix-mars-rover
ExecStart=/home/rover/4tronix-mars-rover/env/bin/python /home/rover/4tronix-mars-rover/pscontroller/main.py
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF
```

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable rover-controller.service
sudo systemctl start rover-controller.service
```

Check status and logs:

```bash
sudo systemctl status rover-controller.service
journalctl -u rover-controller.service -f
```

## Switching Modes

Switch from web mode to controller mode:

```bash
sudo systemctl stop rover-web.service
sudo systemctl start rover-controller.service
```

Switch from controller mode to web mode:

```bash
sudo systemctl stop rover-controller.service
sudo systemctl start rover-web.service
```

## Manual Bluetooth Controller Pairing

Open bluetoothctl:

```bash
sudo bluetoothctl
```

Then run:

```text
power on
agent on
default-agent
scan on
```

Put controller in pairing mode:

- PS4 DualShock: hold `Share + PS`
- PS5 DualSense: hold `Create + PS`

When you see a line like:

```text
Device XX:XX:XX:XX:XX:XX Wireless Controller
```

Pair/connect:

```text
pair XX:XX:XX:XX:XX:XX
trust XX:XX:XX:XX:XX:XX
connect XX:XX:XX:XX:XX:XX
scan off
info XX:XX:XX:XX:XX:XX
quit
```

You want to see `Connected: yes` in the `info` output.
