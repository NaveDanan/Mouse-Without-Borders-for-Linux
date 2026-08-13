# Control the lock screen from the remote computer

The desktop portal cannot reach a locked Linux session. When the screen locks,
the compositor destroys every injected input device, so remote keyboard and
mouse stop at the lock screen and there is no way to log back in from the
other computer. This release adds an optional path that does not have that
limitation, and fixes a settings bug that could stop an older install from
starting.

## Direct kernel input

**Use direct kernel input**, under *Other Options -> Linux Power*, injects
through `/dev/uinput` instead of the RemoteDesktop portal. A uinput device is
indistinguishable from physical hardware at the evdev layer, so the lock screen
accepts it exactly like a real keyboard: remote control keeps working while the
screen is locked, and typing the password from the remote computer logs the
machine back in.

Measured on GNOME 46 / Wayland, driving the real bridge with the backend
enabled: after locking the session, keystrokes sent as ordinary `inject_key`
commands reached the lock screen and were evaluated by PAM, which logged the
expected failure for a deliberately wrong password. Absolute pointer
positioning was verified separately against a fullscreen window and landed on
the exact requested pixel for every sample.

Three virtual devices are created rather than one, because libinput classifies
a device by the axes it advertises and mixing relative with absolute pointer
axes on a single node is ambiguous.

### Why it is off by default

Kernel input steps outside the portal's per-session consent model. Once
enabled there is no permission prompt, and the packaged udev rule lets any
process running as a user in the `input` group synthesise input, including into
the lock screen. That group already owns `/dev/input/event*`, so on most
systems this grants no new access to physical input devices, but it is a real
widening and is worth understanding before turning it on. Removing
`/usr/lib/udev/rules.d/60-powertoys-mouse-without-borders-uinput.rules`
withdraws the capability.

The portal remains the default and nothing changes for existing installs.
Where `/dev/uinput` cannot be opened the application reports the reason and
continues on the portal rather than failing. Screen-edge capture always uses
the portal, so enabling kernel input does not remove the capture prompt.

## Settings from a newer release no longer break an older one

`Config.validate()` rejected any unrecognised entry in `other_options` or
`hotkeys` outright. Because a newer release writes its own keys into the shared
settings file, downgrading, or simply running an older install afterwards, made
it fail to start with `unknown options: ...`. Unrecognised entries are now kept
and reported instead of being fatal, matching how unknown top-level fields were
already tolerated, and they survive a save so upgrading again restores the
choice. Hotkey values are only range-checked for hotkeys this version knows, so
a future release may widen them safely.

## Verification

- 246 Python tests and 34 Rust tests pass; Clippy is clean and AppStream
  metadata validates.
- The lock-screen path was exercised through the production bridge, not a
  prototype, and confirmed from PAM's own log.
- A Rust test asserts the kernel registers all three virtual devices, and skips
  where `/dev/uinput` is not writable, such as on a build machine.
- Unit tests cover the `uinput_user_dev` payload layout, absolute-axis ranging,
  device-name truncation, backend selection, portal fallback reporting and the
  lock-screen behaviour of each backend.

No visual change other than one new checkbox under *Other Options -> Linux
Power*.

## Packages

Debian, RPM and AppImage builds are attached for x86-64 and ARM64, together
with `SHA256SUMS`.
