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
from .file_transfer import FileTransferManager, TransferPeer
from .input import InputManager, capture_targets
from .protocol import ID_ALL, Packet, PackageType
from .power import PowerManager
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
        self.file_transfer: FileTransferManager | None = None
        self.input: InputManager | None = None
        self.power: PowerManager | None = None
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
        self._config_lock = threading.Lock()
        # Runtime intent is separate from the startup preference. Once the
        # user presses Connect, applying settings must keep the session alive;
        # once they press Disconnect, Windows reverse channels must stay off.
        self._connection_requested = self.config.auto_connect
        self._ui_exited = False
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
                self.config,
                self._process_packet,
                self._connection_status,
                self._persist_peer_mac,
                self._resume_after_suspend,
            )
        if self.power is None:
            self.power = PowerManager(self._prepare_for_sleep, self._session_locked)
        if self.clipboard is None and self.config.share_clipboard:
            self.clipboard = ClipboardManager(
                self._broadcast, share_images=self.config.share_images
            )
        if self.file_transfer is None and self.config.share_images:
            self.file_transfer = FileTransferManager(
                self.config,
                self._broadcast,
                self._transfer_peer_by_id,
                self._transfer_peer_by_address,
                self._input_status,
            )
        if self.input is None:
            self.input = InputManager(
                self.config,
                self._broadcast,
                lambda name=None: self.connection.peer_id(name)
                if self.connection
                else None,
                self._input_status,
                self._persist_config,
                lambda machine_id: self.connection.peer_name(machine_id)
                if self.connection
                else None,
                lambda name: self.connection.wake_peer(name)
                if self.connection
                else False,
                control_changed=lambda machine_id: self.file_transfer.control_changed(
                    machine_id
                )
                if self.file_transfer
                else None,
            )
        else:
            self.input.config = self.config
        self._sync_sleep_inhibitor()
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
            if self.file_transfer:
                try:
                    self.file_transfer.start()
                except OSError as exc:
                    self._input_status(f"File transfer unavailable: {exc}")
            if self.power:
                self._sync_sleep_inhibitor()

    def _sync_sleep_inhibitor(self) -> None:
        """Protect the full Connect intent, including transient reconnects."""

        if not self.power:
            return
        configured = bool(
            self.config.resolve_hosts() and len(self.config.secret) >= SECRET_LENGTH
        )
        self.power.set_connected(
            self._connection_requested and configured,
            block_sleep=bool(
                self.config.other_options.get("block_screen_saver", True)
            ),
            block_lid=bool(
                self.config.other_options.get("stay_awake_on_lid_close", False)
            ),
            block_lock=bool(
                self.config.other_options.get("never_lock_while_connected", False)
            ),
        )

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
        if self.file_transfer:
            self.file_transfer.stop()
            self.file_transfer = None
        if self.power:
            self.power.stop()
            self.power = None

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
        if self.file_transfer:
            self.file_transfer.stop()
            self.file_transfer = None
        self._stop_connection()
        if (input_changed or not configured) and self.input:
            self.input.stop()
            self.input = None
        if (clipboard_changed or not configured) and self.clipboard:
            self.clipboard.stop()
            self.clipboard = None

        self.config = candidate
        self._sync_sleep_inhibitor()
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
            self.input.recover_active_peer()
        if state == "connected":
            self._start_features()
            if self.input:
                self.input.retry_pending_switch()
        elif self.connection and self.connection.connected:
            state = "connected"
            message = f"{len(self.connection.peers)} connected; {message}"
        self._set_status(state, message)

    def _input_status(self, message: str) -> None:
        with self._status_lock:
            repeated = self._status["input"] == message
            self._status["input"] = message
            self._status["updated"] = time.time()
        if not repeated:
            # Portal and compositor transitions are the hardest part of this
            # app to diagnose after the fact; keep them in the log.
            LOGGER.info("input=%s", message)

    def _resume_after_suspend(self) -> None:
        if self.input:
            self.input.resume_after_suspend()

    def _prepare_for_sleep(self, about_to_sleep: bool) -> None:
        """React to logind's suspend fence instead of guessing after the fact."""

        if about_to_sleep:
            # Release the compositor grab first so no key stays logically held
            # on the Windows peer, then close the channels while the network
            # interface is still up.
            if self.input:
                self.input.release_local()
            if self.connection:
                self.connection.prepare_for_suspend()
            return
        if self.connection:
            self.connection.resume_after_suspend()

    def _session_locked(self, locked: bool) -> None:
        """Forward lock screen transitions to the portal input session.

        The compositor destroys every remote input device while this PC is
        locked, so recovery has to wait for the unlock rather than fight it.
        """

        if self.input:
            self.input.session_lock_changed(locked)

    def _set_status(self, state: str, message: str) -> None:
        with self._status_lock:
            repeated = self._status["state"] == state and self._status["message"] == message
            self._status["state"] = state
            self._status["message"] = message
            self._status["updated"] = time.time()
        if not repeated:
            # A link that is down retries every second; logging each identical
            # attempt buries the transitions that actually matter.
            LOGGER.info("state=%s: %s", state, message)

    def _persist_config(self) -> None:
        """Serialize portal tokens and learned network metadata without races."""

        with self._config_lock:
            self.config.save(self.config_path)

    def _persist_peer_mac(self, machine_name: str, mac: str) -> None:
        changed = False
        with self._config_lock:
            for remote in self.config.remote_machines:
                if remote["name"].casefold() != machine_name.casefold():
                    continue
                if remote.get("mac") != mac:
                    remote["mac"] = mac
                    changed = True
                break
            if changed:
                self.config.save(self.config_path)

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
                "file_transfer_listening": bool(
                    self.file_transfer and self.file_transfer.listening
                ),
                "sleep_inhibited": bool(self.power and self.power.sleep_inhibited),
                "ui_exited": self._ui_exited,
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
            PackageType.CLIPBOARD_DRAG_DROP,
            PackageType.CLIPBOARD_DRAG_DROP_OPERATION,
            PackageType.CLIPBOARD_DRAG_DROP_END,
            PackageType.EXPLORER_DRAG_DROP,
            PackageType.CLIPBOARD_ASK,
        ):
            if self.file_transfer:
                self.file_transfer.process_packet(packet)
            return
        if packet.type in (
            PackageType.HEARTBEAT,
            PackageType.HEARTBEAT_EX,
            PackageType.AWAKE,
            PackageType.HELLO,
        ):
            if (
                self.power
                and packet.type == PackageType.AWAKE
                and packet.dest in (self.config.machine_id, ID_ALL)
            ):
                self.power.remote_activity()
                if self.input:
                    self.input.wake_display()
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
            if self.power:
                self.power.remote_activity()
            if self.input:
                self.input.inject_keyboard(*packet.keyboard)
            return
        if packet.type == PackageType.MOUSE and packet.dest in (
            self.config.machine_id,
            ID_ALL,
        ):
            if self.power:
                self.power.remote_activity()
            if self.input:
                self.input.inject_mouse(*packet.mouse, source_id=packet.src)
            if self.file_transfer:
                self.file_transfer.handle_remote_mouse(packet.mouse[3])
            return
        if packet.type == PackageType.NEXT_MACHINE and packet.dest in (
            self.config.machine_id,
            ID_ALL,
        ):
            if self.input:
                x, y, next_machine_id, _flags = packet.mouse
                self.input.follow_next_machine(next_machine_id, x, y, packet.src)
            return
        if packet.type in (PackageType.CLIPBOARD_TEXT, PackageType.CLIPBOARD_IMAGE):
            if self.clipboard and packet.complete_clipboard is not None:
                self.clipboard.receive(
                    packet.complete_clipboard,
                    image=packet.type == PackageType.CLIPBOARD_IMAGE,
                )
            return
        if packet.type == PackageType.HIDE_MOUSE:
            # This packet is sent to the previously *controlled* computer when
            # a controller follows NEXT_MACHINE. It is not a request to release
            # Linux's physical InputCapture session; doing so broke the reverse
            # (Windows-hosted) direction by confusing the two roles.
            if self.input:
                self.input.controlled_pointer_hidden(packet.src)
            return

    def reconnect(self) -> None:
        self._ui_exited = False
        self._connection_requested = True
        self._replace_runtime_config(Config.load(self.config_path))

    def connect(self) -> None:
        self._ui_exited = False
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
        if self.file_transfer:
            self.file_transfer.stop()
        if self.power:
            self._sync_sleep_inhibitor()
        self._stop_connection()
        self._set_status("disconnected", "Disconnected")

    def exit_ui(self) -> None:
        """Stop every sharing path but retain a disabled portal grant.

        InputCapture v1 cannot serialize permission grants. Keeping its session
        disabled is the only way to make a later UI launch prompt-free; network,
        clipboard, and capture activity are all stopped before returning.
        """

        self._connection_requested = False
        self._ui_exited = True
        if self.input:
            self.input.pause()
        self._stop_connection()
        if self.clipboard:
            self.clipboard.stop()
            self.clipboard = None
        if self.file_transfer:
            self.file_transfer.stop()
            self.file_transfer = None
        if self.power:
            self._sync_sleep_inhibitor()
        self._set_status("dormant", "Exited; mouse and keyboard sharing stopped")

    def _transfer_peer_by_id(self, machine_id: int) -> TransferPeer | None:
        info = (
            self.connection.transfer_peer(machine_id=machine_id)
            if self.connection
            else None
        )
        return (
            TransferPeer(info.name, info.machine_id, info.address, info.profile)
            if info
            else None
        )

    def _transfer_peer_by_address(self, address: str) -> TransferPeer | None:
        info = (
            self.connection.transfer_peer(address=address)
            if self.connection
            else None
        )
        return (
            TransferPeer(info.name, info.machine_id, info.address, info.profile)
            if info
            else None
        )

    def resume_ui(self) -> None:
        """Resume only a service deliberately parked by the top-bar Exit."""

        if self._ui_exited:
            self.connect()

    @staticmethod
    def _apply_shortcuts(config: Config) -> None:
        try:
            apply_gnome_shortcuts(config)
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            LOGGER.warning("could not update desktop shortcuts: %s", exc)

    def update_config(self, values: dict[str, object]) -> None:
        values = dict(values)
        # Wake-on-LAN addresses are learned by the live network runtime and are
        # deliberately absent from the settings form. Preserve them when the
        # form replaces its name/address records.
        if isinstance(values.get("remote_machines"), list):
            known_macs = {
                remote["name"].casefold(): remote.get("mac", "")
                for remote in self.config.remote_machines
                if remote.get("mac")
            }
            merged_remotes = []
            for raw_remote in values["remote_machines"]:
                if not isinstance(raw_remote, dict):
                    merged_remotes.append(raw_remote)
                    continue
                remote = dict(raw_remote)
                name = str(remote.get("name", "")).casefold()
                if name in known_macs:
                    remote["mac"] = known_macs[name]
                merged_remotes.append(remote)
            values["remote_machines"] = merged_remotes
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
        with self._config_lock:
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
        should_shutdown = False
        try:
            line = client.makefile("rb").readline(MAX_CONTROL_REQUEST_BYTES + 1)
            if len(line) > MAX_CONTROL_REQUEST_BYTES:
                raise ValueError("control request is too large")
            request = json.loads(line)
            if not isinstance(request, dict):
                raise ValueError("control request must be a JSON object")
            response = self._control_command(request)
            should_shutdown = request.get("command") == "quit" and bool(
                response.get("ok")
            )
        except Exception as exc:
            response = {"ok": False, "error": str(exc)}
        try:
            client.sendall(json.dumps(response).encode("utf-8") + b"\n")
        finally:
            client.close()
            # Deliver the acknowledgment before waking the main loop. Without
            # this ordering, a fast service exit can truncate the reply and
            # leave the UI unsure whether fail-closed cleanup is still needed.
            if should_shutdown:
                self._begin_shutdown()

    def _begin_shutdown(self) -> None:
        """Schedule guaranteed cleanup after a quit acknowledgment is sent."""

        self._stop.set()
        threading.Thread(
            target=self.stop,
            name="mwb-service-shutdown",
            daemon=False,
        ).start()

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
            return {"ok": True}
        elif command == "exit_ui":
            self.exit_ui()
        elif command == "resume_ui":
            self.resume_ui()
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
