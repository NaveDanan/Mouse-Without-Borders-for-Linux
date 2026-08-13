import socket
import tempfile
import threading
import time
import unittest
from errno import ECONNREFUSED, ENETUNREACH
from pathlib import Path
from unittest.mock import Mock, patch

from mwb_linux.config import Config
from mwb_linux.config import HostTarget
from mwb_linux.connection import (
    NETWORK_DOWN_RETRY_SECONDS,
    AuthenticationError,
    ConnectionManager,
    HandshakeTimeout,
    PeerConnection,
    PeerInfo,
    configure_tcp_liveness,
    neighbor_mac,
)
from mwb_linux.crypto import CryptoProfile
from mwb_linux.protocol import Packet, PackageType


class ConnectionTests(unittest.TestCase):
    def test_tcp_connections_use_fast_keepalive_and_user_timeout(self):
        sock = Mock()

        configure_tcp_liveness(sock)

        sock.setsockopt.assert_any_call(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        if hasattr(socket, "TCP_KEEPIDLE"):
            sock.setsockopt.assert_any_call(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 15)
        if hasattr(socket, "TCP_USER_TIMEOUT"):
            sock.setsockopt.assert_any_call(
                socket.IPPROTO_TCP, socket.TCP_USER_TIMEOUT, 20_000
            )

    def test_neighbor_mac_reads_the_kernel_arp_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            arp = Path(directory) / "arp"
            arp.write_text(
                "IP address HW type Flags HW address Mask Device\n"
                "192.168.1.20 0x1 0x2 aa:bb:cc:dd:ee:ff * wlan0\n",
                encoding="ascii",
            )

            self.assertEqual(
                neighbor_mac("192.168.1.20", arp), "aa:bb:cc:dd:ee:ff"
            )

    def test_wake_peer_sends_awake_to_a_connected_lock_screen(self):
        manager = ConnectionManager(
            Config(
                machine_name="linux",
                machine_id=10,
                remote_machines=[{"name": "windows", "address": "192.168.1.20"}],
                machine_matrix=["linux", "windows", "", ""],
            ),
            lambda *_: None,
            lambda *_: None,
        )
        peer = Mock(trusted=True)
        peer.info = PeerInfo(name="windows", machine_id=20)
        manager._connections = [peer]
        manager.broadcast = Mock()

        self.assertTrue(manager.wake_peer("windows"))

        packet = manager.broadcast.call_args.args[0]
        self.assertEqual(packet.type, PackageType.AWAKE)
        self.assertEqual(packet.dest, 20)
        self.assertEqual(packet.machine_name, "linux")

    def test_offline_peer_uses_remembered_mac_for_wake_on_lan(self):
        statuses = []
        manager = ConnectionManager(
            Config(
                machine_name="linux",
                machine_id=10,
                remote_machines=[
                    {
                        "name": "windows",
                        "address": "192.168.1.20",
                        "mac": "aa:bb:cc:dd:ee:ff",
                    }
                ],
                machine_matrix=["linux", "windows", "", ""],
            ),
            lambda *_: None,
            lambda state, message: statuses.append((state, message)),
        )

        with patch("mwb_linux.connection.wake_on_lan", return_value=True) as wake:
            self.assertTrue(manager.wake_peer("windows"))

        self.assertEqual(wake.call_args.args[0], "aa:bb:cc:dd:ee:ff")
        self.assertEqual(statuses[-1][0], "waking")
        self.assertTrue(manager._reconnect.is_set())

    def test_resume_discards_stale_sockets_and_retries_immediately(self):
        statuses = []
        resumed = Mock()
        manager = ConnectionManager(
            Config(),
            lambda *_: None,
            lambda state, message: statuses.append((state, message)),
            resume_callback=resumed,
        )
        connection = Mock(trusted=True)
        connection.info = PeerInfo(name="windows", machine_id=20)
        manager._connections = [connection]
        manager._authentication_failed.add("windows")
        manager._retry_after["windows"] = time.monotonic() + 30

        manager.resume_after_suspend()

        connection.close.assert_called_once_with(notify=False)
        self.assertFalse(manager.connections)
        self.assertFalse(manager._authentication_failed)
        self.assertFalse(manager._retry_after)
        self.assertTrue(manager._reconnect.is_set())
        resumed.assert_called_once_with()
        self.assertEqual(statuses[-1], ("connecting", "System resumed; rebuilding connections"))

    def test_connection_worker_detects_a_boottime_suspend_jump(self):
        manager = ConnectionManager(Config(), lambda *_: None, lambda *_: None)
        manager._suspend_offset = 0
        manager._start_listener = Mock()

        def resumed():
            manager._stop.set()

        with (
            patch("mwb_linux.connection._suspend_offset", return_value=2),
            patch.object(manager, "resume_after_suspend", side_effect=resumed) as resume,
        ):
            manager._run()

        resume.assert_called_once_with()

    def test_signalled_resume_stops_the_polling_fallback_repeating_it(self):
        """logind's resume signal arrives before the boottime poll notices."""

        manager = ConnectionManager(Config(), lambda *_: None, lambda *_: None)
        manager._suspend_offset = 0
        manager._start_listener = Mock()
        ticks = []

        with patch("mwb_linux.connection._suspend_offset", return_value=9):
            # The signal handler runs first, exactly as logind delivers it.
            manager.resume_after_suspend()
            self.assertEqual(manager._suspend_offset, 9)

            def tick(_timeout):
                ticks.append(1)
                manager._stop.set()
                return True

            with (
                patch.object(manager, "resume_after_suspend") as resume,
                patch.object(manager._stop, "wait", side_effect=tick),
            ):
                manager._run()

        self.assertEqual(len(ticks), 1)
        resume.assert_not_called()

    def test_pending_suspend_says_good_bye_over_the_live_socket(self):
        statuses = []
        manager = ConnectionManager(
            Config(),
            lambda *_: None,
            lambda state, message: statuses.append((state, message)),
        )
        connection = Mock(trusted=True)
        connection.info = PeerInfo(name="windows", machine_id=20)
        manager._connections = [connection]

        manager.prepare_for_suspend()

        # notify=True sends BYE_BYE so Windows drops us at once instead of
        # waiting out its own TCP timeout on a frozen peer.
        connection.close.assert_called_once_with()
        self.assertFalse(manager.connections)
        self.assertEqual(
            statuses[-1],
            ("disconnected", "System is suspending; closed remote connections"),
        )

    def test_unreachable_link_retries_steadily_without_reporting_an_error(self):
        statuses = []
        manager = ConnectionManager(
            Config(host="windows", secret="0123456789abcdef"),
            lambda *_: None,
            lambda state, message: statuses.append((state, message)),
        )
        target = manager.config.resolve_hosts()[0]

        with patch(
            "mwb_linux.connection.socket.create_connection",
            side_effect=OSError(ENETUNREACH, "Network is unreachable"),
        ):
            manager._connect_target_worker(target)

        self.assertIn("windows", manager._network_down)
        self.assertEqual(
            statuses[-1], ("connecting", "Waiting for the network to reach windows")
        )
        # The backoff stays untouched so the link coming back reconnects fast.
        self.assertNotIn("windows", manager._retry_delay)
        self.assertLessEqual(
            manager._retry_after["windows"] - time.monotonic(), NETWORK_DOWN_RETRY_SECONDS
        )

    def test_a_refused_port_still_backs_off_exponentially(self):
        statuses = []
        manager = ConnectionManager(
            Config(host="windows", secret="0123456789abcdef"),
            lambda *_: None,
            lambda state, message: statuses.append((state, message)),
        )
        target = manager.config.resolve_hosts()[0]

        with patch(
            "mwb_linux.connection.socket.create_connection",
            side_effect=OSError(ECONNREFUSED, "Connection refused"),
        ):
            manager._connect_target_worker(target)
            manager._connect_target_worker(target)

        self.assertNotIn("windows", manager._network_down)
        self.assertEqual(manager._retry_delay["windows"], 4.0)
        self.assertEqual(statuses[-1][0], "error")

    def test_a_slow_handshake_is_not_reported_as_a_wrong_security_key(self):
        """A peer that is still waking must stay retryable, not blacklisted."""

        statuses = []
        manager = ConnectionManager(
            Config(host="windows", secret="0123456789abcdef"),
            lambda *_: None,
            lambda state, message: statuses.append((state, message)),
        )
        target = manager.config.resolve_hosts()[0]

        with (
            patch("mwb_linux.connection.socket.create_connection", return_value=Mock()),
            patch.object(
                PeerConnection,
                "authenticate",
                side_effect=HandshakeTimeout("standalone-50k handshake did not complete"),
            ),
        ):
            manager._connect_target_worker(target)

        self.assertNotIn("windows", manager._authentication_failed)
        self.assertNotEqual(statuses[-1][0], "invalid_key")
        # A real key mismatch stops retrying; a timeout must keep going.
        self.assertIn("windows", manager._retry_after)

    def test_a_rejected_key_still_stops_retrying_every_profile(self):
        statuses = []
        manager = ConnectionManager(
            Config(host="windows", secret="0123456789abcdef"),
            lambda *_: None,
            lambda state, message: statuses.append((state, message)),
        )
        target = manager.config.resolve_hosts()[0]

        with (
            patch("mwb_linux.connection.socket.create_connection", return_value=Mock()),
            patch.object(
                PeerConnection,
                "authenticate",
                side_effect=AuthenticationError("peer returned an invalid handshake proof"),
            ),
        ):
            manager._connect_target_worker(target)

        self.assertIn("windows", manager._authentication_failed)
        self.assertEqual(statuses[-1][0], "invalid_key")

    def test_handshake_timeout_is_distinct_from_a_proof_failure(self):
        client, server = socket.socketpair()
        self.addCleanup(client.close)
        self.addCleanup(server.close)
        connection = PeerConnection(
            client,
            Config(secret="0123456789abcdef"),
            CryptoProfile.STANDALONE_50K,
            lambda *_: None,
            lambda *_: None,
            outbound=True,
        )

        with self.assertRaises(HandshakeTimeout):
            connection.authenticate(timeout=0.2)

        self.assertNotIsInstance(HandshakeTimeout("slow"), AuthenticationError)

    def test_offline_host_does_not_block_other_target_attempts(self):
        manager = ConnectionManager(Config(), lambda *_: None, lambda *_: None)
        both_started = threading.Event()
        release = threading.Event()
        started = []
        lock = threading.Lock()

        def connect(target):
            with lock:
                started.append(target.name)
                if len(started) == 2:
                    both_started.set()
            release.wait(2)
            return False

        manager._connect_outbound = connect
        manager._start_target_connection(HostTarget("offline", "10.0.0.1"))
        manager._start_target_connection(HostTarget("online", "10.0.0.2"))
        try:
            self.assertTrue(both_started.wait(1))
            self.assertCountEqual(started, ["offline", "online"])
        finally:
            release.set()
            for thread in tuple(manager._target_threads):
                thread.join(2)

    def test_symmetric_handshake_for_every_profile(self):
        for profile in CryptoProfile:
            with self.subTest(profile=profile):
                left_socket, right_socket = socket.socketpair()
                left = PeerConnection(
                    left_socket,
                    Config(secret="0123456789abcdef", machine_name="left", machine_id=100),
                    profile,
                    lambda *_: None,
                    lambda *_: None,
                    outbound=True,
                )
                right = PeerConnection(
                    right_socket,
                    Config(secret="0123456789abcdef", machine_name="right", machine_id=200),
                    profile,
                    lambda *_: None,
                    lambda *_: None,
                    outbound=False,
                )
                errors = []

                def authenticate(peer):
                    try:
                        peer.authenticate(timeout=3)
                    except Exception as exc:  # assertion reports the original failure
                        errors.append(exc)

                threads = [
                    threading.Thread(target=authenticate, args=(left,)),
                    threading.Thread(target=authenticate, args=(right,)),
                ]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(5)
                    self.assertFalse(thread.is_alive())
                self.assertEqual(errors, [])
                self.assertEqual(left.info.name, "right")
                self.assertEqual(right.info.name, "left")
                self.assertEqual(left.info.machine_id, 200)
                self.assertEqual(right.info.machine_id, 100)
                self.assertEqual(left._next_packet_id, 0)
                self.assertEqual(right._next_packet_id, 0)
                left.close(notify=False)
                right.close(notify=False)

    def test_connect_retries_after_an_invalid_key_state(self):
        manager = ConnectionManager(Config(), lambda *_: None, lambda *_: None)
        manager._worker = Mock()
        manager._worker.is_alive.return_value = True
        manager._authentication_failed.add("windows")
        manager._reconnect.clear()

        manager.start()

        self.assertFalse(manager._authentication_failed)
        self.assertTrue(manager._reconnect.is_set())

    def test_one_connection_in_each_direction_is_not_a_duplicate(self):
        manager = ConnectionManager(Config(), lambda *_: None, lambda *_: None)
        outbound = Mock()
        outbound.info = PeerInfo(name="windows")
        outbound.outbound = True
        inbound = Mock()
        inbound.info = PeerInfo(name="windows")
        inbound.outbound = False

        manager._register(outbound)
        manager._register(inbound)

        self.assertEqual(manager.connections, (outbound, inbound))

        duplicate_inbound = Mock()
        duplicate_inbound.info = PeerInfo(name="windows")
        duplicate_inbound.outbound = False
        with self.assertRaisesRegex(AuthenticationError, "same-direction"):
            manager._register(duplicate_inbound)
        duplicate_inbound.close.assert_called_once_with(notify=False)
        self.assertEqual(manager.connections, (outbound, inbound))

    def test_authenticated_incoming_channel_reports_connected(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        right_socket = socket.create_connection(listener.getsockname(), timeout=3)
        left_socket, _ = listener.accept()
        listener.close()
        statuses = []
        manager = ConnectionManager(
            Config(
                host="127.0.0.1",
                host_name="windows",
                secret="0123456789abcdef",
                machine_name="linux",
                machine_id=100,
            ),
            lambda *_: None,
            lambda state, message: statuses.append((state, message)),
        )
        manager._working_profiles["windows"] = CryptoProfile.RANDOM_100K
        peer = PeerConnection(
            right_socket,
            Config(
                secret="0123456789abcdef",
                machine_name="windows",
                machine_id=200,
            ),
            CryptoProfile.RANDOM_100K,
            lambda *_: None,
            lambda *_: None,
            outbound=True,
        )
        thread = threading.Thread(target=manager._authenticate_incoming, args=(left_socket,))
        try:
            thread.start()
            peer.authenticate(timeout=3)
            thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertTrue(manager.connected)
            self.assertEqual(statuses[-1][0], "connected")
            self.assertIn("windows", statuses[-1][1])
            self.assertIn("random-100k", statuses[-1][1])
        finally:
            manager.stop()
            peer.close(notify=False)

    def test_ipv4_mapped_and_dns_addresses_select_the_targets_working_profile(self):
        manager = ConnectionManager(
            Config(
                machine_name="linux",
                machine_id=100,
                remote_machines=[{"name": "windows", "address": "naved"}],
                machine_matrix=["linux", "windows", "", ""],
            ),
            lambda *_: None,
            lambda *_: None,
        )
        manager._working_profiles["windows"] = CryptoProfile.RANDOM_100K
        dns_result = [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("192.168.1.53", 0),
            )
        ]

        with patch("mwb_linux.connection.socket.getaddrinfo", return_value=dns_result):
            target = manager._target_for_peer_address("::ffff:192.168.1.53")

        self.assertIsNotNone(target)
        self.assertEqual(target.name, "windows")
        self.assertEqual(manager._profiles(target.name)[0], CryptoProfile.RANDOM_100K)

    def test_explicit_profile_is_used_for_an_incoming_channel(self):
        manager = ConnectionManager(
            Config(crypto_profile=CryptoProfile.RANDOM_50K.value),
            lambda *_: None,
            lambda *_: None,
        )

        self.assertEqual(manager._profiles("any-pc"), (CryptoProfile.RANDOM_50K,))

    def test_stop_while_create_connection_is_blocked_cannot_register_late(self):
        manager = ConnectionManager(Config(), lambda *_: None, lambda *_: None)
        create_started = threading.Event()
        release_create = threading.Event()
        fake_socket = Mock()

        def create_connection(*_args, **_kwargs):
            create_started.set()
            release_create.wait(2)
            return fake_socket

        result = []
        with (
            patch("mwb_linux.connection.socket.create_connection", side_effect=create_connection),
            patch("mwb_linux.connection.PeerConnection") as peer_connection,
        ):
            thread = threading.Thread(
                target=lambda: result.append(
                    manager._connect_outbound(HostTarget("windows", "192.168.1.53"))
                )
            )
            thread.start()
            self.assertTrue(create_started.wait(1))
            manager.stop()
            release_create.set()
            thread.join(2)

        self.assertFalse(thread.is_alive())
        self.assertEqual(result, [False])
        peer_connection.assert_not_called()
        self.assertFalse(manager.connections)

    def test_stop_while_authenticating_cannot_register_late(self):
        manager = ConnectionManager(Config(), lambda *_: None, lambda *_: None)
        authentication_started = threading.Event()
        release_authentication = threading.Event()
        fake_socket = Mock()
        connection = Mock()
        connection.info = PeerInfo(name="windows", machine_id=200)
        connection.trusted = True

        def authenticate():
            authentication_started.set()
            release_authentication.wait(2)

        connection.authenticate.side_effect = authenticate
        result = []
        with (
            patch("mwb_linux.connection.socket.create_connection", return_value=fake_socket),
            patch("mwb_linux.connection.PeerConnection", return_value=connection),
        ):
            thread = threading.Thread(
                target=lambda: result.append(
                    manager._connect_outbound(HostTarget("windows", "192.168.1.53"))
                )
            )
            thread.start()
            self.assertTrue(authentication_started.wait(1))
            manager.stop()
            release_authentication.set()
            thread.join(2)

        self.assertFalse(thread.is_alive())
        self.assertEqual(result, [False])
        connection.start.assert_not_called()
        connection.close.assert_called_with(notify=False)
        self.assertFalse(manager.connections)

    def test_incoming_socket_closed_before_authentication_exits_cleanly(self):
        manager = ConnectionManager(Config(), lambda *_: None, lambda *_: None)
        incoming, peer = socket.socketpair()
        incoming.close()
        peer.close()

        manager._authenticate_incoming(incoming)

        self.assertFalse(manager.connections)

    def test_vertical_matrix_uses_two_rows(self):
        sock, peer = socket.socketpair()
        try:
            config = Config(
                host="windows",
                host_name="windows",
                secret="0123456789abcdef",
                machine_name="linux",
                machine_id=100,
                host_position="top",
                two_row=True,
            )
            connection = PeerConnection(
                sock,
                config,
                CryptoProfile.LEGACY_50K,
                lambda *_: None,
                lambda *_: None,
                outbound=True,
            )
            connection.info.name = "windows"
            sent = []
            connection.send_packet = lambda packet, **_kwargs: sent.append(packet)

            connection.send_matrix()

            self.assertEqual([packet.type for packet in sent], [132] * 4)
            self.assertEqual(
                [packet.machine_name for packet in sent],
                ["windows", "", "linux", ""],
            )
            self.assertEqual([packet.src for packet in sent], [1, 2, 3, 4])
        finally:
            sock.close()
            peer.close()

    def test_matrix_sends_all_four_computers_and_flags(self):
        sock, peer = socket.socketpair()
        try:
            config = Config(
                secret="0123456789abcdef",
                machine_name="linux",
                machine_id=100,
                remote_machines=[
                    {"name": "left", "address": "10.0.0.1"},
                    {"name": "right", "address": "10.0.0.2"},
                    {"name": "bottom", "address": "10.0.0.3"},
                ],
                machine_matrix=["left", "linux", "right", "bottom"],
                two_row=True,
                other_options={"wrap_mouse": True},
            )
            connection = PeerConnection(
                sock,
                config,
                CryptoProfile.LEGACY_50K,
                lambda *_: None,
                lambda *_: None,
                outbound=True,
            )
            sent = []
            connection.send_packet = lambda packet, **_kwargs: sent.append(packet)

            connection.send_matrix()

            self.assertEqual([packet.type for packet in sent], [134] * 4)
            self.assertEqual(
                [packet.machine_name for packet in sent],
                ["left", "linux", "right", "bottom"],
            )
            self.assertEqual([packet.src for packet in sent], [1, 2, 3, 4])
        finally:
            sock.close()
            peer.close()

    def test_directed_packets_are_not_sent_to_unrelated_peers(self):
        manager = ConnectionManager(Config(), lambda *_: None, lambda *_: None)
        manager._next_runtime_packet_id = 6
        left = Mock()
        left.trusted = True
        left.info = PeerInfo(name="left", machine_id=200)
        right = Mock()
        right.trusted = True
        right.info = PeerInfo(name="right", machine_id=300)
        manager._connections = [left, right]
        packet = Packet()
        packet.type = PackageType.MOUSE
        packet.dest = 300

        manager.broadcast(packet)

        self.assertEqual(packet.packet_id, 7)
        left.send_packet.assert_not_called()
        right.send_packet.assert_called_once_with(packet, assign_id=False)

    def test_packet_ids_remain_global_and_monotonic_after_channel_failover(self):
        manager = ConnectionManager(Config(), lambda *_: None, lambda *_: None)
        manager._next_runtime_packet_id = 40
        outbound = Mock()
        outbound.trusted = True
        outbound.info = PeerInfo(name="windows", machine_id=200)
        inbound = Mock()
        inbound.trusted = True
        inbound.info = PeerInfo(name="windows", machine_id=200)
        manager._connections = [outbound, inbound]

        first = Packet()
        first.type = PackageType.MOUSE
        first.dest = 200
        manager.broadcast(first)
        manager._connections.remove(outbound)
        second = Packet()
        second.type = PackageType.MOUSE
        second.dest = 200
        manager.broadcast(second)

        self.assertEqual(first.packet_id, 41)
        self.assertEqual(second.packet_id, 42)
        outbound.send_packet.assert_called_once_with(first, assign_id=False)
        self.assertEqual(
            inbound.send_packet.call_args_list[0].args[0].packet_id,
            41,
        )
        self.assertEqual(
            inbound.send_packet.call_args_list[1].args[0].packet_id,
            42,
        )

    def test_managed_connections_share_runtime_packet_id_provider(self):
        manager = ConnectionManager(Config(), lambda *_: None, lambda *_: None)
        manager._next_runtime_packet_id = 100
        first_socket, first_peer = socket.socketpair()
        second_socket, second_peer = socket.socketpair()
        first = PeerConnection(
            first_socket,
            Config(secret="0123456789abcdef", machine_id=100),
            CryptoProfile.LEGACY_50K,
            lambda *_: None,
            lambda *_: None,
            outbound=True,
            packet_id_provider=manager._new_packet_id,
        )
        second = PeerConnection(
            second_socket,
            Config(secret="0123456789abcdef", machine_id=100),
            CryptoProfile.LEGACY_50K,
            lambda *_: None,
            lambda *_: None,
            outbound=False,
            packet_id_provider=manager._new_packet_id,
        )
        first.stream.send = Mock()
        second.stream.send = Mock()
        try:
            first_packet = Packet()
            first_packet.type = PackageType.HEARTBEAT
            first.send_packet(first_packet)
            second_packet = Packet()
            second_packet.type = PackageType.HEARTBEAT
            second.send_packet(second_packet)

            self.assertEqual(first_packet.packet_id, 101)
            self.assertEqual(second_packet.packet_id, 102)
        finally:
            first.close(notify=False)
            second.close(notify=False)
            first_peer.close()
            second_peer.close()

    def test_peer_loss_status_waits_until_its_last_channel_closes(self):
        statuses = []
        manager = ConnectionManager(
            Config(),
            lambda *_: None,
            lambda state, message: statuses.append((state, message)),
        )
        first_outbound = Mock()
        first_outbound.trusted = True
        first_outbound.outbound = True
        first_outbound.info = PeerInfo(name="first", machine_id=200)
        first_inbound = Mock()
        first_inbound.trusted = True
        first_inbound.outbound = False
        first_inbound.info = PeerInfo(name="first", machine_id=200)
        second = Mock()
        second.trusted = True
        second.outbound = True
        second.info = PeerInfo(name="second", machine_id=300)
        manager._connections = [first_outbound, first_inbound, second]

        manager._connection_closed(first_outbound, None)
        self.assertEqual(statuses, [])
        manager._connection_closed(first_inbound, None)

        self.assertEqual(
            statuses,
            [("connected", "1 connected; first disconnected")],
        )

    def test_listener_can_restart_immediately_on_the_same_port(self):
        probe = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        probe.bind(("::", 0))
        base_port = probe.getsockname()[1] - 1
        probe.close()
        self.assertGreater(base_port, 0)
        config = Config(port=base_port)

        first = ConnectionManager(config, lambda *_: None, lambda *_: None)
        second = ConnectionManager(config, lambda *_: None, lambda *_: None)
        try:
            first._start_listener()
            self.assertIsNotNone(first._listener)
            first.stop()

            second._start_listener()
            self.assertIsNotNone(second._listener)
        finally:
            first.stop()
            second.stop()

    def test_authenticated_runtime_routes_input_and_clipboard_both_ways(self):
        left_socket, right_socket = socket.socketpair()
        left_packets = []
        right_packets = []
        packet_ready = threading.Event()

        def left_callback(_connection, packet):
            left_packets.append(packet)
            if len(left_packets) >= 2:
                packet_ready.set()

        def right_callback(_connection, packet):
            right_packets.append(packet)
            packet_ready.set()

        left = PeerConnection(
            left_socket,
            Config(secret="0123456789abcdef", machine_name="linux", machine_id=100),
            CryptoProfile.LEGACY_50K,
            left_callback,
            lambda *_: None,
            outbound=True,
        )
        right = PeerConnection(
            right_socket,
            Config(secret="0123456789abcdef", machine_name="windows", machine_id=200),
            CryptoProfile.LEGACY_50K,
            right_callback,
            lambda *_: None,
            outbound=False,
        )

        errors = []
        threads = [
            threading.Thread(target=lambda: self._authenticate(left, errors)),
            threading.Thread(target=lambda: self._authenticate(right, errors)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        self.assertEqual(errors, [])
        left.start()
        right.start()

        key = Packet()
        key.type = PackageType.KEYBOARD
        key.dest = 100
        key.keyboard = (0x41, 0)
        right.send_packet(key)

        clipboard = bytes(range(60))
        for offset in range(0, len(clipboard), 48):
            chunk = Packet()
            chunk.type = PackageType.CLIPBOARD_TEXT
            chunk.dest = 100
            chunk.clipboard_payload = clipboard[offset : offset + 48]
            right.send_packet(chunk)
        end = Packet()
        end.type = PackageType.CLIPBOARD_DATA_END
        end.dest = 100
        right.send_packet(end)

        self.assertTrue(packet_ready.wait(3))
        deadline = time.monotonic() + 3
        while len(left_packets) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(left_packets[0].keyboard, (0x41, 0))
        self.assertEqual(left_packets[1].type, PackageType.CLIPBOARD_TEXT)
        self.assertTrue(left_packets[1].complete_clipboard.startswith(clipboard))

        packet_ready.clear()
        mouse = Packet()
        mouse.type = PackageType.MOUSE
        mouse.dest = 200
        mouse.mouse = (123, 456, 0, 0x200)
        left.send_packet(mouse)
        self.assertTrue(packet_ready.wait(3))
        self.assertEqual(right_packets[0].mouse, (123, 456, 0, 0x200))

        left.close(notify=False)
        right.close(notify=False)

    @staticmethod
    def _authenticate(connection, errors):
        try:
            connection.authenticate(timeout=3)
        except Exception as exc:
            errors.append(exc)


if __name__ == "__main__":
    unittest.main()
