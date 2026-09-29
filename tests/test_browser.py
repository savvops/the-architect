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
    destroy_browser_worker,
    detect_browsers,
    find_free_port,
    fork_browser,
    list_browser_workers,
    load_cookie_file,
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


if __name__ == "__main__":
    unittest.main()
