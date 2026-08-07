"""Authenticated control-channel connection and reconnect management."""

from __future__ import annotations

import logging
import os
import re
import secrets
import socket
import threading
import time
from ipaddress import ip_address
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .config import Config, HostTarget
from .crypto import CryptoProfile, EncryptedSocket
from .protocol import (
    ID_ALL,
    ID_NONE,
    PACKAGE_SIZE,
    PACKAGE_SIZE_EX,
    Packet,
    PackageType,
    ProtocolError,
    magic_number,
    packet_type_from_first_block,
)

LOGGER = logging.getLogger(__name__)
HEARTBEAT_INTERVAL = 15.0
RESUME_OFFSET_THRESHOLD = 1.0
TCP_KEEPIDLE_SECONDS = 15
TCP_KEEPINTVL_SECONDS = 5
TCP_KEEPCNT = 3
TCP_USER_TIMEOUT_MS = 20_000
MAC_ADDRESS_PATTERN = re.compile(r"^(?:[0-9a-f]{2}:){5}[0-9a-f]{2}$")


def _suspend_offset() -> float:
    """Return time spent suspended since boot on Linux."""

    clock = getattr(time, "CLOCK_BOOTTIME", None)
    if clock is None:
        return 0.0
    return time.clock_gettime(clock) - time.monotonic()


def configure_tcp_liveness(sock: socket.socket) -> None:
    """Make a dead post-suspend TCP path fail in seconds instead of hours."""

    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    options = (
        ("TCP_KEEPIDLE", TCP_KEEPIDLE_SECONDS),
        ("TCP_KEEPINTVL", TCP_KEEPINTVL_SECONDS),
        ("TCP_KEEPCNT", TCP_KEEPCNT),
        ("TCP_USER_TIMEOUT", TCP_USER_TIMEOUT_MS),
    )
    for option_name, value in options:
        option = getattr(socket, option_name, None)
        if option is None:
            continue
        try:
            sock.setsockopt(socket.IPPROTO_TCP, option, value)
        except OSError:
            # Kernels differ in which optional TCP settings they expose. The
            # portable SO_KEEPALIVE setting above still provides a fallback.
            LOGGER.debug("TCP liveness option %s is unavailable", option_name)


def neighbor_mac(address: str, arp_path: Path = Path("/proc/net/arp")) -> str:
    """Return a same-LAN peer's MAC from the kernel neighbour cache."""

    wanted = address.split("%", 1)[0]
    try:
        lines = arp_path.read_text(encoding="ascii", errors="replace").splitlines()[1:]
    except OSError:
        return ""
    for line in lines:
        columns = line.split()
        if len(columns) < 4 or columns[0] != wanted:
            continue
        mac = columns[3].lower()
        if mac != "00:00:00:00:00:00" and MAC_ADDRESS_PATTERN.fullmatch(mac):
            return mac
    return ""


def wake_on_lan(mac: str, addresses: tuple[str, ...] = ()) -> bool:
    """Send a Wake-on-LAN magic packet by broadcast and resolved unicast."""

    normalized = mac.strip().lower().replace("-", ":")
    if not MAC_ADDRESS_PATTERN.fullmatch(normalized):
        return False
    payload = b"\xff" * 6 + bytes.fromhex(normalized.replace(":", "")) * 16
    destinations = {"255.255.255.255", *addresses}
    sent = False
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as wake_socket:
        wake_socket.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for destination in destinations:
            try:
                wake_socket.sendto(payload, (destination, 9))
                sent = True
            except OSError:
                continue
    return sent


class AuthenticationError(ConnectionError):
    """The peer did not prove knowledge of the shared key."""


@dataclass(slots=True)
class PeerInfo:
    name: str = ""
    machine_id: int = ID_NONE
    address: str = ""
    profile: str = ""
    connected_since: float = 0.0


