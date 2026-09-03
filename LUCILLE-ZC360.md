# Lucille ZC-360 first-run path

This is an adapted checkpoint of
[`purplebrain23/jonsbo-tf3-linux-driver`](https://github.com/purplebrain23/jonsbo-tf3-linux-driver),
whose captured hardware identity exactly matches the three Jonsbo panels seen
on Lucille's machine: USB `43a8:0e61`.

The original MIT license and upstream source files are preserved. Local changes
add stable serial ordering, a safer `uaccess` rule, a required three-panel gate,
an interleaved startup image, project-path systemd wiring, and Lucille Shell's
three-panel telemetry renderer.

## Why the old sync probe timed out

These panels do not use the TURZX 512-byte command-10 protocol. Their command
packets are 32 bytes, use `AA 55` / `BB` framing, DES-ECB for bytes 2 through 9,
and channel `01`. The init sequence is `56:0`, `84:100`, `86:0`. Pixel frames
use channel `02`, a portrait `180x640` BGR framebuffer, and a first-frame commit
command `88`.

## Safety rule

Do not repeatedly start and stop the USB owner. The exact-hardware upstream
reports that claim/release cycles can leave the render path black until a full
power cycle even while commands still acknowledge. The daemon therefore owns
all three interfaces together and has `Restart=no`. Renderers use its local
socket and may be restarted freely.

## Install without touching the panels

```bash
cd ~/Projects/jonsbo-zc360-driver

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

sudo cp udev/99-jonsbo-tf3.rules /etc/udev/rules.d/70-jonsbo-zc360.rules
sudo udevadm control --reload-rules
sudo udevadm trigger --attr-match=idVendor=43a8 --attr-match=idProduct=0e61
```

Dependency installation and the udev rule do not send panel commands. Do not
run the old probe with `--sync` again.

## First hardware test

Run the owner once in a terminal and leave it running:

```bash
cd ~/Projects/jonsbo-zc360-driver
.venv/bin/python3 jonsbo_fan_daemon.py
```

It refuses to claim anything unless all three devices are present. When it
succeeds, the panels should show `ZC-360 PANEL 0/1/2` and their stable serials.

In a second terminal:

```bash
cd ~/Projects/jonsbo-zc360-driver
.venv/bin/python3 examples/send_test_via_daemon.py
```

This client uses only the local socket. It does not open or release USB.

The first socket request after a panel has been idle performs a full interleaved
warmup and may take considerably longer than three seconds. The client and
library therefore use a 120-second acknowledgement timeout.

## Lucille display surface

`lucille_zc360_surface.py` is the installed display process. It is a socket-only
client and may be stopped, edited, or restarted without touching a USB claim.
It supports one wide image, GIF, or video sliced across all three panels, a
mirrored mode, temporary shell-event interruptions, automatic thermal warnings,
and a diagnostic telemetry fallback.

`lucille_zc360_renderer.py` supplies that diagnostic triptych. Its visual
language is intentionally passive instrumentation rather than three dashboard
cards:

- panel 0 defaults to processor load, thermals, clock, and load history;
- panel 1 defaults to Radeon engine load, thermals, VRAM, power, and history;
- panel 2 defaults to clock, memory, disk, network, and uptime;
- dark is structure, pale is information, pink is the committed system seam,
  and yellow is restricted to the live trace register.

The assignment lives in `zc360-layout.json`. USB indices are stable because the
driver sorts the devices by serial number. `gpu_device` defaults to `auto`,
which chooses the AMD DRM device with the largest VRAM aperture so the 7800X3D
iGPU does not win over the RX 9070 XT. It may instead be set to a path such as
`/sys/class/drm/card1/device`.

Preview all three panels without opening the socket or importing PyUSB:

```bash
cd ~/Projects/jonsbo-zc360-driver
.venv/bin/python3 lucille_zc360_renderer.py \
    --preview-dir /tmp/lucille-zc360-preview
```

## Play media

Use the persistent control command while the surface service is running. A wide
source becomes one 1920x180 canvas and is sliced left-to-right across the panel
order in `zc360-layout.json`:

```bash
cd ~/Projects/jonsbo-zc360-driver

.venv/bin/python3 lucille_zc360_control.py play \
    ~/Videos/panel-loop.mp4 \
    --layout span \
    --fit cover \
    --fps 8
```

Still images, animated GIFs, MP4, MKV, WebM, MOV, AVI, and M4V are accepted.
Video uses `ffmpeg`; GIF and still-image playback only require Pillow. The frame
rate is bounded to 0.5-20 fps, and the daemon acknowledgement naturally
prevents the renderer from queuing frames faster than the hardware accepts.

Mirror the same 640x180 composition on every panel:

```bash
.venv/bin/python3 lucille_zc360_control.py play \
    ~/Pictures/panel-loop.gif \
    --layout mirror \
    --fit contain \
    --fps 8
```

Loop three different sources in the confirmed physical left, centre, and right
order:

```bash
.venv/bin/python3 lucille_zc360_control.py play-panels \
    ~/Videos/left.mp4 \
    ~/Videos/centre.gif \
    ~/Videos/right.mp4 \
    --fit cover \
    --fps 8
```

The sources may mix videos, GIFs, and still images. Each is fitted independently
to 640x180. The three decoders are sampled once per render cycle and delivered
in a single synchronized daemon request. Different-duration loops wrap
independently.

### Measure delivered frame rate

While media is active, the surface prints a rolling performance line every five
seconds. It separates decoder wait, composition, PNG serialization, and the
combined socket/daemon/USB round-trip:

```bash
journalctl --user \
    -u lucille-zc360-renderer.service \
    -f |
    rg --line-buffered 'PERF'
```

Example fields are `actual=`, `decode=`, `compose=`, `png=`, `socket+usb=`, and
`payload=`. Requested FPS is only a ceiling; `actual` is the measured delivery
rate. This instrumentation is entirely in the restart-safe socket client and
does not require restarting the USB owner.

Switch back to the diagnostic surface or inspect current state:

```bash
.venv/bin/python3 lucille_zc360_control.py telemetry
.venv/bin/python3 lucille_zc360_control.py status
```

Lucille Shell can use the same stable command path for a short interruption:

```bash
.venv/bin/python3 lucille_zc360_control.py overlay \
    CHAT \
    --detail 'LAYER ACTIVE' \
    --ttl 2.4 \
    --accent pink
```

Media selection persists in
`~/.config/lucille-shell/zc360-display.json`. A temporary overlay does not
replace the selected loop. Critical CPU/GPU temperatures use the same
interruption layer in restrained yellow and then return to the media.

## Make it persistent without disturbing the working owner

The manually running daemon in the first terminal currently owns all three
panels successfully. Do **not** stop it and do **not** start the daemon service
on top of it. Install and enable its unit for the next login, but start only the
socket renderer today:

```bash
cd ~/Projects/jonsbo-zc360-driver
mkdir -p ~/.config/systemd/user

cp systemd/jonsbo-fan-daemon.service \
   systemd/lucille-zc360-renderer.service \
   ~/.config/systemd/user/

systemctl --user daemon-reload
systemctl --user enable jonsbo-fan-daemon.service
systemctl --user enable lucille-zc360-renderer.service
systemctl --user start lucille-zc360-renderer.service
```

The renderer unit has `After=` ordering but deliberately no `Wants=` or
`Requires=` relationship, so starting it cannot start a second USB owner. At
the next normal login, the enabled owner will claim once and the renderer will
wait for its socket. The owner remains `Restart=no`; the renderer is safe to
restart:

```bash
systemctl --user restart lucille-zc360-renderer.service
journalctl --user -u lucille-zc360-renderer.service -n 40 --no-pager
```

Before installation, the checkpoint can be validated without hardware:

```bash
./scripts/validate-checkpoint.sh
```

Do not use `systemctl --user restart jonsbo-fan-daemon.service` as an ordinary
troubleshooting step. If the owner fails, inspect it before allowing another
claim cycle.

### Upgrade an already-running renderer

Replacing and restarting only the renderer is safe. Leave the foreground USB
owner alone:

```bash
cd ~/Projects/jonsbo-zc360-driver

cp systemd/lucille-zc360-renderer.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user restart lucille-zc360-renderer.service
```
