# ZC-360 Reverse-Engineering Experiment Log

This document records the experiments, false starts, measurements, failures,
re-interpretations, and successful tests that led to the current understanding
of the Jonsbo ZC-360 three-display USB transport.

It intentionally includes approaches that did not work.

The goal is to make the reasoning reproducible and to save future driver
authors from repeating the same experiments or misdiagnosing controller-state
problems as framebuffer-format problems.

Research session: 2026-09-03

---

# 1. Starting point

The Linux driver already had a functional implementation of the basic
TURZX/Jonsbo display protocol derived from earlier reverse engineering.

Hardware:

    USB VID:PID   43a8:0e61
    Product       TURZX-338inch-r
    Displays      3 independent USB devices
    Native FB     180 x 640
    Logical image 640 x 180
    Pixel format  BGR888

The three displays expose stable individual USB serial numbers, so the driver
sorts by serial rather than relying on USB bus addresses, which can change
between boots.

The production architecture already used a persistent daemon so renderers
could restart without repeatedly claiming and releasing the USB devices.

---

# 2. USB ownership problems discovered early

Repeated claim/release cycles were observed to be unsafe.

Possible symptoms included:

- displays remaining black;
- displays returning to the factory Jonsbo screen;
- a mixture of factory-logo content and live framebuffer content;
- stale rectangular framebuffer regions;
- later otherwise-valid writes failing to fully recover the display.

A full physical power cycle reliably restored normal controller state.

This led to an important architectural rule:

    Claim the three displays once and retain ownership for the entire session.

The renderer should never directly own the USB interfaces.

---

# 3. Startup permission race

A cold boot could initially fail with a USB permission error because systemd
started the daemon before udev had finished applying access permissions.

The original failure appeared as an access/claim problem even though the
devices became usable shortly afterward.

A USB startup gate was added.

It:

1. enumerates all three devices without claiming them;
2. checks the corresponding /dev/bus/usb nodes for read/write access;
3. waits until all three are available;
4. only then performs the single real claim cycle.

Observed startup:

    USB startup gate: found=3/3 accessible=no
    ...
    USB startup gate: found=3/3 accessible=yes
    USB startup gate: all three panels are accessible

This removed the cold-boot race without introducing extra claim/release cycles.

---

# 4. Baseline performance profiling

The original three-panel renderer was completely serial.

Representative measurements:

    actual output          ~6.4-6.6 FPS initially
    decode                 ~1 ms
    composition            ~2-3 ms
    PNG encoding           ~25-28 ms initially
    socket + USB           ~121-122 ms / three-panel update

The USB path was already the dominant cost.

A static socket benchmark measured roughly:

    ~119 ms / three-panel request

showing that media decoding and composition were not the main bottleneck.

---

# 5. PNG optimization

The renderer originally spent roughly 25 ms compressing PNG images before
sending them over the Unix socket.

Changing the PNG serialization to:

    compress_level=0

reduced this to approximately:

    ~6-7 ms

and improved the serial renderer to roughly:

    ~7.5-7.6 FPS

This was a useful optimization, but it did not solve the larger USB transport
limit.

---

# 6. Frame structure confirmed

One framebuffer contains:

    180 * 640 * 3
    = 345600 bytes

Each protocol record is:

    32-byte header
    480-byte pixel payload
    = 512 bytes

Therefore:

    345600 / 480
    = 720 protocol records

The driver groups eight records per USB write:

    8 * 512
    = 4096 bytes

Thus one complete panel frame is:

    90 * 4096-byte bulk OUT transfers

Sequence numbering is:

    1 ... 720

The first write contains records 1-8.

The final write contains records 713-720.

---

# 7. Increasing USB write size

The number of protocol records grouped into each USB write was made
configurable.

The normal configuration is:

    8 records
    = 4096-byte USB writes
    = 90 writes/frame

A test used:

    16 records
    = 8192-byte USB writes
    = 45 writes/frame

The larger write size reached USB correctly, but total panel-frame transfer
time remained around:

    ~38.5 ms