class PeerConnection:
    """One full-duplex MWB control socket."""

    def __init__(
        self,
        sock: socket.socket,
        config: Config,
        profile: CryptoProfile,
        packet_callback: Callable[["PeerConnection", Packet], None],
        closed_callback: Callable[["PeerConnection", Exception | None], None],
        *,
        outbound: bool,
        packet_id_provider: Callable[[], int] | None = None,
    ) -> None:
        self.socket = sock
        self.config = config
        self.profile = profile
        self.stream = EncryptedSocket(sock, config.secret, profile)
        magic_hash = "sha256" if profile == CryptoProfile.STANDALONE_50K else "sha512"
        self.magic = magic_number(config.secret, magic_hash)
        self.packet_callback = packet_callback
        self.closed_callback = closed_callback
        self.outbound = outbound
        peer_name = sock.getpeername()
        address = peer_name[0] if isinstance(peer_name, tuple) else peer_name
        self.info = PeerInfo(address=str(address or "local"), profile=profile.value)
        self.trusted = False
        self._closed = threading.Event()
        self._send_lock = threading.Lock()
        self._next_packet_id = 0
        self._packet_id_provider = packet_id_provider
        self._receiver: threading.Thread | None = None
        self._challenge_response: tuple[int, int, int, int] | None = None
        self._clipboard_packets: list[bytes] | None = None
        self._clipboard_is_image = False

    def _new_packet_id(self) -> int:
        if self._packet_id_provider is not None:
            return self._packet_id_provider()
        with self._send_lock:
            self._next_packet_id += 1
            if self._next_packet_id > 0x7FFFFFFF:
                self._next_packet_id = 1
            return self._next_packet_id

    def send_packet(self, packet: Packet, *, assign_id: bool = True) -> None:
        if self._closed.is_set():
            raise ConnectionError("connection is closed")
        if assign_id:
            packet.packet_id = self._new_packet_id()
        if packet.src == ID_NONE:
            packet.src = self.config.machine_id
        with self._send_lock:
            self.stream.send(packet.wire_bytes(self.magic))

    def receive_packet(self) -> Packet:
        first = self.stream.receive(PACKAGE_SIZE)
        _, big = packet_type_from_first_block(first, self.magic)
        data = first + (self.stream.receive(PACKAGE_SIZE) if big else b"")
        return Packet.decode(data, self.magic)

    def authenticate(self, timeout: float = 8.0) -> None:
        """Perform the symmetric ten-challenge MWB handshake."""

        self.socket.settimeout(timeout)
        challenge = Packet.random(os.urandom(PACKAGE_SIZE_EX))
        challenge.type = PackageType.HANDSHAKE
        challenge.machine_name = self.config.machine_name
        original_words = challenge.machine_words
        self._challenge_response = tuple(
            (~word) & 0xFFFFFFFF for word in original_words
        )
        for _ in range(10):
            # Windows deliberately reuses the randomized ID/Src/Des challenge.
            self.send_packet(challenge, assign_id=False)

        attempts = 0
        while attempts < 30:
            attempts += 1
            try:
                packet = self.receive_packet()
            except (ProtocolError, TimeoutError, socket.timeout, EOFError) as exc:
                raise AuthenticationError(
                    f"{self.profile.value} handshake failed: {exc}"
                ) from exc
            if packet.type == PackageType.HANDSHAKE:
                packet.type = PackageType.HANDSHAKE_ACK
                packet.src = ID_NONE
                packet.machine_name = self.config.machine_name
                packet.complement_machine_words()
                # Windows' direct TcpSend handshake path preserves the random
                # challenge ID. Handshakes are also excluded from receiver
                # de-duplication, so they must not consume runtime IDs.
                self.send_packet(packet, assign_id=False)
                continue
            if packet.type == PackageType.HANDSHAKE_ACK:
                if packet.machine_words != self._challenge_response:
                    raise AuthenticationError("peer returned an invalid handshake proof")
                if packet.src in (ID_NONE, ID_ALL, self.config.machine_id):
                    raise AuthenticationError("peer returned an invalid machine ID")
                self.info.name = packet.machine_name
                self.info.machine_id = packet.src
                self.info.connected_since = time.time()
                self.trusted = True
                self.socket.settimeout(None)
                return
        raise AuthenticationError("peer did not return a handshake acknowledgement")

    def start(self) -> None:
        if not self.trusted:
            raise RuntimeError("cannot start an unauthenticated connection")
        self._receiver = threading.Thread(
            target=self._receive_loop,
            name=f"mwb-recv-{self.info.name}",
            daemon=True,
        )
        self._receiver.start()

    def _receive_loop(self) -> None:
        failure: Exception | None = None
        try:
            while not self._closed.is_set():
                packet = self.receive_packet()
                if packet.type in (PackageType.HANDSHAKE, PackageType.HANDSHAKE_ACK):
                    if packet.type == PackageType.HANDSHAKE:
                        packet.type = PackageType.HANDSHAKE_ACK
                        packet.src = ID_NONE
                        packet.machine_name = self.config.machine_name
                        packet.complement_machine_words()
                        self.send_packet(packet, assign_id=False)
                    continue
                if packet.type in (
                    PackageType.CLIPBOARD_TEXT,
                    PackageType.CLIPBOARD_IMAGE,
                ):
                    if self._clipboard_packets is None:
                        self._clipboard_packets = []
                        self._clipboard_is_image = (
                            packet.type == PackageType.CLIPBOARD_IMAGE
                        )
                    self._clipboard_packets.append(packet.clipboard_payload)
                    continue
                if packet.type == PackageType.CLIPBOARD_DATA_END:
                    if self._clipboard_packets is not None:
                        combined = b"".join(self._clipboard_packets)
                        synthetic = Packet()
                        synthetic.type = (
                            PackageType.CLIPBOARD_IMAGE
                            if self._clipboard_is_image
                            else PackageType.CLIPBOARD_TEXT
                        )
                        synthetic.raw[16:64] = b"\0" * 48
                        # Attach complete data without changing the wire Packet API.
                        synthetic.complete_clipboard = combined
                        self.packet_callback(self, synthetic)
                    self._clipboard_packets = None
                    continue
                self.packet_callback(self, packet)
        except (OSError, EOFError, ProtocolError, ConnectionError) as exc:
            failure = exc
            if not self._closed.is_set():
                LOGGER.info("control connection ended: %s", exc)
        finally:
            self.close(notify=False)
            self.closed_callback(self, failure)

    def send_heartbeat(self) -> None:
        packet = Packet()
        packet.type = PackageType.HEARTBEAT
        packet.dest = ID_ALL
        packet.machine_name = self.config.machine_name
        self.send_packet(packet)

    def send_matrix(self) -> None:
        # Windows synchronizes the complete four-computer matrix, not merely
        # the peer on this socket. Every authenticated peer therefore receives
        # the same ordered snapshot.
        for index, name in enumerate(self.config.machine_matrix, start=1):
            packet = Packet()
            packet.type = (
                int(PackageType.MATRIX)
                | (2 if self.config.other_options.get("wrap_mouse") else 0)
                | (4 if self.config.two_row else 0)
            )
            packet.src = index  # Matrix packets intentionally overload Src.
            packet.dest = ID_ALL
            packet.machine_name = name
            self.send_packet(packet)

    def close(self, *, notify: bool = True) -> None:
        if self._closed.is_set():
            return
        try:
            if notify and self.trusted:
                packet = Packet()
                packet.type = PackageType.BYE_BYE
                packet.dest = ID_ALL
                packet.machine_name = self.config.machine_name
                self.send_packet(packet)
        except Exception:
            pass
        self._closed.set()
        self.stream.close()
        if notify:
            self.closed_callback(self, None)


