#!/usr/bin/env python3
"""cdp-bridge: CDP for headed Chrome-for-Testing via --remote-debugging-pipe.

The full Chrome-for-Testing build never binds a TCP --remote-debugging-port
in headed mode (verified empirically: spins up fine, no listener, no
DevToolsActivePort — while --headless binds instantly). So this supervisor
spawns Chrome with --remote-debugging-pipe (Chrome reads CDP on fd 3, writes
on fd 4; pipe messages are NUL-terminated JSON, per Chromium's
devtools_pipe_handler.cc kASCIIZ mode and Playwright's pipeTransport)
and serves the classic DevTools HTTP + WebSocket surface on
127.0.0.1:9222. The `mb` CLI and the agent HTTP API talk to the bridge
exactly as if Chrome had opened the port itself.

Endpoints: /json/version, /json/list, /json/new, /json/close/<id>,
/devtools/page/<targetId> (WS), /devtools/browser/bridge (WS), /healthz.

Stdlib only.
"""

import argparse
import base64
import hashlib
import json
import os
import queue
import signal
import struct
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
PORT = 9222
PIPE = None
VERSION_INFO = {}


class BridgeError(RuntimeError):
    pass


# ------------------------------------------------------- pipe transport

class PipeCDP:
    """Speaks CDP to a Chrome child over --remote-debugging-pipe (fds 3/4)."""

    def __init__(self, chrome_bin, profile, display, window_size, log_path):
        # Chrome reads CDP on fd 3 and writes on fd 4. Arrange exactly that
        # in the PARENT before spawning (a preexec_fn dup2 does not survive
        # close_fds: the fd-closing pass runs after preexec and drops fds
        # that aren't in pass_fds).
        in_r, in_w = os.pipe()    # parent -> chrome (chrome reads fd 3)
        out_r, out_w = os.pipe()  # chrome -> parent (chrome writes fd 4)

        def _stash(fd):
            newfd = os.dup(fd)
            os.close(fd)
            return newfd

        # Move our parent-side ends off fds 3/4 if they landed there.
        if in_w == 3 or in_w == 4:
            in_w = _stash(in_w)
        if out_r == 3 or out_r == 4:
            out_r = _stash(out_r)
        os.dup2(in_r, 3)
        os.dup2(out_w, 4)
        for fd, target in ((in_r, 3), (out_w, 4)):
            if fd != target:
                os.close(fd)
        # Now: fd 3 = chrome's CDP stdin, fd 4 = chrome's CDP stdout.

        logf = open(log_path, "ab")
        # Trust the sandbox egress proxy's MITM CA so Chrome accepts the
        # proxy's re-issued certificates. Without this every HTTPS site
        # shows a privacy error. Uses the system CA bundle entry.
        try:
            ca = "/etc/ssl/certs/hatch-egress-ca.pem"
            if os.path.exists(ca):
                os.makedirs(profile, exist_ok=True)
                subprocess.run(
                    ["certutil", "-d", f"sql:{profile}", "-A",
                     "-t", "C,,", "-n", "HatchEgressCA", "-i", ca],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=15)
        except Exception:
            pass
        self.proc = subprocess.Popen(
            [chrome_bin, "--remote-debugging-pipe",
             "--no-sandbox", "--disable-dev-shm-usage",
             # --enable-automation + --no-first-run are REQUIRED for the pipe
             # handler to answer (verified empirically: without them Chrome
             # starts but never replies on fd 4; this matches Playwright's
             # launch flags). Side effect: an infobar notes the browser is
             # automated, and navigator.webdriver=true.
             "--enable-automation", "--no-first-run",
             # Hide the automation flag from JS (navigator.webdriver) while
             # keeping --enable-automation so the CDP pipe still answers.
             # Reduces bot-detection on sites like Google.
             "--disable-blink-features=AutomationControlled",
             # Route through the localhost forward proxy (agent/local-proxy.py),
             # which adds egress Proxy-Authorization. Bypass it for loopback.
             "--proxy-server=http://127.0.0.1:18080",
             "--proxy-bypass-list=localhost;127.0.0.1;[::1]",
             # DIAGNOSTIC ONLY: Chrome-for-Testing ignores NSS trust stores.
             # Remove once proper CA trust is confirmed working.
             "--ignore-certificate-errors",
             f"--user-data-dir={profile}", f"--display={display}",
             f"--window-size={window_size}", "--start-maximized",
             "about:blank"],
            pass_fds=(3, 4),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=logf, close_fds=True)
        # Parent drops its copies of chrome's pipe ends.
        os.close(3)
        os.close(4)
        self.w = os.fdopen(in_w, "wb")
        self.rfd = out_r  # raw fd; read loop uses os.read (no line buffering)
        self._id = 0
        self._id_lock = threading.Lock()
        self._wlock = threading.Lock()
        self._lock = threading.Lock()
        self.pending = {}   # pipe_id -> queue.Queue
        self.sessions = {}  # sessionId (or "__browser__") -> queue.Queue
        threading.Thread(target=self._read_loop, daemon=True).start()

    def _next_id(self):
        with self._id_lock:
            self._id += 1
            return self._id

    def _read_loop(self):
        # Chrome's pipe protocol in default mode (see Chromium's
        # devtools_pipe_handler.cc, ProtocolMode::kASCIIZ, and Playwright's
        # lib/server/pipeTransport.js): messages are NUL-terminated JSON,
        # both directions. Read raw chunks via os.read (no newlines ever
        # arrive, so line iteration would block forever) and split on \0.
        buf = b""
        try:
            while True:
                chunk = os.read(self.rfd, 65536)
                if not chunk:
                    return  # EOF: chrome died
                buf += chunk
                while True:
                    i = buf.find(b"\0")
                    if i < 0:
                        break
                    raw = buf[:i]
                    buf = buf[i + 1:]
                    if not raw:
                        continue
                    try:
                        msg = json.loads(raw)
                    except Exception:

                        continue

                    if "id" in msg:
                        with self._lock:
                            q = self.pending.pop(msg["id"], None)
                        if q is not None:
                            q.put(msg)
                    else:
                        sid = msg.get("sessionId", "__browser__")
                        with self._lock:
                            q = self.sessions.get(sid)
                        if q is not None:
                            q.put(msg)
        except Exception:
            pass

    def send(self, method, params=None, session_id=None, inbox=None):
        """Send a CDP call over the pipe. Returns (pipe_id, response_queue).
        If inbox is given, the reply is routed there instead of a fresh queue
        (avoids a race where a fast reply lands in an orphan queue)."""
        mid = self._next_id()
        q = inbox if inbox is not None else queue.Queue()
        with self._lock:
            self.pending[mid] = q
        payload = {"id": mid, "method": method, "params": params or {}}
        if session_id:
            payload["sessionId"] = session_id
        raw = json.dumps(payload).encode()
        data = raw + b"\0"  # NUL-delimited (Chromium kASCIIZ / Playwright pipeTransport)
        try:
            with self._wlock:
                self.w.write(data)
                self.w.flush()
        except Exception as e:
            with self._lock:
                self.pending.pop(mid, None)
            raise BridgeError(f"pipe write failed: {e}")
        return mid, q

    def call(self, method, params=None, session_id=None, timeout=30):
        mid, q = self.send(method, params, session_id)
        try:
            msg = q.get(timeout=timeout)
        except queue.Empty:
            with self._lock:
                self.pending.pop(mid, None)
            raise BridgeError(f"CDP call timed out: {method}")
        if "error" in msg:
            raise BridgeError(f"CDP {method}: {msg['error']}")
        return msg.get("result", {})

    def register_session(self, session_id):
        q = queue.Queue()
        with self._lock:
            self.sessions[session_id] = q
        return q

    def unregister_session(self, session_id):
        with self._lock:
            self.sessions.pop(session_id, None)

    def terminate(self):
        try:
            self.proc.terminate()
        except Exception:
            pass


