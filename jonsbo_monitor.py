#!/usr/bin/env python3
"""
Jonsbo TF3 display monitor - example client.

Renders a system dashboard onto the 3 fan panels (via jonsbo_fan_daemon.py,
see jonsbo_fan_lib.send_fan_frames()) and, optionally, onto the pump's
640x480 display (via the separate turing-smart-screen-cli project, a
different USB device - VID 1cbe:0035 - not covered by this repo's driver).

This file only renders images and talks to the already-running fan daemon
over a Unix socket - it never claims fan-panel USB interfaces itself, so it
can be restarted freely (see README.md).

Panel index -> physical position is hardware/cable dependent. Run
examples/send_test_image.py against each index to find out which physical
panel is which on your unit, then adjust PANEL_RENDER below.
"""
import subprocess, time, psutil, glob, os, csv, shutil
from PIL import Image, ImageDraw, ImageFont, ImageFilter
from jonsbo_fan_lib import send_fan_frames, install_sigterm_handler, LOGICAL_W, LOGICAL_H

# Pump display (optional): binary name/path of turing-smart-screen-cli's
# `turing-screen` tool. Leave as default if it's on PATH, or set the env var
# to point at it. If not found, the pump-display step is skipped entirely.
TURING_SCREEN_CLI = os.environ.get("TURING_SCREEN_CLI", "turing-screen")

# Optional MangoHud integration: if a game logs FPS via MangoHud's CSV
# output into this folder, the pump display switches to a live FPS
# dashboard while the log is fresh. See README.md "Optional: MangoHud".
MANGO_FOLDER = os.environ.get("MANGOHUD_CSV_DIR", os.path.expanduser("~/mangologs"))
MANGO_MAX_AGE = 8  # seconds - older = no game currently logging, fall back to idle screen

IMG = "/tmp/jonsbo_live.png"
W, H = 640, 480
FAN_W, FAN_H = LOGICAL_W, LOGICAL_H
INTERVAL = 2


def _find_font(candidates, size):
    for path in candidates:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size=size)


# Common cross-distro sans-serif font locations; override with env vars if
# none of these exist on your system.
_BOLD_CANDIDATES = [
    os.environ.get("JONSBO_FONT_BOLD", ""),
    "/usr/share/fonts/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/liberation-sans/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
]
_REG_CANDIDATES = [
    os.environ.get("JONSBO_FONT_REGULAR", ""),
    "/usr/share/fonts/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/liberation-sans/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
]

BG = (8, 10, 16)
CARD = (18, 22, 34)
BORDER = (38, 44, 62)
COL_CPU = (0, 220, 120)
COL_GPU = (0, 160, 255)
COL_RAM = (180, 90, 255)
COL_VRAM = (255, 195, 0)
COL_DIM = (80, 85, 115)
COL_WARN = (255, 160, 0)
COL_CRIT = (255, 60, 60)
COL_FPS = (126, 34, 206)
COL_IDLE_ACCENT = (0, 160, 255)

F_VAL = _find_font(_BOLD_CANDIDATES, 52)
F_UNIT = _find_font(_BOLD_CANDIDATES, 22)
F_LBL = _find_font(_REG_CANDIDATES, 17)
F_SUB = _find_font(_REG_CANDIDATES, 17)
F_CLOCK = _find_font(_BOLD_CANDIDATES, 64)
F_FANSUB = _find_font(_REG_CANDIDATES, 20)
F_HERO = _find_font(_BOLD_CANDIDATES, 150)
F_HEROU = _find_font(_BOLD_CANDIDATES, 34)
F_FT = _find_font(_REG_CANDIDATES, 26)
F_CHIPV = _find_font(_BOLD_CANDIDATES, 34)
F_CHIPU = _find_font(_BOLD_CANDIDATES, 17)
F_IDLE_TITLE = _find_font(_BOLD_CANDIDATES, 36)
F_IDLE_CLOCK = _find_font(_BOLD_CANDIDATES, 56)


