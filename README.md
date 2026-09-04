# Jonsbo ZC-360 Linux Driver

Unofficial Linux userspace driver and reverse-engineering project for the
LCD displays built into the **Jonsbo ZC-360B / ZC-360W** linked fan assembly.

If you found this while searching for a **TF3-360 Linux driver**, **TF3 360**,
**TF3-360SCB**, or **TF3-360SCW**, you're probably in the right place:
the ZC-360 fan-display hardware is also relevant to Jonsbo's TF3-360 setups.

This repo specifically targets the **ZC-360 fan LCDs**. It is not intended to
be a generic driver for every display or controller in the TF3 family.

I originally just wanted the three little screens in my PC to play animations
properly on Linux.

That somehow turned into USB captures, Windows-app reverse engineering,
controller-state archaeology, several increasingly cursed concurrency
experiments, and figuring out why pieces of the Jonsbo boot logo could
occasionally survive underneath an otherwise-valid framebuffer.

So this repo now contains both the driver and the notes from figuring out how
these things actually behave.

**Useful search terms:** Jonsbo ZC-360, ZC-360B, ZC-360W, ZC360, TF3-360,
TF3 360, TF3-360SCB, TF3-360SCW, TURZX-338inch-r, USB `43a8:0e61`.

> **Current state**
>
> The production daemon now owns all three ZC-360 controllers through
> `python-libusb1` from the first USB claim until shutdown.
>
> Initialization and startup warmup stay on the conservative synchronous path
> using those same persistent handles. Complete three-panel frames use the
> genuine asynchronous transport reproduced from the official Windows
> scheduling model.
>
> On the tested three-panel setup, steady-state USB triplets take roughly
> **59-61 ms**, or about **16.5-16.8 FPS** at the transport layer, with all
> three controllers consistently returning `0x62`.
>
> A clean fresh-power production run remained visually stable for roughly an
> hour of normal use and gaming: no stale borders, frozen panels, shaking, or
> surviving framebuffer fragments were observed.
>
> The older threaded PyUSB "vendor triplet" experiment is retained for
> research history but is explicitly unsafe.

Not affiliated with or endorsed by Jonsbo. Provided as-is, without warranty.
See [LICENSE](LICENSE).

---

## Hardware

This project targets the LCD controllers in the **Jonsbo ZC-360B / ZC-360W**
three-fan display assembly.

Tested USB device:

```text
43a8:0e61  turzx.com  TURZX-338inch-r
```

A ZC-360 assembly exposes **three independent USB display devices**, one for
each fan LCD, behind the same internal USB hub.

Each panel has its own USB serial number, so the driver uses serials for stable
identity rather than USB bus/device addresses, which can change after reboot.

Framebuffer:

```text
Native framebuffer: 180 × 640 portrait
Pixel format:        BGR888
Logical input:       640 × 180 landscape
```

The controller is effectively mounted rotated inside the fan assembly, so the
driver accepts normal 640×180 content and rotates it into the native portrait
framebuffer before transmission.

### TF3-360 connection

The ZC-360 fan-display hardware is also relevant to Jonsbo's **TF3-360** AIO
family, including names such as **TF3-360SCB** and **TF3-360SCW**.

That is why TF3-360 appears in some of the project's history and why it is
included as a search term.

The important distinction is:

```text
This repo drives the ZC-360 fan LCDs.

It does not claim to implement every TF3-360 device.
```

The larger pump/block display found in some TF3-360 configurations is a
separate USB device and is not the primary target of this project.

---

## What's here

- `jonsbo_fan_lib.py` — core ZC-360 protocol implementation, command channel,
  frame encoding and known-good serial framebuffer path.
- `jonsbo_fan_daemon.py` — persistent USB owner. Claims all three displays
  once and accepts frames through a Unix socket.
- `async_triplet_probe.py` — genuine asynchronous `python-libusb1` transport
  prototype based on the official Windows scheduling model.
- `vendor_triplet.py` — **unsafe historical experiment** using Python threads
  around blocking PyUSB transfers. Kept for research, not production.
- `lucille_zc360_surface.py` — my main socket-only media/display surface.
- `lucille_zc360_control.py` — media, telemetry, overlay and source controls.
- `lucille_zc360_renderer.py` — diagnostic telemetry renderer/fallback.
- `jonsbo_monitor.py` — earlier/upstream-style monitoring example.
- `examples/send_test_via_daemon.py` — safe panel identity test; never claims
  USB itself.
