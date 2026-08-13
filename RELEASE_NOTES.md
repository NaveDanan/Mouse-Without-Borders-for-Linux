# Stay connected through lid close, suspend and the lock screen

Mouse Without Borders for Linux kept dropping its Windows peers whenever the
machine slept, and it often refused to come back afterwards. This release fixes
the disconnects at their source and makes recovery automatic.

## Why the connection kept dropping

Two independent faults were responsible, and both are fixed.

**systemd ignored the sleep inhibitor when the lid closed.**
`logind.conf` documents that `LidSwitchIgnoreInhibited=` defaults to `yes`, so
the high-level `sleep` lock the app was holding is *silently discarded* on a lid
event. Only the low-level `handle-lid-switch` lock is honoured unconditionally,
and the app never took it. Closing the lid suspended the machine, NetworkManager
tore down Wi-Fi, and the peer connection died.

**A slow handshake was reported as a wrong security key.** A handshake timeout
was raised as an authentication error, which added the peer to the permanently
rejected list, surfaced *"verify its active security key"*, and stopped all
further reconnect attempts. A peer that was merely still waking up was locked
out until the user intervened. Timeouts are now a distinct, retryable condition.

## What changed

- **Suspend is now a clean hand-off.** The app holds a logind `delay` lock and
  reacts to `PrepareForSleep`: it releases the compositor grab and closes the
  control channels while the network is still up, so the Windows peer sees an
  immediate disconnect instead of a half-open socket. On resume it rebuilds
  from the signal rather than waiting for a clock-drift poll.
- **Reconnect understands a down link.** Attempts made before Wi-Fi has
  reassociated no longer burn the exponential backoff or report an error; they
  retry at a steady pace and report *"Waiting for the network"*.
- **Remote input recovers by itself after unlock.** The compositor destroys
  every injected input device when the screen locks. The app now rebuilds only
  the dead injection session, using its restore token, instead of discarding
  the whole portal helper and forcing a fresh consent dialog. Recovery waits
  for the unlock rather than retrying into a locked session.
- **Optional: keep the lid from suspending.** *Stay connected when the laptop
  lid closes* takes the low-level lid lock and locks the session in software
  instead, so the desktop is still secured while the peer stays connected. Off
  by default, because it also stops a closed laptop from sleeping in a bag.
- **Optional: never lock while connected.** A locked GNOME session cannot
  accept remote input at all, so this holds a GNOME idle inhibitor to keep the
  screen from auto-locking while a peer is sharing the desktop. Off by default.
- **Diagnosability.** Portal and compositor transitions are logged, EIS device
  pause and resume are reported instead of being swallowed, and repeated
  identical status lines no longer flood the log.

## Known limitation: the lock screen and the InputCapture prompt

**Remote input cannot control the GNOME lock screen, and this is not fixable
from the application.** Testing on GNOME 46 / Wayland confirmed it twice:

- through the portal, locking removes every EIS device and drops the connection
  (`inject_error: EIS disconnected`);
- through mutter's own private `org.gnome.Mutter.RemoteDesktop` API, the session
  object itself is deleted (`Object does not exist at path ...`).

No portal-based session can inject input into the lock screen. Use *never lock
while connected* to avoid reaching the lock screen, or unlock the machine
locally; the connection and remote input then restore themselves automatically.

A second, related limitation affects screen-edge capture. Session persistence
for InputCapture requires version 2 of that interface, which the portal
frontend has supported since xdg-desktop-portal 1.21.1. The frontend advertises
`MIN(impl_version, 2)`, so the desktop's portal *backend* has to implement it
too, and no released `xdg-desktop-portal-gnome` does: 46, 47, 48, 49 and 50 all
lack `CreateSession2`, `restore_token` and `persist_mode` in their InputCapture
implementation. Until a backend ships it, rebuilding a capture session always
prompts. Remote input injection is unaffected because RemoteDesktop is already
at version 2 and keeps its restore token. Mouse Without Borders already
requests the version 2 flow and falls back cleanly, so persistence starts
working by itself once a desktop provides it; the log now explains the prompt
instead of leaving it unexplained.

## Verification

- 230 Python tests and 27 Rust tests pass; Clippy is clean and AppStream
  metadata validates.
- Two unattended suspend/resume cycles on real hardware, using an RTC wake
  alarm: the pre-suspend close, the resume signal and the reconnection of both
  peers were confirmed from the service log, with no unreachable-network noise.
- The lid fault was confirmed against live logind state: `BlockInhibited` now
  contains `handle-lid-switch`, which it previously did not.
- The lock-screen limitation was measured directly with both APIs described
  above.
- New tests cover the lid inhibitor, the suspend and resume signals, lock-aware
  input recovery, retryable handshake timeouts and network-aware backoff. The
  logind signal tests run against a real private D-Bus in an isolated
  interpreter.

No user-visible visual change other than two new checkboxes under
*Other Options -> Linux Power*.

## Packages

Debian, RPM and AppImage builds are attached for x86-64 and ARM64, together
with `SHA256SUMS`.
