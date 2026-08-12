import unittest
from unittest.mock import Mock, patch

from gi.repository import GLib

from mwb_linux.power import PowerManager


class PowerTests(unittest.TestCase):
    def test_connected_session_holds_and_releases_logind_sleep_inhibitor(self):
        connection = Mock()
        descriptors = Mock()
        descriptors.get.return_value = 42
        connection.call_with_unix_fd_list_sync.return_value = (
            GLib.Variant("(h)", (0,)),
            descriptors,
        )
        manager = PowerManager()

        with (
            patch("mwb_linux.power.Gio.bus_get_sync", return_value=connection),
            patch("mwb_linux.power.os.close") as close,
        ):
            manager.set_connected(True)
            self.assertTrue(manager.sleep_inhibited)
            manager.set_connected(False)

        close.assert_called_once_with(42)
        arguments = connection.call_with_unix_fd_list_sync.call_args.args
        self.assertEqual(arguments[3], "Inhibit")
        self.assertEqual(arguments[4].unpack()[0], "sleep")

    def test_disabling_block_screen_saver_does_not_inhibit_sleep(self):
        manager = PowerManager()
        with patch("mwb_linux.power.Gio.bus_get_sync") as bus:
            manager.set_connected(True, block_sleep=False)
        bus.assert_not_called()
        self.assertFalse(manager.sleep_inhibited)

    def test_remote_activity_calls_freedesktop_screensaver(self):
        connection = Mock()
        manager = PowerManager()
        with patch("mwb_linux.power.Gio.bus_get_sync", return_value=connection):
            manager._signal_activity()

        arguments = connection.call_sync.call_args.args
        self.assertEqual(arguments[0], "org.freedesktop.ScreenSaver")
        self.assertEqual(arguments[3], "SimulateUserActivity")


if __name__ == "__main__":
    unittest.main()
