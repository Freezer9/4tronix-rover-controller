#!/usr/bin/env python3
"""
Rover Web Controller — zero-latency edition
- No Flask: uses Python's built-in http.server (far less overhead)
- TCP_NODELAY: disables Nagle's algorithm so packets are sent instantly
- threading.Condition: stream generator wakes the instant a frame is ready
- Hardware MJPEGEncoder: GPU encodes JPEG, CPU is free
- Low resolution option: 320x240 for Pi Zero, 640x480 for Pi 4/5
"""

import io
import time
import json
import threading
import socket
import socketserver
from http.server import BaseHTTPRequestHandler, HTTPServer

rover = None
PSControllerService = None
Picamera2 = None
MJPEGEncoder = None
FileOutput = None

try:
    import modules.roverlib as rover
    ROVER_AVAILABLE = True
except Exception as exc:
    ROVER_AVAILABLE = False
    print(f"[WARN] roverlib unavailable: {exc}")

try:
    from pscontroller import PSControllerService
    CONTROLLER_AVAILABLE = True
except ImportError:
    CONTROLLER_AVAILABLE = False

try:
    from picamera2 import Picamera2
    from picamera2.encoders import MJPEGEncoder
    from picamera2.outputs import FileOutput
    CAMERA_AVAILABLE = True
except ImportError:
    CAMERA_AVAILABLE = False
    print("[WARN] picamera2 not found")

# ── Config ────────────────────────────────────────────────────────────────────
PORT = 5000
CAM_W, CAM_H = 480, 360     # lower = less latency; change to 640,480 on Pi 4/5
CAM_BITRATE = 4_000_000    # 4 Mbps — enough quality, low bandwidth
WATCHDOG_S = 2.0

# ── Rover state ───────────────────────────────────────────────────────────────
state = {"speed": 40, "direction": 1, "drive_type": "Straight",
         "radius_cm": 200, "status": "STOPPED", "distance": 0.0,
         "source": "web"}
state_lock = threading.Lock()
last_cmd_ts = time.time()
controller_service = None
_drive_api_warned = False


def watchdog():
    while True:
        time.sleep(0.3)
        with state_lock:
            if state["status"] != "STOPPED":
                if time.time() - last_cmd_ts > WATCHDOG_S:
                    state["status"] = "STOPPED"
                    if ROVER_AVAILABLE:
                        rover.stopMotors()
                    print("[WDG] Auto-stopped")


def init_rover():
    if not ROVER_AVAILABLE:
        return
    rover.initRover()
    rover.obstacleDetected = False


def send_drive():
    global _drive_api_warned
    if not ROVER_AVAILABLE:
        return

    powerpct = state["direction"] * state["speed"]
    drive_type = state["drive_type"]
    radius_cm = state["radius_cm"]

    rover.obstacleDetected = False
    try:
        rover.changeDrive(drive_type, powerpct, radius_cm)
        return
    except TypeError:
        # Backward compatibility: older roverlib exposes changeDrive(driveType, powerpct)
        # while Ackermandrive still supports radius for Arc/Spin.
        if not _drive_api_warned:
            print(
                "[WARN] roverlib.changeDrive uses legacy signature; using compatibility mode")
            _drive_api_warned = True

        if drive_type in ("Arc", "Spin") and hasattr(rover, "Ackermandrive"):
            rover.Ackermandrive(drive_type, powerpct, radius_cm)
        else:
            rover.changeDrive(drive_type, powerpct)


