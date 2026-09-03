#!/usr/bin/env python3
"""Persistent control surface for the Lucille ZC-360 renderer."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
from pathlib import Path


def state_path() -> Path:
    configured = os.environ.get("LUCILLE_ZC360_STATE")
    if configured:
        return Path(configured).expanduser()
    root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return root / "lucille-shell" / "zc360-display.json"


def read_state(path: Path) -> dict:
    try:
        with path.open() as stream:
            value = json.load(stream)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def write_state(path: Path, value: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    play = commands.add_parser("play", help="loop an image, GIF, or video")
    play.add_argument("source", type=Path)
    play.add_argument("--layout", choices=("span", "mirror"), default="span")
    play.add_argument("--fit", choices=("cover", "contain", "stretch"), default="cover")
    play.add_argument("--fps", type=float, default=8.0)

    panels = commands.add_parser("play-panels", help="loop separate left, centre, and right media")
    panels.add_argument("sources", nargs=3, type=Path, metavar=("LEFT", "CENTRE", "RIGHT"))
    panels.add_argument("--fit", choices=("cover", "contain", "stretch"), default="cover")
    panels.add_argument("--fps", type=float, default=8.0)

    commands.add_parser("telemetry", help="show the diagnostic triptych")

    overlay = commands.add_parser("overlay", help="show a temporary shell interruption")
    overlay.add_argument("title")
    overlay.add_argument("--detail", default="")
    overlay.add_argument("--ttl", type=float, default=2.4)
    overlay.add_argument("--accent", choices=("pink", "yellow", "pale"), default="pink")

    commands.add_parser("clear-overlay", help="dismiss the current interruption")
    commands.add_parser("status", help="print persistent display state")
    return parser.parse_args()


def main():
    args = parse_args()
    path = state_path()
    state = read_state(path)

    if args.command == "status":
        print(json.dumps(state, indent=2, sort_keys=True))
        print(f"State file: {path}")
        return

    if args.command == "play":
        source = args.source.expanduser().resolve()
        if not source.is_file():
            raise SystemExit(f"media source does not exist: {source}")
        state.update({
            "mode": "media",
            "source": str(source),
            "layout": args.layout,
            "fit": args.fit,
            "frames_per_second": max(0.5, min(20.0, args.fps)),
        })
        state.pop("sources", None)
        write_state(path, state)
        print(f"Looping {source} as {args.layout}/{args.fit}")
        return

    if args.command == "play-panels":
        sources = [source.expanduser().resolve() for source in args.sources]
        missing = [source for source in sources if not source.is_file()]
        if missing:
            raise SystemExit(f"media source does not exist: {missing[0]}")
        state.update({
            "mode": "media",
            "sources": [str(source) for source in sources],
            "layout": "panels",
            "fit": args.fit,
            "frames_per_second": max(0.5, min(20.0, args.fps)),
        })
        state.pop("source", None)
        write_state(path, state)
        print("Looping three panel sources in left / centre / right order")
        return

    if args.command == "telemetry":
        state["mode"] = "telemetry"
        write_state(path, state)
        print("Diagnostic telemetry selected")
        return

    if args.command == "overlay":
        state["overlay"] = {
            "title": args.title,
            "detail": args.detail,
            "accent": args.accent,
            "expires_at": time.time() + max(0.2, args.ttl),
        }
        write_state(path, state)
        print(f"Overlay queued for {max(0.2, args.ttl):.1f}s")
        return

    if args.command == "clear-overlay":
        state.pop("overlay", None)
        write_state(path, state)
        print("Overlay cleared")


if __name__ == "__main__":
    main()
