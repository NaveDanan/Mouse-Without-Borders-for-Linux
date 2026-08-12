"""Windows-compatible file transfer and cross-screen drag/drop state."""

from __future__ import annotations

import json
import logging
import math
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

from .clipboard import background_environment
from .config import Config
from .crypto import CryptoProfile, EncryptedSocket
from .protocol import ID_ALL, ID_NONE, PACKAGE_SIZE_EX, Packet, PackageType
from .topology import neighbour

LOGGER = logging.getLogger(__name__)

CLIPBOARD_HEADER_SIZE = 1024
TRANSFER_CHUNK_SIZE = 64 * 1024
TRANSFER_POST_ACTION_DESKTOP = 1
DRAG_PROBE_TIMEOUT = 2.5
DRAG_CANDIDATE_LIFETIME = 3.0
DRAG_LEAVE_GRACE = 1.0
STAGED_FILE_LIFETIME = 120.0
WINDOWS_INVALID_FILENAME = '<>:"/\\|?*'
WINDOWS_RESERVED_FILENAMES = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}


class FileTransferError(RuntimeError):
    """A file-transfer peer sent invalid data or a transfer could not finish."""


@dataclass(frozen=True, slots=True)
class TransferPeer:
    """The authenticated control-channel identity used by the file socket."""

    name: str
    machine_id: int
    address: str
    profile: str


def _helper_command(name: str) -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, name]
    return [sys.executable, "-m", "mwb_linux", name]


def _drag_probe_command() -> list[str]:
    return _helper_command("_drag-capture")


