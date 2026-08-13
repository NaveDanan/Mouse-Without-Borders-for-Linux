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
GNOME_SCREENSAVER_NAME = "org.gnome.ScreenSaver"
GNOME_SCREENSAVER_PATH = "/org/gnome/ScreenSaver"
GNOME_SCREENSAVER_INTERFACE = "org.gnome.ScreenSaver"
NOTIFICATIONS_NAME = "org.freedesktop.Notifications"
NOTIFICATIONS_PATH = "/org/freedesktop/Notifications"
NOTIFICATIONS_INTERFACE = "org.freedesktop.Notifications"
DESKTOP_ID = "io.github.NaveDanan.MouseWithoutBorders"
ACTIVITY_INTERVAL = 0.5
LOCK_SCREEN_WAKE_INTERVAL = 5.0


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
        self._last_lock_screen_wake = 0.0
        self._wake_notification_id = 0

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
            self._session_bus = connection
            try:
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
            except GLib.Error as exc:
                # GNOME exposes this freedesktop compatibility method but
                # deliberately returns NotSupported. A transient notification
                # is its supported, lock-preserving path to the shell's
                # WakeUpScreen signal. Only use it while the screen shield is
                # active, and rate-limit it while pointer packets are flowing.
                LOGGER.debug("desktop activity signal unavailable: %s", exc)
                self._wake_gnome_lock_screen(connection)
        except GLib.Error as exc:
            LOGGER.debug("desktop wake signal unavailable: %s", exc)
        finally:
            with self._lock:
                self._activity_pending = False

    def _wake_gnome_lock_screen(self, connection: Gio.DBusConnection) -> None:
        now = time.monotonic()
        with self._lock:
            if now - self._last_lock_screen_wake < LOCK_SCREEN_WAKE_INTERVAL:
                return
        try:
            active = connection.call_sync(
                GNOME_SCREENSAVER_NAME,
                GNOME_SCREENSAVER_PATH,
                GNOME_SCREENSAVER_INTERFACE,
                "GetActive",
                None,
                GLib.VariantType.new("(b)"),
                Gio.DBusCallFlags.NONE,
                500,
                None,
            )
            if not active.unpack()[0]:
                return
            with self._lock:
                # Rate-limit attempts too: notification policy or a desktop
                # without a notification daemon must not turn pointer traffic
                # into repeated failing D-Bus calls.
                self._last_lock_screen_wake = now
            notification = connection.call_sync(
                NOTIFICATIONS_NAME,
                NOTIFICATIONS_PATH,
                NOTIFICATIONS_INTERFACE,
                "Notify",
                GLib.Variant(
                    "(susssasa{sv}i)",
                    (
                        "Mouse Without Borders",
                        self._wake_notification_id,
                        DESKTOP_ID,
                        "Remote input received",
                        "The screen remains locked.",
                        [],
                        {
                            "desktop-entry": GLib.Variant("s", DESKTOP_ID),
                            "transient": GLib.Variant("b", True),
                        },
                        1500,
                    ),
                ),
                GLib.VariantType.new("(u)"),
                Gio.DBusCallFlags.NONE,
                750,
                None,
            )
            with self._lock:
                self._wake_notification_id = notification.unpack()[0]
        except (GLib.Error, TypeError, IndexError) as exc:
            LOGGER.debug("GNOME lock-screen wake unavailable: %s", exc)

    def stop(self) -> None:
        self._connected = False
        self._release_sleep_inhibitor()
        self._system_bus = None
        self._session_bus = None
        self._wake_notification_id = 0
