#!/usr/bin/env python3
"""Restricted loopback API for the Savv Spine & Oracle VM Unified Monitor controller."""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = os.environ.get("CTRL_HOST", "127.0.0.1")
PORT = int(os.environ.get("CTRL_PORT", "6081"))
DISPLAY = os.environ.get("DISPLAY", ":101")
ALLOWED_ORIGINS = {
    "https://01-savvopsvm.taila7272b.ts.net:6080",
    "https://01-savvopsvm.taila7272b.ts.net:8445",
    "https://01-savvopsvm.taila7272b.ts.net:8447",
    "https://01-savvopsvm.taila7272b.ts.net:8450",
    "https://01-savvopsvm.taila7272b.ts.net",
    "https://savv-spine.taila7272b.ts.net:6080",
    "http://127.0.0.1:6080",
    "http://localhost:6080",
}
ACTION_LOCK = threading.Lock()
REQUEST_TIMES: deque[float] = deque()
RATE_LIMIT = 400


def run_xdotool(*args: str) -> str:
    env = os.environ.copy()
    env["DISPLAY"] = DISPLAY
    with ACTION_LOCK:
        result = subprocess.run(
            ["/usr/bin/xdotool", *args],
            check=True,
            capture_output=True,
            text=True,
            timeout=1.5,
            env=env,
        )
    return result.stdout.strip()


def rate_allowed() -> bool:
    now = time.monotonic()
    while REQUEST_TIMES and REQUEST_TIMES[0] < now - 1.0:
        REQUEST_TIMES.popleft()
    if len(REQUEST_TIMES) >= RATE_LIMIT:
        return False
    REQUEST_TIMES.append(now)
    return True


def maximize_active_window() -> None:
    """Idempotently maximize the focused XFCE window into the visible work area."""
    window_id = run_xdotool("getactivewindow")
    geometry_text = run_xdotool("getwindowgeometry", "--shell", window_id)
    geometry = {}
    for line in geometry_text.splitlines():
        key, separator, value = line.partition("=")
        if separator and key in {"X", "Y", "WIDTH", "HEIGHT"}:
            geometry[key] = int(value)
    already_fills_work_area = (
        geometry.get("X", 1) <= 0
        and geometry.get("Y", 28) <= 27
        and geometry.get("WIDTH", 0) >= 1440
        and geometry.get("HEIGHT", 0) >= 873
    )
    if not already_fills_work_area:
        run_xdotool(
            "windowactivate", "--sync", window_id,
            "key", "--clearmodifiers", "alt+F10",
        )


