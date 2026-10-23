"""
Tests for The Architect Ephemeral Browser Worker & Profile-Forking Adapter.
Zero external dependencies (pure Python standard library unittest).
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from appctl.browser import (
    BASE_WORKER_DIR,
    CDPClient,
    browser_status,
    bridge_browser,
    SAOBrowserBridge,
    get_unified_browser_url,
    destroy_browser_worker,
    detect_browsers,
    find_free_port,
    fork_browser,
    list_browser_workers,
    load_cookie_file,
    act_on_worker,
    _load_workers,
    _save_workers,
)
from appctl.registry import (
    SchemaValidationError,
    ToolRegistry,
    registry,
    validate_schema,
)

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
APPCTL_PY = os.path.join(REPO_ROOT, "appctl", "appctl.py")


class TestBrowserDetectionAndPort(unittest.TestCase):
    def test_detect_browsers_structure(self):
        detected = detect_browsers()
        self.assertIsInstance(detected, dict)
        for name in ["brave", "chrome", "firefox"]:
            if name in detected:
                info = detected[name]
                self.assertIn("name", info)
                self.assertIn("type", info)
                self.assertIn("ready", info)
                self.assertIn(info["type"], ["chromium", "firefox"])
                self.assertIsInstance(info["ready"], bool)

    def test_find_free_port(self):
        port = find_free_port(start_port=19222, max_attempts=50)
        self.assertIsInstance(port, int)
        self.assertGreaterEqual(port, 19222)
        self.assertLess(port, 19222 + 50)


class TestCookieParsing(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_load_cookie_file_standard_list(self):
        sample_cookies = [
            {
                "name": "SID",
                "value": "sample_sid_123",
                "domain": ".google.com",
                "path": "/",
                "secure": True,
                "httpOnly": True,
                "expirationDate": 1790000000.5,
                "sameSite": "lax",
            },
            {
                "name": "HSID",
                "value": "sample_hsid_456",
                "domain": ".google.com",
                "path": "/sub",
                "secure": False,
                "httpOnly": False,
            },
        ]
        cookie_path = os.path.join(self.temp_dir, "test_cookies.json")
        with open(cookie_path, "w", encoding="utf-8") as f:
            json.dump(sample_cookies, f)

        cdp_cookies = load_cookie_file(cookie_path)
        self.assertEqual(len(cdp_cookies), 2)
        self.assertEqual(cdp_cookies[0]["name"], "SID")
        self.assertEqual(cdp_cookies[0]["value"], "sample_sid_123")
        self.assertEqual(cdp_cookies[0]["domain"], ".google.com")
        self.assertTrue(cdp_cookies[0]["secure"])
        self.assertTrue(cdp_cookies[0]["httpOnly"])
        self.assertEqual(cdp_cookies[0]["sameSite"], "Lax")
        self.assertEqual(cdp_cookies[0]["expires"], 1790000000.5)

        self.assertEqual(cdp_cookies[1]["name"], "HSID")
        self.assertFalse(cdp_cookies[1]["secure"])
        self.assertFalse(cdp_cookies[1]["httpOnly"])

    def test_load_cookie_file_dict_wrapped(self):
        sample_wrapped = {
            "cookies": [
                {
                    "name": "SESSION",
                    "value": "tok_xyz",
                    "domain": "example.com",
                }
            ]
        }
        cookie_path = os.path.join(self.temp_dir, "test_wrapped.json")
        with open(cookie_path, "w", encoding="utf-8") as f:
            json.dump(sample_wrapped, f)

        cdp_cookies = load_cookie_file(cookie_path)
        self.assertEqual(len(cdp_cookies), 1)
        self.assertEqual(cdp_cookies[0]["name"], "SESSION")

    def test_load_cookie_file_invalid(self):
        cookie_path = os.path.join(self.temp_dir, "invalid.json")
        with open(cookie_path, "w", encoding="utf-8") as f:
            json.dump("not a list or dict", f)

        with self.assertRaises(ValueError):
            load_cookie_file(cookie_path)


class TestBrowserRegistryIntegration(unittest.TestCase):
    def test_browser_tools_in_registry(self):
        tool_ids = {t["tool_id"] for t in registry.list_tools()}
        self.assertIn("browser.fork", tool_ids)
        self.assertIn("browser.list", tool_ids)
        self.assertIn("browser.destroy", tool_ids)
        self.assertIn("browser.status", tool_ids)

    def test_schema_validation_browser_fork(self):
        # Valid args
        tool, args = registry.validate_and_prepare("browser.fork", {
            "browser": "brave",
            "url": "https://example.com",
            "headless": True,
            "port": 9333,
        })
        self.assertEqual(args["browser"], "brave")
        self.assertTrue(args["headless"])

        # Invalid browser enum
        with self.assertRaises(SchemaValidationError):
            registry.validate_and_prepare("browser.fork", {"browser": "safari"})

        # Invalid port range
        with self.assertRaises(SchemaValidationError):
            registry.validate_and_prepare("browser.fork", {"port": 80})

    def test_schema_validation_browser_destroy(self):
        # Valid
        tool, args = registry.validate_and_prepare("browser.destroy", {"worker_id": "bwrk_12345"})
        self.assertEqual(args["worker_id"], "bwrk_12345")

        # Missing required worker_id
        with self.assertRaises(SchemaValidationError):
            registry.validate_and_prepare("browser.destroy", {})


class TestBrowserWorkerLifecycle(unittest.TestCase):
    def test_browser_status(self):
        status = browser_status()
        self.assertTrue(status.get("ok"))
        self.assertEqual(status.get("action"), "browser.status")
        ev = status.get("evidence", {})
        self.assertIn("installed_browsers", ev)
        self.assertIn("active_workers_count", ev)
        self.assertIn("sao_bridge", ev)

    def test_destroy_nonexistent_worker(self):
        res = destroy_browser_worker("bwrk_nonexistent_9999999")
        # Should return ok: False or clean message
        if not res.get("ok"):
            self.assertIn("not found", res.get("error", "").lower())
        else:
            self.assertEqual(res.get("evidence", {}).get("destroyed", 0), 0)

    def test_live_fork_and_destroy_if_available(self):
        detected = detect_browsers()
        has_ready = any(b.get("ready") and b.get("type") == "chromium" for b in detected.values())
        if not has_ready:
            self.skipTest("No ready Chromium-based browser found for live headless worker test")

        # Fork a headless worker
        fork_res = fork_browser(headless=True, url="about:blank", timeout=8.0)
        self.assertTrue(fork_res.get("ok"), f"Fork failed: {fork_res}")

        ev = fork_res.get("evidence", {})
        worker_id = ev.get("worker_id")
        pid = ev.get("pid")
        profile_dir = ev.get("profile_dir")
        cdp_port = ev.get("cdp_port")

        self.assertIsNotNone(worker_id)
        self.assertGreater(pid, 0)
        self.assertTrue(os.path.isdir(profile_dir))
        self.assertTrue(ev.get("isolated"))

        try:
            # Verify listed
            list_res = list_browser_workers()
            self.assertTrue(list_res.get("ok"))
            workers = list_res.get("evidence", {}).get("workers", [])
            w_ids = [w.get("worker_id") for w in workers]
            self.assertIn(worker_id, w_ids)
        finally:
            # Destroy worker
            dest_res = destroy_browser_worker(worker_id)
            self.assertTrue(dest_res.get("ok"), f"Destroy failed: {dest_res}")
            dest_ev = dest_res.get("evidence", {})
            self.assertEqual(dest_ev.get("count"), 1)
            self.assertTrue(dest_ev.get("workers", [{}])[0].get("terminated"))
            self.assertTrue(dest_ev.get("workers", [{}])[0].get("profile_purged"))

            # Verify no longer active
            list_after = list_browser_workers()
            w_after_ids = [w.get("worker_id") for w in list_after.get("evidence", {}).get("workers", [])]
            self.assertNotIn(worker_id, w_after_ids)


class TestBrowserCLI(unittest.TestCase):
    def test_cli_browser_status(self):
        res = subprocess.run(
            [sys.executable, APPCTL_PY, "browser", "status"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(res.returncode, 0, f"CLI stderr: {res.stderr}")
        data = json.loads(res.stdout)
        self.assertTrue(data.get("ok"))
        self.assertEqual(data.get("action"), "browser.status")
        self.assertIn("installed_browsers", data.get("evidence", {}))

    def test_cli_browser_list(self):
        res = subprocess.run(
            [sys.executable, APPCTL_PY, "browser", "list"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(res.returncode, 0, f"CLI stderr: {res.stderr}")
        data = json.loads(res.stdout)
        self.assertTrue(data.get("ok"))
        self.assertEqual(data.get("action"), "browser.list")
        self.assertIn("workers", data.get("evidence", {}))

    def test_cli_exec_browser_list(self):
        res = subprocess.run(
            [sys.executable, APPCTL_PY, "exec", "browser.list"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(res.returncode, 0, f"CLI stderr: {res.stderr}")
        data = json.loads(res.stdout)
        self.assertTrue(data.get("ok"))
        self.assertEqual(data.get("action"), "browser.list")


class TestSAOBrowserBridge(unittest.TestCase):
    def test_bridge_tool_in_registry(self):
        tool_ids = {t["tool_id"] for t in registry.list_tools()}
        self.assertIn("browser.bridge", tool_ids)

        tool, args = registry.validate_and_prepare("browser.bridge", {
            "url": "https://example.com",
            "headless": True,
            "prefer_ephemeral": True,
        })
        self.assertEqual(args["url"], "https://example.com")
        self.assertTrue(args["headless"])

    def test_sao_bridge_get_status(self):
        bridge = SAOBrowserBridge()
        res = bridge.get_status()
        self.assertIsInstance(res, dict)
        self.assertIn("available", res)
        if res.get("available"):
            self.assertIn("state", res)
            self.assertIn("paused", res["state"])
            self.assertIn("queue", res["state"])

    def test_sao_bridge_route_ephemeral(self):
        bridge = SAOBrowserBridge()
        res = bridge.route_request(headless=True)
        self.assertTrue(res.get("ok"))
        self.assertEqual(res.get("action"), "browser.bridge")
        ev = res.get("evidence", {})
        self.assertEqual(ev.get("bridge_mode"), "offload_ephemeral")
        self.assertTrue(ev.get("owner_viewport_protected"))
        worker_id = ev.get("worker", {}).get("worker_id")
        if worker_id:
            destroy_browser_worker(worker_id)

    def test_cli_browser_bridge(self):
        res = subprocess.run(
            [sys.executable, APPCTL_PY, "browser", "bridge"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(res.returncode, 0, f"CLI stderr: {res.stderr}")
        data = json.loads(res.stdout)
        self.assertTrue(data.get("ok"))
        self.assertEqual(data.get("action"), "browser.bridge")
        self.assertTrue(data.get("evidence", {}).get("owner_viewport_protected"))
        worker_id = data.get("evidence", {}).get("worker", {}).get("worker_id")
        if worker_id:
            destroy_browser_worker(worker_id)

    def test_unified_browser_url_function(self):
        res = get_unified_browser_url()
        self.assertTrue(res.get("ok"))
        self.assertEqual(res.get("action"), "browser.url")
        ev = res.get("evidence", {})
        self.assertIn("unified_browser_url", ev)
        self.assertIn("savv-spine", ev["unified_browser_url"])

    def test_cli_browser_url(self):
        res = subprocess.run(
            [sys.executable, APPCTL_PY, "browser", "url"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(res.returncode, 0, f"CLI stderr: {res.stderr}")
        data = json.loads(res.stdout)
        self.assertTrue(data.get("ok"))
        self.assertEqual(data.get("action"), "browser.url")
        self.assertIn("unified_browser_url", data.get("evidence", {}))

    def test_cli_exec_browser_url(self):
        res = subprocess.run(
            [sys.executable, APPCTL_PY, "exec", "browser.url"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(res.returncode, 0, f"CLI stderr: {res.stderr}")
        data = json.loads(res.stdout)
        self.assertTrue(data.get("ok"))
        self.assertEqual(data.get("action"), "browser.url")

    def test_takeover_and_resume_in_registry(self):
        tool_ids = {t["tool_id"] for t in registry.list_tools()}
        self.assertIn("browser.takeover", tool_ids)
        self.assertIn("browser.resume", tool_ids)

        t1, a1 = registry.validate_and_prepare("browser.takeover", {"sao_url": "https://example.com"})
        self.assertEqual(a1["sao_url"], "https://example.com")

        t2, a2 = registry.validate_and_prepare("browser.resume", {})
        self.assertEqual(a2, {})

    def test_sao_bridge_shared_mode_when_not_prefer_ephemeral(self):
        bridge = SAOBrowserBridge()
        # Mock get_status to simulate available, unpaused, idle SAO Browser
        bridge.get_status = lambda timeout=3.0: {
            "available": True,
            "state": {"paused": False, "busy": False, "queue": [], "holder": None},
        }
        res = bridge.route_request(url="https://example.com", prefer_ephemeral=False)
        self.assertTrue(res.get("ok"))
        ev = res.get("evidence", {})
        self.assertEqual(ev.get("bridge_mode"), "shared_sao")
        self.assertFalse(ev.get("isolated"))
        self.assertIsNone(ev.get("worker"))
        self.assertTrue(ev.get("owner_viewport_protected"))


class TestCDPClientWebSocketHandling(unittest.TestCase):
    def test_cdp_ws_skips_async_events_and_handles_ping(self):
        import socket
        import struct
        import threading

        server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_sock.bind(("127.0.0.1", 0))
        server_sock.listen(1)
        port = server_sock.getsockname()[1]

        def make_server_frame(opcode: int, payload: bytes) -> bytes:
            length = len(payload)
            if length <= 125:
                return bytes([0x80 | opcode, length]) + payload
            return bytes([0x80 | opcode, 126]) + struct.pack(">H", length) + payload

        def fake_cdp_server():
            try:
                conn, _ = server_sock.accept()
                conn.settimeout(3.0)
                conn.recv(2048)
                # Send HTTP 101 AND an immediate async CDP event in the same packet
                async_event = json.dumps({"method": "Page.frameStartedLoading", "params": {"frameId": "1"}}).encode("utf-8")
                ping_frame = make_server_frame(0x9, b"ping")
                event_frame = make_server_frame(0x1, async_event)
                handshake = (
                    b"HTTP/1.1 101 Switching Protocols\r\n"
                    b"Upgrade: websocket\r\n"
                    b"Connection: Upgrade\r\n\r\n"
                )
                conn.sendall(handshake + ping_frame + event_frame)

                # Receive client's command frame (and pong frame)
                conn.recv(2048)
                # Now send the actual command response with id=1
                reply = json.dumps({"id": 1, "result": {"cookiesSet": True}}).encode("utf-8")
                conn.sendall(make_server_frame(0x1, reply))
                conn.close()
            finally:
                server_sock.close()

        t = threading.Thread(target=fake_cdp_server, daemon=True)
        t.start()

        client = CDPClient(host="127.0.0.1", port=port)
        res = client.send_ws_command(f"ws://127.0.0.1:{port}/devtools/page/1", "Network.setCookies", {"cookies": []})
        t.join(timeout=3.0)

        self.assertEqual(res.get("id"), 1)
        self.assertEqual(res.get("result"), {"cookiesSet": True})


class TestCDPClientPrimitives(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_cdp_client_navigate_and_screenshot(self):
        from unittest.mock import patch, MagicMock
        import base64

        client = CDPClient(host="127.0.0.1", port=9222)
        mock_targets = [{"type": "page", "webSocketDebuggerUrl": "ws://127.0.0.1:9222/page/1"}]

        with patch.object(client, "get_targets", return_value=mock_targets):
            # 1. Navigate
            with patch.object(client, "send_ws_command", return_value={"id": 1, "result": {"frameId": "F123"}}):
                nav_res = client.navigate("https://news.ycombinator.com")
                self.assertTrue(nav_res["ok"])
                self.assertEqual(nav_res["frame_id"], "F123")

            # 2. Screenshot
            dummy_png_bytes = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
            dummy_b64 = base64.b64encode(dummy_png_bytes).decode("ascii")
            out_png = os.path.join(self.temp_dir, "screen.png")

            with patch.object(client, "send_ws_command", return_value={"id": 2, "result": {"data": dummy_b64}}):
                ss_res = client.screenshot(output_path=out_png)
                self.assertTrue(ss_res["ok"])
                self.assertTrue(os.path.exists(out_png))
                with open(out_png, "rb") as f:
                    self.assertEqual(f.read(), dummy_png_bytes)

    def test_cdp_client_eval_and_dom(self):
        from unittest.mock import patch

        client = CDPClient(host="127.0.0.1", port=9222)
        mock_targets = [{"type": "page", "webSocketDebuggerUrl": "ws://127.0.0.1:9222/page/1"}]

        with patch.object(client, "get_targets", return_value=mock_targets):
            # 1. Eval JS
            with patch.object(client, "send_ws_command", return_value={"id": 1, "result": {"result": {"type": "number", "value": 42}}}):
                eval_res = client.eval_js("6 * 7")
                self.assertTrue(eval_res["ok"])
                self.assertEqual(eval_res["value"], 42)

            # 2. DOM extraction
            mock_dom_val = {
                "title": "Hacker News",
                "url": "https://news.ycombinator.com",
                "count": 2,
                "elements": [
                    {"tag": "a", "selector": "a.storylink", "text": "Show HN", "center": [100, 50]},
                    {"tag": "button", "selector": "button#submit", "text": "Vote", "center": [200, 50]},
                ]
            }
            with patch.object(client, "send_ws_command", return_value={"id": 2, "result": {"result": {"type": "object", "value": mock_dom_val}}}):
                dom_res = client.dom()
                self.assertTrue(dom_res["ok"])
                self.assertEqual(dom_res["evidence"]["count"], 2)
                self.assertEqual(dom_res["evidence"]["title"], "Hacker News")

    def test_cdp_client_click_and_type(self):
        from unittest.mock import patch

        client = CDPClient(host="127.0.0.1", port=9222)
        mock_targets = [{"type": "page", "webSocketDebuggerUrl": "ws://127.0.0.1:9222/page/1"}]

        with patch.object(client, "get_targets", return_value=mock_targets):
            # 1. Click by selector
            with patch.object(client, "send_ws_command", return_value={"id": 1, "result": {"result": {"type": "object", "value": {"found": True, "center": [150, 75]}}}}):
                c_res = client.click(selector="button#login")
                self.assertTrue(c_res["ok"])
                self.assertEqual(c_res["method"], "js_click")
                self.assertEqual(c_res["center"], [150, 75])

            # 2. Type text
            with patch.object(client, "send_ws_command", return_value={"id": 2, "result": {}}):
                t_res = client.type_text("admin_user", selector="input#username")
                self.assertTrue(t_res["ok"])

    def test_act_on_worker_dispatch(self):
        from unittest.mock import patch, MagicMock

        mock_workers = [{
            "worker_id": "bwrk_test123",
            "pid": 99999,
            "cdp_port": 19222,
        }]

        with patch("appctl.browser._load_workers", return_value=mock_workers), \
             patch("appctl.browser._is_pid_alive", return_value=True), \
             patch("appctl.browser.CDPClient.navigate", return_value={"ok": True, "action": "navigate", "url": "https://example.com"}):

            res = act_on_worker("bwrk_test123", "navigate", url="https://example.com")
            self.assertTrue(res["ok"])
            self.assertEqual(res["worker_id"], "bwrk_test123")
            self.assertEqual(res["action"], "browser.act.navigate")

    def test_browser_act_and_router_act_registry_schemas(self):
        t_bact = registry.get("browser.act")
        self.assertIsNotNone(t_bact)
        validate_schema({"worker_id": "bwrk_123", "action": "navigate", "url": "https://example.com"}, t_bact["inputs"])

        t_ract = registry.get("router.act")
        self.assertIsNotNone(t_ract)
        validate_schema({"task": "click Save button in notepad"}, t_ract["inputs"])


if __name__ == "__main__":
    unittest.main()