There was effectively no performance improvement.

Conclusion:

    Python write-call overhead was not the limiting factor.

The display controller itself was pacing the stream.

---

# 8. usbmon timing analysis

Linux usbmon showed a synchronous pattern:

    SUBMIT
    COMPLETE
    SUBMIT
    COMPLETE
    ...

Representative completion latency:

    mean      ~392 us
    median    ~209 us
    p95       ~657 us

Python userspace delay between one completion and the next submission was
typically very small:

    mean      ~32.6 us
    median    ~18 us

A complete 90-write frame occupied approximately:

    ~38.5-38.7 ms

This reinforced the conclusion that the device/controller, rather than Python,
was setting the per-panel transfer rate.

---

# 9. Single-panel test

Only one display was updated.

Result:

    ~18.5-19 FPS

Individual push time:

    ~39.2-39.8 ms

This was a major clue.

Each physical display was independently capable of roughly 19 FPS.

The poor three-panel result was therefore primarily caused by updating three
independent devices serially:

    ~39 ms * 3
    ~= 118 ms

rather than by an inherent 6-8 FPS limit of the LCD hardware.

---

# 10. Naive three-panel parallel push

The next experiment used three threads and called the normal blocking
push_image() implementation concurrently for all three devices.

Performance improved substantially.

Typical result:

    individual pushes   ~50-51 ms
    three-panel packet  ~51 ms
    renderer            ~12 FPS

However, visual instability appeared.

Observed symptoms included:

- shaking;
- display instability;
- two displays eventually freezing.

A physical power cycle was required to restore normal operation.

This approach was abandoned.

At the time this was interpreted primarily as unsafe simultaneous controller
traffic.

Later experiments showed that the situation was more complicated.

---

# 11. Staggered parallel tests

To reduce simultaneous activity, panel updates were deliberately offset.

## Two panels, 20 ms stagger

Observed:

    each push       ~40 ms
    packet          ~60 ms
    renderer        ~14 FPS

This was significantly better than fully serial operation.

## Three panels, 22 ms stagger

Observed:

    packet          ~84-85 ms
    renderer        ~10 FPS

The approach worked but sacrificed too much throughput.

It did not appear to be the scheduling model used by the official application.

---

# 12. Continuous two-in-flight scheduler

A more aggressive experiment maintained a continuous stream:

- launch one complete panel transfer roughly every 20 ms;
- allow at most two whole-panel pushes in flight;
- use latest-frame-wins behavior.

Results:

    renderer input        ~19 FPS
    effective output      ~16-16.5 FPS / panel
    individual push       ~40 ms

Initially this looked extremely promising.

Motion was much smoother and obvious shaking was greatly reduced.

However, a persistent rectangular region eventually appeared at an edge of
all three displays.

The rectangle remained even after later frames were sent.

At this stage the artifact was interpreted as controller corruption caused by
overlapping complete framebuffer operations.

The scheduler was disabled.

This interpretation was later revised.

---

# 13. Initial stale-rectangle misdiagnosis

The retained rectangular region looked like a piece of a previous image that
was no longer being overwritten.

Because it appeared after concurrency experiments, the first hypothesis was:

    overlapping transfers corrupted the display controller framebuffer state

The panels were repeatedly power-cycled during debugging.

Later visual inspection showed that the retained content was recognizable as
previous telemetry / built-in display imagery.

This was the first indication that the problem might involve controller state
rather than malformed framebuffer bytes.

---

# 14. Official Windows application capture

The official Jonsbo Windows application was captured using USBPcap/Wireshark.

The three ZC-360 USB devices were identified independently.

The capture produced the most important transport discovery of the session.

The official application does NOT send:

    entire panel A
    then entire panel B
    then entire panel C

Instead, transfers from all three devices are interleaved.

Example:

    panel A submit 4096
    panel C submit 4096
    panel B submit 4096

    panel A complete
    panel A next submit

    panel B complete
    panel B next submit

    panel C complete
    panel C next submit

