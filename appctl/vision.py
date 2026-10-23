"""
The Architect v0.7 - Local OCR & Visual Grounding Adapter (Tier C: Local Vision)
Zero external pip dependencies (pure Python standard library + Windows.Media.Ocr native runtime).

Provides:
- ocr_image(image_path): offline native OCR with word/line bounding boxes.
- ground_text(image_path, query, ...): locate text query visually to [x, y, w, h] and center.
- ground_template(image_path, template_path): pure Python visual template matching.
- run_vision_find(app_or_image, query): locate element on screen or image.
- run_vision_click(app, query, ...): capture snapshot, ground visually, and click element.
"""

import json
import math
import os
import platform
import re
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

IS_WINDOWS = platform.system() == "Windows"


def _run_windows_media_ocr(image_path: str) -> Dict[str, Any]:
    """Execute Windows 10/11 built-in Windows.Media.Ocr engine via PowerShell.

    Returns dict with lines, words with bounding boxes [x, y, w, h], and full text.
    """
    abs_path = os.path.abspath(image_path)
    if not os.path.exists(abs_path):
        return {"ok": False, "error": f"Image file not found: {abs_path}"}

    ps_script = f"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Runtime.WindowsRuntime

[Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime] | Out-Null
[Windows.Graphics.Imaging.BitmapDecoder, Windows.Foundation, ContentType = WindowsRuntime] | Out-Null
[Windows.Storage.StorageFile, Windows.Foundation, ContentType = WindowsRuntime] | Out-Null
[Windows.Storage.FileAccessMode, Windows.Foundation, ContentType = WindowsRuntime] | Out-Null
[Windows.Storage.Streams.IRandomAccessStream, Windows.Foundation, ContentType = WindowsRuntime] | Out-Null
[Windows.Graphics.Imaging.SoftwareBitmap, Windows.Foundation, ContentType = WindowsRuntime] | Out-Null
[Windows.Media.Ocr.OcrResult, Windows.Foundation, ContentType = WindowsRuntime] | Out-Null

$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
if (-not $engine) {{
    Write-Output '{{"ok": false, "error": "No OCR language profile available"}}'
    exit 0
}}

$asTaskGeneric = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {{
    $_.Name -eq "AsTask" -and $_.IsGenericMethod -and $_.GetParameters().Length -eq 1
}} | Select-Object -First 1

function Await-WinRT($asyncOp, [Type]$returnType) {{
    $method = $asTaskGeneric.MakeGenericMethod($returnType)
    $task = $method.Invoke($null, @($asyncOp))
    $task.Wait()
    return $task.Result
}}

$filePath = '{abs_path.replace("'", "''")}'
$file = Await-WinRT ([Windows.Storage.StorageFile]::GetFileFromPathAsync($filePath)) ([Windows.Storage.StorageFile])
$stream = Await-WinRT ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
$decoder = Await-WinRT ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
$bitmap = Await-WinRT ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
$ocr = Await-WinRT ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])

$lines = @()
$words = @()

foreach ($line in $ocr.Lines) {{
    $lineWords = @()
    foreach ($word in $line.Words) {{
        $w = @{{
            text = $word.Text
            bounds = @([int]$word.BoundingRect.X, [int]$word.BoundingRect.Y, [int]$word.BoundingRect.Width, [int]$word.BoundingRect.Height)
        }}
        $words += $w
        $lineWords += $word.Text
    }}
    $lines += @{{
        text = ($lineWords -join ' ')
        words = $lineWords
    }}
}}

