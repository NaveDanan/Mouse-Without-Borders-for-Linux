import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock

from mwb_linux.config import Config
from mwb_linux.crypto import CryptoProfile
from mwb_linux.file_transfer import (
    FileTransferManager,
    TransferPeer,
    safe_remote_name,
    unique_destination,
    windows_safe_name,
)
from mwb_linux.input import WM_LBUTTONUP
from mwb_linux.protocol import Packet, PackageType


class FileTransferTests(unittest.TestCase):
    def _manager(self, name, machine_id, root, profile):
        config = Config(
            machine_name=name,
            machine_id=machine_id,
            secret="0123456789abcdef",
            share_images=True,
        )
        return FileTransferManager(
            config,
            Mock(),
            Mock(),
            Mock(),
            Mock(),
            destination_root=root,
        ), TransferPeer(name, machine_id, "local", profile.value)

    def test_secondary_socket_transfers_unaligned_file_for_every_crypto_profile(self):
        for profile in CryptoProfile:
            with self.subTest(profile=profile), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = root / "source" / "cross-screen.bin"
                source.parent.mkdir()
                content = bytes(range(251)) * 263
                source.write_bytes(content)
                destination_root = root / "received"
                sender, sender_peer = self._manager("linux", 10, root, profile)
                receiver, receiver_peer = self._manager(
                    "windows", 20, destination_root, profile
                )
                sender._staged_file = source
                sender._staged_until = time.monotonic() + 10
                left, right = socket.socketpair()
                errors = []
                received = []

                def send():
                    stream = None
                    try:
                        stream, request = sender._handshake(
                            left, receiver_peer, push=True
                        )
                        self.assertEqual(request.type, PackageType.CLIPBOARD)
                        sender._send_staged_file(stream)
                    except Exception as exc:
                        errors.append(exc)
                    finally:
                        if stream:
                            stream.close()

                def receive():
                    stream = None
                    try:
                        stream, response = receiver._handshake(
                            right,
                            sender_peer,
                            push=False,
                            post_action=1,
                        )
                        self.assertEqual(response.type, PackageType.CLIPBOARD_PUSH)
                        received.append(receiver._receive_file(stream, sender_peer))
                    except Exception as exc:
                        errors.append(exc)
                    finally:
                        if stream:
                            stream.close()

                threads = [threading.Thread(target=send), threading.Thread(target=receive)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(8)
                    self.assertFalse(thread.is_alive())

                self.assertEqual(errors, [])
                self.assertEqual(len(received), 1)
                self.assertEqual(received[0].name, source.name)
                self.assertEqual(received[0].read_bytes(), content)

    def test_base_port_listener_serves_an_authenticated_peer_end_to_end(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "live-listener.txt"
            source.write_text("through the listener", encoding="utf-8")
            probe = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
            probe.bind(("::1", 0))
            port = probe.getsockname()[1]
            probe.close()
            profile = CryptoProfile.LEGACY_50K
            linux_config = Config(
                machine_name="linux",
                machine_id=10,
                secret="0123456789abcdef",
                port=port,
                share_images=True,
            )
            windows_config = Config(
                machine_name="windows",
                machine_id=20,
                secret="0123456789abcdef",
                port=port,
                share_images=True,
            )
            linux_peer = TransferPeer("linux", 10, "::1", profile.value)
            windows_peer = TransferPeer("windows", 20, "::1", profile.value)
            sender = FileTransferManager(
                linux_config,
                Mock(),
                Mock(),
                lambda _address: windows_peer,
                Mock(),
            )
            receiver = FileTransferManager(
                windows_config,
                Mock(),
                Mock(),
                Mock(),
                Mock(),
                destination_root=root / "download",
            )
            sender._staged_file = source
            sender._staged_until = time.monotonic() + 10
            sender.start()
            stream = None
            try:
                raw = socket.create_connection(("::1", port), timeout=3)
                stream, response = receiver._handshake(
                    raw, linux_peer, push=False, post_action=1
                )
                self.assertEqual(response.type, PackageType.CLIPBOARD_PUSH)
                destination = receiver._receive_file(stream, linux_peer)
            finally:
                if stream:
                    stream.close()
                sender.stop()

            self.assertEqual(destination.read_text(encoding="utf-8"), "through the listener")

    def test_clipboard_ask_reverse_pushes_staged_file_end_to_end(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "reverse-push.bin"
            content = bytes(range(251)) * 263
            source.write_bytes(content)
            probe = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
            probe.bind(("::1", 0))
            port = probe.getsockname()[1]
            probe.close()
            profile = CryptoProfile.STANDALONE_50K
            linux_peer = TransferPeer("linux", 10, "::1", profile.value)
            windows_peer = TransferPeer("windows", 20, "::1", profile.value)
            sender = FileTransferManager(
                Config(
                    machine_name="linux",
                    machine_id=10,
                    secret="0123456789abcdef",
                    port=port,
                    share_images=True,
                ),
                Mock(),
                lambda machine_id: windows_peer if machine_id == 20 else None,
                Mock(),
                Mock(),
            )
            receiver = FileTransferManager(
                Config(
                    machine_name="windows",
                    machine_id=20,
                    secret="0123456789abcdef",
                    port=port,
                    share_images=True,
                ),
                Mock(),
                Mock(),
                lambda _address: linux_peer,
                Mock(),
                destination_root=root / "download",
            )
            sender._staged_file = source
            sender._staged_until = time.monotonic() + 10
            receiver._expected_push_id = 10
            receiver._expected_push_until = time.monotonic() + 10
            receiver.start()
            try:
                request = Packet()
                request.type = PackageType.CLIPBOARD_ASK
                request.src = 20
                request.dest = 10
                sender.process_packet(request)
                destination = root / "download" / source.name
                deadline = time.monotonic() + 5
                while not destination.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
            finally:
                receiver.stop()

            self.assertTrue(destination.exists())
            self.assertEqual(destination.read_bytes(), content)

    def test_failed_direct_download_requests_an_authenticated_reverse_push(self):
        packets = []
        peer = TransferPeer("windows", 20, "127.0.0.1", "legacy-50k")
        manager = FileTransferManager(
            Config(machine_name="linux", machine_id=10, share_images=True),
            packets.append,
            lambda machine_id: peer if machine_id == 20 else None,
            Mock(),
            Mock(),
        )
        manager._download_direct = Mock(side_effect=ConnectionRefusedError("closed"))

        manager._download_worker(20)

        self.assertEqual(len(packets), 1)
        self.assertEqual(packets[0].type, PackageType.CLIPBOARD_ASK)
        self.assertEqual(packets[0].dest, 20)
        self.assertEqual(packets[0].machine_name, "linux")
        self.assertEqual(manager._expected_push_id, 20)

    def test_linux_drag_probe_advertises_then_targets_the_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "drag me.txt"
            path.write_text("payload", encoding="utf-8")
            packets = []
            peer = TransferPeer("windows", 20, "127.0.0.1", "legacy-50k")
            manager = FileTransferManager(
                Config(machine_name="linux", machine_id=10, share_images=True),
                packets.append,
                lambda machine_id: peer if machine_id == 20 else None,
                Mock(),
                Mock(),
                drag_probe=lambda: path,
            )
            request = Packet()
            request.type = PackageType.EXPLORER_DRAG_DROP
            request.src = 20
            request.dest = 10

            manager.process_packet(request)
            deadline = time.monotonic() + 2
            while len(packets) < 2 and time.monotonic() < deadline:
                time.sleep(0.01)

            self.assertEqual(
                [packet.type for packet in packets],
                [
                    PackageType.CLIPBOARD_DRAG_DROP,
                    PackageType.CLIPBOARD_DRAG_DROP_OPERATION,
                ],
            )
            self.assertEqual(packets[0].dest, 255)
            self.assertEqual(packets[1].dest, 20)

    def test_linux_control_transition_probes_then_cancels_a_local_drag(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "edge-drag.txt"
            path.write_text("payload", encoding="utf-8")
            packets = []
            peer = TransferPeer("windows", 20, "127.0.0.1", "legacy-50k")
            manager = FileTransferManager(
                Config(machine_name="linux", machine_id=10, share_images=True),
                packets.append,
                lambda machine_id: peer if machine_id == 20 else None,
                Mock(),
                Mock(),
                drag_probe=lambda: path,
            )

            manager.control_changed(20)
            deadline = time.monotonic() + 2
            while len(packets) < 2 and time.monotonic() < deadline:
                time.sleep(0.01)
            manager.control_changed(None)

            self.assertEqual(
                [packet.type for packet in packets],
                [
                    PackageType.CLIPBOARD_DRAG_DROP,
                    PackageType.CLIPBOARD_DRAG_DROP_OPERATION,
                    PackageType.CLIPBOARD_DRAG_DROP_END,
                ],
            )
            self.assertEqual(packets[-1].dest, 20)

    def test_drag_end_cancels_a_probe_that_has_not_returned_yet(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "late-result.txt"
            path.write_text("payload", encoding="utf-8")
            entered = threading.Event()
            release = threading.Event()
            packets = []
            peer = TransferPeer("windows", 20, "127.0.0.1", "legacy-50k")

            def slow_probe():
                entered.set()
                self.assertTrue(release.wait(2))
                return path

            manager = FileTransferManager(
                Config(machine_name="linux", machine_id=10, share_images=True),
                packets.append,
                lambda machine_id: peer if machine_id == 20 else None,
                Mock(),
                Mock(),
                drag_probe=slow_probe,
            )
            manager.control_changed(20)
            self.assertTrue(entered.wait(1))
            cancelled = Packet()
            cancelled.type = PackageType.CLIPBOARD_DRAG_DROP_END
            cancelled.dest = 10
            manager.process_packet(cancelled)
            release.set()
            deadline = time.monotonic() + 1
            while manager._probe_running and time.monotonic() < deadline:
                time.sleep(0.01)

            self.assertEqual(packets, [])

    def test_windows_drag_download_starts_only_on_left_button_release(self):
        started = threading.Event()
        source_ids = []
        manager = FileTransferManager(
            Config(machine_name="linux", machine_id=10, share_images=True),
            Mock(),
            Mock(),
            Mock(),
            Mock(),
        )
        manager._download_worker = lambda source: (source_ids.append(source), started.set())
        advertisement = Packet()
        advertisement.type = PackageType.CLIPBOARD_DRAG_DROP
        advertisement.src = 20
        operation = Packet()
        operation.type = PackageType.CLIPBOARD_DRAG_DROP_OPERATION
        operation.dest = 10
        manager.process_packet(advertisement)
        manager.process_packet(operation)

        manager.handle_remote_mouse(0x200)
        self.assertFalse(started.wait(0.05))
        manager.handle_remote_mouse(WM_LBUTTONUP)

        self.assertTrue(started.wait(1))
        self.assertEqual(source_ids, [20])

    def test_remote_paths_are_sanitized_and_existing_files_are_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "report.txt").write_text("existing", encoding="utf-8")

            self.assertEqual(safe_remote_name(r"C:\Users\A\report.txt"), "report.txt")
            self.assertEqual(unique_destination(root, "report.txt").name, "report (1).txt")
            self.assertEqual(windows_safe_name("CON?.txt"), "CON_.txt")


if __name__ == "__main__":
    unittest.main()
