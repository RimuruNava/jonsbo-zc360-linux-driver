# Safe migration from the checkpoint daemon

The migration is designed so code can be installed while the current daemon
continues owning the panels. Installing files, dependencies, units, or the udev
rule does not contact USB.

## Install without switching owners

From the project directory:

```bash
./scripts/install-user.sh
./scripts/install-udev.sh
zc360ctl doctor
```

`install-user.sh` deliberately enables units without starting or restarting
them. When `zc360ctl` encounters the old running daemon, it performs one safe
zero-panel feature probe and reports `IPC v0` before using legacy frame packets.

The installer also adds **ZC-360 Control** to the application menu. New users
get the generic socket-only renderer. An already-enabled Lucille renderer is
preserved instead of enabling a competing renderer. Existing Lucille display
state is migrated to `~/.config/zc360/display.json` and mirrored during the
transition so the running pre-0.3 renderer can remain untouched.

## Switch at a normal full shutdown

Do not use `systemctl --user restart jonsbo-fan-daemon.service` for migration.
Leave the old owner running until the computer is shut down normally. On the
next boot the enabled packaged daemon will perform the single fresh claim.

After logging in:

```bash
zc360ctl status
journalctl --user -u jonsbo-fan-daemon.service -n 80 --no-pager
```

Expected status includes:

```text
Driver: jonsbo-zc360
IPC: v1
Panel 0: ... (ready)
Panel 1: ... (ready)
Panel 2: ... (ready)
```

Then verify the socket-only test path:

```bash
zc360ctl test-pattern
```

Open `zc360-gui` and apply media after this check. Closing the GUI is safe and
does not stop the background renderer.

## Failure policy

If the owner exits or a panel disappears, inspect the logs. Do not repeatedly
start it in the same physical power session. `Restart=no`,
`TimeoutStopSec=infinity`, and `SendSIGKILL=no` exist because recovering from a
bad ownership cycle may require a complete physical power removal.
