"""End-to-end checks that logind signals really drive the power manager.

The suspend and lid paths are pure D-Bus plumbing: a subscription made on a
private main context in a worker thread has to keep delivering signals while
the daemon (which owns no GLib main loop of its own) runs.  Mocks cannot prove
that, so these tests stand up a throwaway bus with a stub ``login1`` on it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch

from gi.repository import Gio, GLib

from mwb_linux.power import PowerManager

LOGIN1_XML = """
<node>
  <interface name='org.freedesktop.login1.Manager'>
    <method name='Inhibit'>
      <arg type='s' direction='in'/>
      <arg type='s' direction='in'/>
      <arg type='s' direction='in'/>
      <arg type='s' direction='in'/>
      <arg type='h' direction='out'/>
    </method>
    <method name='GetUser'>
      <arg type='u' direction='in'/>
      <arg type='o' direction='out'/>
    </method>
    <signal name='PrepareForSleep'><arg type='b'/></signal>
    <property name='LidClosed' type='b' access='read'/>
  </interface>
</node>
"""

USER_XML = """
<node>
  <interface name='org.freedesktop.login1.User'>
    <property name='Display' type='(so)' access='read'/>
  </interface>
</node>
"""

SESSION_XML = """
<node>
  <interface name='org.freedesktop.login1.Session'>
    <method name='Lock'/>
  </interface>
