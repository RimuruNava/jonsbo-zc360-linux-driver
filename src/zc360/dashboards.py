"""Hardware-blind dashboard and external-frame sources."""

from __future__ import annotations

import json
import platform
import socket
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .constants import LOGICAL_HEIGHT, LOGICAL_WIDTH
from .media import fit_image, split_frame

PANEL_SIZE = (LOGICAL_WIDTH, LOGICAL_HEIGHT)
TRIPTYCH_SIZE = (LOGICAL_WIDTH * 3, LOGICAL_HEIGHT)
BACKGROUND = "#080c13"
CARD = "#111927"
TEXT = "#ecf3ff"
MUTED = "#92a3ba"
PINK = "#ff64b5"
CYAN = "#61dff5"
GREEN = "#72e5b1"
YELLOW = "#ffcf66"


def _font(size: int, bold: bool = False):
    names = (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        if bold
        else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf"
        if bold
        else "/usr/share/fonts/TTF/DejaVuSans.ttf",
    )
    for name in names:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            pass
    return ImageFont.load_default()


FONT_SMALL = _font(14)
FONT_BODY = _font(18)
FONT_TITLE = _font(21, bold=True)
FONT_VALUE = _font(42, bold=True)


def _panel(title: str, accent: str = PINK) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    image = Image.new("RGB", PANEL_SIZE, BACKGROUND)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((8, 8, 631, 171), radius=12, fill=CARD, outline="#29364a", width=2)
    draw.rounded_rectangle((8, 8, 17, 171), radius=5, fill=accent)
    draw.text((34, 20), title.upper(), font=FONT_TITLE, fill=TEXT)
    return image, draw


def _ordered(frames: list[Image.Image], order: list[int]) -> dict[int, Image.Image]:
    return {physical: frames[position] for position, physical in enumerate(order)}


def _bar(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], value: float, color: str):
    left, top, right, bottom = box
    draw.rounded_rectangle(box, radius=6, fill="#202b3c")
    width = max(0, round((right - left) * max(0.0, min(100.0, value)) / 100.0))
    if width:
        draw.rounded_rectangle((left, top, left + width, bottom), radius=6, fill=color)


def _human_bytes(value: float) -> str:
    value = max(0.0, float(value))
    for suffix in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024.0 or suffix == "TB":
            return f"{value:.0f} {suffix}" if suffix in {"B", "KB"} else f"{value:.1f} {suffix}"
        value /= 1024.0
    return f"{value:.1f} TB"


