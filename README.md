# Mouse Without Borders for Linux

An independent, full Linux implementation and adaptation of PowerToys Mouse
Without Borders. It is compatible with the Windows application and lets a
single mouse and keyboard move between Windows and Linux PCs using
the familiar Mouse Without Borders GUI and connection workflow.

The application speaks the Windows program's native encrypted TCP protocol and
provides a GTK 4 rebuild of the classic settings form, a per-user background
service, rootless Wayland input through XDG Desktop Portal and EIS, text/PNG
clipboard sharing, encrypted cross-screen file drag-and-drop, screen-edge
switching, management commands, and DEB, RPM, and AppImage packages for x86-64
and ARM64.

> [!IMPORTANT]
> This is an independent community project. It is not an official Microsoft or
> PowerToys release. Mouse Without Borders and PowerToys are Microsoft product
> names and are referenced only to describe compatibility.

**[Download the latest release](https://github.com/NaveDanan/Mouse-Without-Borders-for-Linux/releases/latest)**

## Screenshot

![Mouse Without Borders for Linux connected to Windows PCs](docs/screenshot.png)

## Install

Download the package for your architecture from the
[GitHub releases](https://github.com/NaveDanan/Mouse-Without-Borders-for-Linux/releases)
page. The release provides these files:

| Distribution | x86-64 | ARM64 |
| --- | --- | --- |
| Debian/Ubuntu | `powertoys-mouse-without-borders_0.8.0_amd64.deb` | `powertoys-mouse-without-borders_0.8.0_arm64.deb` |
| Fedora/RHEL-style | `powertoys-mouse-without-borders-0.8.0-1.x86_64.rpm` | `powertoys-mouse-without-borders-0.8.0-1.aarch64.rpm` |
| Portable AppImage | `Mouse-Without-Borders-0.8.0-x86_64.AppImage` | `Mouse-Without-Borders-0.8.0-aarch64.AppImage` |

Debian or Ubuntu:

```sh
sudo apt install ./powertoys-mouse-without-borders_0.8.0_amd64.deb
powertoys-mouse-without-borders
```

Fedora or another RPM-based distribution:

```sh
sudo dnf install ./powertoys-mouse-without-borders-0.8.0-1.x86_64.rpm
powertoys-mouse-without-borders
```

Portable AppImage:

```sh
chmod +x Mouse-Without-Borders-0.8.0-x86_64.AppImage
./Mouse-Without-Borders-0.8.0-x86_64.AppImage
```

Replace `amd64`/`x86_64` with `arm64`/`aarch64` on an ARM64 computer. The
AppImage bundles the application runtime, but the host must still provide a
systemd user session, XDG Desktop Portal with a compatible compositor backend,
`xclip` or `wl-clipboard`, and `xdotool`. GNOME installations outside Ubuntu
may also need a StatusNotifier/AppIndicator shell extension for the top-bar
menu.

## Build and test

Ubuntu 24.04 on x86-64 and ARM64 is the package-build baseline. GNOME 46+
Wayland is the reference desktop; other desktops require portal backends that
implement InputCapture and RemoteDesktop v2 with EIS.

Install the build and runtime dependencies:

```sh
sudo apt install build-essential cargo python3 python3-gi \
  gir1.2-gtk-4.0 gir1.2-adw-1 python3-cryptography xclip wl-clipboard \
  xdg-desktop-portal xdg-desktop-portal-gnome dbus-user-session xdotool \
  file rpm python3-dev python3-pip python3-venv squashfs-tools
```

Install PyInstaller 6.21.0 in a virtual environment and download the native
`appimagetool` build when producing an AppImage.

```sh
python3 -m unittest discover -s tests -v
cargo test --manifest-path portal-bridge/Cargo.toml --locked
cargo clippy --manifest-path portal-bridge/Cargo.toml --locked --all-targets -- -D warnings
./packaging/build-deb.sh
./packaging/build-rpm.sh
python3 -m venv --system-site-packages build/appimage-venv
build/appimage-venv/bin/pip install PyInstaller==6.21.0
APPIMAGETOOL=/path/to/appimagetool-x86_64.AppImage \
  PATH="$PWD/build/appimage-venv/bin:$PATH" ./packaging/build-appimage.sh
```

Packages are written to `dist/`. Install a local DEB build with:

```sh
sudo apt install ./dist/powertoys-mouse-without-borders_0.8.0_amd64.deb
powertoys-mouse-without-borders
```

The first launch opens the setup experience, which asks for the other
computer's name and its 16-character security key. GNOME then displays its
normal Input Capture and Remote Desktop permission dialogs. The compositor must
receive the first approval from the signed-in user; an application cannot grant
itself global input access. The background service retains the approved portal
session across launches, Connect, Disconnect, Reconnect, suspend, and compatible
settings changes. On **Exit**, the session and all network, clipboard, and input
components are shut down completely. InputCapture v2 desktops persist the
compositor's restore token; InputCapture v1 may ask for permission again after a
full exit. A logout, permission revocation, or monitor-edge change can also require
approval. The service never reads
`/dev/input`, writes `/dev/uinput`, or runs as root.

While the UI is running, its Mouse Without Borders indicator remains visible in
Ubuntu's top bar. Closing the settings window with its **x** or **Close** button
hides the window without stopping sharing. **Exit** closes the settings and
indicator, disconnects every peer, stops clipboard watching, releases any active
input, closes the portal session, and stops the background service. The
indicator uses Ubuntu's built-in StatusNotifierItem/AppIndicator integration
and does not load GTK 3 into the GTK 4 application.

On each launch, the application checks the latest stable GitHub release in the
background. Being up to date and temporary network failures are silent. The
**Check Updates** checkbox on the Other Options tab controls launch checks, and
**Refresh** performs an immediate manual check. When an update is available,
the dialog shows the installed and latest versions. **Download and Install**
downloads the matching Debian package, verifies its GitHub-published SHA-256
digest, and requests administrator authorization through the desktop's normal
PolicyKit prompt. Mouse Without Borders stays open throughout the download and
installation, then closes and relaunches itself only after the installed
package version has been verified. RPM and AppImage installations should update
from the GitHub release page; their in-app automatic installer is not enabled.

## Settings form

The window mirrors the Windows form: Machine Setup, Other Options, and IP
Mappings tabs, the shared encryption key row, and the computer matrix.

The matrix is live rather than decorative. It always contains four computer
slots, like the Windows application. The local Linux computer is locked in one
slot; tick any of the other slots to add up to three Windows PCs, enter each
machine name, and drag the tiles to arrange how the pointer crosses between
them. `Two Row` changes the matrix between a 1x4 row and a 2x2 grid. `Wrap
Mouse` connects the two ends of a row.

Double-clicking a Windows tile takes over that computer and double-clicking the
local tile comes back. The service creates all required outer-edge portal
barriers in one approved capture session and routes each edge to the adjacent
computer. Input routing is bidirectional: a mouse and keyboard physically
attached to Windows can enter Linux and use the same matrix edges to return to
Windows or continue to another connected PC. Physical Linux monitors are
detected separately to ensure barriers are placed only on the desktop's
exterior boundary. Visible Connect and Disconnect buttons control the
background service, while right-clicking the matrix also offers Connect,
Disconnect, and Reconnect. Options with no Linux equivalent yet (for example
Disable CAD) are stored so the form round-trips, and take effect as the matching
feature lands.

With **Transfer file** checked, one regular file can be dragged through a
configured matrix edge in either direction between Linux and Windows. The file
contents use Mouse Without Borders' separate encrypted base-port connection;
the control connection authenticates the machine identity and encryption
profile before that socket is accepted. Files arriving on Linux are written
atomically without overwriting an existing file under
`Desktop/MouseWithoutBorders`. Mouse Without Borders' native protocol supports
one file per drag and does not transfer directories; zip a folder before
dragging it. A visible drop target follows the remote pointer on Linux, while
Windows shows its native PowerToys Mouse Without Borders drag animation.

Absolute remote pointer coordinates alone cannot activate GNOME Shell pressure
barriers. On a desktop edge that is not assigned to another matrix computer,
the Linux client adds outward relative pressure so an auto-hidden Ubuntu Dock
or other edge UI reveals normally. Matrix switching keeps priority on occupied
edges.

After a peer connects, the service remembers its LAN adapter address. Switching
to an offline peer sends Wake-on-LAN and completes the switch after reconnection;
switching to a connected locked peer sends the native Mouse Without Borders
awake packet before input. Wake-on-LAN must be enabled in the sleeping PC's
firmware, network-adapter settings, and operating system, and both machines must
be reachable on the same broadcast LAN.

When **Block Screen Saver on other machines** is checked, the Linux service
holds a logind sleep inhibitor for the complete Connect/reconnect lifetime. The
display may still blank or lock, and incoming mouse, keyboard, or awake packets
signal desktop user activity to wake the display, but the daemon and its TCP/EIS
sessions remain available. This is necessary because a fully suspended
userspace process cannot receive the mouse packet intended to wake it and stock
Windows Mouse Without Borders does not send a Linux Wake-on-LAN packet. If the
system is suspended anyway, the service reacts to logind's `PrepareForSleep`
fence: it releases the compositor grab and closes the control channels while the
network is still up, so the Windows peer sees a clean disconnect rather than a
half-open socket, and it rebuilds both channels the moment the system resumes.
Uncheck the option when normal automatic system suspend is preferred.

Two Linux-only switches sit under **Other Options -> Linux Power**, both off by
default:

**Stay connected when the laptop lid closes.** `logind.conf` documents that
`LidSwitchIgnoreInhibited=` defaults to `yes`, so a plain `sleep` inhibitor is
silently ignored on a lid event and the machine suspends regardless. Enabling
this additionally takes the low-level `handle-lid-switch` lock and locks the
session in software instead, keeping the desktop secured while the peer stays
connected. Leave it off if the machine may be carried in a bag while connected.

**Never lock this screen while a remote PC is connected.** A locked session
cannot accept remote input at all: the compositor destroys every injected input
device when the screen locks, through the portal and through mutter's own API
alike. This holds a GNOME idle inhibitor so the screen never auto-locks while a
peer is sharing the desktop. A manual lock still locks.

### Controlling the lock screen

**Use direct kernel input** replaces both portal paths at once: injection goes
through `/dev/uinput` instead of the RemoteDesktop portal, and screen-edge
capture reads `/dev/input` directly instead of the InputCapture portal. The compositor destroys portal sessions when the screen
locks -- through the portal and through mutter's own private API alike -- so no
portal client can type a password. A uinput device is indistinguishable from
physical hardware at the evdev layer, so remote keyboard and mouse keep working
on the lock screen and you can log back in from the remote computer.

It is off by default because it steps outside the portal's per-session consent
model: once enabled there is no permission prompt, and the packaged udev rule
lets **any** process running as a user in the `input` group synthesise input,
including into the lock screen. That group already owns `/dev/input/event*`, so
on most systems this grants no new physical-input access, but it is a real
widening and is worth understanding before enabling. Delete
`/usr/lib/udev/rules.d/60-powertoys-mouse-without-borders-uinput.rules` to
withdraw the capability. When `/dev/uinput` is not writable the application
reports why and continues on the portal.

Because neither direction is a portal session any more, nothing is destroyed
by the lock screen and **no permission prompt is ever shown**, including after
unlocking. Edge detection works differently from the portal's exact barriers:
evdev reports raw device deltas before pointer acceleration, so the position
estimate is clamped to the desktop exactly as the real cursor is, and a
crossing is reported when an already-pinned pointer keeps pushing outward.

While capturing, the keyboard, mouse and touchpad are held exclusively with
`EVIOCGRAB` so local input does not reach this desktop. Devices are handed back
on release, on shutdown, if the process exits for any reason, and by a watchdog
that ends a capture the application has stopped managing. Devices plugged in or
removed are picked up automatically while capture is idle.

When the screen does lock, remote input is reported as unavailable rather than
silently dropped, and only the sessions the compositor actually destroyed are
rebuilt once the session is unlocked again. Remote input injection restores
itself from its saved RemoteDesktop token without a prompt. Screen-edge capture
cannot do the same yet: InputCapture session persistence needs version 2 of
that interface, and while xdg-desktop-portal has supported it since 1.21.1, no
released desktop backend implements it, so that grant must be approved again.

Base TCP port and encryption compatibility live at the bottom of the IP
Mappings tab; the mapping table itself resolves a machine name to an address
before DNS is consulted. A host firewall must allow both the configured base
port for file data and `base + 1` for control traffic on the trusted LAN.

The key is not accepted as a command-line argument. The UI or
`configure --secret-stdin` writes it to a mode-`0600` file under
`$XDG_CONFIG_HOME/powertoys-mwb-linux/`. Logs redact the configured key. The
Windows wire format uses AES-CBC but no message authentication, so use the tool
only on a trusted LAN or VPN.

## Commands

```sh
powertoys-mouse-without-borders status
powertoys-mouse-without-borders connect
powertoys-mouse-without-borders disconnect
powertoys-mouse-without-borders reconnect
powertoys-mouse-without-borders switch-machine 1
powertoys-mouse-without-borders switch-machine 2
powertoys-mouse-without-borders switch-machine 3
powertoys-mouse-without-borders switch-machine 4
powertoys-mouse-without-borders switch-host
powertoys-mouse-without-borders local
```

While input is captured, `Ctrl+Alt+Esc` is the emergency return shortcut.
GNOME 46 does not expose the Global Shortcuts portal, so the Keyboard Shortcuts
group on the Other Options tab writes per-user GNOME custom bindings instead,
preserving any bindings you already have. "Switch between machines" binds
`Ctrl+Alt+F1` through `F4` (or `Ctrl+Alt+1` through `4`) to the corresponding
matrix slots, and the letter selectors bind reconnect, show settings, and
exit. `Disable` removes the binding again. The same actions remain available
from the matrix and the CLI commands above.

Wayland portal access is intentionally unavailable at the login/lock screen or
in another user's session. Native X11 needs a future XInput/XTest backend;
XWayland cannot provide secure global Wayland input.

## Compatibility

Mouse Without Borders releases use four incompatible encryption profiles and do
not negotiate a version. Auto mode opens a fresh connection for each known
profile: the final standalone Microsoft Garage release with fixed salt/IV,
50,000 PBKDF2-SHA1 iterations, and SHA-256 packet magic; PowerToys stable with
fixed salt/IV and 50,000 PBKDF2-SHA512 iterations; transitional random salt/IV
with 50,000 iterations; and current random salt/IV with 100,000 iterations. The
UI reports Connected only after the peer returns the exact complemented
handshake challenge.

## License and attribution

Released under the [MIT License](LICENSE). This adaptation originated in the
[Microsoft PowerToys](https://github.com/microsoft/PowerToys) codebase; original
Microsoft copyright notices are retained. See [ATTRIBUTION.md](ATTRIBUTION.md)
and the third-party notices included in each release package.
