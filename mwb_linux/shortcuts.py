"""Opt-in GNOME custom keybindings for desktops without the portal API."""

from __future__ import annotations

import ast
import logging
import shutil
import subprocess

from .config import HOTKEY_DISABLED, Config

LOGGER = logging.getLogger(__name__)

BASE = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/"
COMMAND = "powertoys-mouse-without-borders"

#: Accelerators used for "Switch between machines, Ctrl+Alt+:". Each entry
#: selects the corresponding one-based slot in the settings form's four-machine
#: matrix, including the slot occupied by this Linux computer.
SWITCH_KEYS = {
    "fkeys": tuple(f"<Control><Alt>F{slot}" for slot in range(1, 5)),
    "numbers": tuple(f"<Control><Alt>{slot}" for slot in range(1, 5)),
}


def managed_paths() -> tuple[str, ...]:
    """Return every dconf path this module owns, including retired ones."""

    return (
        # Retired single-host bindings remain owned so applying a current
        # configuration removes them from GNOME's active binding list.
        f"{BASE}powertoys-mwb-local/",
        f"{BASE}powertoys-mwb-host/",
        *(f"{BASE}powertoys-mwb-machine-{slot}/" for slot in range(1, 5)),
        f"{BASE}powertoys-mwb-reconnect/",
        f"{BASE}powertoys-mwb-settings/",
        f"{BASE}powertoys-mwb-exit/",
    )


def desired_bindings(config: Config) -> dict[str, tuple[str, str, str]]:
    """Return ``path -> (name, command, accelerator)`` for the saved hotkeys.

    Only actions with a Linux command are registered. Entries set to
    ``Disable`` and hotkeys with no Linux equivalent are left out, so the
    surrounding code removes any binding this module previously wrote.
    """

    bindings: dict[str, tuple[str, str, str]] = {}
    switch_keys = SWITCH_KEYS.get(config.switch_hotkey)
    if switch_keys:
        for slot, accelerator in enumerate(switch_keys, start=1):
            machine = config.machine_matrix[slot - 1]
            target = machine or f"empty slot {slot}"
            bindings[f"{BASE}powertoys-mwb-machine-{slot}/"] = (
                f"Mouse Without Borders: switch to {target}",
                f"{COMMAND} switch-machine {slot}",
                accelerator,
            )
    reconnect = config.hotkeys.get("reconnect", HOTKEY_DISABLED)
    if reconnect != HOTKEY_DISABLED:
        bindings[f"{BASE}powertoys-mwb-reconnect/"] = (
            "Mouse Without Borders: reconnect",
            f"{COMMAND} reconnect",
            f"<Control><Alt>{reconnect.lower()}",
        )
    settings = config.hotkeys.get("settings", HOTKEY_DISABLED)
    if settings != HOTKEY_DISABLED:
        bindings[f"{BASE}powertoys-mwb-settings/"] = (
            "Mouse Without Borders: show settings",
            f"{COMMAND} ui",
            f"<Control><Alt>{settings.lower()}",
        )
    exit_key = config.hotkeys.get("exit", HOTKEY_DISABLED)
    if exit_key != HOTKEY_DISABLED:
        bindings[f"{BASE}powertoys-mwb-exit/"] = (
            "Mouse Without Borders: exit",
            f"{COMMAND} quit",
            f"<Control><Alt><Shift>{exit_key.lower()}",
        )
    return bindings


def _gsettings(*arguments: str) -> str:
    executable = shutil.which("gsettings")
    if not executable:
        raise RuntimeError("gsettings is unavailable on this desktop")
    result = subprocess.run(
        [executable, *arguments],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=5,
    )
    return result.stdout.strip()


def _current_paths() -> list[str]:
    output = _gsettings(
        "get", "org.gnome.settings-daemon.plugins.media-keys", "custom-keybindings"
    )
    value = ast.literal_eval(output.removeprefix("@as "))
    return [str(item) for item in value]


def _format_paths(paths: list[str]) -> str:
    return "[" + ", ".join(repr(path) for path in paths) + "]"


def merge_paths(current: list[str], desired: dict[str, object]) -> list[str]:
    """Add the wanted paths and drop retired ones, preserving other bindings."""

    owned = set(managed_paths())
    paths = [path for path in current if path not in owned or path in desired]
    for path in desired:
        if path not in paths:
            paths.append(path)
    return paths


def apply_gnome_shortcuts(config: Config) -> None:
    bindings = desired_bindings(config)
    paths = _current_paths()
    for path, (name, command, accelerator) in bindings.items():
        schema = f"org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:{path}"
        _gsettings("set", schema, "name", name)
        _gsettings("set", schema, "command", command)
        _gsettings("set", schema, "binding", accelerator)
    _gsettings(
        "set",
        "org.gnome.settings-daemon.plugins.media-keys",
        "custom-keybindings",
        _format_paths(merge_paths(paths, bindings)),
    )
