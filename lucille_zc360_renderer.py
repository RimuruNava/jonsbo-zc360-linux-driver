#!/usr/bin/env python3
"""Lucille Shell telemetry triptych for three Jonsbo ZC-360 panels.

This process is a socket client. It never enumerates, claims, releases, or
writes to a USB device directly, so it is safe to restart while the long-lived
jonsbo_fan_daemon.py process owns the panels.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

try:
    import psutil
except ImportError:  # Preview mode remains useful before the venv is built.
    psutil = None

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "zc360-layout.json"
LOGICAL_W, LOGICAL_H = 640, 180

BG = (7, 9, 13)
BG_LIFT = (11, 14, 20)
STRUCTURE = (49, 56, 67)
STRUCTURE_SOFT = (28, 34, 43)
PALE = (225, 227, 221)
MUTED = (133, 143, 153)
PINK = (255, 91, 177)
YELLOW = (244, 205, 72)


def _font(size: int, bold: bool = False):
    env_name = "JONSBO_FONT_BOLD" if bold else "JONSBO_FONT_REGULAR"
    candidates = [
        os.environ.get(env_name, ""),
        "/usr/share/fonts/jetbrains-mono/JetBrainsMono-Bold.ttf" if bold else
        "/usr/share/fonts/jetbrains-mono/JetBrainsMono-Regular.ttf",
        "/usr/share/fonts/TTF/JetBrainsMono-Bold.ttf" if bold else
        "/usr/share/fonts/TTF/JetBrainsMono-Regular.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf" if bold else
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "/usr/share/fonts/TTF/DejaVuSansMono-Bold.ttf" if bold else
        "/usr/share/fonts/TTF/DejaVuSansMono.ttf",
    ]
    for candidate in candidates:
        if candidate and os.path.exists(candidate):
            return ImageFont.truetype(candidate, size)
    return ImageFont.load_default(size=size)


F_MICRO = _font(9)
F_SMALL = _font(12)
F_SMALL_B = _font(12, True)
F_LABEL = _font(15, True)
F_VALUE = _font(54, True)
F_VALUE_COMPACT = _font(42, True)
F_UNIT = _font(17, True)
F_CLOCK = _font(46, True)


def _read(path: str | Path) -> str | None:
    try:
        return Path(path).read_text().strip()
    except (OSError, ValueError):
        return None


def _number(path: str | Path, scale: float = 1.0) -> float | None:
    raw = _read(path)
    try:
        return float(raw) / scale if raw is not None else None
    except ValueError:
        return None


def _clamp(value: float | None, low: float = 0.0, high: float = 100.0) -> float:
    if value is None:
        return 0.0
    return max(low, min(high, value))


def _fmt(value: float | None, digits: int = 0, missing: str = "--") -> str:
    if value is None:
        return missing
    return f"{value:.{digits}f}"


def _bytes(value: float | None) -> str:
    if value is None:
        return "--"
    units = ("B", "K", "M", "G", "T")
    amount = float(value)
    for unit in units:
        if abs(amount) < 1024 or unit == units[-1]:
            return f"{amount:.0f}{unit}" if amount >= 10 else f"{amount:.1f}{unit}"
        amount /= 1024
    return "--"


@dataclass
class Metrics:
    cpu_load: float | None = None
    cpu_temp: float | None = None
    cpu_freq_ghz: float | None = None
    gpu_load: float | None = None
    gpu_temp: float | None = None
    gpu_vram_used: float | None = None
    gpu_vram_total: float | None = None
    gpu_power_w: float | None = None
    ram_percent: float | None = None
    ram_used: float | None = None
    ram_total: float | None = None
    disk_percent: float | None = None
    net_up_bps: float | None = None
    net_down_bps: float | None = None
    uptime_seconds: float | None = None
    sampled_at: float = field(default_factory=time.time)


class MetricCollector:
    def __init__(self, gpu_device: str = "auto"):
        self._last_net = None
        self._last_net_time = None
        self._gpu_device = self._find_amd_gpu(gpu_device)

    @staticmethod
    def _find_amd_gpu(preferred: str = "auto") -> Path | None:
        if preferred != "auto":
            candidate = Path(preferred).expanduser()
            return candidate if candidate.exists() else None
        candidates = []
        for candidate in sorted(glob.glob("/sys/class/drm/card[0-9]*/device")):
            device = Path(candidate)
            if _read(device / "vendor") == "0x1002":
                # A Ryzen desktop can expose both its small integrated GPU and
                # the discrete Radeon. Prefer the AMD device with the largest
                # visible VRAM aperture instead of trusting DRM card order.
                candidates.append((_number(device / "mem_info_vram_total") or 0, device))
        return max(candidates, default=(0, None), key=lambda item: item[0])[1]

    @staticmethod
    def _temperature(names: tuple[str, ...]) -> float | None:
        if psutil is not None:
            try:
                sensors = psutil.sensors_temperatures()
                for name in names:
                    readings = sensors.get(name, [])
                    if readings:
                        preferred = next(
                            (x for x in readings if "tctl" in x.label.lower()),
                            readings[0],
                        )
                        return float(preferred.current)
            except (AttributeError, OSError):
                pass
        for hwmon in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
            if (_read(Path(hwmon) / "name") or "").lower() not in names:
                continue
            value = _number(Path(hwmon) / "temp1_input", 1000.0)
            if value is not None:
                return value
        return None

    def _gpu_metrics(self):
        device = self._gpu_device
        if device is None:
            return (None, None, None, None, None)
        load = _number(device / "gpu_busy_percent")
        used = _number(device / "mem_info_vram_used")
        total = _number(device / "mem_info_vram_total")
        temp = None
        power = None
        for hwmon in sorted((device / "hwmon").glob("hwmon*")):
            temp = temp if temp is not None else _number(hwmon / "temp1_input", 1000.0)
            power = power if power is not None else _number(hwmon / "power1_average", 1_000_000.0)
        return (load, temp, used, total, power)

    def collect(self) -> Metrics:
        now = time.time()
        m = Metrics(sampled_at=now)
        if psutil is not None:
            m.cpu_load = float(psutil.cpu_percent(interval=None))
            freq = psutil.cpu_freq()
            m.cpu_freq_ghz = freq.current / 1000.0 if freq else None
            memory = psutil.virtual_memory()
            m.ram_percent = float(memory.percent)
            m.ram_used, m.ram_total = float(memory.used), float(memory.total)
            disk = psutil.disk_usage("/")
            m.disk_percent = float(disk.percent)
            m.uptime_seconds = now - psutil.boot_time()
            net = psutil.net_io_counters()
            if self._last_net is not None and self._last_net_time is not None:
                elapsed = max(0.001, now - self._last_net_time)
                m.net_up_bps = max(0.0, (net.bytes_sent - self._last_net.bytes_sent) / elapsed)
                m.net_down_bps = max(0.0, (net.bytes_recv - self._last_net.bytes_recv) / elapsed)
            self._last_net, self._last_net_time = net, now
        m.cpu_temp = self._temperature(("k10temp", "zenpower", "coretemp"))
        (
            m.gpu_load,
            m.gpu_temp,
            m.gpu_vram_used,
            m.gpu_vram_total,
            m.gpu_power_w,
        ) = self._gpu_metrics()
        return m


class History:
    def __init__(self, length: int = 47):
        self.cpu = deque(maxlen=length)
        self.gpu = deque(maxlen=length)
        self.ram = deque(maxlen=length)

    @staticmethod
    def _append(series: deque, value: float):
        # Seed a new history with the first real sample. Starting with zeros
        # created a deceptive square step for roughly 94 seconds after every
        # renderer restart.
        if not series:
            series.extend([value] * series.maxlen)
        else:
            series.append(value)

    def push(self, m: Metrics):
        self._append(self.cpu, _clamp(m.cpu_load))
        self._append(self.gpu, _clamp(m.gpu_load))
        self._append(self.ram, _clamp(m.ram_percent))


def _demo_metrics() -> Metrics:
    return Metrics(
        cpu_load=27.0,
        cpu_temp=51.0,
        cpu_freq_ghz=4.63,
        gpu_load=42.0,
        gpu_temp=58.0,
        gpu_vram_used=8.3 * 1024**3,
        gpu_vram_total=16.0 * 1024**3,
        gpu_power_w=184.0,
        ram_percent=38.0,
        ram_used=12.2 * 1024**3,
        ram_total=32.0 * 1024**3,
        disk_percent=61.0,
        net_up_bps=256 * 1024,
        net_down_bps=8.4 * 1024**2,
        uptime_seconds=13 * 3600 + 27 * 60,
    )


def _base(panel_number: int, name: str, state: str = "NOMINAL"):
    img = Image.new("RGB", (LOGICAL_W, LOGICAL_H), BG)
    d = ImageDraw.Draw(img)

    # Structural registration only: no container cards and no button faces.
    d.rectangle((0, 0, LOGICAL_W - 1, LOGICAL_H - 1), outline=BG_LIFT)
    d.line((0, 14, 184, 14), fill=STRUCTURE, width=1)
    d.line((216, 14, LOGICAL_W, 14), fill=STRUCTURE_SOFT, width=1)
    d.rectangle((198, 12, 202, 16), fill=PALE)
    d.rectangle((18, 28, 22, 151), fill=PINK)
    d.line((22, 28, 34, 28), fill=PINK, width=1)
    d.line((22, 151, 34, 151), fill=PINK, width=1)
    d.text((42, 24), f"0{panel_number} // {name}", font=F_LABEL, fill=PALE)
    d.text((540, 25), state, font=F_MICRO, fill=MUTED)

    # One datum traverses the triptych; the gaps are the physical bezels.
    d.line((0, 160, LOGICAL_W, 160), fill=STRUCTURE, width=1)
    for x in range(42, 620, 64):
        d.line((x, 157, x, 163), fill=STRUCTURE, width=1)
    d.rectangle((606, 157, 612, 163), fill=YELLOW)
    return img, d


def _hero(d, value: float | None, unit: str, x: int = 42, y: int = 49):
    text = _fmt(value)
    d.text((x, y), text, font=F_VALUE, fill=PALE, stroke_width=0)
    width = d.textlength(text, font=F_VALUE)
    d.text((x + width + 7, y + 31), unit, font=F_UNIT, fill=MUTED)


def _pair(d, x: int, y: int, label: str, value: str, unit: str = ""):
    d.text((x, y), label, font=F_MICRO, fill=MUTED)
    d.text((x, y + 15), value, font=F_SMALL_B, fill=PALE)
    if unit:
        width = d.textlength(value, font=F_SMALL_B)
        d.text((x + width + 5, y + 17), unit, font=F_MICRO, fill=MUTED)


def _trace(d, values, x0: int = 42, y0: int = 132, width: int = 568, height: int = 27):
    values = list(values)
    if len(values) < 2:
        return
    step = width / (len(values) - 1)
    points = [
        (int(x0 + i * step), int(y0 + height - _clamp(v) / 100.0 * height))
        for i, v in enumerate(values)
    ]
    d.line((x0, y0 + height, x0 + width, y0 + height), fill=STRUCTURE_SOFT, width=1)
    d.line(points, fill=PALE, width=1)
    x, y = points[-1]
    d.rectangle((x - 2, y - 2, x + 2, y + 2), fill=YELLOW)


def render_processor(m: Metrics, h: History, panel_number: int = 1):
    state = "THERMAL" if (m.cpu_temp or 0) >= 80 else "NOMINAL"
    img, d = _base(panel_number, "PROCESSOR", state)
    _hero(d, m.cpu_load, "%")
    _pair(d, 267, 56, "PACKAGE", _fmt(m.cpu_temp), "C")
    _pair(d, 385, 56, "CLOCK", _fmt(m.cpu_freq_ghz, 2), "GHz")
    _pair(d, 523, 56, "HEADROOM", _fmt(None if m.cpu_temp is None else 89 - m.cpu_temp), "C")
    d.text((42, 111), "LOAD HISTORY // 94 SEC", font=F_MICRO, fill=MUTED)
    _trace(d, h.cpu)
    return img


def render_graphics(m: Metrics, h: History, panel_number: int = 2):
    state = "THERMAL" if (m.gpu_temp or 0) >= 85 else "NOMINAL"
    img, d = _base(panel_number, "GRAPHICS", state)
    _hero(d, m.gpu_load, "%")
    vram_pct = None
    if m.gpu_vram_used is not None and m.gpu_vram_total:
        vram_pct = 100.0 * m.gpu_vram_used / m.gpu_vram_total
    _pair(d, 267, 56, "EDGE", _fmt(m.gpu_temp), "C")
    _pair(d, 385, 56, "VRAM", _fmt(vram_pct), "%")
    _pair(d, 500, 56, "BOARD", _fmt(m.gpu_power_w), "W")
    d.text((42, 111), "ENGINE HISTORY // 94 SEC", font=F_MICRO, fill=MUTED)
    _trace(d, h.gpu)
    return img


def _uptime(seconds: float | None) -> str:
    if seconds is None:
        return "--"
    minutes = int(seconds // 60)
    days, minutes = divmod(minutes, 1440)
    hours, minutes = divmod(minutes, 60)
    return f"{days}D {hours:02d}:{minutes:02d}" if days else f"{hours:02d}:{minutes:02d}"


def render_system(m: Metrics, h: History, panel_number: int = 3):
    img, d = _base(panel_number, "SYSTEM", "LINKED")
    clock = time.strftime("%H:%M")
    d.text((42, 52), clock, font=F_CLOCK, fill=PALE)
    d.text((45, 103), time.strftime("%a  %d %b").upper(), font=F_SMALL, fill=MUTED)
    _pair(d, 274, 51, "MEMORY", _fmt(m.ram_percent), "%")
    _pair(d, 389, 51, "ROOT", _fmt(m.disk_percent), "%")
    _pair(d, 493, 51, "UPTIME", _uptime(m.uptime_seconds))
    _pair(d, 274, 94, "RECEIVE", _bytes(m.net_down_bps), "/s")
    _pair(d, 389, 94, "TRANSMIT", _bytes(m.net_up_bps), "/s")
    _pair(d, 493, 94, "RAM USED", _bytes(m.ram_used))
    # System occupancy trace is deliberately quieter than component load.
    _trace(d, h.ram, x0=274, y0=132, width=336, height=27)
    return img


RENDERERS: dict[str, Callable[[Metrics, History, int], Image.Image]] = {
    "processor": render_processor,
    "graphics": render_graphics,
    "system": render_system,
}


def load_config(path: Path):
    with path.open() as stream:
        config = json.load(stream)
    panels = config.get("panels", [])
    indices = [entry.get("index") for entry in panels]
    roles = [entry.get("role") for entry in panels]
    if len(panels) != 3 or len(set(indices)) != 3:
        raise ValueError("layout must define exactly three unique panel indices")
    unknown = [role for role in roles if role not in RENDERERS]
    if unknown:
        raise ValueError(f"unknown panel role(s): {', '.join(map(str, unknown))}")
    return config


def render_triptych(config, metrics: Metrics, history: History):
    images = {}
    for sequence, panel in enumerate(config["panels"], start=1):
        images[int(panel["index"])] = RENDERERS[panel["role"]](metrics, history, sequence)
    return images


def save_previews(images, directory: Path):
    directory.mkdir(parents=True, exist_ok=True)
    for index, image in sorted(images.items()):
        image.save(directory / f"panel-{index}.png")
    strip = Image.new("RGB", (LOGICAL_W * 3 + 32, LOGICAL_H), (2, 3, 5))
    for column, (_, image) in enumerate(sorted(images.items())):
        strip.paste(image, (column * (LOGICAL_W + 16), 0))
    strip.save(directory / "triptych.png")


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--preview-dir", type=Path, help="write PNG previews; never contact the daemon")
    parser.add_argument("--once", action="store_true", help="send one triptych and exit")
    return parser.parse_args()


def main():
    args = parse_args()
    config = load_config(args.config)
    history = History()

    if args.preview_dir:
        metrics = _demo_metrics()
        for i in range(history.cpu.maxlen):
            history.cpu.append(17 + ((i * 19) % 43))
            history.gpu.append(11 + ((i * 29) % 67))
            history.ram.append(34 + ((i * 7) % 9))
        save_previews(render_triptych(config, metrics, history), args.preview_dir)
        print(f"Preview written to {args.preview_dir}; no socket or USB access occurred.")
        return

    if psutil is None:
        raise SystemExit("psutil is required for live telemetry; install requirements.txt in the project venv")

    # Delay the USB-owning library import until live mode. Preview generation
    # has no PyUSB dependency and cannot touch the socket or hardware.
    from jonsbo_fan_lib import send_fan_frames

    collector = MetricCollector(str(config.get("gpu_device", "auto")))
    psutil.cpu_percent(interval=None)
    refresh = max(0.5, float(config.get("refresh_seconds", 2.0)))
    timeout = max(5.0, float(config.get("socket_timeout_seconds", 120.0)))
    print("Lucille ZC-360 telemetry renderer - socket client only, Ctrl+C to stop")
    print(f"Layout: {args.config}")
    while True:
        started = time.monotonic()
        try:
            metrics = collector.collect()
            history.push(metrics)
            images = render_triptych(config, metrics, history)
            send_fan_frames(images, timeout=timeout)
            if args.once:
                return
        except KeyboardInterrupt:
            print("\nRenderer stopped; USB owner remains running.")
            return
        except Exception as exc:
            print(f"Renderer waiting for daemon: {exc}")
            if args.once:
                raise
        time.sleep(max(0.0, refresh - (time.monotonic() - started)))


if __name__ == "__main__":
    main()
