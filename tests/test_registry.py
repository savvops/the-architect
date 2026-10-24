import json
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from appctl.registry import (
    ToolRegistry,
    SchemaValidationError,
    validate_schema,
    SEED_REGISTRY,
)


class TestSchemaValidation(unittest.TestCase):
    def test_primitive_types(self):
        validate_schema("hello", {"type": "string"})
        validate_schema(123, {"type": "integer"})
        validate_schema(3.14, {"type": "number"})
        validate_schema(True, {"type": "boolean"})
        validate_schema([1, 2], {"type": "array"})
        validate_schema({"a": 1}, {"type": "object"})

        with self.assertRaises(SchemaValidationError):
            validate_schema(123, {"type": "string"})

        with self.assertRaises(SchemaValidationError):
            validate_schema("not_a_bool", {"type": "boolean"})

        with self.assertRaises(SchemaValidationError):
            validate_schema(True, {"type": "integer"})

    def test_required_and_properties(self):
        schema = {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "age": {"type": "integer", "minimum": 0},
            },
            "required": ["name"],
        }
        # Valid
        validate_schema({"name": "Alice", "age": 30}, schema)
        validate_schema({"name": "Bob"}, schema)

        # Missing required
        with self.assertRaises(SchemaValidationError) as ctx:
            validate_schema({"age": 20}, schema)
        self.assertIn("Missing required field", str(ctx.exception))

        # Wrong type in property
        with self.assertRaises(SchemaValidationError) as ctx:
            validate_schema({"name": "Charlie", "age": "thirty"}, schema)
        self.assertIn("expected type 'integer'", str(ctx.exception))

        # Out of bounds
        with self.assertRaises(SchemaValidationError) as ctx:
            validate_schema({"name": "Dan", "age": -5}, schema)
        self.assertIn("is less than minimum", str(ctx.exception))

    def test_enum(self):
        schema = {"type": "string", "enum": ["red", "green", "blue"]}
        validate_schema("green", schema)
        with self.assertRaises(SchemaValidationError):
            validate_schema("yellow", schema)


class TestRegistry(unittest.TestCase):
    def setUp(self):
        self.reg = ToolRegistry(load_custom=False)

    def test_seed_tools_loaded(self):
        tools = self.reg.list_tools()
        self.assertGreaterEqual(len(tools), 10)
        tool_ids = {t["tool_id"] for t in tools}
        self.assertIn("app.open", tool_ids)
        self.assertIn("app.focus", tool_ids)
        self.assertIn("app.see", tool_ids)
        self.assertIn("app.diff", tool_ids)
        self.assertIn("app.type", tool_ids)
        self.assertIn("app.key", tool_ids)
        self.assertIn("editor.save_all", tool_ids)
        self.assertIn("notepad.save", tool_ids)
        self.assertIn("calc.calculate", tool_ids)

    def test_filter_by_app(self):
        notepad_tools = self.reg.list_tools("notepad")
        self.assertGreaterEqual(len(notepad_tools), 1)
        for t in notepad_tools:
            self.assertIn("notepad", t["tool_id"].lower() + t.get("app", "").lower())

    def test_validation_and_preparation(self):
        tool, args = self.reg.validate_and_prepare("editor.save_all", {"app": "code"})
        self.assertEqual(tool["tool_id"], "editor.save_all")
        self.assertEqual(args["app"], "code")

        # Missing required field
        with self.assertRaises(SchemaValidationError):
            self.reg.validate_and_prepare("editor.save_all", {})

        # Unregistered tool
        with self.assertRaises(KeyError):
            self.reg.validate_and_prepare("nonexistent.tool", {})


class TestCliRegistryCommands(unittest.TestCase):
    def run_appctl(self, *args):
        cmd = [sys.executable, os.path.abspath("appctl/appctl.py")] + list(args)
        proc = subprocess.run(cmd, capture_output=True, text=True)
        try:
            data = json.loads(proc.stdout.strip())
        except Exception:
            data = None
        return proc.returncode, data, proc.stderr

    def test_cli_tools(self):
        rc, out, _ = self.run_appctl("tools")
        self.assertEqual(rc, 0)
        self.assertTrue(out["ok"])
        self.assertEqual(out["action"], "tools")
        self.assertGreater(out["count"], 5)

    def test_cli_tools_filtered(self):
        rc, out, _ = self.run_appctl("tools", "notepad")
        self.assertEqual(rc, 0)
        self.assertTrue(out["ok"])
        self.assertEqual(out["filter"], "notepad")

    def test_cli_schema(self):
        rc, out, _ = self.run_appctl("schema", "editor.save_all")
        self.assertEqual(rc, 0)
        self.assertTrue(out["ok"])
        self.assertEqual(out["tool_id"], "editor.save_all")
        self.assertIn("inputs", out["tool"])

    def test_cli_schema_unregistered(self):
        rc, out, _ = self.run_appctl("schema", "fake.tool")
        self.assertEqual(rc, 1)
        self.assertFalse(out["ok"])
        self.assertIn("unregistered tool", out["error"])

    def test_cli_exec_unregistered_rejected(self):
        rc, out, _ = self.run_appctl("exec", "unregistered.fake")
        self.assertEqual(rc, 1)
        self.assertFalse(out["ok"])
        self.assertIn("unregistered tool", out["error"])

    def test_cli_exec_schema_violation_rejected(self):
        rc, out, _ = self.run_appctl("exec", "editor.save_all", "--args-json", "{}")
        self.assertEqual(rc, 1)
        self.assertFalse(out["ok"])
        self.assertIn("schema validation failed", out["error"])

    def test_cli_exec_valid_command(self):
        rc, out, _ = self.run_appctl("exec", "app.list")
        self.assertEqual(rc, 0)
        self.assertTrue(out["ok"])
        self.assertEqual(out["action"], "list")

    def test_cli_click_schema(self):
        rc, out, _ = self.run_appctl("schema", "app.click")
        self.assertEqual(rc, 0)
        self.assertTrue(out["ok"])
        self.assertEqual(out["tool_id"], "app.click")
        self.assertEqual(out["tool"]["inputs"]["required"], ["app", "x", "y"])

    def test_cli_exec_click_missing_coords(self):
        rc, out, _ = self.run_appctl("exec", "app.click", "--args-json", '{"app": "notepad"}')
        self.assertEqual(rc, 1)
        self.assertFalse(out["ok"])
        self.assertIn("Missing required field", out["error"])


if __name__ == "__main__":
    unittest.main()
