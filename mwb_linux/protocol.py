"""Mouse Without Borders wire-format compatibility.

The Windows implementation overlays several structures on a 64-byte little-
endian buffer.  Bytes 1..3 of the type field are repurposed for a checksum and
the high 16 bits of the shared-key magic number while on the wire.
"""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass, field
from enum import IntEnum

PACKAGE_SIZE = 32
PACKAGE_SIZE_EX = 64
CLIPBOARD_DATA_SIZE = 48
CLIPBOARD_DATA_OFFSET = PACKAGE_SIZE_EX - CLIPBOARD_DATA_SIZE
ID_NONE = 0
ID_ALL = 255


class PackageType(IntEnum):
    INVALID = 0xFF
    ERROR = 0xFE
    HI = 2
    HELLO = 3
    BYE_BYE = 4
    HEARTBEAT = 20
    AWAKE = 21
    HIDE_MOUSE = 50
    HEARTBEAT_EX = 51
    HEARTBEAT_EX_L2 = 52
    HEARTBEAT_EX_L3 = 53
    CLIPBOARD = 69
    CLIPBOARD_DRAG_DROP = 70
    CLIPBOARD_DRAG_DROP_END = 71
    EXPLORER_DRAG_DROP = 72
    CLIPBOARD_CAPTURE = 73
    CAPTURE_SCREEN_COMMAND = 74
    CLIPBOARD_DRAG_DROP_OPERATION = 75
    CLIPBOARD_DATA_END = 76
    MACHINE_SWITCHED = 77
    CLIPBOARD_ASK = 78
    CLIPBOARD_PUSH = 79
    NEXT_MACHINE = 121
    KEYBOARD = 122
    MOUSE = 123
    CLIPBOARD_TEXT = 124
    CLIPBOARD_IMAGE = 125
    HANDSHAKE = 126
    HANDSHAKE_ACK = 127
    MATRIX = 128


BIG_TYPES = {
    PackageType.HELLO,
    PackageType.AWAKE,
    PackageType.HEARTBEAT,
    PackageType.HEARTBEAT_EX,
    PackageType.HANDSHAKE,
    PackageType.HANDSHAKE_ACK,
    PackageType.CLIPBOARD_PUSH,
    PackageType.CLIPBOARD,
    PackageType.CLIPBOARD_ASK,
    PackageType.CLIPBOARD_IMAGE,
    PackageType.CLIPBOARD_TEXT,
    PackageType.CLIPBOARD_DATA_END,
}


class ProtocolError(ValueError):
    """Raised when a packet fails framing validation."""


def is_big_type(package_type: int) -> bool:
    """Return the Windows DATA.IsBigPackage result for a numeric type."""

    base_type = package_type & 0xFF
    try:
        if PackageType(base_type) in BIG_TYPES:
            return True
    except ValueError:
        pass
    return bool(base_type & int(PackageType.MATRIX))


def magic_number(secret: str, hash_name: str = "sha512") -> int:
    """Reproduce Encryption.Get24BitHash, including its overlapping shift.

    The last standalone release used SHA-256; PowerToys later changed the
    repeated hash to SHA-512 without changing the packet field layout.
    """

    if not secret:
        return 0
    key_bytes = bytes((ord(ch) & 0xFF) for ch in secret[:PACKAGE_SIZE])
    key_bytes = key_bytes.ljust(PACKAGE_SIZE, b"\0")
    digest = hashlib.new(hash_name, key_bytes).digest()
    for _ in range(50_000):
        digest = hashlib.new(hash_name, digest).digest()
    return (
        (digest[0] << 23)
        + (digest[1] << 16)
        + (digest[-1] << 8)
        + digest[2]
    ) & 0xFFFFFFFF


