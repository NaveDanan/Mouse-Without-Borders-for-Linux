"""Cross-desktop clipboard adapter and MWB text/image framing."""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
import threading
import time
import zlib
from collections.abc import Callable

from .protocol import ID_ALL, Packet, PackageType

LOGGER = logging.getLogger(__name__)
TEXT_SEPARATOR = "{4CFF57F7-BEDD-43d5-AE8F-27A61E886F2F}"
MAX_CLIPBOARD_BYTES = 20 * 1024 * 1024
CHUNK_SIZE = 48
DESKTOP_ACTIVATION_VARIABLES = (
    "DESKTOP_STARTUP_ID",
    "GIO_LAUNCHED_DESKTOP_FILE",
    "GIO_LAUNCHED_DESKTOP_FILE_PID",
    "XDG_ACTIVATION_TOKEN",
)


class ClipboardError(RuntimeError):
    pass


def background_environment() -> dict[str, str]:
    """Keep display access but prevent CLI helpers from flashing in the dock."""

    environment = os.environ.copy()
    for variable in DESKTOP_ACTIVATION_VARIABLES:
        environment.pop(variable, None)
    return environment


def encode_text(text: str) -> bytes:
    """Encode text exactly as the Windows clipboard helper does."""

    plain = f"TXT{text}{TEXT_SEPARATOR}".encode("utf-16le")
    compressor = zlib.compressobj(level=6, wbits=-zlib.MAX_WBITS)
    return compressor.compress(plain) + compressor.flush()


def _inflate_bounded(data: bytes, limit: int = MAX_CLIPBOARD_BYTES) -> bytes:
    last_error: Exception | None = None
    for window_bits in (-zlib.MAX_WBITS, zlib.MAX_WBITS):
        try:
            inflater = zlib.decompressobj(window_bits)
            output = inflater.decompress(data, limit + 1)
            if len(output) > limit or inflater.unconsumed_tail:
                raise ClipboardError("clipboard decompression exceeds safety limit")
            output += inflater.flush(limit + 1 - len(output))
            if len(output) > limit:
                raise ClipboardError("clipboard decompression exceeds safety limit")
            return output
        except zlib.error as exc:
            last_error = exc
    raise ClipboardError(f"invalid compressed clipboard text: {last_error}")


def decode_text(data: bytes) -> str:
    """Decode raw/zlib Deflate and return the preferred TXT representation."""

    decoded = _inflate_bounded(data).decode("utf-16le", errors="strict").rstrip("\0")
    for item in decoded.split(TEXT_SEPARATOR):
        if item.startswith("TXT"):
            return item[3:]
    # Older peers occasionally sent a bare Unicode string.
    return decoded[3:] if decoded.startswith(("RTF", "HTM")) else decoded


def trim_png(data: bytes) -> bytes:
    marker = data.rfind(b"IEND")
    return data[: marker + 8] if marker >= 4 else data.rstrip(b"\0")