def _uptime(seconds: float) -> str:
    minutes = max(0, int(seconds // 60))
    days, minutes = divmod(minutes, 1440)
    hours, minutes = divmod(minutes, 60)
    return f"{days}d {hours}h" if days else f"{hours}h {minutes}m"


class SystemDashboard:
    """Render portable CPU, memory, disk, network, and clock panels."""

    def __init__(self, state: dict, psutil_module=None):
        if psutil_module is None:
            try:
                import psutil as psutil_module
            except ImportError as exc:
                raise RuntimeError("system telemetry requires psutil") from exc
        self.psutil = psutil_module
        self.order = state["panel_order"]
        self.refresh = max(0.5, min(30.0, float(state.get("telemetry_refresh_seconds", 2.0))))
        self.last_network = None
        self.last_network_time = None
        self.psutil.cpu_percent(interval=None)

    def _temperature(self):
        try:
            readings = self.psutil.sensors_temperatures()
        except (AttributeError, OSError):
            return None
        preferred = ("k10temp", "coretemp", "cpu_thermal")
        groups = [readings.get(name, []) for name in preferred]
        groups.extend(values for name, values in readings.items() if name not in preferred)
        for group in groups:
            current = [float(item.current) for item in group if getattr(item, "current", None) is not None]
            if current:
                return max(current)
        return None

    def frame(self) -> tuple[dict[int, Image.Image], float]:
        now = time.monotonic()
        cpu = float(self.psutil.cpu_percent(interval=None))
        memory = self.psutil.virtual_memory()
        disk = self.psutil.disk_usage("/")
        network = self.psutil.net_io_counters()
        elapsed = max(0.001, now - self.last_network_time) if self.last_network_time else None
        rx = (network.bytes_recv - self.last_network.bytes_recv) / elapsed if elapsed else 0.0
        tx = (network.bytes_sent - self.last_network.bytes_sent) / elapsed if elapsed else 0.0
        self.last_network = network
        self.last_network_time = now
        temperature = self._temperature()

        first, draw = _panel("System telemetry", PINK)
        draw.text((34, 58), f"{cpu:.0f}%", font=FONT_VALUE, fill=TEXT)
        draw.text((155, 69), "CPU", font=FONT_BODY, fill=MUTED)
        if temperature is not None:
            draw.text((255, 69), f"{temperature:.0f}°C", font=FONT_BODY, fill=YELLOW)
        _bar(draw, (34, 125, 606, 143), cpu, PINK)
        draw.text((34, 149), f"Uptime {_uptime(time.time() - self.psutil.boot_time())}", font=FONT_SMALL, fill=MUTED)

        second, draw = _panel("Memory and storage", CYAN)
        draw.text((34, 61), f"RAM {memory.percent:.0f}%", font=FONT_VALUE, fill=TEXT)
        draw.text((345, 72), f"{_human_bytes(memory.used)} used", font=FONT_BODY, fill=MUTED)
        _bar(draw, (34, 118, 306, 134), float(memory.percent), CYAN)
        draw.text((34, 143), "MEMORY", font=FONT_SMALL, fill=MUTED)
        _bar(draw, (334, 118, 606, 134), float(disk.percent), GREEN)
        draw.text((334, 143), f"DISK {disk.percent:.0f}%", font=FONT_SMALL, fill=MUTED)

        third, draw = _panel("Network and clock", GREEN)
        draw.text((34, 60), datetime.now().strftime("%H:%M"), font=FONT_VALUE, fill=TEXT)
        draw.text((205, 71), datetime.now().strftime("%a %d %b"), font=FONT_BODY, fill=MUTED)
        draw.text((365, 58), f"↓ {_human_bytes(rx)}/s", font=FONT_BODY, fill=CYAN)
        draw.text((365, 88), f"↑ {_human_bytes(tx)}/s", font=FONT_BODY, fill=GREEN)
        hostname = socket.gethostname() or platform.node() or "Linux"
        draw.text((34, 143), hostname[:48], font=FONT_SMALL, fill=MUTED)
        return _ordered([first, second, third], self.order), self.refresh

    def close(self):
        pass


def _fetch_json(url: str, timeout: float = 10.0) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": "jonsbo-zc360/0.4"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


class WeatherClient:
    """Small Open-Meteo client with location and forecast caching."""

    def __init__(self, fetcher=None):
        self.fetcher = fetcher or _fetch_json
        self.cached_key = None
        self.cached_value = None
        self.cached_at = 0.0

    def get(self, location: str, units: str = "metric") -> dict:
        key = (location.strip().casefold(), units)
        now = time.monotonic()
        if self.cached_key == key and self.cached_value and now - self.cached_at < 900:
            return self.cached_value
        query = urllib.parse.urlencode({"name": location, "count": 1, "language": "en", "format": "json"})
        geocoding = self.fetcher(f"https://geocoding-api.open-meteo.com/v1/search?{query}")
        results = geocoding.get("results") or []
        if not results:
            raise RuntimeError(f"weather location was not found: {location}")
        place = results[0]
        parameters = {
            "latitude": place["latitude"],
            "longitude": place["longitude"],
            "current": "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
            "timezone": "auto",
            "forecast_days": 3,
        }
        if units == "imperial":
            parameters.update({"temperature_unit": "fahrenheit", "wind_speed_unit": "mph", "precipitation_unit": "inch"})
        forecast = self.fetcher(
            "https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(parameters)
        )
        forecast["display_name"] = ", ".join(
            str(value) for value in (place.get("name"), place.get("admin1"), place.get("country_code")) if value
        )
        self.cached_key = key
        self.cached_value = forecast
        self.cached_at = now
        return forecast


WEATHER_CODES = {
    0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Icy fog", 51: "Light drizzle", 53: "Drizzle", 55: "Heavy drizzle",
    56: "Freezing drizzle", 57: "Freezing drizzle", 61: "Light rain", 63: "Rain",
    65: "Heavy rain", 66: "Freezing rain", 67: "Freezing rain", 71: "Light snow",
    73: "Snow", 75: "Heavy snow", 77: "Snow grains", 80: "Rain showers",
    81: "Rain showers", 82: "Heavy showers", 85: "Snow showers", 86: "Heavy snow showers",
    95: "Thunderstorm", 96: "Storm and hail", 99: "Storm and hail",
}


def _daily(forecast: dict, index: int) -> dict:
    daily = forecast.get("daily", {})
    def value(name, default="—"):
        values = daily.get(name) or []
        return values[index] if index < len(values) else default
    return {
        "date": value("time"),
        "code": value("weather_code", -1),
        "high": value("temperature_2m_max"),
        "low": value("temperature_2m_min"),
        "rain": value("precipitation_probability_max"),
    }


def render_weather(forecast: dict, units: str, order: list[int]) -> dict[int, Image.Image]:
    current = forecast.get("current", {})
    temp_unit = "°F" if units == "imperial" else "°C"
    wind_unit = "mph" if units == "imperial" else "km/h"
    location = str(forecast.get("display_name") or "Weather")
    condition = WEATHER_CODES.get(int(current.get("weather_code", -1)), "Current conditions")

    first, draw = _panel(location[:44], CYAN)
    draw.text((34, 58), f"{current.get('temperature_2m', '—')}{temp_unit}", font=FONT_VALUE, fill=TEXT)
    draw.text((245, 64), condition[:28], font=FONT_TITLE, fill=YELLOW)
    draw.text(
        (245, 101),
        f"Feels {current.get('apparent_temperature', '—')}{temp_unit}  ·  Humidity {current.get('relative_humidity_2m', '—')}%",
        font=FONT_SMALL,
        fill=MUTED,
    )
    draw.text((34, 147), f"Wind {current.get('wind_speed_10m', '—')} {wind_unit}", font=FONT_SMALL, fill=MUTED)

    panels = [first]
    for index, accent in ((0, PINK), (1, GREEN)):
        day = _daily(forecast, index)
        try:
            label = datetime.fromisoformat(str(day["date"])).strftime("TODAY" if index == 0 else "%A").upper()
        except ValueError:
            label = "TODAY" if index == 0 else "TOMORROW"
        image, draw = _panel(label, accent)
        draw.text((34, 59), WEATHER_CODES.get(int(day["code"]), "Forecast")[:30], font=FONT_TITLE, fill=TEXT)
        draw.text((34, 99), f"High {day['high']}{temp_unit}", font=FONT_BODY, fill=YELLOW)
        draw.text((230, 99), f"Low {day['low']}{temp_unit}", font=FONT_BODY, fill=CYAN)
        draw.text((430, 99), f"Rain {day['rain']}%", font=FONT_BODY, fill=GREEN)
        draw.text((34, 146), "Weather data: Open-Meteo", font=FONT_SMALL, fill=MUTED)
        panels.append(image)
    return _ordered(panels, order)


def weather_placeholder(location: str, order: list[int]) -> dict[int, Image.Image]:
    frames = []
    for title, detail, accent in (
        (location or "Weather", "Apply to load current conditions", CYAN),
        ("Today", "Forecast appears on the displays", PINK),
        ("Tomorrow", "Weather data: Open-Meteo", GREEN),
    ):
        image, draw = _panel(title, accent)
        draw.text((34, 78), detail, font=FONT_BODY, fill=TEXT)
        frames.append(image)
    return _ordered(frames, order)


class WeatherDashboard:
    def __init__(self, state: dict, client=None):
        self.order = state["panel_order"]
        self.location = str(state.get("weather_location", "")).strip()
        self.units = str(state.get("weather_units", "metric"))
        self.refresh = max(60.0, min(3600.0, float(state.get("weather_refresh_seconds", 900.0))))
        self.client = client or WeatherClient()

    def frame(self) -> tuple[dict[int, Image.Image], float]:
        if not self.location:
            raise RuntimeError("weather mode needs a location")
        forecast = self.client.get(self.location, self.units)
        return render_weather(forecast, self.units, self.order), self.refresh

    def close(self):
        pass


def external_paths(directory: Path) -> tuple[Path | None, list[Path]]:
    triptych = directory / "triptych.png"
    panels = [directory / f"panel-{index}.png" for index in range(3)]
    return (triptych if triptych.is_file() else None), panels


def validate_external_directory(directory: Path) -> None:
    if not directory.is_dir():
        raise RuntimeError(f"external frame directory does not exist: {directory}")
    triptych, panels = external_paths(directory)
    if not triptych and not all(path.is_file() for path in panels):
        raise RuntimeError(
            "external frame directory needs triptych.png or panel-0.png, panel-1.png, and panel-2.png"
        )


def load_external_frames(directory: Path, order: list[int]) -> dict[int, Image.Image]:
    validate_external_directory(directory)
    triptych, panels = external_paths(directory)
    if triptych:
        with Image.open(triptych) as image:
            combined = fit_image(image, TRIPTYCH_SIZE, "stretch")
        return split_frame(combined, "span", order)
    frames = []
    for path in panels:
        with Image.open(path) as image:
            frames.append(fit_image(image, PANEL_SIZE, "stretch"))
    return _ordered(frames, order)


class ExternalFrameSource:
    """Poll PNG frames produced atomically by an independent telemetry tool."""

    def __init__(self, state: dict):
        self.directory = Path(str(state.get("external_directory", ""))).expanduser()
        self.order = state["panel_order"]
        self.refresh = max(0.2, min(60.0, float(state.get("external_refresh_seconds", 1.0))))

    def frame(self) -> tuple[dict[int, Image.Image], float]:
        return load_external_frames(self.directory, self.order), self.refresh

    def close(self):
        pass
