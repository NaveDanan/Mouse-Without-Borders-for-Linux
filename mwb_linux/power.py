"""Connection-scoped Linux sleep protection and remote activity signaling."""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable

from gi.repository import Gio, GLib

LOGGER = logging.getLogger(__name__)

LOGIN1_NAME = "org.freedesktop.login1"
LOGIN1_PATH = "/org/freedesktop/login1"
LOGIN1_MANAGER = "org.freedesktop.login1.Manager"
LOGIN1_USER = "org.freedesktop.login1.User"
LOGIN1_SESSION = "org.freedesktop.login1.Session"
PROPERTIES_INTERFACE = "org.freedesktop.DBus.Properties"
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

#: ``logind`` splits inhibitors into high-level policy locks and low-level
#: device locks. ``LidSwitchIgnoreInhibited=`` defaults to ``yes``, so a
#: ``sleep`` block lock alone is silently ignored when the lid closes; only the
#: low-level ``handle-lid-switch`` lock is honoured unconditionally.
SLEEP_INHIBITOR = "sleep"
LID_INHIBITOR = "sleep:handle-lid-switch"

SESSION_MANAGER_NAME = "org.gnome.SessionManager"
SESSION_MANAGER_PATH = "/org/gnome/SessionManager"
SESSION_MANAGER_INTERFACE = "org.gnome.SessionManager"
#: ``org.gnome.SessionManager`` inhibit flag 8 marks the session permanently
#: active, which is what stops the screensaver from ever engaging the lock.
GNOME_INHIBIT_IDLE = 8


