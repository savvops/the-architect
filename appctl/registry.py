"""
The Architect v0.3 - Tool Registry & JSON Schema Engine
Zero external pip dependencies (pure Python standard library).
"""

import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple


class SchemaValidationError(Exception):
    pass


def validate_schema(value: Any, schema: Dict[str, Any], path: str = "root") -> None:
    """Validate a value against a JSON Schema (draft-7 subset).

    Supports: type, required, properties, items, enum, minimum, maximum, minLength.
    """
    if not isinstance(schema, dict):
        return

    expected_type = schema.get("type")
    if expected_type:
        type_checks = {
            "string": lambda v: isinstance(v, str),
            "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
            "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
            "boolean": lambda v: isinstance(v, bool),
            "array": lambda v: isinstance(v, list),
            "object": lambda v: isinstance(v, dict),
            "null": lambda v: v is None,
        }
        checker = type_checks.get(expected_type)
        if checker and not checker(value):
            actual_type = type(value).__name__
            raise SchemaValidationError(
                f"Field '{path}' expected type '{expected_type}', got '{actual_type}'"
            )

    if "enum" in schema:
        if value not in schema["enum"]:
            raise SchemaValidationError(
                f"Field '{path}' value {value!r} not in allowed enum: {schema['enum']}"
            )

    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            raise SchemaValidationError(
                f"Field '{path}' string length {len(value)} is less than minLength {schema['minLength']}"
            )

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            raise SchemaValidationError(
                f"Field '{path}' value {value} is less than minimum {schema['minimum']}"
            )
        if "maximum" in schema and value > schema["maximum"]:
            raise SchemaValidationError(
                f"Field '{path}' value {value} is greater than maximum {schema['maximum']}"
            )

    if isinstance(value, dict):
        required_fields = schema.get("required", [])
        for req in required_fields:
            if req not in value:
                raise SchemaValidationError(f"Missing required field '{path}.{req}'")

        properties = schema.get("properties", {})
        for prop_name, prop_val in value.items():
            if prop_name in properties:
                validate_schema(prop_val, properties[prop_name], path=f"{path}.{prop_name}")

    if isinstance(value, list) and "items" in schema:
        item_schema = schema["items"]
        for idx, item in enumerate(value):
            validate_schema(item, item_schema, path=f"{path}[{idx}]")