def handle_control_command(cmd, value=None, source="web"):
    global last_cmd_ts

    with state_lock:
        last_cmd_ts = time.time()
        state["source"] = source

        if cmd == "forward":
            state.update(direction=1, drive_type="Straight",
                         radius_cm=200, status="FORWARD")
            send_drive()
        elif cmd == "backward":
            state.update(direction=-1, drive_type="Straight",
                         radius_cm=200, status="BACKWARD")
            send_drive()
        elif cmd == "arc_left":
            state.update(direction=1, drive_type="Arc",
                         radius_cm=-40, status="ARC LEFT")
            send_drive()
        elif cmd == "arc_right":
            state.update(direction=1, drive_type="Arc",
                         radius_cm=40, status="ARC RIGHT")
            send_drive()
        elif cmd == "spin_left":
            state.update(direction=-1, drive_type="Spin",
                         radius_cm=0, status="SPIN LEFT")
            send_drive()
        elif cmd == "spin_right":
            state.update(direction=1, drive_type="Spin",
                         radius_cm=0, status="SPIN RIGHT")
            send_drive()
        elif cmd == "stop":
            state["status"] = "STOPPED"
            if ROVER_AVAILABLE:
                rover.stopMotors()
        elif cmd == "speed":
            if value is None:
                value = 40
            if source == "controller" and isinstance(value, int) and abs(value) <= 20:
                new_speed = state["speed"] + value
            else:
                new_speed = int(value)
            state["speed"] = max(10, min(100, new_speed))
            if state["status"] != "STOPPED":
                send_drive()
        elif cmd == "ping":
            pass

        return {"status": state["status"], "speed": state["speed"]}

# ── Camera streaming ──────────────────────────────────────────────────────────


class StreamingOutput(io.BufferedIOBase):
    def __init__(self):
        self.frame = None
        self.condition = threading.Condition()

    def write(self, buf):
        with self.condition:
            self.frame = buf
            self.condition.notify_all()


stream_output = StreamingOutput()
picam2 = None


def start_camera():
    global picam2
    if not CAMERA_AVAILABLE:
        return
    picam2 = Picamera2()
    picam2.configure(picam2.create_video_configuration(
        main={"size": (CAM_W, CAM_H), "format": "YUV420"}
    ))
    picam2.start_recording(MJPEGEncoder(CAM_BITRATE),
                           FileOutput(stream_output))
    print(f"[CAM] {CAM_W}x{CAM_H} @ {CAM_BITRATE//1_000_000}Mbps hardware MJPEG")

# ── HTTP handler ──────────────────────────────────────────────────────────────


