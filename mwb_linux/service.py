"""Long-running Mouse Without Borders Linux service."""

from __future__ import annotations

import fcntl
import json
import logging
import os
import socket
import subprocess
import threading
import time
from collections import deque
from dataclasses import asdict, fields
from pathlib import Path

from .clipboard import ClipboardManager
from .config import (
    HOTKEY_DEFAULTS,
    OTHER_OPTION_DEFAULTS,
    SECRET_LENGTH,
    Config,
    default_config_path,
    default_runtime_socket,
)
from .connection import ConnectionManager, PeerConnection
from .input import InputManager, capture_targets
from .protocol import ID_ALL, Packet, PackageType
from .shortcuts import apply_gnome_shortcuts

LOGGER = logging.getLogger(__name__)
MAX_CONTROL_REQUEST_BYTES = 1024 * 1024


class SecretRedactionFilter(logging.Filter):
    def __init__(self, secret_getter) -> None:
        super().__init__()
        self.secret_getter = secret_getter

    def filter(self, record: logging.LogRecord) -> bool:
        secret = self.secret_getter()
        if secret:
            record.msg = str(record.msg).replace(secret, "[REDACTED]")
            if record.args:
                record.args = tuple(
                    value.replace(secret, "[REDACTED]")
                    if isinstance(value, str)
                    else value
                    for value in record.args
                )
        return True


