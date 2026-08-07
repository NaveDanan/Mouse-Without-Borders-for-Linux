"""User configuration with restrictive secret storage permissions."""

from __future__ import annotations

import ipaddress
import json
import os
import secrets
import socket
import string
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

SECRET_LENGTH = 16
MAX_MACHINES = 4

HOST_POSITIONS = ("left", "right", "top", "bottom")
CRYPTO_PROFILES = (
    "auto",
    "standalone-50k",
    "legacy-50k",
    "random-50k",
    "random-100k",
)
SWITCH_HOTKEY_MODES = ("fkeys", "numbers", "disabled")

#: Windows parity toggles from the settings form. Entries without a Linux
#: implementation are stored so the form round-trips, and are read by the
#: features that support them as those land.
OTHER_OPTION_DEFAULTS: dict[str, bool] = {
    "wrap_mouse": False,
    "hide_logon_logo": True,
    "hide_mouse_at_edge": True,
    "draw_mouse_cursor": True,
    "validate_remote_ip": True,
    "same_subnet_only": False,
    "disable_cad": True,
    "block_screen_saver": True,
    "move_mouse_relatively": False,
    "block_mouse_at_corners": False,
    "use_key_mappings": False,
    "show_status_messages": False,
}

#: Single-letter accelerators shown on the Other Options tab. ``Disable``
#: turns the entry off, matching the Windows form.
HOTKEY_DEFAULTS: dict[str, str] = {
    "settings": "M",
    "lock_machines": "L",
    "reconnect": "R",
    "screen_capture": "S",
    "exit": "Q",
    "all_pc_mode": "Disable",
    "toggle_easy_mouse": "Disable",
}

HOTKEY_DISABLED = "Disable"


def default_config_path() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "powertoys-mwb-linux" / "config.json"


def default_runtime_socket() -> Path:
    base = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
    return base / "powertoys-mwb-linux.sock"


def generate_secret() -> str:
    """Return a 16-character key in the alphabet the Windows form produces."""

    groups = (string.ascii_lowercase, string.ascii_uppercase, string.digits, "!@#$%^&*()_+-=[]{}|;:,.<>?/")
    alphabet = "".join(groups)
    characters = [secrets.choice(group) for group in groups]
    characters += [secrets.choice(alphabet) for _ in range(SECRET_LENGTH - len(groups))]
    for index in range(len(characters) - 1, 0, -1):
        swap = secrets.randbelow(index + 1)
        characters[index], characters[swap] = characters[swap], characters[index]
    return "".join(characters)


def parse_ip_mappings(text: str) -> dict[str, str]:
    """Parse ``machine-name address`` lines into a name-keyed mapping.

    Blank lines and ``#`` comments are ignored. Later duplicates win, matching
    the Windows form. A malformed line raises ``ValueError`` naming the line.
    """

    mappings: dict[str, str] = {}
    for number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) != 2:
            raise ValueError(f"line {number}: expected 'machine-name ip-address'")
        name, address = parts
        try:
            ipaddress.ip_address(address)
        except ValueError as exc:
            raise ValueError(f"line {number}: {address} is not an IP address") from exc
        mappings[name.casefold()] = address
    return mappings


def format_ip_mappings(mappings: dict[str, str]) -> str:
    return "\n".join(f"{name} {address}" for name, address in mappings.items())


def host_after_name_edit(
    configured_host: str, previous_host_name: str, edited_host_name: str
) -> str:
    """Keep an explicit address when the UI only round-trips a display name.

    Editing the machine card to a genuinely different name intentionally
    switches back to name/mapping resolution.  Merely pressing Apply must not
    replace an explicit IP address with the unchanged display name.
    """

    configured_host = configured_host.strip()
    previous_host_name = previous_host_name.strip()
    edited_host_name = edited_host_name.strip()
    if edited_host_name.casefold() == previous_host_name.casefold():
        return configured_host or edited_host_name
    return edited_host_name


@dataclass(frozen=True, slots=True)
class HostTarget:
    """One remote computer from the four-machine Windows matrix."""

    name: str
    address: str