class Handler(BaseHTTPRequestHandler):
    server_version = "SpineMouse/2"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"{self.address_string()} {fmt % args}", flush=True)

    def _origin_allowed(self) -> bool:
        origin = self.headers.get("Origin")
        if origin in ALLOWED_ORIGINS or (origin and "taila7272b.ts.net" in origin):
            return True
        if origin and (origin.startswith("http://127.0.0.1") or origin.startswith("http://localhost")):
            return True
        host = self.headers.get("Host", "").split(":", 1)[0]
        return origin is None and (host in {"127.0.0.1", "localhost"} or "taila7272b.ts.net" in host)

    def _headers(self, status: int, length: int = 0) -> None:
        self.send_response(status)
        origin = self.headers.get("Origin")
        if origin and (origin in ALLOWED_ORIGINS or "taila7272b.ts.net" in origin or "127.0.0.1" in origin or "localhost" in origin):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        else:
            self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Arch-Actor, X-Arch-Lease")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(length))
        self.end_headers()

    def _json(self, status: int, payload: dict[str, object]) -> None:
        data = json.dumps(payload, separators=(",", ":")).encode()
        self._headers(status, len(data))
        self.wfile.write(data)

    def _norm_path(self) -> str:
        path = self.path.split("?", 1)[0]
        if path.startswith("/control/"):
            path = path[8:]
        elif path == "/control":
            path = "/"
        return path

    def do_OPTIONS(self) -> None:  # noqa: N802
        if not self._origin_allowed():
            self._json(403, {"ok": False, "error": "origin_denied"})
            return
        self._headers(204, 0)

    def do_GET(self) -> None:  # noqa: N802
        path = self._norm_path()
        if path == "/health":
            try:
                position = run_xdotool("getmouselocation", "--shell")
                self._json(200, {"ok": True, "display": DISPLAY, "position": position})
            except Exception:
                self._json(503, {"ok": False, "error": "display_unavailable"})
            return
        self._json(404, {"ok": False, "error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        if not self._origin_allowed():
            self._json(403, {"ok": False, "error": "origin_denied"})
            return
        if not rate_allowed():
            self._json(429, {"ok": False, "error": "rate_limited"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 2 or length > 1024:
                raise ValueError("invalid_length")
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError("invalid_json")

            path = self._norm_path()

            if path == "/move":
                if "x" in body and "y" in body:
                    x = max(0, min(3840, int(round(float(body["x"])))))
                    y = max(0, min(2160, int(round(float(body["y"])))))
                    run_xdotool("mousemove", str(x), str(y))
                else:
                    dx = max(-400, min(400, int(round(float(body.get("dx", 0))))))
                    dy = max(-400, min(400, int(round(float(body.get("dy", 0))))))
                    if dx or dy:
                        run_xdotool("mousemove_relative", "--", str(dx), str(dy))
            elif path in {"/button", "/click"}:
                button = int(body.get("button", 1))
                action = str(body.get("action", "click"))
                if button not in {1, 2, 3} or action not in {"down", "up", "click", "dblclick"}:
                    raise ValueError("invalid_button")
                if button == 3 and action in {"click", "down"}:
                    run_xdotool(
                        "keyup", "Alt_L", "Alt_R", "Control_L", "Control_R",
                        "Super_L", "Super_R", "Shift_L", "Shift_R",
                    )
                args: list[str] = []
                if "x" in body and "y" in body:
                    x = max(0, min(3840, int(round(float(body["x"])))))
                    y = max(0, min(2160, int(round(float(body["y"])))))
                    args.extend(["mousemove", str(x), str(y)])
                if action == "dblclick":
                    args.extend(["click", "--repeat", "2", "--delay", "45", str(button)])
                else:
                    command = {"down": "mousedown", "up": "mouseup", "click": "click"}[action]
                    args.extend([command, str(button)])
                run_xdotool(*args)
            elif path == "/scroll":
                direction = str(body.get("direction", ""))
                amount = max(1, min(8, int(body.get("amount", 1))))
                btn_map = {"up": "4", "down": "5", "left": "6", "right": "7"}
                if direction not in btn_map:
                    raise ValueError("invalid_scroll")
                args = []
                if "x" in body and "y" in body:
                    x = max(0, min(3840, int(round(float(body["x"])))))
                    y = max(0, min(2160, int(round(float(body["y"])))))
                    args.extend(["mousemove", str(x), str(y)])
                args.extend(["click", "--repeat", str(amount), "--delay", "20", btn_map[direction]])
                run_xdotool(*args)
            elif path == "/type":
                text = body.get("text", "")
                if not isinstance(text, str) or not text or len(text) > 300:
                    raise ValueError("invalid_text")
                if any(ord(char) < 32 for char in text):
                    raise ValueError("invalid_text")
                run_xdotool(
                    "keyup", "Alt_L", "Alt_R", "Control_L", "Control_R",
                    "Super_L", "Super_R", "Shift_L", "Shift_R",
                )
                run_xdotool("type", "--delay", "1", text)
            elif path == "/key":
                key = str(body.get("key", ""))
                allowed_keys = {
                    "Return", "BackSpace", "Tab", "Escape", "Delete",
                    "Left", "Right", "Up", "Down", "Home", "End",
                    "Page_Up", "Page_Down", "space",
                }
                if key not in allowed_keys:
                    raise ValueError("invalid_key")
                run_xdotool(
                    "keyup", "Alt_L", "Alt_R", "Control_L", "Control_R",
                    "Super_L", "Super_R", "Shift_L", "Shift_R",
                )
                run_xdotool("key", key)
            elif path == "/shortcut":
                shortcut = str(body.get("shortcut", ""))
                shortcuts = {
                    "select_all": "ctrl+a",
                    "copy": "ctrl+c",
                    "paste": "ctrl+v",
                    "cut": "ctrl+x",
                    "undo": "ctrl+z",
                    "redo": "ctrl+shift+z",
                    "alt_tab": "alt+Tab",
                    "terminal": "ctrl+alt+t",
                    "ctrl_l": "ctrl+l",
                    "ctrl_t": "ctrl+t",
                    "ctrl_w": "ctrl+w",
                }
                combo = shortcuts.get(shortcut)
                if combo is None:
                    raise ValueError("invalid_shortcut")
                run_xdotool(
                    "keyup", "Alt_L", "Alt_R", "Control_L", "Control_R",
                    "Super_L", "Super_R", "Shift_L", "Shift_R",
                )
                run_xdotool("key", combo)
            elif path == "/window":
                if str(body.get("action", "")) != "maximize":
                    raise ValueError("invalid_window_action")
                maximize_active_window()
            elif path == "/release":
                run_xdotool(
                    "mouseup", "1", "mouseup", "2", "mouseup", "3",
                    "keyup", "Alt_L", "Alt_R", "Control_L", "Control_R",
                    "Super_L", "Super_R", "Shift_L", "Shift_R",
                )
            else:
                self._json(404, {"ok": False, "error": "not_found"})
                return
            self._json(200, {"ok": True})
        except (ValueError, json.JSONDecodeError):
            self._json(400, {"ok": False, "error": "invalid_request"})
        except subprocess.TimeoutExpired:
            self._json(504, {"ok": False, "error": "control_timeout"})
        except subprocess.CalledProcessError:
            self._json(503, {"ok": False, "error": "control_failed"})
        except Exception:
            self._json(500, {"ok": False, "error": "internal_error"})


if __name__ == "__main__":
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