Each physical display effectively maintains one outstanding 4096-byte transfer.

When that transfer completes, the next 4096-byte chunk for the same panel is
submitted.

---

# 15. Windows frame boundary discovered

Status IN transfers occurred in synchronized groups.

The distance between consecutive status groups was exactly consistent with:

    90 OUT transfers
    * 3 panels
    * submit + completion
    +
    3 IN status transfers
    * submit + completion

This strongly confirmed a synchronized three-panel frame cycle.

A representative Windows frame boundary looked like:

    final OUT completes on all three panels
                |
                v
        ~12.7 ms quiet period
                |
                v
      three status IN submissions
       essentially simultaneously
                |
                v
      wait for all three completions
                |
                v
           next frame

Observed steady-state rate was approximately:

    ~16 FPS

This became the target Linux transport model.

---

# 16. Windows framebuffer header comparison

A real Windows 4096-byte framebuffer transfer was extracted.

The eight records contained:

    seq 1
    seq 2
    ...
    seq 8

The final transfer contained:

    seq 713
    ...
    seq 720

Every one of the 720 Windows 32-byte framebuffer headers was then compared
against the Linux `_frame_header()` implementation.

Result:

    ALL WINDOWS HEADERS MATCH _frame_header() BYTE-FOR-BYTE

This ruled out:

- wrong magic;
- wrong channel;
- wrong sequence start;
- off-by-one sequence numbering;
- special final-record behavior;
- incorrect terminator;
- incorrect 32-byte header layout.

The framebuffer packet builder was not responsible for the stale region.

---

# 17. Threaded "vendor triplet" prototype

A prototype attempted to imitate the Windows model using:

    three Python threads
    +
    blocking PyUSB writes

Each thread performed:

    write 4096
    wait for completion
    write next 4096

The threads began together, followed by a three-panel barrier.

After all framebuffer writes completed:

    wait ~12.7 ms
    then read three statuses

Measured performance:

    framebuffer phase  ~45-46 ms
    total transaction  ~59 ms
    equivalent rate    ~16.9 FPS
    status             0x62 / 0x62 / 0x62

usbmon showed something superficially very similar to the Windows application:

- one transfer in flight per device;
- completion followed quickly by the next submission;
- all three devices interleaved.

This looked extremely promising.

However, visible stale regions appeared afterward.

This prototype is retained as:

    vendor_triplet.py

but is considered unsafe and should not be used as a production transport.

---

# 18. Visual evidence changed the diagnosis

A video/photo of the displays was examined.

The retained rectangles were clearly recognizable as pieces of previous
telemetry / Jonsbo content.

Examples included surviving cyan and yellow areas matching the old displayed
image.

This proved that the artifact was not random pixel corruption.

It was stale framebuffer/default-controller content.

That distinction changed the investigation from:

    "which framebuffer bytes are malformed?"

toward:

    "why did the controller fail to fully transition/update?"

---

# 19. Post-frame status-delay hypothesis

The normal Linux driver waited approximately:

    1 ms

after the framebuffer writes before reading status.

The Windows capture showed approximately:

    12.7 ms

between the final framebuffer completion and the status stage.

A configurable Linux delay was added and tested at approximately 13 ms.

Result:

    transfer time increased as expected
    visual stale region did NOT improve

Therefore the ordinary 1 ms status timing was not sufficient to explain the
artifact.

The experiment was reverted.

---

# 20. Complete Linux frame submission confirmed

usbmon showed, for each of the three panels:

    90 OUT submissions
    90 OUT completions
    1 status IN submission
    1 status IN completion

The submitted chunk sequence for every panel was:

    1, 9, 17, 25, ...
    ...
    697, 705, 713

Exactly 90 chunks.

No chunks were skipped or duplicated.

The actual submitted URB size was confirmed using:

    usb.urb_len = 4096

A misleading truncated-capture `usb.data_len` value was therefore not treated
as the true transfer size.

---

# 21. Clean serial baseline test

A physical power cycle was performed.

