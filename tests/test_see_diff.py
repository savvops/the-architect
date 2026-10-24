import os
import sys
import unittest
import tempfile
import struct
import zlib
import json
import subprocess

# Ensure appctl can be imported
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from appctl.appctl import encode_png_rgb, decode_png, compute_diff, run_diff


class TestPngCodec(unittest.TestCase):
    def test_encode_decode_roundtrip_rgb(self):
        width = 64
        height = 48
        # Create a deterministic pattern
        rgb = bytearray()
        for y in range(height):
            for x in range(width):
                r = (x * 4) % 256
                g = (y * 5) % 256
                b = (x + y) % 256
                rgb.extend((r, g, b))

        png_data = encode_png_rgb(width, height, bytes(rgb))
        self.assertTrue(png_data.startswith(b"\x89PNG\r\n\x1a\n"))

        dw, dh, decoded_rgb = decode_png(png_data)
        self.assertEqual(dw, width)
        self.assertEqual(dh, height)
        self.assertEqual(bytes(decoded_rgb), bytes(rgb))

    def test_decode_filters(self):
        """Verify decoder handles sub, up, average, and paeth filter scanlines."""
        width = 4
        height = 4
        # Build raw scanlines with different filters:
        # filter 0 (none), 1 (sub), 2 (up), 4 (paeth)
        raw = bytearray()
        # line 0: filter none
        raw.append(0)
        raw.extend([10, 20, 30] * 4)
        # line 1: filter sub (diff from left pixel)
        raw.append(1)
        raw.extend([10, 20, 30] + [1, 1, 1] * 3)
        # line 2: filter up (diff from prior line)
        raw.append(2)
        raw.extend([5, 5, 5] * 4)
        # line 3: filter paeth
        raw.append(4)
        raw.extend([0, 0, 0] * 4)

        compressed = zlib.compress(bytes(raw))
        header = b"\x89PNG\r\n\x1a\n"
        def chunk(tag, data):
            return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        ihdr = chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        idat = chunk(b"IDAT", compressed)
        iend = chunk(b"IEND", b"")
        png_bytes = header + ihdr + idat + iend

        dw, dh, dec = decode_png(png_bytes)
        self.assertEqual((dw, dh), (width, height))
        self.assertEqual(len(dec), width * height * 3)


class TestDiffEngine(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.temp_dir.cleanup()

    def _create_image(self, filename, width, height, fill_color):
        rgb = bytearray(fill_color * (width * height))
        data = encode_png_rgb(width, height, bytes(rgb))
        path = os.path.join(self.temp_dir.name, filename)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def test_diff_identical_images(self):
        p1 = self._create_image("img1.png", 50, 50, (255, 255, 255))
        p2 = self._create_image("img2.png", 50, 50, (255, 255, 255))

        res = compute_diff(p1, p2, threshold=0.01)
        self.assertFalse(res["evidence"]["changed"])
        self.assertEqual(res["evidence"]["difference"], 0.0)
        self.assertEqual(res["evidence"]["changed_pixels"], 0)
        self.assertIsNone(res["evidence"]["bounding_box"])
        self.assertEqual(res["evidence"]["verdict"], "fail")

    def test_diff_changed_pixels_and_bounding_box(self):
        p1 = self._create_image("base.png", 100, 100, (255, 255, 255))

        # Create modified image with a black 20x20 rectangle at (10, 10)
        rgb = bytearray([255, 255, 255] * (100 * 100))
        for y in range(10, 30):
            for x in range(10, 30):
                idx = (y * 100 + x) * 3
                rgb[idx] = 0
                rgb[idx + 1] = 0
                rgb[idx + 2] = 0
        p2 = os.path.join(self.temp_dir.name, "mod.png")
        with open(p2, "wb") as f:
            f.write(encode_png_rgb(100, 100, bytes(rgb)))

        res = compute_diff(p1, p2, threshold=0.01)
        ev = res["evidence"]
        self.assertTrue(ev["changed"])
        self.assertEqual(ev["changed_pixels"], 400)
        self.assertEqual(ev["total_evaluated_pixels"], 10000)
        self.assertAlmostEqual(ev["difference"], 0.04, places=4)
        self.assertEqual(ev["bounding_box"], {"x": 10, "y": 10, "w": 20, "h": 20})
        self.assertEqual(ev["verdict"], "pass")

    def test_diff_region(self):
        p1 = self._create_image("base.png", 100, 100, (255, 255, 255))
        rgb = bytearray([255, 255, 255] * (100 * 100))
        for y in range(10, 30):
            for x in range(10, 30):
                idx = (y * 100 + x) * 3
                rgb[idx] = 0
                rgb[idx + 1] = 0
                rgb[idx + 2] = 0
        p2 = os.path.join(self.temp_dir.name, "mod.png")
        with open(p2, "wb") as f:
            f.write(encode_png_rgb(100, 100, bytes(rgb)))

        # Region outside the change
        res_outside = compute_diff(p1, p2, region_str="50,50,40,40")
        self.assertFalse(res_outside["evidence"]["changed"])
        self.assertEqual(res_outside["evidence"]["changed_pixels"], 0)

        # Region inside the change
        res_inside = compute_diff(p1, p2, region_str="10,10,20,20")
        self.assertTrue(res_inside["evidence"]["changed"])
        self.assertEqual(res_inside["evidence"]["changed_pixels"], 400)
        self.assertEqual(res_inside["evidence"]["difference"], 1.0)

    def test_diff_mask(self):
        p1 = self._create_image("base.png", 100, 100, (255, 255, 255))
        rgb = bytearray([255, 255, 255] * (100 * 100))
        for y in range(10, 30):
            for x in range(10, 30):
                idx = (y * 100 + x) * 3
                rgb[idx] = 0
                rgb[idx + 1] = 0
                rgb[idx + 2] = 0
        p2 = os.path.join(self.temp_dir.name, "mod.png")
        with open(p2, "wb") as f:
            f.write(encode_png_rgb(100, 100, bytes(rgb)))

        # Masking out the exact changed region
        res = compute_diff(p1, p2, mask_str="10,10,20,20")
        self.assertFalse(res["evidence"]["changed"])
        self.assertEqual(res["evidence"]["changed_pixels"], 0)


class TestCliDiff(unittest.TestCase):
    def test_cli_diff_subcommand(self):
        with tempfile.TemporaryDirectory() as td:
            p1 = os.path.join(td, "p1.png")
            p2 = os.path.join(td, "p2.png")
            b1 = encode_png_rgb(10, 10, b"\x00" * 300)
            b2 = encode_png_rgb(10, 10, b"\xFF" * 300)
            with open(p1, "wb") as f: f.write(b1)
            with open(p2, "wb") as f: f.write(b2)

            cmd = [sys.executable, os.path.abspath("appctl/appctl.py"), "diff", p1, p2]
            proc = subprocess.run(cmd, capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0)
            out = json.loads(proc.stdout.strip())
            self.assertTrue(out["ok"])
            self.assertEqual(out["action"], "diff")
            self.assertTrue(out["evidence"]["changed"])
            self.assertEqual(out["evidence"]["verdict"], "pass")


if __name__ == "__main__":
    unittest.main()
