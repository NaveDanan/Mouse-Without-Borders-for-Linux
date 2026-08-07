import random
import unittest

from mwb_linux.protocol import Packet, PackageType, ProtocolError, magic_number


class ProtocolTests(unittest.TestCase):
    def test_magic_golden_vector(self):
        self.assertEqual(magic_number("0123456789abcdef"), 0x6C6E8B44)

    def test_standalone_magic_golden_vector(self):
        self.assertEqual(
            magic_number("0123456789abcdef", "sha256"),
            0x7E2D42E8,
        )

    def test_small_packet_round_trip(self):
        magic = magic_number("0123456789abcdef")
        packet = Packet()
        packet.type = PackageType.KEYBOARD
        packet.packet_id = 42
        packet.src = 123
        packet.dest = 456
        packet.timestamp = 987654321
        packet.keyboard = (0x41, 0x80)
        decoded = Packet.decode(packet.wire_bytes(magic), magic)
        self.assertEqual(decoded.type, PackageType.KEYBOARD)
        self.assertEqual(decoded.keyboard, (0x41, 0x80))
        self.assertEqual(decoded.timestamp, 987654321)

    def test_big_packet_round_trip(self):
        magic = magic_number("0123456789abcdef")
        packet = Packet()
        packet.type = PackageType.HEARTBEAT
        packet.src = 123
        packet.dest = 255
        packet.machine_name = "linux-box"
        wire = packet.wire_bytes(magic)
        self.assertEqual(len(wire), 64)
        decoded = Packet.decode(wire, magic)
        self.assertEqual(decoded.machine_name, "linux-box")

    def test_checksum_and_magic_rejected(self):
        magic = magic_number("0123456789abcdef")
        packet = Packet()
        packet.type = PackageType.MOUSE
        wire = bytearray(packet.wire_bytes(magic))
        wire[10] ^= 1
        with self.assertRaises(ProtocolError):
            Packet.decode(wire, magic)
        wire = bytearray(packet.wire_bytes(magic))
        wire[2] ^= 1
        with self.assertRaises(ProtocolError):
            Packet.decode(wire, magic)

    def test_deterministic_packet_parser_fuzz(self):
        rng = random.Random(0x4D5742)
        magic = magic_number("0123456789abcdef")
        for _ in range(5_000):
            size = rng.choice((0, 1, 15, 31, 32, 33, 63, 64, 65))
            data = rng.randbytes(size)
            try:
                packet = Packet.decode(data, magic)
                self.assertIn(len(packet.wire_bytes(magic)), (32, 64))
            except ProtocolError:
                pass


if __name__ == "__main__":
    unittest.main()
