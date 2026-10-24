"""
The Architect - Ephemeral Browser Worker & Profile-Forking Adapter
Enables 1 shared canonical browser for the owner while allowing AI agents to spawn
and destroy N isolated, credentialed temporary browser copies on demand.
Zero external pip dependencies (pure Python standard library only).
"""

import base64
import json
import os
import platform
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

IS_WINDOWS = sys.platform == "win32"
BASE_WORKER_DIR = os.path.join(tempfile.gettempdir(), "architect_browser")
REGISTRY_FILE = os.path.join(BASE_WORKER_DIR, "workers.json")
SAO_BROWSER_URL = os.environ.get("SAO_BROWSER_URL", "https://savv-spine.taila7272b.ts.net:6092")
SPINE_BROWSER_URL = os.environ.get("SPINE_BROWSER_URL", SAO_BROWSER_URL)
ORACLE_BROWSER_URL = os.environ.get("ORACLE_BROWSER_URL", "https://01-savvopsvm.taila7272b.ts.net:6092")

REMOTE_BROWSERS: Dict[str, Dict[str, Any]] = {
    "spine": {
        "name": "Spine Browser (SAO)",
        "url": SPINE_BROWSER_URL,
        "host": "savv-spine.taila7272b.ts.net",
        "port": 6092,
        "cdp_port": 9222,
        "type": "webrtc-selkies",
    },
    "sao": {
        "name": "Spine Browser (SAO)",
        "url": SAO_BROWSER_URL,
        "host": "savv-spine.taila7272b.ts.net",
        "port": 6092,
        "cdp_port": 9222,
        "type": "webrtc-selkies",
    },
    "oracle": {
        "name": "Oracle Browser",
        "url": ORACLE_BROWSER_URL,
        "host": "01-savvopsvm.taila7272b.ts.net",
        "port": 6092,
        "cdp_ip": "100.112.12.23",
        "cdp_port": 9223,
        "kasm_port": 8447,
        "type": "webrtc-selkies",
    },
}


def find_free_port(start_port: int = 9222, max_attempts: int = 100) -> int:
    """Find an available TCP port for remote debugging."""
    for port in range(start_port, start_port + max_attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.2)
            res = s.connect_ex(("127.0.0.1", port))
            if res != 0:
                return port
    raise RuntimeError(f"No free port found in range {start_port}-{start_port+max_attempts}")


