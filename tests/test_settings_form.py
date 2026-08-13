import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from mwb_linux.config import (
    HOTKEY_DEFAULTS,
    OTHER_OPTION_DEFAULTS,
    Config,
    generate_secret,
    host_after_name_edit,
    parse_ip_mappings,
)
from mwb_linux.shortcuts import BASE, desired_bindings, managed_paths, merge_paths
from mwb_linux.ui import (
    STATUS_TIMEOUT,
    MainWindow,
    MouseWithoutBordersApplication,
    _start_background_service,
    adjacent_remote_edges,
    matrix_coordinates,
    remote_records,
)
from mwb_linux.updater import UpdateRelease


class SettingsFormConfigTests(unittest.TestCase):
    def test_legacy_single_host_migrates_to_four_machine_matrix(self):
        config = Config(
            host="10.0.0.7",
            host_name="WindowsPC",
            machine_name="LinuxPC",
            machine_id=10,
        )

        self.assertEqual(config.machine_matrix, ["LinuxPC", "WindowsPC", "", ""])
        self.assertEqual(
            [(target.name, target.address) for target in config.resolve_hosts()],
            [("WindowsPC", "10.0.0.7")],
        )

    def test_three_remote_computers_round_trip_in_matrix_order(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            config = Config(
                machine_name="LinuxPC",
                machine_id=10,
                secret="0123456789abcdef",
                remote_machines=[
                    {"name": "LeftPC", "address": "10.0.0.1"},
                    {"name": "RightPC", "address": "10.0.0.2"},
                    {"name": "BottomPC", "address": "10.0.0.3"},
                ],
                machine_matrix=["LeftPC", "LinuxPC", "RightPC", "BottomPC"],
            )

            config.validate(require_connection=True)
            config.save(path)
            reloaded = Config.load(path)

            self.assertEqual(reloaded.machine_matrix, config.machine_matrix)
            self.assertEqual(
                [target.name for target in reloaded.resolve_hosts()],
                ["LeftPC", "RightPC", "BottomPC"],
            )

    def test_matrix_rejects_duplicate_and_fourth_remote_computers(self):
        with self.assertRaisesRegex(ValueError, "unique"):
            Config(
                machine_name="LinuxPC",
                machine_id=10,
                remote_machines=[{"name": "WindowsPC", "address": "10.0.0.1"}],
                machine_matrix=["LinuxPC", "WindowsPC", "windowspc", ""],
            ).validate()

    def test_explicit_matrix_does_not_readd_a_removed_remote_record(self):
        config = Config(
            machine_name="LinuxPC",
            machine_id=10,
            remote_machines=[
                {"name": "KeptPC", "address": "10.0.0.1"},
                {"name": "RemovedPC", "address": "10.0.0.2"},
            ],
            machine_matrix=["LinuxPC", "KeptPC", "", ""],
        )

        self.assertEqual(config.machine_matrix, ["LinuxPC", "KeptPC", "", ""])
        self.assertEqual([target.name for target in config.resolve_hosts()], ["KeptPC"])

        with self.assertRaisesRegex(ValueError, "at most three"):
            Config(
                machine_name="LinuxPC",
                machine_id=10,
                remote_machines=[
                    {"name": f"Windows{index}", "address": f"10.0.0.{index}"}
                    for index in range(1, 5)
                ],
            ).validate()

    def test_ip_mappings_round_trip_and_reject_bad_lines(self):
        text = "# comment\nSampleA 192.168.1.5\n\nSampleB 192.168.1.6\n"
        self.assertEqual(
            parse_ip_mappings(text),
            {"samplea": "192.168.1.5", "sampleb": "192.168.1.6"},
        )
        with self.assertRaises(ValueError) as error:
            parse_ip_mappings("SampleA not-an-ip")
        self.assertIn("line 1", str(error.exception))
        with self.assertRaises(ValueError):
            parse_ip_mappings("SampleA")

    def test_mapping_wins_over_dns_for_the_host_name(self):
        config = Config(
            host="WindowsPC",
            host_name="WindowsPC",
            ip_mappings="WindowsPC 10.0.0.7",
        )
        self.assertEqual(config.resolve_host(), "10.0.0.7")
        config.ip_mappings = ""
        self.assertEqual(config.resolve_host(), "WindowsPC")

    def test_editing_the_machine_card_keeps_an_explicit_address(self):
        self.assertEqual(host_after_name_edit("10.0.0.7", "WindowsPC", "WindowsPC"), "10.0.0.7")
        self.assertEqual(host_after_name_edit("10.0.0.7", "WindowsPC", "OtherPC"), "OtherPC")

    def test_generated_key_matches_the_windows_form_shape(self):
        key = generate_secret()
        self.assertEqual(len(key), 16)
        self.assertNotEqual(key, generate_secret())

    def test_form_settings_survive_a_save_and_reload(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            config = Config(
                secret="0123456789abcdef",
                host="pc",
                check_updates=False,
                two_row=True,
                host_zone=[1920, 0, 2560, 1440],
                switch_hotkey="numbers",
                other_options={"wrap_mouse": True},
                hotkeys={"reconnect": "R"},
                ip_mappings="pc 10.0.0.7",
            )
            config.save(path)
            reloaded = Config.load(path)
            self.assertTrue(reloaded.two_row)
            self.assertFalse(reloaded.check_updates)
            self.assertEqual(reloaded.host_zone, [1920, 0, 2560, 1440])
            self.assertEqual(reloaded.switch_hotkey, "numbers")
            self.assertTrue(reloaded.other_options["wrap_mouse"])
            # Missing entries fall back to the documented defaults.
            self.assertEqual(
                reloaded.other_options["disable_cad"], OTHER_OPTION_DEFAULTS["disable_cad"]
            )
            self.assertEqual(reloaded.hotkeys["exit"], HOTKEY_DEFAULTS["exit"])

    def test_invalid_form_values_are_rejected(self):
        with self.assertRaises(ValueError):
            Config(switch_hotkey="always").validate()
        with self.assertRaises(ValueError):
            Config(hotkeys={"exit": "Ctrl"}).validate()
        with self.assertRaises(ValueError):
            Config(other_options={"fly_mouse": True}).validate()
        with self.assertRaises(ValueError):
            Config(host_zone=[0, 0]).validate()

    def test_four_computer_matrix_uses_windows_slot_coordinates(self):
        self.assertEqual(
            [matrix_coordinates(slot, False) for slot in range(4)],
            [(0, 0), (0, 1), (0, 2), (0, 3)],
        )
        self.assertEqual(
            [matrix_coordinates(slot, True) for slot in range(4)],
            [(0, 0), (0, 1), (1, 0), (1, 1)],
        )

    def test_unchanged_remote_keeps_its_explicit_address(self):
        records = remote_records(
            ["linux", "WindowsA", "WindowsB", ""],
            "linux",
            {"windowsa": "10.0.0.7"},
        )
        self.assertEqual(
            records,
            [
                {"name": "WindowsA", "address": "10.0.0.7"},
                {"name": "WindowsB", "address": "WindowsB"},
            ],
        )

    def test_matrix_edges_follow_one_row_and_two_row_topology(self):
        self.assertEqual(
            adjacent_remote_edges(["A", "linux", "", "B"], "linux", False),
            {"right": "B", "left": "A"},
        )
        self.assertEqual(
            adjacent_remote_edges(["linux", "A", "B", ""], "linux", True),
            {"right": "A", "bottom": "B"},
        )

    def test_enabling_removing_and_swapping_computer_slots(self):
        form = SimpleNamespace(
            config=Config(machine_name="linux", machine_id=10),
            _matrix_names=["linux", "", "", ""],
            _enabled_slots={0},
            machine_cards={},
            _remember_matrix_names=lambda: None,
            _render_matrix=Mock(),
        )

        MainWindow._set_slot_enabled(form, 1, True)
        self.assertEqual(form._enabled_slots, {0, 1})
        form._matrix_names[1] = "WindowsA"
        MainWindow._swap_slots(form, 0, 1)
        self.assertEqual(form._matrix_names, ["WindowsA", "linux", "", ""])
        self.assertEqual(form._enabled_slots, {0, 1})

        MainWindow._set_slot_enabled(form, 0, False)
        self.assertEqual(form._matrix_names, ["", "linux", "", ""])
        self.assertEqual(form._enabled_slots, {1})


class BackgroundServiceTests(unittest.TestCase):
    def test_closing_the_window_hides_it_without_destroying_the_application(self):
        form = SimpleNamespace(set_visible=Mock())

        handled = MainWindow._on_close_request(form, form)

        self.assertTrue(handled)
        form.set_visible.assert_called_once_with(False)

    def test_indicator_open_and_settings_actions_present_the_existing_window(self):
        window = SimpleNamespace(present=Mock(), show_settings=Mock())
        application = SimpleNamespace(window=window)

        MouseWithoutBordersApplication.open_window(application)
        MouseWithoutBordersApplication.open_settings(application)

        window.present.assert_called_once_with()
        window.show_settings.assert_called_once_with()

    def test_status_poll_cannot_restart_service_during_exit(self):
        application = Mock(spec=MouseWithoutBordersApplication)
        application._exit_started = True
        form = SimpleNamespace(get_application=Mock(return_value=application))

        with patch("mwb_linux.ui.control_request") as request:
            result = MainWindow._poll_status(form)

        request.assert_not_called()
        self.assertEqual(result, 0)

    def test_indicator_exit_schedules_one_non_blocking_full_shutdown(self):
        indicator = SimpleNamespace(stop=Mock())
        window = SimpleNamespace(set_visible=Mock())
        application = SimpleNamespace(
            indicator=indicator,
            window=window,
            _exit_started=False,
            _held_for_indicator=True,
            _stop_service_and_finish_exit=Mock(),
            release=Mock(),
            quit=Mock(),
        )
        worker = Mock()
        with patch("mwb_linux.ui.threading.Thread", return_value=worker) as thread:
            MouseWithoutBordersApplication.exit_application(application)
            MouseWithoutBordersApplication.exit_application(application)

        indicator.stop.assert_called_once_with()
        window.set_visible.assert_called_once_with(False)
        thread.assert_called_once_with(
            target=application._stop_service_and_finish_exit,
            name="mwb-application-exit",
            daemon=True,
        )
        worker.start.assert_called_once_with()
        application.release.assert_not_called()
        application.quit.assert_not_called()
        self.assertTrue(application._exit_started)
        self.assertIsNone(application.indicator)

    def test_indicator_exit_fully_stops_service_then_finishes_on_gtk_thread(self):
        application = SimpleNamespace(_finish_exit=Mock())
        with (
            patch("mwb_linux.ui.control_request", return_value={"ok": True}) as request,
            patch("mwb_linux.ui.GLib.idle_add") as idle_add,
        ):
            MouseWithoutBordersApplication._stop_service_and_finish_exit(application)

        request.assert_called_once_with("quit", timeout=2.0)
        idle_add.assert_called_once_with(application._finish_exit)

    def test_indicator_exit_fails_closed_when_service_does_not_acknowledge(self):
        application = SimpleNamespace(_finish_exit=Mock())
        with (
            patch("mwb_linux.ui.control_request", side_effect=OSError("gone")),
            patch("mwb_linux.ui.subprocess.run") as run,
            patch("mwb_linux.ui.GLib.idle_add"),
        ):
            MouseWithoutBordersApplication._stop_service_and_finish_exit(application)

        self.assertEqual(
            run.call_args.args[0],
            [
                "systemctl",
                "--user",
                "stop",
                "app-io.github.NaveDanan.MouseWithoutBorders.service",
                "app-io.github.NaveDanan.MouseWithoutBorders@dev.service",
            ],
        )
        self.assertEqual(run.call_args.kwargs["timeout"], 5)

    def test_indicator_exit_fails_closed_when_an_old_daemon_rejects_command(self):
        application = SimpleNamespace(_finish_exit=Mock())
        with (
            patch(
                "mwb_linux.ui.control_request",
                return_value={"ok": False, "error": "unknown command"},
            ),
            patch("mwb_linux.ui.subprocess.run") as run,
            patch("mwb_linux.ui.GLib.idle_add"),
        ):
            MouseWithoutBordersApplication._stop_service_and_finish_exit(application)

        run.assert_called_once()

    def test_indicator_exit_releases_application_hold_on_gtk_thread(self):
        application = SimpleNamespace(
            _held_for_indicator=True,
            release=Mock(),
            quit=Mock(),
        )

        result = MouseWithoutBordersApplication._finish_exit(application)

        application.release.assert_called_once_with()
        application.quit.assert_called_once_with()
        self.assertFalse(application._held_for_indicator)
        self.assertEqual(result, 0)

    def test_installed_ui_starts_the_canonical_app_unit(self):
        with (
            patch(
                "mwb_linux.ui.__file__",
                "/usr/lib/powertoys-mouse-without-borders/mwb_linux/ui.py",
            ),
            patch("mwb_linux.ui.Path.is_file", return_value=True),
            patch(
                "mwb_linux.ui.subprocess.run",
                return_value=SimpleNamespace(returncode=0),
            ) as run,
        ):
            _start_background_service()

        self.assertEqual(
            run.call_args.args[0],
            [
                "systemctl",
                "--user",
                "start",
                "app-io.github.NaveDanan.MouseWithoutBorders.service",
            ],
        )

    def test_source_daemon_uses_its_own_desktop_scope(self):
        launched_environment = {
            "PATH": "/usr/bin",
            "GIO_LAUNCHED_DESKTOP_FILE": "com.t3tools.t3code.desktop",
            "GIO_LAUNCHED_DESKTOP_FILE_PID": "123",
            "XDG_ACTIVATION_TOKEN": "ide-token",
        }
        with (
            patch.dict(
                "mwb_linux.ui.os.environ", launched_environment, clear=True
            ),
            patch(
                "mwb_linux.ui.subprocess.run",
                return_value=SimpleNamespace(returncode=0),
            ) as run,
            patch("mwb_linux.ui.subprocess.Popen") as popen,
        ):
            _start_background_service()

        arguments = run.call_args.args[0]
        self.assertEqual(arguments[:4], ["systemd-run", "--user", "--quiet", "--collect"])
        self.assertIn(
            "--unit=app-io.github.NaveDanan.MouseWithoutBorders@dev.service",
            arguments,
        )
        self.assertTrue(
            any(argument.startswith("--setenv=PYTHONPATH=") for argument in arguments)
        )
        child_environment = run.call_args.kwargs["env"]
        self.assertNotIn("GIO_LAUNCHED_DESKTOP_FILE", child_environment)
        self.assertNotIn("GIO_LAUNCHED_DESKTOP_FILE_PID", child_environment)
        self.assertNotIn("XDG_ACTIVATION_TOKEN", child_environment)
        popen.assert_not_called()

    def test_appimage_daemon_relaunches_from_the_original_image(self):
        appimage = "/opt/Mouse-Without-Borders.AppImage"
        with (
            patch.dict(
                "mwb_linux.ui.os.environ", {"APPIMAGE": appimage}, clear=True
            ),
            patch("mwb_linux.ui.sys.frozen", True, create=True),
            patch(
                "mwb_linux.ui.subprocess.run",
                return_value=SimpleNamespace(returncode=0),
            ) as run,
        ):
            _start_background_service()

        self.assertEqual(run.call_args.args[0][-2:], [appimage, "daemon"])

    def test_source_daemon_fails_closed_when_systemd_is_unavailable(self):
        with (
            patch(
                "mwb_linux.ui.subprocess.run",
                return_value=SimpleNamespace(returncode=1),
            ),
            patch("mwb_linux.ui.subprocess.Popen") as popen,
        ):
            with self.assertRaisesRegex(OSError, "systemd"):
                _start_background_service()

        popen.assert_not_called()


class UpdateUiTests(unittest.TestCase):
    def setUp(self):
        self.release = UpdateRelease(
            version="0.6.0",
            tag="v0.6.0",
            page_url="https://example.invalid/release",
            asset_name="update.deb",
            asset_url="https://example.invalid/update.deb",
            asset_size=20,
            sha256="0" * 64,
        )

    def test_automatic_check_failure_is_silent(self):
        form = SimpleNamespace(
            _update_check_running=True,
            config=SimpleNamespace(check_updates=True),
            update_refresh=SimpleNamespace(set_sensitive=Mock()),
            update_status=SimpleNamespace(set_text=Mock()),
        )

        MainWindow._finish_update_check(form, None, False, False)

        form.update_status.set_text.assert_not_called()
        form.update_refresh.set_sensitive.assert_called_once_with(True)

    def test_non_debian_manual_update_points_to_github_releases(self):
        form = SimpleNamespace(
            _update_check_running=False,
            _installing_update=False,
            update_status=SimpleNamespace(set_text=Mock()),
        )
        with patch("mwb_linux.ui.automatic_install_supported", return_value=False):
            MainWindow._start_update_check(form, manual=True)

        form.update_status.set_text.assert_called_once_with(
            "Download updates from GitHub Releases"
        )

    def test_disabling_checks_while_one_is_running_suppresses_its_result(self):
        form = SimpleNamespace(
            _update_check_running=True,
            _announced_update="",
            config=SimpleNamespace(check_updates=False),
            update_refresh=SimpleNamespace(set_sensitive=Mock()),
            update_status=SimpleNamespace(set_text=Mock()),
            _show_update_available=Mock(),
        )

        MainWindow._finish_update_check(form, self.release, False, True)

        form.update_status.set_text.assert_not_called()
        form._show_update_available.assert_not_called()

    def test_manual_check_reports_current_version_inline(self):
        form = SimpleNamespace(
            _update_check_running=True,
            config=SimpleNamespace(check_updates=True),
            update_refresh=SimpleNamespace(set_sensitive=Mock()),
            update_status=SimpleNamespace(set_text=Mock()),
        )

        MainWindow._finish_update_check(form, None, True, True)

        form.update_status.set_text.assert_called_once_with("Up to date (0.6.0)")

    def test_new_version_is_announced_once(self):
        form = SimpleNamespace(
            _update_check_running=True,
            _announced_update="",
            config=SimpleNamespace(check_updates=True),
            update_refresh=SimpleNamespace(set_sensitive=Mock()),
            update_status=SimpleNamespace(set_text=Mock()),
            _show_update_available=Mock(),
        )

        MainWindow._finish_update_check(form, self.release, False, True)

        form.update_status.set_text.assert_called_once_with("Version 0.6.0 available")
        form._show_update_available.assert_called_once_with(self.release)

    def test_update_dialog_shows_current_and_latest_versions(self):
        dialog = Mock()
        form = SimpleNamespace(present=Mock(), _on_update_choice=Mock())
        with patch("mwb_linux.ui.Gtk.AlertDialog", return_value=dialog):
            MainWindow._show_update_available(form, self.release)

        form.present.assert_called_once_with()
        detail = dialog.set_detail.call_args.args[0]
        self.assertIn("Current version: 0.6.0", detail)
        self.assertIn("Latest version: 0.6.0", detail)
        dialog.set_buttons.assert_called_once_with(["Later", "Download and Install"])

    def test_failed_install_keeps_the_application_open(self):
        application = SimpleNamespace(quit=Mock())
        form = SimpleNamespace(
            _installing_update=True,
            update_refresh=SimpleNamespace(set_sensitive=Mock()),
            update_status=SimpleNamespace(set_text=Mock()),
            get_application=Mock(return_value=application),
        )
        with patch("mwb_linux.ui.schedule_relaunch") as relaunch:
            MainWindow._finish_update_install(form, self.release, False)

        relaunch.assert_not_called()
        application.quit.assert_not_called()
        self.assertFalse(form._installing_update)

    def test_successful_install_schedules_relaunch_then_closes(self):
        application = SimpleNamespace(quit=Mock())
        form = SimpleNamespace(
            _installing_update=True,
            update_refresh=SimpleNamespace(set_sensitive=Mock()),
            update_status=SimpleNamespace(set_text=Mock()),
            get_application=Mock(return_value=application),
        )
        with patch("mwb_linux.ui.schedule_relaunch") as relaunch:
            MainWindow._finish_update_install(form, self.release, True)

        relaunch.assert_called_once_with()
        application.quit.assert_called_once_with()


class DesktopShortcutTests(unittest.TestCase):
    def test_switch_mode_selects_the_accelerators(self):
        config = Config(
            machine_name="LinuxPC",
            remote_machines=[
                {"name": "LeftPC", "address": "10.0.0.1"},
                {"name": "RightPC", "address": "10.0.0.2"},
            ],
            machine_matrix=["LeftPC", "LinuxPC", "RightPC", ""],
            switch_hotkey="numbers",
        )
        bindings = desired_bindings(config)
        for slot in range(1, 5):
            binding = bindings[f"{BASE}powertoys-mwb-machine-{slot}/"]
            self.assertEqual(binding[1], f"powertoys-mouse-without-borders switch-machine {slot}")
            self.assertEqual(binding[2], f"<Control><Alt>{slot}")
        self.assertNotIn(f"{BASE}powertoys-mwb-local/", bindings)
        self.assertNotIn(f"{BASE}powertoys-mwb-host/", bindings)

    def test_function_key_mode_registers_all_four_matrix_slots(self):
        bindings = desired_bindings(Config(switch_hotkey="fkeys"))
        self.assertEqual(
            [bindings[f"{BASE}powertoys-mwb-machine-{slot}/"][2] for slot in range(1, 5)],
            [f"<Control><Alt>F{slot}" for slot in range(1, 5)],
        )

    def test_disabled_switching_removes_the_machine_bindings(self):
        bindings = desired_bindings(Config(switch_hotkey="disabled"))
        for slot in range(1, 5):
            self.assertNotIn(f"{BASE}powertoys-mwb-machine-{slot}/", bindings)
        self.assertIn(f"{BASE}powertoys-mwb-reconnect/", bindings)

    def test_disabled_hotkeys_are_not_registered(self):
        config = Config(hotkeys={"reconnect": "Disable", "settings": "Disable", "exit": "Disable"})
        self.assertEqual(desired_bindings(config), {})

    def test_merging_keeps_unrelated_bindings_and_drops_retired_ones(self):
        current = [
            "/custom/terminal/",
            f"{BASE}powertoys-mwb-local/",
            f"{BASE}powertoys-mwb-host/",
            f"{BASE}powertoys-mwb-machine-4/",
        ]
        wanted = {
            f"{BASE}powertoys-mwb-machine-1/": (),
            f"{BASE}powertoys-mwb-reconnect/": (),
        }
        self.assertEqual(
            merge_paths(current, wanted),
            [
                "/custom/terminal/",
                f"{BASE}powertoys-mwb-machine-1/",
                f"{BASE}powertoys-mwb-reconnect/",
            ],
        )
        self.assertIn(f"{BASE}powertoys-mwb-local/", managed_paths())
        self.assertIn(f"{BASE}powertoys-mwb-host/", managed_paths())


if __name__ == "__main__":
    unittest.main()