- `docs/ZC360_PROTOCOL_RESEARCH.md` — cleaner technical protocol reference.
- `docs/ZC360_EXPERIMENT_LOG.md` — chronological record of the experiments,
  failed approaches and conclusions that changed along the way.
- `systemd/` — user service templates.
- `udev/` — non-root USB access rules.

---

## Protocol, very condensed

A complete native framebuffer contains:

```text
180 × 640 × 3 = 345600 bytes
```

Pixel data is divided into:

```text
32-byte header
480-byte pixel payload
----------------------
512 bytes per protocol record
```

Therefore one framebuffer contains exactly:

```text
345600 / 480 = 720 records
```

The official Windows application groups eight records into each USB bulk OUT:

```text
8 × 512 = 4096 bytes
```

so one complete panel frame is:

```text
90 × 4096-byte USB writes
```

with sequence numbers `1 ... 720`.

The first write contains records `1-8`.

The final write contains records `713-720`.

We extracted a real Windows frame and compared all **720 32-byte headers**
against the Linux `_frame_header()` implementation.

They matched byte-for-byte.

For the full details, see
[`docs/ZC360_PROTOCOL_RESEARCH.md`](docs/ZC360_PROTOCOL_RESEARCH.md).

---

## First-frame / live-mode transition

Command packets use an eight-byte DES-ECB encrypted command payload.

Known commands include:

```text
56  init
84  brightness
86  rotation
88  first-frame / live-mode commit
```

One of the more useful things we discovered is that a freshly powered
controller behaves very differently from one already in normal live-display
mode.

On all three displays, a clean startup capture showed:

```text
first complete framebuffer
        ~10.4-10.6 ms
              ↓
          status 0x60
              ↓
           command 88
              ↓
         response 0x59
              ↓
            settle
              ↓
normal framebuffer
          ~37.5 ms
              ↓
          status 0x62
```

The fast first framebuffer is still complete: all 90 transfers are present.

Command 88 appears to be the transition that takes the controller from its
boot state into normal live-frame operation.

---

## Why there is a daemon

This is probably the most important practical thing we learned:

**these controllers really do not like being repeatedly released and
reclaimed.**

During testing, repeated USB ownership cycles produced displays containing a
mixture of:

```text
built-in Jonsbo logo
+
new framebuffer content
```

Later otherwise-valid writes did not always cleanly recover the display.

A full physical power cycle restored normal operation.

This is extremely misleading while reverse engineering because it can look
like your framebuffer dimensions, rotation or final blocks are wrong when
they are actually fine.

The reliable architecture is:

```text
boot
 ↓
wait until all 3 devices are accessible
 ↓
claim ALL THREE once
 ↓
initialize ALL THREE
 ↓
perform first-frame / command-88 transition
 ↓
keep USB ownership
 ↓
renderer talks to the owner through a Unix socket
```

The renderer can then be restarted freely without releasing the interfaces.

That is why the daemon is not just incidental complexity.

### If your screen looks cursed

If part of the factory Jonsbo logo survives underneath or beside your own
framebuffer after experimenting with USB ownership:

**do not immediately rewrite your framebuffer code.**

Stop anything touching the displays, fully power-cycle the ZC-360 controller,
then establish exactly one clean USB ownership session and test again.

We wasted a fairly impressive amount of time learning this rule so hopefully
you don't have to.

---

## Three-panel performance

A single ZC-360 panel can accept a normal steady-state frame in roughly:

```text
~39 ms
≈19 FPS by itself
```

Updating three complete panels serially therefore costs roughly:

```text
39 ms × 3 ≈118 ms
```

which explains why the original three-panel serial implementation lives in
roughly the 7-8 FPS region after other overhead.

The official Windows application does something much smarter.

It maintains approximately **one 4096-byte transfer outstanding per physical
panel** and lets all three independent USB streams progress together.

After all three reach the same frame boundary:

```text
all framebuffer OUTs complete
          ↓
   ~12.7 ms quiet period
          ↓
3 status IN transfers submitted together
          ↓
 all three status reads complete
          ↓
       next frame
```

This produces roughly **16 FPS across all three displays**.

---

## Async Linux reproduction

A genuine asynchronous implementation using `python-libusb1` successfully
reproduced this model.

Clean measured test:

