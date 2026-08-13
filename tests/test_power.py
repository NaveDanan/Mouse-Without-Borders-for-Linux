import unittest
from unittest.mock import Mock, patch

from gi.repository import GLib

from mwb_linux.power import (
    GNOME_INHIBIT_IDLE,
    LID_INHIBITOR,
    SLEEP_INHIBITOR,
    PowerManager,
)


def inhibit_connection(*descriptors):
    """Return a logind stub handing out one descriptor per Inhibit call."""

    connection = Mock()
    handles = iter(descriptors)

    def inhibit(*_args, **_kwargs):
        table = Mock()
        table.get.return_value = next(handles)
        return GLib.Variant("(h)", (0,)), table

    connection.call_with_unix_fd_list_sync.side_effect = inhibit
    return connection


def inhibitor_kinds(connection):
    """Return the (what, mode) pair of every Inhibit call in order."""

    return [
        (call.args[4].unpack()[0], call.args[4].unpack()[3])
        for call in connection.call_with_unix_fd_list_sync.call_args_list
    ]


class PowerTests(unittest.TestCase):
    def setUp(self):
        monitor = patch.object(PowerManager, "_start_monitor")
        stop = patch.object(PowerManager, "_stop_monitor")
        self.start_monitor = monitor.start()
        self.stop_monitor = stop.start()
        self.addCleanup(monitor.stop)
        self.addCleanup(stop.stop)

    def test_connected_session_holds_and_releases_logind_sleep_inhibitor(self):
        connection = inhibit_connection(42, 43)
        manager = PowerManager()

        with (
            patch("mwb_linux.power.Gio.bus_get_sync", return_value=connection),
            patch("mwb_linux.power.os.close") as close,
        ):
            manager.set_connected(True, block_lid=True)
            self.assertTrue(manager.sleep_inhibited)
            manager.set_connected(False)

        self.assertFalse(manager.sleep_inhibited)
        self.assertEqual(sorted(call.args[0] for call in close.call_args_list), [42, 43])

    def test_connected_session_blocks_the_lid_switch_and_delays_sleep(self):
        """logind ignores a plain ``sleep`` block lock when the lid closes."""

        connection = inhibit_connection(7, 8)
        manager = PowerManager()

        with patch("mwb_linux.power.Gio.bus_get_sync", return_value=connection):
            manager.set_connected(True, block_lid=True)

        self.assertEqual(
            inhibitor_kinds(connection),
            [(LID_INHIBITOR, "block"), (SLEEP_INHIBITOR, "delay")],
        )
        self.assertTrue(manager.lid_inhibited)
        self.start_monitor.assert_called_once_with()

    def test_lid_handling_can_be_left_to_logind(self):
        connection = inhibit_connection(7, 8)
        manager = PowerManager()

        with patch("mwb_linux.power.Gio.bus_get_sync", return_value=connection):
            manager.set_connected(True)

        self.assertEqual(
            inhibitor_kinds(connection),
            [(SLEEP_INHIBITOR, "block"), (SLEEP_INHIBITOR, "delay")],
        )
        self.assertFalse(manager.lid_inhibited)
        self.assertTrue(manager.sleep_inhibited)

    def test_changing_the_lid_policy_replaces_the_immutable_lock(self):
        connection = inhibit_connection(7, 8, 9)
        manager = PowerManager()

        with (
            patch("mwb_linux.power.Gio.bus_get_sync", return_value=connection),
            patch("mwb_linux.power.os.close") as close,
        ):
            manager.set_connected(True, block_lid=True)
            manager.set_connected(True)

        self.assertEqual(
            inhibitor_kinds(connection),
            [
                (LID_INHIBITOR, "block"),
                (SLEEP_INHIBITOR, "delay"),
                (SLEEP_INHIBITOR, "block"),
            ],
        )
        close.assert_called_once_with(7)
        self.assertFalse(manager.lid_inhibited)

    def test_disabling_block_screen_saver_still_delays_sleep_for_a_clean_exit(self):
        connection = inhibit_connection(11)
        manager = PowerManager()

        with patch("mwb_linux.power.Gio.bus_get_sync", return_value=connection):
            manager.set_connected(True, block_sleep=False)

        self.assertFalse(manager.sleep_inhibited)
        self.assertEqual(inhibitor_kinds(connection), [(SLEEP_INHIBITOR, "delay")])

    def test_suspend_notification_closes_channels_then_frees_the_delay_lock(self):
        order = []
        connection = inhibit_connection(11, 12)
        manager = PowerManager(lambda sleeping: order.append(("callback", sleeping)))

        with (
            patch("mwb_linux.power.Gio.bus_get_sync", return_value=connection),
            patch("mwb_linux.power.os.close", side_effect=lambda fd: order.append(("close", fd))),
        ):
            manager.set_connected(True, block_lid=True)
            manager._on_prepare_for_sleep(
                None, None, None, None, None, GLib.Variant("(b)", (True,))
            )

        self.assertEqual(order, [("callback", True), ("close", 12)])

    def test_resume_notification_retakes_the_delay_lock_and_reconnects(self):
        events = []
        connection = inhibit_connection(11, 12, 13)
        manager = PowerManager(lambda sleeping: events.append(sleeping))

        with (
            patch("mwb_linux.power.Gio.bus_get_sync", return_value=connection),
            patch("mwb_linux.power.os.close"),
        ):
            manager.set_connected(True, block_lid=True)
            manager._on_prepare_for_sleep(
                None, None, None, None, None, GLib.Variant("(b)", (True,))
            )
            manager._on_prepare_for_sleep(
                None, None, None, None, None, GLib.Variant("(b)", (False,))
            )

        self.assertEqual(events, [True, False])
        self.assertEqual(
            inhibitor_kinds(connection),
            [
                (LID_INHIBITOR, "block"),
                (SLEEP_INHIBITOR, "delay"),
                (SLEEP_INHIBITOR, "delay"),
            ],
        )

    def test_closing_an_inhibited_lid_locks_the_session_instead_of_suspending(self):
        connection = inhibit_connection(7, 8)
        manager = PowerManager()

        with patch("mwb_linux.power.Gio.bus_get_sync", return_value=connection):
            manager.set_connected(True, block_lid=True)
            connection.call_sync.side_effect = [
                GLib.Variant("(o)", ("/org/freedesktop/login1/user/_1000",)),
                GLib.Variant(
                    "(v)",
                    (
                        GLib.Variant(
                            "(so)", ("116", "/org/freedesktop/login1/session/_3116")
                        ),
                    ),
                ),
                None,
            ]
            manager._on_manager_properties(
                None,
                None,
                None,
                None,
                None,
                GLib.Variant(
                    "(sa{sv}as)",
                    ("org.freedesktop.login1.Manager", {"LidClosed": GLib.Variant("b", True)}, []),
                ),
            )

        lock = connection.call_sync.call_args_list[-1]
        self.assertEqual(lock.args[1], "/org/freedesktop/login1/session/_3116")
        self.assertEqual(lock.args[2], "org.freedesktop.login1.Session")
        self.assertEqual(lock.args[3], "Lock")

    def test_lid_close_is_left_alone_when_logind_still_owns_the_switch(self):
        connection = inhibit_connection(7, 8)
        manager = PowerManager()

        with patch("mwb_linux.power.Gio.bus_get_sync", return_value=connection):
            manager.set_connected(True)
            manager._on_manager_properties(
                None,
                None,
                None,
                None,
                None,
                GLib.Variant(
                    "(sa{sv}as)",
                    ("org.freedesktop.login1.Manager", {"LidClosed": GLib.Variant("b", True)}, []),
                ),
            )

        connection.call_sync.assert_not_called()

    def test_reopening_and_reclosing_the_lid_locks_again(self):
        manager = PowerManager()
        with (
            patch.object(PowerManager, "lid_inhibited", True),
            patch.object(manager, "_lock_session") as lock,
        ):
            for closed in (True, True, False, True):
                manager._on_manager_properties(
                    None,
                    None,
                    None,
                    None,
                    None,
                    GLib.Variant(
                        "(sa{sv}as)",
                        (
                            "org.freedesktop.login1.Manager",
                            {"LidClosed": GLib.Variant("b", closed)},
                            [],
                        ),
                    ),
                )
        # Only the two closing edges lock; the repeat and the open do not.
        self.assertEqual(lock.call_count, 2)

    def test_never_lock_option_takes_a_gnome_idle_inhibitor(self):
        """A locked GNOME session cannot accept remote input at all."""

        connection = inhibit_connection(7, 8)
        connection.call_sync.return_value = GLib.Variant("(u)", (4242,))
        manager = PowerManager()

        with patch("mwb_linux.power.Gio.bus_get_sync", return_value=connection):
            manager.set_connected(True, block_lock=True)
            self.assertTrue(manager.lock_inhibited)
            inhibit = connection.call_sync.call_args_list[0]
            self.assertEqual(inhibit.args[0], "org.gnome.SessionManager")
            self.assertEqual(inhibit.args[3], "Inhibit")
            self.assertEqual(inhibit.args[4].unpack()[3], GNOME_INHIBIT_IDLE)

            manager.set_connected(False)

        self.assertFalse(manager.lock_inhibited)
        uninhibit = connection.call_sync.call_args_list[-1]
        self.assertEqual(uninhibit.args[3], "Uninhibit")
        self.assertEqual(uninhibit.args[4].unpack()[0], 4242)

    def test_lock_is_left_alone_unless_the_option_is_enabled(self):
        connection = inhibit_connection(7, 8)
        manager = PowerManager()

        with patch("mwb_linux.power.Gio.bus_get_sync", return_value=connection):
            manager.set_connected(True)

        self.assertFalse(manager.lock_inhibited)
        connection.call_sync.assert_not_called()

    def test_lock_and_unlock_signals_are_reported_once_per_transition(self):
        seen = []
        manager = PowerManager(session_locked=seen.append)

        manager._on_session_lock()
        manager._on_session_lock()
        manager._on_session_unlock()
        manager._on_session_unlock()
        manager._on_session_lock()

        self.assertEqual(seen, [True, False, True])
        self.assertTrue(manager.session_locked)

    def test_locked_hint_property_also_drives_the_lock_state(self):
        seen = []
        manager = PowerManager(session_locked=seen.append)

        manager._on_session_properties(
            None,
            None,
            None,
            None,
            None,
            GLib.Variant(
                "(sa{sv}as)",
                (
                    "org.freedesktop.login1.Session",
                    {"LockedHint": GLib.Variant("b", True)},
                    [],
                ),
            ),
        )

        self.assertEqual(seen, [True])

    def test_remote_activity_calls_freedesktop_screensaver(self):
        connection = Mock()
        manager = PowerManager()
        with patch("mwb_linux.power.Gio.bus_get_sync", return_value=connection):
            manager._signal_activity()

        arguments = connection.call_sync.call_args.args
        self.assertEqual(arguments[0], "org.freedesktop.ScreenSaver")
        self.assertEqual(arguments[3], "SimulateUserActivity")

    def test_gnome_not_supported_activity_wakes_active_lock_screen(self):
        connection = Mock()
        connection.call_sync.side_effect = [
            GLib.Error("not supported"),
            GLib.Variant("(b)", (True,)),
            GLib.Variant("(u)", (17,)),
        ]
        manager = PowerManager()

        with patch("mwb_linux.power.Gio.bus_get_sync", return_value=connection):
            manager._signal_activity()

        self.assertEqual(connection.call_sync.call_count, 3)
        activity, active, notification = connection.call_sync.call_args_list
        self.assertEqual(activity.args[3], "SimulateUserActivity")
        self.assertEqual(active.args[0], "org.gnome.ScreenSaver")
        self.assertEqual(active.args[3], "GetActive")
        self.assertEqual(notification.args[0], "org.freedesktop.Notifications")
        self.assertEqual(notification.args[3], "Notify")
        self.assertEqual(manager._wake_notification_id, 17)

    def test_gnome_fallback_does_not_notify_an_awake_desktop(self):
        connection = Mock()
        connection.call_sync.return_value = GLib.Variant("(b)", (False,))
        manager = PowerManager()

        manager._wake_gnome_lock_screen(connection)

        self.assertEqual(connection.call_sync.call_count, 1)
        self.assertEqual(connection.call_sync.call_args.args[3], "GetActive")


if __name__ == "__main__":
    unittest.main()
