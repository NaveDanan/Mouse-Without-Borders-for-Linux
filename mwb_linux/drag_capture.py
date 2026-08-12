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
import time

EDGE_MONITOR_THICKNESS = 8


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


def _display_size() -> tuple[int, int]:
    try:
        result = subprocess.check_output(
            ["xdotool", "getdisplaygeometry"],
            text=True,
            timeout=1,
            stderr=subprocess.DEVNULL,
        )
        width, height = result.split()
        return max(1, int(width)), max(1, int(height))
    except (OSError, ValueError, subprocess.SubprocessError):
        return 1920, 1080


def _gtk_modules():
    os.environ.setdefault("GDK_BACKEND", "x11")
    import gi

    gi.require_version("Gdk", "4.0")
    gi.require_version("GdkX11", "4.0")
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gdk, GdkX11, GLib, Gtk

    return Gdk, GdkX11, GLib, Gtk


def _x11_library():
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
    x11.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
    x11.XDefaultRootWindow.restype = ctypes.c_ulong
    x11.XQueryPointer.argtypes = [
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_ulong),
        ctypes.POINTER(ctypes.c_ulong),
        ctypes.POINTER(ctypes.c_int),
        ctypes.POINTER(ctypes.c_int),
        ctypes.POINTER(ctypes.c_int),
        ctypes.POINTER(ctypes.c_int),
        ctypes.POINTER(ctypes.c_uint),
    ]
    x11.XQueryPointer.restype = ctypes.c_int
    x11.XFlush.argtypes = [ctypes.c_void_p]
    x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
    x11.XCloseDisplay.restype = ctypes.c_int
    return x11


def _x11_pointer_position(x11: object, display: int) -> tuple[int, int]:
    root = x11.XDefaultRootWindow(display)
    returned_root = ctypes.c_ulong()
    returned_child = ctypes.c_ulong()
    root_x = ctypes.c_int()
    root_y = ctypes.c_int()
    window_x = ctypes.c_int()
    window_y = ctypes.c_int()
    mask = ctypes.c_uint()
    found = x11.XQueryPointer(
        display,
        root,
        ctypes.byref(returned_root),
        ctypes.byref(returned_child),
        ctypes.byref(root_x),
        ctypes.byref(root_y),
        ctypes.byref(window_x),
        ctypes.byref(window_y),
        ctypes.byref(mask),
    )
    return (root_x.value, root_y.value) if found else (0, 0)


def _configure_surface(GdkX11, surface: object) -> int:
    xid = GdkX11.X11Surface.get_xid(surface)
    GdkX11.X11Surface.set_skip_taskbar_hint(surface, True)
    GdkX11.X11Surface.set_skip_pager_hint(surface, True)
    return xid


def monitor_main() -> int:
    """Watch configured XWayland screen edges before InputCapture activates.

    The drag offer still belongs to the Linux file manager while it traverses
    this narrow transparent strip.  Each observed file is printed immediately
    so the daemon can cache it before the pointer crosses the portal barrier.
    """

    Gdk, GdkX11, GLib, Gtk = _gtk_modules()
    Gtk.init()
    loop = GLib.MainLoop()
    width, height = _display_size()
    requested = {
        edge.strip().lower()
        for edge in os.environ.get(
            "MWB_DRAG_MONITOR_EDGES", "left,right,top,bottom"
        ).split(",")
        if edge.strip()
    }
    default_rectangles = {
        "left": (0, 0, EDGE_MONITOR_THICKNESS, height),
        "right": (
            max(0, width - EDGE_MONITOR_THICKNESS),
            0,
            EDGE_MONITOR_THICKNESS,
            height,
        ),
        "top": (0, 0, width, EDGE_MONITOR_THICKNESS),
        "bottom": (
            0,
            max(0, height - EDGE_MONITOR_THICKNESS),
            width,
            EDGE_MONITOR_THICKNESS,
        ),
    }
    x11 = _x11_library()
    display = x11.XOpenDisplay(None)
    if not display:
        return 1
    windows: list[object] = []
    xids: list[int] = []
    inspectors: list[object] = []
    last_value: list[str] = [""]
    last_emitted_at: list[float] = [0.0]

    def emit(path: str, edge: str) -> None:
        now = time.monotonic()
        if path == last_value[0] and now - last_emitted_at[0] < 0.75:
            return
        last_value[0] = path
        last_emitted_at[0] = now
        sys.stdout.write(
            json.dumps({"event": "drag", "path": path, "edge": edge}) + "\n"
        )
        sys.stdout.flush()

    def leave(edge: str) -> None:
        if not last_value[0]:
            return
        last_value[0] = ""
        last_emitted_at[0] = 0.0
        sys.stdout.write(json.dumps({"event": "leave", "edge": edge}) + "\n")
        sys.stdout.flush()

    rectangles: list[tuple[str, tuple[int, int, int, int]]] = []
    try:
        target_values = json.loads(os.environ.get("MWB_DRAG_MONITOR_TARGETS", "[]"))
    except (json.JSONDecodeError, TypeError):
        target_values = []
    if isinstance(target_values, list):
        for target in target_values:
            if not isinstance(target, dict):
                continue
            edge = str(target.get("edge", "")).lower()
            if edge not in default_rectangles:
                continue
            zone = target.get("zone")
            if not (
                isinstance(zone, list)
                and len(zone) == 4
                and all(isinstance(value, int) for value in zone)
                and zone[2] > 0
                and zone[3] > 0
            ):
                rectangle = default_rectangles[edge]
            else:
                zone_x, zone_y, zone_width, zone_height = zone
                if edge == "left":
                    rectangle = (
                        zone_x,
                        zone_y,
                        EDGE_MONITOR_THICKNESS,
                        zone_height,
                    )
                elif edge == "right":
                    rectangle = (
                        zone_x + zone_width - EDGE_MONITOR_THICKNESS,
                        zone_y,
                        EDGE_MONITOR_THICKNESS,
                        zone_height,
                    )
                elif edge == "top":
                    rectangle = (
                        zone_x,
                        zone_y,
                        zone_width,
                        EDGE_MONITOR_THICKNESS,
                    )
                else:
                    rectangle = (
                        zone_x,
                        zone_y + zone_height - EDGE_MONITOR_THICKNESS,
                        zone_width,
                        EDGE_MONITOR_THICKNESS,
                    )
            rectangles.append((edge, rectangle))
    if not rectangles:
        rectangles = [
            (edge, rectangle)
            for edge, rectangle in default_rectangles.items()
            if edge in requested
        ]

    for edge, rectangle in rectangles:
        window = Gtk.Window()
        window.set_title("Mouse Without Borders drag edge")
        window.set_decorated(False)
        window.set_focusable(False)
        window.set_focus_on_click(False)
        window.set_opacity(0.01)
        window.set_default_size(rectangle[2], rectangle[3])
        target = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        target.set_preload(True)
        window.add_controller(target)

        def inspect(_target=target, _edge=edge) -> None:
            value = _target.get_value()
            if not isinstance(value, Gdk.FileList):
                return
            files = value.get_files()
            if len(files) != 1:
                return
            path = files[0].get_path()
            if path and os.path.isfile(path):
                emit(path, _edge)

        def value_changed(*_args, _target=target, _edge=edge, _inspect=inspect) -> None:
            if isinstance(_target.get_value(), Gdk.FileList):
                _inspect()
            else:
                leave(_edge)

        target.connect("notify::value", value_changed)
        target.connect(
            "enter",
            lambda *_args, _inspect=inspect: (_inspect(), Gdk.DragAction.COPY)[1],
        )
        target.connect("leave", lambda *_args, _edge=edge: leave(_edge))
        inspectors.append(inspect)

        def mapped(_window: object, _rectangle=rectangle) -> None:
            surface = _window.get_surface()
            if surface is None:
                return
            xid = _configure_surface(GdkX11, surface)
            xids.append(xid)
            x11.XMoveResizeWindow(display, xid, *_rectangle)
            x11.XRaiseWindow(display, xid)
            x11.XFlush(display)

        window.connect("map", mapped)
        windows.append(window)
        window.present()

    if not windows:
        x11.XCloseDisplay(display)
        return 1

    def keep_above() -> bool:
        for xid in xids:
            x11.XRaiseWindow(display, xid)
        # Refresh an offer while somebody deliberately holds it on the edge.
        # This avoids expiring a valid cached drag before portal activation.
        for inspect in inspectors:
            inspect()
        x11.XFlush(display)
        return True

    GLib.timeout_add(500, keep_above)
    try:
        loop.run()
    finally:
        for window in windows:
            window.destroy()
        x11.XCloseDisplay(display)
    return 0


