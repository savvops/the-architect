#!/usr/bin/env python3
"""archd.py — Arch Browser daemon.

Unifies:
1. Direct CDP client (Chrome DevTools Protocol on 127.0.0.1:9222, loopback only)
2. Numbered interactive DOM with handles + bounds (click/fill by index or arch-id)
3. Owner takeover + exclusive agent lease arbitration
4. Cost-escalating model router (Tier 0 -> Tier 1 -> Tier 2 -> owner)
5. Autonomous goal loop (/api/task) that returns per-step evidence

Security posture (fail-closed):
- Binds 127.0.0.1 by default (ARCH_BIND). Remote access goes through Tailscale Serve.
- If ARCH_TOKEN is set, every /api/* request needs `Authorization: Bearer <token>`
  (constant-time compare). /healthz stays open for the container healthcheck.
- POST bodies must be application/json (blocks cross-site text/plain form posts).
- No wildcard CORS.
"""

import base64
import hmac
import json
import mimetypes
import os
import select
import socket
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional

from router import ModelRouter

VERSION = "0.3.0"


class WSError(Exception):
    pass


class CDPError(Exception):
    pass


# ------------------------------------------------------------- WebSocket Client
class WS:
    def __init__(self, url: str, timeout: float = 10):
        if not url.startswith("ws://"):
            raise WSError(f"unsupported websocket url: {url}")
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

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass

    def _read(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise WSError("Connection closed")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _send_frame(self, opcode: int, payload: bytes):
        mask = os.urandom(4)
        n = len(payload)
        hdr = bytearray([0x80 | opcode])
        if n < 126:
            hdr.append(0x80 | n)
        elif n < 1 << 16:
            hdr += bytes([0x80 | 126]) + n.to_bytes(2, "big")
        else:
            hdr += bytes([0x80 | 127]) + n.to_bytes(8, "big")
        hdr += mask
        self.sock.sendall(bytes(hdr) + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def send_text(self, text: str):
        self._send_frame(0x1, text.encode("utf-8"))

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
            if opcode == 0x9:  # ping -> pong
                self._send_frame(0xA, payload)
                continue
            if opcode in (0x1, 0x2, 0x0):
                chunks.append(payload)
                if fin:
                    return b"".join(chunks).decode("utf-8")


# ------------------------------------------------------------- DOM index
# Every interactive, visible element gets a stable-for-this-snapshot integer index
# and a data-arch-id handle. Unnamed icon buttons are KEPT (name falls back to
# title/alt/svg title/class hint) so agents can still target them by index.
JS_DOM_INDEX = r"""() => {
  document.querySelectorAll('[data-arch-id]').forEach(e => e.removeAttribute('data-arch-id'));
  const SEL = 'a[href], button, input:not([type=hidden]), select, textarea, summary, ' +
    '[contenteditable=""], [contenteditable="true"], [tabindex]:not([tabindex="-1"]), [onclick], ' +
    '[role=button], [role=link], [role=checkbox], [role=radio], [role=tab], [role=menuitem], ' +
    '[role=option], [role=switch], [role=textbox], [role=combobox], [role=searchbox], [role=slider]';
  const vw = window.innerWidth, vh = window.innerHeight;
  const out = [];
  let idx = 1;
  for (const el of document.querySelectorAll(SEL)) {
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) continue;
    const cs = window.getComputedStyle(el);
    if (cs.visibility === 'hidden' || cs.display === 'none' || parseFloat(cs.opacity) === 0) continue;
    const img = el.querySelector && el.querySelector('img[alt], svg title');
    let name = (el.getAttribute('aria-label') || el.innerText || el.value || el.placeholder ||
      el.getAttribute('title') || el.getAttribute('alt') ||
      (img ? (img.getAttribute('alt') || img.textContent) : '') || el.getAttribute('name') || '').trim().replace(/\s+/g, ' ');
    if (name.length > 80) name = name.slice(0, 77) + '...';
    const id = 'arch-' + idx;
    el.setAttribute('data-arch-id', id);
    const tag = el.tagName.toLowerCase();
    const item = {
      index: idx, id, tag,
      role: el.getAttribute('role') || tag,
      name,
      type: el.type || null,
      bounds: {x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height)},
      in_viewport: r.bottom > 0 && r.right > 0 && r.top < vh && r.left < vw,
      disabled: !!el.disabled || el.getAttribute('aria-disabled') === 'true'
    };
    if (tag === 'a') item.href = el.href;
    if ('value' in el && tag !== 'button' && el.type !== 'password') item.value = String(el.value).slice(0, 200);
    if (el.type === 'checkbox' || el.type === 'radio') item.checked = !!el.checked;
    out.push(item);
    idx++;
  }
  return out;
}"""

KEY_CODES = {
    "Enter": 13, "Tab": 9, "Escape": 27, "Backspace": 8, "Delete": 46, "Space": 32,
    "ArrowLeft": 37, "ArrowUp": 38, "ArrowRight": 39, "ArrowDown": 40,
    "PageUp": 33, "PageDown": 34, "Home": 36, "End": 35,
}


def normalize_selector(s: Any) -> str:
    """Accept '7', 7, 'arch-7', '@arch-7', '#7' or a CSS selector."""
    if s is None:
        return ""
    s = str(s).strip()
    if s.isdigit():
        return f'[data-arch-id="arch-{s}"]'
    if s.startswith("#") and s[1:].isdigit():
        return f'[data-arch-id="arch-{s[1:]}"]'
    if s.startswith("@"):
        s = s[1:]
    if s.startswith("arch-") and s[5:].isdigit():
        return f'[data-arch-id="{s}"]'
    return s


class CDPDriver:
    def __init__(self, port: int = 9222, dialog_policy: str = "auto"):
        self.port = port
        self.lock = threading.RLock()
        self.ws: Optional[WS] = None
        self._msg_id = 0
        self.pending: Dict[int, Dict[str, Any]] = {}
        self.dialog: Optional[Dict[str, Any]] = None
        self.dialog_policy = dialog_policy
        self.downloads: List[Dict[str, Any]] = []
        self._ensure()

    # -- transport
    def _http_json(self, path: str, method: str = "GET"):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method=method)
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.loads(r.read())

    def _new_target(self, url: str = "about:blank") -> Dict[str, Any]:
        # Chromium >= 111 rejects GET /json/new; PUT is required.
        return self._http_json("/json/new?" + urllib.parse.quote(url, safe=":/?&=%"), method="PUT")

    def _ensure(self, max_retries: int = 30, target_id: Optional[str] = None):
        last_err = None
        for _ in range(max_retries):
            try:
                targets = self._http_json("/json/list")
                pages = [t for t in targets if t.get("type") == "page"]
                page = next((t for t in pages if t.get("id") == target_id), None) if target_id else None
                page = page or (pages[0] if pages else self._new_target())
                if self.ws:
                    self.ws.close()
                self.ws = WS(page["webSocketDebuggerUrl"])
                self.target_id = page.get("id")
                self.pending = {}
                self._raw_call("Page.enable")
                self._raw_call("Runtime.enable")
                self._raw_call("DOM.enable")
                try:
                    self._raw_call("Browser.setDownloadBehavior",
                                   {"behavior": "allow", "downloadPath": os.environ.get("ARCH_DOWNLOADS", "/data/downloads"),
                                    "eventsEnabled": True})
                except CDPError:
                    pass
                return
            except Exception as e:  # noqa: BLE001 - retry anything during startup
                last_err = e
                time.sleep(1)
        raise CDPError(f"Could not connect to Chrome DevTools on port {self.port}: {last_err}")

    def _on_event(self, msg: Dict[str, Any]):
        method = msg.get("method")
        params = msg.get("params", {})
        if method == "Page.javascriptDialogOpening":
            self.dialog = {"type": params.get("type"), "message": params.get("message", ""), "url": params.get("url")}
            if self.dialog_policy == "auto" and params.get("type") in ("alert", "beforeunload"):
                self.ws.send_text(json.dumps({"id": 0, "method": "Page.handleJavaScriptDialog", "params": {"accept": True}}))
                self.dialog["auto_handled"] = True
        elif method == "Page.javascriptDialogClosed":
            self.dialog = None
        elif method in ("Browser.downloadWillBegin", "Page.downloadWillBegin"):
            self.downloads.append({"url": params.get("url"), "file": params.get("suggestedFilename"), "guid": params.get("guid"), "state": "started"})
            self.downloads = self.downloads[-20:]
        elif method in ("Browser.downloadProgress", "Page.downloadProgress"):
            for d in self.downloads:
                if d.get("guid") == params.get("guid"):
                    d["state"] = params.get("state", d["state"])

    def _raw_call(self, method: str, params: Optional[Dict[str, Any]] = None, timeout: float = 15) -> Dict[str, Any]:
        self._msg_id += 1
        cid = self._msg_id
        self.ws.send_text(json.dumps({"id": cid, "method": method, "params": params or {}}))
        deadline = time.time() + timeout
        while True:
            if cid in self.pending:
                m = self.pending.pop(cid)
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
            if "id" in msg:
                if msg["id"] == cid:
                    if "error" in msg:
                        raise CDPError(msg["error"])
                    return msg.get("result", {})
                if msg["id"]:
                    self.pending[msg["id"]] = msg
            else:
                # Events are handled and dropped (never accumulated -> no leak).
                self._on_event(msg)

    def call(self, method: str, params: Optional[Dict[str, Any]] = None, timeout: float = 15) -> Dict[str, Any]:
        with self.lock:
            try:
                return self._raw_call(method, params, timeout)
            except (WSError, OSError, BrokenPipeError):
                # Tab closed / renderer crashed: reattach once and retry.
                self._ensure(max_retries=5)
                return self._raw_call(method, params, timeout)

    # -- primitives
    def eval(self, expr: str, timeout: float = 15) -> Any:
        res = self.call("Runtime.evaluate", {"expression": expr, "returnByValue": True, "awaitPromise": True}, timeout)
        if res.get("exceptionDetails"):
            det = res["exceptionDetails"]
            text = det.get("exception", {}).get("description") or det.get("text", "evaluation failed")
            raise CDPError(f"JS exception: {text}")
        val = res.get("result", {})
        return val.get("value")

    def status(self) -> Dict[str, Any]:
        targets = self._http_json("/json/list")
        pages = [t for t in targets if t.get("type") == "page"]
        st: Dict[str, Any] = {"tabs_count": len(pages), "dialog": self.dialog, "downloads": self.downloads[-5:]}
        if self.dialog and not self.dialog.get("auto_handled"):
            cur = next((t for t in pages if t.get("id") == getattr(self, "target_id", None)), {})
            st.update({"url": cur.get("url"), "title": cur.get("title")})
            return st
        st["url"] = self.eval("location.href")
        st["title"] = self.eval("document.title")
        return st

    def dom_elements(self) -> List[Dict[str, Any]]:
        return self.eval(f"({JS_DOM_INDEX})()") or []

    def _locate(self, selector: str) -> Optional[Dict[str, Any]]:
        sel = normalize_selector(selector)
        js = f"""(() => {{
          const el = document.querySelector({json.dumps(sel)});
          if (!el) return null;
          el.scrollIntoView({{block: 'center', inline: 'center'}});
          const r = el.getBoundingClientRect();
          return {{x: r.x + r.width / 2, y: r.y + r.height / 2, w: r.width, h: r.height,
                   tag: el.tagName.toLowerCase(), disabled: !!el.disabled}};
        }})()"""
        return self.eval(js)

    def click(self, selector: str) -> Dict[str, Any]:
        before = self.eval("location.href")
        loc = self._locate(selector)
        if not loc:
            return {"ok": False, "error": "element not found", "selector": selector, "url_before": before}
        if loc.get("disabled"):
            return {"ok": False, "error": "element disabled", "selector": selector, "url_before": before}
        x, y = round(loc["x"]), round(loc["y"])
        # Real input events (works with React/Vue handlers that ignore el.click()).
        for t in ("mouseMoved", "mousePressed", "mouseReleased"):
            p = {"type": t, "x": x, "y": y, "button": "left", "clickCount": 1}
            if t == "mouseMoved":
                p.pop("clickCount")
            self.call("Input.dispatchMouseEvent", p)
        time.sleep(0.4)
        after = self.status()
        return {"ok": True, "selector": selector, "point": {"x": x, "y": y}, "url_before": before,
                "url_after": after.get("url"), "navigated": after.get("url") != before, "dialog": after.get("dialog")}

    def mouse_click(self, x: float, y: float, button: str = "left") -> Dict[str, Any]:
        btn = "right" if button in ("right", "secondary") else "left"
        ix, iy = round(x), round(y)
        self.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": ix, "y": iy})
        self.call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": ix, "y": iy, "button": btn, "clickCount": 1})
        time.sleep(0.04)
        self.call("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": ix, "y": iy, "button": btn, "clickCount": 1})
        return {"ok": True, "point": {"x": ix, "y": iy}, "button": btn}

    def mouse_move(self, x: float, y: float) -> Dict[str, Any]:
        ix, iy = round(x), round(y)
        self.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": ix, "y": iy})
        return {"ok": True, "point": {"x": ix, "y": iy}}

    def fill(self, selector: str, text: str) -> Dict[str, Any]:
        sel = normalize_selector(selector)
        js = f"""(() => {{
          const el = document.querySelector({json.dumps(sel)});
          if (!el) return null;
          el.focus();
          const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
          const setter = Object.getOwnPropertyDescriptor(proto, 'value');
          if (el.isContentEditable) {{ el.textContent = {json.dumps(text)}; }}
          else if (setter && setter.set) {{ setter.set.call(el, {json.dumps(text)}); }}
          else {{ el.value = {json.dumps(text)}; }}
          el.dispatchEvent(new Event('input', {{bubbles: true}}));
          el.dispatchEvent(new Event('change', {{bubbles: true}}));
          return el.isContentEditable ? el.textContent : el.value;
        }})()"""
        val = self.eval(js)
        if val is None:
            return {"ok": False, "error": "element not found", "selector": selector}
        return {"ok": val == text, "selector": selector, "value_after": val if "password" not in sel else "***"}

    def type_keys(self, text: str, selector: Optional[str] = None) -> Dict[str, Any]:
        if selector:
            r = self.click(selector)
            if not r.get("ok"):
                return r
        self.call("Input.insertText", {"text": text})
        return {"ok": True, "typed_chars": len(text)}

    def press_key(self, key_name: str) -> Dict[str, Any]:
        code = KEY_CODES.get(key_name)
        if code is None and len(key_name) == 1:
            self.call("Input.insertText", {"text": key_name})
            return {"ok": True, "key": key_name}
        if code is None:
            return {"ok": False, "error": f"unsupported key {key_name}", "supported": sorted(KEY_CODES)}
        base = {"key": key_name, "code": key_name, "windowsVirtualKeyCode": code, "nativeVirtualKeyCode": code}
        down = dict(base, type="rawKeyDown")
        if key_name == "Enter":
            down = dict(base, type="keyDown", text="\r")
        self.call("Input.dispatchKeyEvent", down)
        self.call("Input.dispatchKeyEvent", dict(base, type="keyUp"))
        return {"ok": True, "key": key_name}

    def scroll(self, direction: str = "down", amount: int = 600, selector: Optional[str] = None) -> Dict[str, Any]:
        dy = {"down": amount, "up": -amount}.get(direction, 0)
        dx = {"right": amount, "left": -amount}.get(direction, 0)
        target = "window"
        if selector:
            target = f"document.querySelector({json.dumps(normalize_selector(selector))})"
        js = f"""(() => {{ const t = {target}; if (!t) return null;
          const b = t === window ? window.scrollY : t.scrollTop;
          t.scrollBy({dx}, {dy});
          const a = t === window ? window.scrollY : t.scrollTop;
          return {{before: b, after: a}}; }})()"""
        res = self.eval(js)
        if res is None:
            return {"ok": False, "error": "scroll target not found"}
        return {"ok": True, "moved": res["after"] != res["before"], **res}

    def wait_for(self, selector: Optional[str] = None, text: Optional[str] = None, timeout_s: float = 10) -> Dict[str, Any]:
        deadline = time.time() + max(0.5, min(timeout_s, 60))
        sel = normalize_selector(selector) if selector else None
        while time.time() < deadline:
            if sel and self.eval(f"!!document.querySelector({json.dumps(sel)})"):
                return {"ok": True, "found": "selector"}
            if text and self.eval(f"document.body && document.body.innerText.includes({json.dumps(text)})"):
                return {"ok": True, "found": "text"}
            if not sel and not text and self.eval("document.readyState") == "complete":
                return {"ok": True, "found": "load"}
            time.sleep(0.25)
        return {"ok": False, "error": "timeout"}

    def handle_dialog(self, accept: bool, prompt_text: Optional[str] = None) -> Dict[str, Any]:
        if not self.dialog:
            return {"ok": False, "error": "no dialog open"}
        p: Dict[str, Any] = {"accept": bool(accept)}
        if prompt_text is not None:
            p["promptText"] = prompt_text
        self.call("Page.handleJavaScriptDialog", p)
        handled, self.dialog = self.dialog, None
        return {"ok": True, "handled": handled, "accepted": bool(accept)}

    def navigate(self, url: str, wait_s: float = 15) -> Dict[str, Any]:
        before = self.eval("location.href") if not self.dialog else None
        res = self.call("Page.navigate", {"url": url})
        if res.get("errorText"):
            return {"ok": False, "error": res["errorText"], "url_before": before}
        loaded = self.wait_for(timeout_s=wait_s).get("ok", False)
        st = self.status()
        return {"ok": True, "url_before": before, "loaded": loaded, **st}

    def history(self, delta: int) -> Dict[str, Any]:
        self.eval(f"history.go({int(delta)})")
        time.sleep(0.8)
        return {"ok": True, **self.status()}

    def tabs(self) -> List[Dict[str, Any]]:
        return [{"id": t["id"], "url": t.get("url"), "title": t.get("title"), "active": t["id"] == getattr(self, "target_id", None)}
                for t in self._http_json("/json/list") if t.get("type") == "page"]

    def new_tab(self, url: str = "about:blank") -> Dict[str, Any]:
        t = self._new_target(url)
        with self.lock:
            self._ensure(max_retries=5, target_id=t.get("id"))
        return {"ok": True, "id": t.get("id"), "url": url}

    def switch_tab(self, target_id: str) -> Dict[str, Any]:
        with self.lock:
            self._ensure(max_retries=3, target_id=target_id)
        return {"ok": self.target_id == target_id, "active": self.target_id}

    def screenshot(self) -> bytes:
        res = self.call("Page.captureScreenshot", {"format": "png"})
        return base64.b64decode(res["data"])


