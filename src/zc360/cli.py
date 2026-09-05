"""Socket-only command line client for the ZC-360 driver daemon."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import stat
import sys
from pathlib import Path

from PIL import Image, ImageDraw

from . import __version__
from .constants import LOGICAL_HEIGHT, LOGICAL_WIDTH, PANEL_COUNT, socket_path
from .ipc import IPCError, daemon_status, send_fan_frames
from .media import fit_image
from .state import normalized_framing, read_state, state_path, update_state

ACCENTS = ((255, 96, 180), (94, 226, 255), (255, 210, 84))


def panel_indices(value: str) -> list[int]:
    if value == "all":
        return list(range(PANEL_COUNT))
    index = int(value)
    if not 0 <= index < PANEL_COUNT:
        raise argparse.ArgumentTypeError(f"panel must be all or 0..{PANEL_COUNT - 1}")
    return [index]


def test_pattern(index: int) -> Image.Image:
    image = Image.new("RGB", (LOGICAL_WIDTH, LOGICAL_HEIGHT), (11, 14, 20))
    draw = ImageDraw.Draw(image)
    accent = ACCENTS[index]
    draw.rectangle((0, 0, LOGICAL_WIDTH - 1, LOGICAL_HEIGHT - 1), outline=accent, width=12)
    draw.rectangle((34, 34, 48, LOGICAL_HEIGHT - 35), fill=accent)
    draw.text((78, 50), f"PANEL {index}", fill=(240, 242, 246))
    draw.text((78, 92), "ZC360CTL TEST", fill=accent)
    draw.text((78, 132), "SOCKET CLIENT / NO USB CLAIM", fill=(145, 154, 168))
    return image


def print_status(data: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(data, indent=2, sort_keys=True))
        return
    print(f"Driver: {data.get('driver', 'unknown')}")
    print(f"IPC: v{data.get('ipc_version', '?')}")
    if data.get("driver_version"):
        print(f"Version: {data['driver_version']}")
    if data.get("uptime_seconds") is not None:
        print(f"Uptime: {data['uptime_seconds']:.1f}s")
    for panel in data.get("panels", []):
        print(
            f"Panel {panel['index']}: {panel['serial']} "
            f"({'ready' if panel.get('warmed') else 'not warmed'})"
        )
    if data.get("last_transfer"):
        last = data["last_transfer"]
        print(
            f"Last transfer: {last.get('mode')} / {last.get('packet_ms')}ms / "
            f"status={last.get('statuses')}"
        )
    if data.get("message"):
        print(data["message"])


def command_doctor(as_json: bool) -> int:
    path = socket_path()
    checks = {
        "python": sys.version.split()[0],
        "dependencies": {
            name: importlib.util.find_spec(module) is not None
            for name, module in {
                "libusb1": "usb1",
                "Pillow": "PIL",
                "pycryptodome": "Crypto",
            }.items()
        },
        "socket": {
            "path": str(path),
            "exists": path.exists(),
            "owner_only": False,
        },
        "daemon": None,
        "errors": [],
    }
    if path.exists():
        checks["socket"]["owner_only"] = stat.S_IMODE(path.stat().st_mode) == 0o600
    missing = [name for name, present in checks["dependencies"].items() if not present]
    if missing:
        checks["errors"].append(f"missing dependencies: {', '.join(missing)}")
    try:
        checks["daemon"] = daemon_status()
    except Exception as exc:
        checks["errors"].append(f"daemon unavailable: {exc}")
    if as_json:
        print(json.dumps(checks, indent=2, sort_keys=True))
    else:
        print(f"Python: {checks['python']}")
        for name, present in checks["dependencies"].items():
            print(f"Dependency {name}: {'ok' if present else 'missing'}")
        print(f"Socket: {path} ({'present' if path.exists() else 'missing'})")
        if checks["daemon"]:
            print_status(checks["daemon"], False)
        for error in checks["errors"]:
            print(f"ERROR: {error}")
    return 1 if checks["errors"] else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=f"zc360ctl {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    status = commands.add_parser("status", help="show daemon and panel state")
    status.add_argument("--json", action="store_true")

    doctor = commands.add_parser("doctor", help="run non-destructive installation checks")
    doctor.add_argument("--json", action="store_true")

    display_status = commands.add_parser("display-status", help="show renderer configuration")
    display_status.add_argument("--json", action="store_true")

    play = commands.add_parser("play", help="loop an image, GIF, or video")
    play.add_argument("source", type=Path)
    play.add_argument("--layout", choices=("span", "mirror"), default="span")
    play.add_argument("--fit", choices=("cover", "contain", "stretch", "manual"), default="cover")
    play.add_argument("--fps", type=float, default=8.0)

    panels = commands.add_parser("play-panels", help="loop one source per physical position")
    panels.add_argument("sources", nargs=3, type=Path, metavar=("LEFT", "CENTRE", "RIGHT"))
    panels.add_argument("--fit", choices=("cover", "contain", "stretch", "manual"), default="cover")
    panels.add_argument("--fps", type=float, default=8.0)

    commands.add_parser("pause", help="freeze the panels on their current frame")
    commands.add_parser("resume", help="resume configured media")
    commands.add_parser("idle", help="stop rendering and retain the current framebuffer")

    framing = commands.add_parser("framing", help="edit non-destructive crop and zoom")
    framing.add_argument("--panel", choices=("0", "1", "2", "all"), default="all")
    framing.add_argument("--focus-x", type=float)
    framing.add_argument("--focus-y", type=float)
    framing.add_argument("--zoom", type=float)
    action = framing.add_mutually_exclusive_group()
    action.add_argument("--begin", action="store_true")
    action.add_argument("--apply", action="store_true")
    action.add_argument("--cancel", action="store_true")

    test = commands.add_parser("test-pattern", help="send labelled frames through the daemon")
    test.add_argument("--panel", default="all", choices=("all", "0", "1", "2"))

    blank = commands.add_parser("blank", help="send a black frame through the daemon")
    blank.add_argument("--panel", default="all", choices=("all", "0", "1", "2"))

    image = commands.add_parser("image", help="send a still image through the daemon")
    image.add_argument("source", type=Path)
    image.add_argument("--panel", default="all", choices=("all", "0", "1", "2"))
    image.add_argument("--fit", default="cover", choices=("cover", "contain", "stretch"))
    return parser


def command_play(args: argparse.Namespace) -> int:
    source = args.source.expanduser().resolve()
    if not source.is_file():
        raise IPCError(f"media source does not exist: {source}")

    def mutate(state: dict) -> None:
        state.update({
            "mode": "media",
            "source": str(source),
            "layout": args.layout,
            "fit": args.fit,
            "frames_per_second": args.fps,
            "paused": False,
        })
        state.pop("sources", None)
        for key in ("framing_editing", "framing_draft_fit", "framing_draft", "panel_framing_draft"):
            state.pop(key, None)

    state = update_state(mutate)
    print(f"Playing {source} as {state['layout']}/{state['fit']} at {state['frames_per_second']:g} FPS")
    return 0


def command_play_panels(args: argparse.Namespace) -> int:
    sources = [source.expanduser().resolve() for source in args.sources]
    missing = next((source for source in sources if not source.is_file()), None)
    if missing:
        raise IPCError(f"media source does not exist: {missing}")

    def mutate(state: dict) -> None:
        state.update({
            "mode": "media",
            "sources": [str(source) for source in sources],
            "layout": "panels",
            "fit": args.fit,
            "frames_per_second": args.fps,
            "paused": False,
        })
        state.pop("source", None)
        for key in ("framing_editing", "framing_draft_fit", "framing_draft", "panel_framing_draft"):
            state.pop(key, None)

    state = update_state(mutate)
    print(f"Playing three panel sources as {state['fit']} at {state['frames_per_second']:g} FPS")
    return 0


def command_framing(args: argparse.Namespace) -> int:
    result: dict[str, str] = {}

    def mutate(state: dict) -> None:
        editing = bool(state.get("framing_editing"))
        if args.begin:
            if editing:
                raise ValueError("a framing session is already active")
            state["framing_editing"] = True
            state["framing_draft_fit"] = state["fit"]
            state["framing_draft"] = dict(state["framing"])
            state["panel_framing_draft"] = [dict(item) for item in state["panel_framing"]]
            result["message"] = "Framing session started; the physical display is frozen"
            return
        if args.apply:
            if not editing:
                raise ValueError("no framing session is active")
            state["fit"] = state.get("framing_draft_fit", state["fit"])
            state["framing"] = normalized_framing(state.get("framing_draft"))
            drafts = state.get("panel_framing_draft")
            if isinstance(drafts, list) and len(drafts) == 3:
                state["panel_framing"] = [normalized_framing(item) for item in drafts]
            for key in ("framing_editing", "framing_draft_fit", "framing_draft", "panel_framing_draft"):
                state.pop(key, None)
            result["message"] = "Framing applied; playback resumed"
            return
        if args.cancel:
            if not editing:
                raise ValueError("no framing session is active")
            for key in ("framing_editing", "framing_draft_fit", "framing_draft", "panel_framing_draft"):
                state.pop(key, None)
            result["message"] = "Framing cancelled; playback resumed unchanged"
            return
        if not editing:
            raise ValueError("begin a framing session first with: zc360ctl framing --begin")
        state["framing_draft_fit"] = "manual"
        if state["layout"] == "panels":
            values = state.get("panel_framing_draft")
            if not isinstance(values, list) or len(values) != 3:
                values = [dict(state["panel_framing"][index]) for index in range(3)]
            indexes = range(3) if args.panel == "all" else (int(args.panel),)
            for index in indexes:
                value = dict(values[index])
                for source_name, key in (("focus_x", "focus_x"), ("focus_y", "focus_y"), ("zoom", "zoom")):
                    supplied = getattr(args, source_name)
                    if supplied is not None:
                        value[key] = supplied
                values[index] = normalized_framing(value)
            state["panel_framing_draft"] = values
        else:
            value = dict(state.get("framing_draft", state["framing"]))
            if args.focus_x is not None:
                value["focus_x"] = args.focus_x
            if args.focus_y is not None:
                value["focus_y"] = args.focus_y
            if args.zoom is not None:
                value["zoom"] = args.zoom
            state["framing_draft"] = normalized_framing(value)
        result["message"] = "Draft framing updated; use --apply or --cancel"

    update_state(mutate)
    print(result["message"])
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "status":
            print_status(daemon_status(), args.json)
            return 0
        if args.command == "doctor":
            return command_doctor(args.json)
        if args.command == "display-status":
            state = read_state()
            if args.json:
                print(json.dumps(state, indent=2, sort_keys=True))
            else:
                print(f"State: {state_path()}")
                print(f"Mode: {state['mode']}{' (paused)' if state['paused'] else ''}")
                print(f"Layout: {state['layout']} / {state['fit']} / {state['frames_per_second']:g} FPS")
                print(f"Panel order: {state['panel_order']}")
            return 0
        if args.command == "play":
            return command_play(args)
        if args.command == "play-panels":
            return command_play_panels(args)
        if args.command == "framing":
            return command_framing(args)
        if args.command in {"pause", "resume", "idle"}:
            def mutate(state: dict) -> None:
                if args.command == "pause":
                    state["paused"] = True
                elif args.command == "resume":
                    state["paused"] = False
                    for key in (
                        "identify_until", "framing_editing", "framing_draft_fit",
                        "framing_draft", "panel_framing_draft",
                    ):
                        state.pop(key, None)
                else:
                    state["mode"] = "idle"
                    state["paused"] = False

            update_state(mutate)
            print({"pause": "Playback paused", "resume": "Playback resumed", "idle": "Renderer is idle"}[args.command])
            return 0
        indexes = panel_indices(args.panel)
        if args.command == "test-pattern":
            frames = {index: test_pattern(index) for index in indexes}
        elif args.command == "blank":
            update_state(lambda state: state.update({"mode": "blank", "paused": False}))
            frames = {
                index: Image.new("RGB", (LOGICAL_WIDTH, LOGICAL_HEIGHT), (0, 0, 0))
                for index in indexes
            }
        else:
            source = args.source.expanduser()
            if not source.is_file():
                raise IPCError(f"image does not exist: {source}")
            with Image.open(source) as opened:
                fitted = fit_image(opened, (LOGICAL_WIDTH, LOGICAL_HEIGHT), args.fit)
            frames = {index: fitted.copy() for index in indexes}
        metrics = send_fan_frames(frames)
        print(
            f"Sent panels {','.join(map(str, indexes))} through daemon "
            f"using IPC v{metrics.get('ipc_version', 0)}"
        )
        return 0
    except (IPCError, OSError, ValueError) as exc:
        print(f"zc360ctl: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
