import importlib.util
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw

from zc360.gui import create_window_class, main as gui_main
from zc360.media import (
    crop_box,
    fit_image,
    load_source_frame,
    open_preview_source,
    preview_size,
    render_first_frame,
    split_frame,
)


class MediaTests(unittest.TestCase):
    def test_manual_framing_moves_crop_focus(self):
        image = Image.new("RGB", (200, 100), "red")
        ImageDraw.Draw(image).rectangle((100, 0, 199, 99), fill="blue")
        left = fit_image(image, (100, 100), "manual", {"focus_x": 0, "focus_y": 0.5})
        right = fit_image(image, (100, 100), "manual", {"focus_x": 1, "focus_y": 0.5})
        self.assertEqual(left.getpixel((20, 50)), (255, 0, 0))
        self.assertEqual(right.getpixel((80, 50)), (0, 0, 255))

    def test_crop_box_matches_panel_aspect_and_zoom(self):
        left, top, right, bottom = crop_box(
            (1920, 1080),
            (640, 180),
            {"focus_x": 1.0, "focus_y": 0.0, "zoom": 2.0},
        )
        self.assertAlmostEqual((right - left) / (bottom - top), 640 / 180)
        self.assertAlmostEqual(right, 1920.0)
        self.assertAlmostEqual(top, 0.0)

    def test_uncropped_editor_frame_preserves_source_dimensions(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "source.png"
            Image.new("RGB", (321, 123), "purple").save(path)
            frame = load_source_frame(path)
            self.assertEqual(frame.size, (321, 123))

    def test_live_gif_editor_preview_keeps_full_source_aspect(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "source.gif"
            frames = [
                Image.new("RGB", (1600, 900), "red"),
                Image.new("RGB", (1600, 900), "blue"),
            ]
            frames[0].save(path, save_all=True, append_images=frames[1:], duration=80, loop=0)
            source = open_preview_source(path)
            try:
                first, _ = source.frame()
                second, _ = source.frame()
            finally:
                source.close()
            self.assertTrue(source.animated)
            self.assertEqual(first.size, (1280, 720))
            self.assertNotEqual(first.getpixel((0, 0)), second.getpixel((0, 0)))

    def test_preview_size_never_upscales(self):
        self.assertEqual(preview_size((640, 180)), (640, 180))
        self.assertEqual(preview_size((3840, 2160)), (1280, 720))

    def test_panel_order_maps_visual_columns_to_stable_indexes(self):
        triptych = Image.new("RGB", (1920, 180))
        draw = ImageDraw.Draw(triptych)
        draw.rectangle((0, 0, 639, 179), fill="red")
        draw.rectangle((640, 0, 1279, 179), fill="green")
        draw.rectangle((1280, 0, 1919, 179), fill="blue")
        frames = split_frame(triptych, "span", [2, 0, 1])
        self.assertEqual(frames[2].getpixel((0, 0)), (255, 0, 0))
        self.assertEqual(frames[0].getpixel((0, 0)), (0, 128, 0))
        self.assertEqual(frames[1].getpixel((0, 0)), (0, 0, 255))

    def test_static_three_panel_preview_uses_generic_engine(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths = []
            for index, color in enumerate(("red", "green", "blue")):
                path = Path(temporary) / f"{index}.png"
                Image.new("RGB", (640, 180), color).save(path)
                paths.append(str(path))
            frames = render_first_frame({
                "layout": "panels",
                "fit": "cover",
                "frames_per_second": 8,
                "panel_order": [0, 1, 2],
                "panel_framing": [{}, {}, {}],
                "sources": paths,
            })
            self.assertEqual(sorted(frames), [0, 1, 2])
            self.assertEqual(frames[2].getpixel((0, 0)), (0, 0, 255))

    def test_gui_version_does_not_require_qt_import(self):
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(gui_main(["--version"]), 0)
        self.assertIn("zc360-gui", output.getvalue())

    @unittest.skipUnless(importlib.util.find_spec("PySide6"), "PySide6 is not installed")
    def test_qt_window_has_one_crop_editor_button_per_position(self):
        with tempfile.TemporaryDirectory() as temporary:
            os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
            from PySide6 import QtCore, QtGui, QtWidgets

            state_file = Path(temporary) / "state.json"
            with patch.dict(os.environ, {"ZC360_STATE": str(state_file)}):
                application = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
                window_class = create_window_class(QtCore, QtGui, QtWidgets)
                with patch("zc360.gui.daemon_status", side_effect=OSError("offline test")):
                    window = window_class()
                self.assertEqual(len(window.edit_buttons), 3)
                self.assertTrue(all("Edit" in button.text() for button in window.edit_buttons))
                self.assertFalse(hasattr(window, "focus_x"))
                window.close()
                application.processEvents()


if __name__ == "__main__":
    unittest.main()
