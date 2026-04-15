#!/usr/bin/env python3

from __future__ import annotations

import select
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, Optional

try:
    from evdev import InputDevice, ecodes, list_devices
    EVDEV_AVAILABLE = True
except ImportError:
    EVDEV_AVAILABLE = False
    InputDevice = None
    ecodes = None
    list_devices = None


_CONTROLLER_NAME_PATTERNS = (
    "Wireless Controller",
    "DUALSHOCK",
    "DualShock",
    "DualSense",
    "PLAYSTATION(R)3 Controller",
    "Sony Interactive Entertainment Wireless Controller",
)


@dataclass
class ControllerStatus:
    available: bool = False
    enabled: bool = False
    connected: bool = False
    discovered: bool = False
    paired: bool = False
    name: str = ""
    address: str = ""
    device_path: str = ""
    source: str = "none"
    last_command: str = ""
    last_event: str = ""
    message: str = "Controller support unavailable"
    last_seen: float = 0.0
    devices: list[dict[str, Any]] = field(default_factory=list)


class PSControllerService:
    def __init__(
        self,
        command_callback: Callable[[str, Optional[int], str], Any],
        poll_interval: float = 2.0,
        scan_duration: float = 6.0,
    ) -> None:
        self._command_callback = command_callback
        self._poll_interval = poll_interval
        self._scan_duration = scan_duration
        self._bluetoothctl = shutil.which("bluetoothctl")
        self._stop_event = threading.Event()
        self._rescan_event = threading.Event()
        self._lock = threading.Lock()
        self._device: Optional[Any] = None
        self._device_key = ""
        self._hat_x = 0
        self._hat_y = 0
        self._status = ControllerStatus()
        self._status.devices = []

        if EVDEV_AVAILABLE and self._bluetoothctl:
            self._status.available = True
            self._status.enabled = True
            self._status.message = "Waiting for Bluetooth controller"
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
        else:
            if not EVDEV_AVAILABLE:
                self._status.message = "evdev is not installed"
            elif not self._bluetoothctl:
                self._status.message = "bluetoothctl is not available"
            self._status.enabled = False
            self._thread = None

    def stop(self) -> None:
        self._stop_event.set()
        self._close_device()

    def request_rescan(self) -> None:
        self._rescan_event.set()

    def refresh_devices(self) -> list[dict[str, Any]]:
        devices = self._scan_for_devices()
        self._set_status(
            devices=self._format_devices(devices),
            discovered=bool(devices),
            message=(
                f"Detected {len(devices)} Bluetooth device(s)" if devices else "No Bluetooth device detected"),
        )
        return self.list_devices()

    def list_devices(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._status.devices or [])

    def pair_device(self, address: str) -> bool:
        devices = self._scan_for_devices()
        self._set_status(
            devices=self._format_devices(devices),
            discovered=bool(devices),
            message="Pairing selected Bluetooth device",
            address=address,
        )

        selected_name = self._device_name_for_address(address, devices)
        if not selected_name:
            self._set_status(
                message=f"Device {address} not found in scan list")
            return False

        if not self._pair_trust_connect(address):
            self._set_status(message=f"Failed to pair {selected_name}")
            return False

        time.sleep(1.0)
        found = self._find_connected_device(name=selected_name)
        if found is None:
            found = self._find_connected_device()
        if found is None:
            self._set_status(
                message=f"Paired {selected_name}, waiting for input device")
            return True

        self._attach_device(*found, address=address, name=selected_name)
        return True

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return asdict(self._status)

    def _set_status(self, **updates: Any) -> None:
        with self._lock:
            for key, value in updates.items():
                setattr(self._status, key, value)

    def _run(self) -> None:
        while not self._stop_event.is_set():
            if self._device is None:
                self._discover_and_attach()
                time.sleep(self._poll_interval)
                continue

            try:
                for event in self._device.read_loop():
                    if self._stop_event.is_set():
                        return
                    self._handle_event(event)
                    if self._rescan_event.is_set():
                        self._rescan_event.clear()
                        break
            except Exception:
                self._close_device()
                self._set_status(
                    connected=False,
                    device_path="",
                    last_event="",
                    message="Controller disconnected",
                )
                time.sleep(self._poll_interval)

    def _discover_and_attach(self) -> None:
        if self._rescan_event.is_set():
            self._rescan_event.clear()

        already_connected = self._find_connected_device()
        if already_connected is not None:
            self._attach_device(*already_connected)
            return

        devices = self._scan_for_devices()
        self._set_status(
            devices=self._format_devices(devices),
            discovered=bool(devices),
            message=(
                "Detected Bluetooth devices" if devices else "Waiting for Bluetooth controller"),
        )

    def _scan_for_devices(self) -> list[tuple[str, str]]:
        devices: list[tuple[str, str]] = []
        bluetooth_devices = self._bluetoothctl_run(["devices"])
        devices.extend(self._parse_bluetooth_devices(bluetooth_devices))
        if devices:
            return devices

        process = subprocess.Popen(
            [self._bluetoothctl],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        try:
            assert process.stdin is not None
            assert process.stdout is not None
            process.stdin.write("power on\nagent on\ndefault-agent\nscan on\n")
            process.stdin.flush()

            deadline = time.time() + self._scan_duration
            buffer: list[str] = []
            while time.time() < deadline and not self._stop_event.is_set():
                ready, _, _ = select.select([process.stdout], [], [], 0.5)
                if not ready:
                    continue
                line = process.stdout.readline()
                if not line:
                    continue
                buffer.append(line.strip())

            process.stdin.write("scan off\nquit\n")
            process.stdin.flush()
            process.wait(timeout=5)
            devices.extend(self._parse_bluetooth_devices(buffer))
        except Exception:
            try:
                process.kill()
            except Exception:
                pass

        unique: dict[str, str] = {}
        for address, name in devices:
            unique[address] = name
        return [(address, unique[address]) for address in unique]

    def _format_devices(self, devices: list[tuple[str, str]]) -> list[dict[str, Any]]:
        connected_address = self._status.address
        return [
            {
                "address": address,
                "name": name,
                "connected": bool(connected_address and address == connected_address),
                "paired": True,
                "known_controller": self._is_controller_name(name),
            }
            for address, name in devices
        ]

    def _device_name_for_address(self, address: str, devices: list[tuple[str, str]]) -> str:
        for device_address, name in devices:
            if device_address == address:
                return name
        return ""

    def _parse_bluetooth_devices(self, lines: list[str]) -> list[tuple[str, str]]:
        devices: list[tuple[str, str]] = []
        for line in lines:
            if isinstance(line, bytes):
                line = line.decode(errors="ignore")
            text = str(line).strip()
            if text.startswith("Device "):
                parts = text.split(maxsplit=2)
                if len(parts) >= 3:
                    address = parts[1]
                    name = parts[2]
                    devices.append((address, name))
        return devices

    def _bluetoothctl_run(self, args: list[str]) -> list[str]:
        if not self._bluetoothctl:
            return []
        try:
            completed = subprocess.run(
                [self._bluetoothctl, *args],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        except Exception:
            return []
        output = completed.stdout.splitlines() if completed.stdout else []
        output.extend(completed.stderr.splitlines()
                      if completed.stderr else [])
        return output

    def _pair_trust_connect(self, address: str) -> bool:
        self._bluetoothctl_run(["power", "on"])
        self._bluetoothctl_run(["agent", "on"])
        self._bluetoothctl_run(["default-agent"])
        pair_output = self._bluetoothctl_run(["pair", address])
        trust_output = self._bluetoothctl_run(["trust", address])
        connect_output = self._bluetoothctl_run(["connect", address])

        joined = "\n".join(pair_output + trust_output + connect_output).lower()
        success_tokens = (
            "pairing successful",
            "connection successful",
            "already connected",
            "paired: yes",
            "connected: yes",
            "trust succeeded",
        )
        paired = any(token in joined for token in success_tokens)
        self._set_status(
            paired=paired, message="Connecting to Bluetooth controller")
        return paired

    def _find_connected_device(
        self,
        name: Optional[str] = None,
    ) -> Optional[tuple[Any, str]]:
        if not EVDEV_AVAILABLE:
            return None

        candidates: list[tuple[int, Any, str]] = []

        for device_path in list_devices():
            try:
                device = InputDevice(device_path)
            except Exception:
                continue

            if name is not None and device.name != name:
                continue

            if not self._is_controller_name(device.name):
                continue

            candidates.append(
                (self._device_priority(device.name), device, device_path)
            )

        if not candidates:
            return None

        candidates.sort(key=lambda row: row[0], reverse=True)
        _, device, device_path = candidates[0]
        return device, device_path

    def _device_priority(self, device_name: str) -> int:
        name = device_name.lower()
        if "touchpad" in name:
            return -20
        if "motion" in name:
            return -10
        return 10

    def _attach_device(
        self,
        device: Any,
        device_path: str,
        address: str = "",
        name: str = "",
    ) -> None:
        self._device = device
        self._device_key = device_path
        self._hat_x = 0
        self._hat_y = 0
        self._set_status(
            connected=True,
            discovered=True,
            paired=True,
            name=name or getattr(device, "name", "Bluetooth Controller"),
            address=address,
            device_path=device_path,
            source="controller",
            message="Bluetooth controller connected",
            last_seen=time.time(),
        )

    def _close_device(self) -> None:
        device = self._device
        self._device = None
        if device is not None:
            try:
                device.close()
            except Exception:
                pass
        self._device_key = ""

    def _is_controller_name(self, name: str) -> bool:
        upper_name = name.upper()
        return any(pattern.upper() in upper_name for pattern in _CONTROLLER_NAME_PATTERNS)

    def _handle_event(self, event: Any) -> None:
        event_type = event.type
        event_code = event.code
        event_value = event.value

        self._set_status(last_seen=time.time(),
                         last_event=f"{event_type}:{event_code}:{event_value}")

        if event_type == ecodes.EV_ABS:
            self._handle_axis(event_code, event_value)
        elif event_type == ecodes.EV_KEY:
            self._handle_button(event_code, event_value)

    def _handle_axis(self, code: int, value: int) -> None:
        if ecodes is None:
            return

        if code in (ecodes.ABS_HAT0X, ecodes.ABS_X):
            self._hat_x = self._normalize_axis(code, value)
            self._apply_drive_state()
        elif code in (ecodes.ABS_HAT0Y, ecodes.ABS_Y):
            self._hat_y = self._normalize_axis(code, value)
            self._apply_drive_state()

    def _handle_button(self, code: int, value: int) -> None:
        if ecodes is None or value != 1:
            return

        command_map = {
            ecodes.BTN_SOUTH: ("stop", None),
            ecodes.BTN_WEST: ("spin_left", None),
            ecodes.BTN_EAST: ("spin_right", None),
            ecodes.BTN_NORTH: ("forward", None),
            ecodes.BTN_TL: ("speed", -10),
            ecodes.BTN_TR: ("speed", 10),
            ecodes.BTN_SELECT: ("stop", None),
            ecodes.BTN_START: ("forward", None),
        }

        if code in command_map:
            command, extra = command_map[code]
            self._emit_command(command, extra)

    def _normalize_axis(self, code: int, value: int) -> int:
        if ecodes is None:
            return 0

        if code in (ecodes.ABS_HAT0X, ecodes.ABS_HAT0Y):
            if value < 0:
                return -1
            if value > 0:
                return 1
            return 0

        device = self._device
        if device is None:
            return 0

        try:
            abs_info = device.absinfo(code)
        except Exception:
            return 0

        center = (abs_info.max + abs_info.min) / 2
        span = max(center - abs_info.min, abs_info.max - center, 1)
        offset = (value - center) / span
        if abs(offset) < 0.35:
            return 0
        return -1 if offset < 0 else 1

    def _apply_drive_state(self) -> None:
        if self._hat_y == -1:
            self._emit_command("forward")
            return
        if self._hat_y == 1:
            self._emit_command("backward")
            return
        if self._hat_x == -1:
            self._emit_command("arc_left")
            return
        if self._hat_x == 1:
            self._emit_command("arc_right")
            return
        self._emit_command("stop")

    def _emit_command(self, command: str, value: Optional[int] = None) -> None:
        self._set_status(last_command=command, source="controller")
        self._command_callback(command, value, "controller")


def _standalone_drive(rover: Any, state: dict[str, Any]) -> None:
    powerpct = state["direction"] * state["speed"]
    drive_type = state["drive_type"]
    radius_cm = state["radius_cm"]

    rover.obstacleDetected = False
    try:
        rover.changeDrive(drive_type, powerpct, radius_cm)
    except TypeError:
        # Compatibility with legacy roverlib signature changeDrive(driveType, powerpct).
        if drive_type in ("Arc", "Spin") and hasattr(rover, "Ackermandrive"):
            rover.Ackermandrive(drive_type, powerpct, radius_cm)
        else:
            rover.changeDrive(drive_type, powerpct)


def run_standalone() -> int:
    # Allow running via: python pscontroller/main.py from repository root.
    from pathlib import Path
    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    try:
        import modules.roverlib as rover
    except Exception as exc:
        print(f"[ERR] Cannot import rover library: {exc}")
        return 1

    state: dict[str, Any] = {
        "speed": 40,
        "direction": 1,
        "drive_type": "Straight",
        "radius_cm": 200,
        "status": "STOPPED",
    }
    state_lock = threading.Lock()
    stop_event = threading.Event()
    last_cmd_ts = time.time()
    watchdog_s = 2.0

    def handle_control_command(cmd: str, value: Optional[int] = None, source: str = "controller") -> None:
        nonlocal last_cmd_ts
        with state_lock:
            last_cmd_ts = time.time()

            if cmd == "forward":
                state.update(direction=1, drive_type="Straight",
                             radius_cm=200, status="FORWARD")
                _standalone_drive(rover, state)
            elif cmd == "backward":
                state.update(direction=-1, drive_type="Straight",
                             radius_cm=200, status="BACKWARD")
                _standalone_drive(rover, state)
            elif cmd == "arc_left":
                state.update(direction=1, drive_type="Arc",
                             radius_cm=-40, status="ARC LEFT")
                _standalone_drive(rover, state)
            elif cmd == "arc_right":
                state.update(direction=1, drive_type="Arc",
                             radius_cm=40, status="ARC RIGHT")
                _standalone_drive(rover, state)
            elif cmd == "spin_left":
                state.update(direction=-1, drive_type="Spin",
                             radius_cm=0, status="SPIN LEFT")
                _standalone_drive(rover, state)
            elif cmd == "spin_right":
                state.update(direction=1, drive_type="Spin",
                             radius_cm=0, status="SPIN RIGHT")
                _standalone_drive(rover, state)
            elif cmd == "stop":
                state["status"] = "STOPPED"
                rover.stopMotors()
            elif cmd == "speed":
                if value is None:
                    value = state["speed"]
                if isinstance(value, int) and abs(value) <= 20:
                    state["speed"] = max(10, min(100, state["speed"] + value))
                else:
                    state["speed"] = max(10, min(100, int(value)))
                if state["status"] != "STOPPED":
                    _standalone_drive(rover, state)

            print(
                f"[CMD] {cmd:<10} speed={state['speed']:>3} status={state['status']}")

    def watchdog() -> None:
        while not stop_event.is_set():
            time.sleep(0.3)
            with state_lock:
                if state["status"] != "STOPPED" and (time.time() - last_cmd_ts > watchdog_s):
                    state["status"] = "STOPPED"
                    rover.stopMotors()
                    print("[WDG] Auto-stopped")

    print("[BOOT] Initializing rover electronics")
    rover.initRover()
    rover.obstacleDetected = False

    controller = PSControllerService(handle_control_command)
    threading.Thread(target=watchdog, daemon=True).start()

    print("[BOOT] Controller-only mode started")
    print("[INFO] Pair/connect controller over Bluetooth, then move left stick or press buttons")
    print("[INFO] Ctrl+C to stop")

    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("\n[STOP] Shutting down")
    finally:
        stop_event.set()
        controller.stop()
        try:
            rover.cleanupRover()
        except Exception:
            rover.stopMotors()

    return 0


if __name__ == "__main__":
    raise SystemExit(run_standalone())