def detect_browsers() -> Dict[str, Dict[str, Any]]:
    """Auto-detect installed browsers and their canonical profile directories."""
    browsers: Dict[str, Dict[str, Any]] = {}

    if IS_WINDOWS:
        candidates = {
            "brave": {
                "bins": [
                    r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
                    r"C:\Program Files (x86)\BraveSoftware\Brave-Browser\Application\brave.exe",
                    os.path.expandvars(r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\Application\brave.exe"),
                ],
                "profile": os.path.expandvars(r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\User Data"),
                "type": "chromium",
            },
            "chrome": {
                "bins": [
                    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
                ],
                "profile": os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\User Data"),
                "type": "chromium",
            },
            "chromium": {
                "bins": [
                    os.path.expandvars(r"%LOCALAPPDATA%\Chromium\Application\chrome.exe"),
                    r"C:\Program Files\Chromium\Application\chrome.exe",
                ],
                "profile": os.path.expandvars(r"%LOCALAPPDATA%\Chromium\User Data"),
                "type": "chromium",
            },
            "edge": {
                "bins": [
                    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
                ],
                "profile": os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\User Data"),
                "type": "chromium",
            },
            "firefox": {
                "bins": [
                    r"C:\Program Files\Mozilla Firefox\firefox.exe",
                    r"C:\Program Files (x86)\Mozilla Firefox\firefox.exe",
                ],
                "profile": os.path.expandvars(r"%APPDATA%\Mozilla\Firefox\Profiles"),
                "type": "firefox",
            },
        }
    elif sys.platform == "darwin":
        candidates = {
            "brave": {
                "bins": ["/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"],
                "profile": os.path.expanduser("~/Library/Application Support/BraveSoftware/Brave-Browser"),
                "type": "chromium",
            },
            "chrome": {
                "bins": ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"],
                "profile": os.path.expanduser("~/Library/Application Support/Google/Chrome"),
                "type": "chromium",
            },
            "firefox": {
                "bins": ["/Applications/Firefox.app/Contents/MacOS/firefox"],
                "profile": os.path.expanduser("~/Library/Application Support/Firefox/Profiles"),
                "type": "firefox",
            },
        }
    else:  # Linux
        candidates = {
            "brave": {
                "bins": [shutil.which("brave-browser") or "", shutil.which("brave") or ""],
                "profile": os.path.expanduser("~/.config/BraveSoftware/Brave-Browser"),
                "type": "chromium",
            },
            "chrome": {
                "bins": [shutil.which("google-chrome") or "", shutil.which("chrome") or ""],
                "profile": os.path.expanduser("~/.config/google-chrome"),
                "type": "chromium",
            },
            "chromium": {
                "bins": [shutil.which("chromium") or "", shutil.which("chromium-browser") or ""],
                "profile": os.path.expanduser("~/.config/chromium"),
                "type": "chromium",
            },
            "firefox": {
                "bins": [shutil.which("firefox") or ""],
                "profile": os.path.expanduser("~/.mozilla/firefox"),
                "type": "firefox",
            },
        }

    for name, c in candidates.items():
        found_bin = None
        for b in c["bins"]:
            if b and os.path.exists(b):
                found_bin = b
                break
        if not found_bin:
            # check PATH
            which_res = shutil.which(name)
            if which_res:
                found_bin = which_res

        has_profile = bool(c["profile"] and os.path.exists(c["profile"]))
        if found_bin or has_profile:
            browsers[name] = {
                "name": name,
                "binary": found_bin,
                "profile": c["profile"] if has_profile else None,
                "type": c["type"],
                "ready": bool(found_bin),
            }

    return browsers


class CDPClient:
    """Minimal, pure standard library WebSocket client for Chrome DevTools Protocol."""

    def __init__(self, host: str = "127.0.0.1", port: int = 9222):
        self.host = host
        self.port = port
        self._msg_id = 0

    def get_version(self, timeout: float = 3.0) -> Dict[str, Any]:
        url = f"http://{self.host}:{self.port}/json/version"
        req = urllib.request.Request(url, headers={"Host": f"{self.host}:{self.port}"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def get_targets(self, timeout: float = 3.0) -> List[Dict[str, Any]]:
        url = f"http://{self.host}:{self.port}/json/list"
        req = urllib.request.Request(url, headers={"Host": f"{self.host}:{self.port}"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def send_ws_command(self, ws_url: str, method: str, params: Optional[Dict[str, Any]] = None, timeout: float = 5.0) -> Dict[str, Any]:
        """Send a single CDP command over WebSocket and read matching response (skipping async events)."""
        parsed = urllib.parse.urlparse(ws_url)
        host = parsed.hostname or self.host
        port = parsed.port or self.port
        path = parsed.path

        s = socket.socket()
        s.settimeout(timeout)
        s.connect((host, port))

        try:
            # WebSocket Handshake (RFC 6455)
            key = base64.b64encode(os.urandom(16)).decode("ascii")
            req = (
                f"GET {path} HTTP/1.1\r\n"
                f"Host: {host}:{port}\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\n"
                "Sec-WebSocket-Version: 13\r\n\r\n"
            )
            s.sendall(req.encode("ascii"))
            raw_handshake = b""
            while b"\r\n\r\n" not in raw_handshake:
                chunk = s.recv(2048)
                if not chunk:
                    raise RuntimeError("WebSocket connection closed during handshake")
                raw_handshake += chunk
            head_bytes, _, tail_bytes = raw_handshake.partition(b"\r\n\r\n")
            resp = head_bytes.decode("latin-1")
            if "101 Switching Protocols" not in resp and "101 WebSocket Protocol Handshake" not in resp:
                raise RuntimeError(f"WebSocket upgrade failed: {resp[:100]}")

            self._msg_id += 1
            expected_id = self._msg_id
            cmd_obj = {"id": expected_id, "method": method, "params": params or {}}
            payload = json.dumps(cmd_obj).encode("utf-8")

            # Client-to-server frames must be masked
            mask = os.urandom(4)
            masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            length = len(payload)

            if length <= 125:
                header = bytes([0x81, 0x80 | length]) + mask
            elif length <= 65535:
                header = bytes([0x81, 0x80 | 126]) + struct.pack(">H", length) + mask
            else:
                header = bytes([0x81, 0x80 | 127]) + struct.pack(">Q", length) + mask

            s.sendall(header + masked)

            # Read response frames until message matching expected_id arrives
            buffer = bytearray(tail_bytes)
            fragments = bytearray()
            while True:
                while len(buffer) >= 2:
                    b0, b1 = buffer[0], buffer[1]
                    fin = bool(b0 & 0x80)
                    opcode = b0 & 0x0F
                    is_masked = bool(b1 & 0x80)
                    payload_len = b1 & 0x7F
                    offset = 2
                    if payload_len == 126:
                        if len(buffer) < 4:
                            break
                        payload_len = struct.unpack(">H", buffer[2:4])[0]
                        offset = 4
                    elif payload_len == 127:
                        if len(buffer) < 10:
                            break
                        payload_len = struct.unpack(">Q", buffer[2:10])[0]
                        offset = 10

                    mask_len = 4 if is_masked else 0
                    total_frame_len = offset + mask_len + payload_len
                    if len(buffer) < total_frame_len:
                        break

                    frame_mask = buffer[offset : offset + 4] if is_masked else None
                    frame_payload = bytes(buffer[offset + mask_len : total_frame_len])
                    del buffer[:total_frame_len]

                    if frame_mask:
                        frame_payload = bytes(b ^ frame_mask[i % 4] for i, b in enumerate(frame_payload))

                    if opcode == 0x8:  # Close
                        raise RuntimeError("CDP WebSocket closed by peer")
                    elif opcode == 0x9:  # Ping -> send Pong (0xA)
                        pong_mask = os.urandom(4)
                        pong_masked = bytes(b ^ pong_mask[i % 4] for i, b in enumerate(frame_payload))
                        s.sendall(bytes([0x8A, 0x80 | len(frame_payload)]) + pong_mask + pong_masked)
                        continue
                    elif opcode in (0x1, 0x0):  # Text or continuation
                        fragments.extend(frame_payload)
                        if fin:
                            msg_text = fragments.decode("utf-8", errors="replace")
                            fragments.clear()
                            msg_obj = json.loads(msg_text)
                            if msg_obj.get("id") == expected_id:
                                return msg_obj

                chunk = s.recv(4096)
                if not chunk:
                    break
                buffer.extend(chunk)

            raise RuntimeError("Incomplete response from CDP WebSocket")
        finally:
            s.close()

    def set_cookies(self, cookies: List[Dict[str, Any]]) -> bool:
        """Inject cookies into browser via CDP Network.setCookies."""
        version_info = self.get_version()
        ws_url = version_info.get("webSocketDebuggerUrl")
        if not ws_url:
            targets = self.get_targets()
            if targets:
                ws_url = targets[0].get("webSocketDebuggerUrl")
        if not ws_url:
            raise RuntimeError("No WebSocket debugger URL available on CDP endpoint")

        res = self.send_ws_command(ws_url, "Network.setCookies", {"cookies": cookies})
        return "error" not in res

    def get_page_ws_url(self, timeout: float = 3.0) -> str:
        """Resolve the WebSocket debugger URL for the active page target."""
        try:
            targets = self.get_targets(timeout=timeout)
            for t in targets:
                if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
                    return t["webSocketDebuggerUrl"]
            if targets and targets[0].get("webSocketDebuggerUrl"):
                return targets[0]["webSocketDebuggerUrl"]
        except Exception:
            pass

        version_info = self.get_version(timeout=timeout)
        ws_url = version_info.get("webSocketDebuggerUrl")
        if ws_url:
            return ws_url
        raise RuntimeError(f"No active CDP page target found on {self.host}:{self.port}")

    def navigate(self, url: str) -> Dict[str, Any]:
        """Navigate active page to target URL via Page.navigate."""
        ws_url = self.get_page_ws_url()
        res = self.send_ws_command(ws_url, "Page.navigate", {"url": url})
        return {
            "ok": "error" not in res,
            "action": "navigate",
            "url": url,
            "frame_id": res.get("result", {}).get("frameId"),
            "response": res,
        }

    def screenshot(self, output_path: Optional[str] = None) -> Dict[str, Any]:
        """Capture page screenshot via Page.captureScreenshot and optionally write PNG file."""
        ws_url = self.get_page_ws_url()
        res = self.send_ws_command(ws_url, "Page.captureScreenshot", {"format": "png"})
        if "error" in res:
            return {"ok": False, "action": "screenshot", "error": res["error"]}

        b64_data = res.get("result", {}).get("data", "")
        saved_path = None
        if output_path:
            raw_bytes = base64.b64decode(b64_data)
            size_bytes = len(raw_bytes)
            os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
            with open(output_path, "wb") as f:
                f.write(raw_bytes)
            saved_path = os.path.abspath(output_path)
        else:
            size_bytes = len(base64.b64decode(b64_data)) if b64_data else 0

        return {
            "ok": True,
            "action": "screenshot",
            "size_bytes": size_bytes,
            "path": saved_path,
            "data": b64_data if not output_path else None,
        }

    def eval_js(self, expression: str) -> Dict[str, Any]:
        """Evaluate arbitrary JavaScript in page context via Runtime.evaluate."""
        ws_url = self.get_page_ws_url()
        res = self.send_ws_command(
            ws_url,
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
        )
        if "error" in res:
            return {"ok": False, "action": "eval", "error": res["error"]}

        result_obj = res.get("result", {}).get("result", {})
        val = result_obj.get("value")
        return {
            "ok": True,
            "action": "eval",
            "expression": expression,
            "type": result_obj.get("type"),
            "value": val,
        }

    def dom(self, limit: int = 200) -> Dict[str, Any]:
        """Extract visible, interactive DOM elements (buttons, inputs, links, text) with selectors."""
        script = f"""
        (() => {{
            const elements = [];
            const walker = document.createTreeWalker(
                document.body || document.documentElement,
                NodeFilter.SHOW_ELEMENT,
                null,
                false
            );
            let node;
            let count = 0;
            while ((node = walker.nextNode()) && count < {limit}) {{
                const tag = node.tagName.toLowerCase();
                const role = node.getAttribute('role') || '';
                const id = node.id || '';
                const className = typeof node.className === 'string' ? node.className : '';
                const isClickable = ['button', 'a', 'input', 'select', 'textarea'].includes(tag) ||
                                    role === 'button' || role === 'link' || node.onclick !== null;
                const isInput = ['input', 'textarea', 'select'].includes(tag);
                const rect = node.getBoundingClientRect();
                const visible = rect.width > 0 && rect.height > 0 &&
                                window.getComputedStyle(node).visibility !== 'hidden' &&
                                window.getComputedStyle(node).display !== 'none';
                if ((isClickable || isInput || ['h1','h2','h3','p'].includes(tag)) && visible) {{
                    const text = (node.innerText || node.textContent || node.placeholder || node.value || '').trim().slice(0, 100);
                    let sel = tag;
                    if (id) sel += '#' + id;
                    else if (className) sel += '.' + className.trim().split(/\\s+/)[0];
                    elements.push({{
                        tag: tag,
                        selector: sel,
                        role: role,
                        id: id,
                        text: text,
                        bounds: [Math.round(rect.left), Math.round(rect.top), Math.round(rect.width), Math.round(rect.height)],
                        center: [Math.round(rect.left + rect.width / 2), Math.round(rect.top + rect.height / 2)]
                    }});
                    count++;
                }}
            }}
            return {{
                title: document.title,
                url: window.location.href,
                count: elements.length,
                elements: elements
            }};
        }})()
        """
        res = self.eval_js(script)
        if not res.get("ok"):
            return res
        return {
            "ok": True,
            "action": "dom",
            "evidence": res.get("value", {}),
        }

    def click(self, selector: Optional[str] = None, x: Optional[int] = None, y: Optional[int] = None) -> Dict[str, Any]:
        """Click an element by CSS selector or dispatch mouse event at (x, y)."""
        ws_url = self.get_page_ws_url()
        if selector:
            script = f"""
            (() => {{
                const el = document.querySelector({json.dumps(selector)});
                if (!el) return {{ found: false }};
                el.scrollIntoView({{ block: 'center', inline: 'center' }});
                el.click();
                const r = el.getBoundingClientRect();
                return {{ found: true, center: [Math.round(r.left + r.width / 2), Math.round(r.top + r.height / 2)] }};
            }})()
            """
            eval_res = self.eval_js(script)
            val = eval_res.get("value", {}) if eval_res.get("ok") else {}
            if val.get("found"):
                return {
                    "ok": True,
                    "action": "click",
                    "method": "js_click",
                    "selector": selector,
                    "center": val.get("center"),
                }
            if x is None and y is None:
                return {
                    "ok": False,
                    "action": "click",
                    "error": f"Element '{selector}' not found in DOM",
                }

        if x is not None and y is not None:
            self.send_ws_command(
                ws_url,
                "Input.dispatchMouseEvent",
                {"type": "mousePressed", "x": int(x), "y": int(y), "button": "left", "clickCount": 1},
            )
            time.sleep(0.02)
            self.send_ws_command(
                ws_url,
                "Input.dispatchMouseEvent",
                {"type": "mouseReleased", "x": int(x), "y": int(y), "button": "left", "clickCount": 1},
            )
            return {
                "ok": True,
                "action": "click",
                "method": "input_click",
                "coordinates": {"x": int(x), "y": int(y)},
            }

        return {"ok": False, "action": "click", "error": "Must specify either selector or x, y coordinates"}

    def type_text(self, text: str, selector: Optional[str] = None) -> Dict[str, Any]:
        """Type text into an input field matching selector, or insert text at current focus."""
        ws_url = self.get_page_ws_url()
        if selector:
            script = f"""
            (() => {{
                const el = document.querySelector({json.dumps(selector)});
                if (!el) return false;
                el.focus();
                if ('value' in el) {{
                    el.value = {json.dumps(text)};
                    el.dispatchEvent(new Event('input', {{ bubbles: true }}));
                    el.dispatchEvent(new Event('change', {{ bubbles: true }}));
                    return true;
                }}
                return false;
            }})()
            """
            eval_res = self.eval_js(script)
            if eval_res.get("ok") and eval_res.get("value"):
                return {
                    "ok": True,
                    "action": "type",
                    "method": "dom_value",
                    "selector": selector,
                    "chars": len(text),
                }

        # Fallback to CDP Input.insertText
        res = self.send_ws_command(ws_url, "Input.insertText", {"text": text})
        return {
            "ok": "error" not in res,
            "action": "type",
            "method": "insert_text",
            "chars": len(text),
            "response": res,
        }


def load_cookie_file(cookie_path: str) -> List[Dict[str, Any]]:
    """Parse JSON cookies (standard export format like sockmusegoogle.json)."""
    with open(cookie_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, list):
        if isinstance(data, dict) and "cookies" in data:
            data = data["cookies"]
        else:
            raise ValueError("Expected JSON array of cookies")

    cdp_cookies = []
    for c in data:
        if not isinstance(c, dict) or "name" not in c or "value" not in c:
            continue
        cookie = {
            "name": c["name"],
            "value": c["value"],
            "domain": c.get("domain", ""),
            "path": c.get("path", "/"),
            "secure": bool(c.get("secure", False)),
            "httpOnly": bool(c.get("httpOnly", False)),
        }
        if c.get("expirationDate"):
            cookie["expires"] = float(c["expirationDate"])
        elif c.get("expires"):
            cookie["expires"] = float(c["expires"])

        same_site = c.get("sameSite")
        if isinstance(same_site, str) and same_site.lower() in ("strict", "lax", "none", "no_restriction"):
            cookie["sameSite"] = "None" if same_site.lower() in ("none", "no_restriction") else same_site.capitalize()

        cdp_cookies.append(cookie)
    return cdp_cookies


def _load_workers() -> List[Dict[str, Any]]:
    os.makedirs(BASE_WORKER_DIR, exist_ok=True)
    if os.path.exists(REGISTRY_FILE):
        try:
            with open(REGISTRY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []


def _save_workers(workers: List[Dict[str, Any]]) -> None:
    os.makedirs(BASE_WORKER_DIR, exist_ok=True)
    with open(REGISTRY_FILE, "w", encoding="utf-8") as f:
        json.dump(workers, f, indent=2)


def _is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if IS_WINDOWS:
        kernel32 = ctypes.windll.kernel32
        h_proc = kernel32.OpenProcess(0x1000, False, pid)
        if not h_proc:
            return False
        exit_code = wintypes.DWORD()
        kernel32.GetExitCodeProcess(h_proc, ctypes.byref(exit_code))
        kernel32.CloseHandle(h_proc)
        return exit_code.value == 259  # STILL_ACTIVE
    else:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes


def _spawn_detached_process(cmd: List[str]) -> Tuple[int, Optional[subprocess.Popen]]:
    """Spawn a process detached from parent lifecycle so worker survives CLI invocations."""
    if IS_WINDOWS:
        cmd_parts = []
        for arg in cmd:
            if " " in arg or '"' in arg:
                escaped = arg.replace('"', '\\"')
                cmd_parts.append(f'"{escaped}"')
            else:
                cmd_parts.append(arg)
        cmd_line = " ".join(cmd_parts)

        # Try spawning via WMI / Win32_Process for full detached top-level lifetime
        try:
            escaped_cmd = cmd_line.replace("'", "''")
            ps_script = (
                f"$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create "
                f"-Arguments @{{CommandLine='{escaped_cmd}'}}; "
                f"Write-Output $r.ProcessId"
            )
            res = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps_script],
                capture_output=True,
                text=True,
                timeout=10,
            )
            out = res.stdout.strip().splitlines()
            if out and out[0].strip().isdigit():
                pid = int(out[0].strip())
                if pid > 0:
                    return pid, None
        except Exception:
            pass

        # Fallback to standard Popen
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=0x08000000 | 0x00000200,  # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
        )
        return proc.pid, proc
    else:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return proc.pid, proc


def fork_browser(
    browser: Optional[str] = None,
    url: Optional[str] = None,
    headless: bool = False,
    port: Optional[int] = None,
    cookie_file: Optional[str] = None,
    copy_profile: bool = True,
    timeout: float = 10.0,
) -> Dict[str, Any]:
    """Spawn an ephemeral browser instance pre-loaded with credentials.

    Parameters:
        browser: 'brave', 'chrome', 'chromium', 'edge', or 'firefox'. Auto-detected if omitted.
        url: Initial destination URL (default: 'about:blank').
        headless: Run headless (default: False, runs visible window).
        port: Remote debugging port for CDP (auto-allocated if None).
        cookie_file: Path to JSON cookie file (e.g. sockmusegoogle.json).
        copy_profile: Copy master profile settings (Local State, Preferences, Bookmarks).
        timeout: Seconds to wait for browser readiness.
    """
    detected = detect_browsers()
    if not detected:
        return {
            "ok": False,
            "action": "browser.fork",
            "error": "No supported browser installed (checked Brave, Chrome, Chromium, Edge, Firefox)",
        }

    # Select browser
    chosen_name = browser.lower() if browser else None
    if not chosen_name:
        for pref in ["brave", "chrome", "chromium", "edge", "firefox"]:
            if pref in detected and detected[pref]["ready"]:
                chosen_name = pref
                break
    if not chosen_name or chosen_name not in detected or not detected[chosen_name]["ready"]:
        return {
            "ok": False,
            "action": "browser.fork",
            "error": f"Browser '{browser}' not available. Installed: {list(detected.keys())}",
        }

    b_info = detected[chosen_name]
    binary = b_info["binary"]
    b_type = b_info["type"]
    master_profile = b_info["profile"]

    worker_id = f"bwrk_{int(time.time())}_{os.urandom(3).hex()}"
    ephemeral_dir = os.path.join(BASE_WORKER_DIR, worker_id)
    os.makedirs(ephemeral_dir, exist_ok=True)

    target_port = port or find_free_port()
    injected_cookies_count = 0

    # 1. Profile snapshot (non-blocking copy of non-locked files)
    if copy_profile and master_profile and os.path.exists(master_profile):
        if b_type == "chromium":
            # Copy Local State for DPAPI key consistency
            ls_src = os.path.join(master_profile, "Local State")
            if os.path.exists(ls_src):
                try:
                    shutil.copy2(ls_src, os.path.join(ephemeral_dir, "Local State"))
                except Exception:
                    pass

            def_src = os.path.join(master_profile, "Default")
            def_dst = os.path.join(ephemeral_dir, "Default")
            os.makedirs(def_dst, exist_ok=True)

            for item in ["Preferences", "Secure Preferences", "Bookmarks", "Login Data", "Web Data"]:
                sp = os.path.join(def_src, item)
                if os.path.exists(sp):
                    try:
                        shutil.copy2(sp, os.path.join(def_dst, item))
                    except Exception:
                        pass
        elif b_type == "firefox":
            # Find default profile in Firefox Profiles dir
            if os.path.isdir(master_profile):
                subdirs = [d for d in os.listdir(master_profile) if os.path.isdir(os.path.join(master_profile, d))]
                default_sub = next((d for d in subdirs if "default-release" in d or "default" in d), subdirs[0] if subdirs else None)
                if default_sub:
                    src_dir = os.path.join(master_profile, default_sub)
                    for item in ["prefs.js", "logins.json", "key4.db", "cert9.db"]:
                        sp = os.path.join(src_dir, item)
                        if os.path.exists(sp):
                            try:
                                shutil.copy2(sp, os.path.join(ephemeral_dir, item))
                            except Exception:
                                pass

    # 2. Build command line
    cmd: List[str] = [binary]
    target_url = url or "about:blank"

    if b_type == "chromium":
        cmd.extend([
            f"--user-data-dir={ephemeral_dir}",
            f"--remote-debugging-port={target_port}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-networking",
            "--disable-component-update",
            "--disable-sync",
        ])
        if headless:
            cmd.append("--headless=new")
        cmd.append(target_url)
    else:  # Firefox
        cmd.extend([
            "-profile", ephemeral_dir,
            "-no-remote",
            target_url,
        ])

    # 3. Launch process detached
    try:
        proc_pid, _ = _spawn_detached_process(cmd)
    except Exception as e:
        shutil.rmtree(ephemeral_dir, ignore_errors=True)
        return {
            "ok": False,
            "action": "browser.fork",
            "error": f"Failed to spawn {chosen_name}: {e}",
        }

    # 4. Wait for readiness
    cdp_ready = False
    cdp_client = CDPClient(port=target_port)
    start_t = time.time()

    if b_type == "chromium":
        while time.time() - start_t < timeout:
            if not _is_pid_alive(proc_pid):
                break
            try:
                cdp_client.get_version(timeout=1.0)
                cdp_ready = True
                break
            except Exception:
                time.sleep(0.3)

    if not _is_pid_alive(proc_pid):
        shutil.rmtree(ephemeral_dir, ignore_errors=True)
        return {
            "ok": False,
            "action": "browser.fork",
            "error": f"{chosen_name} process exited immediately (PID {proc_pid})",
        }

    # 5. Inject cookies if requested
    if cookie_file and os.path.isfile(cookie_file) and cdp_ready:
        try:
            cookies = load_cookie_file(cookie_file)
            cdp_client.set_cookies(cookies)
            injected_cookies_count = len(cookies)
            # Reload page with cookies
            if url and url != "about:blank":
                vinfo = cdp_client.get_version()
                ws_url = vinfo.get("webSocketDebuggerUrl")
                if ws_url:
                    cdp_client.send_ws_command(ws_url, "Page.navigate", {"url": target_url})
        except Exception as e:
            pass

    # 6. Register worker
    worker_record = {
        "worker_id": worker_id,
        "pid": proc_pid,
        "browser": chosen_name,
        "binary": binary,
        "profile_dir": ephemeral_dir,
        "port": target_port,
        "headless": headless,
        "url": target_url,
        "created_at": int(time.time()),
        "authenticated": injected_cookies_count > 0,
    }
    workers = _load_workers()
    workers.append(worker_record)
    _save_workers(workers)

    return {
        "ok": True,
        "action": "browser.fork",
        "evidence": {
            "worker_id": worker_id,
            "pid": proc_pid,
            "browser": chosen_name,
            "profile_dir": ephemeral_dir,
            "cdp_port": target_port,
            "cdp_ready": cdp_ready,
            "headless": headless,
            "url": target_url,
            "cookies_injected": injected_cookies_count,
            "isolated": True,
        },
    }


def list_browser_workers() -> Dict[str, Any]:
    """List all running ephemeral browser workers, pruning terminated ones."""
    workers = _load_workers()
    alive_workers = []
    pruned_count = 0

    for w in workers:
        pid = w.get("pid", 0)
        if _is_pid_alive(pid):
            alive_workers.append(w)
        else:
            # Clean up residual disk directory if process is dead
            pdir = w.get("profile_dir")
            if pdir and os.path.exists(pdir):
                shutil.rmtree(pdir, ignore_errors=True)
            pruned_count += 1

    if pruned_count > 0 or len(alive_workers) != len(workers):
        _save_workers(alive_workers)

    return {
        "ok": True,
        "action": "browser.list",
        "evidence": {
            "count": len(alive_workers),
            "pruned": pruned_count,
            "workers": alive_workers,
        },
    }


def destroy_browser_worker(worker_id: str) -> Dict[str, Any]:
    """Terminate an ephemeral browser worker and securely purge its profile directory."""
    workers = _load_workers()
    if not workers:
        return {
            "ok": True,
            "action": "browser.destroy",
            "evidence": {"destroyed": 0, "message": "No active browser workers found"},
        }

    targets = []
    remaining = []

    if worker_id.lower() == "all":
        targets = workers
    else:
        for w in workers:
            if w.get("worker_id") == worker_id or str(w.get("pid")) == worker_id:
                targets.append(w)
            else:
                remaining.append(w)

    if not targets:
        return {
            "ok": False,
            "action": "browser.destroy",
            "error": f"Worker '{worker_id}' not found in registry",
            "evidence": {"worker_id": worker_id},
        }

    destroyed_evidence = []
    for t in targets:
        pid = t.get("pid")
        pdir = t.get("profile_dir")
        killed = False

        if pid and _is_pid_alive(pid):
            try:
                if IS_WINDOWS:
                    # Terminate process tree via taskkill
                    subprocess.run(
                        ["taskkill.exe", "/PID", str(pid), "/T", "/F"],
                        capture_output=True,
                        timeout=5,
                    )
                    time.sleep(0.2)
                    if not _is_pid_alive(pid):
                        killed = True
                    else:
                        kernel32 = ctypes.windll.kernel32
                        h = kernel32.OpenProcess(0x0001, False, pid)
                        if h:
                            kernel32.TerminateProcess(h, 0)
                            kernel32.CloseHandle(h)
                            killed = True
                else:
                    os.kill(pid, 15)  # SIGTERM
                    time.sleep(0.3)
                    if _is_pid_alive(pid):
                        os.kill(pid, 9)  # SIGKILL
                    killed = True
            except Exception:
                pass

        # Purge temporary directory
        purged = False
        if pdir and os.path.exists(pdir):
            for _ in range(6):
                try:
                    time.sleep(0.15)
                    shutil.rmtree(pdir, ignore_errors=True)
                    if not os.path.exists(pdir):
                        purged = True
                        break
                except Exception:
                    pass
            purged = not os.path.exists(pdir)

        destroyed_evidence.append({
            "worker_id": t.get("worker_id"),
            "pid": pid,
            "terminated": killed or not _is_pid_alive(pid),
            "profile_purged": purged,
        })

    _save_workers(remaining)
    return {
        "ok": True,
        "action": "browser.destroy",
        "evidence": {
            "count": len(destroyed_evidence),
            "workers": destroyed_evidence,
        },
    }


def browser_status(target: Optional[str] = None) -> Dict[str, Any]:
    """Inspect local browser readiness and remote cloud browser connectivity (Spine & Oracle)."""
    detected = detect_browsers()
    workers_res = list_browser_workers()
    active_workers = workers_res.get("evidence", {}).get("workers", [])

    import ssl
    ctx = ssl._create_unverified_context()

    targets_to_check = ["spine", "oracle"]
    if target and target.lower() in REMOTE_BROWSERS:
        t_key = "spine" if target.lower() == "sao" else target.lower()
        targets_to_check = [t_key]

    remote_statuses: Dict[str, Any] = {}
    for t_name in ["spine", "oracle"]:
        cfg = REMOTE_BROWSERS[t_name]
        t_url = cfg["url"].rstrip("/")
        st: Dict[str, Any] = {
            "name": cfg["name"],
            "url": t_url,
            "available": False,
        }
        if "cdp_ip" in cfg:
            st["cdp_endpoint"] = f"http://{cfg['cdp_ip']}:{cfg['cdp_port']}"
        if t_name in targets_to_check:
            try:
                req = urllib.request.Request(
                    f"{t_url}/sao-control/status",
                    headers={"User-Agent": "Architect-appctl/0.6", "X-SAO-Browser": "1"}
                )
                with urllib.request.urlopen(req, timeout=3.0, context=ctx) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    st["available"] = True
                    st["state"] = data
            except Exception as e:
                st["error"] = str(e)
        remote_statuses[t_name] = st

    active_target_key = "spine" if not target or target.lower() in ("spine", "sao") else target.lower()
    sao_bridge = remote_statuses.get(active_target_key, remote_statuses["spine"])

    return {
        "ok": True,
        "action": "browser.status",
        "evidence": {
            "installed_browsers": detected,
            "active_workers_count": len(active_workers),
            "active_workers": active_workers,
            "remote_browsers": remote_statuses,
            "sao_bridge": sao_bridge,
        },
    }


def get_unified_browser_url(sao_url: Optional[str] = None, target: str = "all") -> Dict[str, Any]:
    """Return canonical unified browser URLs and status for Master Control, The Architect, and agents."""
    status = browser_status()
    remote_map = status.get("evidence", {}).get("remote_browsers", {})
    spine_info = remote_map.get("spine", {})
    oracle_info = remote_map.get("oracle", {})

    spine_url = (sao_url or spine_info.get("url") or REMOTE_BROWSERS["spine"]["url"]).rstrip("/")
    oracle_url = (oracle_info.get("url") or REMOTE_BROWSERS["oracle"]["url"]).rstrip("/")

    if target and target.lower() in ("oracle", "oracle-browser"):
        return {
            "ok": True,
            "action": "browser.url",
            "evidence": {
                "target": "oracle",
                "name": "Oracle Browser",
                "unified_browser_url": f"{oracle_url}/",
                "webrtc_stream_url": f"{oracle_url}/",
                "kasm_web_url": "https://01-savvopsvm.taila7272b.ts.net:8447/",
                "sao_control_status_url": f"{oracle_url}/sao-control/status",
                "cdp_endpoint": "http://100.112.12.23:9223",
                "cdp_version_url": "http://100.112.12.23:9223/json/version",
                "bridge_connected": oracle_info.get("available", False),
                "owner_state": oracle_info.get("state", {}),
            },
        }

    if target and target.lower() in ("spine", "sao", "savv-spine"):
        return {
            "ok": True,
            "action": "browser.url",
            "evidence": {
                "target": "spine",
                "name": "Spine Browser (SAO)",
                "unified_browser_url": f"{spine_url}/",
                "webrtc_stream_url": f"{spine_url}/",
                "sao_control_status_url": f"{spine_url}/sao-control/status",
                "cdp_base_port": 9222,
                "bridge_connected": spine_info.get("available", False),
                "owner_state": spine_info.get("state", {}),
            },
        }

    return {
        "ok": True,
        "action": "browser.url",
        "evidence": {
            "unified_browser_url": f"{spine_url}/",
            "webrtc_stream_url": f"{spine_url}/",
            "sao_control_status_url": f"{spine_url}/sao-control/status",
            "cdp_base_port": 9222,
            "bridge_connected": spine_info.get("available", False),
            "owner_state": spine_info.get("state", {}),
            "browsers": {
                "spine": {
                    "name": "Spine Browser (SAO)",
                    "url": f"{spine_url}/",
                    "status_url": f"{spine_url}/sao-control/status",
                    "connected": spine_info.get("available", False),
                },
                "oracle": {
                    "name": "Oracle Browser",
                    "url": f"{oracle_url}/",
                    "kasm_url": "https://01-savvopsvm.taila7272b.ts.net:8447/",
                    "status_url": f"{oracle_url}/sao-control/status",
                    "cdp_endpoint": "http://100.112.12.23:9223",
                    "connected": oracle_info.get("available", False),
                },
            },
        },
    }


class SAOBrowserBridge:
    """Bridge adapter connecting remote SAO Browser (savv-spine:6092) with Architect's ephemeral workers."""

    def __init__(self, sao_url: str = SAO_BROWSER_URL):
        self.sao_url = sao_url.rstrip("/")
        import ssl
        self.ctx = ssl._create_unverified_context()

    def get_status(self, timeout: float = 3.0) -> Dict[str, Any]:
        """Fetch remote SAO Browser state."""
        try:
            req = urllib.request.Request(
                f"{self.sao_url}/sao-control/status",
                headers={"User-Agent": "Architect-Bridge/1.0", "X-SAO-Browser": "1"},
            )
            with urllib.request.urlopen(req, timeout=timeout, context=self.ctx) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return {"available": True, "state": data}
        except Exception as e:
            return {"available": False, "error": str(e)}

    def takeover(self, timeout: float = 3.0) -> Dict[str, Any]:
        """Pause autonomous agents on SAO Browser and lock viewport for exclusive owner control."""
        try:
            req = urllib.request.Request(
                f"{self.sao_url}/sao-control/owner/stop",
                data=b"{}",
                method="POST",
                headers={
                    "User-Agent": "Architect-Bridge/1.0",
                    "X-SAO-Browser": "1",
                    "Content-Type": "application/json",
                },
            )
            with urllib.request.urlopen(req, timeout=timeout, context=self.ctx) as resp:
                data = json.loads(resp.read().decode("utf-8")) if resp.length != 0 else {}
                return {
                    "ok": True,
                    "action": "browser.takeover",
                    "evidence": {
                        "paused": True,
                        "sao_url": f"{self.sao_url}/",
                        "response": data,
                    },
                }
        except Exception as e:
            return {
                "ok": False,
                "action": "browser.takeover",
                "error": str(e),
                "evidence": {"sao_url": f"{self.sao_url}/"},
            }

    def resume(self, timeout: float = 3.0) -> Dict[str, Any]:
        """Resume autonomous agent operations on SAO Browser."""
        try:
            req = urllib.request.Request(
                f"{self.sao_url}/sao-control/owner/resume",
                data=b"{}",
                method="POST",
                headers={
                    "User-Agent": "Architect-Bridge/1.0",
                    "X-SAO-Browser": "1",
                    "Content-Type": "application/json",
                },
            )
            with urllib.request.urlopen(req, timeout=timeout, context=self.ctx) as resp:
                data = json.loads(resp.read().decode("utf-8")) if resp.length != 0 else {}
                return {
                    "ok": True,
                    "action": "browser.resume",
                    "evidence": {
                        "paused": False,
                        "sao_url": f"{self.sao_url}/",
                        "response": data,
                    },
                }
        except Exception as e:
            return {
                "ok": False,
                "action": "browser.resume",
                "error": str(e),
                "evidence": {"sao_url": f"{self.sao_url}/"},
            }

    def route_request(
        self,
        url: Optional[str] = None,
        cookie_file: Optional[str] = None,
        headless: bool = True,
        prefer_ephemeral: bool = True,
    ) -> Dict[str, Any]:
        """Decide whether to execute via local ephemeral worker or report SAO availability.

        If SAO Browser is paused by owner, busy, or prefer_ephemeral is True:
        Automatically offloads the agent request to an ephemeral Architect browser worker,
        guaranteeing the owner's session is never interrupted and eliminating queue wait time.
        """
        sao_res = self.get_status()
        sao_state = sao_res.get("state", {}) if sao_res.get("available") else {}

        is_paused = sao_state.get("paused", False)
        is_busy = sao_state.get("busy", False)
        queue_len = len(sao_state.get("queue", []))
        holder = sao_state.get("holder")

        if not prefer_ephemeral and sao_res.get("available") and not is_paused and not is_busy and queue_len == 0:
            return {
                "ok": True,
                "action": "browser.bridge",
                "evidence": {
                    "bridge_mode": "shared_sao",
                    "offload_reason": None,
                    "sao_status": sao_res,
                    "unified_browser_url": f"{self.sao_url}/",
                    "worker": None,
                    "isolated": False,
                    "owner_viewport_protected": True,
                },
            }

        # Determine routing reason
        reason = "concurrency_isolation"
        if not sao_res.get("available"):
            reason = "sao_offline_fallback"
        elif is_paused:
            reason = "owner_takeover_active"
        elif is_busy or queue_len > 0:
            reason = f"sao_busy_holder_{holder}"

        # Fork ephemeral worker
        fork_res = fork_browser(
            url=url,
            cookie_file=cookie_file,
            headless=headless,
        )

        return {
            "ok": fork_res.get("ok", False),
            "action": "browser.bridge",
            "evidence": {
                "bridge_mode": "offload_ephemeral",
                "offload_reason": reason,
                "sao_status": sao_res,
                "worker": fork_res.get("evidence"),
                "isolated": True,
                "owner_viewport_protected": True,
            },
        }


def bridge_browser(
    url: Optional[str] = None,
    cookie_file: Optional[str] = None,
    headless: bool = True,
    prefer_ephemeral: bool = True,
    target: Optional[str] = None,
    sao_url: Optional[str] = None,
) -> Dict[str, Any]:
    """Bridge remote browser requests (Spine or Oracle) to Architect's ephemeral worker infrastructure."""
    dest_url = sao_url
    if not dest_url:
        t_key = "oracle" if target and target.lower() in ("oracle", "oracle-browser") else "spine"
        dest_url = REMOTE_BROWSERS[t_key]["url"]
    bridge = SAOBrowserBridge(sao_url=dest_url)
    return bridge.route_request(
        url=url,
        cookie_file=cookie_file,
        headless=headless,
        prefer_ephemeral=prefer_ephemeral,
    )


def takeover_browser(target: Optional[str] = None, sao_url: Optional[str] = None) -> Dict[str, Any]:
    """Pause agents on remote browser (Spine or Oracle) for exclusive manual control."""
    dest_url = sao_url
    if not dest_url:
        t_key = "oracle" if target and target.lower() in ("oracle", "oracle-browser") else "spine"
        dest_url = REMOTE_BROWSERS[t_key]["url"]
    bridge = SAOBrowserBridge(sao_url=dest_url)
    return bridge.takeover()


def resume_browser(target: Optional[str] = None, sao_url: Optional[str] = None) -> Dict[str, Any]:
    """Resume agent access on remote browser (Spine or Oracle)."""
    dest_url = sao_url
    if not dest_url:
        t_key = "oracle" if target and target.lower() in ("oracle", "oracle-browser") else "spine"
        dest_url = REMOTE_BROWSERS[t_key]["url"]
    bridge = SAOBrowserBridge(sao_url=dest_url)
    return bridge.resume()


def act_on_worker(
    worker_id: str,
    action: str,
    url: Optional[str] = None,
    selector: Optional[str] = None,
    text: Optional[str] = None,
    expr: Optional[str] = None,
    output: Optional[str] = None,
    x: Optional[int] = None,
    y: Optional[int] = None,
    timeout: float = 10.0,
) -> Dict[str, Any]:
    """Execute CDP browser action on a running ephemeral worker or remote cloud browser (Oracle/Spine)."""
    cdp_port = None
    if worker_id.lower() in ("oracle", "oracle-browser"):
        cdp_host = REMOTE_BROWSERS["oracle"]["cdp_ip"]
        cdp_port = REMOTE_BROWSERS["oracle"]["cdp_port"]
        client = CDPClient(host=cdp_host, port=cdp_port)
    elif worker_id.lower() in ("spine", "sao", "savv-spine"):
        cdp_host = REMOTE_BROWSERS["spine"]["host"]
        cdp_port = REMOTE_BROWSERS["spine"]["cdp_port"]
        client = CDPClient(host=cdp_host, port=cdp_port)
    else:
        workers = _load_workers()
        worker = None
        for w in workers:
            if w.get("worker_id") == worker_id or str(w.get("pid")) == worker_id:
                worker = w
                break

        if not worker:
            return {
                "ok": False,
                "action": f"browser.act.{action}",
                "error": f"Worker '{worker_id}' not found in registry",
            }

        pid = worker.get("pid", 0)
        if not _is_pid_alive(pid):
            return {
                "ok": False,
                "action": f"browser.act.{action}",
                "error": f"Worker process PID {pid} is no longer running",
            }

        cdp_port = worker.get("cdp_port")
        if not cdp_port:
            return {
                "ok": False,
                "action": f"browser.act.{action}",
                "error": f"Worker '{worker_id}' does not have a CDP port configured",
            }

        client = CDPClient(port=cdp_port)

    try:
        if action == "navigate":
            if not url:
                return {"ok": False, "action": "browser.act.navigate", "error": "Missing 'url' argument"}
            res = client.navigate(url)
        elif action == "screenshot":
            res = client.screenshot(output_path=output)
        elif action == "eval":
            if not expr:
                return {"ok": False, "action": "browser.act.eval", "error": "Missing 'expr' argument"}
            res = client.eval_js(expr)
        elif action == "dom":
            res = client.dom()
        elif action == "click":
            res = client.click(selector=selector, x=x, y=y)
        elif action == "type":
            if text is None:
                return {"ok": False, "action": "browser.act.type", "error": "Missing 'text' argument"}
            res = client.type_text(text, selector=selector)
        else:
            return {
                "ok": False,
                "action": f"browser.act.{action}",
                "error": f"Unsupported browser action: '{action}'",
            }

        return {
            "ok": res.get("ok", True),
            "action": f"browser.act.{action}",
            "worker_id": worker_id,
            "cdp_port": cdp_port,
            "evidence": res,
        }
    except Exception as e:
        return {
            "ok": False,
            "action": f"browser.act.{action}",
            "worker_id": worker_id,
            "error": str(e),
        }