All experimental scheduler environment variables were removed.

The normal persistent PyUSB daemon was started.

One solid-color frame was sent using the ordinary serial implementation.

Result:

    complete solid frame
    no stale logo
    no rectangular residue

The remaining panels were tested similarly.

Result:

    all three could be fully overwritten in pristine serial mode

This was critical.

It proved that the fundamental framebuffer format and normal PyUSB serial path
were sound.

---

# 22. python-libusb1 introduced

`python-libusb1` 3.4.0 was installed in the project virtual environment.

Runtime library:

    libusb 1.0.30

The necessary asynchronous APIs were verified:

    USBDeviceHandle.getTransfer()
    USBTransfer.setBulk()
    USBTransfer.submit()
    USBContext.handleEvents()
    USBTransfer.getStatus()
    USBTransfer.getActualLength()
    USBTransfer.getBuffer()

This made it possible to reproduce the vendor transport using genuine libusb
asynchronous transfers rather than Python threads around blocking PyUSB calls.

---

# 23. First libusb1 probe failure

The first standalone libusb1 probe:

1. found all three displays;
2. claimed all three;
3. began initialization;
4. timed out reading the response to command 56.

The script aborted.

The displays returned to their default logo.

The problem was not the controller.

The probe was simply stricter than the known-good PyUSB implementation.

The existing PyUSB `_send_cmd()` treats a missing command response as nonfatal.

The libusb1 probe was modified to behave the same way.

---

# 24. Reclaim-state contamination discovered

After an aborted probe, the devices were released.

Running another probe in the same physical power session produced:

    colored framebuffer square
    +
    retained Jonsbo/default-logo region

This initially looked like a difference between PyUSB and libusb1.

A clean physical power cycle was then performed.

On the fresh controller state:

    libusb1 synchronous init
    +
    libusb1 synchronous RGB warmup

produced completely solid:

    red
    green
    blue

on all three displays.

Therefore libusb1 itself was not the cause.

The corrupted-looking state had been introduced by:

    claim
    partial initialization
    release
    reclaim

without a physical controller reset.

This became one of the most important practical findings of the session.

---

# 25. Known-good PyUSB startup capture

A fresh physical power cycle was followed by a capture of the normal PyUSB
daemon startup.

The initialization sequence was:

    cmd 56
    cmd 84 brightness=100
    response 0x55
    cmd 86 rotation=0
    response 0x57

for each panel.

The command-56 response could time out without preventing successful startup.

---

# 26. First-frame state transition discovered

The known-good startup capture exposed an unusual first-frame state.

For all three displays, the first complete framebuffer:

    contained all 90 writes;
    contained the full sequence;
    completed in only ~10.4-10.6 ms.

Immediately afterward the controller reported:

    0x60

The driver then sent:

    cmd 88

The command response was:

    0x59

After a ~300 ms settle period, later complete frames took:

    ~37.5 ms

and status became:

    0x62

This strongly indicates:

    0x60 = first-frame/pre-live state
    cmd 88 = transition/commit into normal live-display mode
    0x59 = command-88 acknowledgement
    0x62 = steady-state framebuffer status

The first ~10.5 ms transfer is NOT an incomplete framebuffer.

All 90 transfers were present.

The controller simply behaves very differently before the command-88 state
transition.

---

# 27. True asynchronous libusb prototype

A new prototype used genuine asynchronous libusb transfers.

Architecture:

    one USBTransfer object per panel

For each panel:

    submit chunk
    completion callback fires
    immediately reconfigure/re-submit same transfer with next chunk

All three devices are driven by one:

    USBContext.handleEvents()

event loop.

There are no Python worker threads around blocking framebuffer writes.

After all 90 chunks on all three displays complete:

    common frame barrier
    wait ~12.7 ms

Three asynchronous status IN transfers are then submitted BEFORE processing
their completion events.

This reproduces the vendor model significantly more faithfully.

---

# 28. Clean asynchronous test

The displays were first established in a known-clean state using solid:

    left    red
    centre  green
    right   blue