def get_gpu(query):
    """NVIDIA-only via nvidia-smi; returns 0 if unavailable (e.g. AMD/Intel -
    adapt this function for other vendors, e.g. via `rocm-smi` or sysfs)."""
    try:
        r = subprocess.run(
            ["nvidia-smi", f"--query-gpu={query}", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=2)
        return int(r.stdout.strip())
    except Exception:
        return 0


def rounded_rect(d, x, y, x2, y2, r, fill, outline=None, ow=1):
    d.rectangle([x + r, y, x2 - r, y2], fill=fill)
    d.rectangle([x, y + r, x2, y2 - r], fill=fill)
    for cx, cy in [(x, y), (x2 - 2 * r, y), (x, y2 - 2 * r), (x2 - 2 * r, y2 - 2 * r)]:
        d.ellipse([cx, cy, cx + 2 * r, cy + 2 * r], fill=fill)
    if outline:
        d.arc([x, y, x + 2 * r, y + 2 * r], 180, 270, fill=outline, width=ow)
        d.arc([x2 - 2 * r, y, x2, y + 2 * r], 270, 360, fill=outline, width=ow)
        d.arc([x, y2 - 2 * r, x + 2 * r, y2], 90, 180, fill=outline, width=ow)
        d.arc([x2 - 2 * r, y2 - 2 * r, x2, y2], 0, 90, fill=outline, width=ow)
        d.line([x + r, y, x2 - r, y], fill=outline, width=ow)
        d.line([x + r, y2, x2 - r, y2], fill=outline, width=ow)
        d.line([x, y + r, x, y2 - r], fill=outline, width=ow)
        d.line([x2, y + r, x2, y2 - r], fill=outline, width=ow)


def progress_bar(d, x, y, w, h, pct, color, r=5):
    rounded_rect(d, x, y, x + w, y + h, r, (28, 32, 48))
    fw = max(int(w * pct / 100), 2 * r if pct > 0 else 0)
    if fw > 0:
        rounded_rect(d, x, y, x + fw, y + h, r, color)
        hi = tuple(min(255, int(c * 1.5)) for c in color)
        if fw > 8:
            d.line([x + r, y + 1, x + fw - r, y + 1], fill=hi, width=1)


def stat_card(d, fx, fy, fw, fh, label, val_str, unit_str, sub_str, pct, base_col,
              f_val, f_unit, f_lbl, f_sub):
    rounded_rect(d, fx, fy, fx + fw, fy + fh, 10, CARD, BORDER, 1)
    d.text((fx + 16, fy + 10), label, fill=COL_DIM, font=f_lbl)
    col = base_col
    d.text((fx + 16, fy + 34), val_str, fill=col, font=f_val)
    vw = int(f_val.getlength(val_str))
    d.text((fx + 20 + vw, fy + 50), unit_str, fill=col, font=f_unit)
    d.text((fx + 16, fy + 106), sub_str, fill=COL_DIM, font=f_sub)
    progress_bar(d, fx + 16, fy + 136, fw - 32, 13, pct, base_col)


def chip(d, fx, fy, fw, fh, label, val_str, unit_str):
    rounded_rect(d, fx, fy, fx + fw, fy + fh, 10, CARD, BORDER, 1)
    d.text((fx + 16, fy + 12), label, fill=COL_DIM, font=F_LBL)
    d.text((fx + 16, fy + 38), val_str, fill=(230, 232, 240), font=F_CHIPV)
    vw = int(F_CHIPV.getlength(val_str))
    d.text((fx + 20 + vw, fy + 52), unit_str, fill=COL_DIM, font=F_CHIPU)


def get_metrics():
    cpu = psutil.cpu_percent(interval=0.3)
    ram_i = psutil.virtual_memory()
    ram = ram_i.percent
    try:
        t = psutil.sensors_temperatures()
        ts = t.get("k10temp") or t.get("coretemp") or []
        cpu_t = int(ts[0].current) if ts else 0
    except Exception:
        cpu_t = 0

    gpu_l = get_gpu("utilization.gpu")
    gpu_t = get_gpu("temperature.gpu")
    gpu_mv = get_gpu("memory.used")
    gpu_mt = get_gpu("memory.total")
    vram = gpu_mv * 100 // max(gpu_mt, 1)

    try:
        freq_ghz = f"{psutil.cpu_freq().current / 1000:.1f}GHz"
    except Exception:
        freq_ghz = ""

    net = psutil.net_io_counters()
    up = int(time.time() - psutil.boot_time())
    uh, um = divmod(up // 60, 60)

    return dict(cpu=cpu, cpu_t=cpu_t, freq_ghz=freq_ghz, ram=ram,
                used_mb=ram_i.used // 1024 // 1024,
                gpu_l=gpu_l, gpu_t=gpu_t, gpu_mv=gpu_mv, gpu_mt=gpu_mt, vram=vram,
                net=net, uh=uh, um=um)


def get_mango_metrics():
    """Last row of the newest MangoHud CSV in MANGO_FOLDER, or None if no
    game is currently logging (file missing or older than MANGO_MAX_AGE -
    MangoHud stops appending once the game exits)."""
    files = glob.glob(os.path.join(MANGO_FOLDER, "*.csv"))
    if not files:
        return None
    latest = max(files, key=os.path.getmtime)
    if time.time() - os.path.getmtime(latest) > MANGO_MAX_AGE:
        return None
    try:
        with open(latest, newline="") as f:
            rows = list(csv.reader(f))
    except Exception:
        return None
    if len(rows) < 4:
        return None
    header = rows[2]
    # The very last line can be truncated mid-write - fall back to the
    # second-to-last (guaranteed complete) line in that case.
    data_row = rows[-1] if len(rows[-1]) == len(header) else rows[-2]
    if len(data_row) != len(header):
        return None
    d = dict(zip(header, data_row))
    try:
        return dict(
            fps=float(d.get("fps", 0)),
            frametime=float(d.get("frametime", 0)),
            cpu_power=float(d.get("cpu_power", 0)),
            gpu_power=float(d.get("gpu_power", 0)),
            gpu_core_clock=float(d.get("gpu_core_clock", 0)),
        )
    except ValueError:
        return None


def render_pump_game(gm):
    """MangoHud-exclusive game dashboard - only values the 3 fan panels
    don't already show (CPU/RAM/GPU/VRAM/clock/net stay on those). Replaces
    the idle screen while a game is actively logging (see get_mango_metrics())."""
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    HERO_Y, HERO_H = 10, 230
    rounded_rect(d, 16, HERO_Y, W - 16, HERO_Y + HERO_H, 14, CARD, BORDER, 1)
    d.text((40, HERO_Y + 14), "FPS", fill=COL_DIM, font=F_LBL)
    fps_str = f"{gm['fps']:.0f}"
    d.text((40, HERO_Y + 34), fps_str, fill=COL_FPS, font=F_HERO)
    fw = int(F_HERO.getlength(fps_str))
    d.text((60 + fw, HERO_Y + 95), "fps", fill=COL_FPS, font=F_HEROU)
    d.text((40, HERO_Y + HERO_H - 40), f"{gm['frametime']:.1f} ms frametime",
           fill=COL_DIM, font=F_FT)

    CHIP_Y = HERO_Y + HERO_H + 16
    CHIP_H = H - CHIP_Y - 10
    GAP = 16
    CHIP_W = (W - GAP * 4) // 3
    chip(d, GAP, CHIP_Y, CHIP_W, CHIP_H, "CPU POWER", f"{gm['cpu_power']:.0f}", "W")
    chip(d, GAP * 2 + CHIP_W, CHIP_Y, CHIP_W, CHIP_H, "GPU POWER", f"{gm['gpu_power']:.0f}", "W")
    chip(d, GAP * 3 + CHIP_W * 2, CHIP_Y, CHIP_W, CHIP_H, "GPU CLOCK", f"{gm['gpu_core_clock']:.0f}", "MHz")

    img = img.rotate(180)
    img.save(IMG, "PNG")


def render_pump_idle():
    """Idle screen (clock) shown on the pump display when no game is
    logging. Swap this out for your own artwork if you like - see README.md
    for how render_pump() dispatches here."""
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    # soft radial glow, purely decorative
    glow = Image.new("RGB", (W, H), BG)
    gd = ImageDraw.Draw(glow)
    cx, cy = W // 2, H // 2 - 40
    for r in range(180, 0, -4):
        t = 1 - r / 180
        col = tuple(int(BG[i] + (COL_IDLE_ACCENT[i] - BG[i]) * t * 0.5) for i in range(3))
        gd.ellipse([cx - r, cy - r, cx + r, cy + r], fill=col)
    glow = glow.filter(ImageFilter.GaussianBlur(40))
    img = Image.composite(glow, img, Image.new("L", (W, H), 255))
    d = ImageDraw.Draw(img)

    uhr = time.strftime("%H:%M:%S")
    tw = F_IDLE_CLOCK.getlength(uhr)
    d.text(((W - tw) / 2, cy - 40), uhr, fill=(235, 236, 244), font=F_IDLE_CLOCK)

    title = "JONSBO TF3"
    ttw = F_IDLE_TITLE.getlength(title)
    d.text(((W - ttw) / 2, cy + 40), title, fill=COL_IDLE_ACCENT, font=F_IDLE_TITLE)

    return img


def render_pump():
    gm = get_mango_metrics()
    if gm:
        render_pump_game(gm)
        return

    img = render_pump_idle()
    img = img.rotate(180)
    img.save(IMG, "PNG")


def render_fan_a(m):
    """Example layout: CPU + RAM."""
    img = Image.new("RGB", (FAN_W, FAN_H), BG)
    d = ImageDraw.Draw(img)
    CARD_W, CARD_H, GAP = 305, 160, 10
    stat_card(d, GAP, GAP, CARD_W, CARD_H,
              "CPU", f"{m['cpu']:.0f}", "%",
              f"{m['cpu_t']}°C  {m['freq_ghz']}",
              m['cpu'], COL_CPU, F_VAL, F_UNIT, F_LBL, F_SUB)
    stat_card(d, GAP * 2 + CARD_W, GAP, CARD_W, CARD_H,
              "RAM", f"{m['ram']:.0f}", "%",
              f"{m['used_mb']} MB",
              m['ram'], COL_RAM, F_VAL, F_UNIT, F_LBL, F_SUB)
    return img


def render_fan_b(m):
    """Example layout: GPU + VRAM."""
    img = Image.new("RGB", (FAN_W, FAN_H), BG)
    d = ImageDraw.Draw(img)
    CARD_W, CARD_H, GAP = 305, 160, 10
    stat_card(d, GAP, GAP, CARD_W, CARD_H,
              "GPU", f"{m['gpu_l']}", "%",
              f"{m['gpu_t']}°C",
              m['gpu_l'], COL_GPU, F_VAL, F_UNIT, F_LBL, F_SUB)
    stat_card(d, GAP * 2 + CARD_W, GAP, CARD_W, CARD_H,
              "VRAM", f"{m['vram']}", "%",
              f"{m['gpu_mv']} / {m['gpu_mt']} MB",
              m['vram'], COL_VRAM, F_VAL, F_UNIT, F_LBL, F_SUB)
    return img


def render_fan_c(m):
    """Example layout: clock + uptime + network throughput."""
    img = Image.new("RGB", (FAN_W, FAN_H), BG)
    d = ImageDraw.Draw(img)
    rounded_rect(d, 10, 10, FAN_W - 10, FAN_H - 10, 12, CARD, BORDER, 1)

    uhr = time.strftime("%H:%M:%S")
    tw = F_CLOCK.getlength(uhr)
    d.text(((FAN_W - tw) / 2, 24), uhr, fill=COL_FPS, font=F_CLOCK)

    sub = (f"UP {m['uh']}h {m['um']:02d}m    "
           f"↑{m['net'].bytes_sent // 1024 // 1024}  ↓{m['net'].bytes_recv // 1024 // 1024} MB")
    sw = F_FANSUB.getlength(sub)
    d.text(((FAN_W - sw) / 2, 116), sub, fill=COL_DIM, font=F_FANSUB)
    return img


def main():
    print(f"Jonsbo Monitor  pump {W}x{H} + 3 fan panels (via jonsbo_fan_daemon)  - Ctrl+C to stop")
    psutil.cpu_percent()
    time.sleep(0.5)

    turing_screen_bin = shutil.which(TURING_SCREEN_CLI)
    if not turing_screen_bin:
        print(f"'{TURING_SCREEN_CLI}' not found on PATH - pump display step will be skipped. "
              f"Install turing-smart-screen-cli or set TURING_SCREEN_CLI to enable it.")

    shutdown_requested = install_sigterm_handler()

    # find_panels() order depends on your hardware - verify with
    # examples/send_test_image.py and adjust this mapping.
    PANEL_RENDER = {0: render_fan_a, 1: render_fan_b, 2: render_fan_c}

    while not shutdown_requested.is_set():
        try:
            m = get_metrics()

            render_pump()
            if turing_screen_bin:
                r = subprocess.run(
                    [turing_screen_bin, "send-image", "--path", IMG],
                    capture_output=True, text=True)
                if "Resource busy" in r.stderr:
                    print("USB busy - retry in 1s")
                    shutdown_requested.wait(1)

            try:
                send_fan_frames({idx: fn(m) for idx, fn in PANEL_RENDER.items()})
            except Exception as e:
                print(f"Fan daemon unreachable: {e}")
        except KeyboardInterrupt:
            print("\nStopped.")
            break
        except Exception as e:
            print(f"Error: {e}")
        if shutdown_requested.is_set():
            break
        shutdown_requested.wait(INTERVAL)

    print("Stopped.")


if __name__ == "__main__":
    main()