class ConnectionManager:
    """Own listeners, auto-profile connection attempts, and heartbeats."""

    def __init__(
        self,
        config: Config,
        packet_callback: Callable[[PeerConnection, Packet], None],
        status_callback: Callable[[str, str], None],
        persist_peer_mac: Callable[[str, str], None] | None = None,
    ) -> None:
        self.config = config
        self.packet_callback = packet_callback
        self.status_callback = status_callback
        self.persist_peer_mac = persist_peer_mac or (lambda _name, _mac: None)
        self._connections: list[PeerConnection] = []
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._reconnect = threading.Event()
        self._worker: threading.Thread | None = None
        self._listener: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None
        self._connecting_sockets: set[socket.socket] = set()
        self._working_profiles: dict[str, CryptoProfile] = {}
        self._authentication_failed: set[str] = set()
        self._retry_after: dict[str, float] = {}
        self._retry_delay: dict[str, float] = {}
        self._connecting_targets: set[str] = set()
        self._target_threads: set[threading.Thread] = set()
        self._authentication_threads: set[threading.Thread] = set()
        self._suspend_offset = _suspend_offset()
        # Windows assigns IDs once per sending process, then reuses that ID on
        # every redundant socket. Start away from zero so a daemon restart is
        # also unlikely to collide with the Windows receiver's recent-ID queue.
        self._next_runtime_packet_id = secrets.randbelow(0x7FFFFFFE) + 1

    def _new_packet_id(self) -> int:
        """Return one process-runtime ID shared by every managed socket."""

        with self._lock:
            self._next_runtime_packet_id += 1
            if self._next_runtime_packet_id > 0x7FFFFFFF:
                self._next_runtime_packet_id = 1
            return self._next_runtime_packet_id

    @staticmethod
    def _numeric_addresses(address: str, *, resolve: bool) -> set[str]:
        """Return canonical IPs, unwrapping IPv4-mapped IPv6 endpoints."""

        value = address.strip().strip("[]")
        addresses: set[str] = set()

        def add(candidate: str) -> None:
            candidate = candidate.split("%", 1)[0]
            try:
                parsed = ip_address(candidate)
            except ValueError:
                return
            if getattr(parsed, "ipv4_mapped", None) is not None:
                parsed = parsed.ipv4_mapped
            addresses.add(parsed.compressed.casefold())

        add(value)
        if resolve:
            try:
                results = socket.getaddrinfo(value, None, type=socket.SOCK_STREAM)
            except (OSError, UnicodeError):
                results = ()
            for result in results:
                add(str(result[4][0]))
        return addresses

    def _target_for_peer_address(self, address: str) -> HostTarget | None:
        peer_addresses = self._numeric_addresses(address, resolve=False)
        if not peer_addresses:
            return None
        for target in self.config.resolve_hosts():
            if peer_addresses & self._numeric_addresses(target.address, resolve=True):
                return target
        return None

    @property
    def connections(self) -> tuple[PeerConnection, ...]:
        with self._lock:
            return tuple(connection for connection in self._connections if connection.trusted)

    @property
    def connected(self) -> bool:
        return bool(self.connections)

    @property
    def peer(self) -> PeerInfo | None:
        peers = self.peers
        if not peers:
            return None
        order = {
            target.name.casefold(): index
            for index, target in enumerate(self.config.resolve_hosts())
        }
        return min(peers, key=lambda item: order.get(item.name.casefold(), len(order)))

    @property
    def peers(self) -> tuple[PeerInfo, ...]:
        """Return one status record per remote PC, hiding the reverse channel."""

        unique: dict[str, PeerInfo] = {}
        for connection in self.connections:
            unique.setdefault(connection.info.name.casefold(), connection.info)
        return tuple(unique.values())

    def peer_id(self, machine_name: str | None = None) -> int | None:
        if machine_name:
            key = machine_name.casefold()
            for peer in self.peers:
                if peer.name.casefold() == key:
                    return peer.machine_id
            return None
        peer = self.peer
        return peer.machine_id if peer else None

    def peer_name(self, machine_id: int) -> str | None:
        for peer in self.peers:
            if peer.machine_id == machine_id:
                return peer.name
        return None

    def start(self) -> None:
        if self._worker and self._worker.is_alive():
            if self._authentication_failed:
                self._authentication_failed.clear()
                self._reconnect.set()
            return
        self.config.validate(require_connection=True)
        self._stop.clear()
        self._authentication_failed.clear()
        self._reconnect.set()
        self._worker = threading.Thread(
            target=self._run, name="mwb-connection-manager", daemon=True
        )
        self._worker.start()

    def _profiles(self, machine_name: str = "") -> tuple[CryptoProfile, ...]:
        if self.config.crypto_profile != "auto":
            return (CryptoProfile(self.config.crypto_profile),)
        working = self._working_profiles.get(machine_name.casefold())
        if working:
            others = tuple(
                profile
                for profile in CryptoProfile.connection_order()
                if profile != working
            )
            return (working,) + others
        return CryptoProfile.connection_order()

    def _run(self) -> None:
        self._start_listener()
        last_heartbeat = 0.0
        while not self._stop.is_set():
            suspend_offset = _suspend_offset()
            if suspend_offset - self._suspend_offset >= RESUME_OFFSET_THRESHOLD:
                self._suspend_offset = suspend_offset
                self.resume_after_suspend()
            if self._reconnect.is_set():
                self._reconnect.clear()
                self._authentication_failed.clear()
                self._retry_after.clear()
            now = time.monotonic()
            for target in self.config.resolve_hosts():
                key = target.name.casefold()
                if self._stop.is_set():
                    break
                if (
                    self._has_outbound(target)
                    or key in self._authentication_failed
                    or key in self._connecting_targets
                ):
                    continue
                if now < self._retry_after.get(key, 0):
                    continue
                self._start_target_connection(target)
            if self.connected and time.monotonic() - last_heartbeat >= HEARTBEAT_INTERVAL:
                self.broadcast_heartbeat()
                last_heartbeat = time.monotonic()
            self._stop.wait(0.5)

    def _start_target_connection(self, target: HostTarget) -> None:
        key = target.name.casefold()
        with self._lock:
            if key in self._connecting_targets:
                return
            self._connecting_targets.add(key)
            thread = threading.Thread(
                target=self._connect_target_worker,
                args=(target,),
                name=f"mwb-connect-{target.name}",
                daemon=True,
            )
            self._target_threads.add(thread)
            thread.start()

    def _connect_target_worker(self, target: HostTarget) -> None:
        key = target.name.casefold()
        try:
            if self._connect_outbound(target):
                self._retry_delay[key] = 1.0
                self._retry_after.pop(key, None)
            else:
                delay = self._retry_delay.get(key, 1.0)
                self._retry_after[key] = time.monotonic() + delay
                self._retry_delay[key] = min(delay * 2, 30.0)
        finally:
            with self._lock:
                self._connecting_targets.discard(key)
                self._target_threads.discard(threading.current_thread())

    def _has_outbound(self, target: HostTarget) -> bool:
        key = target.name.casefold()
        address = target.address.casefold()
        return any(
            connection.outbound
            and (
                connection.info.name.casefold() == key
                or connection.info.address.casefold() == address
            )
            for connection in self.connections
        )

    def _connect_outbound(self, target: HostTarget | None = None) -> bool:
        target = target or self.config.resolve_hosts()[0]
        target_key = target.name.casefold()
        last_error = "connection failed"
        authentication_errors = 0
        profiles = self._profiles(target.name)
        for profile in profiles:
            if self._stop.is_set():
                return False
            self.status_callback(
                "connecting", f"Connecting to {target.name} using {profile.value}"
            )
            raw_socket: socket.socket | None = None
            connection: PeerConnection | None = None
            registered = False
            try:
                raw_socket = socket.create_connection(
                    (target.address, self.config.port + 1), timeout=5
                )
                with self._lock:
                    stopping = self._stop.is_set()
                    if not stopping:
                        self._connecting_sockets.add(raw_socket)
                # stop() cannot close a socket that create_connection has not
                # returned yet. Close it here if it appeared after shutdown
                # took the connecting-socket snapshot.
                if stopping:
                    raw_socket.close()
                    return False
                raw_socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                configure_tcp_liveness(raw_socket)
                connection = PeerConnection(
                    raw_socket,
                    self.config,
                    profile,
                    self.packet_callback,
                    self._connection_closed,
                    outbound=True,
                    packet_id_provider=self._new_packet_id,
                )
                connection.authenticate()
                if self._stop.is_set():
                    connection.close(notify=False)
                    return False
                if connection.info.name.casefold() != target_key:
                    raise AuthenticationError(
                        f"{target.name} identified itself as {connection.info.name}"
                    )
                self._working_profiles[target_key] = profile
                self._remember_peer_mac(connection.info.name, connection.info.address)
                self._register(connection)
                registered = True
                connection.start()
                connection.send_heartbeat()
                if self._stop.is_set():
                    self._discard_connection(connection)
                    return False
                # Let Windows learn the machine before committing the layout.
                threading.Timer(1.0, self._safe_send_matrix, args=(connection,)).start()
                self._report_connected(connection)
                return True
            except (OSError, AuthenticationError, ProtocolError) as exc:
                last_error = str(exc)
                if isinstance(exc, (AuthenticationError, ProtocolError)):
                    authentication_errors += 1
                LOGGER.warning("%s profile rejected: %s", profile.value, exc)
                if registered and connection is not None:
                    self._discard_connection(connection)
                elif connection is not None:
                    connection.close(notify=False)
                elif raw_socket:
                    try:
                        raw_socket.close()
                    except OSError:
                        pass
            finally:
                with self._lock:
                    if raw_socket is not None:
                        self._connecting_sockets.discard(raw_socket)
        if self._stop.is_set():
            return False
        if authentication_errors == len(profiles):
            self._authentication_failed.add(target_key)
            self.status_callback(
                "invalid_key",
                f"{target.name} rejected every compatible encryption profile; "
                "verify its active security key",
            )
        else:
            self.status_callback("error", f"{target.name}: {last_error}")
        return False

    def _start_listener(self) -> None:
        try:
            listener = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
            listener.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("::", self.config.port + 1))
            listener.listen(8)
            listener.settimeout(1)
            self._listener = listener
            self._accept_thread = threading.Thread(
                target=self._accept_loop, name="mwb-control-listener", daemon=True
            )
            self._accept_thread.start()
        except OSError as exc:
            LOGGER.warning("control listener unavailable: %s", exc)

    def _accept_loop(self) -> None:
        listener = self._listener
        if listener is None:
            return
        while not self._stop.is_set():
            try:
                incoming, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            thread = threading.Thread(
                target=self._authenticate_incoming,
                args=(incoming,),
                name="mwb-incoming-auth",
                daemon=True,
            )
            with self._lock:
                if self._stop.is_set():
                    try:
                        incoming.close()
                    except OSError:
                        pass
                    break
                self._connecting_sockets.add(incoming)
                self._authentication_threads.add(thread)
                # Start while holding the same lock stop() uses to snapshot
                # authentication threads, so it can never try to join a
                # Thread object that has not started yet.
                thread.start()

    def _authenticate_incoming(self, incoming: socket.socket) -> None:
        address = str(incoming.getpeername()[0])
        target = self._target_for_peer_address(address)
        profile = self._profiles(target.name if target else "")[0]
        connection: PeerConnection | None = None
        registered = False
        try:
            if self._stop.is_set():
                incoming.close()
                return
            incoming.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            configure_tcp_liveness(incoming)
            connection = PeerConnection(
                incoming,
                self.config,
                profile,
                self.packet_callback,
                self._connection_closed,
                outbound=False,
                packet_id_provider=self._new_packet_id,
            )
            connection.authenticate()
            configured_names = {
                candidate.name.casefold() for candidate in self.config.resolve_hosts()
            }
            if connection.info.name.casefold() not in configured_names:
                raise AuthenticationError("peer is not present in the computer matrix")
            if self._stop.is_set():
                connection.close(notify=False)
                return
            self._working_profiles[connection.info.name.casefold()] = profile
            self._remember_peer_mac(connection.info.name, connection.info.address)
            self._register(connection)
            registered = True
            connection.start()
            connection.send_heartbeat()
            if self._stop.is_set():
                self._discard_connection(connection)
                return
            threading.Timer(1.0, self._safe_send_matrix, args=(connection,)).start()
            self._report_connected(connection)
        except (OSError, AuthenticationError, ProtocolError) as exc:
            LOGGER.info("incoming connection rejected: %s", exc)
            if registered and connection is not None:
                self._discard_connection(connection)
            elif connection is not None:
                connection.close(notify=False)
            else:
                try:
                    incoming.close()
                except OSError:
                    pass
        finally:
            with self._lock:
                self._connecting_sockets.discard(incoming)
                self._authentication_threads.discard(threading.current_thread())

    def _report_connected(self, connection: PeerConnection) -> None:
        if self._stop.is_set() or connection not in self.connections:
            return
        self.status_callback(
            "connected",
            f"Connected to {connection.info.name} using {connection.profile.value}",
        )

    def _register(self, connection: PeerConnection) -> None:
        with self._lock:
            if self._stop.is_set():
                connection.close(notify=False)
                raise ConnectionAbortedError("connection manager is stopping")
            duplicates = [
                existing
                for existing in self._connections
                if existing.info.name.casefold() == connection.info.name.casefold()
                and existing.outbound == connection.outbound
            ]
            # Standalone MWB deliberately keeps one client and one server
            # socket per peer. Reject only a second socket in the same
            # direction; the reverse channel must remain available.
            if duplicates:
                connection.close(notify=False)
                raise AuthenticationError("duplicate same-direction connection")
            self._connections.append(connection)

    def _discard_connection(self, connection: PeerConnection) -> None:
        with self._lock:
            if connection in self._connections:
                self._connections.remove(connection)
        connection.close(notify=False)

    def _safe_send_matrix(self, connection: PeerConnection) -> None:
        try:
            if connection in self.connections:
                connection.send_matrix()
        except (OSError, ConnectionError) as exc:
            LOGGER.info("matrix send failed: %s", exc)

    def _connection_closed(
        self, connection: PeerConnection, failure: Exception | None
    ) -> None:
        name = connection.info.name
        key = name.casefold()
        with self._lock:
            if connection in self._connections:
                self._connections.remove(connection)
            peer_still_connected = any(
                existing.trusted and existing.info.name.casefold() == key
                for existing in self._connections
            )
            remaining_peer_names = {
                existing.info.name.casefold()
                for existing in self._connections
                if existing.trusted
            }
        if not self._stop.is_set():
            if not remaining_peer_names:
                self.status_callback("disconnected", "Connection closed; reconnecting")
            elif not peer_still_connected:
                self.status_callback(
                    "connected",
                    f"{len(remaining_peer_names)} connected; {name} disconnected",
                )
        if connection.outbound and not self._stop.is_set():
            self._retry_after[key] = 0
            self._authentication_failed.discard(key)

    def broadcast(self, packet: Packet) -> None:
        if packet.dest == ID_ALL:
            recipients = self.connections
        else:
            recipients = tuple(
                connection
                for connection in self.connections
                if connection.info.machine_id == packet.dest
            )
        if not recipients:
            raise ConnectionError("target computer is not connected")
        errors = 0
        packet.packet_id = self._new_packet_id()
        for connection in recipients:
            try:
                connection.send_packet(packet, assign_id=False)
            except (OSError, ConnectionError):
                errors += 1
        if errors and errors == len(recipients):
            raise ConnectionError("no active connection accepted the packet")

    def broadcast_heartbeat(self) -> None:
        if not self.connections:
            return
        packet = Packet()
        packet.type = PackageType.HEARTBEAT
        packet.dest = ID_ALL
        packet.machine_name = self.config.machine_name
        try:
            self.broadcast(packet)
        except (OSError, ConnectionError):
            pass

    def _remember_peer_mac(self, machine_name: str, address: str) -> str:
        mac = neighbor_mac(address)
        if mac:
            self.persist_peer_mac(machine_name, mac)
        return mac

    def wake_peer(self, machine_name: str) -> bool:
        """Wake a connected lock screen or an offline Wake-on-LAN peer."""

        target = next(
            (
                candidate
                for candidate in self.config.resolve_hosts()
                if candidate.name.casefold() == machine_name.casefold()
            ),
            None,
        )
        if target is None:
            return False
        awake_sent = False
        machine_id = self.peer_id(target.name)
        if machine_id is not None:
            packet = Packet()
            packet.type = PackageType.AWAKE
            packet.dest = machine_id
            packet.machine_name = self.config.machine_name
            try:
                self.broadcast(packet)
                awake_sent = True
            except (OSError, ConnectionError):
                pass

        mac = target.mac or self._remember_peer_mac(target.name, target.address)
        if not mac:
            if awake_sent:
                return True
            self.status_callback(
                "waking",
                f"Cannot wake {target.name} until its network adapter MAC has been learned",
            )
            return False
        addresses = tuple(self._numeric_addresses(target.address, resolve=True))
        sent = wake_on_lan(mac, addresses)
        if sent:
            self.status_callback("waking", f"Wake-on-LAN sent to {target.name}")
            self._authentication_failed.discard(target.name.casefold())
            self._retry_after[target.name.casefold()] = 0
            self._reconnect.set()
        return sent or awake_sent

    def resume_after_suspend(self) -> None:
        """Invalidate inherited sockets and immediately rebuild both channels."""

        if self._stop.is_set():
            return
        with self._lock:
            connections = list(self._connections)
            self._connections.clear()
        for connection in connections:
            connection.close(notify=False)
        self._authentication_failed.clear()
        self._retry_after.clear()
        self._retry_delay.clear()
        self._reconnect.set()
        self.status_callback("connecting", "System resumed; rebuilding connections")

    def reconnect(self) -> None:
        self.disconnect()
        self._authentication_failed.clear()
        self._stop.clear()
        self._reconnect.set()
        self.start()

    def disconnect(self) -> None:
        with self._lock:
            connections = list(self._connections)
            self._connections.clear()
        for connection in connections:
            connection.close(notify=False)
        self.status_callback("disconnected", "Disconnected")

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            connecting_sockets = tuple(self._connecting_sockets)
            self._connecting_sockets.clear()
        for connecting_socket in connecting_sockets:
            try:
                connecting_socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                connecting_socket.close()
            except OSError:
                pass
        self.disconnect()
        listener = self._listener
        self._listener = None
        if listener:
            try:
                listener.close()
            except OSError:
                pass
        current = threading.current_thread()
        if self._accept_thread and self._accept_thread is not current:
            self._accept_thread.join(timeout=1.5)
        self._accept_thread = None
        if self._worker and self._worker is not current:
            self._worker.join(timeout=2)
        self._worker = None
        with self._lock:
            target_threads = tuple(self._target_threads)
            authentication_threads = tuple(self._authentication_threads)
        for thread in target_threads:
            if thread is not current:
                thread.join(timeout=1)
        for thread in authentication_threads:
            if thread is not current:
                thread.join(timeout=1)