class MouseWithoutBordersService:
    def __init__(self, config_path: Path | None = None) -> None:
        self.config_path = config_path or default_config_path()
        self.config = Config.load(self.config_path)
        self.connection: ConnectionManager | None = None
        self.clipboard: ClipboardManager | None = None
        self.input: InputManager | None = None
        self._stop = threading.Event()
        self._shutdown_lock = threading.Lock()
        self._shutdown_complete = False
        self._runtime_socket_path = default_runtime_socket()
        self._instance_lock_fd: int | None = None
        self._owns_runtime_socket = False
        self._control_socket: socket.socket | None = None
        self._control_thread: threading.Thread | None = None
        self._status_lock = threading.Lock()
        self._features_lock = threading.Lock()
        # Runtime intent is separate from the startup preference. Once the
        # user presses Connect, applying settings must keep the session alive;
        # once they press Disconnect, Windows reverse channels must stay off.
        self._connection_requested = self.config.auto_connect
        self._status = {
            "state": "stopped",
            "message": "Not started",
            "input": "Not initialized",
            "updated": time.time(),
        }
        # Packet IDs are generated independently by every Windows machine, so
        # de-duplicate by source as well as ID.
        self._recent_packet_ids: deque[tuple[int, int]] = deque(maxlen=200)
        redaction_filter = SecretRedactionFilter(lambda: self.config.secret)
        root_logger = logging.getLogger()
        root_logger.addFilter(redaction_filter)
        for handler in root_logger.handlers:
            handler.addFilter(redaction_filter)

    def start(self) -> None:
        self._shutdown_complete = False
        self._stop.clear()
        self._acquire_instance_lock()
        try:
            self._start_control_server()
            self._start_components()
        except Exception:
            self.stop()
            raise

    def _acquire_instance_lock(self) -> None:
        if self._instance_lock_fd is not None:
            return
        lock_path = self._runtime_socket_path.with_suffix(
            self._runtime_socket_path.suffix + ".lock"
        )
        lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        os.chmod(lock_path, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(fd)
            raise OSError("another Mouse Without Borders service is running") from exc
        self._instance_lock_fd = fd

    def _release_instance_lock(self) -> None:
        fd = self._instance_lock_fd
        self._instance_lock_fd = None
        if fd is None:
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def _start_components(self) -> None:
        if not self.config.resolve_hosts() or len(self.config.secret) < SECRET_LENGTH:
            self._set_status("unconfigured", "Enter a host and security key")
            return
        try:
            self.config.validate(require_connection=True)
        except ValueError as exc:
            self._set_status("error", str(exc))
            return
        if self.connection is None:
            self.connection = ConnectionManager(
                self.config, self._process_packet, self._connection_status
            )
        if self.clipboard is None and self.config.share_clipboard:
            self.clipboard = ClipboardManager(
                self._broadcast, share_images=self.config.share_images
            )
        if self.input is None:
            self.input = InputManager(
                self.config,
                self._broadcast,
                lambda name=None: self.connection.peer_id(name)
                if self.connection
                else None,
                self._input_status,
                lambda: self.config.save(self.config_path),
            )
        else:
            self.input.config = self.config
        if self._connection_requested:
            self.connection.start()
        else:
            self._set_status("disconnected", "Ready to connect")

    def _broadcast(self, packet: Packet) -> None:
        """Route long-lived desktop features to the current network runtime."""

        connection = self.connection
        if connection and connection.connected:
            connection.broadcast(packet)

    def _start_features(self) -> None:
        """Request desktop access only after the Windows peer authenticates."""

        with self._features_lock:
            if self.config.share_clipboard and self.clipboard:
                try:
                    self.clipboard.start()
                except Exception as exc:
                    self._input_status(f"Clipboard unavailable: {exc}")
            if self.input:
                self.input.start()

    def _stop_connection(self) -> None:
        if self.connection:
            self.connection.stop()
            self.connection = None

    def _stop_features(self) -> None:
        if self.input:
            self.input.stop()
            self.input = None
        if self.clipboard:
            self.clipboard.stop()
            self.clipboard = None

    def _stop_components(self) -> None:
        self._stop_features()
        self._stop_connection()

    def _replace_runtime_config(self, candidate: Config) -> None:
        """Apply settings while retaining portal sessions when compatible."""

        previous = self.config
        input_changed = (
            previous.host_position != candidate.host_position
            or previous.host_zone != candidate.host_zone
            or previous.edge_switching != candidate.edge_switching
            or capture_targets(previous) != capture_targets(candidate)
        )
        clipboard_changed = (
            previous.share_clipboard != candidate.share_clipboard
            or previous.share_images != candidate.share_images
        )
        configured = bool(
            candidate.resolve_hosts() and len(candidate.secret) >= SECRET_LENGTH
        )

        # Release capture while the old authenticated channel can still send
        # key-up events, then replace the sockets and listener.
        if self.input:
            self.input.release_local()
        self._stop_connection()
        if (input_changed or not configured) and self.input:
            self.input.stop()
            self.input = None
        if (clipboard_changed or not configured) and self.clipboard:
            self.clipboard.stop()
            self.clipboard = None

        self.config = candidate
        if self.input:
            self.input.config = candidate
            configured_names = {
                target.name.casefold() for target in candidate.resolve_hosts()
            }
            if self.input.active_remote_name.casefold() not in configured_names:
                self.input.active_remote_name = next(
                    (target.name for target in candidate.resolve_hosts()), ""
                )
        self._start_components()

    def stop(self) -> None:
        with self._shutdown_lock:
            if self._shutdown_complete:
                return
            self._stop.set()
            self._stop_components()
            if self._control_socket:
                try:
                    self._control_socket.close()
                except OSError:
                    pass
            if self._owns_runtime_socket:
                try:
                    self._runtime_socket_path.unlink()
                except FileNotFoundError:
                    pass
                self._owns_runtime_socket = False
            self._release_instance_lock()
            self._set_status("stopped", "Stopped")
            self._shutdown_complete = True

    def wait(self) -> None:
        while not self._stop.wait(1):
            pass

    def _connection_status(self, state: str, message: str) -> None:
        if (
            self.input
            and self.input.remote_active
            and self.connection
            and self.connection.peer_id(self.input.active_remote_name) is None
        ):
            # Never leave the compositor capture active after the selected PC
            # loses its final channel. Other peers may still be connected, so
            # the manager's overall state alone cannot make this decision.
            self.input.release_local()
        if state == "connected":
            self._start_features()
        elif self.connection and self.connection.connected:
            state = "connected"
            message = f"{len(self.connection.peers)} connected; {message}"
        self._set_status(state, message)

    def _input_status(self, message: str) -> None:
        with self._status_lock:
            self._status["input"] = message
            self._status["updated"] = time.time()

    def _set_status(self, state: str, message: str) -> None:
        with self._status_lock:
            self._status["state"] = state
            self._status["message"] = message
            self._status["updated"] = time.time()
        LOGGER.info("state=%s: %s", state, message)

    def status(self) -> dict[str, object]:
        with self._status_lock:
            status = dict(self._status)
        peer = self.connection.peer if self.connection else None
        peers = self.connection.peers if self.connection else ()
        status.update(
            {
                "configured": bool(self.config.resolve_hosts() and self.config.secret),
                "config": self.config.public_dict(),
                "peer": asdict(peer) if peer else None,
                "peers": [asdict(item) for item in peers],
                "remote_active": bool(self.input and self.input.remote_active),
                "active_remote_name": self.input.active_remote_name if self.input else "",
            }
        )
        return status

    def _process_packet(self, peer: PeerConnection, packet: Packet) -> None:
        if packet.packet_id and packet.type not in (
            PackageType.CLIPBOARD_TEXT,
            PackageType.CLIPBOARD_IMAGE,
        ):
            identity = (packet.src, packet.packet_id)
            if identity in self._recent_packet_ids:
                return
            self._recent_packet_ids.append(identity)
        if packet.type in (
            PackageType.HEARTBEAT,
            PackageType.HEARTBEAT_EX,
            PackageType.AWAKE,
            PackageType.HELLO,
        ):
            if packet.machine_name:
                peer.info.name = packet.machine_name
            if packet.src not in (0, ID_ALL):
                peer.info.machine_id = packet.src
            if packet.type == PackageType.HELLO:
                peer.send_heartbeat()
            return
        if packet.type == PackageType.KEYBOARD and packet.dest in (
            self.config.machine_id,
            ID_ALL,
        ):
            if self.input:
                self.input.inject_keyboard(*packet.keyboard)
            return
        if packet.type == PackageType.MOUSE and packet.dest in (
            self.config.machine_id,
            ID_ALL,
        ):
            if self.input:
                self.input.inject_mouse(*packet.mouse)
            return
        if packet.type in (PackageType.CLIPBOARD_TEXT, PackageType.CLIPBOARD_IMAGE):
            if self.clipboard and packet.complete_clipboard is not None:
                self.clipboard.receive(
                    packet.complete_clipboard,
                    image=packet.type == PackageType.CLIPBOARD_IMAGE,
                )
            return
        if packet.type == PackageType.HIDE_MOUSE and self.input:
            active_id = (
                self.connection.peer_id(self.input.active_remote_name)
                if self.connection
                else None
            )
            if not self.input.remote_active or packet.src in (active_id, 0, ID_ALL):
                self.input.release_local()

    def reconnect(self) -> None:
        self._connection_requested = True
        self._replace_runtime_config(Config.load(self.config_path))

    def connect(self) -> None:
        self._connection_requested = True
        if not self.connection:
            self._start_components()
        if self.connection and not self.connection.connected:
            self.connection.start()

    def disconnect(self) -> None:
        # A standalone Windows peer maintains a reverse channel to us. Merely
        # closing the current sockets leaves the listener alive, allowing that
        # channel to reconnect immediately while the UI still says
        # "Disconnected". Stop the network runtime and listener, but keep the
        # already-authorized portal session alive because InputCapture v1
        # cannot persist permissions.
        self._connection_requested = False
        if self.input:
            self.input.release_local()
        self._stop_connection()
        self._set_status("disconnected", "Disconnected")

    @staticmethod
    def _apply_shortcuts(config: Config) -> None:
        try:
            apply_gnome_shortcuts(config)
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            LOGGER.warning("could not update desktop shortcuts: %s", exc)

    def update_config(self, values: dict[str, object]) -> None:
        allowed = {field.name for field in fields(Config)}
        candidate_values = asdict(self.config)
        for key, value in values.items():
            if key in allowed:
                candidate_values[key] = value
        candidate = Config(**candidate_values)
        candidate.other_options = {
            **OTHER_OPTION_DEFAULTS,
            **candidate.other_options,
        }
        candidate.hotkeys = {**HOTKEY_DEFAULTS, **candidate.hotkeys}
        candidate.validate()
        candidate.save(self.config_path)
        # Saving from the UI is also the repair path when desktop settings were
        # reset outside the application, so reapply the desired bindings even
        # when their values did not change.
        self._apply_shortcuts(candidate)
        # Preserve the current Connect/Disconnect choice while replacing the
        # components that depend on settings such as port, key, and profile.
        self._replace_runtime_config(candidate)

    def _start_control_server(self) -> None:
        path = self._runtime_socket_path
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(path))
        self._owns_runtime_socket = True
        os.chmod(path, 0o600)
        server.listen(8)
        server.settimeout(1)
        self._control_socket = server
        self._control_thread = threading.Thread(
            target=self._control_loop, name="mwb-control-api", daemon=True
        )
        self._control_thread.start()

    def _control_loop(self) -> None:
        while not self._stop.is_set() and self._control_socket:
            try:
                client, _ = self._control_socket.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(
                target=self._handle_control_client,
                args=(client,),
                name="mwb-control-client",
                daemon=True,
            ).start()

    def _handle_control_client(self, client: socket.socket) -> None:
        try:
            line = client.makefile("rb").readline(MAX_CONTROL_REQUEST_BYTES + 1)
            if len(line) > MAX_CONTROL_REQUEST_BYTES:
                raise ValueError("control request is too large")
            request = json.loads(line)
            if not isinstance(request, dict):
                raise ValueError("control request must be a JSON object")
            response = self._control_command(request)
        except Exception as exc:
            response = {"ok": False, "error": str(exc)}
        try:
            client.sendall(json.dumps(response).encode("utf-8") + b"\n")
        finally:
            client.close()

    def _control_command(self, request: dict) -> dict[str, object]:
        command = request.get("command")
        if command == "status":
            return {"ok": True, "status": self.status()}
        if command == "connect":
            self.connect()
        elif command == "disconnect":
            self.disconnect()
        elif command == "reconnect":
            self.reconnect()
        elif command == "switch_remote":
            if self.input:
                machine_name = request.get("machine_name")
                self.input.switch_remote(str(machine_name) if machine_name else None)
        elif command == "switch_machine":
            self.switch_machine(int(request.get("slot", 0)))
        elif command == "release_local":
            if self.input:
                self.input.release_local()
        elif command == "save_config":
            self.update_config(request.get("config", {}))
        elif command == "quit":
            threading.Thread(target=self.stop, daemon=True).start()
        else:
            return {"ok": False, "error": f"unknown command: {command}"}
        return {"ok": True, "status": self.status()}

    def switch_machine(self, slot: int) -> None:
        if not 1 <= slot <= len(self.config.machine_matrix):
            raise ValueError("machine slot must be between 1 and 4")
        machine_name = self.config.machine_matrix[slot - 1].strip()
        if not machine_name:
            raise ValueError(f"machine slot {slot} is empty")
        if not self.input:
            raise RuntimeError("input sharing is not initialized")
        if machine_name.casefold() == self.config.machine_name.casefold():
            self.input.release_local()
        else:
            self.input.switch_remote(machine_name)


def control_request(command: str, timeout: float = 5.0, **arguments: object) -> dict:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout)
    client.connect(str(default_runtime_socket()))
    client.sendall(
        json.dumps({"command": command, **arguments}).encode("utf-8") + b"\n"
    )
    response = client.makefile("rb").readline()
    client.close()
    return json.loads(response)
