import json
import os
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from mwb_linux.config import Config, default_runtime_socket
from mwb_linux.service import MouseWithoutBordersService, control_request
from mwb_linux.protocol import Packet, PackageType


class ServiceTests(unittest.TestCase):
    def test_quit_acknowledges_before_scheduling_guaranteed_shutdown(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            Config(auto_connect=False).save(config_path)
            service = MouseWithoutBordersService(config_path)
            worker = Mock()
            client, server = socket.socketpair()
            self.addCleanup(client.close)
            client.sendall(b'{"command":"quit"}\n')

            with patch("mwb_linux.service.threading.Thread", return_value=worker) as thread:
                service._handle_control_client(server)

            with client.makefile("rb") as stream:
                response = json.loads(stream.readline())

            self.assertEqual(response, {"ok": True})
            self.assertTrue(service._stop.is_set())
            thread.assert_called_once_with(
                target=service.stop,
                name="mwb-service-shutdown",
                daemon=False,
            )
            worker.start.assert_called_once_with()

    def test_top_bar_exit_stops_every_sharing_path_and_parks_permission(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            Config(auto_connect=False).save(config_path)
            service = MouseWithoutBordersService(config_path)
            input_manager = Mock()
            connection = Mock()
            clipboard = Mock()
            service.input = input_manager
            service.connection = connection
            service.clipboard = clipboard

            service.exit_ui()

            input_manager.pause.assert_called_once_with()
            connection.stop.assert_called_once_with()
            clipboard.stop.assert_called_once_with()
            self.assertIsNone(service.connection)
            self.assertIsNone(service.clipboard)
            self.assertFalse(service._connection_requested)
            self.assertTrue(service.status()["ui_exited"])
            self.assertEqual(service.status()["state"], "dormant")

    def test_ui_relaunch_resumes_only_a_service_parked_by_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            Config(auto_connect=False).save(config_path)
            service = MouseWithoutBordersService(config_path)
            with patch.object(service, "connect") as connect:
                service.resume_ui()
                connect.assert_not_called()
                service._ui_exited = True
                service.resume_ui()
                connect.assert_called_once_with()

    def test_windows_mouse_packets_keep_the_physical_controller_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            Config(
                host="windows",
                secret="0123456789abcdef",
                machine_name="linux",
                machine_id=100,
                auto_connect=False,
            ).save(config_path)
            service = MouseWithoutBordersService(config_path)
            service.input = Mock()
            packet = Packet()
            packet.type = PackageType.MOUSE
            packet.src = 200
            packet.dest = 100
            packet.mouse = (1, 2, 3, 0x200)

            service._process_packet(Mock(), packet)

            service.input.inject_mouse.assert_called_once_with(
                1, 2, 3, 0x200, source_id=200
            )

    def test_next_machine_packet_is_forwarded_to_linux_controller(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            Config(
                host="windows",
                secret="0123456789abcdef",
                machine_name="linux",
                machine_id=100,
                auto_connect=False,
            ).save(config_path)
            service = MouseWithoutBordersService(config_path)
            service.input = Mock()
            packet = Packet()
            packet.type = PackageType.NEXT_MACHINE
            packet.src = 200
            packet.dest = 100
            packet.mouse = (800, 32000, 100, 0)

            service._process_packet(Mock(), packet)

            service.input.follow_next_machine.assert_called_once_with(
                100, 800, 32000, 200
            )

    def test_hide_mouse_does_not_release_the_unrelated_capture_role(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            Config(host="windows", secret="0123456789abcdef", auto_connect=False).save(
                config_path
            )
            service = MouseWithoutBordersService(config_path)
            service.input = Mock(remote_active=True, active_remote_name="windows")
            packet = Packet()
            packet.type = PackageType.HIDE_MOUSE

            service._process_packet(Mock(), packet)

            service.input.release_local.assert_not_called()

    def test_settings_save_preserves_learned_wake_on_lan_mac(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            Config(
                secret="0123456789abcdef",
                machine_name="linux",
                machine_id=100,
                remote_machines=[
                    {
                        "name": "windows",
                        "address": "192.168.1.20",
                        "mac": "aa:bb:cc:dd:ee:ff",
                    }
                ],
                machine_matrix=["linux", "windows", "", ""],
                auto_connect=False,
            ).save(config_path)
            service = MouseWithoutBordersService(config_path)

            with patch.object(service, "_replace_runtime_config") as replace:
                service.update_config(
                    {
                        "remote_machines": [
                            {"name": "windows", "address": "192.168.1.21"}
                        ]
                    }
                )

            candidate = replace.call_args.args[0]
            self.assertEqual(candidate.remote_machines[0]["mac"], "aa:bb:cc:dd:ee:ff")
            self.assertEqual(
                Config.load(config_path).remote_machines[0]["mac"],
                "aa:bb:cc:dd:ee:ff",
            )

    def test_concurrent_stop_is_serialized_and_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            Config(auto_connect=False).save(config_path)
            service = MouseWithoutBordersService(config_path)
            entered = threading.Event()
            release = threading.Event()

            def slow_stop_components():
                entered.set()
                self.assertTrue(release.wait(2))

            with patch.object(
                service, "_stop_components", side_effect=slow_stop_components
            ) as stop_components:
                first = threading.Thread(target=service.stop)
                second = threading.Thread(target=service.stop)
                first.start()
                self.assertTrue(entered.wait(2))
                second.start()
                release.set()
                first.join(2)
                second.join(2)

            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
            stop_components.assert_called_once_with()

    def test_packet_ids_are_deduplicated_per_source_machine(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            Config(
                host="windows",
                secret="0123456789abcdef",
                machine_name="linux",
                machine_id=100,
                auto_connect=False,
            ).save(config_path)
            service = MouseWithoutBordersService(config_path)
            service.input = Mock()
            peer = Mock()

            def keyboard(source):
                packet = Packet()
                packet.type = PackageType.KEYBOARD
                packet.packet_id = 7
                packet.src = source
                packet.dest = 100
                packet.keyboard = (0x41, 0)
                return packet

            service._process_packet(peer, keyboard(200))
            service._process_packet(peer, keyboard(300))
            service._process_packet(peer, keyboard(200))

            self.assertEqual(service.input.inject_keyboard.call_count, 2)

    def test_losing_the_active_peer_releases_input_capture(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            Config(host="windows", secret="0123456789abcdef", auto_connect=False).save(
                config_path
            )
            service = MouseWithoutBordersService(config_path)
            service.input = Mock(remote_active=True, active_remote_name="windows")
            service.connection = Mock(connected=True, peers=(Mock(),))
            service.connection.peer_id.return_value = None

            service._connection_status(
                "connected", "1 connected; windows connection closed"
            )

            service.input.recover_active_peer.assert_called_once_with()

            service.input.reset_mock()
            service.connection.peer_id.return_value = 200
            service._connection_status("connected", "Connected to windows")
            service.input.recover_active_peer.assert_not_called()

    def test_switch_machine_routes_remote_local_and_rejects_empty_slot(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            Config(
                host="windows",
                host_name="windows",
                secret="0123456789abcdef",
                machine_name="linux",
                machine_id=100,
                machine_matrix=["linux", "windows", "", ""],
                auto_connect=False,
            ).save(config_path)
            service = MouseWithoutBordersService(config_path)
            service.input = Mock()

            service.switch_machine(2)
            service.input.switch_remote.assert_called_once_with("windows")
            service.switch_machine(1)
            service.input.release_local.assert_called_once_with()
            with self.assertRaisesRegex(ValueError, "empty"):
                service.switch_machine(3)

    def test_disconnect_stops_runtime_and_connect_rebuilds_it(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            Config(
                host="windows",
                secret="0123456789abcdef",
                auto_connect=False,
            ).save(config_path)
            service = MouseWithoutBordersService(config_path)
            old_connection = Mock()
            old_connection.connected = True
            old_clipboard = Mock()
            old_input = Mock()
            service.connection = old_connection
            service.clipboard = old_clipboard
            service.input = old_input
            service._features_started = True

            service.disconnect()

            old_input.release_local.assert_called_once_with()
            old_input.stop.assert_not_called()
            old_clipboard.stop.assert_not_called()
            old_connection.stop.assert_called_once_with()
            self.assertIs(service.input, old_input)
            self.assertIs(service.clipboard, old_clipboard)
            self.assertIsNone(service.connection)
            self.assertEqual(service.status()["state"], "disconnected")
            self.assertFalse(service._connection_requested)

            replacement = Mock()
            replacement.connected = False

            def rebuild_components():
                service.connection = replacement

            with patch.object(
                service, "_start_components", side_effect=rebuild_components
            ):
                service.connect()

            self.assertTrue(service._connection_requested)
            replacement.start.assert_called_once_with()

    def test_config_update_preserves_connection_intent(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            Config(
                host="windows",
                secret="0123456789abcdef",
                auto_connect=False,
            ).save(config_path)
            service = MouseWithoutBordersService(config_path)
            service._connection_requested = True

            with (
                patch.object(service, "_apply_shortcuts") as apply_shortcuts,
                patch.object(service, "_replace_runtime_config") as replace_config,
            ):
                service.update_config({"port": 15102})

            self.assertTrue(service._connection_requested)
            replacement = replace_config.call_args.args[0]
            self.assertEqual(replacement.port, 15102)
            apply_shortcuts.assert_called_once_with(replacement)

    def test_explicit_reconnect_ignores_disabled_startup_preference(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            Config(
                host="windows",
                secret="0123456789abcdef",
                auto_connect=False,
            ).save(config_path)
            service = MouseWithoutBordersService(config_path)

            with patch.object(service, "_replace_runtime_config") as replace_config:
                service.reconnect()

            self.assertTrue(service._connection_requested)
            replace_config.assert_called_once()

    def test_rejected_config_update_does_not_mutate_live_state(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            Config(host="original", port=15100, auto_connect=False).save(config_path)
            service = MouseWithoutBordersService(config_path)

            with self.assertRaisesRegex(ValueError, "base port"):
                service.update_config({"host": "changed", "port": 0})

            self.assertEqual(service.config.host, "original")
            self.assertEqual(service.config.port, 15100)
            self.assertEqual(Config.load(config_path).host, "original")

    def test_duplicate_daemon_cannot_steal_the_control_socket(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / "runtime"
            config_home = root / "config"
            runtime.mkdir(mode=0o700)
            environment = {
                "XDG_RUNTIME_DIR": str(runtime),
                "XDG_CONFIG_HOME": str(config_home),
            }
            with patch.dict(os.environ, environment):
                config_path = config_home / "powertoys-mwb-linux" / "config.json"
                Config(auto_connect=False).save(config_path)
                first = MouseWithoutBordersService(config_path)
                duplicate = MouseWithoutBordersService(config_path)
                try:
                    first.start()
                    self.assertTrue(default_runtime_socket().exists())
                    self.assertEqual(
                        control_request("status")["status"]["state"], "unconfigured"
                    )

                    with self.assertRaisesRegex(OSError, "another .* service is running"):
                        duplicate.start()
                    duplicate.stop()

                    self.assertTrue(default_runtime_socket().exists())
                    self.assertTrue(control_request("status")["ok"])
                finally:
                    first.stop()
                    duplicate.stop()

                self.assertFalse(default_runtime_socket().exists())


if __name__ == "__main__":
    unittest.main()
