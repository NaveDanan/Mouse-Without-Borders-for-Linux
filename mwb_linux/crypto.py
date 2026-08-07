"""AES stream profiles used by released Mouse Without Borders builds."""

from __future__ import annotations

import hashlib
import os
import socket
import threading
from dataclasses import dataclass
from enum import Enum

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

AES_BLOCK_SIZE = 16
LEGACY_INITIAL_IV_TEXT = str((1 << 64) - 1)


class CryptoProfile(str, Enum):
    """Known incompatible Windows encryption generations."""

    STANDALONE_50K = "standalone-50k"
    LEGACY_50K = "legacy-50k"
    RANDOM_50K = "random-50k"
    RANDOM_100K = "random-100k"

    @classmethod
    def connection_order(cls) -> tuple["CryptoProfile", ...]:
        # The final standalone release uses SHA-1. PowerToys moved to SHA-512
        # before adding per-connection random headers.
        return (
            cls.STANDALONE_50K,
            cls.LEGACY_50K,
            cls.RANDOM_50K,
            cls.RANDOM_100K,
        )


@dataclass(frozen=True, slots=True)
class ProfileParameters:
    iterations: int
    random_header: bool
    hash_name: str


PARAMETERS = {
    CryptoProfile.STANDALONE_50K: ProfileParameters(50_000, False, "sha1"),
    CryptoProfile.LEGACY_50K: ProfileParameters(50_000, False, "sha512"),
    CryptoProfile.RANDOM_50K: ProfileParameters(50_000, True, "sha512"),
    CryptoProfile.RANDOM_100K: ProfileParameters(100_000, True, "sha512"),
}


def _legacy_salt() -> bytes:
    return LEGACY_INITIAL_IV_TEXT.encode("utf-16le")


def _legacy_iv() -> bytes:
    return LEGACY_INITIAL_IV_TEXT.encode("ascii")[:AES_BLOCK_SIZE].ljust(
        AES_BLOCK_SIZE, b" "
    )


def derive_key(
    secret: str, salt: bytes, iterations: int, hash_name: str = "sha512"
) -> bytes:
    return hashlib.pbkdf2_hmac(
        hash_name, secret.encode("utf-8"), salt, iterations, dklen=32
    )


class EncryptedSocket:
    """Independent CBC reader/writer streams layered over one TCP socket.

    This mirrors the two lazily created .NET CryptoStreams.  Every write is
    block-aligned in the MWB protocol, so zero-padding never adds a terminal
    block and the CBC contexts can remain open for the lifetime of the socket.
    """

    def __init__(self, sock: socket.socket, secret: str, profile: CryptoProfile):
        self.socket = sock
        self.secret = secret
        self.profile = profile
        self._send_lock = threading.Lock()
        self._recv_lock = threading.Lock()
        self._encryptor = None
        self._decryptor = None

    def _recv_exact_raw(self, size: int) -> bytes:
        chunks = bytearray()
        while len(chunks) < size:
            block = self.socket.recv(size - len(chunks))
            if not block:
                raise EOFError("peer closed the encrypted stream")
            chunks.extend(block)
        return bytes(chunks)

    def _init_writer(self) -> None:
        if self._encryptor is not None:
            return
        params = PARAMETERS[self.profile]
        if params.random_header:
            salt = os.urandom(AES_BLOCK_SIZE)
            iv = os.urandom(AES_BLOCK_SIZE)
            self.socket.sendall(salt + iv)
        else:
            salt = _legacy_salt()
            iv = _legacy_iv()
        key = derive_key(self.secret, salt, params.iterations, params.hash_name)
        self._encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
        self.socket.sendall(self._encryptor.update(os.urandom(AES_BLOCK_SIZE)))

    def _init_reader(self) -> None:
        if self._decryptor is not None:
            return
        params = PARAMETERS[self.profile]
        if params.random_header:
            header = self._recv_exact_raw(AES_BLOCK_SIZE * 2)
            salt, iv = header[:AES_BLOCK_SIZE], header[AES_BLOCK_SIZE:]
        else:
            salt = _legacy_salt()
            iv = _legacy_iv()
        key = derive_key(self.secret, salt, params.iterations, params.hash_name)
        self._decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
        self._decryptor.update(self._recv_exact_raw(AES_BLOCK_SIZE))

    def send(self, plaintext: bytes) -> None:
        if not plaintext or len(plaintext) % AES_BLOCK_SIZE:
            raise ValueError("encrypted writes must be non-empty and block aligned")
        with self._send_lock:
            self._init_writer()
            self.socket.sendall(self._encryptor.update(plaintext))

    def receive(self, size: int) -> bytes:
        if size <= 0 or size % AES_BLOCK_SIZE:
            raise ValueError("encrypted reads must be positive and block aligned")
        with self._recv_lock:
            self._init_reader()
            return self._decryptor.update(self._recv_exact_raw(size))

    def close(self) -> None:
        try:
            self.socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.socket.close()
