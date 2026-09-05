# Changelog

## 0.4.0 - 2026-09-05

- Added a visible display-mode selector for media, built-in system telemetry,
  weather, and external telemetry frames.
- Added a portable CPU, temperature, memory, storage, network, uptime, and
  clock dashboard using `psutil`.
- Added location-based current conditions and forecasts through Open-Meteo,
  with metric and imperial units and a fifteen-minute request cache.
- Added a safe image-folder bridge for user telemetry tools. It accepts a
  1920x180 `triptych.png` or three 640x180 panel PNG files and never executes
  third-party code.
- Replaced the persistent test-panel pause with a four-second identification
  overlay that automatically restores the previous display.
- Removed the persistent pink selection outline from the main panel preview.
- Added an in-app help dialog with the correct apply, framing, pause, identify,
  missing-source, and recovery workflow.
- Compacted the main layout so previews and controls remain grouped in tall
  tiling-window layouts.
- Added a subtle version label and `Made by Lucille Hon 💗` credit in the GUI
  and README, clearer control tooltips, and long-video guidance in both places.

## 0.3.2 - 2026-09-05

- Removed the redundant quick-framing controls from the main window.
- Added live, looping GIF and video playback behind the crop rectangle in the
  focused framing editor.
- Kept framing previews bounded to 1280x720 while preserving source aspect,
  so editing remains responsive without changing renderer output quality.
- Continued using the existing ffmpeg path instead of relying on desktop
  multimedia codecs that vary between Linux distributions.

## 0.3.1 - 2026-09-05

- Added an edit control beneath every panel preview.
- Added a focused full-source framing dialog with the exact output rectangle,
  shaded excluded area, thirds guides, drag positioning, wheel zoom, sliders,
  and explicit use/cancel actions.
- Added three-panel divisions and selected-position highlighting when framing
  a source that spans the full display triptych.
- Unified still/GIF crop calculation with the rectangle shown by the editor.
- Added uncropped first-frame extraction for video framing previews.

## 0.3.0 - 2026-09-05

- Added a standalone PySide6 GUI for stills, GIFs, video, span/mirror/panel
  layouts, draggable crop/zoom, FPS, physical panel mapping, and safe controls.
- Added a public socket-only background media renderer with latest-frame-wins
  delivery and no USB imports.
- Added atomic shared display state with automatic Lucille-state migration and
  live compatibility mirroring during upgrades.
- Expanded `zc360ctl` with persistent play, three-source, pause/resume,
  idle/blank, display-status, and transactional framing commands.
- Added desktop integration and an upgrade-aware generic renderer service.
- Kept the validated USB transport unchanged and retained `Restart=no` for the
  sole USB owner.

## 0.2.1 - 2026-09-05

- Rate-limited steady-state USB performance logging to one five-second
  summary instead of one journal entry per displayed frame.
- Preserved immediate first-frame timing and exact latest-transfer data in
  `zc360ctl status`.

## 0.2.0 - 2026-09-05

- Added the installable `jonsbo-zc360` Python package.
- Added `zc360d` and socket-only `zc360ctl` entry points.
- Split constants, pure protocol construction, image conversion, lifecycle,
  IPC, daemon, and transport facade into explicit modules.
- Added bounded IPC v1 requests, structured status and error responses, and a
  zero-panel-safe negotiation fallback for the old daemon.
- Added daemon status for serial identities, warmup state, uptime, completed
  frame sets, errors, timings, transport mode, and controller status bytes.
- Preserved the hardware-validated asynchronous transport byte-for-byte.
- Preserved the existing media surface and framing controls byte-for-byte.
- Added socket packet validation and correct failure acknowledgements.
- Installed the daemon into a stable private runtime instead of launching it
  from a mutable checkout venv.
- Hardened shutdown against systemd SIGKILL escalation and retained
  `Restart=no`.
- Added hardware-blind protocol, frame, IPC, daemon-control, preview, and
  framing-session validation.
- Added safe user/udev installers and a reboot-boundary migration guide.