# Built-in seed registry
SEED_REGISTRY: List[Dict[str, Any]] = [
    # ------------------------------------------------------------- Core verbs -
    {
        "tool_id": "app.open",
        "app": "system",
        "action": "open",
        "inputs": {
            "type": "object",
            "properties": {
                "app": {"type": "string", "description": "Target application name or path"},
                "args": {"type": "array", "items": {"type": "string"}, "description": "CLI arguments"}
            },
            "required": ["app"]
        },
        "expect": {"postconditions": ["process_running", "window_visible"]},
        "risk": "low"
    },
    {
        "tool_id": "app.focus",
        "app": "system",
        "action": "focus",
        "inputs": {
            "type": "object",
            "properties": {
                "app": {"type": "string", "description": "Target application name, title, or PID"}
            },
            "required": ["app"]
        },
        "expect": {"postconditions": ["foreground_active"]},
        "risk": "low"
    },
    {
        "tool_id": "app.see",
        "app": "system",
        "action": "see",
        "inputs": {
            "type": "object",
            "properties": {
                "app": {"type": "string", "description": "Target application name, title, or PID"},
                "output": {"type": "string", "description": "Destination file path for PNG snapshot"}
            },
            "required": ["app"]
        },
        "expect": {"postconditions": ["screenshot_file_exists", "geometry_captured"]},
        "risk": "low"
    },
    {
        "tool_id": "app.diff",
        "app": "system",
        "action": "diff",
        "inputs": {
            "type": "object",
            "properties": {
                "before": {"type": "string", "description": "Path to before PNG image"},
                "after": {"type": "string", "description": "Path to after PNG image"},
                "region": {"type": "string", "description": "Bounding box x,y,w,h to inspect"},
                "mask": {"type": "string", "description": "Bounding box x,y,w,h to exclude"},
                "threshold": {"type": "number", "minimum": 0.0, "maximum": 1.0, "description": "Change threshold ratio"}
            },
            "required": ["before", "after"]
        },
        "expect": {"postconditions": ["pixel_difference_evaluated"]},
        "risk": "low"
    },
    {
        "tool_id": "app.type",
        "app": "system",
        "action": "type",
        "inputs": {
            "type": "object",
            "properties": {
                "app": {"type": "string", "description": "Target application name, title, or PID"},
                "text": {"type": "string", "description": "Text content to type into the focused element"}
            },
            "required": ["app", "text"]
        },
        "expect": {"postconditions": ["keystrokes_dispatched"]},
        "risk": "medium"
    },
    {
        "tool_id": "app.key",
        "app": "system",
        "action": "key",
        "inputs": {
            "type": "object",
            "properties": {
                "app": {"type": "string", "description": "Target application name, title, or PID"},
                "key": {"type": "string", "description": "Key identifier or modifier combo (e.g. 'ctrl+s', 'enter')"}
            },
            "required": ["app", "key"]
        },
        "expect": {"postconditions": ["key_combination_dispatched"]},
        "risk": "medium"
    },
    {
        "tool_id": "app.click",
        "app": "system",
        "action": "click",
        "inputs": {
            "type": "object",
            "properties": {
                "app": {"type": "string", "description": "Target application name, title, or PID"},
                "x": {"type": "integer", "description": "X coordinate relative to window top-left"},
                "y": {"type": "integer", "description": "Y coordinate relative to window top-left"},
                "button": {"type": "string", "enum": ["left", "right", "double"], "description": "Mouse button action (default: left)"}
            },
            "required": ["app", "x", "y"]
        },
        "expect": {"postconditions": ["mouse_click_dispatched"]},
        "risk": "medium"
    },
    {
        "tool_id": "app.status",
        "app": "system",
        "action": "status",
        "inputs": {
            "type": "object",
            "properties": {
                "app": {"type": "string", "description": "Application name or PID"}
            },
            "required": ["app"]
        },
        "expect": {"postconditions": ["status_queried"]},
        "risk": "low"
    },
    {
        "tool_id": "app.list",
        "app": "system",
        "action": "list",
        "inputs": {
            "type": "object",
            "properties": {}
        },
        "expect": {"postconditions": ["processes_enumerated"]},
        "risk": "low"
    },
    {
        "tool_id": "app.quit",
        "app": "system",
        "action": "quit",
        "inputs": {
            "type": "object",
            "properties": {
                "app": {"type": "string", "description": "Application name or PID"},
                "force": {"type": "boolean", "description": "Force kill without asking"}
            },
            "required": ["app"]
        },
        "expect": {"postconditions": ["process_terminated"]},
        "risk": "medium"
    },
    {
        "tool_id": "app.macro_node",
        "app": "system",
        "action": "node",
        "inputs": {
            "type": "object",
            "properties": {
                "app": {"type": "string", "description": "Target application name, title, or PID"},
                "id": {"type": "string", "description": "Macro-node workflow identifier"},
                "preconditions": {
                    "type": "object",
                    "properties": {
                        "focused": {"type": "boolean", "description": "Require target application to be focused"},
                        "title_match": {"type": "string", "description": "Regex pattern window title must match"},
                        "see": {"type": "boolean", "description": "Capture baseline screenshot before execution"}
                    }
                },
                "steps": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "action": {"type": "string", "enum": ["key", "type", "click", "sleep"]},
                            "key": {"type": "string"},
                            "text": {"type": "string"},
                            "text_template": {"type": "string"},
                            "x": {"type": "integer"},
                            "y": {"type": "integer"},
                            "button": {"type": "string", "enum": ["left", "right", "double"]},
                            "duration": {"type": "number"}
                        },
                        "required": ["action"]
                    },
                    "description": "Sequence of typed workflow steps"
                },
                "postconditions": {
                    "type": "object",
                    "properties": {
                        "focused": {"type": "boolean", "description": "Verify target application is focused after execution"},
                        "title_match": {"type": "string", "description": "Regex pattern window title must match after execution"},
                        "assert_changed": {"type": "boolean", "description": "Assert visual pixels changed"},
                        "assert_unchanged": {"type": "boolean", "description": "Assert visual pixels remained unchanged"},
                        "diff": {"type": "object"}
                    }
                },
                "recovery": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "Fallback steps to execute if execution or verification fails"
                }
            },
            "required": ["app", "steps"]
        },
        "expect": {"postconditions": ["preconditions_verified", "steps_dispatched", "postconditions_verified"]},
        "risk": "medium"
    },
    {
        "tool_id": "a11y.tree",
        "app": "system",
        "action": "a11y_tree",
        "inputs": {
            "type": "object",
            "properties": {
                "app": {"type": "string", "description": "Target application name, title, or PID"},
                "depth": {"type": "integer", "minimum": 1, "maximum": 10, "description": "Max hierarchy depth (default: 3)"},
                "max_children": {"type": "integer", "minimum": 1, "maximum": 100, "description": "Max child elements per node (default: 25)"}
            },
            "required": ["app"]
        },
        "expect": {"postconditions": ["semantic_tree_enumerated"]},
        "risk": "low"
    },
    {
        "tool_id": "a11y.query",
        "app": "system",
        "action": "a11y_query",
        "inputs": {
            "type": "object",
            "properties": {
                "app": {"type": "string", "description": "Target application name, title, or PID"},
                "role": {"type": "string", "description": "Control role filter (e.g. 'button', 'edit', 'menu item')"},
                "name": {"type": "string", "description": "Element name regex or substring filter"},
                "id": {"type": "string", "description": "Automation ID regex or substring filter"}
            },
            "required": ["app"]
        },
        "expect": {"postconditions": ["matching_elements_found"]},
        "risk": "low"
    },
    {
        "tool_id": "a11y.click",
        "app": "system",
        "action": "a11y_click",
        "inputs": {
            "type": "object",
            "properties": {
                "app": {"type": "string", "description": "Target application name, title, or PID"},
                "role": {"type": "string", "description": "Control role filter (e.g. 'button')"},
                "name": {"type": "string", "description": "Element name regex or substring filter"},
                "id": {"type": "string", "description": "Automation ID regex or substring filter"},
                "button": {"type": "string", "enum": ["left", "right", "double"], "description": "Mouse button action (default: left)"}
            },
            "required": ["app"]
        },
        "expect": {"postconditions": ["element_clicked"]},
        "risk": "medium"
    },

    # ----------------------------------------------------------- vision tools -
    {
        "tool_id": "vision.ocr",
        "app": "system",
        "action": "ocr",
        "inputs": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "Target application name or image file path"}
            },
            "required": ["target"]
        },
        "expect": {"postconditions": ["text_recognized_with_bounds"]},
        "risk": "low"
    },
    {
        "tool_id": "vision.find",
        "app": "system",
        "action": "vision_find",
        "inputs": {
            "type": "object",
            "properties": {
                "app": {"type": "string", "description": "Target application name or image file path"},
                "query": {"type": "string", "description": "Text query to locate visually"}
            },
            "required": ["app", "query"]
        },
        "expect": {"postconditions": ["visual_text_grounded"]},
        "risk": "low"
    },
    {
        "tool_id": "vision.click",
        "app": "system",
        "action": "vision_click",
        "inputs": {
            "type": "object",
            "properties": {
                "app": {"type": "string", "description": "Target application name"},
                "query": {"type": "string", "description": "Text query to locate and click visually"},
                "button": {"type": "string", "enum": ["left", "right", "double"], "description": "Mouse button action (default: left)"}
            },
            "required": ["app", "query"]
        },
        "expect": {"postconditions": ["visual_target_clicked"]},
        "risk": "medium"
    },

    # ----------------------------------------------------------- router tools -
    {
        "tool_id": "router.route",
        "app": "system",
        "action": "route",
        "inputs": {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "Task or intent description to route"},
                "context": {"type": "object", "description": "Contextual state (app, has_a11y, etc.)"}
            },
            "required": ["task"]
        },
        "expect": {"postconditions": ["decision_calibrated", "tool_selected"]},
        "risk": "low"
    },
    {
        "tool_id": "router.benchmark",
        "app": "system",
        "action": "benchmark",
        "inputs": {
            "type": "object",
            "properties": {}
        },
        "expect": {"postconditions": ["scorecard_generated"]},
        "risk": "low"
    },

    # ----------------------------------------------------------- editor tools -
    {
        "tool_id": "editor.save_all",
        "app": "editor",
        "action": "key",
        "inputs": {
            "type": "object",
            "properties": {
                "app": {"type": "string", "description": "Editor application name or PID"}
            },
            "required": ["app"]
        },
        "params": {"key": "ctrl+k s"},
        "expect": {"postconditions": ["buffers_saved"]},
        "risk": "low"
    },
    {
        "tool_id": "editor.find_file",
        "app": "editor",
        "action": "macro",
        "inputs": {
            "type": "object",
            "properties": {
                "app": {"type": "string", "description": "Editor application name or PID"},
                "filename": {"type": "string", "description": "Filename pattern to open"}
            },
            "required": ["app", "filename"]
        },
        "params": {
            "steps": [
                {"action": "key", "key": "ctrl+p"},
                {"action": "type", "text_template": "{filename}"},
                {"action": "key", "key": "enter"}
            ]
        },
        "expect": {"postconditions": ["file_opened"]},
        "risk": "low"
    },

    # ---------------------------------------------------------- notepad tools -
    {
        "tool_id": "notepad.save",
        "app": "notepad",
        "action": "key",
        "inputs": {
            "type": "object",
            "properties": {
                "app": {"type": "string", "description": "Target Notepad process or window"}
            },
            "required": ["app"]
        },
        "params": {"key": "ctrl+s"},
        "expect": {"postconditions": ["file_saved"]},
        "risk": "low"
    },
    {
        "tool_id": "notepad.find",
        "app": "notepad",
        "action": "macro",
        "inputs": {
            "type": "object",
            "properties": {
                "app": {"type": "string", "description": "Target Notepad process or window"},
                "query": {"type": "string", "description": "Search term"}
            },
            "required": ["app", "query"]
        },
        "params": {
            "steps": [
                {"action": "key", "key": "ctrl+f"},
                {"action": "type", "text_template": "{query}"},
                {"action": "key", "key": "enter"}
            ]
        },
        "expect": {"postconditions": ["search_executed"]},
        "risk": "low"
    },

    # ------------------------------------------------------------- calc tools -
    {
        "tool_id": "calc.calculate",
        "app": "calc",
        "action": "macro",
        "inputs": {
            "type": "object",
            "properties": {
                "app": {"type": "string", "description": "Target Calculator process or window"},
                "expression": {"type": "string", "description": "Mathematical formula to evaluate"}
            },
            "required": ["app", "expression"]
        },
        "params": {
            "steps": [
                {"action": "key", "key": "escape"},
                {"action": "type", "text_template": "{expression}"},
                {"action": "key", "key": "enter"}
            ]
        },
        "expect": {"postconditions": ["result_computed"]},
        "risk": "low"
    },
    {
        "tool_id": "calc.clear",
        "app": "calc",
        "action": "key",
        "inputs": {
            "type": "object",
            "properties": {
                "app": {"type": "string", "description": "Target Calculator process or window"}
            },
            "required": ["app"]
        },
        "params": {"key": "escape"},
        "expect": {"postconditions": ["display_cleared"]},
        "risk": "low"
    },
    # ----------------------------------------------------------- Browser Tools -
    {
        "tool_id": "browser.fork",
        "app": "browser",
        "action": "browser_fork",
        "inputs": {
            "type": "object",
            "properties": {
                "browser": {
                    "type": "string",
                    "enum": ["brave", "chrome", "chromium", "edge", "firefox"],
                    "description": "Browser flavor (brave, chrome, chromium, edge, firefox)",
                },
                "url": {
                    "type": "string",
                    "description": "Initial target URL",
                },
                "headless": {
                    "type": "boolean",
                    "description": "Launch headless in background",
                },
                "port": {
                    "type": "integer",
                    "minimum": 1024,
                    "maximum": 65535,
                    "description": "CDP remote debugging port",
                },
                "cookie_file": {
                    "type": "string",
                    "description": "Path to JSON cookie file to inject",
                },
                "copy_profile": {
                    "type": "boolean",
                    "description": "Whether to copy master profile state",
                },
            },
        },
        "expect": {"postconditions": ["ephemeral_process_started", "isolated_profile_created"]},
        "risk": "medium",
    },
    {
        "tool_id": "browser.list",
        "app": "browser",
        "action": "browser_list",
        "inputs": {
            "type": "object",
            "properties": {},
        },
        "expect": {"postconditions": ["workers_enumerated"]},
        "risk": "low",
    },
    {
        "tool_id": "browser.destroy",
        "app": "browser",
        "action": "browser_destroy",
        "inputs": {
            "type": "object",
            "properties": {
                "worker_id": {
                    "type": "string",
                    "description": "Worker ID, PID, or 'all'",
                },
            },
            "required": ["worker_id"],
        },
        "expect": {"postconditions": ["worker_terminated", "profile_purged"]},
        "risk": "medium",
    },
    {
        "tool_id": "browser.status",
        "app": "browser",
        "action": "browser_status",
        "inputs": {
            "type": "object",
            "properties": {},
        },
        "expect": {"postconditions": ["browser_status_reported"]},
        "risk": "low",
    },
]