All three were visually confirmed clean.

Exactly ONE asynchronous three-panel gray frame was then sent.

Measured result:

    write phase             46.578 ms
    first-submit span        1.134 ms
    frame/status gap        12.832 ms
    status phase             0.123 ms
    status-submit span       0.018 ms
    total                   59.539 ms
    equivalent rate         16.80 FPS

Statuses:

    0x62 / 0x62 / 0x62

Visual result:

    all three panels appeared uniformly gray

No obvious:

- red residue;
- green residue;
- blue residue;
- factory Jonsbo logo;
- stale rectangle

was visible.

This is the first Linux three-panel transport tested in this session that both:

    approaches the official Windows cadence

and:

    produces a clean framebuffer on all three displays.

---

# 29. Capture-analysis caveat

Linux usbmon/tcpdump captures dropped some packets during the high-rate tests.

Examples included hundreds of dropped capture records.

This created misleading post-processing results.

One analyzer incorrectly selected serial warmup frames from different panels
and reported them as the asynchronous triplet.

Another analyzer could not find all three simultaneous seq=1 submissions
because one or more capture records had been dropped.

Therefore:

    Missing packet in pcap != missing USB transfer

when the capture reports dropped records.

Application-level libusb completion status, device status responses, timing,
and visible display output must also be considered.

For actual submitted transfer size, use:

    usb.urb_len

rather than truncated capture payload length.

---

# 30. Important false conclusions corrected during testing

Several interpretations changed as stronger evidence became available.

## Initial belief

Concurrent writes inherently corrupt the three controllers.

## Revised

The official application itself uses concurrent/interleaved writes.

The unsafe behavior was specific to attempted implementations and/or poisoned
controller state.

---

## Initial belief

A retained rectangle meant the framebuffer tail was not being generated.

## Revised

All 720 frame headers and all 90 Linux chunk submissions were correct.

The retained region visibly contained old/default imagery.

---

## Initial belief

The normal 1 ms Linux post-frame delay might be too short.

## Revised

Testing a ~13 ms delay did not fix the artifact.

---

## Initial belief

Direct libusb1 initialization might behave differently from PyUSB.

## Revised

Fresh-controller libusb1 synchronous initialization and warmup produced fully
solid displays.

The bad state had resulted from an earlier claim/release cycle.

---

# 31. Approaches and current status

## Fully serial PyUSB

Status:

    SAFE / known-good

Performance:

    roughly 7-8 FPS three-panel renderer after other optimizations

Use:

    fallback / baseline

---

## Larger 8 KiB framebuffer writes

Status:

    WORKS but provides no meaningful speed gain

Reason:

    device pacing dominates

---

## Whole-frame three-thread PyUSB parallelism

Status:

    UNSAFE / rejected

Observed:

    shaking
    freezes
    stale-frame/controller-state problems

---

## Staggered whole-frame PyUSB

Status:

    experimental / not preferred

Observed:

    10-14 FPS depending on configuration

---

## Continuous overlapping whole-frame scheduler

Status:

    rejected

Observed:

    ~16 FPS
    later stale rectangular regions

---

## Threaded "vendor triplet" with blocking PyUSB

Status:

    UNSAFE / historical experiment

Observed:

    ~16.9 FPS
    USB schedule superficially similar to Windows
    persistent stale regions seen in testing

---

## True asynchronous python-libusb1 transport

Status:

    SUCCESSFUL PROTOTYPE / preferred production direction

Observed:

    16.80 FPS equivalent frame transaction
    clean all-gray output
    0x62 status on all three panels
    vendor-like status submission timing

---

# 32. Current production direction

The intended final architecture is:

    persistent daemon
           |
           +-- wait for udev permissions
           |
           +-- discover all 3 panels
           |
           +-- claim ALL 3 once
           |
           +-- initialize ALL 3
           |
           +-- perform first-frame / cmd-88 live-mode transition
           |
           +-- keep ownership permanently
           |
           +-- true asynchronous libusb steady-state transport
           |
           +-- Unix socket
                    |
                    +-- renderer/UI clients

