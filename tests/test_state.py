import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from zc360.state import normalize_state, read_state, state_path, update_state


class StateTests(unittest.TestCase):
    def test_normalization_bounds_values_and_repairs_panel_order(self):
        state = normalize_state({
            "layout": "wat",
            "fit": "manual",
            "frames_per_second": 99,
            "panel_order": [0, 0, 7],
            "framing": {"focus_x": -2, "focus_y": 3, "zoom": 80},
        })
        self.assertEqual(state["layout"], "span")
        self.assertEqual(state["frames_per_second"], 20.0)
        self.assertEqual(state["panel_order"], [0, 1, 2])
        self.assertEqual(state["framing"], {"focus_x": 0.0, "focus_y": 1.0, "zoom": 8.0})

    def test_public_display_modes_and_refresh_values_are_normalized(self):
        state = normalize_state({
            "mode": "weather",
            "weather_location": "Osaka",
            "weather_units": "imperial",
            "weather_refresh_seconds": 5,
            "external_refresh_seconds": 999,
        })
        self.assertEqual(state["mode"], "weather")
        self.assertEqual(state["weather_units"], "imperial")
        self.assertEqual(state["weather_refresh_seconds"], 60.0)
        self.assertEqual(state["external_refresh_seconds"], 60.0)
        self.assertEqual(normalize_state({"mode": "unknown"})["mode"], "idle")

    def test_legacy_state_is_migrated_and_kept_in_sync(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            legacy = root / "lucille-shell" / "zc360-display.json"
            legacy.parent.mkdir(parents=True)
            legacy.write_text(json.dumps({"mode": "media", "source": "/tmp/demo.png"}))
            with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(root)}, clear=False):
                state = read_state()
                self.assertEqual(state["source"], "/tmp/demo.png")
                self.assertTrue(state_path().is_file())
                update_state(lambda value: value.update({"paused": True}))
                self.assertTrue(json.loads(legacy.read_text())["paused"])


if __name__ == "__main__":
    unittest.main()
