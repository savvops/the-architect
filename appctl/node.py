"""
The Architect - Macro-Node Runner (Phase 5: Tier B - Keyboard nodes)
Focus + state contracts, typed action sequences, postcondition verification,
visual pixel diffing, automated recovery, and verifiable evidence payloads.
Zero external pip dependencies (pure Python standard library).
"""

import json
import os
import re
import sys
import time
from typing import Any, Dict, List, Optional, Tuple


def _load_spec(spec_or_path: Any) -> Dict[str, Any]:
    """Parse spec from dict, JSON string, or JSON file path."""
    if isinstance(spec_or_path, dict):
        return spec_or_path
    if isinstance(spec_or_path, str):
        spec_or_path = spec_or_path.strip()
        if os.path.isfile(spec_or_path):
            with open(spec_or_path, "r", encoding="utf-8") as f:
                return json.load(f)
        if (spec_or_path.startswith("{") and spec_or_path.endswith("}")) or (
            spec_or_path.startswith("[") and spec_or_path.endswith("]")
        ):
            return json.loads(spec_or_path)
    raise ValueError(f"Invalid macro-node specification: {spec_or_path!r}")


def execute_macro_node(
    spec_input: Any,
    handlers: Dict[str, Any],
    diff_fn=None,
    see_fn=None,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Execute a macro-node contract with focus/state verification and recovery.

    Parameters:
        spec_input: Dict or path to JSON or JSON string defining node contract.
        handlers: OS dispatch handlers dictionary (must include resolve, focus, key_raw, type_raw, click_raw).
        diff_fn: Function to compute diff between two PNG paths (compute_diff).
        see_fn: Function to capture window screenshot to PNG.
        params: Optional dict for text_template parameter substitution.

    Returns:
        Standard Architect JSON payload with typed evidence.
    """
    try:
        spec = _load_spec(spec_input)
    except Exception as e:
        return {
            "ok": False,
            "action": "node",
            "error": f"Failed to parse macro-node spec: {e}",
            "evidence": {"parsed": False},
        }

    node_id = spec.get("id", "macro_node")
    app = spec.get("app")
    if not app:
        return {
            "ok": False,
            "action": "node",
            "error": "macro-node specification missing required 'app' field",
            "evidence": {"id": node_id},
        }

    preconditions = spec.get("preconditions", {})
    steps = spec.get("steps", [])
    postconditions = spec.get("postconditions", {})
    recovery_steps = spec.get("recovery", [])
    merged_params = dict(params or {})

    # Helper for recovery execution
    def run_recovery(target_ctx, reason: str) -> Dict[str, Any]:
        recovery_log = []
        if not recovery_steps:
            return {"attempted": False, "reason": reason}
        if not target_ctx or "key_raw" not in handlers:
            return {"attempted": False, "reason": f"recovery unavailable: {reason}"}

        for idx, rstep in enumerate(recovery_steps):
            r_action = rstep.get("action")
            try:
                if r_action == "key":
                    handlers["key_raw"](target_ctx, rstep.get("key"))
                elif r_action == "type":
                    text = rstep.get("text", "")
                    handlers["type_raw"](target_ctx, text)
                elif r_action == "sleep":
                    time.sleep(float(rstep.get("duration", 0.1)))
                recovery_log.append({"step": idx, "action": r_action, "status": "dispatched"})
                time.sleep(0.05)
            except Exception as rx:
                recovery_log.append({"step": idx, "action": r_action, "error": str(rx)})
        return {"attempted": True, "reason": reason, "steps": recovery_log}

    # 1. Resolve target
    if "resolve" not in handlers:
        return {
            "ok": False,
            "action": "node",
            "error": f"Target resolution not supported on this platform",
            "evidence": {"id": node_id, "app": app},
        }

    target = handlers["resolve"](app)
    if not target:
        rec_res = run_recovery(None, f"target '{app}' not found")
        return {
            "ok": False,
            "action": "node",
            "error": f"target application '{app}' not found or has no visible window",
            "evidence": {
                "id": node_id,
                "app": app,
                "preconditions_met": False,
                "recovery": rec_res,
            },
        }

    # 2. Check Preconditions
    pre_evidence: Dict[str, Any] = {}

    # Precondition: title_match
    if "title_match" in preconditions:
        pattern = preconditions["title_match"]
        title = target.get("title", "")
        if not re.search(pattern, title, re.IGNORECASE):
            rec_res = run_recovery(target, f"title '{title}' did not match pattern '{pattern}'")
            return {
                "ok": False,
                "action": "node",
                "error": f"Precondition failed: window title '{title}' did not match '{pattern}'",
                "evidence": {
                    "id": node_id,
                    "app": app,
                    "preconditions_met": False,
                    "expected_title_match": pattern,
                    "actual_title": title,
                    "recovery": rec_res,
                },
            }
        pre_evidence["title_matched"] = True

    # Precondition: focused
    if preconditions.get("focused", True):
        # Attempt focus if not already focused
        if "focus_raw" in handlers:
            ok, actual = handlers["focus_raw"](target["hwnd"])
            if not ok:
                rec_res = run_recovery(target, "focus precondition failed")
                return {
                    "ok": False,
                    "action": "node",
                    "error": f"Precondition failed: could not focus '{app}'",
                    "evidence": {
                        "id": node_id,
                        "app": app,
                        "preconditions_met": False,
                        "actual_hwnd": actual,
                        "recovery": rec_res,
                    },
                }
        elif "focus" in handlers:
            f_res = handlers["focus"](app)
            if isinstance(f_res, dict) and not f_res.get("ok"):
                rec_res = run_recovery(target, "focus precondition failed")
                return {
                    "ok": False,
                    "action": "node",
                    "error": f"Precondition failed: could not focus '{app}'",
                    "evidence": {
                        "id": node_id,
                        "app": app,
                        "preconditions_met": False,
                        "focus_result": f_res,
                        "recovery": rec_res,
                    },
                }
        pre_evidence["focused"] = True

    # Precondition: baseline screenshot if diff postcondition is requested or see requested
    baseline_img_path = None
    needs_diff = "diff" in postconditions or postconditions.get("assert_changed") or postconditions.get("assert_unchanged")
    if (preconditions.get("see") or needs_diff):
        import tempfile
        temp_dir = os.path.join(tempfile.gettempdir(), "appctl_node")
        os.makedirs(temp_dir, exist_ok=True)
        baseline_img_path = os.path.join(temp_dir, f"baseline_{int(time.time() * 1000)}.png")
        try:
            if "see_raw" in handlers:
                handlers["see_raw"](target, output_path=baseline_img_path)
            elif see_fn:
                see_fn(app, output_path=baseline_img_path)
        except Exception as e:
            rec_res = run_recovery(target, f"baseline screenshot failed: {e}")
            return {
                "ok": False,
                "action": "node",
                "error": f"Precondition failed: could not capture baseline screenshot: {e}",
                "evidence": {
                    "id": node_id,
                    "app": app,
                    "preconditions_met": False,
                    "recovery": rec_res,
                },
            }
        pre_evidence["baseline_see"] = baseline_img_path

    # 3. Action Sequence Execution
    executed_steps: List[Dict[str, Any]] = []
    step_error: Optional[str] = None

    for idx, step in enumerate(steps):
        action = step.get("action")
        step_log: Dict[str, Any] = {"step": idx, "action": action}

        # Guard: re-verify target window before dispatching input
        cur_target = handlers["resolve"](app)
        if not cur_target:
            step_error = f"Target window disappeared before step {idx} ({action})"
            break

        try:
            if action == "key":
                key_name = step.get("key")
                if not key_name:
                    raise ValueError(f"Step {idx} missing 'key'")
                handlers["key_raw"](cur_target, key_name)
                step_log["key"] = key_name
                step_log["status"] = "ok"

            elif action == "type":
                text = step.get("text")
                if text is None and "text_template" in step:
                    text = step["text_template"].format(**merged_params)
                if text is None:
                    raise ValueError(f"Step {idx} missing 'text' or 'text_template'")
                handlers["type_raw"](cur_target, text)
                step_log["chars"] = len(text)
                step_log["status"] = "ok"

            elif action == "click":
                x = step.get("x")
                y = step.get("y")
                button = step.get("button", "left")
                if x is None or y is None:
                    raise ValueError(f"Step {idx} missing 'x' or 'y'")
                if "click_raw" not in handlers:
                    raise NotImplementedError("click_raw not supported on this platform")
                handlers["click_raw"](cur_target, int(x), int(y), button)
                step_log["x"] = int(x)
                step_log["y"] = int(y)
                step_log["button"] = button
                step_log["status"] = "ok"

            elif action == "sleep":
                duration = float(step.get("duration", 0.1))
                time.sleep(duration)
                step_log["duration"] = duration
                step_log["status"] = "ok"

            else:
                raise ValueError(f"Unsupported step action: '{action}'")

            executed_steps.append(step_log)
            time.sleep(0.05)

        except Exception as ex:
            step_error = f"Step {idx} ({action}) failed: {ex}"
            step_log["status"] = "error"
            step_log["error"] = str(ex)
            executed_steps.append(step_log)
            break

    if step_error:
        rec_res = run_recovery(target, step_error)
        return {
            "ok": False,
            "action": "node",
            "error": step_error,
            "evidence": {
                "id": node_id,
                "app": app,
                "preconditions_met": True,
                "steps_executed": len(executed_steps),
                "steps_log": executed_steps,
                "postconditions_met": False,
                "recovery": rec_res,
            },
        }

    # 4. Check Postconditions
    post_evidence: Dict[str, Any] = {}
    post_target = handlers["resolve"](app)

    # Postcondition: title_match
    if "title_match" in postconditions and post_target:
        pattern = postconditions["title_match"]
        new_title = post_target.get("title", "")
        if not re.search(pattern, new_title, re.IGNORECASE):
            rec_res = run_recovery(post_target, f"postcondition title mismatch: '{new_title}' != '{pattern}'")
            return {
                "ok": False,
                "action": "node",
                "error": f"Postcondition failed: window title '{new_title}' did not match '{pattern}'",
                "evidence": {
                    "id": node_id,
                    "app": app,
                    "preconditions_met": True,
                    "steps_executed": len(executed_steps),
                    "steps_log": executed_steps,
                    "postconditions_met": False,
                    "expected_title_match": pattern,
                    "actual_title": new_title,
                    "recovery": rec_res,
                },
            }
        post_evidence["title_matched"] = True
        post_evidence["title"] = new_title

    # Postcondition: focused
    if postconditions.get("focused") and post_target:
        is_focused = True
        if sys.platform == "win32":
            import ctypes
            fg = ctypes.windll.user32.GetForegroundWindow()
            if fg != post_target["hwnd"] and ctypes.windll.user32.GetAncestor(fg, 2) != post_target["hwnd"]:
                is_focused = False
        if not is_focused:
            rec_res = run_recovery(post_target, "postcondition focus failed")
            return {
                "ok": False,
                "action": "node",
                "error": f"Postcondition failed: window '{app}' is no longer focused",
                "evidence": {
                    "id": node_id,
                    "app": app,
                    "preconditions_met": True,
                    "steps_executed": len(executed_steps),
                    "postconditions_met": False,
                    "recovery": rec_res,
                },
            }
        post_evidence["focused"] = True

    # Postcondition: diff evaluation
    if needs_diff and diff_fn and baseline_img_path and post_target:
        import tempfile
        temp_dir = os.path.join(tempfile.gettempdir(), "appctl_node")
        post_img_path = os.path.join(temp_dir, f"post_{int(time.time() * 1000)}.png")
        try:
            if "see_raw" in handlers:
                handlers["see_raw"](post_target, output_path=post_img_path)
            elif see_fn:
                see_fn(app, output_path=post_img_path)
        except Exception as e:
            rec_res = run_recovery(post_target, f"post-step screenshot failed: {e}")
            return {
                "ok": False,
                "action": "node",
                "error": f"Postcondition verification failed: could not capture post screenshot: {e}",
                "evidence": {
                    "id": node_id,
                    "app": app,
                    "preconditions_met": True,
                    "steps_executed": len(executed_steps),
                    "recovery": rec_res,
                },
            }

        diff_spec = postconditions.get("diff", {})
        min_ratio = diff_spec.get("min_ratio", 0.0)
        max_ratio = diff_spec.get("max_ratio", 1.0)
        threshold = diff_spec.get("threshold", 0.001)
        region = diff_spec.get("region")
        mask = diff_spec.get("mask")

        if postconditions.get("assert_changed"):
            min_ratio = max(min_ratio, threshold)
        if postconditions.get("assert_unchanged"):
            max_ratio = min(max_ratio, threshold)

        diff_res = diff_fn(
            baseline_img_path, post_img_path, region_str=region, mask_str=mask, threshold=threshold
        )

        diff_evidence = diff_res.get("evidence", {})
        diff_ratio = diff_evidence.get("diff_ratio", 0.0)
        post_evidence["diff_ratio"] = diff_ratio
        post_evidence["diff_result"] = diff_res

        if diff_ratio < min_ratio or diff_ratio > max_ratio:
            rec_res = run_recovery(
                post_target,
                f"diff ratio {diff_ratio:.5f} outside expected range [{min_ratio}, {max_ratio}]",
            )
            return {
                "ok": False,
                "action": "node",
                "error": f"Postcondition verification failed: diff ratio {diff_ratio:.5f} outside range [{min_ratio}, {max_ratio}]",
                "evidence": {
                    "id": node_id,
                    "app": app,
                    "preconditions_met": True,
                    "steps_executed": len(executed_steps),
                    "steps_log": executed_steps,
                    "postconditions_met": False,
                    "diff_ratio": diff_ratio,
                    "expected_min": min_ratio,
                    "expected_max": max_ratio,
                    "baseline_img": baseline_img_path,
                    "post_img": post_img_path,
                    "recovery": rec_res,
                },
            }

    # 5. Success Return
    return {
        "ok": True,
        "action": "node",
        "evidence": {
            "id": node_id,
            "app": app,
            "pid": target["pid"],
            "window": target["title"],
            "preconditions_met": True,
            "steps_executed": len(executed_steps),
            "steps_log": executed_steps,
            "postconditions_met": True,
            "verification": post_evidence,
            "recovered": False,
        },
    }
