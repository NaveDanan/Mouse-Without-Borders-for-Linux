import os
import random
import threading
import unittest
import zlib
from unittest.mock import patch

from mwb_linux.clipboard import (
    ClipboardError,
    ClipboardManager,
    CommandClipboard,
    decode_text,
    encode_text,
    trim_png,
)


class ClipboardTests(unittest.TestCase):
    def test_stop_waits_for_poll_and_prevents_late_packets(self):
        entered = threading.Event()
        release = threading.Event()
        packets = []

        class BlockingAdapter:
            def available_types(self):
                return ("text/plain;charset=utf-8",)

            def get_text(self):
                entered.set()
                if not release.wait(2):
                    raise AssertionError("test did not release clipboard read")
                return "late clipboard value"

        manager = ClipboardManager(packets.append, adapter=BlockingAdapter())
        manager.start()
        self.assertTrue(entered.wait(2))
        stopped = threading.Event()
        stopper = threading.Thread(target=lambda: (manager.stop(), stopped.set()))
        stopper.start()
        self.assertFalse(stopped.wait(0.05))
        release.set()
        stopper.join(2)

        self.assertFalse(stopper.is_alive())
        self.assertFalse(manager._thread)
        self.assertEqual(packets, [])

    def test_xwayland_is_preferred_over_the_windowed_gnome_fallback(self):
        with (
            patch.dict(
                os.environ,
                {"WAYLAND_DISPLAY": "wayland-0", "DISPLAY": ":0"},
                clear=True,
            ),
            patch("shutil.which", side_effect=lambda command: f"/usr/bin/{command}"),
        ):
            adapter = CommandClipboard()

        self.assertTrue(adapter.wayland)
        self.assertTrue(adapter.x11)
        self.assertTrue(adapter.use_x11)

    def test_command_helpers_do_not_inherit_desktop_activation(self):
        adapter = CommandClipboard.__new__(CommandClipboard)
        with (
            patch.dict(
                "os.environ",
                {
                    "WAYLAND_DISPLAY": "wayland-0",
                    "GIO_LAUNCHED_DESKTOP_FILE": "/tmp/mwb.desktop",
                    "GIO_LAUNCHED_DESKTOP_FILE_PID": "123",
                    "XDG_ACTIVATION_TOKEN": "token",
                },
            ),
            patch("subprocess.run") as run,
        ):
            run.return_value.stdout = b"text/plain\n"
            adapter._run(["wl-paste", "--list-types"])

        environment = run.call_args.kwargs["env"]
        self.assertEqual(environment["WAYLAND_DISPLAY"], "wayland-0")
        self.assertNotIn("GIO_LAUNCHED_DESKTOP_FILE", environment)
        self.assertNotIn("GIO_LAUNCHED_DESKTOP_FILE_PID", environment)
        self.assertNotIn("XDG_ACTIVATION_TOKEN", environment)

    def test_unicode_round_trip(self):
        text = "PowerToys — שלום — こんにちは 👋\nsecond line"
        self.assertEqual(decode_text(encode_text(text)), text)

    def test_trailing_packet_padding_is_ignored(self):
        data = encode_text("hello") + b"\0" * 47
        self.assertEqual(decode_text(data), "hello")

    def test_invalid_data_rejected(self):
        with self.assertRaises(ClipboardError):
            decode_text(b"not deflate data")

    def test_decompression_bomb_rejected(self):
        compressor = zlib.compressobj(wbits=-zlib.MAX_WBITS)
        data = compressor.compress(b"x" * 4096) + compressor.flush()
        with self.assertRaises(ClipboardError):
            decode_text(data) if False else __import__(
                "mwb_linux.clipboard", fromlist=["_inflate_bounded"]
            )._inflate_bounded(data, 100)

    def test_png_padding_trim(self):
        fake = b"\x89PNG\r\n\x1a\n" + b"data" + b"IEND" + b"crc!" + b"\0" * 20
        self.assertEqual(trim_png(fake), fake[:-20])

    def test_deterministic_decompression_fuzz(self):
        rng = random.Random(0xC11B04AD)
        for _ in range(1_000):
            try:
                decode_text(rng.randbytes(rng.randrange(0, 512)))
            except (ClipboardError, UnicodeError):
                pass


if __name__ == "__main__":
    unittest.main()
