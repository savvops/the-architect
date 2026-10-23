"""
Tests for The Architect v0.7 - Local OCR & Visual Grounding Adapter (Tier C: Local Vision).
"""

import json
import os
import unittest
from unittest.mock import MagicMock, patch

from appctl.vision import (
    ocr_image,
    ground_text,
    run_vision_find,
    run_vision_click,
)


class TestVisionGrounding(unittest.TestCase):
    def setUp(self):
        # Create a mock OCR result
        self.mock_ocr_result = {
            "ok": True,
            "text": "File Edit View History Bookmarks Settings Help",
            "lines": [
                {"text": "File Edit View History Bookmarks Settings Help"}
            ],
            "words": [
                {"text": "File", "bounds": [10, 5, 30, 20]},
                {"text": "Edit", "bounds": [45, 5, 30, 20]},
                {"text": "View", "bounds": [80, 5, 30, 20]},
                {"text": "History", "bounds": [115, 5, 50, 20]},
                {"text": "Bookmarks", "bounds": [170, 5, 70, 20]},
                {"text": "Settings", "bounds": [245, 5, 60, 20]},
                {"text": "Help", "bounds": [310, 5, 35, 20]},
            ],
            "engine": "mock_ocr",
        }

    @patch("appctl.vision.ocr_image")
    def test_ground_text_exact_match(self, mock_ocr):
        mock_ocr.return_value = self.mock_ocr_result

        res = ground_text("dummy.png", "Settings")
        self.assertTrue(res["ok"])
        self.assertEqual(res["matched_text"], "Settings")
        self.assertEqual(res["bounds"], [245, 5, 60, 20])
        self.assertEqual(res["center"], [275, 15])  # 245 + 60//2 = 275, 5 + 20//2 = 15
        self.assertEqual(res["confidence"], 1.0)

    @patch("appctl.vision.ocr_image")
    def test_ground_text_case_insensitive(self, mock_ocr):
        mock_ocr.return_value = self.mock_ocr_result

        res = ground_text("dummy.png", "settings", case_sensitive=False)
        self.assertTrue(res["ok"])
        self.assertEqual(res["matched_text"], "Settings")
        self.assertEqual(res["center"], [275, 15])

    @patch("appctl.vision.ocr_image")
    def test_ground_text_multi_word_phrase(self, mock_ocr):
        mock_ocr.return_value = self.mock_ocr_result

        res = ground_text("dummy.png", "Bookmarks Settings")
        self.assertTrue(res["ok"])
        self.assertEqual(res["matched_text"], "Bookmarks Settings")
        self.assertEqual(res["bounds"], [170, 5, 135, 20])  # min_x=170, max_x=305 -> w=135
        self.assertEqual(res["center"], [170 + 135 // 2, 5 + 20 // 2])

    @patch("appctl.vision.ocr_image")
    def test_ground_text_not_found(self, mock_ocr):
        mock_ocr.return_value = self.mock_ocr_result

        res = ground_text("dummy.png", "NonExistentButton")
        self.assertFalse(res["ok"])
        self.assertIn("not found", res["error"])

    @patch("appctl.vision.ground_text")
    def test_run_vision_find(self, mock_ground):
        mock_ground.return_value = {
            "ok": True,
            "matched_text": "Save",
            "bounds": [100, 200, 50, 25],
            "center": [125, 212],
            "confidence": 1.0,
            "total_matches": 1,
        }

        # Mock image file
        with patch("os.path.isfile", return_value=True):
            res = run_vision_find("snapshot.png", "Save")
            self.assertTrue(res["ok"])
            self.assertEqual(res["evidence"]["matched_text"], "Save")
            self.assertEqual(res["evidence"]["center"], [125, 212])

    @patch("appctl.vision.ground_text")
    def test_run_vision_click(self, mock_ground):
        mock_ground.return_value = {
            "ok": True,
            "matched_text": "Submit",
            "bounds": [150, 300, 80, 30],
            "center": [190, 315],
            "confidence": 1.0,
            "total_matches": 1,
        }

        mock_see = MagicMock(return_value={"ok": True})
        mock_click = MagicMock()
        mock_focus = MagicMock()

        with patch("os.path.exists", return_value=True), patch("os.remove"):
            res = run_vision_click(
                "chrome",
                "Submit",
                button="left",
                see_fn=mock_see,
                click_fn=mock_click,
                focus_fn=mock_focus,
            )
            self.assertTrue(res["ok"])
            mock_focus.assert_called_once_with("chrome")
            mock_see.assert_called_once()
            mock_click.assert_called_once_with("chrome", 190, 315, button="left")
            self.assertEqual(res["evidence"]["relative_coordinates"], {"x": 190, "y": 315})

    @patch("appctl.vision.ground_text")
    def test_run_vision_click_with_resolved_target(self, mock_ground):
        mock_ground.return_value = {
            "ok": True,
            "matched_text": "Submit",
            "bounds": [150, 300, 80, 30],
            "center": [190, 315],
            "confidence": 1.0,
            "total_matches": 1,
        }

        target_dict = {"hwnd": 12345, "pid": 6789, "title": "Test App"}
        mock_resolve = MagicMock(return_value=target_dict)
        mock_see = MagicMock(return_value={"ok": True})
        mock_click = MagicMock()
        mock_focus = MagicMock()

        with patch("os.path.exists", return_value=True), patch("os.remove"):
            res = run_vision_click(
                "testapp",
                "Submit",
                button="left",
                resolve_fn=mock_resolve,
                see_fn=mock_see,
                click_fn=mock_click,
                focus_fn=mock_focus,
            )
            self.assertTrue(res["ok"])
            mock_resolve.assert_called_once_with("testapp")
            mock_focus.assert_called_once_with(12345)
            mock_see.assert_called_once()
            self.assertEqual(mock_see.call_args[0][0], target_dict)
            mock_click.assert_called_once_with(target_dict, 190, 315, button="left")

    @patch("appctl.vision.ground_text")
    def test_run_vision_find_with_resolved_target(self, mock_ground):
        mock_ground.return_value = {
            "ok": True,
            "matched_text": "Options",
            "bounds": [10, 20, 30, 40],
            "center": [25, 40],
            "confidence": 0.95,
            "total_matches": 1,
        }

        target_dict = {"hwnd": 1111, "pid": 2222, "title": "Options App"}
        mock_resolve = MagicMock(return_value=target_dict)
        mock_see = MagicMock(return_value={"ok": True})

        with patch("os.path.isfile", return_value=False), patch("os.path.exists", return_value=True), patch("os.remove"):
            res = run_vision_find(
                "optionsapp",
                "Options",
                resolve_fn=mock_resolve,
                see_fn=mock_see,
            )
            self.assertTrue(res["ok"])
            mock_resolve.assert_called_once_with("optionsapp")
            mock_see.assert_called_once()
            self.assertEqual(mock_see.call_args[0][0], target_dict)


if __name__ == "__main__":
    unittest.main()