@dataclass(slots=True)
class Config:
    host: str = ""
    host_name: str = ""
    secret: str = ""
    port: int = 15100
    machine_name: str = ""
    machine_id: int = 0
    host_position: str = "right"
    host_zone: list[int] = field(default_factory=list)
    share_clipboard: bool = True
    share_images: bool = True
    edge_switching: bool = True
    switch_hotkey: str = "disabled"
    auto_connect: bool = True
    crypto_profile: str = "auto"
    two_row: bool = False
    ip_mappings: str = ""
    other_options: dict[str, bool] = field(default_factory=dict)
    hotkeys: dict[str, str] = field(default_factory=dict)
    capture_restore_token: str = ""
    inject_restore_token: str = ""
    # ``host`` and ``host_name`` remain as a migration/CLI compatibility pair.
    # New settings use the complete Windows-compatible four-machine matrix and
    # a name/address record for each remote computer.
    remote_machines: list[dict[str, str]] = field(default_factory=list)
    machine_matrix: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.machine_name:
            self.machine_name = socket.gethostname().split(".", 1)[0][:32]
        if not self.machine_id:
            self.machine_id = secrets.randbelow(0x7FFFFFFE) + 1
        self.other_options = {**OTHER_OPTION_DEFAULTS, **self.other_options}
        self.hotkeys = {**HOTKEY_DEFAULTS, **self.hotkeys}
        matrix_was_explicit = bool(self.machine_matrix)
        # The legacy two-computer model expressed vertical placement through
        # ``host_position`` alone.  Once migrated to the Windows four-slot
        # matrix, top/bottom occupy separate rows and must carry the two-row
        # flag or Windows interprets all four slots as one horizontal row.
        if not matrix_was_explicit and self.host_position in ("top", "bottom"):
            self.two_row = True
        self.remote_machines = self._normalized_remote_machines()
        self.machine_matrix = self._normalized_machine_matrix(
            seed_remotes=not matrix_was_explicit
        )

    def _normalized_remote_machines(self) -> list[dict[str, str]]:
        remotes: list[dict[str, str]] = []
        for raw in self.remote_machines:
            if not isinstance(raw, dict):
                continue
            address = str(raw.get("address", raw.get("host", ""))).strip()
            name = str(raw.get("name", "")).strip() or address
            if name:
                remotes.append({"name": name, "address": address or name})
        if not remotes:
            address = self.host.strip()
            name = self.host_name.strip() or address
            if name:
                remotes.append({"name": name, "address": address or name})
        return remotes

    def _legacy_matrix(self) -> list[str]:
        remote = self.remote_machines[0]["name"] if self.remote_machines else ""
        if self.host_position == "left":
            return [remote, self.machine_name, "", ""]
        if self.host_position == "top":
            return [remote, "", self.machine_name, ""]
        if self.host_position == "bottom":
            return [self.machine_name, "", remote, ""]
        return [self.machine_name, remote, "", ""]

    def _normalized_machine_matrix(self, *, seed_remotes: bool) -> list[str]:
        if self.machine_matrix:
            matrix = [str(value).strip() for value in self.machine_matrix[:MAX_MACHINES]]
            matrix.extend([""] * (MAX_MACHINES - len(matrix)))
        else:
            matrix = self._legacy_matrix()

        local = self.machine_name.casefold()
        if not any(name.casefold() == local for name in matrix if name):
            try:
                matrix[matrix.index("")] = self.machine_name
            except ValueError:
                matrix[0] = self.machine_name
        for remote in self.remote_machines if seed_remotes else ():
            name = remote["name"]
            if any(value.casefold() == name.casefold() for value in matrix if value):
                continue
            try:
                matrix[matrix.index("")] = name
            except ValueError:
                break
        return matrix

    def validate(self, require_connection: bool = False) -> None:
        if not 1 <= int(self.port) <= 65534:
            raise ValueError("base port must be between 1 and 65534")
        if not self.machine_name.strip():
            raise ValueError("local machine name is required")
        try:
            self.machine_name.encode("ascii")
        except UnicodeEncodeError as exc:
            raise ValueError("local machine name must contain ASCII characters") from exc
        if len(self.machine_name.encode("ascii")) > 32:
            raise ValueError("local machine name must be at most 32 ASCII bytes")
        if self.host_position not in HOST_POSITIONS:
            raise ValueError("host position must be left, right, top, or bottom")
        if self.host_zone and len(self.host_zone) != 4:
            raise ValueError("host monitor must be an x, y, width, height rectangle")
        if self.crypto_profile not in CRYPTO_PROFILES:
            raise ValueError("unsupported crypto profile")
        if self.switch_hotkey not in SWITCH_HOTKEY_MODES:
            raise ValueError("switch hotkey must be fkeys, numbers, or disabled")
        unknown_options = set(self.other_options) - set(OTHER_OPTION_DEFAULTS)
        if unknown_options:
            raise ValueError(f"unknown options: {', '.join(sorted(unknown_options))}")
        unknown_hotkeys = set(self.hotkeys) - set(HOTKEY_DEFAULTS)
        if unknown_hotkeys:
            raise ValueError(f"unknown hotkeys: {', '.join(sorted(unknown_hotkeys))}")
        for name, value in self.hotkeys.items():
            if value != HOTKEY_DISABLED and value not in string.ascii_uppercase:
                raise ValueError(f"hotkey {name} must be a single A-Z letter or Disable")
        parse_ip_mappings(self.ip_mappings)
        if len(self.remote_machines) > MAX_MACHINES - 1:
            raise ValueError("at most three remote computers can be configured")
        remote_names: set[str] = set()
        for remote in self.remote_machines:
            name = remote.get("name", "").strip()
            address = remote.get("address", "").strip()
            if not name or not address:
                raise ValueError("each remote computer requires a name and address")
            try:
                name.encode("ascii")
            except UnicodeEncodeError as exc:
                raise ValueError("remote computer names must contain ASCII characters") from exc
            if len(name.encode("ascii")) > 32:
                raise ValueError("remote computer names must be at most 32 ASCII bytes")
            key = name.casefold()
            if key in remote_names or key == self.machine_name.casefold():
                raise ValueError("computer names in the matrix must be unique")
            remote_names.add(key)
        if len(self.machine_matrix) != MAX_MACHINES:
            raise ValueError("computer matrix must contain exactly four slots")
        matrix_names = [name.strip() for name in self.machine_matrix if name.strip()]
        if len({name.casefold() for name in matrix_names}) != len(matrix_names):
            raise ValueError("computer names in the matrix must be unique")
        if sum(name.casefold() == self.machine_name.casefold() for name in matrix_names) != 1:
            raise ValueError("computer matrix must contain the local computer exactly once")
        for name in matrix_names:
            try:
                name.encode("ascii")
            except UnicodeEncodeError as exc:
                raise ValueError("matrix computer names must contain ASCII characters") from exc
            if len(name.encode("ascii")) > 32:
                raise ValueError("matrix computer names must be at most 32 ASCII bytes")
        if require_connection:
            if not self.resolve_hosts():
                raise ValueError("host address is required")
            if len(self.secret) < SECRET_LENGTH:
                raise ValueError("security key must contain at least 16 characters")

    def resolve_hosts(self) -> list[HostTarget]:
        """Resolve every remote matrix entry, preserving matrix order."""

        mappings = parse_ip_mappings(self.ip_mappings)
        configured = {
            remote["name"].casefold(): remote["address"]
            for remote in self.remote_machines
        }
        targets: list[HostTarget] = []
        seen: set[str] = set()
        for name in self.machine_matrix:
            name = name.strip()
            key = name.casefold()
            if not name or key == self.machine_name.casefold() or key in seen:
                continue
            address = mappings.get(key) or configured.get(key) or name
            targets.append(HostTarget(name=name, address=address))
            seen.add(key)
        return targets

    def resolve_host(self, machine_name: str | None = None) -> str:
        """Return the address to dial, preferring an explicit IP mapping.

        The form collects machine names, so the mapping table is consulted
        first and DNS resolution of the name is the fallback.
        """

        if machine_name is not None:
            key = machine_name.strip().casefold()
            for target in self.resolve_hosts():
                if target.name.casefold() == key:
                    return target.address
            return ""
        targets = self.resolve_hosts()
        if targets:
            return targets[0].address

        mappings = parse_ip_mappings(self.ip_mappings)
        for name in (self.host, self.host_name):
            candidate = name.strip()
            if candidate and candidate.casefold() in mappings:
                return mappings[candidate.casefold()]
        return self.host.strip() or self.host_name.strip()

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        path = path or default_config_path()
        if not path.exists():
            config = cls()
            config.save(path)
            return config
        mode = path.stat().st_mode & 0o777
        if mode & 0o077:
            raise PermissionError(
                f"refusing to load {path}: permissions must be 0600 or stricter"
            )
        data = json.loads(path.read_text(encoding="utf-8"))
        allowed = {item.name for item in fields(cls)}
        return cls(**{key: value for key, value in data.items() if key in allowed})

    def save(self, path: Path | None = None) -> None:
        self.validate()
        path = path or default_config_path()
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        temporary = path.with_suffix(".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                json.dump(asdict(self), output, indent=2, sort_keys=True)
                output.write("\n")
            os.replace(temporary, path)
            os.chmod(path, 0o600)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def public_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["secret"] = "" if not self.secret else "configured"
        return data