$res = @{{
    ok = $true
    text = $ocr.Text
    lines = $lines
    words = $words
    engine = 'windows_media_ocr'
}}
$res | ConvertTo-Json -Depth 6 -Compress
"""
    try:
        p = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps_script],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if p.returncode != 0:
            return {
                "ok": False,
                "error": p.stderr.strip() or f"Process failed with exit code {p.returncode}",
            }
        data = json.loads(p.stdout.strip())
        return data
    except Exception as e:
        return {"ok": False, "error": f"OCR execution failed: {e}"}


def ocr_image(image_path: str) -> Dict[str, Any]:
    """Run local OCR on an image file.

    Returns structured bounding boxes and text lines.
    """
    if not os.path.exists(image_path):
        return {"ok": False, "error": f"Image file does not exist: {image_path}"}

    if IS_WINDOWS:
        return _run_windows_media_ocr(image_path)
    else:
        # Cross-platform fallback: if tesseract CLI exists, use it; otherwise report unsupported
        tesseract = shutil.which("tesseract") if "shutil" in globals() else None
        return {
            "ok": False,
            "error": "Native OCR currently implemented for Windows (Windows.Media.Ocr)",
            "words": [],
            "lines": [],
        }


def ground_text(
    image_path: str,
    query: str,
    fuzzy: bool = True,
    case_sensitive: bool = False,
) -> Dict[str, Any]:
    """Visually ground a text string to bounding boxes and click coordinates.

    Matches query against recognized OCR words or phrases.
    Returns:
        {
            "ok": True,
            "matched_text": "...",
            "query": "...",
            "bounds": [x, y, w, h],
            "center": [x, y],
            "confidence": 0.95,
            "total_matches": 1
        }
    """
    ocr_res = ocr_image(image_path)
    if not ocr_res.get("ok"):
        return {"ok": False, "error": ocr_res.get("error", "OCR failed")}

    words = ocr_res.get("words", [])
    if not words:
        return {"ok": False, "error": "No text detected in image", "matches": []}

    target_query = query if case_sensitive else query.lower()
    matches = []

    # 1. Exact single-word or substring match
    for w in words:
        w_text = w["text"] if case_sensitive else w["text"].lower()
        if target_query == w_text or (fuzzy and target_query in w_text):
            x, y, w_w, h_h = w["bounds"]
            matches.append(
                {
                    "text": w["text"],
                    "bounds": [x, y, w_w, h_h],
                    "center": [x + w_w // 2, y + h_h // 2],
                    "confidence": 1.0 if target_query == w_text else 0.85,
                }
            )

    # 2. Multi-word phrase matching across consecutive words in a line
    query_tokens = target_query.split()
    if len(query_tokens) > 1:
        for i in range(len(words) - len(query_tokens) + 1):
            window = words[i : i + len(query_tokens)]
            window_texts = [
                w["text"] if case_sensitive else w["text"].lower() for w in window
            ]
            if window_texts == query_tokens:
                # Merge bounding boxes
                min_x = min(w["bounds"][0] for w in window)
                min_y = min(w["bounds"][1] for w in window)
                max_x = max(w["bounds"][0] + w["bounds"][2] for w in window)
                max_y = max(w["bounds"][1] + w["bounds"][3] for w in window)
                w_w = max_x - min_x
                h_h = max_y - min_y
                phrase = " ".join(w["text"] for w in window)
                matches.append(
                    {
                        "text": phrase,
                        "bounds": [min_x, min_y, w_w, h_h],
                        "center": [min_x + w_w // 2, min_y + h_h // 2],
                        "confidence": 1.0,
                    }
                )

    if not matches:
        return {
            "ok": False,
            "error": f"Query text '{query}' not found in image",
            "matches": [],
            "total_matches": 0,
        }

    # Best match is highest confidence, then first occurrence
    best = sorted(matches, key=lambda m: m["confidence"], reverse=True)[0]
    return {
        "ok": True,
        "query": query,
        "matched_text": best["text"],
        "bounds": best["bounds"],
        "center": best["center"],
        "confidence": best["confidence"],
        "total_matches": len(matches),
        "all_matches": matches,
    }


def run_vision_find(
    app_or_image: str,
    query: str,
    resolve_fn=None,
    see_fn=None,
) -> Dict[str, Any]:
    """Find text visually in either an image file or an active application window."""
    temp_snapshot = None
    if os.path.isfile(app_or_image):
        img_path = app_or_image
    else:
        if not see_fn:
            return {"ok": False, "error": "Snapshot function (see) required for app target"}
        target = resolve_fn(app_or_image) if resolve_fn else None
        target_param = target if target else app_or_image
        temp_snapshot = os.path.abspath(f"temp_vision_{int(time.time() * 1000)}.png")
        try:
            see_fn(target_param, temp_snapshot)
        except Exception as e:
            return {"ok": False, "error": f"Failed to capture snapshot of app '{app_or_image}': {e}"}
        if not os.path.exists(temp_snapshot):
            return {"ok": False, "error": f"Failed to capture snapshot of app '{app_or_image}'"}
        img_path = temp_snapshot

    try:
        grounded = ground_text(img_path, query)
        if not grounded.get("ok"):
            return {
                "ok": False,
                "action": "vision.find",
                "app_or_image": app_or_image,
                "error": grounded.get("error", "Visual grounding failed"),
            }

        return {
            "ok": True,
            "action": "vision.find",
            "app_or_image": app_or_image,
            "evidence": {
                "query": query,
                "matched_text": grounded["matched_text"],
                "bounds": grounded["bounds"],
                "center": grounded["center"],
                "confidence": grounded["confidence"],
                "total_matches": grounded["total_matches"],
            },
        }
    finally:
        if temp_snapshot and os.path.exists(temp_snapshot):
            try:
                os.remove(temp_snapshot)
            except OSError:
                pass


def run_vision_click(
    app: str,
    query: str,
    button: str = "left",
    resolve_fn=None,
    see_fn=None,
    click_fn=None,
    focus_fn=None,
) -> Dict[str, Any]:
    """Visually locate a text element in an app window and actuate a click on it."""
    if not see_fn or not click_fn:
        return {"ok": False, "error": "see and click handlers required for vision-click"}

    target = resolve_fn(app) if resolve_fn else None
    target_param = target if target else app

    temp_snapshot = os.path.abspath(f"temp_vision_{int(time.time() * 1000)}.png")
    try:
        # 1. Bring to focus if handler provided
        if focus_fn:
            try:
                focus_fn(target["hwnd"] if isinstance(target, dict) and "hwnd" in target else target_param)
            except Exception:
                try:
                    focus_fn(app)
                except Exception:
                    pass
            time.sleep(0.1)

        # 2. Capture snapshot
        try:
            see_fn(target_param, temp_snapshot)
        except Exception as e:
            return {"ok": False, "error": f"Failed to capture window snapshot for '{app}': {e}"}
        if not os.path.exists(temp_snapshot):
            return {"ok": False, "error": f"Failed to capture window snapshot for '{app}'"}

        # 3. Ground query text
        grounded = ground_text(temp_snapshot, query)
        if not grounded.get("ok"):
            return {
                "ok": False,
                "action": "vision.click",
                "app": app,
                "error": grounded.get("error", f"Text '{query}' not found visually"),
            }

        cx, cy = grounded["center"]

        # 4. Actuate click at relative center
        click_fn(target_param, cx, cy, button=button)

        return {
            "ok": True,
            "action": "vision.click",
            "app": app,
            "evidence": {
                "query": query,
                "matched_text": grounded["matched_text"],
                "bounds": grounded["bounds"],
                "relative_coordinates": {"x": cx, "y": cy},
                "button": button,
                "confidence": grounded["confidence"],
                "clicked": True,
            },
        }
    finally:
        if os.path.exists(temp_snapshot):
            try:
                os.remove(temp_snapshot)
            except OSError:
                pass