# ------------------------------------------------------------- Broker
class Broker:
    """Owner takeover beats everything; otherwise at most one agent lease at a time.

    With no active lease, agent mutations are allowed (single-agent convenience).
    With an active lease, mutations must present the matching X-Arch-Lease header.
    """

    def __init__(self, clock=time.time):
        self.lock = threading.Lock()
        self.clock = clock
        self.owner_takeover = False
        self.lease: Optional[Dict[str, Any]] = None

    def _expire(self):
        if self.lease and self.lease["expires_at"] <= self.clock():
            self.lease = None

    def takeover(self) -> Dict[str, Any]:
        with self.lock:
            self.owner_takeover = True
            return {"ok": True, "owner_takeover": True, "message": "Human takeover active. Agent mutations locked."}

    def release_takeover(self) -> Dict[str, Any]:
        with self.lock:
            self.owner_takeover = False
            return {"ok": True, "owner_takeover": False, "message": "Human takeover released. Agents allowed."}

    def acquire(self, holder: str, ttl_s: float = 120) -> Dict[str, Any]:
        ttl_s = max(5.0, min(float(ttl_s), 1800.0))
        with self.lock:
            self._expire()
            if self.owner_takeover:
                return {"ok": False, "error": "owner takeover active"}
            if self.lease and self.lease["holder"] != holder:
                return {"ok": False, "error": "lease held", "holder": self.lease["holder"],
                        "expires_in_s": round(self.lease["expires_at"] - self.clock(), 1)}
            lease_id = self.lease["id"] if self.lease else base64.urlsafe_b64encode(os.urandom(12)).decode()
            self.lease = {"id": lease_id, "holder": holder, "expires_at": self.clock() + ttl_s}
            return {"ok": True, "lease_id": lease_id, "holder": holder, "ttl_s": ttl_s}

    def release(self, lease_id: str) -> Dict[str, Any]:
        with self.lock:
            self._expire()
            if self.lease and hmac.compare_digest(self.lease["id"], lease_id or ""):
                self.lease = None
                return {"ok": True}
            return {"ok": False, "error": "lease not held"}

    def check(self, lease_id: Optional[str], actor: str = "agent") -> Optional[Dict[str, Any]]:
        """Return None if allowed, else (http_code, payload)."""
        with self.lock:
            self._expire()
            if actor == "owner":
                return None
            if self.owner_takeover:
                return {"code": 423, "error": "Human in control: agent operations locked", "owner_takeover": True}
            if self.lease and not hmac.compare_digest(self.lease["id"], lease_id or ""):
                return {"code": 409, "error": "Another agent holds the lease", "holder": self.lease["holder"]}
            return None

    def snapshot(self) -> Dict[str, Any]:
        with self.lock:
            self._expire()
            lease = None
            if self.lease:
                lease = {"holder": self.lease["holder"], "expires_in_s": round(self.lease["expires_at"] - self.clock(), 1)}
            return {"owner_takeover": self.owner_takeover, "lease": lease}


