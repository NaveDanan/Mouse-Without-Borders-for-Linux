# Reliable lock-screen wake and permission continuity

Mouse Without Borders for Linux **v0.5.5** makes remote access dependable when
the Linux display blanks, locks, reconnects, or returns from a forced suspend.

## ✨ What changed

- **The PC stays remotely reachable while reconnecting.** The logind sleep
  inhibitor now follows the complete Connect intent instead of disappearing
  during a temporary TCP interruption.
- **Remote input wakes GNOME's locked display.** GNOME 46 advertises the
  freedesktop activity method but rejects it at runtime, so the app now uses a
  lock-preserving GNOME notification wake path and harmless EIS activity.
- **Portal approval survives forced suspend whenever the session survives.**
  Resume recovery now health-checks the existing portal bridge and only
  rebuilds it when it is actually dead. This prevents unnecessary object-share
  prompts on InputCapture v1 desktops, which cannot issue restore tokens.
- **Recovery remains automatic when a session really is lost.** Stale network
  sockets are discarded, connections retry immediately, and a dead portal
  bridge is still rebuilt with restore tokens where the desktop supports them.

## ✅ Verification

- 193 Python tests, including new reconnect, lock wake, EIS wake, and portal
  preservation regressions.
- 27 Rust portal-bridge tests plus formatting and Clippy validation.
- AppStream metadata and release-package inspection.
- Live GNOME 46 inspection confirmed the real `SimulateUserActivity`
  `NotSupported` behavior, the active logind inhibitor, InputCapture v1, and
  safe fallback behavior while the desktop is already awake.

This release has no visual UI change, so no new screenshot is required.

## 📦 Downloads

Choose the DEB, RPM, or AppImage for x86-64 or ARM64 below. `SHA256SUMS` is
included for independent verification.

> [!NOTE]
> InputCapture v1 cannot persist permission across a genuine compositor or
> login-session loss. v0.5.5 avoids needless session destruction, but a real
> portal loss on those desktops still requires user approval.
