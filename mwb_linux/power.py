"""Connection-scoped Linux sleep protection and remote activity signaling."""

from __future__ import annotations

import logging
import os
import threading
import time

from gi.repository import Gio, GLib

LOGGER = logging.getLogger(__name__)

LOGIN1_NAME = "org.freedesktop.login1"
LOGIN1_PATH = "/org/freedesktop/login1"
LOGIN1_MANAGER = "org.freedesktop.login1.Manager"
SCREENSAVER_NAME = "org.freedesktop.ScreenSaver"
SCREENSAVER_PATH = "/org/freedesktop/ScreenSaver"
SCREENSAVER_INTERFACE = "org.freedesktop.ScreenSaver"
ACTIVITY_INTERVAL = 0.5


class PowerManager:
    """Keep a controllable Linux session online while Windows peers use it.

    A fully suspended process cannot receive the mouse packet that would wake
    it, and stock PowerToys does not send Linux Wake-on-LAN magic packets.
    Holding a logind sleep inhibitor while connected preserves the TCP and EIS
    sessions while still allowing the display to blank and lock.  Remote input
    separately resets the desktop idle timer so a blank display wakes.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._inhibit_fd: int | None = None
        self._system_bus: Gio.DBusConnection | None = None
        self._session_bus: Gio.DBusConnection | None = None
        self._connected = False
        self._activity_pending = False
        self._last_activity = 0.0

    @property
    def sleep_inhibited(self) -> bool:
        with self._lock:
            return self._inhibit_fd is not None

    def set_connected(self, connected: bool, *, block_sleep: bool = True) -> None:
        self._connected = connected
        if connected and block_sleep:
            self._acquire_sleep_inhibitor()
        else:
            self._release_sleep_inhibitor()

    def _acquire_sleep_inhibitor(self) -> None:
        with self._lock:
            if self._inhibit_fd is not None:
                return
        try:
            connection = self._system_bus or Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
            reply, descriptors = connection.call_with_unix_fd_list_sync(
                LOGIN1_NAME,
                LOGIN1_PATH,
                LOGIN1_MANAGER,
                "Inhibit",
                GLib.Variant(
                    "(ssss)",
                    (
                        "sleep",
                        "Mouse Without Borders",
                        "Keep remote mouse, keyboard, and network control available",
                        "block",
                    ),
                ),
                GLib.VariantType.new("(h)"),
                Gio.DBusCallFlags.NONE,
                2000,
                None,
                None,
            )
            descriptor = descriptors.get(reply.unpack()[0])
            with self._lock:
                if self._connected and self._inhibit_fd is None:
                    self._system_bus = connection
                    self._inhibit_fd = descriptor
                    descriptor = -1
            if descriptor >= 0:
                os.close(descriptor)
        except (GLib.Error, OSError, IndexError, TypeError) as exc:
            LOGGER.warning("could not inhibit sleep while connected: %s", exc)

    def _release_sleep_inhibitor(self) -> None:
        with self._lock:
            descriptor = self._inhibit_fd
            self._inhibit_fd = None
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass

    def remote_activity(self) -> None:
        """Coalesce remote input and wake/reset the desktop idle timer."""

        now = time.monotonic()
        with self._lock:
            if self._activity_pending or now - self._last_activity < ACTIVITY_INTERVAL:
                return
            self._activity_pending = True
            self._last_activity = now
        threading.Thread(
            target=self._signal_activity,
            name="mwb-remote-activity",
            daemon=True,
        ).start()

    def _signal_activity(self) -> None:
        try:
            connection = self._session_bus or Gio.bus_get_sync(Gio.BusType.SESSION, None)
            connection.call_sync(
                SCREENSAVER_NAME,
                SCREENSAVER_PATH,
                SCREENSAVER_INTERFACE,
                "SimulateUserActivity",
                None,
                None,
                Gio.DBusCallFlags.NONE,
                750,
                None,
            )
            self._session_bus = connection
        except GLib.Error as exc:
            # Injected EIS movement still wakes compositors that do not expose
            # the freedesktop screensaver interface.
            LOGGER.debug("desktop activity signal unavailable: %s", exc)
        finally:
            with self._lock:
                self._activity_pending = False

    def stop(self) -> None:
        self._connected = False
        self._release_sleep_inhibitor()
        self._system_bus = None
        self._session_bus = None
