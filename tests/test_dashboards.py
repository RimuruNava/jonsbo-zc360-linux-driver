import tempfile
import unittest
import urllib.parse
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from zc360.dashboards import (
    SystemDashboard,
    WeatherClient,
    WeatherDashboard,
    load_external_frames,
    validate_external_directory,
)
from zc360.renderer import dashboard_key, identify_active


class FakePsutil:
    network = SimpleNamespace(bytes_recv=1024, bytes_sent=2048)

    @staticmethod
    def cpu_percent(interval=None):
        return 42.0

    @staticmethod
    def virtual_memory():
        return SimpleNamespace(percent=55.0, used=8 * 1024**3)

    @staticmethod
    def disk_usage(path):
        return SimpleNamespace(percent=66.0)

    @classmethod
    def net_io_counters(cls):
        return cls.network

    @staticmethod
    def sensors_temperatures():
        return {"coretemp": [SimpleNamespace(current=61.5)]}

    @staticmethod
    def boot_time():
        return 0.0


class DashboardTests(unittest.TestCase):
    def test_system_dashboard_renders_three_mapped_panels(self):
        dashboard = SystemDashboard(
            {"panel_order": [2, 0, 1], "telemetry_refresh_seconds": 2},
            psutil_module=FakePsutil,
        )
        images, delay = dashboard.frame()
        self.assertEqual(sorted(images), [0, 1, 2])
        self.assertEqual(images[0].size, (640, 180))
        self.assertEqual(delay, 2.0)

    def test_external_triptych_and_individual_frames(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            triptych = Image.new("RGB", (1920, 180), "red")
            triptych.paste(Image.new("RGB", (640, 180), "green"), (640, 0))
            triptych.paste(Image.new("RGB", (640, 180), "blue"), (1280, 0))
            triptych.save(root / "triptych.png")
            frames = load_external_frames(root, [2, 0, 1])
            self.assertEqual(frames[2].getpixel((20, 20)), (255, 0, 0))
            self.assertEqual(frames[0].getpixel((20, 20)), (0, 128, 0))
            (root / "triptych.png").unlink()
            for index, color in enumerate(("yellow", "purple", "cyan")):
                Image.new("RGB", (320, 90), color).save(root / f"panel-{index}.png")
            frames = load_external_frames(root, [0, 1, 2])
            self.assertEqual(frames[0].size, (640, 180))
            self.assertEqual(frames[2].getpixel((20, 20)), (0, 255, 255))

    def test_external_directory_requires_complete_contract(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            Image.new("RGB", (640, 180)).save(root / "panel-0.png")
            with self.assertRaisesRegex(RuntimeError, "triptych.png"):
                validate_external_directory(root)

    def test_weather_client_geocodes_and_fetches_metric_forecast(self):
        urls = []

        def fetch(url):
            urls.append(url)
            if "geocoding-api" in url:
                return {"results": [{
                    "name": "Osaka", "admin1": "Osaka", "country_code": "JP",
                    "latitude": 34.69, "longitude": 135.5,
                }]}
            return {
                "current": {
                    "temperature_2m": 25, "apparent_temperature": 27,
                    "relative_humidity_2m": 60, "weather_code": 1,
                    "wind_speed_10m": 8,
                },
                "daily": {
                    "time": ["2026-09-05", "2026-09-06", "2026-09-07"],
                    "weather_code": [1, 2, 3],
                    "temperature_2m_max": [29, 28, 27],
                    "temperature_2m_min": [21, 20, 19],
                    "precipitation_probability_max": [10, 20, 30],
                },
            }

        client = WeatherClient(fetcher=fetch)
        dashboard = WeatherDashboard(
            {"panel_order": [0, 1, 2], "weather_location": "Osaka", "weather_units": "metric"},
            client=client,
        )
        images, delay = dashboard.frame()
        self.assertEqual(len(images), 3)
        self.assertEqual(delay, 900.0)
        self.assertEqual(len(urls), 2)
        query = urllib.parse.parse_qs(urllib.parse.urlparse(urls[1]).query)
        self.assertEqual(query["latitude"], ["34.69"])
        dashboard.frame()
        self.assertEqual(len(urls), 2, "forecast should use the fifteen-minute cache")

    def test_dashboard_key_and_identify_expiry(self):
        state = {
            "mode": "external", "panel_order": [0, 1, 2],
            "external_directory": "/tmp/frames", "external_refresh_seconds": 1,
        }
        self.assertIn("/tmp/frames", dashboard_key(state))
        self.assertTrue(identify_active({"identify_until": 100.0}, now=99.0))
        self.assertFalse(identify_active({"identify_until": 100.0}, now=100.0))


if __name__ == "__main__":
    unittest.main()
