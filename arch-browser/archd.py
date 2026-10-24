#!/usr/bin/env python3
"""archd.py — Arch Browser S-Tier Daemon.

Unifies:
1. Direct CDP bridge client (Chrome DevTools Protocol on 127.0.0.1:9222)
2. Semantic A-Tier DOM & Accessibility extraction (data-arch-id attributes)
3. Owner Takeover / Exclusive Agent Lease Broker
4. Cost-Escalating Model Router integration (Tier 0 -> Tier 1 -> Tier 2)
5. Autonomous goal execution loop (/api/task)
"""

import base64
import hashlib
import json
import os
import re
import socket
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional

from router import ModelRouter

VERSION = "0.2.0"
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class WSError(Exception):
    pass


class CDPError(Exception):
    pass


# ------------------------------------------------------------- WebSocket Client
class WS:
    def __init__(self, url: str, timeout: float = 10):
        assert url.startswith("ws://"), url
        rest = url[5:]
        hostport, _, path = rest.partition("/")
        host, _, port = hostport.partition(":")
        self.sock = socket.create_connection((host, int(port or 80)), timeout=timeout)
        self.sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            f"GET /{path} HTTP/1.1\r\n"
            f"Host: {hostport}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(req.encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise WSError("Handshake: connection closed")
            resp += chunk
        head, _, self._buf = resp.partition(b"\r\n\r\n")
        if " 101 " not in head.decode("latin1").split("\r\n", 1)[0]:
            raise WSError("Handshake failed")

    def _read(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise WSError("Connection closed")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def send_text(self, text: str):
        payload = text.encode("utf-8")
        mask = os.urandom(4)
        n = len(payload)
        hdr = bytearray([0x81])
        if n < 126:
            hdr.append(0x80 | n)
        elif n < 1 << 16:
            hdr += bytes([0x80 | 126]) + n.to_bytes(2, "big")
        else:
            hdr += bytes([0x80 | 127]) + n.to_bytes(8, "big")
        hdr += mask
        self.sock.sendall(bytes(hdr) + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def recv_message(self) -> str:
        chunks = []
        while True:
            b1, b2 = self._read(2)
            fin, opcode = b1 & 0x80, b1 & 0x0F
            masked, ln = b2 & 0x80, b2 & 0x7F
            if ln == 126:
                ln = int.from_bytes(self._read(2), "big")
            elif ln == 127:
                ln = int.from_bytes(self._read(8), "big")
            mask = self._read(4) if masked else None
            payload = self._read(ln)
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 0x8:
                raise WSError("Peer closed")
            if opcode in (0x1, 0x0):
                chunks.append(payload)
                if fin:
                    return b"".join(chunks).decode("utf-8")


# ------------------------------------------------------------- CDP Driver
JS_DOM_INDEX = """() => {
  let id = 1;
  const interactive = [];
  const nodes = document.querySelectorAll('a, button, input, select, textarea, [role="button"], [role="checkbox"], [role="link"], [onclick]');
  for (const el of nodes) {
    const rect = el.getBoundingClientRect();
    if (rect.width === 0 || rect.height === 0 || window.getComputedStyle(el).visibility === 'hidden') continue;
    const archId = 'arch-' + (id++);
    el.setAttribute('data-arch-id', archId);
    let text = (el.innerText || el.value || el.placeholder || el.getAttribute('aria-label') || '').trim();
    if (text.length > 80) text = text.slice(0, 77) + '...';
    interactive.push({
      id: archId,
      selector: `[data-arch-id="${archId}"]`,
      tag: el.tagName.toLowerCase(),
      type: el.type || null,
      role: el.getAttribute('role') || el.tagName.toLowerCase(),
      text: text,
      disabled: el.disabled || false
    });
  }
  return interactive;
}"""


class CDPDriver:
    def __init__(self, port: int = 9222):
        self.port = port
        self.lock = threading.Lock()
        self.ws: Optional[WS] = None
        self._msg_id = 0
        self.pending: List[Dict[str, Any]] = []
        self._ensure()

    def _get_json(self, path: str):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=8) as r:
            return json.loads(r.read())

    def _ensure(self):
        targets = self._get_json("/json/list")
        page = next((t for t in targets if t.get("type") == "page"), None)
        if not page:
            page = self._get_json("/json/new?about:blank")
        if self.ws:
            try:
                self.ws.sock.close()
            except Exception:
                pass
        self.ws = WS(page["webSocketDebuggerUrl"])
        self.pending = []
        self.call("Page.enable")
        self.call("Runtime.enable")
        self.call("DOM.enable")

    def call(self, method: str, params: Optional[Dict[str, Any]] = None, timeout: float = 15) -> Dict[str, Any]:
        with self.lock:
            self._msg_id += 1
            cid = self._msg_id
            self.ws.send_text(json.dumps({"id": cid, "method": method, "params": params or {}}))
            deadline = time.time() + timeout
            while True:
                for i, m in enumerate(self.pending):
                    if m.get("id") == cid:
                        del self.pending[i]
                        if "error" in m:
                            raise CDPError(m["error"])
                        return m.get("result", {})
                rem = deadline - time.time()
                if rem <= 0:
                    raise CDPError(f"Timeout waiting for {method}")
                self.ws.sock.settimeout(rem)
                try:
                    msg = json.loads(self.ws.recv_message())
                except socket.timeout:
                    raise CDPError(f"Timeout waiting for {method}")
                if msg.get("id") == cid:
                    if "error" in msg:
                        raise CDPError(msg["error"])
                    return msg.get("result", {})
                self.pending.append(msg)

    def eval(self, expr: str) -> Any:
        res = self.call("Runtime.evaluate", {"expression": expr, "returnByValue": True, "awaitPromise": True})
        val = res.get("result", {})
        if "value" in val:
            return val["value"]
        return val

    def status(self) -> Dict[str, Any]:
        targets = self._get_json("/json/list")
        url = self.eval("window.location.href")
        title = self.eval("document.title")
        return {
            "url": url,
            "title": title,
            "tabs_count": len([t for t in targets if t.get("type") == "page"]),
        }

    def dom_elements(self) -> List[Dict[str, Any]]:
        return self.eval(f"({JS_DOM_INDEX})()") or []

    def click(self, selector: str) -> bool:
        js = f"""(() => {{
          const el = document.querySelector({json.dumps(selector)});
          if (!el) return false;
          el.scrollIntoView({{block: 'center', inline: 'center'}});
          el.click();
          return true;
        }})()"""
        return bool(self.eval(js))

    def fill(self, selector: str, text: str) -> bool:
        js = f"""(() => {{
          const el = document.querySelector({json.dumps(selector)});
          if (!el) return false;
          el.focus();
          el.value = {json.dumps(text)};
          el.dispatchEvent(new Event('input', {{bubbles: true}}));
          el.dispatchEvent(new Event('change', {{bubbles: true}}));
          return true;
        }})()"""
        return bool(self.eval(js))

    def type_keys(self, text: str, selector: Optional[str] = None) -> bool:
        if selector:
            self.click(selector)
        for ch in text:
            self.call("Input.dispatchKeyEvent", {"type": "keyDown", "text": ch, "unmodifiedText": ch})
            self.call("Input.dispatchKeyEvent", {"type": "keyUp", "text": ch, "unmodifiedText": ch})
        return True

    def press_key(self, key_name: str) -> bool:
        self.call("Input.dispatchKeyEvent", {"type": "rawKeyDown", "windowsVirtualKeyCode": 13 if key_name == "Enter" else 9 if key_name == "Tab" else 27, "key": key_name})
        self.call("Input.dispatchKeyEvent", {"type": "keyUp", "key": key_name})
        return True

    def navigate(self, url: str) -> Dict[str, Any]:
        return self.call("Page.navigate", {"url": url})

    def screenshot(self) -> bytes:
        res = self.call("Page.captureScreenshot", {"format": "png"})
        return base64.b64decode(res["data"])


# ------------------------------------------------------------- Broker & State
class Broker:
    def __init__(self):
        self.lock = threading.Lock()
        self.owner_takeover = False
        self.current_lease: Optional[Dict[str, Any]] = None
        self.task_running = False

    def takeover(self) -> Dict[str, Any]:
        with self.lock:
            self.owner_takeover = True
            return {"ok": True, "owner_takeover": True, "message": "Human takeover active. Agent mutations locked."}

    def release_takeover(self) -> Dict[str, Any]:
        with self.lock:
            self.owner_takeover = False
            return {"ok": True, "owner_takeover": False, "message": "Human takeover released. Agents allowed."}

    def check_agent_allowed(self) -> Optional[str]:
        with self.lock:
            if self.owner_takeover:
                return "Human in control: agent operations locked"
        return None


DRIVER: Optional[CDPDriver] = None
BROKER = Broker()
ROUTER = ModelRouter()


# ------------------------------------------------------------- HTTP Server
class ArchHandler(BaseHTTPRequestHandler):
    def _send_json(self, code: int, data: Any):
        body = json.dumps(data, indent=2).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> Dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        except Exception:
            return {}

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path in ("/", "/index.html"):
            ui_path = os.path.join(os.path.dirname(__file__), "ui", "index.html")
            if os.path.exists(ui_path):
                with open(ui_path, "rb") as f:
                    content = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
                return

        if path == "/api/status":
            st = DRIVER.status() if DRIVER else {}
            st.update({
                "owner_takeover": BROKER.owner_takeover,
                "tier1_model": ROUTER.tier1_model,
                "tier2_model": ROUTER.tier2_model,
                "jev_url": ROUTER.jev_url,
                "version": VERSION,
            })
            return self._send_json(200, {"ok": True, **st})

        if path == "/api/dom":
            elements = DRIVER.dom_elements() if DRIVER else []
            return self._send_json(200, {"ok": True, "count": len(elements), "elements": elements})

        if path == "/api/screenshot":
            data = DRIVER.screenshot() if DRIVER else b""
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        if path == "/healthz":
            return self._send_json(200, {"ok": True, "service": "archd"})

        self._send_json(404, {"ok": False, "error": "Not found"})

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        body = self._body()

        # Owner controls (always bypass agent lock)
        if path == "/api/takeover":
            return self._send_json(200, BROKER.takeover())
        if path == "/api/release":
            return self._send_json(200, BROKER.release_takeover())

        # Check human lock for agent actions
        lock_err = BROKER.check_agent_allowed()
        if lock_err:
            return self._send_json(423, {"ok": False, "error": lock_err, "owner_takeover": True})

        if path == "/api/navigate" or path == "/api/goto":
            url = body.get("url")
            if not url:
                return self._send_json(400, {"ok": False, "error": "URL required"})
            DRIVER.navigate(url)
            time.sleep(1.0)
            return self._send_json(200, {"ok": True, **DRIVER.status()})

        if path == "/api/click":
            sel = body.get("selector")
            ok = DRIVER.click(sel) if sel else False
            return self._send_json(200 if ok else 400, {"ok": ok, "selector": sel})

        if path == "/api/fill":
            sel, text = body.get("selector"), body.get("text", "")
            ok = DRIVER.fill(sel, text) if sel else False
            return self._send_json(200 if ok else 400, {"ok": ok, "selector": sel})

        if path == "/api/type":
            text, sel = body.get("text", ""), body.get("selector")
            ok = DRIVER.type_keys(text, sel)
            return self._send_json(200, {"ok": ok})

        if path == "/api/press":
            key = body.get("key", "Enter")
            ok = DRIVER.press_key(key)
            return self._send_json(200, {"ok": ok, "key": key})

        if path == "/api/classify":
            # Direct access to the Cost-Escalating Classifier (Tier 0 -> Tier 1 -> Tier 2)
            state = body.get("state", {})
            question = body.get("question", "Choose the best option.")
            options = body.get("options", [])
            res = ROUTER.classify(state, question, options)
            return self._send_json(200, {"ok": True, **res})

        if path == "/api/task":
            # Autonomous task runner with observe -> act -> verify loop
            goal = body.get("goal")
            if not goal:
                return self._send_json(400, {"ok": False, "error": "Goal required"})

            max_steps = int(body.get("max_steps", 10))
            history: List[Dict[str, Any]] = []
            failure_count = 0

            for step in range(1, max_steps + 1):
                if BROKER.owner_takeover:
                    return self._send_json(423, {"ok": False, "error": "Owner took over; task paused", "history": history})

                st = DRIVER.status()
                elements = DRIVER.dom_elements()
                action_plan = ROUTER.plan_next_action(goal, st.get("url", ""), st.get("title", ""), elements, history, failure_count)

                act = action_plan.get("action")
                if act == "done":
                    history.append({"step": step, "plan": action_plan, "status": "completed"})
                    return self._send_json(200, {"ok": True, "status": "completed", "summary": action_plan.get("summary"), "history": history})

                if act == "takeover_request":
                    return self._send_json(200, {"ok": False, "status": "takeover_requested", "reason": action_plan.get("reason"), "history": history})

                # Execute action
                prev_url = st.get("url")
                act_ok = False
                if act == "click":
                    act_ok = DRIVER.click(action_plan.get("selector", ""))
                elif act == "fill":
                    act_ok = DRIVER.fill(action_plan.get("selector", ""), action_plan.get("text", ""))
                elif act == "type":
                    act_ok = DRIVER.type_keys(action_plan.get("text", ""), action_plan.get("selector"))
                elif act == "press":
                    act_ok = DRIVER.press_key(action_plan.get("key", "Enter"))
                elif act == "navigate":
                    DRIVER.navigate(action_plan.get("url", ""))
                    act_ok = True
                elif act == "wait":
                    time.sleep(2.0)
                    act_ok = True

                time.sleep(0.8)
                new_st = DRIVER.status()
                # Verify state progress
                state_progress = (new_st.get("url") != prev_url) or act_ok
                if not state_progress:
                    failure_count += 1
                else:
                    failure_count = 0

                history.append({
                    "step": step,
                    "plan": action_plan,
                    "executed": act_ok,
                    "verified_progress": state_progress,
                    "url": new_st.get("url"),
                })

            return self._send_json(200, {"ok": False, "status": "max_steps_reached", "history": history})

        self._send_json(404, {"ok": False, "error": "Not found"})


def main():
    cdp_port = int(os.environ.get("ARCH_CDP_PORT", "9222"))
    port = int(os.environ.get("ARCH_PORT", "8901"))
    global DRIVER
    print(f"Connecting archd to Chrome DevTools on 127.0.0.1:{cdp_port}...")
    DRIVER = CDPDriver(port=cdp_port)
    print(f"archd {VERSION} running on http://0.0.0.0:{port}")
    server = ThreadingHTTPServer(("0.0.0.0", port), ArchHandler)
    server.serve_forever()


if __name__ == "__main__":
    main()