Important rule:

    Renderer restarts must never imply USB interface release/reclaim.

---

# 33. Practical recovery rule

If a display shows a mixture of:

    factory Jonsbo logo
    +
    new framebuffer content

after experimental USB ownership changes:

DO NOT immediately assume the framebuffer math is broken.

First suspect controller ownership/state contamination.

Recommended recovery:

1. stop anything accessing the displays;
2. perform a full physical power cycle;
3. establish exactly one clean ownership cycle;
4. retest using the known-good serial path.

This procedure repeatedly restored normal behavior during development.

---

# 34. Why this history is preserved

Several failed implementations produced USB traces that appeared superficially
reasonable.

Without the chronological experimental history, a future developer could
easily repeat them and reach the same misleading conclusions.

The most important lesson from the session is that this hardware has two
separate concerns:

    protocol correctness

and:

    controller lifecycle / transport semantics

A perfectly correct framebuffer can still produce misleading visible output
if the controller has been left in an abnormal state by interface
release/reclaim behavior.

Conversely, true asynchronous three-device transmission is possible and is
used by the official application.

## 35. Production daemon migration and sustained validation

The standalone true-async result was promoted into the real persistent daemon.

### Migration

The production path now uses `python-libusb1` for controller ownership from
the first claim until final shutdown.

Changes included:

- `libusb1` added to `requirements.txt`;
- framebuffer preparation extracted into shared `prepare_frame_chunks()`;
- `zc360_async_transport.py` created from the known-good probe transport;
- daemon no longer selects the threaded `vendor_triplet` experiment;
- daemon no longer owns panels through PyUSB;
- all three panels are still claimed before any are initialized;
- initialization and warmup remain synchronous on the same libusb1 handles;
- complete three-panel packets use genuine asynchronous transfers;
- partial-panel packets remain on the conservative synchronous path.

The important ownership sequence is now:

```text
libusb1 claim
    ↓
libusb1 init / warmup
    ↓
libusb1 async steady state
    ↓
release only at daemon shutdown
```

### First clean production boot

The old daemon and renderer were stopped and the machine/controller was given
a full physical power flush.

The new daemon then received exactly one fresh claim cycle.

All three startup ownership frames appeared cleanly with no factory-logo
fragments or partial framebuffer corruption.

Command 56 produced `LIBUSB_ERROR_TIMEOUT [-7]` on all three panels during
initialization. This matched the previously observed benign cmd56 timeout and
was intentionally tolerated.

### Continuous playback

Representative steady-state output:

```text
USB PERF packet≈61-63ms
panels=3
mode=async-triplet
usb≈59-61ms
status=0x62/0x62/0x62
```

This corresponds to roughly:

```text
~59.8 ms per USB triplet
≈16.7 FPS transport rate
```

The renderer itself reported approximately:

```text
actual≈13.5-13.6fps
png≈6-7ms
socket+usb≈65ms
```

The first renderer packet after the displays had been idle triggered the
existing 30-cycle synchronous stale-panel warmup and therefore took roughly
five seconds. This was expected and was not an async transport failure.

### Sustained validation

The machine was then used normally, including gaming, for roughly one hour.

No visible:

- stale rectangular regions;
- old Jonsbo boot-logo fragments;
- frozen panels;
- shaking;
- panel desynchronization;
- partial-frame borders

were observed.

Steady-state logs continued to report `0x62/0x62/0x62` and USB triplets
remained around the expected 59-61 ms range.

This is the first fast three-panel implementation in the project that both
matches the official Windows scheduling model and remains visually clean
during sustained production use.

### Conclusion

The true libusb asynchronous implementation is now the production transport.

The next performance problem is above the USB layer. The remaining gap between
roughly 16.7 FPS transport capacity and roughly 13.5 FPS rendered playback
comes from the serialized renderer → PNG → socket → USB pipeline.

The proven USB scheduling should be left alone unless new evidence requires
changing it.