# ------------------------------------------------- server-side websocket

class ServerWS:
    def __init__(self, sock):
        self.sock = sock
        self.buf = b""

    def _recv_exact(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("ws closed")
            self.buf += chunk
        data, self.buf = self.buf[:n], self.buf[n:]
        return data

    def recv_json(self):
        message = bytearray()
        while True:
            hdr = self._recv_exact(2)
            fin = hdr[0] & 0x80
            opcode = hdr[0] & 0x0F
            masked = hdr[1] & 0x80
            length = hdr[1] & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._recv_exact(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._recv_exact(8))[0]
            mask = self._recv_exact(4) if masked else None
            payload = self._recv_exact(length)
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 0x8:
                raise ConnectionError("ws closed by peer")
            if opcode == 0x9:  # ping -> pong
                self._send_frame(0xA, payload)
                continue
            if opcode in (0x0, 0x1, 0x2):
                message += payload
                if fin:
                    return json.loads(bytes(message).decode("utf-8"))
                continue

    def send_json(self, obj):
        self._send_frame(0x1, json.dumps(obj).encode())

    def _send_frame(self, opcode, payload):
        hdr = bytes([0x80 | opcode])
        n = len(payload)
        if n < 126:
            hdr += bytes([n])
        elif n < 65536:
            hdr += bytes([126]) + struct.pack(">H", n)
        else:
            hdr += bytes([127]) + struct.pack(">Q", n)
        self.sock.sendall(hdr + payload)

    def close(self):
        try:
            self.sock.shutdown(2)
        except OSError:
            pass
        self.sock.close()


# ------------------------------------------------------------- HTTP API

def _targets():
    infos = PIPE.call("Target.getTargets", timeout=10).get("targetInfos", [])
    out = []
    for t in infos:
        if t.get("type") != "page":
            continue
        tid = t["targetId"]
        out.append({
            "description": "",
            "devtoolsFrontendUrl": f"/devtools/page/{tid}",
            "id": tid,
            "title": t.get("title", ""),
            "type": "page",
            "url": t.get("url", ""),
            "webSocketDebuggerUrl": f"ws://127.0.0.1:{PORT}/devtools/page/{tid}",
        })
    return out


class Handler(BaseHTTPRequestHandler):
    server_version = "cdp-bridge"

    def log_message(self, fmt, *args):
        sys.stderr.write("bridge: " + fmt % args + "\n")

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _new_target(self):
        qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        url = qs.get("url", ["about:blank"])[0]
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
        except ValueError:
            length = 0
        if length:
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
                url = body.get("url", url)
            except Exception:
                pass
        try:
            r = PIPE.call("Target.createTarget", {"url": url}, timeout=15)
            self._json({"id": r.get("targetId"), "url": url})
        except BridgeError as e:
            self._json({"error": str(e)}, 502)

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        if path == "/healthz":
            return self._json({"ok": True, "service": "cdp-bridge"})
        if path == "/json/version":
            info = dict(VERSION_INFO)
            info["webSocketDebuggerUrl"] = (
                f"ws://127.0.0.1:{PORT}/devtools/browser/bridge")
            return self._json(info)
        if path == "/json/list" or path == "/json":
            try:
                return self._json(_targets())
            except BridgeError as e:
                return self._json({"error": str(e)}, 502)
        if path == "/json/new":
            return self._new_target()
        if path.startswith("/json/close/"):
            tid = path.rsplit("/", 1)[-1]
            try:
                PIPE.call("Target.closeTarget", {"targetId": tid}, timeout=10)
                return self._json({"ok": True})
            except BridgeError as e:
                return self._json({"error": str(e)}, 502)
        if path.startswith("/devtools/"):
            return self._ws_proxy(path)
        return self._json({"error": "not found"}, 404)

    def do_PUT(self):
        path = urllib.parse.urlsplit(self.path).path
        if path == "/json/new":
            return self._new_target()
        return self._json({"error": "not found"}, 404)

    # -------------------------------------------------- websocket proxy
    def _ws_proxy(self, path):
        parts = path.strip("/").split("/")
        if len(parts) < 2 or parts[0] != "devtools":
            return self._json({"error": "not found"}, 404)
        key = self.headers.get("Sec-WebSocket-Key")
        if not key or self.headers.get("Upgrade", "").lower() != "websocket":
            return self._json({"error": "websocket upgrade required"}, 400)

        session_id = None
        if parts[1] == "page" and len(parts) == 3:
            try:
                r = PIPE.call("Target.attachToTarget",
                              {"targetId": parts[2], "flatten": True},
                              timeout=10)
                session_id = r["sessionId"]
            except (BridgeError, KeyError) as e:
                return self._json({"error": f"attach failed: {e}"}, 502)
        elif parts[1] == "browser":
            session_id = None  # browser-level session, no attach needed
        else:
            return self._json({"error": "unknown target"}, 404)

        accept = base64.b64encode(
            hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
        self.send_response(101)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()

        ws = ServerWS(self.connection)
        inbox = PIPE.register_session(session_id or "__browser__")
        idmap = {}  # pipe_id -> client message id
        stop = threading.Event()

        def pipe_to_client():
            while not stop.is_set():
                try:
                    msg = inbox.get(timeout=1)
                except queue.Empty:
                    continue
                try:

                    if "id" in msg:
                        cid = idmap.pop(msg["id"], None)

                        if cid is None:
                            continue
                        out = {"id": cid}
                        if "error" in msg:
                            out["error"] = msg["error"]
                        else:
                            out["result"] = msg.get("result", {})
                        ws.send_json(out)
                    else:
                        ws.send_json({"method": msg.get("method"),
                                      "params": msg.get("params", {})})
                except Exception:
                    break

        t = threading.Thread(target=pipe_to_client, daemon=True)
        t.start()
        try:
            while True:
                req = ws.recv_json()
                mid, _ = PIPE.send(req.get("method"), req.get("params"),
                                   session_id, inbox=inbox)
                idmap[mid] = req.get("id")
        except Exception:
            pass
        finally:
            stop.set()
            PIPE.unregister_session(session_id or "__browser__")
            try:
                ws.close()
            except Exception:
                pass
            if session_id:
                try:
                    PIPE.call("Target.detachFromTarget",
                              {"sessionId": session_id}, timeout=5)
                except Exception:
                    pass


# ------------------------------------------------------------------ main

def main():
    ap = argparse.ArgumentParser(description="CDP pipe bridge for headed Chrome.")
    ap.add_argument("--chrome", required=True)
    ap.add_argument("--profile", required=True)
    ap.add_argument("--display", default=":99")
    ap.add_argument("--window-size", default="1280,800")
    ap.add_argument("--port", type=int, default=9222)
    ap.add_argument("--log", default="/tmp/musebrowser-chromium.log")
    args = ap.parse_args()

    global PIPE, PORT, VERSION_INFO
    PORT = args.port
    PIPE = PipeCDP(args.chrome, args.profile, args.display,
                    args.window_size, args.log)

    # wait for chrome to answer over the pipe
    for _ in range(60):
        try:
            PIPE.call("Target.getTargets", timeout=5)
            break
        except Exception:
            time.sleep(1)
    else:
        sys.stderr.write("bridge: chrome did not answer over the pipe\n")
        PIPE.terminate()
        sys.exit(1)

    try:
        v = PIPE.call("Browser.getVersion", timeout=10)
        VERSION_INFO = {
            "Browser": v.get("product", "Chrome"),
            "Protocol-Version": v.get("protocolVersion", "1.3"),
            "User-Agent": v.get("userAgent", ""),
            "V8-Version": v.get("jsVersion", ""),
            "WebKit-Version": v.get("revision", ""),
        }
    except Exception:
        VERSION_INFO = {"Browser": "Chrome", "Protocol-Version": "1.3"}

    def _stop(signum, frame):
        PIPE.terminate()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    httpd.daemon_threads = True
    sys.stderr.write(f"bridge: serving DevTools on 127.0.0.1:{args.port}\n")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
