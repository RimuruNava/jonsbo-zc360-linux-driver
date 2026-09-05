#!/usr/bin/env python3
"""Persistent control surface for the Lucille ZC-360 renderer."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

try:
    from zc360.state import (
        normalized_framing as _normalized_framing,
        read_state as _read_state,
        state_path as _state_path,
        write_state as _write_state,
    )
except ModuleNotFoundError:
    sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
    from zc360.state import (
        normalized_framing as _normalized_framing,
        read_state as _read_state,
        state_path as _state_path,
        write_state as _write_state,
    )


def state_path() -> Path:
    return _state_path()


def read_state(path: Path) -> dict:
    return _read_state(path)


def write_state(path: Path, value: dict):
    _write_state(path, value)


def normalized_framing(value=None):
    return _normalized_framing(value)


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

    framing = commands.add_parser(
        "framing",
        help="adjust non-destructive media framing",
    )
    framing.add_argument(
        "--panel",
        choices=("left", "centre", "right", "all"),
        default="all",
    )
    framing.add_argument("--focus-x", type=float)
    framing.add_argument("--focus-y", type=float)
    framing.add_argument("--zoom", type=float)
    framing.add_argument("--cover", action="store_true")
    action = framing.add_mutually_exclusive_group()
    action.add_argument(
        "--begin",
        action="store_true",
        help="freeze the current display and begin a draft framing session",
    )
    action.add_argument(
        "--apply",
        action="store_true",
        help="commit the current draft and resume playback",
    )
    action.add_argument(
        "--cancel",
        action="store_true",
        help="discard the current draft and resume the previous framing",
    )

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

    if args.command == "framing":
        if state.get("mode") != "media":
            raise SystemExit("framing requires active media")

        editing = bool(state.get("framing_editing", False))
        panel_layout = state.get("layout") == "panels"

        def clear_draft():
            state.pop("framing_editing", None)
            state.pop("framing_draft_fit", None)
            state.pop("framing_draft", None)
            state.pop("panel_framing_draft", None)

        if args.begin:
            if editing:
                print("Framing session is already active")
                return

            state["framing_editing"] = True
            state["framing_draft_fit"] = state.get("fit", "cover")

            if panel_layout:
                values = state.get("panel_framing")
                if not isinstance(values, list) or len(values) != 3:
                    values = [normalized_framing() for _ in range(3)]
                else:
                    values = [normalized_framing(value) for value in values]
                state["panel_framing_draft"] = values
            else:
                state["framing_draft"] = normalized_framing(
                    state.get("framing")
                )

            write_state(path, state)
            print("Framing session started; display will remain frozen until apply/cancel")
            return

        if args.apply:
            if not editing:
                raise SystemExit("no framing session is active")

            state["fit"] = state.get(
                "framing_draft_fit",
                state.get("fit", "cover"),
            )

            if panel_layout:
                values = state.get("panel_framing_draft")
                if isinstance(values, list) and len(values) == 3:
                    state["panel_framing"] = [
                        normalized_framing(value)
                        for value in values
                    ]
            else:
                state["framing"] = normalized_framing(
                    state.get("framing_draft")
                )

            clear_draft()
            write_state(path, state)
            print("Framing applied; playback will resume")
            return

        if args.cancel:
            if not editing:
                raise SystemExit("no framing session is active")

            clear_draft()
            write_state(path, state)
            print("Framing cancelled; previous playback will resume")
            return

        # Framing changes are deliberately transactional.
        #
        # The physical ZC-360 panels retain their last framebuffer while the
        # renderer is paused.  Require an explicit editing session so framing
        # always follows:
        #
        #     begin -> adjust draft -> apply/cancel
        #
        # This mirrors the behaviour of the Jonsbo application and prevents
        # live crop changes from restarting ffmpeg underneath active playback.
        if not editing:
            raise SystemExit(
                "begin a framing session first: "
                "lucille_zc360_control.py framing --begin"
            )

        target_fit_key = "framing_draft_fit"

        if args.cover:
            state[target_fit_key] = "cover"

            if editing:
                if panel_layout:
                    state["panel_framing_draft"] = [
                        normalized_framing()
                        for _ in range(3)
                    ]
                else:
                    state["framing_draft"] = normalized_framing()
                write_state(path, state)
                print("Draft reset to centered cover framing")
            else:
                write_state(path, state)
                print("Returned media to centered cover framing")
            return

        state[target_fit_key] = "manual"

        if panel_layout:
            key = "panel_framing_draft" if editing else "panel_framing"
            values = state.get(key)

            if not isinstance(values, list) or len(values) != 3:
                values = [normalized_framing() for _ in range(3)]
            else:
                values = [normalized_framing(value) for value in values]

            panel_map = {
                "left": 0,
                "centre": 1,
                "right": 2,
            }
            indexes = (
                range(3)
                if args.panel == "all"
                else (panel_map[args.panel],)
            )

            for index in indexes:
                value = dict(values[index])
                if args.focus_x is not None:
                    value["focus_x"] = args.focus_x
                if args.focus_y is not None:
                    value["focus_y"] = args.focus_y
                if args.zoom is not None:
                    value["zoom"] = args.zoom
                values[index] = normalized_framing(value)

            state[key] = values
            write_state(path, state)

            label = "Draft" if editing else "Applied"
            print(f"{label} panel framing:")
            print(json.dumps(values, indent=2))
            return

        key = "framing_draft" if editing else "framing"
        value = normalized_framing(state.get(key))

        if args.focus_x is not None:
            value["focus_x"] = args.focus_x
        if args.focus_y is not None:
            value["focus_y"] = args.focus_y
        if args.zoom is not None:
            value["zoom"] = args.zoom

        state[key] = normalized_framing(value)
        write_state(path, state)

        label = "Draft" if editing else "Applied"
        print(f"{label} framing:")
        print(json.dumps(state[key], indent=2))
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
