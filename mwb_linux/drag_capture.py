"""Short-lived XWayland drop catcher for a file drag leaving Linux.

Wayland intentionally exposes drag offers only to a surface under the pointer.
PowerToys solves the equivalent Windows problem with a helper window placed
under the cursor.  This module does the same through GTK's X11 backend, where a
window can be positioned without weakening the daemon's rootless design.
"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys


def _pointer_position() -> tuple[int, int]:
    try:
        result = subprocess.check_output(
            ["xdotool", "getmouselocation", "--shell"],
            text=True,
            timeout=1,
            stderr=subprocess.DEVNULL,
        )
        values = dict(
            line.split("=", 1) for line in result.splitlines() if "=" in line
        )
        return int(values.get("X", 0)), int(values.get("Y", 0))
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0, 0


def main() -> int:
    os.environ.setdefault("GDK_BACKEND", "x11")
    import gi

    gi.require_version("Gdk", "4.0")
    gi.require_version("GdkX11", "4.0")
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gdk, GdkX11, GLib, Gtk

    Gtk.init()
    loop = GLib.MainLoop()
    window = Gtk.Window()
    window.set_title("Mouse Without Borders drag bridge")
    window.set_decorated(False)
    window.set_focusable(False)
    window.set_focus_on_click(False)
    window.set_default_size(200, 200)
    window.set_opacity(0.01)
    target = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
    target.set_preload(True)
    window.add_controller(target)
    captured: list[str] = []

    def inspect_value(*_args: object) -> None:
        value = target.get_value()
        if not isinstance(value, Gdk.FileList):
            return
        files = value.get_files()
        if len(files) != 1:
            return
        path = files[0].get_path()
        if path and os.path.isfile(path):
            captured.append(path)
            loop.quit()

    target.connect("notify::value", inspect_value)
    target.connect("enter", lambda *_args: (inspect_value(), Gdk.DragAction.COPY)[1])

    x, y = _pointer_position()
    x11 = ctypes.CDLL("libX11.so.6")
    x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
    x11.XOpenDisplay.restype = ctypes.c_void_p
    x11.XMoveResizeWindow.argtypes = [
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint,
        ctypes.c_uint,
    ]
    x11.XRaiseWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    x11.XFlush.argtypes = [ctypes.c_void_p]
    x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
    x11.XCloseDisplay.restype = ctypes.c_int
    display = x11.XOpenDisplay(None)
    xid = 0
    tick = 0

    def place_window(*_args: object) -> bool:
        nonlocal xid, tick
        surface = window.get_surface()
        if surface is None or not display:
            return True
        if not xid:
            xid = GdkX11.X11Surface.get_xid(surface)
            GdkX11.X11Surface.set_skip_taskbar_hint(surface, True)
            GdkX11.X11Surface.set_skip_pager_hint(surface, True)
        # Moving the helper around a stationary grabbed pointer causes the
        # compositor's XWayland DND bridge to deliver DragEnter, just as the
        # Microsoft helper does on Windows.
        offset = (tick % 20) - 10
        tick += 1
        x11.XMoveResizeWindow(
            display, xid, x - 100 + offset, y - 100 + offset, 200, 200
        )
        x11.XRaiseWindow(display, xid)
        x11.XFlush(display)
        return not captured and tick < 40

    window.connect("map", lambda *_args: GLib.timeout_add(20, place_window))
    timeout = float(os.environ.get("MWB_DRAG_CAPTURE_TIMEOUT", "2.0"))
    GLib.timeout_add(max(250, int(timeout * 1000)), lambda: (loop.quit(), False)[1])
    window.present()
    loop.run()
    window.destroy()
    if display:
        x11.XCloseDisplay(display)
    if not captured:
        return 1
    sys.stdout.write(json.dumps(captured[0]))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