</node>
"""

MANAGER_PATH = "/org/freedesktop/login1"
USER_PATH = "/org/freedesktop/login1/user/_1000"
SESSION_PATH = "/org/freedesktop/login1/session/_3116"


class FakeLogind:
    """A minimal ``org.freedesktop.login1`` implementation on a private bus."""

    def __init__(self) -> None:
        # --nofork keeps the bus as a direct child, so the test owns its
        # lifetime; a forked daemon would outlive every run and pile up.
        self.daemon = subprocess.Popen(
            ["dbus-daemon", "--session", "--print-address", "--nofork"],
            stdout=subprocess.PIPE,
            text=True,
        )
        self.address = self.daemon.stdout.readline().strip()
        if not self.address:
            raise RuntimeError("dbus-daemon did not report a bus address")
        self.locked = threading.Event()
        self._ready = threading.Event()
        self._loop: GLib.MainLoop | None = None
        self.connection: Gio.DBusConnection | None = None
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=30):
            # Own the cleanup here: the caller never gets an object to close,
            # so a failed start would otherwise strand the bus process.
            self._terminate_daemon()
            raise RuntimeError("the login1 stub did not start")

    def _serve(self) -> None:
        # Objects dispatch on whichever main context was thread-default when
        # they were registered, so the stub owns a private one and drives it.
        context = GLib.MainContext.new()
        context.push_thread_default()
        self.connection = Gio.DBusConnection.new_for_address_sync(
            self.address,
            Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
            | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
            None,
            None,
        )
        self.registrations = [
            self.connection.register_object(
                MANAGER_PATH,
                Gio.DBusNodeInfo.new_for_xml(LOGIN1_XML).interfaces[0],
                self._manager_call,
                None,
                None,
            ),
            self.connection.register_object(
                USER_PATH,
                Gio.DBusNodeInfo.new_for_xml(USER_XML).interfaces[0],
                None,
                self._user_property,
                None,
            ),
            self.connection.register_object(
                SESSION_PATH,
                Gio.DBusNodeInfo.new_for_xml(SESSION_XML).interfaces[0],
                self._session_call,
                None,
                None,
            ),
        ]
        # RequestName is synchronous, so the well-known name is owned before
        # any client subscribes; the async helper would need a running loop.
        self.connection.call_sync(
            "org.freedesktop.DBus",
            "/org/freedesktop/DBus",
            "org.freedesktop.DBus",
            "RequestName",
            GLib.Variant("(su)", ("org.freedesktop.login1", 0)),
            GLib.VariantType.new("(u)"),
            Gio.DBusCallFlags.NONE,
            5000,
            None,
        )
        self._loop = GLib.MainLoop.new(context, False)
        started = GLib.idle_source_new()
        started.set_callback(lambda *_: (self._ready.set(), GLib.SOURCE_REMOVE)[1])
        started.attach(context)
        self._loop.run()

    def _manager_call(self, _conn, _sender, _path, _iface, method, _params, invocation):
        if method == "GetUser":
            invocation.return_value(GLib.Variant("(o)", (USER_PATH,)))
        else:
            invocation.return_error_literal(
                Gio.dbus_error_quark(), Gio.DBusError.NOT_SUPPORTED, method
            )

    def _user_property(self, _conn, _sender, _path, _iface, name):
        if name == "Display":
            return GLib.Variant("(so)", ("116", SESSION_PATH))
        return None

    def _session_call(self, _conn, _sender, _path, _iface, method, _params, invocation):
        if method == "Lock":
            self.locked.set()
            invocation.return_value(None)

    def emit_prepare_for_sleep(self, about_to_sleep: bool) -> None:
        self.connection.emit_signal(
            None,
            MANAGER_PATH,
            "org.freedesktop.login1.Manager",
            "PrepareForSleep",
            GLib.Variant("(b)", (about_to_sleep,)),
        )
        self.connection.flush_sync(None)

    def emit_lid_closed(self, closed: bool) -> None:
        self.connection.emit_signal(
            None,
            MANAGER_PATH,
            "org.freedesktop.DBus.Properties",
            "PropertiesChanged",
            GLib.Variant(
                "(sa{sv}as)",
                (
                    "org.freedesktop.login1.Manager",
                    {"LidClosed": GLib.Variant("b", closed)},
                    [],
                ),
            ),
        )
        self.connection.flush_sync(None)

    def client(self) -> Gio.DBusConnection:
        return Gio.DBusConnection.new_for_address_sync(
            self.address,
            Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
            | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
            None,
            None,
        )

    def close(self) -> None:
        if self._loop is not None:
            self._loop.quit()
        self._thread.join(timeout=5)
        if self.connection is not None:
            try:
                self.connection.close_sync(None)
            except GLib.Error:
                pass
        self._terminate_daemon()

    def _terminate_daemon(self) -> None:
        self.daemon.terminate()
        try:
            self.daemon.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.daemon.kill()
            self.daemon.wait(timeout=5)
        if self.daemon.stdout:
            self.daemon.stdout.close()


#: These tests need a real private bus, real GLib main contexts and real
#: worker threads. Sharing an interpreter with the rest of the suite makes
#: GDBus' synchronous connection setup contend with whatever main context and
#: threads earlier tests left behind, which shows up as random timeouts. Run
#: them in a dedicated process instead of weakening what they verify.
CHILD_ENV = "MWB_POWER_SIGNAL_CHILD"


@unittest.skipUnless(shutil.which("dbus-daemon"), "dbus-daemon is unavailable")
@unittest.skipUnless(os.environ.get(CHILD_ENV), "runs in an isolated interpreter")
class PowerSignalTests(unittest.TestCase):
    def setUp(self):
        self.bus = FakeLogind()
        self.addCleanup(self.bus.close)
        self.client = self.bus.client()
        self.addCleanup(self.client.close_sync, None)

    def manager(self, callback=None) -> PowerManager:
        manager = PowerManager(callback)
        # Only the transport is swapped; the subscription, worker thread and
        # private main context are the production ones.
        patcher = patch(
            "mwb_linux.power.Gio.bus_get_sync", return_value=self.client
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(manager.stop)
        manager._start_monitor()
        return manager

    def test_a_real_suspend_signal_reaches_the_daemon_without_a_main_loop(self):
        seen: list[bool] = []
        received = threading.Event()

        def callback(sleeping):
            seen.append(sleeping)
            if len(seen) == 2:
                received.set()

        manager = self.manager(callback)
        manager._connected = True

        # The stub refuses Inhibit, which also proves a logind that denies the
        # delay lock cannot stop the resume path from running.
        with self.assertLogs("mwb_linux.power", level="WARNING"):
            self.bus.emit_prepare_for_sleep(True)
            self.bus.emit_prepare_for_sleep(False)
            self.assertTrue(received.wait(timeout=5), "no PrepareForSleep was delivered")

        self.assertEqual(seen, [True, False])

    def test_a_real_lid_close_locks_the_resolved_graphical_session(self):
        manager = self.manager()
        manager._inhibit_fd = -1
        manager._inhibit_what = "sleep:handle-lid-switch"
        manager._system_bus = self.client

        self.bus.emit_lid_closed(True)

        self.assertTrue(
            self.bus.locked.wait(timeout=5),
            "the lid close did not lock the session",
        )
        manager._inhibit_fd = None

    def test_stopping_the_monitor_ends_its_thread(self):
        manager = self.manager()
        thread = manager._monitor_thread
        self.assertIsNotNone(thread)
        manager._stop_monitor()
        self.assertFalse(thread.is_alive())


@unittest.skipUnless(shutil.which("dbus-daemon"), "dbus-daemon is unavailable")
@unittest.skipIf(os.environ.get(CHILD_ENV), "already the isolated interpreter")
class IsolatedPowerSignalTests(unittest.TestCase):
    def test_logind_signal_integration_suite(self):
        environment = {**os.environ, CHILD_ENV: "1"}
        repository = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        completed = subprocess.run(
            [sys.executable, "-m", "unittest", "-v", "tests.test_power_signals"],
            cwd=repository,
            env=environment,
            capture_output=True,
            text=True,
            timeout=180,
        )
        self.assertEqual(
            completed.returncode,
            0,
            f"isolated logind signal tests failed\n{completed.stdout}\n{completed.stderr}",
        )
        self.assertIn("OK", completed.stderr)


if __name__ == "__main__":
    unittest.main()