class CommandClipboard:
    """Clipboard access using standard Wayland/X11 command-line clients."""

    def __init__(self) -> None:
        self.wayland = bool(os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-paste"))
        self.x11 = bool(os.environ.get("DISPLAY") and shutil.which("xclip"))
        if not self.wayland and not self.x11:
            raise ClipboardError("install wl-clipboard (Wayland) or xclip (X11)")

    @property
    def use_x11(self) -> bool:
        """Prefer XWayland because GNOME lacks background data-control.

        Without ``zwlr-data-control``, wl-clipboard creates a tiny focused
        toplevel and waits for a selection offer. Polling that fallback both
        hangs and flashes a dock item. Mutter's XWayland clipboard bridge is
        background-safe and mirrors native Wayland selections, so use xclip
        whenever DISPLAY is available. Native wl-clipboard remains the
        fallback on compositors that expose no X11 display.
        """

        return self.x11

    def _run(self, command: list[str], data: bytes | None = None) -> bytes:
        try:
            result = subprocess.run(
                command,
                input=data,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=True,
                timeout=3,
                env=background_environment(),
            )
            return result.stdout
        except (subprocess.SubprocessError, OSError) as exc:
            raise ClipboardError(str(exc)) from exc

    def available_types(self) -> tuple[str, ...]:
        if not self.use_x11:
            output = self._run(["wl-paste", "--list-types"])
            return tuple(output.decode(errors="replace").splitlines())
        output = self._run(
            ["xclip", "-selection", "clipboard", "-t", "TARGETS", "-o"]
        )
        return tuple(output.decode(errors="replace").splitlines())

    def get_text(self) -> str | None:
        try:
            if self.use_x11:
                output = self._run(
                    ["xclip", "-selection", "clipboard", "-t", "UTF8_STRING", "-o"]
                )
            else:
                output = self._run(
                    ["wl-paste", "--no-newline", "--type", "text/plain;charset=utf-8"]
                )
            return output.decode("utf-8")
        except (ClipboardError, UnicodeDecodeError):
            return None

    def get_image(self) -> bytes | None:
        try:
            command = (
                ["xclip", "-selection", "clipboard", "-t", "image/png", "-o"]
                if self.use_x11
                else ["wl-paste", "--no-newline", "--type", "image/png"]
            )
            output = self._run(command)
            return output or None
        except ClipboardError:
            return None

    def set_text(self, text: str) -> None:
        data = text.encode("utf-8")
        command = (
            ["xclip", "-selection", "clipboard", "-t", "UTF8_STRING", "-i"]
            if self.use_x11
            else ["wl-copy", "--type", "text/plain;charset=utf-8"]
        )
        self._run(command, data)

    def set_image(self, image: bytes) -> None:
        command = (
            ["xclip", "-selection", "clipboard", "-t", "image/png", "-i"]
            if self.use_x11
            else ["wl-copy", "--type", "image/png"]
        )
        self._run(command, image)


class ClipboardManager:
    """Poll local clipboard, transmit changes, and prevent echo loops."""

    def __init__(
        self,
        send_packet: Callable[[Packet], None],
        *,
        share_images: bool = True,
        adapter: CommandClipboard | None = None,
    ) -> None:
        self.send_packet = send_packet
        self.share_images = share_images
        self.adapter = adapter
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_digest: bytes | None = None
        self._remote_digest: bytes | None = None
        self._remote_until = 0.0

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        if self.adapter is None:
            self.adapter = CommandClipboard()
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._watch_loop, name="mwb-clipboard", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread and thread is not threading.current_thread():
            # One helper call can block for up to three seconds. Do not return
            # while the old watcher could still publish to a newly replaced
            # connection runtime.
            thread.join(timeout=3.5)
        if not thread or not thread.is_alive():
            self._thread = None

    def _watch_loop(self) -> None:
        while not self._stop.wait(0.75):
            try:
                types = self.adapter.available_types()
                if self._stop.is_set():
                    break
                if any("text" in item.lower() or item == "UTF8_STRING" for item in types):
                    text = self.adapter.get_text()
                    if self._stop.is_set():
                        break
                    if text is not None:
                        self._maybe_send(text.encode("utf-8"), image=False)
                        continue
                if self.share_images and any("image/png" in item.lower() for item in types):
                    image = self.adapter.get_image()
                    if self._stop.is_set():
                        break
                    if image:
                        self._maybe_send(image, image=True)
            except ClipboardError as exc:
                LOGGER.debug("clipboard poll failed: %s", exc)

    def _maybe_send(self, content: bytes, *, image: bool) -> None:
        if self._stop.is_set():
            return
        digest = hashlib.sha256((b"I" if image else b"T") + content).digest()
        if digest == self._last_digest:
            return
        self._last_digest = digest
        if digest == self._remote_digest and time.monotonic() < self._remote_until:
            return
        payload = content if image else encode_text(content.decode("utf-8"))
        if len(payload) > 1024 * 1024:
            LOGGER.warning("clipboard item exceeds instant-transfer limit; not sent")
            return
        for offset in range(0, len(payload), CHUNK_SIZE):
            if self._stop.is_set():
                return
            packet = Packet()
            packet.type = (
                PackageType.CLIPBOARD_IMAGE if image else PackageType.CLIPBOARD_TEXT
            )
            packet.dest = ID_ALL
            packet.clipboard_payload = payload[offset : offset + CHUNK_SIZE]
            self.send_packet(packet)
        end = Packet()
        if self._stop.is_set():
            return
        end.type = PackageType.CLIPBOARD_DATA_END
        end.dest = ID_ALL
        self.send_packet(end)

    def receive(self, data: bytes, *, image: bool) -> None:
        try:
            if image:
                content = trim_png(data)
                self.adapter.set_image(content)
                digest_content = content
            else:
                text = decode_text(data)
                self.adapter.set_text(text)
                digest_content = text.encode("utf-8")
            digest = hashlib.sha256((b"I" if image else b"T") + digest_content).digest()
            self._remote_digest = self._last_digest = digest
            self._remote_until = time.monotonic() + 2.0
        except (ClipboardError, UnicodeError) as exc:
            LOGGER.warning("rejected remote clipboard data: %s", exc)
