"""Portal bridge integration and Windows/Linux input translation."""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

from .config import Config
from .keymap import evdev_to_windows, windows_to_evdev
from .protocol import ID_ALL, ID_NONE, Packet, PackageType
from .topology import direction_to, neighbour

LOGGER = logging.getLogger(__name__)
#: InputCapture gained CreateSession2/Start with restore tokens in interface
#: version 2, shipped by xdg-desktop-portal 1.21.1.
CAPTURE_PERSIST_VERSION = 2
CAPTURE_PERSIST_RELEASE = "1.21.1"
WINDOWS_EPOCH_TICKS = 621_355_968_000_000_000

WM_MOUSEMOVE = 0x200
WM_LBUTTONDOWN = 0x201
WM_LBUTTONUP = 0x202
WM_RBUTTONDOWN = 0x204
WM_RBUTTONUP = 0x205
WM_MBUTTONDOWN = 0x207
WM_MBUTTONUP = 0x208
WM_MOUSEWHEEL = 0x20A
WM_XBUTTONDOWN = 0x20B
WM_XBUTTONUP = 0x20C
WM_MOUSEHWHEEL = 0x20E
CONTROLLED_EDGE_THRESHOLD = 128
EDGE_ENTRY_OFFSET = 800
# Ubuntu Dock defaults to a 100px pressure threshold. One injected frame must
# cross it because Windows may send only one clamped coordinate at the edge.
EDGE_PRESSURE_DISTANCE = 128.0

BUTTON_TO_WM = {
    (272, True): WM_LBUTTONDOWN,
    (272, False): WM_LBUTTONUP,
    (273, True): WM_RBUTTONDOWN,
    (273, False): WM_RBUTTONUP,
    (274, True): WM_MBUTTONDOWN,
    (274, False): WM_MBUTTONUP,
    (275, True): WM_XBUTTONDOWN,
    (275, False): WM_XBUTTONUP,
    (276, True): WM_XBUTTONDOWN,
    (276, False): WM_XBUTTONUP,
}


def _mwb_to_eis_coordinate(value: int, origin: float, extent: float) -> float:
    """Decode one MWB absolute axis into a valid EIS region coordinate.

    The Windows sender quantizes a pixel coordinate with ``pixel * 65535 /
    screen_extent`` using integer division.  A plain floating-point inverse is
    therefore just below the original pixel for almost every nonzero value
    (for example, row 1079 becomes 1078.995 on a 1080-row display).  Round the
    inverse to the nearest pixel to recover the sender's quantization without
    shifting ordinary midpoint values, then clamp it to the half-open EIS
    region so the 65535 endpoint never becomes the out-of-bounds ``origin +
    extent``.
    """

    region_origin = int(origin)
    region_extent = max(1, int(extent))
    normalized = max(0, min(65535, int(value)))
    pixel = (normalized * region_extent + 32767) // 65535
    return float(region_origin + min(region_extent - 1, pixel))


def capture_targets(config: Config) -> list[dict[str, object]]:
    """Return each remote directly reachable from the local matrix tile."""

    targets: list[dict[str, object]] = []
    for edge in ("left", "right", "top", "bottom"):
        machine_name = neighbour(
            config.machine_matrix,
            config.two_row,
            config.machine_name,
            edge,
            wrap=bool(config.other_options.get("wrap_mouse")),
        )
        if not machine_name or machine_name.casefold() == config.machine_name.casefold():
            continue
        target: dict[str, object] = {"edge": edge, "target": machine_name}
        if edge == config.host_position and config.host_zone:
            target["zone"] = list(config.host_zone)
        targets.append(target)
    return targets


def find_bridge() -> str | None:
    override = os.environ.get("MWB_PORTAL_BRIDGE")
    module_root = Path(__file__).resolve().parents[1]
    installed_root = Path("/usr/lib/powertoys-mouse-without-borders")
    installed = str(installed_root / "mwb-portal-bridge")
    frozen = (
        str(Path(sys.executable).with_name("mwb-portal-bridge"))
        if getattr(sys, "frozen", False)
        else None
    )
    source = [
        str(module_root / "portal-bridge" / "target" / "release" / "mwb-portal-bridge"),
        str(module_root / "portal-bridge" / "target" / "debug" / "mwb-portal-bridge"),
    ]
    # A source daemon must exercise the source bridge instead of silently
    # falling back to an older system package. Installed modules keep using
    # their co-packaged helper.
    packaged = module_root.is_relative_to(installed_root)
    preferred = [installed, *source] if packaged else [*source, installed]
    candidates = [
        override,
        frozen,
        *preferred,
        shutil.which("mwb-portal-bridge"),
    ]
    return next((candidate for candidate in candidates if candidate and os.access(candidate, os.X_OK)), None)


def portal_environment() -> dict[str, str]:
    """Identify the helper as Mouse Without Borders to desktop portals."""

    environment = os.environ.copy()
    module_root = Path(__file__).resolve().parents[1]
    installed_root = Path("/usr/lib/powertoys-mouse-without-borders")
    appdir = os.environ.get("APPDIR")
    appimage_desktop_file = None
    if appdir:
        appimage_desktop_file = (
            Path(appdir)
            / "usr/share/applications/io.github.NaveDanan.MouseWithoutBorders.desktop"
        )
    if appimage_desktop_file and appimage_desktop_file.is_file():
        desktop_file = appimage_desktop_file
    elif module_root.is_relative_to(installed_root):
        desktop_file = Path(
            "/usr/share/applications/io.github.NaveDanan.MouseWithoutBorders.desktop"
        )
    else:
        desktop_file = (
            module_root
            / "resources"
            / "io.github.NaveDanan.MouseWithoutBorders.desktop"
        )
    if desktop_file.is_file():
        environment["GIO_LAUNCHED_DESKTOP_FILE"] = str(desktop_file)
        environment["GIO_LAUNCHED_DESKTOP_FILE_PID"] = str(os.getpid())
    return environment


