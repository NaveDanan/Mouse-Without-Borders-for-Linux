# Switch machines without a permission prompt

Screen-edge capture can now read the input devices directly instead of going
through the InputCapture portal. That removes the last reason Mouse Without
Borders had to interrupt you: the "Capture Input" dialog that reappeared every
time you unlocked the screen.

## Why the prompt kept coming back

The compositor destroys a portal capture session whenever the screen locks, and
InputCapture interface version 1 has no restore token to rebuild it silently.
Version 2 added one, but the portal frontend only advertises
`MIN(impl_version, 2)`, and no released desktop backend implements it —
`xdg-desktop-portal-gnome` 46 through 50 all lack `CreateSession2`,
`restore_token` and `persist_mode`. So on every unlock the session had to be
recreated, and recreating it meant asking again.

Reading `/dev/input` sidesteps that entirely. There is no session for a lock
screen to take away, so **no prompt is shown, ever**. Together with the kernel
injection added in 0.7.0, enabling *Use direct kernel input* now replaces both
portal paths, and the option covers both directions.

### How edge detection works without the portal

The portal reports exact barrier crossings. evdev reports raw device deltas
before the compositor applies pointer acceleration, so a running position
estimate drifts from the real cursor and cannot be compared directly. Instead
only *overshoot* is counted: the part of a movement the desktop boundary
clipped. The compositor clips the real cursor at the same boundary, so
overshoot is the one quantity both agree on. Travelling to the edge is not a
crossing, a small nudge past it is not either, sustained pressure is, and a
hard flick into the edge crosses immediately.

### Holding the devices safely

While capturing, the keyboard, mouse and touchpad are held exclusively with
`EVIOCGRAB`, so local input does not reach this desktop. Since a device left
grabbed is a keyboard the user cannot get back, release is unconditional: on
request, on shutdown, when the owning object is dropped, if the process exits
for any reason, and through a watchdog that ends a capture the application has
stopped managing. Devices plugged in or unplugged are picked up automatically
while capture is idle.

## Session setup no longer blocks the command loop

Creating a portal session can wait on a consent dialog for as long as the user
takes, and the bridge processed commands strictly in order, so everything
behind it waited too — including the release that hands grabbed input devices
back. Measured before the fix: with a portal injection request pending,
`capture_release` never completed; with no dialog involved it returned at once.
Session creation now runs off the command loop, so control commands are always
answered. This was a pre-existing ordering problem that only became dangerous
once a command could be holding a keyboard.

## Also fixed

- **Touchpad tool codes were forwarded as keystrokes.** A captured touchpad
  reports `BTN_TOUCH` and `BTN_TOOL_FINGER` continuously; those sit outside the
  mouse-button range and were being sent as key presses, typing nonsense on the
  remote computer. Non-mouse button codes are now dropped.
- **System buttons were classified as keyboards.** Power buttons, the video
  bus, and vendor hotkey arrays all report a few key codes. Grabbing them would
  have taken away the power and brightness keys, so a keyboard now has to carry
  actual letter keys.
- **Recovery re-armed capture without recording it.** The recovery path issued
  `capture_init` with its own request id, so the reply bypassed the bookkeeping
  that marks capture ready and stores its zones: the session was recreated, and
  the application still believed capture was dead.
- **An expected lock-screen teardown replaced a more useful status.** Losing
  the capture session while locked is normal, and saying so buried the report
  of whether remote input still worked.
- **Idle devices could block a read.** Input devices are opened non-blocking so
  polling several of them cannot stall on one nobody is touching.

## Verification

- 254 Python tests and 66 Rust tests pass; Clippy is clean and AppStream
  metadata validates.
- Exercised through the production bridge on real hardware: an edge crossing
  activated capture at the desktop boundary, pointer events were forwarded
  while held, and the devices were released on request.
- Capture was confirmed to receive every event a device emits, and the release
  path was re-tested in the exact configuration that previously hung.
- Hotplug was verified by adding and removing a device mid-session.
- Device classification was checked against a real laptop, where only the
  typing keyboard, the mouse node and the touchpad are selected, and the
  application's own virtual devices are excluded so capture cannot feed
  injection back into itself.

No visual change beyond the wording of one checkbox.

## Packages

Debian, RPM and AppImage builds are attached for x86-64 and ARM64, together
with `SHA256SUMS`.