class RoverHandler(BaseHTTPRequestHandler):

    def log_message(self, *args):
        pass          # silence per-request logs for performance

    # ── GET ───────────────────────────────────────────────────────────────────
    def do_GET(self):
        if self.path == "/":
            body = HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)

        elif self.path == "/stream.mjpg":
            self.send_response(200)
            self.send_header("Age", 0)
            self.send_header("Cache-Control", "no-cache, private")
            self.send_header("Pragma", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Content-Type",
                             "multipart/x-mixed-replace; boundary=FRAME")
            self.end_headers()
            try:
                while True:
                    with stream_output.condition:
                        stream_output.condition.wait()
                        frame = stream_output.frame
                    self.wfile.write(b"--FRAME\r\n"
                                     b"Content-Type: image/jpeg\r\n"
                                     b"Content-Length: " +
                                     str(len(frame)).encode() + b"\r\n\r\n"
                                     + frame + b"\r\n")
            except Exception:
                pass

        elif self.path == "/state":
            dist = 0.0
            if ROVER_AVAILABLE:
                try:
                    dist = rover.getSonarDistance()
                except Exception:
                    pass
            state["distance"] = dist
            payload = dict(state)
            payload["controller"] = controller_service.snapshot() if controller_service else {
                "available": False,
                "enabled": False,
                "connected": False,
                "discovered": False,
                "paired": False,
                "name": "",
                "address": "",
                "device_path": "",
                "source": "none",
                "last_command": "",
                "last_event": "",
                "message": "Controller support unavailable",
                "last_seen": 0.0,
            }
            self._json(payload)

        elif self.path == "/controller":
            if controller_service:
                self._json(controller_service.snapshot())
            else:
                self.send_response(404)
                self.end_headers()

        else:
            self.send_response(404)
            self.end_headers()

    # ── POST ──────────────────────────────────────────────────────────────────
    def do_POST(self):
        if self.path not in ("/control", "/controller"):
            self.send_response(404)
            self.end_headers()
            return

        length = int(self.headers.get("Content-Length", 0))
        data = json.loads(self.rfile.read(length) or b"{}")

        if self.path == "/controller":
            if not controller_service:
                self.send_response(404)
                self.end_headers()
                return

            action = data.get("action", "rescan")
            address = data.get("address", "")
            if action == "pair" and address:
                ok = controller_service.pair_device(address)
                self._json(
                    {"ok": ok, "controller": controller_service.snapshot()})
            else:
                controller_service.refresh_devices()
                self._json(
                    {"ok": True, "controller": controller_service.snapshot()})
            return

        cmd = data.get("cmd", "")
        result = handle_control_command(cmd, data.get("value"), "web")
        self._json(result)

    def _json(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

# ── Server with TCP_NODELAY ───────────────────────────────────────────────────


class RoverServer(socketserver.ThreadingMixIn, HTTPServer):
    """
    ThreadingMixIn  → each connection gets its own thread
                       (MJPEG stream and API calls don't block each other)
    TCP_NODELAY     → disables Nagle's algorithm; packets sent immediately,
                       not buffered to wait for more data — critical for
                       low-latency MJPEG and fast API responses
    """
    allow_reuse_address = True
    daemon_threads = True

    def server_bind(self):
        # Disable Nagle — most impactful single change for reducing ping
        self.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        super().server_bind()


# ── HTML ──────────────────────────────────────────────────────────────────────
HTML = """<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Rover Control</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600&family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root{
  --text-xs:clamp(.75rem,.7rem + .25vw,.875rem);
  --text-sm:clamp(.875rem,.8rem + .35vw,1rem);
  --text-base:clamp(1rem,.95rem + .25vw,1.125rem);
  --text-lg:clamp(1.125rem,1rem + .75vw,1.5rem);
  --s1:.25rem;--s2:.5rem;--s3:.75rem;--s4:1rem;--s5:1.25rem;--s6:1.5rem;
  --r-sm:.375rem;--r-md:.5rem;--r-lg:.75rem;--r-xl:1rem;--r-full:9999px;
  --ease:150ms cubic-bezier(.16,1,.3,1);
  --font:'Inter',system-ui,sans-serif;
  --mono:'JetBrains Mono',monospace;
}
[data-theme="dark"]{
  --bg:#0e0e0c;--sur:#141412;--sur2:#1a1917;--sur3:#222120;
  --bdr:rgba(255,255,255,.08);--text:#d4d3d0;--muted:#6e6d6a;--faint:#3a3937;
  --pri:#4f98a3;--danger:#c94f4f;--ok:#6daa45;
  --shd:0 4px 20px rgba(0,0,0,.5);
}
[data-theme="light"]{
  --bg:#f5f4f0;--sur:#fafaf7;--sur2:#f0eeea;--sur3:#e8e6e2;
  --bdr:rgba(0,0,0,.09);--text:#1e1c18;--muted:#6a6860;--faint:#c8c6c0;
  --pri:#01696f;--danger:#c0392b;--ok:#2e7d32;
  --shd:0 4px 20px rgba(0,0,0,.1);
}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
html{-webkit-font-smoothing:antialiased}
body{min-height:100dvh;font-family:var(--font);font-size:var(--text-base);color:var(--text);background:var(--bg);display:grid;grid-template-rows:auto 1fr auto}
img,svg{display:block}button{cursor:pointer;border:none;background:none;font:inherit;color:inherit}
a,button,input{transition:background var(--ease),color var(--ease),transform var(--ease)}
header{display:flex;align-items:center;justify-content:space-between;padding:var(--s3) var(--s6);background:var(--sur);border-bottom:1px solid var(--bdr);position:sticky;top:0;z-index:10}
.logo{display:flex;align-items:center;gap:var(--s3);font-weight:600}
.logo svg{color:var(--pri)}
.logo span{font-size:var(--text-sm);letter-spacing:.06em;text-transform:uppercase}
.header-r{display:flex;align-items:center;gap:var(--s4)}
.pill{display:flex;align-items:center;gap:var(--s2);padding:var(--s1) var(--s3);background:var(--sur3);border-radius:var(--r-full);font-size:var(--text-xs);font-family:var(--mono);font-weight:600}
.dot{width:7px;height:7px;border-radius:50%;background:var(--faint);transition:background var(--ease),box-shadow var(--ease)}
.dot.on{background:var(--ok);box-shadow:0 0 6px var(--ok)}
.tbtn{width:36px;height:36px;border-radius:var(--r-md);display:grid;place-items:center;color:var(--muted)}
.tbtn:hover{background:var(--sur3);color:var(--text)}
main{display:grid;grid-template-columns:1fr 310px;gap:var(--s6);padding:var(--s6);max-width:1200px;margin-inline:auto;width:100%;align-items:start}
.left-stack{display:flex;flex-direction:column;gap:var(--s4)}
.cam-panel{background:var(--sur);border:1px solid var(--bdr);border-radius:var(--r-xl);overflow:hidden;box-shadow:var(--shd)}
.panel-hdr{display:flex;align-items:center;justify-content:space-between;padding:var(--s3) var(--s5);border-bottom:1px solid var(--bdr)}
.panel-title{font-size:var(--text-xs);font-weight:600;text-transform:uppercase;letter-spacing:.08em;color:var(--muted)}
.panel-hdr-r{display:flex;align-items:center;gap:var(--s4)}
.live{display:flex;align-items:center;gap:var(--s1);font-size:var(--text-xs);font-family:var(--mono);color:var(--danger);font-weight:600}
.live-dot{width:6px;height:6px;border-radius:50%;background:var(--danger);animation:blink 1.4s ease-in-out infinite}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.3}}
.rot-ctrl{display:flex;align-items:center;gap:var(--s1)}
.rot-lbl{font-size:var(--text-xs);color:var(--muted);margin-right:var(--s2)}
.rot-btn{width:28px;height:28px;border-radius:var(--r-sm);background:var(--sur3);border:1px solid var(--bdr);display:grid;place-items:center;font-size:11px;color:var(--muted);font-family:var(--mono)}
.rot-btn:hover{background:var(--sur2);color:var(--text)}
.rot-btn.active{background:var(--pri);color:#fff;border-color:transparent}
.cam-box{aspect-ratio:4/3;width:100%;background:#000;display:grid;place-items:center;overflow:hidden}
.cam-box img{width:100%;height:100%;object-fit:cover;transition:transform 300ms cubic-bezier(.16,1,.3,1)}
.no-cam{color:var(--faint);font-size:var(--text-sm);font-family:var(--mono)}
.ctrl{display:flex;flex-direction:column;gap:var(--s4)}
.card{background:var(--sur);border:1px solid var(--bdr);border-radius:var(--r-xl);padding:var(--s5);box-shadow:var(--shd)}
.card-lbl{font-size:var(--text-xs);font-weight:600;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin-bottom:var(--s4)}
.dpad{display:grid;grid-template-areas:". up ." "left stop right" ". dn .";grid-template-columns:1fr 1fr 1fr;gap:var(--s2)}
.db{aspect-ratio:1;border-radius:var(--r-lg);background:var(--sur2);border:1px solid var(--bdr);display:grid;place-items:center;font-size:1.2rem;user-select:none;-webkit-user-select:none;touch-action:none}
.db:hover{background:var(--sur3)}
.db:active,.db.pressed{background:var(--pri);color:#fff;transform:scale(.93);border-color:transparent}
.db.stop-btn{font-size:var(--text-xs);font-weight:700;letter-spacing:.06em;color:var(--muted)}
.db.stop-btn:active,.db.stop-btn.pressed{background:var(--danger);color:#fff}
[data-cmd=forward]{grid-area:up}[data-cmd=backward]{grid-area:dn}
[data-cmd=left]{grid-area:left}[data-cmd=right]{grid-area:right}[data-cmd=stop]{grid-area:stop}
.spd-row{display:flex;align-items:baseline;justify-content:space-between;margin-bottom:var(--s3)}
.spd-val{font-family:var(--mono);font-size:var(--text-lg);font-weight:600;color:var(--pri)}
.spd-unit{font-size:var(--text-xs);color:var(--muted);margin-left:2px}
.spd-hint{font-size:var(--text-xs);color:var(--muted);font-family:var(--mono)}
input[type=range]{width:100%;-webkit-appearance:none;appearance:none;height:4px;border-radius:2px;background:linear-gradient(to right,var(--pri) var(--p,34%),var(--sur3) var(--p,34%));outline:none;cursor:pointer}
input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;width:18px;height:18px;border-radius:50%;background:var(--pri);border:2px solid var(--bg)}
.spd-btns{display:flex;gap:var(--s2);margin-top:var(--s3)}
.spd-bump{flex:1;padding:var(--s2) 0;border-radius:var(--r-md);background:var(--sur2);border:1px solid var(--bdr);font-family:var(--mono);font-size:var(--text-sm);font-weight:600;color:var(--muted)}
.spd-bump:hover{background:var(--sur3);color:var(--text)}
.spd-bump:active{background:var(--pri);color:#fff;transform:scale(.96)}
.sel-row{display:flex;gap:var(--s2);align-items:center}
select.bt-select{flex:1;min-width:0;padding:var(--s2) var(--s3);border-radius:var(--r-md);border:1px solid var(--bdr);background:var(--sur2);color:var(--text);font:inherit}
.bt-note{margin-top:var(--s3);font-size:var(--text-xs);color:var(--muted);font-family:var(--mono)}
.tgrid{display:grid;grid-template-columns:1fr 1fr;gap:var(--s3)}
.ti{background:var(--sur2);border-radius:var(--r-lg);padding:var(--s3) var(--s4)}
.ti-lbl{font-size:var(--text-xs);color:var(--muted);margin-bottom:var(--s1)}
.ti-val{font-family:var(--mono);font-size:var(--text-base);font-weight:600}
.kgrid{display:grid;grid-template-columns:1fr 1fr;gap:var(--s2)}
.kr{display:flex;align-items:center;gap:var(--s2);font-size:var(--text-xs);color:var(--muted)}
kbd{display:inline-block;padding:2px 6px;background:var(--sur3);border:1px solid var(--bdr);border-radius:var(--r-sm);font-family:var(--mono);font-size:11px;color:var(--text);min-width:22px;text-align:center}
footer{padding:var(--s3) var(--s6);border-top:1px solid var(--bdr);font-size:var(--text-xs);color:var(--faint);display:flex;align-items:center;justify-content:space-between;font-family:var(--mono)}
@media(max-width:768px){main{grid-template-columns:1fr;padding:var(--s4)}.ctrl{order:-1}.rot-lbl{display:none}}
</style>
</head>
<body>
<header>
  <div class="logo">
    <svg width="26" height="26" viewBox="0 0 28 28" fill="none" aria-label="Rover">
      <rect x="4" y="10" width="20" height="12" rx="3" stroke="currentColor" stroke-width="1.8"/>
      <circle cx="8" cy="23" r="2.8" stroke="currentColor" stroke-width="1.8"/>
      <circle cx="20" cy="23" r="2.8" stroke="currentColor" stroke-width="1.8"/>
      <rect x="10" y="5" width="8" height="6" rx="1.5" stroke="currentColor" stroke-width="1.5"/>
      <line x1="14" y1="5" x2="14" y2="2" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>
      <circle cx="14" cy="1.5" r="1.5" fill="currentColor"/>
    </svg>
    <span>Rover Control</span>
  </div>
  <div class="header-r">
    <div class="pill"><span class="dot" id="sdot"></span><span id="stxt">STOPPED</span></div>
    <button class="tbtn" data-theme-toggle aria-label="Toggle theme">
      <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
        <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/>
      </svg>
    </button>
  </div>
</header>

<main>
  <div class="left-stack">
  <div class="cam-panel">
    <div class="panel-hdr">
      <span class="panel-title">Live — IMX500 AI Camera</span>
      <div class="panel-hdr-r">
        <div class="rot-ctrl" role="group" aria-label="Camera rotation">
          <span class="rot-lbl">Rotate</span>
          <button class="rot-btn active" data-rot="0">0°</button>
          <button class="rot-btn" data-rot="90">90°</button>
          <button class="rot-btn" data-rot="180">180°</button>
          <button class="rot-btn" data-rot="270">270°</button>
        </div>
        <span class="live"><span class="live-dot"></span>LIVE</span>
      </div>
    </div>
    <div class="cam-box">
      <img src="/stream.mjpg" alt="Live rover camera" id="cam"
           onerror="this.style.display='none';document.getElementById('nocam').style.display='block'">
      <div class="no-cam" id="nocam" style="display:none">Camera unavailable</div>
    </div>
  </div>

    <div class="card">
      <div class="card-lbl">Bluetooth Controller</div>
      <div class="sel-row">
        <select id="bt-device" class="bt-select" aria-label="Detected Bluetooth devices">
          <option value="">Scan for devices</option>
        </select>
        <button class="spd-bump" id="bt-refresh" aria-label="Refresh device list">Scan</button>
      </div>
      <div class="sel-row" style="margin-top:var(--s3)">
        <button class="spd-bump" id="bt-pair" aria-label="Pair selected device">Pair Selected</button>
      </div>
      <div class="bt-note" id="bt-note">No Bluetooth device selected</div>
      <div class="bt-note" id="bt-status">Waiting for scan</div>
    </div>
  </div>

  <div class="ctrl">
    <div class="card">
      <div class="card-lbl">Drive — WASD</div>
      <div class="dpad">
        <button class="db" data-cmd="forward" aria-label="Forward">&#9650;</button>
        <button class="db" data-cmd="arc_left" aria-label="Arc Left">&#9664;</button>
        <button class="db stop-btn" data-cmd="stop" aria-label="Stop">STOP</button>
        <button class="db" data-cmd="arc_right" aria-label="Arc Right">&#9654;</button>
        <button class="db" data-cmd="backward" aria-label="Backward">&#9660;</button>
      </div>
    </div>

    <div class="card">
      <div class="card-lbl">Speed — ← →</div>
      <div class="spd-row">
        <div class="spd-val" id="spd-disp">40<span class="spd-unit">%</span></div>
        <span class="spd-hint">Arrow keys ±10</span>
      </div>
      <input type="range" id="spd" min="10" max="100" step="10" value="40" aria-label="Speed">
      <div class="spd-btns">
        <button class="spd-bump" id="spd-dn" aria-label="Decrease speed">− 10</button>
        <button class="spd-bump" id="spd-up" aria-label="Increase speed">+ 10</button>
      </div>
    </div>

    <div class="card">
      <div class="card-lbl">Telemetry</div>
      <div class="tgrid">
        <div class="ti"><div class="ti-lbl">Status</div><div class="ti-val" id="t-status">STOPPED</div></div>
        <div class="ti"><div class="ti-lbl">Speed</div><div class="ti-val" id="t-speed">40%</div></div>
        <div class="ti"><div class="ti-lbl">Distance</div><div class="ti-val" id="t-dist">— cm</div></div>
        <div class="ti"><div class="ti-lbl">Ping</div><div class="ti-val" id="t-ping">— ms</div></div>
      </div>
    </div>

    <div class="card">
      <div class="card-lbl">Keyboard</div>
      <div class="kgrid">
        <div class="kr"><kbd>W</kbd> Forward</div>
        <div class="kr"><kbd>S</kbd> Backward</div>
        <div class="kr"><kbd>A</kbd> Spin Left</div>
        <div class="kr"><kbd>D</kbd> Spin Right</div>
        <div class="kr"><kbd>→</kbd> Speed +10</div>
        <div class="kr"><kbd>←</kbd> Speed −10</div>
        <div class="kr" style="grid-column:span 2"><kbd>Space</kbd> Emergency Stop</div>
      </div>
    </div>
  </div>
</main>

<footer>
  <span>4tronix M.A.R.S. Rover — Web Controller</span>
  <span>:5000</span>
</footer>

<script>
// ── Theme ─────────────────────────────────────────────────────────────────────
const html = document.documentElement;
const themeBtn = document.querySelector('[data-theme-toggle]');
let theme = 'dark';
themeBtn.addEventListener('click', () => {
  theme = theme === 'dark' ? 'light' : 'dark';
  html.setAttribute('data-theme', theme);
  themeBtn.innerHTML = theme === 'dark'
    ? '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>'
    : '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="5"/><path d="M12 1v2M12 21v2M4.22 4.22l1.42 1.42M18.36 18.36l1.42 1.42M1 12h2M21 12h2M4.22 19.78l1.42-1.42M18.36 5.64l1.42-1.42"/></svg>';
});

// ── Camera rotation ───────────────────────────────────────────────────────────
const camImg = document.getElementById('cam');
document.querySelectorAll('.rot-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    const deg = parseInt(btn.dataset.rot);
    camImg.style.transform = (deg===90||deg===270) ? `rotate(${deg}deg) scale(${4/3})` : `rotate(${deg}deg)`;
    document.querySelectorAll('.rot-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
  });
});

// ── Status ────────────────────────────────────────────────────────────────────
function setStatus(s, ping) {
  document.getElementById('sdot').className = 'dot ' + (s!=='STOPPED'?'on':'');
  document.getElementById('stxt').textContent = s;
  document.getElementById('t-status').textContent = s;
  if (ping != null) document.getElementById('t-ping').textContent = ping + ' ms';
}

function setController(c) {
  const select = document.getElementById('bt-device');
  const devices = Array.isArray(c && c.devices) ? c.devices : [];
  const currentValue = select.value;
  if (devices.length === 0) {
    select.innerHTML = '<option value="">No Bluetooth devices detected</option>';
    select.value = '';
  } else {
    select.innerHTML = devices.map(d => {
      const parts = [];
      if (d.name) parts.push(d.name);
      parts.push(d.address);
      const label = parts.join(' - ');
      const selected = d.address === currentValue || d.connected;
      return `<option value="${d.address}"${selected ? ' selected' : ''}>${label}</option>`;
    }).join('');
    if (!select.value) {
      select.value = devices[0].address;
    }
  }
  document.getElementById('bt-note').textContent = c && c.name ? `${c.name} ${c.connected ? 'connected' : 'detected'}` : 'No Bluetooth device selected';
  document.getElementById('bt-status').textContent = c && c.message ? c.message : 'Waiting for scan';
}

// ── Command — fire-and-forget, reuse connection ───────────────────────────────
function send(cmd, extra={}) {
  const t0 = performance.now();
  fetch('/control', {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({cmd, ...extra}),
    keepalive: true
  }).then(r=>r.json()).then(d=>{
    setStatus(d.status, Math.round(performance.now()-t0));
  }).catch(()=>{});
}

// ── D-pad ─────────────────────────────────────────────────────────────────────
document.querySelectorAll('.db').forEach(btn => {
  const cmd = btn.dataset.cmd;
  const press   = () => { btn.classList.add('pressed');    send(cmd); };
  const release = () => { btn.classList.remove('pressed'); if(cmd!=='stop') send('stop'); };
  btn.addEventListener('mousedown',  press);
  btn.addEventListener('touchstart', e=>{e.preventDefault();press();},{passive:false});
  btn.addEventListener('mouseup',    release);
  btn.addEventListener('touchend',   release);
  btn.addEventListener('mouseleave', release);
});

// ── Speed ─────────────────────────────────────────────────────────────────────
const slider = document.getElementById('spd');
let spd = 40;
function applySpeed(v) {
  spd = Math.max(10, Math.min(100, v));
  slider.value = spd;
  slider.style.setProperty('--p', ((spd-10)/90*100).toFixed(1)+'%');
  document.getElementById('spd-disp').innerHTML = spd + '<span class="spd-unit">%</span>';
  document.getElementById('t-speed').textContent = spd+'%';
  send('speed',{value:spd});
}
slider.addEventListener('input', ()=>applySpeed(+slider.value));
document.getElementById('spd-up').addEventListener('click', ()=>applySpeed(spd+10));
document.getElementById('spd-dn').addEventListener('click', ()=>applySpeed(spd-10));

function controllerAction(action, address='') {
  return fetch('/controller', {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({action, address})
  }).then(r => r.json()).then(d => {
    if (d && d.controller) setController(d.controller);
  }).catch(()=>{});
}

document.getElementById('bt-refresh').addEventListener('click', () => controllerAction('rescan'));
document.getElementById('bt-pair').addEventListener('click', () => {
  const address = document.getElementById('bt-device').value;
  if (address) {
    controllerAction('pair', address);
  }
});

controllerAction('rescan');

// ── Keyboard ──────────────────────────────────────────────────────────────────
const driveKeys = {w:'forward',s:'backward',a:'spin_left',d:'spin_right'};
const held = new Set();
document.addEventListener('keydown', e => {
  if(e.target.tagName==='INPUT') return;
  if(e.key==='ArrowUp')   {applySpeed(spd+10);return;}
  if(e.key==='ArrowDown') {applySpeed(spd-10);return;}
  if(e.key==='ArrowLeft') {if(!held.has(e.key)){held.add(e.key);send('arc_left'); document.querySelector('[data-cmd=arc_left]')?.classList.add('pressed');}return;}
  if(e.key==='ArrowRight'){if(!held.has(e.key)){held.add(e.key);send('arc_right');document.querySelector('[data-cmd=arc_right]')?.classList.add('pressed');}return;}
  if(e.key===' '){e.preventDefault();send('stop');return;}
  const cmd = driveKeys[e.key.toLowerCase()];
  if(!cmd||held.has(e.key)) return;
  held.add(e.key);
  send(cmd);
  document.querySelector(`[data-cmd="${cmd}"]`)?.classList.add('pressed');
});
document.addEventListener('keyup', e => {
  if(e.key==='ArrowLeft') {held.delete(e.key);send('stop');document.querySelector('[data-cmd=arc_left]')?.classList.remove('pressed');return;}
  if(e.key==='ArrowRight'){held.delete(e.key);send('stop');document.querySelector('[data-cmd=arc_right]')?.classList.remove('pressed');return;}
  const cmd = driveKeys[e.key.toLowerCase()];
  if(!cmd) return;
  held.delete(e.key);
  send('stop');
  document.querySelector(`[data-cmd="${cmd}"]`)?.classList.remove('pressed');
});

// ── Heartbeat — keeps watchdog alive ─────────────────────────────────────────
setInterval(()=>{ if(held.size>0) send('ping'); }, 500);

// ── Telemetry polling ─────────────────────────────────────────────────────────
setInterval(async()=>{
  try{
    const d = await(await fetch('/state')).json();
    setStatus(d.status);
    setController(d.controller);
    document.getElementById('t-dist').textContent =
      d.distance>0 ? d.distance.toFixed(1)+' cm' : '— cm';
  }catch(e){}
}, 1000);
</script>
</body>
</html>"""

# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if ROVER_AVAILABLE:
        init_rover()
    if CONTROLLER_AVAILABLE and PSControllerService is not None:
        controller_service = PSControllerService(handle_control_command)
    start_camera()
    threading.Thread(target=watchdog, daemon=True).start()
    server = RoverServer(("", PORT), RoverHandler)
    print(f"Rover Web Controller → http://0.0.0.0:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if controller_service:
            controller_service.stop()
