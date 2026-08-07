import os
import socket
import threading
import unittest

from mwb_linux.crypto import CryptoProfile, EncryptedSocket, derive_key


class CryptoTests(unittest.TestCase):
    def test_pbkdf2_golden_vector(self):
        self.assertEqual(
            derive_key("password", b"salt", 50_000).hex(),
            "337dbf33fe200d5c6d34340bd8eaac18285f43b6e46947efee3a4d54e8f2a32f",
        )

    def test_standalone_pbkdf2_sha1_golden_vector(self):
        salt = "18446744073709551615".encode("utf-16le")
        self.assertEqual(
            derive_key("0123456789abcdef", salt, 50_000, "sha1").hex(),
            "7bd4fe8baf32239a4675bf41dcefa845019dfb2db73f6847fd68f92ab867c9be",
        )

    def test_all_profiles_are_full_duplex(self):
        for profile in CryptoProfile:
            with self.subTest(profile=profile):
                left_socket, right_socket = socket.socketpair()
                left = EncryptedSocket(left_socket, "0123456789abcdef", profile)
                right = EncryptedSocket(right_socket, "0123456789abcdef", profile)
                left_message = os.urandom(64)
                right_message = os.urandom(32)
                results = {}

                def left_side():
                    left.send(left_message)
                    results["left"] = left.receive(len(right_message))

                def right_side():
                    right.send(right_message)
                    results["right"] = right.receive(len(left_message))

                threads = [threading.Thread(target=left_side), threading.Thread(target=right_side)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(5)
                    self.assertFalse(thread.is_alive())
                self.assertEqual(results["left"], right_message)
                self.assertEqual(results["right"], left_message)
                left.close()
                right.close()


if __name__ == "__main__":
    unittest.main()