```text
write phase:          46.578 ms
first-submit span:     1.134 ms
frame/status gap:     12.832 ms
status phase:          0.123 ms
status-submit span:    0.018 ms
total:                59.539 ms
equivalent rate:      16.80 FPS

statuses:
0x62 / 0x62 / 0x62
```

Before that test the three displays contained solid red, green and blue
frames.

Exactly one asynchronous gray triplet was then sent.

All three displays became visually uniform gray with no obvious surviving
RGB region or factory-logo residue.

This transport is now integrated into the persistent production daemon.

---

## Things we tried that did not become the answer

Quite a few.

Increasing USB writes from 4 KiB to 8 KiB did not meaningfully improve frame
time; controller pacing dominates.

Naively pushing three complete frames from three PyUSB threads improved speed
but caused instability.

Staggered whole-frame updates were safer but left too much performance on the
table.

A continuous overlapping scheduler reached roughly 16 FPS but was followed by
stale rectangular regions.

A threaded attempt to imitate the Windows transport looked surprisingly
convincing in usbmon and reached around 16.9 FPS, but real hardware still
developed stale-frame behavior.

The genuine asynchronous libusb implementation is the first fast three-panel
approach from this testing that also produced a clean visual result.

The full chronological story is in:

[`docs/ZC360_EXPERIMENT_LOG.md`](docs/ZC360_EXPERIMENT_LOG.md)

---

## Install

```bash
git clone https://github.com/RimuruNava/jonsbo-zc360-linux-driver.git
cd jonsbo-zc360-linux-driver

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

sudo cp udev/99-jonsbo-zc360.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules
sudo udevadm trigger
```

Unplug/replug the ZC-360 USB connection or reboot after installing the udev
rule so the permissions take effect.

### Find your panel mapping

Physical left/centre/right mapping is machine-specific.

With the daemon already running:

```bash
.venv/bin/python3 examples/send_test_via_daemon.py
```

Copy:

```text
zc360-layout.example.json
```

to:

```text
zc360-layout.json
```

and adjust it for your hardware.

The real `zc360-layout.json` is ignored by Git because it contains
machine-specific panel serial numbers.

---

## Running it

The current setup is documented in:

[`LUCILLE-ZC360.md`](LUCILLE-ZC360.md)

The important rule is still:

**do not casually restart the process that owns the USB interfaces.**

The renderer is socket-only and safe to restart.

The USB owner is intentionally long-lived.

If you're developing against the hardware, read the ownership/controller-state
sections in the research docs first. It may save you a power cycle or twenty.

---

## Separate pump display

Some TF3-360 configurations also have a larger screen on the pump/block.

That display is **not a ZC-360 controller** and is not the device being
reverse engineered here.

The setup this project came from exposes the separate pump display as:

```text
1cbe:0035
```

Earlier versions of this project used a patched
`turing-smart-screen-python` / `turingscreencli` setup to drive it.

That support remains optional and separate from the ZC-360 fan driver.

The protocol research in this repository should not be assumed to apply to
that pump screen.

---

## Research notes

The clean technical reference is:

[`docs/ZC360_PROTOCOL_RESEARCH.md`](docs/ZC360_PROTOCOL_RESEARCH.md)

The chronological experiment log is:

[`docs/ZC360_EXPERIMENT_LOG.md`](docs/ZC360_EXPERIMENT_LOG.md)

The experiment log deliberately includes dead ends and incorrect early
conclusions.

Reverse-engineered hardware is particularly good at making the wrong
explanation look convincing, so I wanted the failures preserved too instead
of publishing only the final answer.

---

## Credits

This project builds on earlier Linux reverse-engineering work for the Jonsbo /
TURZX display protocol, including the original driver this ZC-360 adaptation
started from.

Additional ZC-360 work in this repository includes USBPcap/usbmon analysis,
three-panel transport characterization, controller lifecycle testing,
first-frame state analysis, Windows transport comparison and the asynchronous
libusb reproduction.

Protocol information was obtained through USB traffic analysis and
decompilation of the vendor Windows application for interoperability purposes.

No Jonsbo vendor code or assets are redistributed here.

---

## Disclaimer

Unofficial project.

Not affiliated with, supported by, or endorsed by Jonsbo.

Use it at your own risk — and, seriously, if you're experimenting with USB
ownership, be prepared to physically power-cycle the displays.