def token_ok(expected: str, header: Optional[str]) -> bool:
    """Fail-closed bearer check. Empty expected token => auth disabled (loopback-only use)."""
    if not expected:
        return True
    if not header or not header.startswith("Bearer "):
        return False
    return hmac.compare_digest(header[7:].strip().encode(), expected.encode())


DRIVER: Optional[CDPDriver] = None
BROKER = Broker()
ROUTER = ModelRouter()
TOKEN = os.environ.get("ARCH_TOKEN", "")


# ------------------------------------------------------------- HTTP Server
class ArchHandler(BaseHTTPRequestHandler):
    server_version = "archd/" + VERSION

    def log_message(self, fmt, *args):
        if "/healthz" in (self.path or ""):
            return
        super().log_message(fmt, *args)

    def _send_json(self, code: int, data: Any):
        body = json.dumps(data, indent=2).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> Optional[Dict[str, Any]]:
        length = int(self.headers.get("Content-Length", 0) or 0)
        if not length:
            return {}
        if "application/json" not in (self.headers.get("Content-Type") or ""):
            return None
        try:
            data = json.loads(self.rfile.read(min(length, 1 << 20)).decode("utf-8"))
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    def _authed(self) -> bool:
        auth_hdr = self.headers.get("Authorization")
        if not auth_hdr:
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            token_q = qs.get("token", [None])[0]
            if token_q:
                auth_hdr = f"Bearer {token_q}"
        if token_ok(TOKEN, auth_hdr):
            return True
        self._send_json(401, {"ok": False, "error": "unauthorized"})
        return False

    def _proxy_websocket(self):
        if not self._authed():
            return
        vnc_port = int(os.environ.get("VNC_PORT", 6080))
        try:
            sock = socket.create_connection(("127.0.0.1", vnc_port), timeout=10)
        except Exception as e:
            return self._send_json(502, {"ok": False, "error": f"vnc bridge unavailable: {e}"})

        try:
            req_line = f"{self.command} {self.path} {self.request_version}\r\n"
            sock.sendall(req_line.encode("latin-1"))
            for k, v in self.headers.items():
                sock.sendall(f"{k}: {v}\r\n".encode("latin-1"))
            sock.sendall(b"\r\n")

            self.close_connection = True
            client_sock = self.connection
            backend_sock = sock
            conns = [client_sock, backend_sock]
            while True:
                r, _, x = select.select(conns, [], conns, 60)
                if x or not r:
                    break
                for s in r:
                    other = backend_sock if s is client_sock else client_sock
                    data = s.recv(65536)
                    if not data:
                        return
                    other.sendall(data)
        except (socket.error, ConnectionResetError, BrokenPipeError):
            pass
        finally:
            try:
                sock.close()
            except Exception:
                pass

    def _actor(self) -> str:
        return "owner" if self.headers.get("X-Arch-Actor") == "owner" else "agent"

    def _safe(self, fn):
        try:
            fn()
        except (CDPError, WSError) as e:
            self._send_json(502, {"ok": False, "error": f"browser: {e}"})
        except Exception as e:  # noqa: BLE001
            self._send_json(500, {"ok": False, "error": f"{type(e).__name__}: {e}"})

    def do_GET(self):
        self._safe(self._get)

    def do_HEAD(self):
        self._safe(self._get)

    def do_POST(self):
        self._safe(self._post)

    def _get(self):
        path = urllib.parse.urlparse(self.path).path
        if self.headers.get("Upgrade", "").lower() == "websocket" or path == "/websockify":
            return self._proxy_websocket()

        if path == "/healthz":
            return self._send_json(200, {"ok": True, "service": "archd", "version": VERSION})

        if path in ("/", "/index.html", "/ui"):
            ui_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui", "index.html")
            with open(ui_path, "rb") as f:
                content = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
            return

        if path.startswith("/novnc/"):
            rel = path[len("/novnc/"):].lstrip("/")
            base = os.path.abspath(os.environ.get("NOVNC_DIR", "/usr/share/novnc"))
            target = os.path.abspath(os.path.join(base, rel))
            if os.path.commonpath([base, target]) == base and os.path.isfile(target):
                mime, _ = mimetypes.guess_type(target)
                if target.endswith(".js") or target.endswith(".mjs"):
                    mime = "application/javascript"
                elif target.endswith(".css"):
                    mime = "text/css"
                elif target.endswith(".html"):
                    mime = "text/html"
                with open(target, "rb") as f:
                    content = f.read()
                self.send_response(200)
                self.send_header("Content-Type", mime or "application/octet-stream")
                self.send_header("Content-Length", str(len(content)))
                self.send_header("Cache-Control", "public, max-age=3600")
                self.end_headers()
                self.wfile.write(content)
                return
            return self._send_json(404, {"ok": False, "error": "novnc file not found"})

        if path.startswith("/ui/"):
            rel = path[len("/ui/"):].lstrip("/")
            base = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui"))
            target = os.path.abspath(os.path.join(base, rel))
            if os.path.commonpath([base, target]) == base and os.path.isfile(target):
                mime, _ = mimetypes.guess_type(target)
                if target.endswith(".js"):
                    mime = "application/javascript"
                elif target.endswith(".css"):
                    mime = "text/css"
                with open(target, "rb") as f:
                    content = f.read()
                self.send_response(200)
                self.send_header("Content-Type", mime or "application/octet-stream")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
                return
            return self._send_json(404, {"ok": False, "error": "file not found"})

        if not self._authed():
            return
        if path == "/api/status":
            st = DRIVER.status()
            st.update(BROKER.snapshot())
            st.update({"tiers": ROUTER.describe(), "version": VERSION})
            return self._send_json(200, {"ok": True, **st})
        if path == "/api/dom":
            elements = DRIVER.dom_elements()
            return self._send_json(200, {"ok": True, "count": len(elements), "elements": elements})
        if path == "/api/tabs":
            return self._send_json(200, {"ok": True, "tabs": DRIVER.tabs()})
        if path == "/api/screenshot":
            data = DRIVER.screenshot()
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self._send_json(404, {"ok": False, "error": "Not found"})

    def _post(self):
        path = urllib.parse.urlparse(self.path).path
        if not self._authed():
            return
        body = self._body()
        if body is None:
            return self._send_json(415, {"ok": False, "error": "body must be a JSON object with Content-Type: application/json"})

        # Owner + lease controls (not subject to the agent lock)
        if path == "/api/takeover":
            return self._send_json(200, BROKER.takeover())
        if path == "/api/release":
            return self._send_json(200, BROKER.release_takeover())
        if path == "/api/lease":
            res = BROKER.acquire(str(body.get("holder") or "agent"), body.get("ttl_s", 120))
            return self._send_json(200 if res["ok"] else 409, res)
        if path == "/api/lease/release":
            res = BROKER.release(str(body.get("lease_id") or self.headers.get("X-Arch-Lease") or ""))
            return self._send_json(200 if res["ok"] else 409, res)
        if path == "/api/classify":
            # Read-only decision; no browser mutation, so no lock check.
            res = ROUTER.classify(body.get("state", {}), body.get("question", "Choose the best option."), body.get("options", []))
            return self._send_json(200, {"ok": True, **res})

        denied = BROKER.check(self.headers.get("X-Arch-Lease"), self._actor())
        if denied:
            code = denied.pop("code")
            return self._send_json(code, {"ok": False, **denied})

        def reply(res: Dict[str, Any]):
            return self._send_json(200 if res.get("ok") else 400, res)

        if path == "/api/eval":
            js = body.get("js") or body.get("script")
            if not js:
                return self._send_json(400, {"ok": False, "error": "js required"})
            return self._send_json(200, {"ok": True, "result": DRIVER.eval(js)})
        if path in ("/api/navigate", "/api/goto"):
            url = body.get("url")
            if not url:
                return self._send_json(400, {"ok": False, "error": "URL required"})
            if "://" not in url and not url.startswith("about:"):
                url = "https://" + url
            return reply(DRIVER.navigate(url))
        if path == "/api/click":
            return reply(DRIVER.click(body.get("selector") or body.get("index") or ""))
        if path == "/api/fill":
            return reply(DRIVER.fill(body.get("selector") or body.get("index") or "", body.get("text", "")))
        if path == "/api/type":
            return reply(DRIVER.type_keys(body.get("text", ""), body.get("selector")))
        if path == "/api/press":
            return reply(DRIVER.press_key(body.get("key", "Enter")))
        if path == "/api/scroll":
            return reply(DRIVER.scroll(body.get("direction", "down"), int(body.get("amount", 600)), body.get("selector")))
        if path == "/api/wait":
            return reply(DRIVER.wait_for(body.get("selector"), body.get("text"), float(body.get("timeout_s", 10))))
        if path == "/api/dialog":
            return reply(DRIVER.handle_dialog(bool(body.get("accept", True)), body.get("text")))
        if path == "/api/back":
            return reply(DRIVER.history(-1))
        if path == "/api/forward":
            return reply(DRIVER.history(1))
        if path == "/api/tabs/new":
            return reply(DRIVER.new_tab(body.get("url", "about:blank")))
        if path == "/api/tabs/switch":
            return reply(DRIVER.switch_tab(str(body.get("id", ""))))
        if path == "/api/mouse/click":
            return reply(DRIVER.mouse_click(float(body.get("x", 0)), float(body.get("y", 0)), str(body.get("button", "left"))))
        if path == "/api/mouse/move":
            return reply(DRIVER.mouse_move(float(body.get("x", 0)), float(body.get("y", 0))))
        if path == "/api/task":
            return self._run_task(body)
        self._send_json(404, {"ok": False, "error": "Not found"})

    def _run_task(self, body: Dict[str, Any]):
        goal = body.get("goal")
        if not goal:
            return self._send_json(400, {"ok": False, "error": "Goal required"})
        max_steps = max(1, min(int(body.get("max_steps", 10)), 40))
        holder = f"task:{goal[:40]}"
        caller_lease = self.headers.get("X-Arch-Lease")
        own_lease = None
        if not caller_lease:
            lease = BROKER.acquire(holder, ttl_s=max_steps * 45)
            if not lease["ok"]:
                return self._send_json(409, lease)
            own_lease = lease["lease_id"]
        lease_id = caller_lease or own_lease
        history: List[Dict[str, Any]] = []
        failure_count = 0
        cost = 0.0
        try:
            for step in range(1, max_steps + 1):
                denied = BROKER.check(lease_id)
                if denied:
                    return self._send_json(denied["code"], {"ok": False, "status": "paused", "error": denied["error"], "history": history})
                st = DRIVER.status()
                elements = DRIVER.dom_elements()
                plan = ROUTER.plan_next_action(goal, st.get("url", ""), st.get("title", ""), elements, history, failure_count)
                cost += float((plan.get("usage") or {}).get("cost") or 0)
                act = plan.get("action")
                if act == "done":
                    history.append({"step": step, "plan": plan, "status": "completed"})
                    return self._send_json(200, {"ok": True, "status": "completed", "summary": plan.get("summary"),
                                                 "cost_usd": round(cost, 6), "history": history})
                if act == "takeover_request":
                    return self._send_json(200, {"ok": False, "status": "takeover_requested", "reason": plan.get("reason"),
                                                 "cost_usd": round(cost, 6), "history": history})
                target = plan.get("index") or plan.get("selector") or ""
                if act == "click":
                    res = DRIVER.click(target)
                elif act == "fill":
                    res = DRIVER.fill(target, plan.get("text", ""))
                elif act == "type":
                    res = DRIVER.type_keys(plan.get("text", ""), target or None)
                elif act == "press":
                    res = DRIVER.press_key(plan.get("key", "Enter"))
                elif act == "navigate":
                    res = DRIVER.navigate(plan.get("url", ""))
                elif act == "scroll":
                    res = DRIVER.scroll(plan.get("direction", "down"))
                elif act == "back":
                    res = DRIVER.history(-1)
                elif act == "wait":
                    res = DRIVER.wait_for(target or None, timeout_s=5)
                else:
                    res = {"ok": False, "error": f"unknown action {act!r}"}
                failure_count = 0 if res.get("ok") else failure_count + 1
                history.append({"step": step, "plan": {k: v for k, v in plan.items() if k != "usage"}, "result": res})
            return self._send_json(200, {"ok": False, "status": "max_steps_reached", "cost_usd": round(cost, 6), "history": history})
        finally:
            if own_lease:
                BROKER.release(own_lease)


def main():
    global DRIVER
    cdp_port = int(os.environ.get("ARCH_CDP_PORT", "9222"))
    port = int(os.environ.get("ARCH_PORT", "8901"))
    bind = os.environ.get("ARCH_BIND", "127.0.0.1")
    if bind not in ("127.0.0.1", "::1", "localhost") and not TOKEN:
        raise SystemExit("refusing to bind a non-loopback address without ARCH_TOKEN set")
    print(f"Connecting archd to Chrome DevTools on 127.0.0.1:{cdp_port}...", flush=True)
    DRIVER = CDPDriver(port=cdp_port, dialog_policy=os.environ.get("ARCH_DIALOG_POLICY", "auto"))
    print(f"archd {VERSION} on http://{bind}:{port} (auth={'token' if TOKEN else 'none/loopback'})", flush=True)
    server = ThreadingHTTPServer((bind, port), ArchHandler)
    server.daemon_threads = True
    server.serve_forever()


if __name__ == "__main__":
    main()