def indicator_main() -> int:
    """Show the animated PowerToys-style file target under a remote pointer."""

    Gdk, GdkX11, GLib, Gtk = _gtk_modules()
    Gtk.init()
    loop = GLib.MainLoop()
    screen_width, screen_height = _display_size()
    x11 = _x11_library()
    display = x11.XOpenDisplay(None)
    if not display:
        return 1
    window = Gtk.Window()
    window.set_title("Mouse Without Borders file drop")
    window.set_decorated(False)
    window.set_focusable(False)
    window.set_focus_on_click(False)
    window.set_default_size(104, 84)
    window.set_opacity(0.94)
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
    box.set_margin_top(9)
    box.set_margin_bottom(8)
    box.set_margin_start(10)
    box.set_margin_end(10)
    image = Gtk.Image.new_from_icon_name("document-save-symbolic")
    image.set_pixel_size(34)
    label = Gtk.Label(label="Drop file")
    box.append(image)
    box.append(label)
    window.set_child(box)
    provider = Gtk.CssProvider()
    provider.load_from_data(
        b"window { background: rgba(36, 105, 201, .96); "
        b"border: 2px solid rgba(255,255,255,.9); border-radius: 16px; "
        b"color: white; } label { font-weight: 700; }"
    )
    Gtk.StyleContext.add_provider_for_display(
        Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
    )
    xid = 0
    tick = 0

    def animate() -> bool:
        nonlocal xid, tick
        surface = window.get_surface()
        if surface is None:
            return True
        if not xid:
            xid = _configure_surface(GdkX11, surface)
        pointer_x, pointer_y = _x11_pointer_position(x11, display)
        bob = (tick % 16) // 4
        tick += 1
        x = min(max(0, pointer_x + 22), max(0, screen_width - 104))
        y = min(max(0, pointer_y + 22 - bob), max(0, screen_height - 84))
        x11.XMoveResizeWindow(display, xid, x, y, 104, 84)
        x11.XRaiseWindow(display, xid)
        x11.XFlush(display)
        window.set_opacity(0.82 + (tick % 12) / 100)
        return True

    window.connect("map", lambda *_args: GLib.timeout_add(40, animate))
    GLib.timeout_add(30_000, lambda: (loop.quit(), False)[1])
    window.present()
    try:
        loop.run()
    finally:
        window.destroy()
        x11.XCloseDisplay(display)
    return 0


def main() -> int:
    Gdk, GdkX11, GLib, Gtk = _gtk_modules()

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
    x11 = _x11_library()
    display = x11.XOpenDisplay(None)
    xid = 0
    tick = 0

    def place_window(*_args: object) -> bool:
        nonlocal xid, tick
        surface = window.get_surface()
        if surface is None or not display:
            return True
        if not xid:
            xid = _configure_surface(GdkX11, surface)
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