class PortalBridge:
    """Newline-delimited JSON client for the rootless Rust portal helper."""

    def __init__(self, event_callback: Callable[[dict], None]) -> None:
        self.event_callback = event_callback
        self.process: subprocess.Popen[str] | None = None
        self._write_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._pending_responses: dict[str, tuple[threading.Event, dict]] = {}
        self._stopping = False

    def start(
        self,
        edge: str,
        capture_restore_token: str = "",
        inject_restore_token: str = "",
        *,
        enable_capture: bool = True,
        zone: list[int] | None = None,
        targets: list[dict[str, object]] | None = None,
        backend: str = "portal",
        screen: list[int] | None = None,
        capture_backend: str = "portal",
    ) -> None:
        executable = find_bridge()
        if not executable:
            raise FileNotFoundError("mwb-portal-bridge is not built or installed")
        self._stopping = False
        self.process = subprocess.Popen(
            [executable],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=portal_environment(),
            text=True,
            bufsize=1,
        )
        threading.Thread(target=self._read_loop, name="mwb-portal-events", daemon=True).start()
        threading.Thread(target=self._stderr_loop, name="mwb-portal-log", daemon=True).start()
        if enable_capture:
            self.command(
                "capture_init",
                id="capture",
                edge=edge,
                backend=capture_backend,
                screen=list(screen) if screen else None,
                restore_token=capture_restore_token or None,
                # Restricts the pointer barrier to the monitor the settings
                # matrix places the Windows host against.
                zone=list(zone) if zone else None,
                targets=targets or None,
            )
        self.command(
            "inject_init",
            id="inject",
            restore_token=inject_restore_token or None,
            backend=backend,
            screen=list(screen) if screen else None,
        )

    def _read_loop(self) -> None:
        assert self.process and self.process.stdout
        for line in self.process.stdout:
            try:
                self._dispatch_message(json.loads(line))
            except (json.JSONDecodeError, TypeError) as exc:
                LOGGER.warning("invalid portal bridge response: %s", exc)
        with self._pending_lock:
            pending = list(self._pending_responses.values())
        for event, result in pending:
            result["response"] = {
                "ok": False,
                "error": "portal bridge stopped before replying",
            }
            event.set()
        if not self._stopping:
            self.event_callback({"type": "event", "event": "bridge_stopped"})

    def _dispatch_message(self, message: dict) -> None:
        if message.get("type") == "response":
            request_id = message.get("id")
            with self._pending_lock:
                pending = self._pending_responses.get(request_id)
            if pending:
                event, result = pending
                result["response"] = message
                event.set()
        self.event_callback(message)

    def _stderr_loop(self) -> None:
        assert self.process and self.process.stderr
        for line in self.process.stderr:
            LOGGER.debug("portal: %s", line.rstrip())

    def command(self, command: str, **arguments: object) -> None:
        if not self.process or self.process.poll() is not None or not self.process.stdin:
            raise ConnectionError("portal bridge is not running")
        message = json.dumps({"command": command, **arguments}, separators=(",", ":"))
        with self._write_lock:
            self.process.stdin.write(message + "\n")
            self.process.stdin.flush()

    def request(
        self, command: str, *, timeout: float = 3.0, **arguments: object
    ) -> dict:
        """Send a state-changing command and wait for its acknowledgement."""

        request_id = f"python-{time.monotonic_ns()}"
        event = threading.Event()
        result: dict = {}
        with self._pending_lock:
            self._pending_responses[request_id] = (event, result)
        try:
            self.command(command, id=request_id, **arguments)
            if not event.wait(timeout):
                raise TimeoutError(f"portal bridge did not acknowledge {command}")
            response = result["response"]
            if not response.get("ok"):
                raise ConnectionError(
                    str(response.get("error", f"portal bridge rejected {command}"))
                )
            return response
        finally:
            with self._pending_lock:
                self._pending_responses.pop(request_id, None)

    def stop(self) -> None:
        if not self.process:
            return
        self._stopping = True
        try:
            self.command("shutdown")
            self.process.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired, ConnectionError):
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=3)
        self.process = None
        self._stopping = False


