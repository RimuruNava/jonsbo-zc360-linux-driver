"""Shared, atomic display state for every ZC-360 frontend."""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

DEFAULT_FRAMING = {"focus_x": 0.5, "focus_y": 0.5, "zoom": 1.0}
VALID_LAYOUTS = {"span", "mirror", "panels"}
VALID_FITS = {"cover", "contain", "stretch", "manual"}
VALID_MODES = {"idle", "media", "telemetry", "weather", "external", "blank"}


def state_path() -> Path:
    configured = os.environ.get("ZC360_STATE") or os.environ.get("LUCILLE_ZC360_STATE")
    if configured:
        return Path(configured).expanduser()
    root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return root / "zc360" / "display.json"


def legacy_state_path() -> Path:
    root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return root / "lucille-shell" / "zc360-display.json"


def normalized_framing(value: object = None) -> dict[str, float]:
    value = value if isinstance(value, dict) else {}

    def number(name: str, default: float) -> float:
        try:
            return float(value.get(name, default))
        except (TypeError, ValueError):
            return default

    return {
        "focus_x": max(0.0, min(1.0, number("focus_x", 0.5))),
        "focus_y": max(0.0, min(1.0, number("focus_y", 0.5))),
        "zoom": max(1.0, min(8.0, number("zoom", 1.0))),
    }


def default_state() -> dict:
    return {
        "schema_version": 1,
        "mode": "idle",
        "layout": "span",
        "fit": "cover",
        "frames_per_second": 8.0,
        "panel_order": [0, 1, 2],
        "framing": dict(DEFAULT_FRAMING),
        "panel_framing": [dict(DEFAULT_FRAMING) for _ in range(3)],
        "paused": False,
        "telemetry_refresh_seconds": 2.0,
        "weather_location": "",
        "weather_units": "metric",
        "weather_refresh_seconds": 900.0,
        "external_directory": "",
        "external_refresh_seconds": 1.0,
    }


def normalize_state(value: object) -> dict:
    source = value if isinstance(value, dict) else {}
    state = default_state()
    state.update(source)
    state["schema_version"] = 1
    mode = str(state.get("mode", "idle"))
    state["mode"] = mode if mode in VALID_MODES else "idle"
    layout = str(state.get("layout", "span"))
    state["layout"] = layout if layout in VALID_LAYOUTS else "span"
    fit = str(state.get("fit", "cover"))
    state["fit"] = fit if fit in VALID_FITS else "cover"
    try:
        fps = float(state.get("frames_per_second", 8.0))
    except (TypeError, ValueError):
        fps = 8.0
    state["frames_per_second"] = max(0.5, min(20.0, fps))
    order = state.get("panel_order")
    try:
        order = [int(item) for item in order]
    except (TypeError, ValueError):
        order = [0, 1, 2]
    state["panel_order"] = order if sorted(order) == [0, 1, 2] else [0, 1, 2]
    state["framing"] = normalized_framing(state.get("framing"))
    framings = state.get("panel_framing")
    if not isinstance(framings, list) or len(framings) != 3:
        framings = [None, None, None]
    state["panel_framing"] = [normalized_framing(item) for item in framings]
    state["paused"] = bool(state.get("paused", False))
    for name, default, minimum, maximum in (
        ("telemetry_refresh_seconds", 2.0, 0.5, 30.0),
        ("weather_refresh_seconds", 900.0, 60.0, 3600.0),
        ("external_refresh_seconds", 1.0, 0.2, 60.0),
    ):
        try:
            number = float(state.get(name, default))
        except (TypeError, ValueError):
            number = default
        state[name] = max(minimum, min(maximum, number))
    state["weather_location"] = str(state.get("weather_location", ""))
    units = str(state.get("weather_units", "metric"))
    state["weather_units"] = units if units in {"metric", "imperial"} else "metric"
    state["external_directory"] = str(state.get("external_directory", ""))
    if "identify_until" in state:
        try:
            state["identify_until"] = float(state["identify_until"])
        except (TypeError, ValueError):
            state.pop("identify_until", None)
    if "source" in state:
        state["source"] = str(state["source"])
    if "sources" in state:
        values = state["sources"]
        state["sources"] = [str(item) for item in values] if isinstance(values, list) else []
    return state


def _load(path: Path) -> dict:
    try:
        with path.open(encoding="utf-8") as stream:
            return normalize_state(json.load(stream))
    except (OSError, ValueError):
        return default_state()


def read_state(path: Path | None = None) -> dict:
    target = path or state_path()
    if not target.exists() and path is None and not (
        os.environ.get("ZC360_STATE") or os.environ.get("LUCILLE_ZC360_STATE")
    ):
        legacy = legacy_state_path()
        if legacy.is_file():
            value = _load(legacy)
            write_state(target, value)
            return value
    return _load(target)


def _write_unlocked(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(normalize_state(value), stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _mirror_target(path: Path) -> Path | None:
    """Mirror changes for an already-running pre-0.3 Lucille renderer."""
    if os.environ.get("ZC360_STATE") or os.environ.get("LUCILLE_ZC360_STATE"):
        return None
    legacy = legacy_state_path()
    try:
        is_default = path.resolve() == state_path().resolve()
    except OSError:
        is_default = path == state_path()
    return legacy if is_default and legacy.is_file() else None


def _legacy_compatible_state(value: dict) -> dict:
    mirrored = dict(value)
    # Pre-0.3 Lucille renderers only know framing_editing as a freeze signal.
    if mirrored.get("paused") or mirrored.get("mode") in {"idle", "blank"}:
        mirrored["framing_editing"] = True
    return mirrored


@contextmanager
def _state_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f".{path.name}.lock")
    with lock_path.open("a+", encoding="utf-8") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def write_state(path: Path | None, value: dict) -> None:
    target = path or state_path()
    with _state_lock(target):
        _write_unlocked(target, value)
    legacy = _mirror_target(target)
    if legacy:
        with _state_lock(legacy):
            _write_unlocked(legacy, _legacy_compatible_state(value))


def update_state(mutator: Callable[[dict], None], path: Path | None = None) -> dict:
    target = path or state_path()
    with _state_lock(target):
        state = _load(target)
        mutator(state)
        state = normalize_state(state)
        _write_unlocked(target, state)
    legacy = _mirror_target(target)
    if legacy:
        with _state_lock(legacy):
            _write_unlocked(legacy, _legacy_compatible_state(state))
    return state
