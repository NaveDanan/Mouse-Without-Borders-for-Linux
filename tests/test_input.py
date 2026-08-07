import unittest
from unittest.mock import Mock, patch

from mwb_linux.config import Config
from mwb_linux.input import (
    InputManager,
    WINDOWS_EPOCH_TICKS,
    WM_LBUTTONDOWN,
    WM_MOUSEMOVE,
    WM_MOUSEWHEEL,
    WM_XBUTTONDOWN,
    capture_targets,
    portal_environment,
)
from mwb_linux.protocol import PackageType


class FakeBridge:
    def __init__(self):
        self.commands = []

    def start(self, *args, **kwargs):
        self.commands.append(("start", args, kwargs))

    def command(self, command, **kwargs):
        self.commands.append((command, kwargs))

    def stop(self):
        self.commands.append(("stop", {}))


class InputTests(unittest.TestCase):
    def test_capture_targets_follow_adjacent_matrix_computers(self):
        config = Config(
            machine_name="linux",
            machine_id=10,
            remote_machines=[
                {"name": "right", "address": "10.0.0.2"},
                {"name": "bottom", "address": "10.0.0.3"},
            ],
            machine_matrix=["linux", "right", "bottom", ""],
            two_row=True,
            host_position="right",
            host_zone=[0, 0, 1920, 1080],
        )
        self.assertEqual(
            capture_targets(config),
            [
                {
                    "edge": "right",
                    "target": "right",
                    "zone": [0, 0, 1920, 1080],
                },
                {"edge": "bottom", "target": "bottom"},
            ],
        )

    def test_non_adjacent_shortcut_keeps_exact_target_through_capture_entry(self):
        packets = []
        messages = []
        config = Config(
            machine_name="linux",
            machine_id=10,
            remote_machines=[
                {"name": "middle", "address": "10.0.0.2"},
                {"name": "far", "address": "10.0.0.3"},
            ],
            machine_matrix=["linux", "middle", "far", ""],
        )
        manager = InputManager(
            config,
            packets.append,
            lambda name=None: 30 if name == "far" else None,
            messages.append,
            bridge=FakeBridge(),
        )

        with patch.object(manager, "_trigger_edge", return_value=True):
            manager.switch_remote("far")

        self.assertEqual(manager._first_matrix_hop("far"), "middle")
        self.assertEqual(manager._pending_remote_name, "far")
        manager._bridge_event(
            {
                "type": "event",
                "event": "capture_activated",
                "target": "middle",
                "edge": "right",
            }
        )
        manager._bridge_event(
            {"type": "event", "event": "pointer_motion", "dx": 1, "dy": 0}
        )

        self.assertTrue(manager.remote_active)
        self.assertEqual(manager.active_remote_name, "far")
        self.assertEqual(manager._pending_remote_name, "")
        self.assertEqual(packets[-1].dest, 30)
        self.assertEqual(messages[-1], "Controlling far")

    def test_portal_permission_is_reused_across_network_reconnects(self):
        bridge = FakeBridge()
        manager = InputManager(
            Config(machine_name="linux", machine_id=10),
            lambda _packet: None,
            lambda: 20,
            lambda _message: None,
            bridge=bridge,
        )

        manager.start()
        manager.start()
        self.assertEqual(
            [command[0] for command in bridge.commands].count("start"), 1
        )

        manager.stop()
        manager.start()
        self.assertEqual(
            [command[0] for command in bridge.commands].count("start"), 2
        )

    def test_capture_restore_token_is_saved_for_portal_v2_relaunches(self):
        persisted = []
        config = Config(machine_name="linux", machine_id=10)
        manager = InputManager(
            config,
            lambda _packet: None,
            lambda: 20,
            lambda _message: None,
            lambda: persisted.append(True),
            bridge=FakeBridge(),
        )

        manager._bridge_event(
            {
                "type": "response",
                "id": "capture",
                "ok": True,
                "result": {"restore_token": "capture-token", "zones": []},
            }
        )

        self.assertEqual(config.capture_restore_token, "capture-token")
        self.assertEqual(len(persisted), 1)

    def test_capture_is_immediately_released_without_a_connected_peer(self):
        bridge = FakeBridge()
        messages = []
        manager = InputManager(
            Config(machine_name="linux", machine_id=10),
            lambda _packet: None,
            lambda: None,
            messages.append,
            bridge=bridge,
        )

        manager._bridge_event({"type": "event", "event": "capture_activated"})

        self.assertFalse(manager.remote_active)
        self.assertEqual(bridge.commands[-1][0], "capture_release")
        self.assertEqual(messages[-1], "Cannot switch: no connected host")

    def test_portal_helper_does_not_inherit_the_calling_ide_identity(self):
        with patch.dict(
            "os.environ",
            {
                "GIO_LAUNCHED_DESKTOP_FILE": "/tmp/t3-code.desktop",
                "GIO_LAUNCHED_DESKTOP_FILE_PID": "123",
            },
        ):
            environment = portal_environment()

        self.assertIn("MouseWithoutBorders", environment["GIO_LAUNCHED_DESKTOP_FILE"])
        self.assertNotEqual(environment["GIO_LAUNCHED_DESKTOP_FILE_PID"], "123")

    def test_captured_key_and_mouse_packets(self):
        packets = []
        bridge = FakeBridge()
        manager = InputManager(
            Config(machine_name="linux", machine_id=10),
            packets.append,
            lambda: 20,
            lambda _message: None,
            bridge=bridge,
        )
        manager._activate_remote()
        manager._bridge_event(
            {"type": "event", "event": "key", "keycode": 30, "state": "pressed"}
        )
        manager._bridge_event(
            {"type": "event", "event": "pointer_motion", "dx": 10, "dy": 5}
        )
        manager._bridge_event(
            {"type": "event", "event": "button", "button": 272, "state": "pressed"}
        )
        self.assertEqual([packet.type for packet in packets], [
            PackageType.KEYBOARD,
            PackageType.MOUSE,
            PackageType.MOUSE,
        ])
        self.assertEqual(packets[0].keyboard, (0x41, 0))
        self.assertGreater(packets[0].timestamp, WINDOWS_EPOCH_TICKS)
        self.assertEqual(packets[1].mouse[3], WM_MOUSEMOVE)
        self.assertEqual(packets[2].mouse[3], WM_LBUTTONDOWN)

    def test_remote_input_becomes_portal_commands(self):
        bridge = FakeBridge()
        manager = InputManager(
            Config(machine_name="linux", machine_id=10),
            lambda _packet: None,
            lambda: 20,
            lambda _message: None,
            bridge=bridge,
        )
        manager.inject_keyboard(0x41, 0)
        manager.inject_mouse(32768, 32768, 0, WM_MOUSEMOVE)
        self.assertEqual(bridge.commands[0][0], "inject_key")
        self.assertEqual(bridge.commands[0][1]["keycode"], 30)
        self.assertEqual(bridge.commands[1][0], "inject_pointer_absolute")

    def test_remote_pointer_can_reach_the_last_linux_pixel_row(self):
        bridge = FakeBridge()
        manager = InputManager(
            Config(machine_name="linux", machine_id=10),
            lambda _packet: None,
            lambda: 20,
            lambda _message: None,
            bridge=bridge,
        )
        manager._bridge_event(
            {
                "type": "event",
                "event": "inject_device_added",
                "pointer_absolute": True,
                "regions": [{"x": 0, "y": 0, "width": 1920, "height": 1080}],
            }
        )

        # This is exactly what the Windows sender produces for pixel row 1079:
        # 1079 * 65535 // 1080.  The inverse must recover row 1079, not a
        # fractional coordinate below it that the compositor places on 1078.
        windows_bottom_row = 1079 * 65535 // 1080
        manager.inject_mouse(0, windows_bottom_row, 0, WM_MOUSEMOVE)

        self.assertEqual(bridge.commands[-1][1]["y"], 1079.0)

    def test_remote_pointer_endpoints_stay_inside_offset_eis_region(self):
        bridge = FakeBridge()
        manager = InputManager(
            Config(machine_name="linux", machine_id=10),
            lambda _packet: None,
            lambda: 20,
            lambda _message: None,
            bridge=bridge,
        )
        manager._bridge_event(
            {
                "type": "event",
                "event": "inject_device_added",
                "pointer_absolute": True,
                "regions": [{"x": 1920, "y": 120, "width": 2560, "height": 1440}],
            }
        )

        manager.inject_mouse(65535, 65535, 0, WM_MOUSEMOVE)

        command, values = bridge.commands[-1]
        self.assertEqual(command, "inject_pointer_absolute")
        self.assertEqual(values["x"], 4479.0)
        self.assertEqual(values["y"], 1559.0)

        manager.inject_mouse(32768, 32768, 0, WM_MOUSEMOVE)
        values = bridge.commands[-1][1]
        self.assertEqual(values["x"], 3200.0)
        self.assertEqual(values["y"], 840.0)

    def test_remote_pointer_spans_regions_announced_by_multiple_devices(self):
        bridge = FakeBridge()
        manager = InputManager(
            Config(machine_name="linux", machine_id=10),
            lambda _packet: None,
            lambda: 20,
            lambda _message: None,
            bridge=bridge,
        )
        for region in (
            {"x": 0, "y": 0, "width": 1920, "height": 1080},
            {"x": 1920, "y": 0, "width": 2560, "height": 1440},
        ):
            manager._bridge_event(
                {
                    "type": "event",
                    "event": "inject_device_added",
                    "pointer_absolute": True,
                    "regions": [region],
                }
            )

        manager.inject_mouse(65535, 65535, 0, WM_MOUSEMOVE)

        command, values = bridge.commands[-1]
        self.assertEqual(command, "inject_pointer_absolute")
        self.assertEqual(values["x"], 4479.0)
        self.assertEqual(values["y"], 1439.0)

    def test_remote_wheel_and_xbutton_keep_windows_units(self):
        bridge = FakeBridge()
        manager = InputManager(
            Config(machine_name="linux", machine_id=10),
            lambda _packet: None,
            lambda: 20,
            lambda _message: None,
            bridge=bridge,
        )

        manager.inject_mouse(0, 0, -120, WM_MOUSEWHEEL)
        manager.inject_mouse(0, 0, 1, WM_XBUTTONDOWN)
        manager.inject_mouse(0, 0, 2, WM_XBUTTONDOWN)

        self.assertEqual(bridge.commands[0][1]["dy"], 120)
        self.assertEqual(bridge.commands[1][1]["button"], 275)
        self.assertEqual(bridge.commands[2][1]["button"], 276)

    def test_windows_controller_gets_next_machine_at_linux_edge(self):
        packets = []
        bridge = FakeBridge()
        config = Config(
            machine_name="linux",
            machine_id=10,
            remote_machines=[{"name": "windows", "address": "10.0.0.2"}],
            machine_matrix=["windows", "linux", "", ""],
        )
        manager = InputManager(
            config,
            packets.append,
            lambda name=None: 20 if name == "windows" else None,
            lambda _message: None,
            peer_name=lambda machine_id: "windows" if machine_id == 20 else None,
            bridge=bridge,
        )

        # The entry coordinate is edge-adjacent and must only arm routing.
        manager.inject_mouse(0, 32768, 0, WM_MOUSEMOVE, source_id=20)
        manager.inject_mouse(32768, 32768, 0, WM_MOUSEMOVE, source_id=20)
        manager.inject_mouse(0, 32768, 0, WM_MOUSEMOVE, source_id=20)

        self.assertEqual(bridge.commands[0][0], "inject_pointer_absolute")
        self.assertEqual(len(packets), 1)
        self.assertEqual(packets[0].type, PackageType.NEXT_MACHINE)
        self.assertEqual(packets[0].dest, 20)
        self.assertEqual(packets[0].mouse, (65535 - 800, 32768, 20, 0))

    def test_next_machine_from_controlled_windows_returns_linux_capture(self):
        bridge = FakeBridge()
        manager = InputManager(
            Config(
                machine_name="linux",
                machine_id=10,
                remote_machines=[{"name": "windows", "address": "10.0.0.2"}],
                machine_matrix=["linux", "windows", "", ""],
            ),
            lambda _packet: None,
            lambda name=None: 20 if name == "windows" else None,
            lambda _message: None,
            peer_name=lambda machine_id: "windows" if machine_id == 20 else None,
            bridge=bridge,
        )
        manager.remote_active = True
        manager.active_remote_name = "windows"

        manager.follow_next_machine(10, 800, 32768, 20)

        self.assertFalse(manager.remote_active)
        command, arguments = bridge.commands[-1]
        self.assertEqual(command, "capture_release")
        self.assertIn("cursor_position", arguments)

    def test_offline_switch_wakes_then_retries_after_connection(self):
        connected = False
        wake = Mock(return_value=True)
        manager = InputManager(
            Config(
                machine_name="linux",
                machine_id=10,
                remote_machines=[{"name": "windows", "address": "10.0.0.2"}],
                machine_matrix=["linux", "windows", "", ""],
            ),
            lambda _packet: None,
            lambda name=None: 20 if connected and name == "windows" else None,
            lambda _message: None,
            wake_peer=wake,
            bridge=FakeBridge(),
        )

        manager.switch_remote("windows")
        self.assertEqual(manager._waking_remote_name, "windows")
        wake.assert_called_once_with("windows")

        connected = True
        manager._capture_ready = True
        with patch.object(manager, "_trigger_edge", return_value=True) as trigger:
            manager.retry_pending_switch()

        self.assertEqual(manager._waking_remote_name, "")
        trigger.assert_called_once_with()

    def test_remote_edge_switches_to_the_next_connected_matrix_pc(self):
        packets = []
        config = Config(
            machine_name="linux",
            machine_id=10,
            remote_machines=[
                {"name": "right", "address": "10.0.0.2"},
                {"name": "far", "address": "10.0.0.3"},
            ],
            machine_matrix=["linux", "right", "far", ""],
        )
        manager = InputManager(
            config,
            packets.append,
            lambda name=None: {"right": 20, "far": 30}.get(name),
            lambda _message: None,
            bridge=FakeBridge(),
        )
        manager.active_remote_name = "right"
        manager.remote_active = True
        manager.x = 65_000

        manager._send_motion(100, 0)

        self.assertEqual(manager.active_remote_name, "far")
        self.assertEqual([packet.type for packet in packets], [
            PackageType.HIDE_MOUSE,
            PackageType.MOUSE,
        ])
        self.assertEqual([packet.dest for packet in packets], [20, 30])

    def test_remote_edge_adjacent_to_local_releases_capture(self):
        bridge = FakeBridge()
        config = Config(
            machine_name="linux",
            machine_id=10,
            remote_machines=[{"name": "right", "address": "10.0.0.2"}],
            machine_matrix=["linux", "right", "", ""],
        )
        manager = InputManager(
            config,
            lambda _packet: None,
            lambda name=None: 20 if name == "right" else None,
            lambda _message: None,
            bridge=bridge,
        )
        manager.active_remote_name = "right"
        manager.remote_active = True
        manager.x = 500

        manager._send_motion(-100, 0)

        self.assertFalse(manager.remote_active)
        self.assertEqual(bridge.commands[-1][0], "capture_release")


if __name__ == "__main__":
    unittest.main()