@dataclass(slots=True)
class Packet:
    """Mutable view of the Windows 64-byte DATA union."""

    raw: bytearray = field(default_factory=lambda: bytearray(PACKAGE_SIZE_EX))
    complete_clipboard: bytes | None = None

    @classmethod
    def random(cls, data: bytes) -> "Packet":
        if len(data) != PACKAGE_SIZE_EX:
            raise ValueError("random packet seed must be 64 bytes")
        return cls(bytearray(data))

    @classmethod
    def decode(cls, data: bytes, magic: int) -> "Packet":
        if len(data) not in (PACKAGE_SIZE, PACKAGE_SIZE_EX):
            raise ProtocolError(f"invalid packet length: {len(data)}")
        raw = bytearray(data.ljust(PACKAGE_SIZE_EX, b"\0"))
        expected_magic = magic & 0xFFFF0000
        actual_magic = (raw[3] << 24) | (raw[2] << 16)
        if actual_magic != expected_magic:
            raise ProtocolError("shared-key magic mismatch")
        if raw[1] != (sum(raw[2:PACKAGE_SIZE]) & 0xFF):
            raise ProtocolError("packet checksum mismatch")
        raw[1:4] = b"\0\0\0"
        packet = cls(raw)
        expected = PACKAGE_SIZE_EX if packet.is_big else PACKAGE_SIZE
        if len(data) != expected:
            raise ProtocolError(
                f"type {packet.type} requires {expected} bytes, got {len(data)}"
            )
        return packet

    @property
    def type(self) -> int:
        return struct.unpack_from("<I", self.raw, 0)[0]

    @type.setter
    def type(self, value: int | PackageType) -> None:
        struct.pack_into("<I", self.raw, 0, int(value))

    @property
    def is_big(self) -> bool:
        return is_big_type(self.type)

    @property
    def packet_id(self) -> int:
        return struct.unpack_from("<i", self.raw, 4)[0]

    @packet_id.setter
    def packet_id(self, value: int) -> None:
        struct.pack_into("<i", self.raw, 4, value)

    @property
    def src(self) -> int:
        return struct.unpack_from("<I", self.raw, 8)[0]

    @src.setter
    def src(self, value: int) -> None:
        struct.pack_into("<I", self.raw, 8, value & 0xFFFFFFFF)

    @property
    def dest(self) -> int:
        return struct.unpack_from("<I", self.raw, 12)[0]

    @dest.setter
    def dest(self, value: int) -> None:
        struct.pack_into("<I", self.raw, 12, value & 0xFFFFFFFF)

    @property
    def timestamp(self) -> int:
        return struct.unpack_from("<q", self.raw, 16)[0]

    @timestamp.setter
    def timestamp(self, value: int) -> None:
        struct.pack_into("<q", self.raw, 16, value)

    @property
    def keyboard(self) -> tuple[int, int]:
        return struct.unpack_from("<ii", self.raw, 24)

    @keyboard.setter
    def keyboard(self, value: tuple[int, int]) -> None:
        struct.pack_into("<ii", self.raw, 24, *value)

    @property
    def mouse(self) -> tuple[int, int, int, int]:
        return struct.unpack_from("<iiii", self.raw, 16)

    @mouse.setter
    def mouse(self, value: tuple[int, int, int, int]) -> None:
        struct.pack_into("<iiii", self.raw, 16, *value)

    @property
    def machine_words(self) -> tuple[int, int, int, int]:
        return struct.unpack_from("<IIII", self.raw, 16)

    @machine_words.setter
    def machine_words(self, value: tuple[int, int, int, int]) -> None:
        struct.pack_into("<IIII", self.raw, 16, *value)

    def complement_machine_words(self) -> None:
        self.machine_words = tuple((~word) & 0xFFFFFFFF for word in self.machine_words)

    @property
    def machine_name(self) -> str:
        return bytes(self.raw[32:64]).decode("ascii", errors="replace").rstrip(" \0")

    @machine_name.setter
    def machine_name(self, value: str) -> None:
        encoded = value.encode("ascii", errors="replace")[:32]
        self.raw[32:64] = encoded.ljust(32, b" ")

    @property
    def post_action(self) -> int:
        return struct.unpack_from("<I", self.raw, 16)[0]

    @post_action.setter
    def post_action(self, value: int) -> None:
        struct.pack_into("<I", self.raw, 16, value)

    @property
    def clipboard_payload(self) -> bytes:
        return bytes(self.raw[CLIPBOARD_DATA_OFFSET:PACKAGE_SIZE_EX])

    @clipboard_payload.setter
    def clipboard_payload(self, value: bytes) -> None:
        if len(value) > CLIPBOARD_DATA_SIZE:
            raise ValueError("clipboard payload is larger than 48 bytes")
        self.raw[CLIPBOARD_DATA_OFFSET:PACKAGE_SIZE_EX] = value.ljust(
            CLIPBOARD_DATA_SIZE, b"\0"
        )

    def wire_bytes(self, magic: int) -> bytes:
        data = bytearray(self.raw[: PACKAGE_SIZE_EX if self.is_big else PACKAGE_SIZE])
        data[3] = (magic >> 24) & 0xFF
        data[2] = (magic >> 16) & 0xFF
        data[1] = sum(data[2:PACKAGE_SIZE]) & 0xFF
        return bytes(data)


def packet_type_from_first_block(first_block: bytes, magic: int) -> tuple[int, bool]:
    """Validate the first 32 bytes enough to determine the total packet size."""

    if len(first_block) != PACKAGE_SIZE:
        raise ProtocolError("short first packet block")
    expected_magic = magic & 0xFFFF0000
    actual_magic = (first_block[3] << 24) | (first_block[2] << 16)
    if actual_magic != expected_magic:
        raise ProtocolError("shared-key magic mismatch")
    if first_block[1] != (sum(first_block[2:PACKAGE_SIZE]) & 0xFF):
        raise ProtocolError("packet checksum mismatch")
    package_type = first_block[0]
    return package_type, is_big_type(package_type)
