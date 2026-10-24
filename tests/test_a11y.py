"""
Unit and integration tests for Accessibility Adapter / Desktop DOM (Phase 6: Tier A - Accessibility Adapter).
Tests semantic tree extraction, query filters, relative coordinate calculation,
semantic click dispatch, tool schemas, and live UIA tree inspection.
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from appctl.a11y import (
    run_a11y_tree,
    run_a11y_query,
    run_a11y_click,
    IS_WINDOWS,
)
from appctl.registry import registry, SchemaValidationError, validate_schema


class TestAccessibilityAdapter(unittest.TestCase):
    def test_registry_schemas(self):
        # 1. a11y.tree
        t_tree = registry.get("a11y.tree")
        self.assertIsNotNone(t_tree)
        self.assertEqual(t_tree["action"], "a11y_tree")
        validate_schema({"app": "firefox", "depth": 3, "max_children": 10}, t_tree["inputs"])

        with self.assertRaises(SchemaValidationError):
            validate_schema({}, t_tree["inputs"])  # missing app

        with self.assertRaises(SchemaValidationError):
            validate_schema({"app": "firefox", "depth": 50}, t_tree["inputs"])  # depth > 10

        # 2. a11y.query
        t_query = registry.get("a11y.query")
        self.assertIsNotNone(t_query)
        self.assertEqual(t_query["action"], "a11y_query")
        validate_schema({"app": "firefox", "role": "button", "name": "Sidebars"}, t_query["inputs"])

        # 3. a11y.click
        t_click = registry.get("a11y.click")
        self.assertIsNotNone(t_click)
        self.assertEqual(t_click["action"], "a11y_click")
        validate_schema({"app": "firefox", "name": "Sidebars", "button": "left"}, t_click["inputs"])

        with self.assertRaises(SchemaValidationError):
            validate_schema({"app": "firefox", "button": "invalid_btn"}, t_click["inputs"])

    def test_query_and_semantic_click_dispatch(self):
        mock_target = {
            "hwnd": 12345,
            "pid": 5678,
            "title": "Mock Browser Window",
        }
        mock_elements = [
            {
                "name": "Sidebars Toggle",
                "role": "button",
                "id": "sidebar-button",
                "class": "toolbarbutton",
                "bounds": [100, 200, 40, 30],
                "center": [120, 215],
                "relative_center": [20, 15],
            },
            {
                "name": "Reload Tab",
                "role": "button",
                "id": "reload-button",
                "class": "toolbarbutton",
                "bounds": [150, 200, 40, 30],
                "center": [170, 215],
                "relative_center": [70, 15],
            },
        ]

        def mock_resolve(app):
            return mock_target

        clicked_coords = []

        def mock_click(target, x, y, button="left"):
            clicked_coords.append((target["hwnd"], x, y, button))

        # Test simulated semantic click using custom mock
        from unittest.mock import patch

        with patch("appctl.a11y.query_a11y_elements", return_value=mock_elements):
            # Query element
            q_res = run_a11y_query("browser", role="button", name="Sidebars", resolve_fn=mock_resolve)
            self.assertTrue(q_res["ok"])
            self.assertEqual(q_res["evidence"]["count"], 2)

            # Click element
            c_res = run_a11y_click(
                "browser",
                name="Sidebars",
                button="left",
                resolve_fn=mock_resolve,
                click_fn=mock_click,
            )
            self.assertTrue(c_res["ok"])
            self.assertEqual(c_res["action"], "a11y-click")
            self.assertEqual(len(clicked_coords), 1)
            self.assertEqual(clicked_coords[0], (12345, 20, 15, "left"))
            self.assertEqual(c_res["evidence"]["relative_coordinates"], {"x": 20, "y": 15})

    def test_element_not_found_handling(self):
        mock_target = {"hwnd": 111, "pid": 222, "title": "Mock Window"}

        def mock_resolve(app):
            return mock_target

        from unittest.mock import patch

        with patch("appctl.a11y.query_a11y_elements", return_value=[]):
            res = run_a11y_click(
                "mock",
                name="NonExistentElement",
                resolve_fn=mock_resolve,
                click_fn=lambda *a, **k: None,
            )
            self.assertFalse(res["ok"])
            self.assertIn("no element matched query", res["error"])

    def test_uia_invoke_pattern_preference(self):
        mock_target = {"hwnd": 111, "pid": 222, "title": "Mock Window"}
        mock_element = {
            "name": "SaveButton",
            "role": "button",
            "id": "save-id",
            "bounds": [10, 20, 30, 40],
            "relative_center": [25, 40],
        }

        from unittest.mock import patch, MagicMock
        from ctypes import c_void_p

        mock_client = MagicMock()
        def fake_elem_from_handle(uia, hwnd, out_ptr):
            out_ptr._obj.value = 12345
            return 0

        mock_client.fn_ElementFromHandle = MagicMock(side_effect=fake_elem_from_handle)
        mock_client.find_element = MagicMock(return_value=c_void_p(67890))
        mock_client.invoke_element = MagicMock(return_value=True)  # InvokePattern succeeds

        mock_click = MagicMock()

        with patch("appctl.a11y.query_a11y_elements", return_value=[mock_element]), \
             patch("appctl.a11y.get_uia_client", return_value=mock_client):
            res = run_a11y_click(
                "mock",
                name="SaveButton",
                button="left",
                resolve_fn=lambda a: mock_target,
                click_fn=mock_click,
            )
            self.assertTrue(res["ok"])
            self.assertTrue(res["evidence"]["invoked"])
            self.assertEqual(res["evidence"]["method"], "InvokePattern")
            # Coordinate click handler must NOT be invoked when InvokePattern succeeded
            mock_click.assert_not_called()

    def test_uia_invoke_pattern_fallback_to_coordinate_click(self):
        mock_target = {"hwnd": 111, "pid": 222, "title": "Mock Window"}
        mock_element = {
            "name": "PlainLabel",
            "role": "text",
            "id": "lbl-id",
            "bounds": [10, 20, 30, 40],
            "relative_center": [25, 40],
        }

        from unittest.mock import patch, MagicMock
        mock_client = MagicMock()
        mock_client.fn_ElementFromHandle = MagicMock(return_value=0)
        mock_client.find_element = MagicMock(return_value=MagicMock())
        mock_client.invoke_element = MagicMock(return_value=False)  # InvokePattern unsupported

        mock_click = MagicMock()

        with patch("appctl.a11y.query_a11y_elements", return_value=[mock_element]), \
             patch("appctl.a11y.get_uia_client", return_value=mock_client):
            res = run_a11y_click(
                "mock",
                name="PlainLabel",
                button="left",
                resolve_fn=lambda a: mock_target,
                click_fn=mock_click,
            )
            self.assertTrue(res["ok"])
            self.assertFalse(res["evidence"]["invoked"])
            self.assertEqual(res["evidence"]["method"], "coordinate_click")
            mock_click.assert_called_once_with(mock_target, 25, 40, button="left")

    @unittest.skipUnless(IS_WINDOWS, "Live UIA tests require Windows")
    def test_live_windows_uia_query(self):
        from appctl.appctl import _win_resolve_target
        # Try finding a real window (explorer or firefox)
        target = _win_resolve_target("explorer") or _win_resolve_target("firefox")
        if not target:
            self.skipTest("No GUI window available for live UIA test")

        res = run_a11y_query(target["title"], resolve_fn=_win_resolve_target)
        self.assertTrue(res["ok"])
        self.assertIn("count", res["evidence"])
        self.assertIn("elements", res["evidence"])

    @unittest.skipUnless(IS_WINDOWS, "Live UIA tests require Windows")
    def test_live_windows_uia_tree(self):
        from appctl.appctl import _win_resolve_target
        target = _win_resolve_target("explorer") or _win_resolve_target("firefox")
        if not target:
            self.skipTest("No GUI window available for live UIA test")

        res = run_a11y_tree(target["title"], depth=2, max_children=5, resolve_fn=_win_resolve_target)
        self.assertTrue(res["ok"])
        self.assertIn("tree", res["evidence"])
        tree = res["evidence"]["tree"]
        self.assertIn("role", tree)
        self.assertIn("bounds", tree)


if __name__ == "__main__":
    unittest.main()
