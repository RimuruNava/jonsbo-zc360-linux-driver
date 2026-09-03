#!/usr/bin/env python3
"""Send three labelled test frames through the long-lived USB owner."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from PIL import Image, ImageDraw

from jonsbo_fan_lib import LOGICAL_H, LOGICAL_W, send_fan_frames


ACCENTS = ((255, 96, 180), (94, 226, 255), (255, 210, 84))


def test_frame(index):
    img = Image.new("RGB", (LOGICAL_W, LOGICAL_H), (11, 14, 20))
    draw = ImageDraw.Draw(img)
    accent = ACCENTS[index]
    draw.rectangle((0, 0, LOGICAL_W - 1, LOGICAL_H - 1), outline=accent, width=12)
    draw.rectangle((34, 34, 48, LOGICAL_H - 35), fill=accent)
    draw.text((78, 50), f"PANEL {index}", fill=(240, 242, 246))
    draw.text((78, 92), "SOCKET PATH OK", fill=accent)
    draw.text((78, 132), "640 x 180 LANDSCAPE", fill=(145, 154, 168))
    return img


def main():
    # A stale set of panels receives a 30-frame interleaved warmup. That can
    # legitimately take longer than a conventional local-socket timeout.
    send_fan_frames(
        {index: test_frame(index) for index in range(3)},
        timeout=120.0,
    )
    print("Sent three test frames through the daemon; this client never claimed USB.")


if __name__ == "__main__":
    main()
