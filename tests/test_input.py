import io
import json
import threading
import time
import unittest
from unittest.mock import Mock, patch

from mwb_linux.config import Config
from mwb_linux.input import (
    InputManager,
    PortalBridge,
    WINDOWS_EPOCH_TICKS,
    WM_LBUTTONDOWN,
    WM_MOUSEMOVE,
    WM_MOUSEWHEEL,
    WM_XBUTTONDOWN,
    capture_targets,
    find_bridge,
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

    def request(self, command, **kwargs):
        self.commands.append((command, kwargs))
        return {"ok": True}

    def stop(self):
        self.commands.append(("stop", {}))


class InputTests(unittest.TestCase):
    def test_frozen_application_finds_its_co_packaged_portal_bridge(self):
        executable = "/tmp/AppDir/usr/lib/mwb/powertoys-mouse-without-borders"
        expected = "/tmp/AppDir/usr/lib/mwb/mwb-portal-bridge"
        with (
            patch.dict("mwb_linux.input.os.environ", {}, clear=True),
            patch("mwb_linux.input.sys.frozen", True, create=True),
            patch("mwb_linux.input.sys.executable", executable),
            patch("mwb_linux.input.os.access", side_effect=lambda path, _mode: path == expected),
        ):
            self.assertEqual(find_bridge(), expected)

    def test_appimage_portal_identity_uses_its_embedded_desktop_file(self):
        appdir = "/tmp/.mount_mwb"
        with (
            patch.dict("mwb_linux.input.os.environ", {"APPDIR": appdir}, clear=True),
            patch("mwb_linux.input.Path.is_file", return_value=True),
        ):
            environment = portal_environment()

        self.assertEqual(
            environment["GIO_LAUNCHED_DESKTOP_FILE"],
            f"{appdir}/usr/share/applications/"
            "io.github.NaveDanan.MouseWithoutBorders.desktop",
        )

    def test_portal_state_request_waits_for_matching_acknowledgement(self):
        messages = []
        bridge = PortalBridge(messages.append)
        bridge.process = Mock(stdin=io.StringIO())
        bridge.process.poll.return_value = None
        outcome = {}

        worker = threading.Thread(
            target=lambda: outcome.setdefault("response", bridge.request("capture_disable"))
        )
        worker.start()
        deadline = time.monotonic() + 1
        while not bridge.process.stdin.getvalue() and time.monotonic() < deadline:
            time.sleep(0.01)
        request = json.loads(bridge.process.stdin.getvalue())
        bridge._dispatch_message(
            {"type": "response", "id": request["id"], "ok": True, "result": {}}
        )
        worker.join(timeout=1)

        self.assertFalse(worker.is_alive())
        self.assertTrue(outcome["response"]["ok"])
        self.assertEqual(messages[-1]["id"], request["id"])

    def test_pause_disables_capture_and_relaunch_reenables_same_session(self):
        bridge = FakeBridge()
        messages = []
        manager = InputManager(
            Config(edge_switching=True),
            Mock(),
            Mock(return_value=None),
            messages.append,
            bridge=bridge,
        )
        manager._started = True
        manager._capture_ready = True
        manager._capture_enabled = True

        manager.pause()
        manager.start()

        self.assertEqual(
            bridge.commands,
            [
                ("capture_disable", {}),
                ("capture_enable", {}),
            ],
        )
        self.assertTrue(manager._started)
        self.assertTrue(manager._desired)
        self.assertTrue(manager._capture_enabled)

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
        self.assertNotIn("capture_enable", [command[0] for command in bridge.commands])

        manager.stop()
        manager.start()
        self.assertEqual(
            [command[0] for command in bridge.commands].count("start"), 2
        )

    def test_suspend_resume_preserves_a_live_portal_session(self):
        bridge = FakeBridge()
        messages = []
        manager = InputManager(
            Config(machine_name="linux", machine_id=10),
            lambda _packet: None,
            lambda: 20,
            messages.append,
            bridge=bridge,
        )
        manager._desired = True
        manager._started = True
        manager._capture_ready = True

        manager.resume_after_suspend()

        self.assertEqual(bridge.commands, [("ping", {"timeout": 3.0})])
        self.assertTrue(manager._started)
        self.assertTrue(manager._capture_ready)
        self.assertEqual(messages[-1], "Remote input session resumed")

    def test_suspend_resume_restarts_a_dead_portal_bridge(self):
        bridge = FakeBridge()
        bridge.request = Mock(side_effect=ConnectionError("bridge stopped"))
        manager = InputManager(
            Config(machine_name="linux", machine_id=10),
            lambda _packet: None,
            lambda: 20,
            lambda _message: None,
            bridge=bridge,
        )
        manager._desired = True
        manager._started = True

        with patch.object(manager, "_schedule_bridge_restart") as restart:
            manager.resume_after_suspend()

        restart.assert_called_once_with()

    def test_awake_packet_activity_uses_the_existing_eis_session(self):
        bridge = FakeBridge()
        manager = InputManager(
            Config(machine_name="linux", machine_id=10),
            lambda _packet: None,
            lambda: 20,
            lambda _message: None,
            bridge=bridge,
        )
        manager._started = True

        manager.wake_display()

        self.assertEqual(
            bridge.commands,
            [("inject_pointer_motion", {"dx": 0.0, "dy": 0.0})],
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

    def test_linux_control_transitions_notify_file_drag_manager(self):
        changes = []
        manager = InputManager(
            Config(machine_name="linux", machine_id=10),
            lambda _packet: None,
            lambda: 20,
            lambda _message: None,
            bridge=FakeBridge(),
            control_changed=changes.append,
        )

        manager._activate_remote()
        manager.release_local()

        self.assertEqual(changes, [20, None])

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

        absolute = [command for command in bridge.commands if command[0] == "inject_pointer_absolute"][-1]
        self.assertEqual(absolute[1]["y"], 1079.0)

    def test_remote_pointer_pushes_an_unoccupied_edge_for_auto_hidden_dock(self):
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

        # Bottom has no matrix neighbour, so motion continues past the final
        # absolute pixel and activates GNOME Shell's pressure barrier.
        manager.inject_mouse(32768, 65535, 0, WM_MOUSEMOVE, source_id=20)
        self.assertEqual(
            [command for command in bridge.commands if command[0] == "inject_pointer_motion"],
            [("inject_pointer_motion", {"dx": 0.0, "dy": 128.0})],
        )

        # Right belongs to the Windows tile and must remain a machine-switch
        # edge rather than revealing desktop chrome.
        bridge.commands.clear()
        manager.inject_mouse(65535, 32768, 0, WM_MOUSEMOVE, source_id=20)
        self.assertNotIn(
            "inject_pointer_motion", [command[0] for command in bridge.commands]
        )

        # At a corner, the occupied right edge must not suppress pressure on
        # the unoccupied bottom dock edge.
        bridge.commands.clear()
        manager.inject_mouse(65535, 65535, 0, WM_MOUSEMOVE, source_id=20)
        self.assertEqual(
            [command for command in bridge.commands if command[0] == "inject_pointer_motion"],
            [("inject_pointer_motion", {"dx": 0.0, "dy": 128.0})],
        )

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

        command, values = [
            item for item in bridge.commands if item[0] == "inject_pointer_absolute"
        ][-1]
        self.assertEqual(command, "inject_pointer_absolute")
        self.assertEqual(values["x"], 4479.0)
        self.assertEqual(values["y"], 1559.0)

        manager.inject_mouse(32768, 32768, 0, WM_MOUSEMOVE)
        values = [
            item for item in bridge.commands if item[0] == "inject_pointer_absolute"
        ][-1][1]
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

        command, values = [
            item for item in bridge.commands if item[0] == "inject_pointer_absolute"
        ][-1]
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


class LockScreenRecoveryTests(unittest.TestCase):
    """GNOME destroys every injected input device while the screen is locked.

    Recovery therefore has to rebuild only the dead half of the portal, and
    only once the session is unlocked again. Restarting the whole bridge would
    also discard the InputCapture session, which has no restore token on
    portal version 1 and would force a fresh consent dialog every unlock.
    """

    def manager(self, bridge=None, messages=None):
        return InputManager(
            Config(edge_switching=True),
            Mock(),
            Mock(return_value=None),
            (messages if messages is not None else []).append,
            bridge=bridge or FakeBridge(),
        )

    def test_injection_loss_never_restarts_the_bridge(self):
        bridge = FakeBridge()
        manager = self.manager(bridge)
        manager._desired = True
        manager._started = True
        manager._inject_ready = True
        manager._capture_ready = True

        with (
            patch.object(manager, "_schedule_bridge_restart") as restart,
            patch.object(manager, "_schedule_session_recovery") as recover,
        ):
            manager._bridge_event(
                {"type": "event", "event": "inject_error", "error": "EIS disconnected: "}
            )

        # Killing the bridge is what produces the extra consent dialog.
        restart.assert_not_called()
        recover.assert_called_once_with()
        self.assertFalse(manager._inject_ready)
        self.assertTrue(manager.injection_paused)

    def test_recovery_is_deferred_while_the_session_is_locked(self):
        manager = self.manager()
        manager._desired = True
        manager._started = True

        manager.session_lock_changed(True)
        with patch.object(manager, "_recover_sessions") as recover:
            manager._schedule_session_recovery()

        recover.assert_not_called()
        self.assertTrue(manager.session_locked)
        self.assertTrue(manager.injection_paused)

    def test_unlocking_rebuilds_only_the_dead_injection_session(self):
        bridge = FakeBridge()
        messages = []
        manager = self.manager(bridge, messages)
        manager._desired = True
        manager._started = True
        manager._inject_ready = False
        manager._inject_started = True
        # Capture survived, so it must not be touched or re-requested.
        manager._capture_ready = True
        manager.config.inject_restore_token = "token-123"

        self.assertTrue(manager._recover_sessions())

        commands = [name for name, _ in bridge.commands]
        self.assertEqual(commands, ["inject_stop", "inject_init"])
        self.assertNotIn("capture_init", commands)
        init = dict(bridge.commands[1][1])
        self.assertEqual(init["restore_token"], "token-123")
        self.assertTrue(manager._inject_ready)
        self.assertFalse(manager.injection_paused)

    def test_a_dead_capture_session_is_rebuilt_alongside_injection(self):
        bridge = FakeBridge()
        manager = self.manager(bridge)
        manager._desired = True
        manager._started = True
        manager._inject_ready = True
        manager._capture_ready = False

        self.assertTrue(manager._recover_sessions())

        commands = [name for name, _ in bridge.commands]
        self.assertEqual(commands, ["capture_init"])

    def test_locking_releases_capture_so_no_key_stays_held(self):
        manager = self.manager()
        manager._desired = True
        manager._started = True

        with patch.object(manager, "release_local") as release:
            manager.session_lock_changed(True)

        release.assert_called_once_with()

    def test_unlock_schedules_recovery(self):
        manager = self.manager()
        manager._desired = True
        manager.session_locked = True

        with patch.object(manager, "_schedule_session_recovery") as recover:
            manager.session_lock_changed(False)

        recover.assert_called_once_with()
        self.assertFalse(manager.session_locked)

    def test_bridge_death_still_restarts_the_whole_helper(self):
        manager = self.manager()
        manager._desired = True

        with patch.object(manager, "_schedule_bridge_restart") as restart:
            manager._bridge_event({"type": "event", "event": "bridge_stopped"})

        restart.assert_called_once_with()

    def test_paused_devices_report_the_lock_without_restarting_anything(self):
        messages = []
        manager = self.manager(messages=messages)

        with patch.object(manager, "_schedule_bridge_restart") as restart:
            manager._bridge_event(
                {"type": "event", "event": "inject_devices_paused", "active": 0}
            )

        restart.assert_not_called()
        self.assertTrue(manager.injection_paused)
        self.assertIn("locked", messages[-1])

    def test_commands_refused_by_a_locked_session_do_not_replace_the_status(self):
        messages = []
        manager = self.manager(messages=messages)
        manager.session_locked = True
        manager._inject_ready = False

        manager._bridge_event(
            {"type": "response", "ok": False, "error": "input injection task is not running"}
        )

        # The lock explanation must survive a burst of remote mouse packets.
        self.assertEqual(messages, [])

    def test_a_genuine_portal_error_is_still_reported(self):
        messages = []
        manager = self.manager(messages=messages)
        manager.session_locked = False
        manager._inject_ready = True

        manager._bridge_event(
            {"type": "response", "ok": False, "error": "portal is broken"}
        )

        self.assertIn("portal is broken", messages[-1])


class CapturePersistenceTests(unittest.TestCase):
    """InputCapture only gained restore tokens in interface version 2."""

    def manager(self, messages=None):
        return InputManager(
            Config(edge_switching=True),
            Mock(),
            Mock(return_value=None),
            (messages if messages is not None else []).append,
            bridge=FakeBridge(),
        )

    def capture_response(self, manager, **result):
        manager._bridge_event(
            {"type": "response", "ok": True, "id": "capture", "result": result}
        )

    def test_version_one_reports_that_the_prompt_cannot_be_remembered(self):
        manager = self.manager()

        with self.assertLogs("mwb_linux.input", level="WARNING") as logs:
            self.capture_response(manager, portal_version=1, zones=[])

        self.assertFalse(manager.capture_persistable)
        self.assertEqual(manager.capture_portal_version, 1)
        self.assertIn("1.21.1", "\n".join(logs.output))

    def test_the_unrememberable_prompt_is_explained_only_once(self):
        manager = self.manager()

        with self.assertLogs("mwb_linux.input", level="WARNING") as logs:
            self.capture_response(manager, portal_version=1, zones=[])
            self.capture_response(manager, portal_version=1, zones=[])

        self.assertEqual(len(logs.output), 1)

    def test_version_two_persists_the_token_without_warning(self):
        manager = self.manager()

        self.capture_response(
            manager, portal_version=2, restore_token="tok-1", zones=[]
        )

        self.assertTrue(manager.capture_persistable)
        self.assertEqual(manager.config.capture_restore_token, "tok-1")

    def test_a_returned_token_counts_as_persistable_whatever_the_version(self):
        manager = self.manager()

        self.capture_response(manager, portal_version=0, restore_token="tok-2", zones=[])

        self.assertTrue(manager.capture_persistable)
