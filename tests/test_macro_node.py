"""
Unit and integration tests for Macro-Node Runner (Phase 5: Tier B - Keyboard nodes).
Tests focus + state contracts, action sequences, visual diff verification,
automated recovery, and verifiable evidence payloads.
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from appctl.node import execute_macro_node, _load_spec
from appctl.registry import registry, SchemaValidationError, validate_schema


class TestMacroNodeRunner(unittest.TestCase):
    def setUp(self):
        # Create mock target and handlers
        self.mock_target = {
            "hwnd": 99999,
            "pid": 12345,
            "title": "MyTestApp - Editing Document.txt",
        }
        self.dispatched_keys = []
        self.dispatched_types = []
        self.dispatched_clicks = []
        self.focus_calls = []

        def mock_resolve(app):
            if app == "fail_app":
                return None
            return dict(self.mock_target)

        def mock_focus(hwnd_or_app):
            self.focus_calls.append(hwnd_or_app)
            return True, self.mock_target["hwnd"]

        def mock_key(target, key):
            self.dispatched_keys.append((target["hwnd"], key))

        def mock_type(target, text):
            self.dispatched_types.append((target["hwnd"], text))

        def mock_click(target, x, y, button="left"):
            self.dispatched_clicks.append((target["hwnd"], x, y, button))

        self.mock_handlers = {
            "resolve": mock_resolve,
            "focus_raw": mock_focus,
            "key_raw": mock_key,
            "type_raw": mock_type,
            "click_raw": mock_click,
        }

    def test_spec_loading(self):
        # Dict
        d = {"app": "test", "steps": []}
        self.assertEqual(_load_spec(d), d)

        # JSON String
        s = '{"app": "test", "steps": [{"action": "key", "key": "esc"}]}'
        self.assertEqual(_load_spec(s)["app"], "test")

        # JSON File
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".json") as f:
            f.write(s)
            f_path = f.name
        try:
            loaded = _load_spec(f_path)
            self.assertEqual(loaded["app"], "test")
            self.assertEqual(len(loaded["steps"]), 1)
        finally:
            if os.path.exists(f_path):
                os.remove(f_path)

        # Invalid spec
        with self.assertRaises(ValueError):
            _load_spec("not valid json at all")

    def test_precondition_title_match_pass(self):
        spec = {
            "id": "node_title_ok",
            "app": "testapp",
            "preconditions": {
                "title_match": "Editing Document",
            },
            "steps": [
                {"action": "key", "key": "ctrl+s"},
                {"action": "sleep", "duration": 0.01},
            ],
            "postconditions": {
                "title_match": "Document\\.txt",
            },
        }
        res = execute_macro_node(spec, self.mock_handlers)
        self.assertTrue(res["ok"])
        self.assertEqual(res["action"], "node")
        ev = res["evidence"]
        self.assertEqual(ev["id"], "node_title_ok")
        self.assertTrue(ev["preconditions_met"])
        self.assertEqual(ev["steps_executed"], 2)
        self.assertTrue(ev["postconditions_met"])
        self.assertEqual(len(self.dispatched_keys), 1)
        self.assertEqual(self.dispatched_keys[0][1], "ctrl+s")

    def test_precondition_title_match_fail_and_recover(self):
        spec = {
            "id": "node_pre_fail",
            "app": "testapp",
            "preconditions": {
                "title_match": "^CompletelyDifferentTitle$",
            },
            "steps": [
                {"action": "key", "key": "ctrl+s"},
            ],
            "recovery": [
                {"action": "key", "key": "escape"},
                {"action": "key", "key": "ctrl+c"},
            ],
        }
        res = execute_macro_node(spec, self.mock_handlers)
        self.assertFalse(res["ok"])
        ev = res["evidence"]
        self.assertFalse(ev["preconditions_met"])
        self.assertTrue(ev["recovery"]["attempted"])
        self.assertEqual(len(ev["recovery"]["steps"]), 2)
        self.assertEqual(len(self.dispatched_keys), 2)
        self.assertEqual(self.dispatched_keys[0][1], "escape")
        self.assertEqual(self.dispatched_keys[1][1], "ctrl+c")

    def test_step_failure_and_recovery(self):
        def buggy_key(target, key):
            if key == "buggy_key":
                raise RuntimeError("Hardware device detached")
            self.dispatched_keys.append((target["hwnd"], key))

        handlers = dict(self.mock_handlers)
        handlers["key_raw"] = buggy_key

        spec = {
            "id": "node_step_fail",
            "app": "testapp",
            "steps": [
                {"action": "key", "key": "normal"},
                {"action": "key", "key": "buggy_key"},
                {"action": "key", "key": "never_reached"},
            ],
            "recovery": [
                {"action": "key", "key": "escape"},
            ],
        }
        res = execute_macro_node(spec, handlers)
        self.assertFalse(res["ok"])
        ev = res["evidence"]
        self.assertEqual(ev["steps_executed"], 2)
        self.assertEqual(ev["steps_log"][1]["status"], "error")
        self.assertTrue(ev["recovery"]["attempted"])
        self.assertEqual(self.dispatched_keys[-1][1], "escape")

    def test_parameter_interpolation(self):
        spec = {
            "id": "node_type_param",
            "app": "testapp",
            "steps": [
                {"action": "type", "text_template": ":w {output_file}\n"},
            ],
        }
        params = {"output_file": "report_2026.txt"}
        res = execute_macro_node(spec, self.mock_handlers, params=params)
        self.assertTrue(res["ok"])
        self.assertEqual(len(self.dispatched_types), 1)
        self.assertEqual(self.dispatched_types[0][1], ":w report_2026.txt\n")

    def test_visual_diff_postcondition_evaluation(self):
        # Mock diff function and see_raw
        mock_diff_results = {"diff_ratio": 0.05, "changed": True}

        def mock_diff_fn(before, after, region_str=None, mask_str=None, threshold=0.001):
            return {
                "ok": True,
                "action": "diff",
                "evidence": dict(mock_diff_results),
            }

        captured_screenshots = []

        def mock_see_raw(target, output_path=None):
            captured_screenshots.append(output_path)
            # Create a dummy file
            with open(output_path, "wb") as f:
                f.write(b"PNG_MOCK_DATA")
            return {"path": output_path, "size_bytes": 13}

        handlers = dict(self.mock_handlers)
        handlers["see_raw"] = mock_see_raw

        spec = {
            "id": "node_diff_test",
            "app": "testapp",
            "preconditions": {
                "see": True,
            },
            "steps": [
                {"action": "sleep", "duration": 0.01},
            ],
            "postconditions": {
                "diff": {
                    "min_ratio": 0.01,
                    "max_ratio": 0.10,
                },
            },
        }
        res = execute_macro_node(spec, handlers, diff_fn=mock_diff_fn)
        self.assertTrue(res["ok"])
        self.assertEqual(len(captured_screenshots), 2)  # baseline and post
        self.assertAlmostEqual(res["evidence"]["verification"]["diff_ratio"], 0.05)

        # Test failure when diff ratio outside expected bounds
        mock_diff_results["diff_ratio"] = 0.50
        res_fail = execute_macro_node(spec, handlers, diff_fn=mock_diff_fn)
        self.assertFalse(res_fail["ok"])
        self.assertIn("diff ratio 0.50000 outside range", res_fail["error"])

    def test_tool_registry_schema_validation(self):
        # Validate that app.macro_node is registered and accepts valid schema
        macro_tool = registry.get("app.macro_node")
        self.assertIsNotNone(macro_tool)
        schema = macro_tool["inputs"]

        valid_args = {
            "app": "vim",
            "id": "save_buffer",
            "preconditions": {"focused": True, "title_match": ".*"},
            "steps": [
                {"action": "key", "key": "escape"},
                {"action": "type", "text": ":w\n"},
            ],
            "postconditions": {"focused": True, "assert_changed": False},
        }
        # Should validate without error
        validate_schema(valid_args, schema)

        # Invalid args: missing steps
        invalid_args = {"app": "vim"}
        with self.assertRaises(SchemaValidationError):
            validate_schema(invalid_args, schema)

        # Invalid step action
        invalid_step = {
            "app": "vim",
            "steps": [{"action": "invalid_action_verb"}],
        }
        with self.assertRaises(SchemaValidationError):
            validate_schema(invalid_step, schema)


if __name__ == "__main__":
    unittest.main()
