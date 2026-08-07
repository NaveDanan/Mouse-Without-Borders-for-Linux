import contextlib
import io
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from mwb_linux.__main__ import main
from mwb_linux.config import Config, host_after_name_edit
from mwb_linux.keymap import evdev_to_windows, windows_to_evdev


class ConfigAndKeymapTests(unittest.TestCase):
    def test_config_is_mode_0600_and_redacted_publicly(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            config = Config(host="pc", secret="0123456789abcdef")
            config.save(path)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(Config.load(path).secret, "0123456789abcdef")
            self.assertEqual(config.public_dict()["secret"], "configured")

    def test_insecure_config_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            Config().save(path)
            os.chmod(path, 0o644)
            with self.assertRaises(PermissionError):
                Config.load(path)

    def test_legacy_vertical_positions_migrate_to_two_rows(self):
        expected_matrices = {
            "top": ["WindowsPC", "", "LinuxPC", ""],
            "bottom": ["LinuxPC", "", "WindowsPC", ""],
        }
        for position, expected_matrix in expected_matrices.items():
            with self.subTest(position=position), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "config.json"
                path.write_text(
                    json.dumps(
                        {
                            "host": "10.0.0.7",
                            "host_name": "WindowsPC",
                            "host_position": position,
                            "machine_name": "LinuxPC",
                            "machine_id": 10,
                        }
                    ),
                    encoding="utf-8",
                )
                os.chmod(path, 0o600)

                config = Config.load(path)

                self.assertTrue(config.two_row)
                self.assertEqual(config.machine_matrix, expected_matrix)

    def test_explicit_vertical_matrix_preserves_its_row_mode(self):
        config = Config(
            host_position="top",
            machine_name="LinuxPC",
            machine_id=10,
            machine_matrix=["LinuxPC", "WindowsPC", "", ""],
            two_row=False,
        )

        self.assertFalse(config.two_row)

    def test_cli_machine_rename_replaces_local_matrix_entry(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            Config(
                host="10.0.0.7",
                host_name="WindowsPC",
                secret="0123456789abcdef",
                machine_name="OldLinux",
                machine_id=10,
                remote_machines=[{"name": "WindowsPC", "address": "10.0.0.7"}],
                machine_matrix=["WindowsPC", "OldLinux", "", ""],
            ).save(path)

            with contextlib.redirect_stdout(io.StringIO()):
                result = main(
                    [
                        "configure",
                        "--config",
                        str(path),
                        "--machine-name",
                        "NewLinux",
                    ]
                )

            reloaded = Config.load(path)
            self.assertEqual(result, 0)
            self.assertEqual(reloaded.machine_name, "NewLinux")
            self.assertEqual(
                reloaded.machine_matrix,
                ["WindowsPC", "NewLinux", "", ""],
            )
            reloaded.validate(require_connection=True)

    def test_unchanged_machine_name_preserves_explicit_host_address(self):
        self.assertEqual(
            host_after_name_edit("10.0.0.7", "WindowsPC", "WindowsPC"),
            "10.0.0.7",
        )

    def test_changed_machine_name_switches_to_name_resolution(self):
        self.assertEqual(
            host_after_name_edit("10.0.0.7", "WindowsPC", "OtherPC"),
            "OtherPC",
        )

    def test_key_mapping_preserves_release_and_side(self):
        self.assertEqual(evdev_to_windows(30, True), (0x41, 0))
        self.assertEqual(evdev_to_windows(30, False), (0x41, 0x80))
        self.assertEqual(windows_to_evdev(0xA3, 0), (97, True))
        self.assertEqual(windows_to_evdev(0xA3, 0x80), (97, False))
        self.assertEqual(windows_to_evdev(0x11, 0x01), (97, True))


if __name__ == "__main__":
    unittest.main()