def probe_dragged_file(timeout: float = DRAG_PROBE_TIMEOUT) -> Path | None:
    """Ask the short-lived XWayland drop catcher for the active file drag."""

    if not os.environ.get("DISPLAY"):
        return None
    environment = background_environment()
    environment["GDK_BACKEND"] = "x11"
    environment["MWB_DRAG_CAPTURE_TIMEOUT"] = str(max(0.25, timeout - 0.25))
    try:
        result = subprocess.run(
            _drag_probe_command(),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=timeout,
            env=environment,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode:
        return None
    try:
        value = json.loads(result.stdout.strip())
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(value, str) or not value:
        return None
    path = Path(value)
    return path if path.is_file() else None


def desktop_directory() -> Path:
    """Return the localized XDG desktop directory with a safe fallback."""

    executable = shutil.which("xdg-user-dir")
    if executable:
        try:
            value = subprocess.check_output(
                [executable, "DESKTOP"],
                text=True,
                timeout=2,
                stderr=subprocess.DEVNULL,
                env=background_environment(),
            ).strip()
            if value:
                return Path(value).expanduser()
        except (OSError, subprocess.SubprocessError):
            pass
    return Path.home() / "Desktop"


def safe_remote_name(value: str) -> str:
    """Reduce a Windows or POSIX path to one harmless local filename."""

    cleaned = value.replace("\x00", "").strip()
    # PureWindowsPath also handles drive letters and backslashes.  Apply the
    # POSIX basename afterwards because PowerToys accepts forward slashes too.
    name = Path(PureWindowsPath(cleaned).name).name
    if name in ("", ".", ".."):
        raise FileTransferError("the remote file has no valid filename")
    return name


def windows_safe_name(value: str) -> str:
    """Return a basename Windows can create for a Linux-origin file."""

    translated = "".join(
        "_"
        if character in WINDOWS_INVALID_FILENAME or ord(character) < 32
        else character
        for character in value
    ).rstrip(" .")
    if not translated:
        translated = "dragged-file"
    stem = translated.split(".", 1)[0].casefold()
    if stem in WINDOWS_RESERVED_FILENAMES:
        translated = f"_{translated}"
    return translated


def unique_destination(directory: Path, filename: str) -> Path:
    """Choose a collision-free destination without overwriting user files."""

    candidate = directory / filename
    if not candidate.exists() and not candidate.with_name(
        candidate.name + ".part"
    ).exists():
        return candidate
    suffix = candidate.suffix
    stem = candidate.name[: -len(suffix)] if suffix else candidate.name
    for index in range(1, 10_000):
        candidate = directory / f"{stem} ({index}){suffix}"
        if not candidate.exists() and not candidate.with_name(
            candidate.name + ".part"
        ).exists():
            return candidate
    raise FileTransferError("could not choose a free destination filename")


class FileTransferManager:
    """Own the base-port file service and the PowerToys drag/drop handshake."""

    def __init__(
        self,
        config: Config,
        send_packet: Callable[[Packet], None],
        peer_by_id: Callable[[int], TransferPeer | None],
        peer_by_address: Callable[[str], TransferPeer | None],
        status_callback: Callable[[str], None],
        *,
        drag_probe: Callable[[], Path | None] = probe_dragged_file,
        destination_root: Path | None = None,
        visual_feedback: bool = True,
    ) -> None:
        self.config = config
        self.send_packet = send_packet
        self.peer_by_id = peer_by_id
        self.peer_by_address = peer_by_address
        self.status_callback = status_callback
        self.drag_probe = drag_probe
        self.destination_root = destination_root
        self.visual_feedback = visual_feedback
        self._stop = threading.Event()
        self._listener: socket.socket | None = None
        self._listener_thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self._client_sockets: set[socket.socket] = set()
        self._client_threads: set[threading.Thread] = set()
        self._staged_file: Path | None = None
        self._staged_until = 0.0
        self._remote_source_id = ID_NONE
        self._remote_drop_active = False
        self._probe_running = False
        self._candidate_stage_running = False
        self._download_running = False
        self._upload_running = False
        self._local_destination_id = ID_NONE
        self._expected_push_id = ID_NONE
        self._expected_push_until = 0.0
        self._drag_candidate: Path | None = None
        self._drag_candidate_until = 0.0
        self._drag_monitor_process: subprocess.Popen[str] | None = None
        self._drag_monitor_thread: threading.Thread | None = None
        self._drag_monitor_failures = 0
        self._drop_indicator_process: subprocess.Popen[str] | None = None

    @property
    def enabled(self) -> bool:
        # ``share_images`` is the persisted setting behind the Windows form's
        # indented "Transfer file" checkbox.  Keep its historical field name
        # for configuration compatibility.
        return bool(self.config.share_images)

    @property
    def listening(self) -> bool:
        return bool(
            self._listener
            and self._listener_thread
            and self._listener_thread.is_alive()
        )

    def start(self) -> None:
        if not self.enabled or (
            self._listener_thread and self._listener_thread.is_alive()
        ):
            return
        listener = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        listener.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind(("::", self.config.port))
            listener.listen(8)
            listener.settimeout(1)
        except Exception:
            listener.close()
            raise
        self._stop.clear()
        self._listener = listener
        self._listener_thread = threading.Thread(
            target=self._accept_loop,
            name="mwb-file-listener",
            daemon=True,
        )
        self._listener_thread.start()
        if self.visual_feedback:
            self._start_drag_monitor()

    def stop(self) -> None:
        self._stop.set()
        self._stop_drag_monitor()
        self._hide_drop_indicator()
        listener = self._listener
        self._listener = None
        if listener:
            try:
                listener.close()
            except OSError:
                pass
        with self._lock:
            clients = tuple(self._client_sockets)
        for client in clients:
            try:
                client.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                client.close()
            except OSError:
                pass
        current = threading.current_thread()
        if self._listener_thread and self._listener_thread is not current:
            self._listener_thread.join(timeout=1.5)
        self._listener_thread = None
        with self._lock:
            threads = tuple(self._client_threads)
        for thread in threads:
            if thread is not current:
                thread.join(timeout=2)
        with self._lock:
            self._client_sockets.clear()
            self._client_threads.clear()
            self._staged_file = None
            self._remote_source_id = ID_NONE
            self._remote_drop_active = False
            self._local_destination_id = ID_NONE
            self._expected_push_id = ID_NONE
            self._expected_push_until = 0.0
            self._drag_candidate = None
            self._drag_candidate_until = 0.0
            self._candidate_stage_running = False

    def process_packet(self, packet: Packet) -> None:
        """Advance the PowerToys drag state from one control packet."""

        if not self.enabled or self._stop.is_set():
            return
        if packet.type == PackageType.CLIPBOARD_DRAG_DROP:
            if packet.src not in (ID_NONE, ID_ALL):
                with self._lock:
                    self._remote_source_id = packet.src
            return
        if packet.type == PackageType.CLIPBOARD_DRAG_DROP_OPERATION:
            if packet.dest in (self.config.machine_id, ID_ALL):
                with self._lock:
                    self._remote_drop_active = self._remote_source_id not in (
                        ID_NONE,
                        ID_ALL,
                    )
                if self._remote_drop_active:
                    self._show_drop_indicator()
                    self.status_callback("Release the mouse to receive the dragged file")
            return
        if packet.type == PackageType.CLIPBOARD_DRAG_DROP_END:
            self._hide_drop_indicator()
            with self._lock:
                self._remote_source_id = ID_NONE
                self._remote_drop_active = False
                self._staged_file = None
                self._staged_until = 0.0
                # A controller can cancel while the GTK helper is still
                # inspecting the active Wayland drag.  Do not let that late
                # result advertise a file to the previous destination.
                self._local_destination_id = ID_NONE
                self._expected_push_id = ID_NONE
                self._expected_push_until = 0.0
            return
        if (
            packet.type == PackageType.CLIPBOARD_ASK
            and packet.dest in (self.config.machine_id, ID_ALL)
            and packet.src not in (ID_NONE, ID_ALL)
        ):
            self._start_upload(packet.src)
            return
        if (
            packet.type == PackageType.EXPLORER_DRAG_DROP
            and packet.dest in (self.config.machine_id, ID_ALL)
            and packet.src not in (ID_NONE, ID_ALL)
        ):
            self.control_changed(packet.src)

    def control_changed(self, destination_id: int | None) -> None:
        """Track where Linux-controlled input is and probe/cancel local DND."""

        if self._stop.is_set():
            return
        destination_id = destination_id or ID_NONE
        with self._lock:
            previous_id = self._local_destination_id
            self._local_destination_id = destination_id
            staged = (
                self._staged_file is not None
                and time.monotonic() <= self._staged_until
            )
            if not staged:
                self._staged_file = None
                self._staged_until = 0.0
            candidate = (
                self._drag_candidate
                if self._drag_candidate is not None
                and time.monotonic() <= self._drag_candidate_until
                else None
            )
            if candidate is None:
                self._drag_candidate = None
                self._drag_candidate_until = 0.0
        if destination_id in (ID_NONE, ID_ALL):
            if previous_id not in (ID_NONE, ID_ALL) and staged:
                self._send_drag_end(previous_id)
            with self._lock:
                self._staged_file = None
                self._staged_until = 0.0
                self._drag_candidate = None
                self._drag_candidate_until = 0.0
            return
        if staged:
            if previous_id != destination_id:
                if previous_id not in (ID_NONE, ID_ALL):
                    self._send_drag_end(previous_id)
                operation = Packet()
                operation.type = PackageType.CLIPBOARD_DRAG_DROP_OPERATION
                operation.dest = destination_id
                self.send_packet(operation)
            return
        if candidate is not None:
            with self._lock:
                self._drag_candidate = None
                self._drag_candidate_until = 0.0
            try:
                self.stage_file(candidate, destination_id)
            except (FileTransferError, OSError) as exc:
                self.status_callback(f"File drag unavailable: {exc}")
            return
        self._start_drag_probe()

    def handle_remote_mouse(self, event: int) -> None:
        """Finish a Windows-origin drag when its injected left button rises."""

        # Importing input here would create a cycle; this is WM_LBUTTONUP.
        if self._stop.is_set():
            return
        # PowerToys treats a right-button release as drag cancellation.
        if event == 0x205:
            with self._lock:
                self._remote_source_id = ID_NONE
                self._remote_drop_active = False
            self._hide_drop_indicator()
            return
        if event != 0x202:
            return
        with self._lock:
            if (
                not self._remote_drop_active
                or self._remote_source_id in (ID_NONE, ID_ALL)
                or self._download_running
            ):
                return
            source_id = self._remote_source_id
            self._remote_drop_active = False
            self._download_running = True
        self._hide_drop_indicator()
        thread = threading.Thread(
            target=self._download_worker,
            args=(source_id,),
            name="mwb-file-download",
            daemon=True,
        )
        thread.start()

    def stage_file(self, path: Path, destination_id: int) -> None:
        """Publish one local file as the active cross-screen drag."""

        path = path.expanduser().resolve()
        if not path.is_file():
            raise FileTransferError(
                "only one regular file can be dragged between computers"
            )
        if destination_id in (ID_NONE, ID_ALL) or not self.peer_by_id(destination_id):
            raise FileTransferError("the drag destination is no longer connected")
        with self._lock:
            self._staged_file = path
            self._staged_until = time.monotonic() + STAGED_FILE_LIFETIME
        advertised = Packet()
        advertised.type = PackageType.CLIPBOARD_DRAG_DROP
        advertised.dest = ID_ALL
        self.send_packet(advertised)
        operation = Packet()
        operation.type = PackageType.CLIPBOARD_DRAG_DROP_OPERATION
        operation.dest = destination_id
        self.send_packet(operation)
        self.status_callback(f"Dragging {path.name} to the other computer")

    def _start_drag_probe(self) -> None:
        with self._lock:
            if (
                self._stop.is_set()
                or self._probe_running
                or self._candidate_stage_running
            ):
                return
            self._probe_running = True

        def worker() -> None:
            try:
                path = self.drag_probe()
                if path is not None and not self._stop.is_set():
                    with self._lock:
                        destination_id = self._local_destination_id
                        already_staged = self._staged_file is not None
                    if (
                        destination_id not in (ID_NONE, ID_ALL)
                        and not already_staged
                    ):
                        self.stage_file(path, destination_id)
            except (FileTransferError, OSError) as exc:
                self.status_callback(f"File drag unavailable: {exc}")
            finally:
                with self._lock:
                    self._probe_running = False

        threading.Thread(
            target=worker,
            name="mwb-drag-probe",
            daemon=True,
        ).start()

    def _drag_monitor_edges(self) -> tuple[str, ...]:
        return tuple(
            edge
            for edge in ("left", "right", "top", "bottom")
            if neighbour(
                self.config.machine_matrix,
                self.config.two_row,
                self.config.machine_name,
                edge,
                wrap=bool(self.config.other_options.get("wrap_mouse")),
            )
        )

    def _drag_monitor_targets(self) -> list[dict[str, object]]:
        targets: list[dict[str, object]] = []
        for edge in self._drag_monitor_edges():
            target: dict[str, object] = {"edge": edge}
            if (
                edge == self.config.host_position
                and len(self.config.host_zone) == 4
                and self.config.host_zone[2] > 0
                and self.config.host_zone[3] > 0
            ):
                target["zone"] = list(self.config.host_zone)
            targets.append(target)
        return targets

    def _start_drag_monitor(self) -> None:
        if (
            self._stop.is_set()
            or not os.environ.get("DISPLAY")
            or self._drag_monitor_process is not None
        ):
            return
        edges = self._drag_monitor_edges()
        if not edges:
            return
        environment = background_environment()
        environment["GDK_BACKEND"] = "x11"
        environment["MWB_DRAG_MONITOR_EDGES"] = ",".join(edges)
        environment["MWB_DRAG_MONITOR_TARGETS"] = json.dumps(
            self._drag_monitor_targets(), separators=(",", ":")
        )
        try:
            process = subprocess.Popen(
                _helper_command("_drag-monitor"),
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
                env=environment,
            )
        except OSError as exc:
            LOGGER.warning("could not start the Linux drag edge monitor: %s", exc)
            return
        self._drag_monitor_process = process
        started_at = time.monotonic()

        def read_candidates() -> None:
            stream = process.stdout
            if stream is None:
                return
            try:
                for line in stream:
                    if self._stop.is_set():
                        break
                    try:
                        value = json.loads(line)
                        event = value.get("event") if isinstance(value, dict) else None
                        path_value = value.get("path") if isinstance(value, dict) else None
                    except (json.JSONDecodeError, TypeError):
                        continue
                    if event == "drag" and isinstance(path_value, str):
                        self._record_drag_candidate(Path(path_value))
                    elif event == "leave":
                        self._record_drag_leave()
            finally:
                stream.close()
                with self._lock:
                    if time.monotonic() - started_at >= 5.0:
                        self._drag_monitor_failures = 1
                    else:
                        self._drag_monitor_failures += 1
                    should_restart = (
                        not self._stop.is_set()
                        and self._drag_monitor_process is process
                        and self._drag_monitor_failures <= 3
                    )
                    if self._drag_monitor_process is process:
                        self._drag_monitor_process = None
                if should_restart:
                    LOGGER.warning("Linux drag edge monitor exited; restarting it")
                    time.sleep(0.5 * (2 ** (self._drag_monitor_failures - 1)))
                    self._start_drag_monitor()
                elif not self._stop.is_set():
                    LOGGER.error(
                        "Linux drag edge monitor repeatedly failed; reconnect to retry"
                    )

        self._drag_monitor_thread = threading.Thread(
            target=read_candidates,
            name="mwb-drag-monitor",
            daemon=True,
        )
        self._drag_monitor_thread.start()

    def _stop_drag_monitor(self) -> None:
        process = self._drag_monitor_process
        self._drag_monitor_process = None
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        thread = self._drag_monitor_thread
        self._drag_monitor_thread = None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2)

    def _record_drag_candidate(self, path: Path) -> None:
        try:
            path = path.expanduser().resolve()
        except OSError:
            return
        if not path.is_file() or self._stop.is_set():
            return
        with self._lock:
            self._drag_candidate = path
            self._drag_candidate_until = time.monotonic() + DRAG_CANDIDATE_LIFETIME
            destination_id = self._local_destination_id
            already_staged = self._staged_file is not None
        if destination_id in (ID_NONE, ID_ALL) or already_staged:
            return
        # Claim the candidate under the same lock used by ``control_changed``.
        # The edge event and portal activation can arrive on different threads
        # only a few microseconds apart; exactly one of them may stage the file.
        with self._lock:
            if self._drag_candidate != path:
                return
            self._drag_candidate = None
            self._drag_candidate_until = 0.0
            self._candidate_stage_running = True
        try:
            self.stage_file(path, destination_id)
        except (FileTransferError, OSError) as exc:
            self.status_callback(f"File drag unavailable: {exc}")
        finally:
            with self._lock:
                self._candidate_stage_running = False

    def _record_drag_leave(self) -> None:
        """Keep a brief crossing grace period, then discard a cancelled drag."""

        with self._lock:
            if self._drag_candidate is not None:
                self._drag_candidate_until = min(
                    self._drag_candidate_until,
                    time.monotonic() + DRAG_LEAVE_GRACE,
                )

    def _show_drop_indicator(self) -> None:
        if not self.visual_feedback or not os.environ.get("DISPLAY"):
            return
        process = self._drop_indicator_process
        if process is not None and process.poll() is None:
            return
        environment = background_environment()
        environment["GDK_BACKEND"] = "x11"
        try:
            self._drop_indicator_process = subprocess.Popen(
                _helper_command("_drop-indicator"),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=environment,
            )
        except OSError as exc:
            LOGGER.debug("could not show the file drop animation: %s", exc)

    def _hide_drop_indicator(self) -> None:
        process = self._drop_indicator_process
        self._drop_indicator_process = None
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=1)

    def _send_drag_end(self, destination_id: int) -> None:
        packet = Packet()
        packet.type = PackageType.CLIPBOARD_DRAG_DROP_END
        packet.dest = destination_id
        try:
            self.send_packet(packet)
        except (OSError, ConnectionError):
            pass

    def _start_upload(self, destination_id: int) -> None:
        """Honor PowerToys' reverse-push fallback for a staged local file."""

        with self._lock:
            staged = (
                self._staged_file is not None
                and time.monotonic() <= self._staged_until
            )
            if self._stop.is_set() or not staged or self._upload_running:
                return
            self._upload_running = True
        threading.Thread(
            target=self._upload_worker,
            args=(destination_id,),
            name="mwb-file-upload",
            daemon=True,
        ).start()

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            listener = self._listener
            if listener is None:
                break
            try:
                client, address = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            remote_address = str(address[0])
            thread = threading.Thread(
                target=self._serve_client,
                args=(client, remote_address),
                name="mwb-file-client",
                daemon=True,
            )
            with self._lock:
                if self._stop.is_set():
                    client.close()
                    break
                self._client_sockets.add(client)
                self._client_threads.add(thread)
            thread.start()

    def _serve_client(self, client: socket.socket, address: str) -> None:
        stream: EncryptedSocket | None = None
        try:
            client.settimeout(30)
            peer = self.peer_by_address(address)
            if peer is None:
                raise FileTransferError("file connection is not from an authenticated peer")
            stream, request = self._handshake(client, peer, push=True)
            if request.type == PackageType.CLIPBOARD_PUSH:
                with self._lock:
                    expected = (
                        self._expected_push_id == peer.machine_id
                        and time.monotonic() <= self._expected_push_until
                    )
                    if expected:
                        self._expected_push_id = ID_NONE
                        self._expected_push_until = 0.0
                if not expected:
                    raise FileTransferError("peer pushed a file without a pending drop")
                destination = self._receive_file(stream, peer)
                self.status_callback(
                    f"Received {destination.name} in {destination.parent}"
                )
            elif request.type == PackageType.CLIPBOARD:
                self._send_staged_file(stream)
            else:
                raise FileTransferError("unexpected file connection request")
        except (OSError, EOFError, ValueError, FileTransferError) as exc:
            if not self._stop.is_set():
                LOGGER.warning("file connection from %s failed: %s", address, exc)
        finally:
            if stream is not None:
                stream.close()
            else:
                try:
                    client.close()
                except OSError:
                    pass
            with self._lock:
                self._client_sockets.discard(client)
                self._client_threads.discard(threading.current_thread())

    def _upload_worker(self, destination_id: int) -> None:
        peer = self.peer_by_id(destination_id)
        stream: EncryptedSocket | None = None
        raw: socket.socket | None = None
        try:
            if peer is None:
                raise FileTransferError("the file destination disconnected")
            raw = socket.create_connection((peer.address, self.config.port), timeout=8)
            raw.settimeout(30)
            with self._lock:
                self._client_sockets.add(raw)
            stream, _response = self._handshake(raw, peer, push=True)
            self._send_staged_file(stream)
        except (OSError, EOFError, ValueError, FileTransferError) as exc:
            if not self._stop.is_set():
                self.status_callback(f"File transfer failed: {exc}")
                LOGGER.warning(
                    "could not push dragged file to %s: %s", destination_id, exc
                )
        finally:
            if stream is not None:
                stream.close()
            elif raw is not None:
                try:
                    raw.close()
                except OSError:
                    pass
            if raw is not None:
                with self._lock:
                    self._client_sockets.discard(raw)
            with self._lock:
                self._upload_running = False

    def _download_worker(self, source_id: int) -> None:
        peer = self.peer_by_id(source_id)
        try:
            if peer is None:
                raise FileTransferError("the computer with the dragged file disconnected")
            try:
                destination = self._download_direct(peer)
            except (OSError, EOFError) as direct_error:
                if self._stop.is_set():
                    raise direct_error
                # This is the same fallback used by PowerToys when its current
                # control-channel direction cannot open the peer's base port:
                # ask the source to connect back and push the file instead.
                with self._lock:
                    self._expected_push_id = source_id
                    self._expected_push_until = time.monotonic() + 30.0
                request = Packet()
                request.type = PackageType.CLIPBOARD_ASK
                request.dest = source_id
                request.machine_name = self.config.machine_name
                request.post_action = TRANSFER_POST_ACTION_DESKTOP
                try:
                    self.send_packet(request)
                except Exception:
                    with self._lock:
                        self._expected_push_id = ID_NONE
                        self._expected_push_until = 0.0
                    raise direct_error
                self.status_callback("Waiting for the other computer to send the file")
                return
            self.status_callback(f"Received {destination.name} in {destination.parent}")
        except (OSError, EOFError, ValueError, FileTransferError) as exc:
            if not self._stop.is_set():
                self.status_callback(f"File transfer failed: {exc}")
                LOGGER.warning(
                    "could not receive dragged file from %s: %s", source_id, exc
                )
        finally:
            with self._lock:
                self._download_running = False
                self._remote_source_id = ID_NONE

    def _download_direct(self, peer: TransferPeer) -> Path:
        raw = socket.create_connection((peer.address, self.config.port), timeout=8)
        raw.settimeout(30)
        with self._lock:
            self._client_sockets.add(raw)
        stream: EncryptedSocket | None = None
        try:
            stream, _response = self._handshake(
                raw,
                peer,
                push=False,
                post_action=TRANSFER_POST_ACTION_DESKTOP,
            )
            return self._receive_file(stream, peer)
        finally:
            if stream is not None:
                stream.close()
            else:
                raw.close()
            with self._lock:
                self._client_sockets.discard(raw)

    def _handshake(
        self,
        client: socket.socket,
        peer: TransferPeer,
        *,
        push: bool,
        post_action: int = 0,
    ) -> tuple[EncryptedSocket, Packet]:
        try:
            profile = CryptoProfile(peer.profile)
        except ValueError as exc:
            raise FileTransferError(
                f"unsupported peer encryption profile {peer.profile}"
            ) from exc
        stream = EncryptedSocket(client, self.config.secret, profile)
        outgoing = Packet()
        outgoing.type = (
            PackageType.CLIPBOARD_PUSH if push else PackageType.CLIPBOARD
        )
        outgoing.src = self.config.machine_id
        outgoing.post_action = post_action
        outgoing.machine_name = self.config.machine_name
        stream.send(bytes(outgoing.raw))
        incoming = Packet(bytearray(stream.receive(PACKAGE_SIZE_EX)))
        if incoming.type not in (PackageType.CLIPBOARD, PackageType.CLIPBOARD_PUSH):
            raise FileTransferError("peer returned an invalid file handshake")
        if (
            incoming.src != peer.machine_id
            or incoming.machine_name.casefold() != peer.name.casefold()
        ):
            raise FileTransferError("file handshake does not match the control connection")
        return stream, incoming

    def _send_staged_file(self, stream: EncryptedSocket) -> None:
        with self._lock:
            path = self._staged_file
            valid = path is not None and time.monotonic() <= self._staged_until
            self._staged_file = None
            self._staged_until = 0.0
        if not valid or path is None or not path.is_file():
            raise FileTransferError("no dragged file is available")
        size = path.stat().st_size
        self._send_header(stream, size, windows_safe_name(path.name))
        with path.open("rb") as source:
            remaining = size
            while remaining >= TRANSFER_CHUNK_SIZE:
                block = source.read(TRANSFER_CHUNK_SIZE)
                if len(block) != TRANSFER_CHUNK_SIZE:
                    raise FileTransferError("the dragged file changed while it was sent")
                stream.send(block)
                remaining -= len(block)
            tail = source.read(remaining)
            if len(tail) != remaining:
                raise FileTransferError("the dragged file changed while it was sent")
        # PowerToys pads file streams to a 32-byte package boundary and sends
        # a complete block even when the file is already aligned.
        padding = 32 - (size % 32)
        stream.send(tail + b"\0" * padding)
        self.status_callback(f"Sent {path.name}")

    @staticmethod
    def _send_header(stream: EncryptedSocket, size: int, filename: str) -> None:
        encoded = f"{size}*{filename}".encode("utf-16le")
        if len(encoded) > CLIPBOARD_HEADER_SIZE:
            raise FileTransferError("the filename is too long for Mouse Without Borders")
        stream.send(encoded.ljust(CLIPBOARD_HEADER_SIZE, b"\0"))

    def _receive_file(self, stream: EncryptedSocket, peer: TransferPeer) -> Path:
        header = stream.receive(CLIPBOARD_HEADER_SIZE)
        try:
            value = header.decode("utf-16le").rstrip("\x00")
            size_text, remote_path = value.split("*", 1)
            size = int(size_text)
        except (UnicodeError, ValueError) as exc:
            raise FileTransferError("peer sent an invalid file header") from exc
        if size < 0:
            raise FileTransferError("peer announced a negative file size")
        filename = safe_remote_name(remote_path)
        root = self.destination_root or desktop_directory() / "MouseWithoutBorders"
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if shutil.disk_usage(root).free < size:
            raise FileTransferError("not enough free disk space for the dragged file")
        destination = unique_destination(root, filename)
        partial = destination.with_name(destination.name + ".part")
        remaining = size
        try:
            with partial.open("xb") as output:
                os.chmod(partial, 0o600)
                while remaining:
                    wanted = min(remaining, TRANSFER_CHUNK_SIZE)
                    encrypted_size = int(math.ceil(wanted / 16)) * 16
                    block = stream.receive(encrypted_size)
                    output.write(block[:wanted])
                    remaining -= wanted
                output.flush()
                os.fsync(output.fileno())
            # Path.replace() would overwrite a file created after the initial
            # collision check. A same-directory hard link publishes the
            # completed inode atomically and fails instead of overwriting.
            while True:
                try:
                    os.link(partial, destination)
                    break
                except FileExistsError:
                    destination = unique_destination(root, filename)
            partial.unlink()
        except Exception:
            try:
                partial.unlink()
            except FileNotFoundError:
                pass
            raise
        LOGGER.info("received %s bytes from %s as %s", size, peer.name, destination)
        return destination
