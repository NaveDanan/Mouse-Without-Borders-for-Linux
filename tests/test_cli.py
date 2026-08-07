import io
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch

from mwb_linux.__main__ import build_parser, main


class CliTests(unittest.TestCase):
    def test_switch_machine_sends_one_based_slot_to_control_service(self):
        with patch("mwb_linux.__main__.control_request", return_value={"ok": True}) as request:
            result = main(["switch-machine", "3"])

        self.assertEqual(result, 0)
        request.assert_called_once_with("switch_machine", slot=3)

    def test_switch_machine_accepts_exactly_four_slots(self):
        parser = build_parser()
        self.assertEqual(parser.parse_args(["switch-machine", "1"]).slot, 1)
        self.assertEqual(parser.parse_args(["switch-machine", "4"]).slot, 4)
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                parser.parse_args(["switch-machine", "0"])
            with self.assertRaises(SystemExit):
                parser.parse_args(["switch-machine", "5"])


if __name__ == "__main__":
    unittest.main()
