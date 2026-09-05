# Driver architecture

## Safety invariant

The three controllers are claimed together exactly once for the lifetime of
`zc360d`. No renderer, CLI, preview command, or shell integration imports the
USB transport or opens a USB device.

```text
GUI / generic renderer / telemetry / zc360ctl
             |
       Unix socket IPC
             |
          zc360d
             |
 persistent libusb1 handles
             |
     three ZC-360 panels
```

Automatic daemon restart, socket activation, and automatic hotplug reclaim are
intentionally absent. This hardware has repeatedly entered a poisoned display
state after release/reclaim cycles. A proper driver must fail closed when a
controller disappears rather than repeatedly attempting to reclaim it.

## Package boundaries

- `zc360.constants` contains hardware geometry, IDs, and transport constants.
- `zc360.protocol` builds and validates command and framebuffer records. It is
  pure apart from the lazily loaded DES implementation.
- `zc360.frames` converts logical 640x180 RGB images into native 180x640 BGR888
  frame chunks.
- `zc360.ipc` owns image packet validation, IPC negotiation, structured daemon
  responses, and compatibility fallback.
- `zc360.transport` exposes the validated libusb1 implementation.
- `zc360.daemon` owns the complete claim, initialization, warmup, frame, and
  shutdown lifecycle.
- `zc360.state` owns the atomic, frontend-neutral display configuration and
  migration from the original Lucille path.
- `zc360.media` decodes and fits stills, GIFs, and ffmpeg video without any
  hardware access.
- `zc360.dashboards` renders portable system/weather panels and loads the
  image-only external telemetry folder contract without hardware access.
- `zc360.renderer` watches state and uses a latest-frame-wins socket pipeline.
- `zc360.gui` is a lazy-loading Qt Widgets control app. It previews locally and
  writes state; its temporary identify and blank buttons use IPC only.
- `zc360.cli` provides daemon diagnostics plus the same persistent generic
  media controls for headless use.

The original root module names remain as compatibility launchers so the
existing Lucille renderer and reverse-engineering scripts continue working.
The installed daemon runtime lives under `~/.local/share/jonsbo-zc360/venv`
instead of depending on a mutable checkout venv.

The generic renderer and Lucille renderer are alternative frontends. The
installer enables the generic renderer for new users and preserves Lucille on
machines where its service is already enabled. Neither renderer has a
`Wants=` or `Requires=` dependency that could implicitly start a USB owner.

## IPC

IPC v1 starts with a zero escape byte followed by `ZC36`, a version, opcode,
and bounded payload length. The zero byte is deliberately safe against the old
daemon: it is interpreted as an empty legacy frame set and performs no USB I/O.
This lets a new client probe an old running daemon and fall back to the legacy
frame protocol without restarting the owner.

Frame images use uncompressed binary PPM inside the local socket packet. This
is intentional. It preserves RGB pixels and avoids spending roughly 20 ms per
triplet on compression that provides no benefit on a local Unix socket.

IPC v1 adds structured success and failure responses plus status fields for:

- package and IPC version;
- stable serial-sorted panel identities;
- warmup state;
- daemon uptime and completed frame-set count;
- last transport mode, timing, and controller status bytes;
- processing error count.

## Transport policy

Complete 0/1/2 frame sets use the hardware-validated async triplet scheduler.
It maintains one outstanding 4096-byte write per controller, meets at a common
90-write barrier, waits the validated frame/status gap, and submits all three
status reads together.

Partial frame sets remain on the conservative synchronous path. Initialization
and the command-88 transition also remain synchronous on the same persistent
libusb1 handles.