class ToolRegistry:
    def __init__(self, load_custom: bool = True):
        self._tools: Dict[str, Dict[str, Any]] = {}
        for item in SEED_REGISTRY:
            self.register(item)

        if load_custom:
            self._load_custom_dirs()

    def register(self, tool_def: Dict[str, Any]) -> None:
        """Register a tool definition."""
        tool_id = tool_def.get("tool_id")
        if not tool_id:
            raise ValueError("Tool definition missing 'tool_id'")
        self._tools[tool_id] = tool_def

    def _load_custom_dirs(self) -> None:
        """Load tools from custom directories (~/.the-architect/tools/ and ./tools/)."""
        custom_dirs = [
            os.path.expanduser("~/.the-architect/tools"),
            os.path.join(os.getcwd(), "tools"),
        ]
        for cdir in custom_dirs:
            if os.path.isdir(cdir):
                for fname in os.listdir(cdir):
                    if fname.endswith(".json"):
                        fpath = os.path.join(cdir, fname)
                        try:
                            with open(fpath, "r", encoding="utf-8") as f:
                                data = json.load(f)
                                if isinstance(data, list):
                                    for t in data:
                                        self.register(t)
                                elif isinstance(data, dict) and "tool_id" in data:
                                    self.register(data)
                        except Exception:
                            pass

    def get(self, tool_id: str) -> Optional[Dict[str, Any]]:
        return self._tools.get(tool_id)

    def list_tools(self, app_filter: Optional[str] = None) -> List[Dict[str, Any]]:
        results = []
        app_lower = app_filter.lower() if app_filter else None
        for t in self._tools.values():
            if not app_lower or t.get("app", "").lower() == app_lower or app_lower in t.get("tool_id", "").lower():
                results.append(t)
        return results

    def validate_and_prepare(
        self, tool_id: str, args: Dict[str, Any]
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """Validate args against tool's JSON schema.

        Returns (tool_def, prepared_args).
        Raises KeyError if tool not found, SchemaValidationError if invalid.
        """
        tool = self.get(tool_id)
        if not tool:
            raise KeyError(f"unregistered tool: '{tool_id}'")

        schema = tool.get("inputs", {})
        validate_schema(args, schema, path=f"{tool_id}.args")
        return tool, args


# Global singleton
registry = ToolRegistry()