class InputManager:
    """Translate portal events to MWB packets and remote packets to EIS."""

    def __init__(
        self,
        config: Config,
        send_packet: Callable[[Packet], None],
        peer_id: Callable[..., int | None],
        status_callback: Callable[[str], None],
        persist_config: Callable[[], None] | None = None,
        peer_name: Callable[[int], str | None] | None = None,
        wake_peer: Callable[[str], bool] | None = None,
        bridge: PortalBridge | None = None,
        control_changed: Callable[[int | None], None] | None = None,
    ) -> None:
        self.config = config
        self.send_packet = send_packet
        self._peer_lookup = peer_id
        self.status_callback = status_callback
        self.persist_config = persist_config or (lambda: None)
        self._peer_name_lookup = peer_name or (lambda _machine_id: None)
        self._wake_peer = wake_peer or (lambda _machine_name: False)
        self._control_changed = control_changed or (lambda _machine_id: None)
        self.bridge = bridge or PortalBridge(self._bridge_event)
        self.remote_active = False
        self.width = 1920
        self.height = 1080
        self.x = 0
        self.y = 32768
        self.inject_x = 0.0
        self.inject_y = 0.0
        self.inject_width = float(self.width)
        self.inject_height = float(self.height)
        self.inject_regions: list[tuple[int, int, int, int]] = []
        # The compositor pauses every injection device while this PC is
        # locked. Tracking it keeps a dead lock screen from looking like a
        # working session that silently swallows every packet.
        self.injection_paused = False
        self.session_locked = False
        self._inject_ready = False
        self._inject_started = False
        self._recovery_scheduled = False
        self.capture_portal_version = 0
        self.capture_persistable = False
        self._capture_persistence_reported = False
        self._keys_down: set[int] = set()
        self._started = False
        self._desired = False
        self._capture_ready = False
        self._capture_enabled = False
        self._restart_lock = threading.Lock()
        self._restart_scheduled = False
        self._pending_remote_name = ""
        self._waking_remote_name = ""
        self._controlled_edge = ""
        self._controlled_source_id = ID_NONE
        adjacent = neighbour(
            self.config.machine_matrix,
            self.config.two_row,
            self.config.machine_name,
            self.config.host_position,
            wrap=bool(self.config.other_options.get("wrap_mouse")),
        )
        self.active_remote_name = adjacent or next(
            (target.name for target in self.config.resolve_hosts()), ""
        )

    def _peer_id(self, machine_name: str | None = None) -> int | None:
        """Look up one peer while accepting legacy no-argument test adapters."""

        try:
            return self._peer_lookup(machine_name)
        except TypeError:
            return self._peer_lookup()

    def start(self) -> None:
        self._desired = True
        if self._started:
            if (
                self.config.edge_switching
                and self._capture_ready
                and not self._capture_enabled
            ):
                try:
                    self.bridge.request("capture_enable")
                    self._capture_enabled = True
                    self.status_callback("Screen-edge capture resumed")
                except (ConnectionError, TimeoutError) as exc:
                    self.status_callback(f"Input unavailable: {exc}")
            return
        try:
            self._capture_ready = not self.config.edge_switching
            self.bridge.start(
                self.config.host_position,
                self.config.capture_restore_token,
                self.config.inject_restore_token,
                enable_capture=self.config.edge_switching,
                zone=self.config.host_zone or None,
                targets=capture_targets(self.config),
                backend=self.inject_backend,
                screen=self.desktop_geometry(),
                capture_backend=self.capture_backend,
            )
            self._started = True
            self._inject_ready = False
            self._inject_started = True
            # InputCapture initialization is asynchronous and may still be
            # displaying the first-run consent dialog.
            self._capture_enabled = False
            self.status_callback("Portal permission requested")
        except (FileNotFoundError, OSError, ConnectionError) as exc:
            try:
                self.bridge.stop()
            except Exception:
                LOGGER.debug("could not clean up failed portal initialization", exc_info=True)
            self._started = False
            self.status_callback(f"Input unavailable: {exc}")
            LOGGER.warning("input portal unavailable: %s", exc)

    def pause(self) -> None:
        """Stop sharing while retaining the compositor's approved session."""

        self._desired = False
        self.release_local()
        LOGGER.info(
            "pausing portal bridge: started=%s capture_ready=%s capture_enabled=%s",
            self._started,
            self._capture_ready,
            self._capture_enabled,
        )
        if self._started and not self._capture_ready:
            # There is no approved session to preserve yet. Closing the bridge
            # also closes an unanswered portal prompt when the user exits.
            self.bridge.stop()
            self._started = False
        elif self._started and self._capture_enabled:
            try:
                self.bridge.request("capture_disable")
            except (ConnectionError, TimeoutError) as exc:
                # Exit must fail closed even if the compositor does not
                # acknowledge Disable. Closing the session guarantees that no
                # capture remains active, at the cost of another prompt later.
                LOGGER.warning("could not disable the portal capture session: %s", exc)
                self.bridge.stop()
                self._started = False
                self._capture_ready = False
        self._capture_enabled = False
        self.status_callback("Input sharing stopped")

    def stop(self) -> None:
        self._desired = False
        self.release_local()
        self.bridge.stop()
        self._started = False
        self._capture_ready = False
        self._capture_enabled = False
        self._inject_ready = False
        self._inject_started = False
        self.injection_paused = False

    def switch_remote(self, machine_name: str | None = None) -> None:
        if machine_name:
            self._pending_remote_name = ""
            if machine_name.casefold() == self.config.machine_name.casefold():
                self.release_local()
                return
            if not self._peer_id(machine_name):
                self.active_remote_name = machine_name
                self._waking_remote_name = machine_name
                if self._wake_peer(machine_name):
                    self.status_callback(f"Waking {machine_name}; waiting for connection")
                else:
                    self.status_callback(f"Cannot switch: {machine_name} is not connected")
                return
            self._waking_remote_name = ""
            self._wake_peer(machine_name)
            if self.remote_active:
                if machine_name.casefold() == self.active_remote_name.casefold():
                    self.status_callback(f"Already controlling {machine_name}")
                    return
                self._switch_active_target(machine_name)
                return
            self.active_remote_name = machine_name
            entry = self._first_matrix_hop(machine_name)
            if entry and entry.casefold() != machine_name.casefold():
                # The compositor can only activate a barrier adjacent to the
                # local tile. Remember the shortcut's exact destination while
                # capture enters through the first reachable matrix hop.
                self._pending_remote_name = machine_name
        peer = self._peer_id(self.active_remote_name)
        if not peer:
            self.status_callback("Cannot switch: no connected host")
            return
        if not self.config.edge_switching:
            self.status_callback("Enable screen-edge switching first")
            return
        if not self._capture_ready:
            self._waking_remote_name = self.active_remote_name
            self.status_callback("Waiting for input capture permission")
            return
        if not self.remote_active and self._trigger_edge():
            self.status_callback("Activating host input capture")
            return
        self.status_callback("Move the pointer through the configured screen edge")

    def retry_pending_switch(self) -> None:
        """Complete a requested switch after Wake-on-LAN reconnects the peer."""

        machine_name = self._waking_remote_name
        if machine_name and self._capture_ready and self._peer_id(machine_name):
            self._waking_remote_name = ""
            self.switch_remote(machine_name)

    def recover_active_peer(self) -> None:
        """Release a lost target, wake it, and resume control after reconnect."""

        machine_name = self.active_remote_name
        self.release_local()
        if machine_name:
            self._waking_remote_name = machine_name
            self._wake_peer(machine_name)
            self.status_callback(f"Reconnecting to {machine_name}")

    def _activate_remote(self, machine_name: str = "", edge: str = "") -> None:
        if self.remote_active:
            return
        if self._pending_remote_name:
            machine_name = self._pending_remote_name
            self._pending_remote_name = ""
        if machine_name:
            self.active_remote_name = machine_name
        if not self._peer_id(self.active_remote_name):
            # The portal may remain authorized while the network is manually
            # disconnected so GNOME 46 does not prompt again on every Connect.
            # If the pointer still crosses its barrier, release it immediately.
            try:
                self.bridge.command("capture_release")
            except ConnectionError:
                pass
            self._waking_remote_name = self.active_remote_name
            if self.active_remote_name and self._wake_peer(self.active_remote_name):
                self.status_callback(
                    f"Waking {self.active_remote_name}; waiting for connection"
                )
            else:
                self.status_callback("Cannot switch: no connected host")
            return
        self._waking_remote_name = ""
        self.remote_active = True
        edge = edge or direction_to(
            self.config.machine_matrix,
            self.config.two_row,
            self.config.machine_name,
            self.active_remote_name,
        ) or self.config.host_position
        self.x = 32768
        self.y = 32768
        if edge == "right":
            self.x = 800
        elif edge == "left":
            self.x = 65535 - 800
        elif edge == "bottom":
            self.y = 800
        else:
            self.y = 65535 - 800
        self._control_changed(self._peer_id(self.active_remote_name))
        # Capture is activated by crossing the compositor-owned barrier. There
        # is intentionally no portal API that force-grabs global input.
        self.status_callback(f"Controlling {self.active_remote_name}")

    def _trigger_edge(self) -> bool:
        """Use XWayland pointer warping when available to trigger the portal barrier."""

        executable = shutil.which("xdotool")
        if not executable:
            return False
        try:
            location = subprocess.check_output(
                [executable, "getmouselocation", "--shell"], text=True, timeout=2
            )
            values = dict(
                line.split("=", 1) for line in location.splitlines() if "=" in line
            )
            x = int(values.get("X", self.width // 2))
            y = int(values.get("Y", self.height // 2))
            requested = self._pending_remote_name or self.active_remote_name
            entry = self._first_matrix_hop(requested) or requested
            edge = direction_to(
                self.config.machine_matrix,
                self.config.two_row,
                self.config.machine_name,
                entry,
            ) or self.config.host_position
            targets = {
                "right": (self.width - 2, y, 8, 0),
                "left": (1, y, -8, 0),
                "bottom": (x, self.height - 2, 0, 8),
                "top": (x, 1, 0, -8),
            }
            target_x, target_y, relative_x, relative_y = targets[edge]
            subprocess.run(
                [executable, "mousemove", str(target_x), str(target_y)],
                check=True,
                timeout=2,
            )
            subprocess.run(
                [
                    executable,
                    "mousemove_relative",
                    "--",
                    str(relative_x),
                    str(relative_y),
                ],
                check=True,
                timeout=2,
            )
            return True
        except (OSError, ValueError, subprocess.SubprocessError):
            return False

    def _first_matrix_hop(self, destination: str) -> str:
        """Return the local-adjacent tile on a route to ``destination``."""

        source = self.config.machine_name
        queue: list[tuple[str, str]] = [(source, "")]
        visited = {source.casefold()}
        while queue:
            current, first = queue.pop(0)
            for direction in ("left", "right", "top", "bottom"):
                candidate = neighbour(
                    self.config.machine_matrix,
                    self.config.two_row,
                    current,
                    direction,
                    wrap=bool(self.config.other_options.get("wrap_mouse")),
                )
                key = candidate.casefold()
                if not candidate or key in visited:
                    continue
                first_hop = first or candidate
                if key == destination.casefold():
                    return first_hop
                visited.add(key)
                queue.append((candidate, first_hop))
        return ""

    def release_local(self, cursor_position: tuple[int, int] | None = None) -> None:
        self._pending_remote_name = ""
        if not self.remote_active:
            return
        self.remote_active = False
        self._control_changed(None)
        for code in list(self._keys_down):
            self._send_key(code, False)
        self._keys_down.clear()
        try:
            arguments = {}
            if cursor_position is not None:
                arguments["cursor_position"] = [
                    _mwb_to_eis_coordinate(
                        cursor_position[0], self.inject_x, self.inject_width
                    ),
                    _mwb_to_eis_coordinate(
                        cursor_position[1], self.inject_y, self.inject_height
                    ),
                ]
            self.bridge.command("capture_release", **arguments)
        except ConnectionError:
            pass
        self.status_callback("Controlling this computer")

    def _bridge_event(self, event: dict) -> None:
        if event.get("type") == "response":
            if not event.get("ok"):
                error = str(event.get("error", "unknown"))
                if self.session_locked or not self._inject_ready:
                    # Every injection is refused while the compositor holds no
                    # input devices. That is expected, and the lock status
                    # already explains it, so do not bury it under one error
                    # per remote mouse packet.
                    LOGGER.debug("portal command rejected while unavailable: %s", error)
                    return
                self.status_callback(f"Input portal error: {error}")
                return
            if event.get("id") == "capture":
                self.apply_capture_result(event.get("result", {}))
            elif event.get("id") == "inject":
                self.apply_inject_result(event.get("result", {}))
            return
        event_type = event.get("event")
        if event_type == "capture_activated":
            self._activate_remote(
                str(event.get("target", "")), str(event.get("edge", ""))
            )
        elif event_type in ("capture_deactivated", "capture_disabled"):
            self.release_local()
        elif event_type == "key" and self.remote_active:
            code = int(event["keycode"])
            pressed = event["state"] == "pressed"
            if pressed:
                self._keys_down.add(code)
            else:
                self._keys_down.discard(code)
            # Ctrl+Alt+Esc is the always-available panic return while captured.
            if pressed and code == 1 and {29, 56}.issubset(self._keys_down):
                self.release_local()
                return
            self._send_key(code, pressed)
        elif event_type == "pointer_motion" and self.remote_active:
            self._send_motion(float(event.get("dx", 0)), float(event.get("dy", 0)))
        elif event_type == "button" and self.remote_active:
            self._send_button(int(event["button"]), event["state"] == "pressed")
        elif event_type == "scroll" and self.remote_active:
            self._send_scroll(
                float(event.get("dx", 0)),
                float(event.get("dy", 0)),
                discrete=bool(event.get("discrete")),
            )
        elif event_type == "inject_device_added" and event.get("pointer_absolute"):
            regions = event.get("regions", [])
            for region in regions:
                parsed = (
                    int(region.get("x", 0)),
                    int(region.get("y", 0)),
                    int(region.get("width", 0)),
                    int(region.get("height", 0)),
                )
                if parsed[2] > 0 and parsed[3] > 0 and parsed not in self.inject_regions:
                    self.inject_regions.append(parsed)
            if self.inject_regions:
                left = min(region[0] for region in self.inject_regions)
                top = min(region[1] for region in self.inject_regions)
                right = max(region[0] + region[2] for region in self.inject_regions)
                bottom = max(region[1] + region[3] for region in self.inject_regions)
                self.inject_x = float(left)
                self.inject_y = float(top)
                self.inject_width = float(right - left)
                self.inject_height = float(bottom - top)
        elif event_type == "capture_devices_changed":
            devices = event.get("devices", [])
            LOGGER.info(
                "screen-edge capture now watches %d input devices: %s",
                len(devices),
                ", ".join(str(name) for name in devices),
            )
        elif event_type == "capture_backend_fallback":
            self.status_callback(
                "Direct kernel capture unavailable, using the portal: "
                f"{event.get('reason', 'unknown')}"
            )
        elif event_type == "inject_backend_fallback":
            self.status_callback(
                "Direct kernel input unavailable, using the portal: "
                f"{event.get('reason', 'unknown')}"
            )
        elif event_type in ("inject_devices_paused", "inject_devices_resumed"):
            self._update_injection_availability(int(event.get("active", 0)))
        elif event_type == "inject_error":
            # The compositor tears the RemoteDesktop session down every time
            # the screen locks. Injection owns a restore token, so it can be
            # rebuilt silently; destroying the whole bridge would also throw
            # away the capture session and force a fresh consent dialog.
            self.injection_paused = True
            self._inject_ready = False
            self.status_callback(
                "This PC is locked; remote input resumes when you unlock it"
                if self.session_locked
                else f"Remote input interrupted: {event.get('error', 'unknown')}"
            )
            self._schedule_session_recovery()
        elif event_type == "capture_error":
            self._capture_ready = False
            self._capture_enabled = False
            if self.session_locked:
                # The compositor always drops the capture session at the lock
                # screen. Saying so here would bury the far more useful report
                # of whether remote input still reaches this PC.
                LOGGER.info(
                    "screen-edge capture ended with the lock screen: %s",
                    event.get("error", "unknown"),
                )
            else:
                self.status_callback(
                    f"Screen-edge capture interrupted: {event.get('error', 'unknown')}"
                )
            self._schedule_session_recovery()
        elif event_type == "bridge_stopped":
            self.status_callback("Input portal helper stopped; restarting")
            self._schedule_bridge_restart()
        elif event_type and event_type.endswith("_error"):
            self.status_callback(f"Input portal error: {event.get('error', 'unknown')}")
            self._schedule_bridge_restart()

    def session_lock_changed(self, locked: bool) -> None:
        """Track the lock screen, which decides whether portal input can exist."""

        self.session_locked = locked
        if locked:
            # Screen-edge capture always dies with the portal session, so the
            # local grab must be dropped either way.
            self.release_local()
            if self.injection_survives_lock:
                # Kernel input devices are not portal sessions; the compositor
                # has nothing to revoke, so remote control continues.
                self.status_callback(
                    "This PC is locked; remote keyboard and mouse still work"
                )
                return
            self.injection_paused = True
            self.status_callback(
                "This PC is locked; remote input resumes when you unlock it"
            )
            return
        self._schedule_session_recovery()

    def _schedule_session_recovery(self) -> None:
        """Rebuild only the portal sessions the compositor actually destroyed."""

        if not self._desired or self.session_locked:
            return
        with self._restart_lock:
            if self._recovery_scheduled:
                return
            self._recovery_scheduled = True

        def recover() -> None:
            try:
                for attempt in range(6):
                    time.sleep(min(1 + attempt, 4))
                    if not self._desired or self.session_locked:
                        return
                    if self._recover_sessions():
                        return
                self.status_callback(
                    "Remote input could not be restored; reconnect to retry"
                )
            finally:
                with self._restart_lock:
                    self._recovery_scheduled = False
        self.capture_portal_version = 0
        self.capture_persistable = False
        self._capture_persistence_reported = False

        threading.Thread(
            target=recover, name="mwb-portal-recovery", daemon=True
        ).start()

    def _recover_sessions(self) -> bool:
        """Re-arm the dead half of the portal without disturbing the live half."""

        if not self._started:
            self.start()
            return self._started
        recovered = True
        if not self._inject_ready:
            if self._inject_started:
                try:
                    # Frees the bridge's slot. The compositor already destroyed
                    # the session, so a failure here is expected and harmless.
                    self.bridge.request("inject_stop", timeout=3.0)
                except (ConnectionError, TimeoutError):
                    LOGGER.debug("dead injection session could not be closed politely")
                self._inject_started = False
            try:
                response = self.bridge.request(
                    "inject_init",
                    timeout=15.0,
                    restore_token=self.config.inject_restore_token or None,
                    backend=self.inject_backend,
                    screen=self.desktop_geometry(),
                )
            except (ConnectionError, TimeoutError) as exc:
                LOGGER.info("remote input session not restorable yet: %s", exc)
                recovered = False
            else:
                self.apply_inject_result(response.get("result", {}))
        if self.config.edge_switching and not self._capture_ready:
            try:
                response = self.bridge.request(
                    "capture_init",
                    timeout=60.0,
                    edge=self.config.host_position,
                    backend=self.capture_backend,
                    screen=self.desktop_geometry(),
                    restore_token=self.config.capture_restore_token or None,
                    zone=list(self.config.host_zone) if self.config.host_zone else None,
                    targets=capture_targets(self.config) or None,
                )
            except (ConnectionError, TimeoutError) as exc:
                LOGGER.info("screen-edge capture not restorable yet: %s", exc)
                recovered = False
            else:
                self.apply_capture_result(response.get("result", {}))
        return recovered

    def apply_capture_result(self, result: dict) -> None:
        """Record a started capture session, however it was requested.

        Recovery re-arms the session with its own request id, so this
        bookkeeping cannot live in the branch that only matches the initial
        "capture" id: doing so left the session running while the daemon still
        believed capture was dead.
        """

        token = result.get("restore_token")
        if token:
            self.config.capture_restore_token = token
            self.persist_config()
        self._note_capture_persistence(result, bool(token))
        zones = result.get("zones", [])
        if zones:
            left = min(int(zone.get("x", 0)) for zone in zones)
            top = min(int(zone.get("y", 0)) for zone in zones)
            right = max(int(zone.get("x", 0)) + int(zone.get("width", 0)) for zone in zones)
            bottom = max(int(zone.get("y", 0)) + int(zone.get("height", 0)) for zone in zones)
            self.width = max(1, right - left)
            self.height = max(1, bottom - top)
        self.status_callback("Screen-edge capture ready")
        self._capture_ready = True
        self._capture_enabled = True
        self.retry_pending_switch()

    def apply_inject_result(self, result: dict) -> None:
        """Record a started injection session, however it was requested."""

        token = result.get("restore_token")
        if token:
            self.config.inject_restore_token = token
            self.persist_config()
        self._inject_ready = True
        self._inject_started = True
        self.injection_paused = False
        self.status_callback("Remote input permission ready")

    @property
    def inject_backend(self) -> str:
        """Return the injection path the user configured."""

        if self.config.other_options.get("use_kernel_input"):
            return "uinput"
        return "portal"

    def desktop_geometry(self) -> list[int]:
        """Return the desktop rectangle the absolute pointer is ranged over."""

        if self.inject_regions:
            left = min(region[0] for region in self.inject_regions)
            top = min(region[1] for region in self.inject_regions)
            right = max(region[0] + region[2] for region in self.inject_regions)
            bottom = max(region[1] + region[3] for region in self.inject_regions)
            return [left, top, max(1, right - left), max(1, bottom - top)]
        return [0, 0, max(1, int(self.width)), max(1, int(self.height))]

    @property
    def capture_backend(self) -> str:
        """Return the screen-edge capture path the user configured."""

        if self.config.other_options.get("use_kernel_input"):
            return "evdev"
        return "portal"

    @property
    def injection_survives_lock(self) -> bool:
        """A kernel input device is not revoked when the session locks."""

        return self.inject_backend == "uinput"

    def _note_capture_persistence(self, result: dict, stored_token: bool) -> None:
        """Explain a permission prompt that the portal is unable to remember.

        Session persistence for InputCapture arrived in interface version 2
        (xdg-desktop-portal 1.21.1). On version 1 there is no restore token at
        all, so every rebuilt capture session must ask the user again. Saying
        so once beats leaving the repeated dialog unexplained.
        """

        try:
            version = int(result.get("portal_version") or 0)
        except (TypeError, ValueError):
            version = 0
        self.capture_portal_version = version
        self.capture_persistable = (
            bool(stored_token)
            or bool(result.get("persistent"))
            or version >= CAPTURE_PERSIST_VERSION
        )
        if self.capture_persistable or self._capture_persistence_reported:
            return
        self._capture_persistence_reported = True
        LOGGER.warning(
            "InputCapture portal version %d cannot remember this permission; "
            "xdg-desktop-portal %s or newer is required to stop the prompt "
            "returning after every lock or restart",
            version,
            CAPTURE_PERSIST_RELEASE,
        )

    def _update_injection_availability(self, active_devices: int) -> None:
        """Report the compositor suspending injection, usually a lock screen.

        A paused device is not an error and must not restart the bridge: the
        portal session is still valid and the compositor resumes it by itself
        once the session is unlocked.
        """

        paused = active_devices <= 0
        if paused == self.injection_paused:
            return
        self.injection_paused = paused
        if paused:
            self.status_callback(
                "This PC is locked; unlock it to let the remote keyboard and "
                "mouse control it again"
            )
        else:
            self.status_callback("Remote input session resumed")

    def _schedule_bridge_restart(self) -> None:
        """Recreate portal sessions if the compositor drops them after resume."""

        if not self._desired:
            return
        with self._restart_lock:
            if self._restart_scheduled:
                return
            self._restart_scheduled = True

        def restart() -> None:
            try:
                for attempt in range(5):
                    time.sleep(min(1 + attempt, 5))
                    if not self._desired:
                        return
                    self.bridge.stop()
                    self._started = False
                    self._capture_ready = False
                    self.start()
                    if self._started:
                        return
                self.status_callback("Input unavailable after resume; reconnect to retry")
            finally:
                with self._restart_lock:
                    self._restart_scheduled = False

        threading.Thread(
            target=restart, name="mwb-portal-restart", daemon=True
        ).start()

    def resume_after_suspend(self) -> None:
        """Keep live compositor grants and restart only a dead bridge."""

        if not self._desired:
            return
        self.release_local()
        self.status_callback("System resumed; checking remote input session")
        try:
            self.bridge.request("ping", timeout=3.0)
            self.status_callback("Remote input session resumed")
        except (ConnectionError, TimeoutError):
            # The bridge's EIS loops report their own portal errors. A failed
            # process-level ping is the only reason to discard both sessions;
            # keeping a live InputCapture v1 session avoids another consent
            # prompt because that portal version has no restore tokens.
            self._schedule_bridge_restart()

    def wake_display(self) -> None:
        """Send harmless compositor activity for an incoming AWAKE packet."""

        if not self._started:
            return
        try:
            self.bridge.command("inject_pointer_motion", dx=0.0, dy=0.0)
        except ConnectionError as exc:
            LOGGER.debug("compositor wake injection unavailable: %s", exc)

    def _send_key(self, code: int, pressed: bool) -> None:
        mapped = evdev_to_windows(code, pressed)
        peer = self._peer_id(self.active_remote_name)
        if not mapped or not peer:
            return
        packet = Packet()
        packet.type = PackageType.KEYBOARD
        packet.dest = peer
        packet.timestamp = WINDOWS_EPOCH_TICKS + int(time.time() * 10_000_000)
        packet.keyboard = mapped
        self.send_packet(packet)

    def _send_motion(self, dx: float, dy: float) -> None:
        peer = self._peer_id(self.active_remote_name)
        if not peer:
            return
        self.x += int(dx * 65535 / max(self.width, 1))
        self.y += int(dy * 65535 / max(self.height, 1))
        crossed = self._crossed_edge()
        if crossed:
            self._handle_edge_crossing(crossed)
            return
        self.x = max(0, min(65535, self.x))
        self.y = max(0, min(65535, self.y))
        self._send_mouse(WM_MOUSEMOVE, 0)

    def _crossed_edge(self) -> str:
        if self.x <= 0:
            return "left"
        if self.x >= 65535:
            return "right"
        if self.y <= 0:
            return "top"
        if self.y >= 65535:
            return "bottom"
        return ""

    def _handle_edge_crossing(self, edge: str) -> None:
        destination = neighbour(
            self.config.machine_matrix,
            self.config.two_row,
            self.active_remote_name,
            edge,
            wrap=bool(self.config.other_options.get("wrap_mouse")),
        )
        if destination.casefold() == self.config.machine_name.casefold():
            self.release_local()
            return
        if destination and self._peer_id(destination):
            self._switch_active_target(destination, edge=edge)
            return
        # No connected neighbour occupies this edge, so keep the pointer on
        # the active remote instead of accidentally returning to Linux.
        self.x = max(0, min(65535, self.x))
        self.y = max(0, min(65535, self.y))
        self._send_mouse(WM_MOUSEMOVE, 0)

    def _switch_active_target(
        self,
        machine_name: str,
        *,
        edge: str = "",
        position: tuple[int, int] | None = None,
    ) -> None:
        previous_id = self._peer_id(self.active_remote_name)
        if previous_id:
            hide = Packet()
            hide.type = PackageType.HIDE_MOUSE
            hide.dest = previous_id
            self.send_packet(hide)
        self.active_remote_name = machine_name
        if position is not None:
            self.x, self.y = position
        elif edge == "right":
            self.x = EDGE_ENTRY_OFFSET
        elif edge == "left":
            self.x = 65535 - EDGE_ENTRY_OFFSET
        elif edge == "bottom":
            self.y = EDGE_ENTRY_OFFSET
        elif edge == "top":
            self.y = 65535 - EDGE_ENTRY_OFFSET
        else:
            self.x = self.y = 32768
        self._control_changed(self._peer_id(machine_name))
        self._send_mouse(WM_MOUSEMOVE, 0)
        self.status_callback(f"Controlling {machine_name}")

    def _send_button(self, code: int, pressed: bool) -> None:
        event = BUTTON_TO_WM.get((code, pressed))
        if event:
            self._send_mouse(event, 2 if code == 276 else 1 if code == 275 else 0)

    def _send_scroll(self, dx: float, dy: float, *, discrete: bool) -> None:
        scale = 1 if discrete else 120
        if dy:
            self._send_mouse(WM_MOUSEWHEEL, int(-dy * scale))
        if dx:
            self._send_mouse(WM_MOUSEHWHEEL, int(dx * scale))

    def _send_mouse(self, event: int, wheel: int) -> None:
        peer = self._peer_id(self.active_remote_name)
        if not peer:
            return
        packet = Packet()
        packet.type = PackageType.MOUSE
        packet.dest = peer
        packet.mouse = (self.x, self.y, wheel, event)
        self.send_packet(packet)

    def inject_keyboard(self, vk: int, flags: int) -> None:
        mapped = windows_to_evdev(vk, flags)
        if not mapped:
            LOGGER.debug("unmapped Windows VK: %#x", vk)
            return
        code, pressed = mapped
        try:
            self.bridge.command(
                "inject_key",
                keycode=code,
                state="pressed" if pressed else "released",
            )
        except ConnectionError as exc:
            LOGGER.warning("keyboard injection unavailable: %s", exc)

    def inject_mouse(
        self,
        x: int,
        y: int,
        wheel: int,
        event: int,
        *,
        source_id: int = ID_NONE,
    ) -> None:
        try:
            if event == WM_MOUSEMOVE:
                self.bridge.command(
                    "inject_pointer_absolute",
                    x=_mwb_to_eis_coordinate(x, self.inject_x, self.inject_width),
                    y=_mwb_to_eis_coordinate(y, self.inject_y, self.inject_height),
                )
                self._push_controlled_desktop_edge(x, y)
                self._route_controlled_edge(x, y, source_id)
            elif event in (WM_MOUSEWHEEL, WM_MOUSEHWHEEL):
                self.bridge.command(
                    "inject_scroll",
                    dx=(wheel if event == WM_MOUSEHWHEEL else 0),
                    dy=(-wheel if event == WM_MOUSEWHEEL else 0),
                    discrete=True,
                )
            else:
                if event in (WM_XBUTTONDOWN, WM_XBUTTONUP):
                    button = (276 if wheel == 2 else 275, event == WM_XBUTTONDOWN)
                else:
                    reverse = {value: key for key, value in BUTTON_TO_WM.items()}
                    button = reverse.get(event)
                if button:
                    self.bridge.command(
                        "inject_button",
                        button=button[0],
                        state="pressed" if button[1] else "released",
                    )
        except ConnectionError as exc:
            LOGGER.warning("pointer injection unavailable: %s", exc)

    def _push_controlled_desktop_edge(self, x: int, y: int) -> None:
        """Apply relative pressure so GNOME reveals an auto-hidden edge UI.

        An absolute EIS event can reach the final pixel but cannot move beyond
        it.  GNOME Shell's pressure barriers (including Ubuntu Dock) require
        that outward relative motion.  Never push an edge occupied by another
        matrix computer because that edge belongs to machine switching.
        """

        dx = dy = 0.0
        if x <= CONTROLLED_EDGE_THRESHOLD:
            if not self._edge_has_matrix_neighbour("left"):
                dx = -EDGE_PRESSURE_DISTANCE
        elif x >= 65535 - CONTROLLED_EDGE_THRESHOLD:
            if not self._edge_has_matrix_neighbour("right"):
                dx = EDGE_PRESSURE_DISTANCE
        if y <= CONTROLLED_EDGE_THRESHOLD:
            if not self._edge_has_matrix_neighbour("top"):
                dy = -EDGE_PRESSURE_DISTANCE
        elif y >= 65535 - CONTROLLED_EDGE_THRESHOLD:
            if not self._edge_has_matrix_neighbour("bottom"):
                dy = EDGE_PRESSURE_DISTANCE
        if not dx and not dy:
            return
        try:
            self.bridge.command("inject_pointer_motion", dx=dx, dy=dy)
        except ConnectionError as exc:
            # Absolute injection and reverse edge routing must continue on a
            # portal backend that exposes no relative pointer device.
            LOGGER.debug("desktop edge pressure unavailable: %s", exc)

    def _edge_has_matrix_neighbour(self, edge: str) -> bool:
        return bool(
            neighbour(
                self.config.machine_matrix,
                self.config.two_row,
                self.config.machine_name,
                edge,
                wrap=bool(self.config.other_options.get("wrap_mouse")),
            )
        )

    def _route_controlled_edge(self, x: int, y: int, source_id: int) -> None:
        """Tell a Windows controller when its pointer leaves the Linux tile."""

        if (
            source_id in (ID_NONE, ID_ALL)
            or self.remote_active
            or not self.config.edge_switching
            or not self._peer_name_lookup(source_id)
        ):
            return
        edge = ""
        if x <= CONTROLLED_EDGE_THRESHOLD:
            edge = "left"
        elif x >= 65535 - CONTROLLED_EDGE_THRESHOLD:
            edge = "right"
        elif y <= CONTROLLED_EDGE_THRESHOLD:
            edge = "top"
        elif y >= 65535 - CONTROLLED_EDGE_THRESHOLD:
            edge = "bottom"
        # Windows places the pointer only two pixels inside a newly controlled
        # computer. That first coordinate is itself inside the edge threshold;
        # arm the edge and require an interior movement before treating a later
        # visit as an exit, otherwise control bounces straight back to Windows.
        if self._controlled_source_id != source_id:
            self._controlled_source_id = source_id
            self._controlled_edge = edge
            return
        if not edge:
            self._controlled_edge = ""
            return
        if self._controlled_edge == edge and self._controlled_source_id == source_id:
            return

        destination = neighbour(
            self.config.machine_matrix,
            self.config.two_row,
            self.config.machine_name,
            edge,
            wrap=bool(self.config.other_options.get("wrap_mouse")),
        )
        destination_id = self._peer_id(destination) if destination else None
        if not destination_id:
            if destination:
                self._wake_peer(destination)
            return

        entry_x, entry_y = x, y
        if edge == "right":
            entry_x = EDGE_ENTRY_OFFSET
        elif edge == "left":
            entry_x = 65535 - EDGE_ENTRY_OFFSET
        elif edge == "bottom":
            entry_y = EDGE_ENTRY_OFFSET
        else:
            entry_y = 65535 - EDGE_ENTRY_OFFSET
        packet = Packet()
        packet.type = PackageType.NEXT_MACHINE
        packet.dest = source_id
        packet.mouse = (entry_x, entry_y, destination_id, 0)
        self.send_packet(packet)
        self._controlled_edge = edge
        self._controlled_source_id = source_id
        self.status_callback(f"Controller switched to {destination}")

    def controlled_pointer_hidden(self, source_id: int) -> None:
        """Reset reverse-routing state when the controller leaves Linux."""

        if source_id in (ID_NONE, ID_ALL, self._controlled_source_id):
            self._controlled_edge = ""
            self._controlled_source_id = ID_NONE

    def follow_next_machine(
        self, machine_id: int, x: int, y: int, source_id: int
    ) -> None:
        """Honor edge routing reported by a Windows controlled machine."""

        if not self.remote_active:
            return
        active_id = self._peer_id(self.active_remote_name)
        if source_id not in (active_id, ID_NONE, ID_ALL):
            return
        if machine_id == self.config.machine_id:
            self.release_local((x, y))
            return
        machine_name = self._peer_name_lookup(machine_id)
        if machine_name and self._peer_id(machine_name):
            self._switch_active_target(machine_name, position=(x, y))