class PowerManager:
    """Keep a controllable Linux session online while Windows peers use it.

    A fully suspended process cannot receive the mouse packet that would wake
    it, and stock PowerToys does not send Linux Wake-on-LAN magic packets.
    Holding logind inhibitors while connected preserves the TCP and EIS
    sessions while still allowing the display to blank and lock.  Remote input
    separately resets the desktop idle timer so a blank display wakes.

    Three locks cooperate:

    ``sleep`` (block)
        Stops idle and menu initiated suspend.
    ``handle-lid-switch`` (block)
        Stops the lid from suspending the machine behind the ``sleep`` lock's
        back.  The session is locked in software instead, so closing the lid
        still secures the desktop without dropping the peer connection.
    ``sleep`` (delay)
        Guarantees a ``PrepareForSleep`` notification with enough time to close
        the control channels cleanly whenever a suspend does happen anyway.
    """

    def __init__(
        self,
        prepare_for_sleep: Callable[[bool], None] | None = None,
        session_locked: Callable[[bool], None] | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self._inhibit_fd: int | None = None
        self._inhibit_what = ""
        self._delay_fd: int | None = None
        self._idle_cookie: int | None = None
        self._system_bus: Gio.DBusConnection | None = None
        self._session_bus: Gio.DBusConnection | None = None
        self._connected = False
        self._block_lid = False
        self._activity_pending = False
        self._last_activity = 0.0
        self._last_lock_screen_wake = 0.0
        self._wake_notification_id = 0
        self._prepare_for_sleep = prepare_for_sleep or (lambda _sleeping: None)
        self._session_locked_callback = session_locked or (lambda _locked: None)
        self._monitor_thread: threading.Thread | None = None
        self._monitor_loop: GLib.MainLoop | None = None
        self._monitor_ready = threading.Event()
        self._lid_closed = False
        self._session_path = ""
        self.session_locked = False

    @property
    def sleep_inhibited(self) -> bool:
        with self._lock:
            return self._inhibit_fd is not None

    @property
    def lid_inhibited(self) -> bool:
        with self._lock:
            return self._inhibit_fd is not None and self._inhibit_what == LID_INHIBITOR

    def set_connected(
        self,
        connected: bool,
        *,
        block_sleep: bool = True,
        block_lid: bool = False,
        block_lock: bool = False,
    ) -> None:
        self._connected = connected
        self._block_lid = bool(block_sleep and block_lid)
        if connected and block_sleep:
            self._acquire_sleep_inhibitor()
        else:
            self._release_sleep_inhibitor()
        if connected and block_lock:
            self._acquire_idle_inhibitor()
        else:
            self._release_idle_inhibitor()
        if connected:
            # The delay lock and the signal monitor are useful even when the
            # user allows suspend: they turn an abrupt freeze into a clean
            # good-bye and an immediate post-resume rebuild.
            self._start_monitor()
            self._acquire_sleep_delay()
        else:
            self._release_sleep_delay()
            self._stop_monitor()

    @property
    def lock_inhibited(self) -> bool:
        with self._lock:
            return self._idle_cookie is not None

    def _acquire_idle_inhibitor(self) -> None:
        """Stop GNOME idling into the lock screen while a peer is connected.

        A locked session has no remote input at all, so for an actively shared
        desktop the useful protection is never reaching the lock screen.
        """

        with self._lock:
            if self._idle_cookie is not None:
                return
        try:
            connection = self._session_bus or Gio.bus_get_sync(
                Gio.BusType.SESSION, None
            )
            self._session_bus = connection
            cookie = connection.call_sync(
                SESSION_MANAGER_NAME,
                SESSION_MANAGER_PATH,
                SESSION_MANAGER_INTERFACE,
                "Inhibit",
                GLib.Variant(
                    "(susu)",
                    (
                        "Mouse Without Borders",
                        0,
                        "A remote computer is sharing this desktop",
                        GNOME_INHIBIT_IDLE,
                    ),
                ),
                GLib.VariantType.new("(u)"),
                Gio.DBusCallFlags.NONE,
                2000,
                None,
            ).unpack()[0]
        except (GLib.Error, IndexError, TypeError) as exc:
            LOGGER.warning("could not keep the session from locking: %s", exc)
            return
        release = False
        with self._lock:
            if self._connected and self._idle_cookie is None:
                self._idle_cookie = cookie
            else:
                release = True
        if release:
            self._uninhibit_idle(cookie)

    def _release_idle_inhibitor(self) -> None:
        with self._lock:
            cookie = self._idle_cookie
            self._idle_cookie = None
        if cookie is not None:
            self._uninhibit_idle(cookie)

    def _uninhibit_idle(self, cookie: int) -> None:
        try:
            connection = self._session_bus or Gio.bus_get_sync(
                Gio.BusType.SESSION, None
            )
            connection.call_sync(
                SESSION_MANAGER_NAME,
                SESSION_MANAGER_PATH,
                SESSION_MANAGER_INTERFACE,
                "Uninhibit",
                GLib.Variant("(u)", (cookie,)),
                None,
                Gio.DBusCallFlags.NONE,
                2000,
                None,
            )
        except GLib.Error as exc:
            LOGGER.debug("could not release the GNOME idle inhibitor: %s", exc)

    def _inhibit(self, what: str, mode: str, why: str) -> int | None:
        """Take one logind inhibitor lock and return its owning descriptor."""

        try:
            connection = self._system_bus or Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
            reply, descriptors = connection.call_with_unix_fd_list_sync(
                LOGIN1_NAME,
                LOGIN1_PATH,
                LOGIN1_MANAGER,
                "Inhibit",
                GLib.Variant("(ssss)", (what, "Mouse Without Borders", why, mode)),
                GLib.VariantType.new("(h)"),
                Gio.DBusCallFlags.NONE,
                2000,
                None,
                None,
            )
            self._system_bus = connection
            return descriptors.get(reply.unpack()[0])
        except (GLib.Error, OSError, IndexError, TypeError) as exc:
            LOGGER.warning("could not take the %s %s inhibitor: %s", what, mode, exc)
            return None

    def _acquire_sleep_inhibitor(self) -> None:
        wanted = LID_INHIBITOR if self._block_lid else SLEEP_INHIBITOR
        with self._lock:
            if self._inhibit_fd is not None and self._inhibit_what == wanted:
                return
        # The lock set is immutable once taken, so switching the lid policy
        # means replacing the descriptor rather than editing it.
        self._release_sleep_inhibitor()
        descriptor = self._inhibit(
            wanted,
            "block",
            "Keep remote mouse, keyboard, and network control available",
        )
        if descriptor is None:
            return
        with self._lock:
            if self._connected and self._inhibit_fd is None:
                self._inhibit_fd = descriptor
                self._inhibit_what = wanted
                descriptor = -1
        if descriptor >= 0:
            os.close(descriptor)

    def _release_sleep_inhibitor(self) -> None:
        with self._lock:
            descriptor = self._inhibit_fd
            self._inhibit_fd = None
            self._inhibit_what = ""
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass

    def _acquire_sleep_delay(self) -> None:
        with self._lock:
            if self._delay_fd is not None:
                return
        descriptor = self._inhibit(
            SLEEP_INHIBITOR, "delay", "Close remote control channels before sleeping"
        )
        if descriptor is None:
            return
        with self._lock:
            if self._connected and self._delay_fd is None:
                self._delay_fd = descriptor
                descriptor = -1
        if descriptor >= 0:
            os.close(descriptor)

    def _release_sleep_delay(self) -> None:
        with self._lock:
            descriptor = self._delay_fd
            self._delay_fd = None
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass

    def _start_monitor(self) -> None:
        """Watch logind for suspend and lid events on a private main context."""

        with self._lock:
            if self._monitor_thread is not None and self._monitor_thread.is_alive():
                return
            self._monitor_ready.clear()
            self._monitor_thread = threading.Thread(
                target=self._monitor, name="mwb-power-monitor", daemon=True
            )
            thread = self._monitor_thread
        thread.start()
        # A caller that immediately suspends must not race the subscription.
        self._monitor_ready.wait(timeout=2.0)

    def _stop_monitor(self) -> None:
        with self._lock:
            loop = self._monitor_loop
            thread = self._monitor_thread
            self._monitor_loop = None
            self._monitor_thread = None
        if loop is not None:
            loop.quit()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)

    def _monitor(self) -> None:
        subscriptions: list[int] = []
        connection: Gio.DBusConnection | None = None
        try:
            # GDBus delivers signals on the main context that was
            # thread-default when subscribing, so the daemon gets logind
            # events without ever owning the process-wide default context.
            context = GLib.MainContext.new()
            context.push_thread_default()
            connection = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
            subscriptions.append(
                connection.signal_subscribe(
                    LOGIN1_NAME,
                    LOGIN1_MANAGER,
                    "PrepareForSleep",
                    LOGIN1_PATH,
                    None,
                    Gio.DBusSignalFlags.NONE,
                    self._on_prepare_for_sleep,
                )
            )
            subscriptions.append(
                connection.signal_subscribe(
                    LOGIN1_NAME,
                    PROPERTIES_INTERFACE,
                    "PropertiesChanged",
                    LOGIN1_PATH,
                    None,
                    Gio.DBusSignalFlags.NONE,
                    self._on_manager_properties,
                )
            )
            # Lock and unlock decide whether portal input can exist at all, so
            # watch both the explicit signals and the authoritative hint.
            session_path = self._graphical_session_path()
            if session_path:
                subscriptions.append(
                    connection.signal_subscribe(
                        LOGIN1_NAME,
                        LOGIN1_SESSION,
                        "Lock",
                        session_path,
                        None,
                        Gio.DBusSignalFlags.NONE,
                        self._on_session_lock,
                    )
                )
                subscriptions.append(
                    connection.signal_subscribe(
                        LOGIN1_NAME,
                        LOGIN1_SESSION,
                        "Unlock",
                        session_path,
                        None,
                        Gio.DBusSignalFlags.NONE,
                        self._on_session_unlock,
                    )
                )
                subscriptions.append(
                    connection.signal_subscribe(
                        LOGIN1_NAME,
                        PROPERTIES_INTERFACE,
                        "PropertiesChanged",
                        session_path,
                        None,
                        Gio.DBusSignalFlags.NONE,
                        self._on_session_properties,
                    )
                )
            loop = GLib.MainLoop.new(context, False)
            with self._lock:
                self._monitor_loop = loop
            # g_main_loop_run() sets is_running itself, so a quit() that lands
            # between here and the first iteration would be swallowed and the
            # thread would never exit. Report readiness from inside the loop.
            started = GLib.idle_source_new()
            started.set_callback(self._monitor_started)
            started.attach(context)
            loop.run()
        except GLib.Error as exc:
            LOGGER.warning("logind power monitoring unavailable: %s", exc)
        finally:
            self._monitor_ready.set()
            if connection is not None:
                for subscription in subscriptions:
                    try:
                        connection.signal_unsubscribe(subscription)
                    except (GLib.Error, TypeError):
                        pass

    def _monitor_started(self, *_args) -> bool:
        self._monitor_ready.set()
        return GLib.SOURCE_REMOVE

    def _on_prepare_for_sleep(self, _connection, _sender, _path, _iface, _signal, parameters) -> None:
        try:
            about_to_sleep = bool(parameters.unpack()[0])
        except (IndexError, TypeError):
            return
        if about_to_sleep:
            LOGGER.info("system is suspending; closing remote control channels")
            try:
                self._prepare_for_sleep(True)
            except Exception as exc:
                LOGGER.warning("pre-suspend shutdown failed: %s", exc)
            finally:
                # Never hold the delay lock longer than the work needs; the
                # system should not wait out InhibitDelayMaxSec on our account.
                self._release_sleep_delay()
            return
        LOGGER.info("system resumed; rebuilding remote control channels")
        if self._connected:
            self._acquire_sleep_delay()
        try:
            self._prepare_for_sleep(False)
        except Exception as exc:
            LOGGER.warning("post-resume recovery failed: %s", exc)

    def _on_manager_properties(self, _connection, _sender, _path, _iface, _signal, parameters) -> None:
        try:
            _interface, changed, _invalidated = parameters.unpack()
        except (ValueError, TypeError):
            return
        if "LidClosed" not in changed:
            return
        closed = bool(changed["LidClosed"])
        previously_closed = self._lid_closed
        self._lid_closed = closed
        if not closed or previously_closed:
            return
        if not self.lid_inhibited:
            # logind still owns the lid, so it applies the user's own policy.
            return
        # We suppressed logind's suspend, so the security half of closing the
        # lid is now ours to honour.
        self._lock_session()

    def _lock_session(self) -> None:
        session_path = self._graphical_session_path()
        if not session_path:
            return
        try:
            connection = self._system_bus or Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
            connection.call_sync(
                LOGIN1_NAME,
                session_path,
                LOGIN1_SESSION,
                "Lock",
                None,
                None,
                Gio.DBusCallFlags.NONE,
                2000,
                None,
            )
            LOGGER.info("lid closed; locked the session and stayed connected")
        except (GLib.Error, IndexError, TypeError) as exc:
            LOGGER.warning("could not lock the session on lid close: %s", exc)

    def _graphical_session_path(self) -> str:
        """Resolve this user's graphical logind session object path.

        The daemon runs under ``user@.service`` rather than inside the session
        scope, so ``GetSessionByPID`` does not apply; the user object's
        ``Display`` property is the supported route.
        """

        if self._session_path:
            return self._session_path
        try:
            connection = self._system_bus or Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
            self._system_bus = connection
            user_path = connection.call_sync(
                LOGIN1_NAME,
                LOGIN1_PATH,
                LOGIN1_MANAGER,
                "GetUser",
                GLib.Variant("(u)", (os.getuid(),)),
                GLib.VariantType.new("(o)"),
                Gio.DBusCallFlags.NONE,
                2000,
                None,
            ).unpack()[0]
            display = connection.call_sync(
                LOGIN1_NAME,
                user_path,
                PROPERTIES_INTERFACE,
                "Get",
                GLib.Variant("(ss)", (LOGIN1_USER, "Display")),
                GLib.VariantType.new("(v)"),
                Gio.DBusCallFlags.NONE,
                2000,
                None,
            ).unpack()[0]
            self._session_path = str(display[1])
        except (GLib.Error, IndexError, TypeError) as exc:
            LOGGER.warning("could not resolve the graphical session: %s", exc)
            return ""
        return self._session_path

    def _on_session_lock(self, *_args) -> None:
        self._report_session_locked(True)

    def _on_session_unlock(self, *_args) -> None:
        self._report_session_locked(False)

    def _on_session_properties(self, _connection, _sender, _path, _iface, _signal, parameters) -> None:
        try:
            _interface, changed, _invalidated = parameters.unpack()
        except (ValueError, TypeError):
            return
        if "LockedHint" not in changed:
            return
        self._report_session_locked(bool(changed["LockedHint"]))

    def _report_session_locked(self, locked: bool) -> None:
        if locked == self.session_locked:
            return
        self.session_locked = locked
        LOGGER.info("session %s", "locked" if locked else "unlocked")
        try:
            self._session_locked_callback(locked)
        except Exception as exc:
            LOGGER.warning("session lock callback failed: %s", exc)

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
        self._block_lid = False
        self._release_sleep_inhibitor()
        self._release_idle_inhibitor()
        self._release_sleep_delay()
        self._stop_monitor()
        self._system_bus = None
        self._session_bus = None
        self._wake_notification_id = 0
