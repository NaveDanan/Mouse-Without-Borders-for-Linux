"""The classic Mouse Without Borders settings form, rebuilt with GTK 4."""

from __future__ import annotations

import logging
import os
import string
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from . import __version__
from .config import (
    CRYPTO_PROFILES,
    HOTKEY_DISABLED,
    Config,
    generate_secret,
    parse_ip_mappings,
)
from .indicator import TopBarIndicator
from .monitors import (
    Monitor,
    is_exterior,
    read_monitors,
)
from .service import control_request
from .updater import (
    UpdateRelease,
    automatic_install_supported,
    check_for_update,
    download_release,
    install_package,
    schedule_relaunch,
)
from .widgets import (
    APP_ID,
    ICON_DIRECTORY,
    Fieldset,
    app_icon,
    check_button,
    dropdown,
    dropdown_value,
    horizontal,
    label,
    link_button,
    load_css,
    push_button,
    register_icons,
    vertical,
)
from .wizard import SetupWizard

LOGGER = logging.getLogger(__name__)

#: The Windows form is a fixed 580px wide dialog; the setup experience is the
#: same width but taller.
FORM_SIZE = (580, 470)
WIZARD_SIZE = (580, 500)

SYSTEMD_UNIT = "app-io.github.NaveDanan.MouseWithoutBorders.service"
SOURCE_SYSTEMD_UNIT = "app-io.github.NaveDanan.MouseWithoutBorders@dev.service"
INSTALLED_MODULE_ROOT = Path("/usr/lib/powertoys-mouse-without-borders")
HELP_URL = "https://aka.ms/mm"
LOG_PATH_SUFFIX = "powertoys-mwb-linux/service.log"
LOG_TAIL_LINES = 400
#: Status polls must never block the form, and must not keep respawning a
#: service that fails to start.
STATUS_TIMEOUT = 0.4
SERVICE_START_INTERVAL = 15.0
DESKTOP_ACTIVATION_VARIABLES = (
    "DESKTOP_STARTUP_ID",
    "GIO_LAUNCHED_DESKTOP_FILE",
    "GIO_LAUNCHED_DESKTOP_FILE_PID",
    "XDG_ACTIVATION_TOKEN",
)

LETTER_CHOICES = [HOTKEY_DISABLED, *string.ascii_uppercase]

MATRIX_CAPTION = (
    "Computer Matrix - Drag and drop computer thumbnails below to match "
    "computer physical layout. Check the box next to each computer thumbnail "
    "to type in computer name."
)
IP_DESCRIPTION = (
    "You should always connect to other machines by name but if there is "
    "problem resolving machine name to IP address (rarely), you can manually "
    "enter the mappings below. The app will use the IP Addresses from the "
    "mappings below and the DNS resolution result."
)
IP_NOTE = (
    "Note: If your machine IP address is dynamic, you will need to change the "
    "mapping each time the machine IP address changes."
)

#: (option key, caption, indented) for the left and right columns of the
#: Other Options tab, in the order the Windows form lists them.
LEFT_OPTIONS = (
    ("wrap_mouse", "Wrap Mouse", False),
    ("share_clipboard", "Share Clipboard", False),
    ("share_images", "Transfer file", True),
    ("hide_logon_logo", "Hide logo from Logon Screen", False),
    ("hide_mouse_at_edge", "Hide mouse at screen edge", False),
    ("draw_mouse_cursor", "Draw mouse cursor", False),
    ("validate_remote_ip", "Validate remote machine IP Address", False),
    ("same_subnet_only", "Same subnet only", False),
)
RIGHT_OPTIONS = (
    ("disable_cad", "Disable CAD", False),
    ("block_screen_saver", "Block Screen Saver on other machines", False),
    ("move_mouse_relatively", "Move mouse relatively", False),
    ("block_mouse_at_corners", "Block mouse at screen corners", False),
    ("use_key_mappings", "Use Key Mappings", False),
    ("show_status_messages", "Show clipboard/network status messages", False),
)

#: Options stored directly on Config rather than in Config.other_options.
DEDICATED_OPTIONS = {"share_clipboard", "share_images"}

#: Linux-only power behaviour, shown apart from the Windows parity columns.
LINUX_OPTIONS = (
    (
        "stay_awake_on_lid_close",
        "Stay connected when the laptop lid closes (locks the session instead "
        "of suspending)",
    ),
    (
        "never_lock_while_connected",
        "Never lock this screen while a remote PC is connected (a locked "
        "session cannot accept remote input)",
    ),
)

SWITCH_MODES = (("fkeys", "F1, F2, F3, F4"), ("numbers", "1, 2, 3, 4"), ("disabled", "Disable"))

LEFT_HOTKEYS = (
    ("settings", "Show Settings Form, Ctrl+Alt+:"),
    ("lock_machines", "Lock machine(s), Ctrl+Alt+:"),
    ("reconnect", "Reconnect to other machines, Ctrl+Alt+:"),
    ("screen_capture", "Custom screen capture, Ctrl+Shift+:"),
)
RIGHT_HOTKEYS = (
    ("exit", "Exit the application, Ctrl+Alt+Shift+:"),
    ("all_pc_mode", "Switch to ALL PC mode:"),
)


def matrix_coordinates(slot: int, two_row: bool) -> tuple[int, int]:
    """Return the GTK/Windows row and column for a four-machine slot."""

    return (slot // 2, slot % 2) if two_row else (0, slot)


def remote_records(
    matrix: list[str], local_name: str, known_addresses: dict[str, str]
) -> list[dict[str, str]]:
    """Build remote config records in matrix order without losing addresses."""

    local = local_name.casefold()
    records: list[dict[str, str]] = []
    for raw_name in matrix[:4]:
        name = raw_name.strip()
        if not name or name.casefold() == local:
            continue
        records.append(
            {
                "name": name,
                "address": known_addresses.get(name.casefold(), name),
            }
        )
    return records


def adjacent_remote_edges(
    matrix: list[str], local_name: str, two_row: bool
) -> dict[str, str]:
    """Return the remote reached from each outer edge of the local tile."""

    local_key = local_name.casefold()
    try:
        local_slot = next(
            index
            for index, name in enumerate(matrix[:4])
            if name.strip().casefold() == local_key
        )
    except StopIteration:
        return {}

    edges: dict[str, str] = {}
    if not two_row:
        for index in range(local_slot + 1, min(len(matrix), 4)):
            if matrix[index].strip():
                edges["right"] = matrix[index].strip()
                break
        for index in range(local_slot - 1, -1, -1):
            if matrix[index].strip():
                edges["left"] = matrix[index].strip()
                break
        return edges

    row, column = matrix_coordinates(local_slot, True)
    neighbours = {
        "right": (row, column + 1),
        "left": (row, column - 1),
        "bottom": (row + 1, column),
        "top": (row - 1, column),
    }
    for edge, wanted in neighbours.items():
        for slot in range(4):
            if matrix_coordinates(slot, True) == wanted and matrix[slot].strip():
                edges[edge] = matrix[slot].strip()
                break
    return edges


def _start_background_service() -> None:
    """Start the daemon outside the launching application's desktop scope."""

    module_path = Path(__file__).resolve()
    unit_path = Path("/usr/lib/systemd/user") / SYSTEMD_UNIT
    if module_path.is_relative_to(INSTALLED_MODULE_ROOT) and unit_path.is_file():
        try:
            result = subprocess.run(
                ["systemctl", "--user", "start", SYSTEMD_UNIT],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2,
                check=False,
            )
            if result.returncode == 0:
                return
        except (OSError, subprocess.TimeoutExpired):
            pass
    environment = os.environ.copy()
    # A daemon and its clipboard CLI children are not launchable windows. If
    # they inherit the settings window's startup token, GNOME repeatedly shows
    # a transient dock item whenever a short-lived helper starts.
    for variable in DESKTOP_ACTIVATION_VARIABLES:
        environment.pop(variable, None)
    # Source development is often launched from an IDE whose systemd app scope
    # identifies every descendant as that IDE. Put the daemon in its own
    # transient unit so portal prompts and short-lived clipboard helpers are
    # attributed to Mouse Without Borders instead of flashing the IDE's icon.
    source_root = module_path.parents[1]
    if getattr(sys, "frozen", False):
        appimage = os.environ.get("APPIMAGE")
        launch_command = [appimage or sys.executable, "daemon"]
    else:
        launch_command = [sys.executable, "-m", "mwb_linux", "daemon"]
    try:
        result = subprocess.run(
            [
                "systemd-run",
                "--user",
                "--quiet",
                "--collect",
                f"--unit={SOURCE_SYSTEMD_UNIT}",
                "--property=Type=exec",
                f"--setenv=PYTHONPATH={source_root}",
                *launch_command,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2,
            check=False,
            env=environment,
        )
        if result.returncode == 0:
            return
    except (OSError, subprocess.TimeoutExpired):
        pass
    raise OSError(
        "could not start the Mouse Without Borders user service with systemd"
    )


def _request(command: str, **arguments):
    try:
        return control_request(command, **arguments)
    except (OSError, TimeoutError):
        _start_background_service()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            time.sleep(0.1)
            try:
                return control_request(command, **arguments)
            except (OSError, TimeoutError):
                pass
        raise ConnectionError("background service did not start")


class MatrixCell(Gtk.Box):
    """One thumbnail in the computer matrix.

    The Windows wire protocol has exactly four *computer* slots. Physical
    displays are deliberately not cards here; their compositor geometry is
    retained separately for pointer-barrier placement.
    """

    LOCAL = "local"
    REMOTE = "remote"
    EMPTY = "empty"

    DRAG_PREFIX = "mwb-machine:"

    def __init__(
        self,
        slot: int,
        kind: str,
        *,
        on_enabled: Callable[[int, bool], None],
        on_swapped: Callable[[int, int], None],
        on_activate: Callable[[str, str], None],
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.slot = slot
        self.kind = kind
        self._on_enabled = on_enabled
        self._on_swapped = on_swapped
        self._updating = False
        self.set_size_request(105, -1)
        self.set_halign(Gtk.Align.CENTER)

        self.screen = Gtk.Box()
        self.screen.add_css_class("mwb-screen")
        self.screen.set_size_request(78, 44)
        self.screen.set_halign(Gtk.Align.CENTER)
        self.screen.set_valign(Gtk.Align.CENTER)
        self.monitor = Gtk.Box()
        self.monitor.add_css_class("mwb-monitor")
        self.monitor.set_size_request(86, 52)
        self.monitor.set_halign(Gtk.Align.CENTER)
        # Leaves room for the selection box to sit on the outside corner, the
        # way the Windows form draws it.
        self.monitor.set_margin_end(7)
        self.monitor.set_margin_bottom(7)
        self.monitor.append(self.screen)

        self.selected = Gtk.CheckButton()
        self.selected.set_halign(Gtk.Align.END)
        self.selected.set_valign(Gtk.Align.END)
        self.selected.set_active(kind != self.EMPTY)
        self.selected.set_sensitive(kind != self.LOCAL)
        self.selected.connect("toggled", self._on_selected)
        overlay = Gtk.Overlay()
        overlay.set_halign(Gtk.Align.CENTER)
        overlay.set_child(self.monitor)
        overlay.add_overlay(self.selected)
        self.append(overlay)

        self.name = Gtk.Entry()
        self.name.add_css_class("mwb-comp-name")
        self.name.set_alignment(0.5)
        self.name.set_max_length(32)
        self.name.set_size_request(96, 20)
        self.name.set_margin_top(6)
        self.name.set_halign(Gtk.Align.CENTER)
        self.name.set_editable(kind == self.REMOTE)
        self.name.set_can_focus(kind == self.REMOTE)
        self.append(self.name)

        self.status = label(
            "...\n...", css="mwb-comp-status", xalign=0.5, justify=Gtk.Justification.CENTER
        )
        self.status.set_margin_top(4)
        self.append(self.status)

        self.set_screen_on(kind != self.EMPTY)
        if kind != self.EMPTY:
            source = Gtk.DragSource()
            source.set_actions(Gdk.DragAction.MOVE)
            source.connect(
                "prepare",
                lambda *_: Gdk.ContentProvider.new_for_value(
                    f"{self.DRAG_PREFIX}{self.slot}"
                ),
            )
            self.monitor.add_controller(source)

        target = Gtk.DropTarget.new(str, Gdk.DragAction.MOVE)
        target.connect("drop", lambda _target, value, *_: self._on_drop(value))
        target.connect("enter", lambda *_: self._highlight(True) or Gdk.DragAction.MOVE)
        target.connect("leave", lambda *_: self._highlight(False))
        self.monitor.add_controller(target)

        click = Gtk.GestureClick()
        click.connect(
            "pressed",
            lambda _gesture, presses, *_: on_activate(self.kind, self.machine_name)
            if presses == 2
            else None,
        )
        self.monitor.add_controller(click)

    def _highlight(self, active: bool) -> None:
        if active:
            self.monitor.add_css_class("mwb-drop-target")
        else:
            self.monitor.remove_css_class("mwb-drop-target")

    def _on_drop(self, value: str) -> bool:
        self._highlight(False)
        if not value.startswith(self.DRAG_PREFIX):
            return False
        try:
            source = int(value.removeprefix(self.DRAG_PREFIX))
        except ValueError:
            return False
        if source not in range(4):
            return False
        self._on_swapped(source, self.slot)
        return True

    def _on_selected(self, button: Gtk.CheckButton) -> None:
        if self._updating:
            return
        self._updating = True
        if self.kind == self.LOCAL:
            button.set_active(True)
        else:
            self._on_enabled(self.slot, button.get_active())
        self._updating = False

    def set_screen_on(self, on: bool) -> None:
        if on:
            self.screen.remove_css_class("off")
        else:
            self.screen.add_css_class("off")

    def set_screen_shape(self, width: int, height: int) -> None:
        """Match the thumbnail's aspect ratio to the real monitor."""

        if width <= 0 or height <= 0:
            return
        scale = min(78 / width, 44 / height)
        self.screen.set_size_request(max(12, round(width * scale)), max(8, round(height * scale)))

    def set_status(self, first: str, second: str) -> None:
        self.status.set_text(f"{first}\n{second}")

    @property
    def machine_name(self) -> str:
        return self.name.get_text().strip()


class MainWindow(Gtk.ApplicationWindow):
    def __init__(self, application: Adw.Application) -> None:
        super().__init__(application=application)
        self.add_css_class("mwb-root")
        self.set_title("Mouse without Borders - Settings")
        self.set_icon_name(APP_ID)
        self.set_default_size(*FORM_SIZE)

        self.config = self._safe_load_config()
        self._secret = self.config.secret
        self._matrix_names = list(self.config.machine_matrix)
        self._enabled_slots = {
            index for index, name in enumerate(self._matrix_names) if name.strip()
        }
        self._saved_remote_addresses = {
            remote["name"].casefold(): remote["address"]
            for remote in self.config.remote_machines
        }
        self._log_window: Gtk.Window | None = None
        self._service_started_at = 0.0
        self._loading_config = True
        self._update_check_running = False
        self._installing_update = False
        self._announced_update = ""
        self.machine_cards: dict[int, MatrixCell] = {}
        self._read_monitors()

        self.title_stack = Gtk.Stack()
        self.title_stack.add_named(self._build_settings_titlebar(), "settings")
        self.title_stack.add_named(self._build_wizard_titlebar(), "wizard")
        handle = Gtk.WindowHandle()
        handle.set_child(self.title_stack)
        self.set_titlebar(handle)

        self.screens = Gtk.Stack()
        # The setup experience has its own size; measuring only the visible
        # screen keeps the form at the width the Windows dialog uses.
        self.screens.set_vhomogeneous(False)
        self.screens.set_hhomogeneous(False)
        self.screens.add_named(self._build_settings_view(), "settings")
        self.wizard = SetupWizard(self)
        self.screens.add_named(self.wizard, "wizard")
        self.set_child(self.screens)

        self._install_actions()
        self.connect("close-request", self._on_close_request)
        display = Gdk.Display.get_default()
        if display is not None:
            display.get_monitors().connect("items-changed", self._on_monitors_changed)
        self._load_config()
        self._loading_config = False
        if not self._secret:
            self.show_wizard_page("start")
        GLib.timeout_add_seconds(1, self._poll_status)
        if self.config.check_updates:
            self._start_update_check()

    def _on_close_request(self, _window: Gtk.Window) -> bool:
        """Hide the form while the application remains in the top bar."""

        self.set_visible(False)
        return True

    def show_settings(self) -> None:
        """Leave the setup wizard and reveal the full settings form."""

        self.close_wizard()
        self.present()

    # ---------------------------------------------------------- title bars

    def _build_settings_titlebar(self) -> Gtk.Widget:
        bar = horizontal(6)
        bar.add_css_class("mwb-titlebar")
        bar.append(app_icon(16))
        caption = label(
            f"Mouse without Borders {__version__} - Settings", css="mwb-titlebar-text"
        )
        bar.append(caption)
        controls = horizontal(0)
        controls.set_halign(Gtk.Align.END)
        controls.set_hexpand(True)
        for text, action, extra in (
            ("\u2014", self.minimize, None),
            ("\u25a1", self._toggle_maximize, None),
            ("\u2715", self.close, "close"),
        ):
            button = Gtk.Button()
            button.set_child(label(text, xalign=0.5))
            button.add_css_class("mwb-window-button")
            if extra:
                button.add_css_class(extra)
            button.connect("clicked", lambda _button, callback=action: callback())
            controls.append(button)
        bar.append(controls)
        return bar

    def _build_wizard_titlebar(self) -> Gtk.Widget:
        bar = horizontal(8)
        bar.add_css_class("mwb-setup-titlebar")
        bar.append(app_icon(16))
        bar.append(label("MOUSE W/O BORDERS", css="mwb-setup-title-text"))
        close = Gtk.Button()
        close.set_child(label("x", xalign=0.5))
        close.add_css_class("mwb-setup-close")
        close.set_halign(Gtk.Align.END)
        close.set_hexpand(True)
        close.connect("clicked", lambda *_: self.close_wizard())
        bar.append(close)
        return bar

    def _toggle_maximize(self) -> None:
        if self.is_maximized():
            self.unmaximize()
        else:
            self.maximize()

    # ------------------------------------------------------- settings view

    def _build_settings_view(self) -> Gtk.Widget:
        body = vertical(0)
        body.add_css_class("mwb-body")

        self.pages = Gtk.Stack()
        top_nav = horizontal(0)
        top_nav.set_valign(Gtk.Align.END)
        top_nav.set_margin_bottom(8)
        tabs = horizontal(0)
        tabs.set_valign(Gtk.Align.END)
        first_tab: Gtk.ToggleButton | None = None
        for name, caption in (
            ("machine-setup", "Machine Setup"),
            ("other-options", "Other Options"),
            ("ip-mappings", "IP Mappings"),
        ):
            tab = Gtk.ToggleButton(label=caption)
            tab.add_css_class("mwb-tab")
            if first_tab is None:
                first_tab = tab
                tab.set_active(True)
            else:
                tab.set_group(first_tab)
            tab.connect(
                "toggled",
                lambda button, page=name: self.pages.set_visible_child_name(page)
                if button.get_active()
                else None,
            )
            tabs.append(tab)
        top_nav.append(tabs)

        links = horizontal(0)
        links.set_halign(Gtk.Align.END)
        links.set_hexpand(True)
        links.set_valign(Gtk.Align.END)
        links.append(link_button("Help - http://aka.ms/mm", self._open_help))
        links.append(link_button("Mini Log", self._show_mini_log))
        top_nav.append(links)
        body.append(top_nav)

        self.pages.add_named(self._build_machine_setup_page(), "machine-setup")
        self.pages.add_named(self._build_other_options_page(), "other-options")
        self.pages.add_named(self._build_ip_mappings_page(), "ip-mappings")
        body.append(self.pages)
        return body

    def _build_machine_setup_page(self) -> Gtk.Widget:
        page = vertical(0)

        key_group = Fieldset("Shared encryption key")
        key_row = horizontal(8)
        key_row.append(label("Security Key:"))
        self.key_entry = Gtk.Entry()
        self.key_entry.add_css_class("mwb-key-entry")
        self.key_entry.set_hexpand(True)
        self.key_entry.set_visibility(False)
        self.key_entry.set_invisible_char("*")
        self.key_entry.set_max_length(64)
        key_row.append(self.key_entry)
        self.show_key = check_button("Show text")
        self.show_key.connect(
            "toggled", lambda button: self.key_entry.set_visibility(button.get_active())
        )
        key_row.append(self.show_key)
        key_row.append(push_button("New Key", self._generate_key))
        key_group.append(key_row)
        page.append(key_group)

        matrix_group = Fieldset(MATRIX_CAPTION, wrap=True)
        self.matrix = Gtk.Grid(column_spacing=12, row_spacing=12)
        self.matrix.set_halign(Gtk.Align.CENTER)
        self.matrix.set_margin_top(10)
        self.matrix.set_margin_bottom(6)
        matrix_group.append(self.matrix)
        self._attach_matrix_menu()

        footer = horizontal(0)
        self.two_row = check_button("Two Row")
        self.two_row.connect("toggled", self._on_layout_changed)
        footer.append(self.two_row)
        setup_link = link_button(
            "Go through the setup experience", lambda: self.show_wizard_page("start")
        )
        setup_link.set_halign(Gtk.Align.END)
        setup_link.set_hexpand(True)
        footer.append(setup_link)
        matrix_group.append(footer)
        page.append(matrix_group)

        actions = horizontal(12)
        actions.set_halign(Gtk.Align.CENTER)
        actions.set_margin_top(4)
        self.connect_button = push_button("Connect", self._connect, dialog=True)
        self.disconnect_button = push_button(
            "Disconnect", lambda: self._command("disconnect"), dialog=True
        )
        actions.append(self.connect_button)
        actions.append(self.disconnect_button)
        actions.append(push_button("Apply", self._apply, dialog=True))
        actions.append(push_button("Close", self.close, dialog=True))
        page.append(actions)
        return page

    def _build_other_options_page(self) -> Gtk.Widget:
        page = vertical(0)
        self.option_buttons: dict[str, Gtk.CheckButton] = {}

        columns = horizontal(20)
        columns.set_margin_top(6)
        columns.set_margin_bottom(14)
        for options in (LEFT_OPTIONS, RIGHT_OPTIONS):
            column = vertical(6)
            column.set_hexpand(True)
            for key, caption, indented in options:
                button = check_button(caption)
                if indented:
                    button.set_margin_start(20)
                self.option_buttons[key] = button
                column.append(button)
            columns.append(column)
        page.append(columns)

        power = Fieldset("Linux Power")
        for key, caption in LINUX_OPTIONS:
            button = check_button(caption)
            self.option_buttons[key] = button
            power.append(button)
        page.append(power)

        updates = horizontal(8)
        updates.set_margin_bottom(8)
        self.check_updates = check_button("Check Updates")
        self.check_updates.connect("toggled", self._on_check_updates_toggled)
        updates.append(self.check_updates)
        self.update_status = label("", css="mwb-update-status")
        self.update_status.set_ellipsize(3)  # Pango.EllipsizeMode.END
        self.update_status.set_hexpand(True)
        self.update_status.set_halign(Gtk.Align.END)
        updates.append(self.update_status)
        self.update_refresh = push_button("Refresh", self._manual_update_check)
        updates.append(self.update_refresh)
        page.append(updates)

        shortcuts = Fieldset("Keyboard Shortcuts")
        switch_row = horizontal(16)
        switch_row.set_margin_bottom(8)
        switch_row.append(label("Switch between machines, Ctrl+Alt+:"))
        self.switch_buttons: dict[str, Gtk.CheckButton] = {}
        group = horizontal(16)
        group.set_halign(Gtk.Align.END)
        group.set_hexpand(True)
        first: Gtk.CheckButton | None = None
        for mode, caption in SWITCH_MODES:
            button = Gtk.CheckButton(label=caption)
            if first is None:
                first = button
            else:
                button.set_group(first)
            self.switch_buttons[mode] = button
            group.append(button)
        switch_row.append(group)
        shortcuts.append(switch_row)

        self.hotkey_selectors: dict[str, Gtk.DropDown] = {}
        grid = horizontal(16)
        for entries, extras in (
            (LEFT_HOTKEYS, ()),
            (RIGHT_HOTKEYS, ("easy_mouse", "toggle_easy_mouse")),
        ):
            column = vertical(6)
            column.set_hexpand(True)
            for key, caption in entries:
                column.append(self._hotkey_row(caption, key))
            for extra in extras:
                if extra == "easy_mouse":
                    row = horizontal(6)
                    row.append(label("Easy Mouse:"))
                    self.easy_mouse = dropdown(["Enable", "Disable"], "Enable", width=72)
                    self.easy_mouse.set_halign(Gtk.Align.END)
                    self.easy_mouse.set_hexpand(True)
                    row.append(self.easy_mouse)
                    column.append(row)
                else:
                    column.append(
                        self._hotkey_row("Toggle Easy Mouse, Ctrl+Alt+:", extra)
                    )
            grid.append(column)
        shortcuts.append(grid)
        page.append(shortcuts)
        return page

    def _hotkey_row(self, caption: str, key: str) -> Gtk.Widget:
        row = horizontal(6)
        text = label(caption)
        text.set_ellipsize(3)  # Pango.EllipsizeMode.END
        row.append(text)
        selector = dropdown(LETTER_CHOICES, HOTKEY_DISABLED, width=56)
        selector.set_halign(Gtk.Align.END)
        selector.set_hexpand(True)
        self.hotkey_selectors[key] = selector
        row.append(selector)
        return row

    def _build_ip_mappings_page(self) -> Gtk.Widget:
        page = vertical(6)
        description = vertical(6)
        description.set_margin_top(4)
        description.set_margin_bottom(4)
        for text in (IP_DESCRIPTION, IP_NOTE):
            description.append(label(text, css="mwb-desc", wrap=True))
        page.append(description)

        mappings = Fieldset("Machine name to IP address mappings")
        self.mappings_view = Gtk.TextView()
        self.mappings_view.add_css_class("mwb-ip-text")
        self.mappings_view.set_monospace(False)
        self.mappings_view.set_top_margin(2)
        self.mappings_view.set_left_margin(4)
        scroller = Gtk.ScrolledWindow()
        scroller.add_css_class("mwb-textview-frame")
        scroller.set_child(self.mappings_view)
        scroller.set_size_request(-1, 92)
        mappings.append(scroller)
        page.append(mappings)

        advanced = Fieldset("Connection")
        row = horizontal(8)
        row.append(label("Base TCP port:"))
        self.port = Gtk.SpinButton.new_with_range(1024, 65534, 1)
        self.port.set_valign(Gtk.Align.CENTER)
        row.append(self.port)
        profile_label = label("Encryption compatibility:")
        profile_label.set_margin_start(16)
        row.append(profile_label)
        self.profile = dropdown(list(CRYPTO_PROFILES), "auto", width=120)
        self.profile.set_halign(Gtk.Align.END)
        self.profile.set_hexpand(True)
        row.append(self.profile)
        advanced.append(row)
        page.append(advanced)

        watermark = label("Mouse without Borders", css="mwb-watermark", xalign=0.5)
        watermark.set_margin_top(10)
        watermark.set_vexpand(True)
        watermark.set_valign(Gtk.Align.CENTER)
        page.append(watermark)
        return page

    # ------------------------------------------------------------- matrix

    def _read_monitors(self) -> None:
        """Refresh compositor geometry used by the input-capture barrier."""

        display = Gdk.Display.get_default()
        self._monitors = read_monitors(display) if display else []
        if not self._monitors:
            # Without a compositor answer, fall back to a single screen so the
            # form still shows this computer.
            self._monitors = [Monitor(name="Display", x=0, y=0, width=1920, height=1080)]

    def _on_monitors_changed(self, *_args) -> None:
        self._remember_matrix_names()
        self._read_monitors()
        self._render_matrix()

    def _on_layout_changed(self, *_args) -> None:
        self._remember_matrix_names()
        self._render_matrix()

    def _remember_matrix_names(self) -> None:
        for slot, card in self.machine_cards.items():
            if card.kind == MatrixCell.REMOTE:
                self._matrix_names[slot] = card.machine_name

    def _render_matrix(self) -> None:
        """Draw the four computer slots encoded by Windows MATRIX packets."""

        child = self.matrix.get_first_child()
        while child is not None:
            following = child.get_next_sibling()
            self.matrix.remove(child)
            child = following

        self.machine_cards = {}
        local_key = self.config.machine_name.casefold()
        for slot in range(4):
            name = self._matrix_names[slot].strip()
            if name.casefold() == local_key:
                kind = MatrixCell.LOCAL
                self._enabled_slots.add(slot)
            elif slot in self._enabled_slots:
                kind = MatrixCell.REMOTE
            else:
                kind = MatrixCell.EMPTY
                self._matrix_names[slot] = ""
            card = MatrixCell(
                slot,
                kind,
                on_enabled=self._set_slot_enabled,
                on_swapped=self._swap_slots,
                on_activate=self._activate_card,
            )
            card.name.set_text(self.config.machine_name if kind == MatrixCell.LOCAL else name)
            if kind == MatrixCell.LOCAL:
                monitor = self._monitors[0]
                card.set_screen_shape(monitor.width, monitor.height)
                display_name = (
                    monitor.name
                    if len(self._monitors) == 1
                    else f"{len(self._monitors)} displays"
                )
                card.set_status(display_name, monitor.resolution)
            elif kind == MatrixCell.REMOTE:
                card.set_status("...", "Not connected")
            row, column = matrix_coordinates(slot, self.two_row.get_active())
            self.matrix.attach(card, column, row, 1, 1)
            self.machine_cards[slot] = card

    def _set_slot_enabled(self, slot: int, enabled: bool) -> None:
        if self._matrix_names[slot].strip().casefold() == self.config.machine_name.casefold():
            return
        self._remember_matrix_names()
        if enabled:
            self._enabled_slots.add(slot)
        else:
            self._enabled_slots.discard(slot)
            self._matrix_names[slot] = ""
        self._render_matrix()
        if enabled and slot in self.machine_cards:
            self.machine_cards[slot].name.grab_focus()

    def _swap_slots(self, source: int, destination: int) -> None:
        if source == destination:
            return
        self._remember_matrix_names()
        self._matrix_names[source], self._matrix_names[destination] = (
            self._matrix_names[destination],
            self._matrix_names[source],
        )
        source_enabled = source in self._enabled_slots
        destination_enabled = destination in self._enabled_slots
        self._enabled_slots.discard(source)
        self._enabled_slots.discard(destination)
        if source_enabled:
            self._enabled_slots.add(destination)
        if destination_enabled:
            self._enabled_slots.add(source)
        self._render_matrix()

    def _activate_card(self, kind: str, machine_name: str) -> None:
        if kind == MatrixCell.REMOTE and machine_name:
            self._command("switch_remote", machine_name=machine_name)
        elif kind == MatrixCell.LOCAL:
            self._command("release_local")

    def _host_placement(self) -> tuple[str, list[int]]:
        """Retain one legacy portal edge while the bridge gains multi-edge capture."""

        edges = adjacent_remote_edges(
            self._matrix_names,
            self.config.machine_name,
            self.two_row.get_active(),
        )
        edge = self.config.host_position if self.config.host_position in edges else next(
            iter(edges), self.config.host_position
        )
        if self.config.host_zone and is_exterior(
            self._monitors, edge, self.config.host_zone
        ):
            return edge, list(self.config.host_zone)

        keys = {
            "left": lambda monitor: monitor.x,
            "right": lambda monitor: -(monitor.x + monitor.width),
            "top": lambda monitor: monitor.y,
            "bottom": lambda monitor: -(monitor.y + monitor.height),
        }
        monitor = min(self._monitors, key=keys[edge])
        return edge, monitor.zone

    # ------------------------------------------------------ configuration

    def _safe_load_config(self) -> Config:
        try:
            return Config.load()
        except (OSError, ValueError) as exc:
            GLib.idle_add(self.report, f"Could not read the configuration: {exc}")
            return Config()

    def _load_config(self) -> None:
        config = self.config
        self._secret = config.secret
        self.key_entry.set_text(config.secret)
        self.show_key.set_active(False)
        self.key_entry.set_visibility(False)

        self._matrix_names = list(config.machine_matrix[:4])
        self._matrix_names.extend([""] * (4 - len(self._matrix_names)))
        self._enabled_slots = {
            index for index, name in enumerate(self._matrix_names) if name.strip()
        }
        self._saved_remote_addresses = {
            remote["name"].casefold(): remote["address"]
            for remote in config.remote_machines
        }
        self.two_row.set_active(config.two_row)
        self._read_monitors()
        self._render_matrix()

        for key, button in self.option_buttons.items():
            if key == "share_clipboard":
                button.set_active(config.share_clipboard)
            elif key == "share_images":
                button.set_active(config.share_images)
            else:
                button.set_active(bool(config.other_options.get(key)))

        self.switch_buttons[config.switch_hotkey].set_active(True)
        for key, selector in self.hotkey_selectors.items():
            value = config.hotkeys.get(key, HOTKEY_DISABLED)
            selector.set_selected(
                LETTER_CHOICES.index(value) if value in LETTER_CHOICES else 0
            )
        self.easy_mouse.set_selected(0 if config.edge_switching else 1)
        self.check_updates.set_active(config.check_updates)

        self.mappings_view.get_buffer().set_text(config.ip_mappings)
        self.port.set_value(config.port)
        self.profile.set_selected(
            CRYPTO_PROFILES.index(config.crypto_profile)
            if config.crypto_profile in CRYPTO_PROFILES
            else 0
        )

    def _mappings_text(self) -> str:
        buffer = self.mappings_view.get_buffer()
        return buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False)

    def _collect(self) -> dict[str, object]:
        self._remember_matrix_names()
        matrix = [
            self._matrix_names[slot].strip() if slot in self._enabled_slots else ""
            for slot in range(4)
        ]
        remotes = remote_records(
            matrix, self.config.machine_name, self._saved_remote_addresses
        )
        primary = remotes[0] if remotes else {"name": "", "address": ""}
        two_row = self.two_row.get_active()
        position, zone = self._host_placement()
        other_options = {
            key: button.get_active()
            for key, button in self.option_buttons.items()
            if key not in DEDICATED_OPTIONS
        }
        hotkeys = {
            key: dropdown_value(selector)
            for key, selector in self.hotkey_selectors.items()
        }
        switch_hotkey = next(
            mode for mode, button in self.switch_buttons.items() if button.get_active()
        )
        return {
            "secret": self.key_entry.get_text(),
            # Preserve the old pair for CLI/config compatibility while the
            # complete matrix and remote records remain authoritative.
            "host_name": primary["name"],
            "host": primary["address"],
            "machine_matrix": matrix,
            "remote_machines": remotes,
            "host_position": position,
            "host_zone": zone,
            "two_row": two_row,
            "share_clipboard": self.option_buttons["share_clipboard"].get_active(),
            "share_images": self.option_buttons["share_images"].get_active(),
            "edge_switching": dropdown_value(self.easy_mouse) == "Enable",
            "check_updates": self.check_updates.get_active(),
            "switch_hotkey": switch_hotkey,
            "other_options": other_options,
            "hotkeys": hotkeys,
            "ip_mappings": self._mappings_text(),
            "port": int(self.port.get_value()),
            "crypto_profile": dropdown_value(self.profile),
        }

    def _apply(self) -> bool:
        values = self._collect()
        local_key = self.config.machine_name.casefold()
        for slot in self._enabled_slots:
            if (
                self._matrix_names[slot].strip().casefold() != local_key
                and not self._matrix_names[slot].strip()
            ):
                self.report("Type a computer name or uncheck its thumbnail.")
                return False
        try:
            parse_ip_mappings(str(values["ip_mappings"]))
        except ValueError as exc:
            self.report(f"IP mappings: {exc}")
            return False
        if not is_exterior(self._monitors, str(values["host_position"]), list(values["host_zone"])):
            self.report(
                "Place the other computer beyond an outer screen edge. The "
                "pointer cannot cross where two of this computer's monitors "
                "meet."
            )
            return False
        try:
            response = _request("save_config", config=values)
            if not response.get("ok"):
                raise ValueError(response.get("error", "save failed"))
        except Exception as exc:
            self.report(str(exc))
            return False
        for key, value in values.items():
            setattr(self.config, key, value)
        self._matrix_names = list(values["machine_matrix"])
        self._enabled_slots = {
            index for index, name in enumerate(self._matrix_names) if str(name).strip()
        }
        self._saved_remote_addresses = {
            remote["name"].casefold(): remote["address"]
            for remote in values["remote_machines"]
        }
        self._secret = self.config.secret
        return True

    def _connect(self) -> None:
        if self._apply():
            self._command("connect")

    def _generate_key(self) -> None:
        self.key_entry.set_text(generate_secret())
        self.show_key.set_active(True)

    def _command(self, command: str, **arguments: object) -> None:
        try:
            response = _request(command, **arguments)
            if not response.get("ok"):
                raise RuntimeError(response.get("error", "command failed"))
        except Exception as exc:
            self.report(str(exc))

    # ------------------------------------------------------------ wizard

    def show_wizard_page(self, name: str) -> None:
        self.wizard.show_page(name)
        self.title_stack.set_visible_child_name("wizard")
        self.screens.set_visible_child_name("wizard")
        self._resize(WIZARD_SIZE)

    def close_wizard(self) -> None:
        self.title_stack.set_visible_child_name("settings")
        self.screens.set_visible_child_name("settings")
        self._resize(FORM_SIZE)

    def _resize(self, size: tuple[int, int]) -> None:
        if not self.is_maximized() and not self.is_fullscreen():
            self.set_default_size(*size)

    def link_to_machine(self, security_code: str, machine_name: str) -> None:
        if len(security_code) < 16:
            self.report("Enter the 16-character security code from the other computer.")
            return
        if not machine_name:
            self.report("Enter the other computer's name.")
            return
        self.key_entry.set_text(security_code)
        self._remember_matrix_names()
        local_key = self.config.machine_name.casefold()
        slot = next(
            (
                index
                for index, name in enumerate(self._matrix_names)
                if name.strip().casefold() == machine_name.casefold()
                and name.strip().casefold() != local_key
            ),
            None,
        )
        if slot is None:
            slot = next(
                (
                    index
                    for index in range(4)
                    if not self._matrix_names[index].strip()
                ),
                None,
            )
        if slot is None:
            self.report("The four-computer matrix is already full.")
            return
        self._matrix_names[slot] = machine_name
        self._enabled_slots.add(slot)
        self._render_matrix()
        if self._apply():
            self.close_wizard()
            self._command("connect")

    def take_security_key(self) -> str:
        """Return the key to share, generating one when none is configured."""

        if len(self.key_entry.get_text()) < 16:
            self.key_entry.set_text(generate_secret())
            self._apply()
        return self.key_entry.get_text()

    def local_machine_name(self) -> str:
        return self.config.machine_name

    # ------------------------------------------------------------- extras

    def _attach_matrix_menu(self) -> None:
        """Right-click the matrix for the commands the form has no room for."""

        menu = Gio.Menu()
        menu.append("Connect", "mwb.connect")
        menu.append("Disconnect", "mwb.disconnect")
        menu.append("Reconnect", "mwb.reconnect")
        popover = Gtk.PopoverMenu.new_from_model(menu)
        popover.set_parent(self.matrix)
        popover.set_has_arrow(False)
        gesture = Gtk.GestureClick(button=3)
        gesture.connect("pressed", lambda _gesture, _n, x, y: self._popup(popover, x, y))
        self.matrix.add_controller(gesture)

    def _popup(self, popover: Gtk.PopoverMenu, x: float, y: float) -> None:
        popover.set_pointing_to(Gdk.Rectangle(x=int(x), y=int(y), width=1, height=1))
        popover.popup()

    def _install_actions(self) -> None:
        actions = Gio.SimpleActionGroup()
        for name, command in (
            ("connect", "connect"),
            ("disconnect", "disconnect"),
            ("reconnect", "reconnect"),
        ):
            action = Gio.SimpleAction.new(name, None)
            if command == "connect":
                action.connect("activate", lambda *_: self._connect())
            else:
                action.connect("activate", lambda *_, value=command: self._command(value))
            actions.add_action(action)
        self.insert_action_group("mwb", actions)

    def _open_help(self) -> None:
        Gtk.UriLauncher.new(HELP_URL).launch(self, None, None)

    def _log_text(self) -> str:
        state_home = Path(
            GLib.getenv("XDG_STATE_HOME") or str(Path.home() / ".local/state")
        )
        path = state_home / LOG_PATH_SUFFIX
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as exc:
            return f"Could not read {path}: {exc}"
        return "\n".join(lines[-LOG_TAIL_LINES:])

    def _show_mini_log(self) -> None:
        if self._log_window is None:
            window = Gtk.Window(title="Mouse without Borders - Mini Log")
            window.set_transient_for(self)
            window.set_default_size(700, 420)
            view = Gtk.TextView(editable=False, monospace=True)
            view.set_left_margin(6)
            view.set_top_margin(6)
            scroller = Gtk.ScrolledWindow()
            scroller.set_child(view)
            window.set_child(scroller)
            window.connect("close-request", self._on_log_window_closed)
            self._log_view = view
            self._log_window = window
        self._log_view.get_buffer().set_text(self._log_text())
        self._log_window.present()

    def _on_log_window_closed(self, window: Gtk.Window) -> bool:
        window.set_visible(False)
        return True

    def report(self, message: str) -> None:
        dialog = Gtk.AlertDialog()
        dialog.set_message("Mouse without Borders")
        dialog.set_detail(message)
        dialog.show(self)

    # ------------------------------------------------------------- updates

    def _on_check_updates_toggled(self, button: Gtk.CheckButton) -> None:
        """Persist the launch-check preference without requiring Apply."""

        if self._loading_config:
            return
        self.config.check_updates = button.get_active()
        try:
            self.config.save()
        except OSError as exc:
            LOGGER.warning("could not save update preference: %s", exc)
        if button.get_active():
            self._start_update_check()

    def _manual_update_check(self) -> None:
        self._start_update_check(manual=True)

    def _start_update_check(self, *, manual: bool = False) -> None:
        """Check GitHub off the GTK thread; automatic failures stay silent."""

        if self._update_check_running or self._installing_update:
            return
        if not automatic_install_supported():
            if manual:
                self.update_status.set_text("Download updates from GitHub Releases")
            return
        self._update_check_running = True
        self.update_refresh.set_sensitive(False)
        if manual:
            self.update_status.set_text("Checking...")

        def check() -> None:
            try:
                release = check_for_update(__version__)
            except Exception as exc:
                LOGGER.info("update check unavailable: %s", exc)
                GLib.idle_add(self._finish_update_check, None, manual, False)
            else:
                GLib.idle_add(self._finish_update_check, release, manual, True)

        threading.Thread(target=check, name="mwb-update-check", daemon=True).start()

    def _finish_update_check(
        self,
        release: UpdateRelease | None,
        manual: bool,
        succeeded: bool,
    ) -> bool:
        self._update_check_running = False
        self.update_refresh.set_sensitive(True)
        if not manual and not self.config.check_updates:
            return GLib.SOURCE_REMOVE
        if not succeeded:
            if manual:
                self.update_status.set_text("Unable to check right now")
            return GLib.SOURCE_REMOVE
        if release is None:
            if manual:
                self.update_status.set_text(f"Up to date ({__version__})")
            return GLib.SOURCE_REMOVE
        self.update_status.set_text(f"Version {release.version} available")
        if manual or self._announced_update != release.version:
            self._announced_update = release.version
            self._show_update_available(release)
        return GLib.SOURCE_REMOVE

    def _show_update_available(self, release: UpdateRelease) -> None:
        self.present()
        dialog = Gtk.AlertDialog()
        dialog.set_message("A new version is available")
        dialog.set_detail(
            f"Current version: {__version__}\nLatest version: {release.version}\n\n"
            "Mouse without Borders will remain open while the update downloads "
            "and installs, then close and relaunch automatically."
        )
        dialog.set_buttons(["Later", "Download and Install"])
        dialog.set_cancel_button(0)
        dialog.set_default_button(1)
        dialog.choose(
            self,
            None,
            lambda source, result, *_: self._on_update_choice(
                source, result, release
            ),
        )

    def _on_update_choice(
        self,
        dialog: Gtk.AlertDialog,
        result: Gio.AsyncResult,
        release: UpdateRelease,
    ) -> None:
        try:
            choice = dialog.choose_finish(result)
        except GLib.Error:
            return
        if choice == 1:
            self._install_update(release)

    def _install_update(self, release: UpdateRelease) -> None:
        if self._installing_update:
            return
        self._installing_update = True
        self.update_refresh.set_sensitive(False)
        self.update_status.set_text(f"Downloading {release.version}...")

        def install() -> None:
            try:
                package = download_release(release)
                GLib.idle_add(
                    self.update_status.set_text,
                    f"Installing {release.version}...",
                )
                install_package(package, release.version)
            except Exception as exc:
                LOGGER.warning("update failed: %s", exc)
                GLib.idle_add(self._finish_update_install, release, False)
            else:
                GLib.idle_add(self._finish_update_install, release, True)

        threading.Thread(target=install, name="mwb-update-install", daemon=True).start()

    def _finish_update_install(
        self, release: UpdateRelease, succeeded: bool
    ) -> bool:
        if not succeeded:
            self._installing_update = False
            self.update_refresh.set_sensitive(True)
            self.update_status.set_text("Update was not installed")
            return GLib.SOURCE_REMOVE
        self.update_status.set_text(f"Updated to {release.version}; relaunching...")
        try:
            schedule_relaunch()
        except Exception as exc:
            LOGGER.warning("could not relaunch after update: %s", exc)
            self._installing_update = False
            self.update_refresh.set_sensitive(True)
            self.update_status.set_text("Updated; close and reopen to finish")
            return GLib.SOURCE_REMOVE
        application = self.get_application()
        if isinstance(application, MouseWithoutBordersApplication):
            application.exit_application()
        else:
            application.quit()
        return GLib.SOURCE_REMOVE

    # ------------------------------------------------------------- status

    def _start_service_if_idle(self) -> None:
        """Start the background service at most once every few seconds."""

        now = time.monotonic()
        if now - self._service_started_at < SERVICE_START_INTERVAL:
            return
        self._service_started_at = now
        def start() -> None:
            try:
                _start_background_service()
            except OSError as exc:
                LOGGER.warning("could not start the background service: %s", exc)

        threading.Thread(target=start, name="mwb-service-start", daemon=True).start()

    def _poll_status(self) -> bool:
        application = self.get_application()
        if (
            isinstance(application, MouseWithoutBordersApplication)
            and application._exit_started
        ):
            # Do not let the normal self-healing status poll restart the service
            # after top-bar Exit has deliberately begun shutting it down.
            return GLib.SOURCE_REMOVE
        try:
            status = control_request("status", timeout=STATUS_TIMEOUT)["status"]
        except Exception:
            self._start_service_if_idle()
            for card in self.machine_cards.values():
                if card.kind == MatrixCell.REMOTE:
                    card.set_screen_on(False)
                    card.set_status("...", "Starting service")
            return GLib.SOURCE_CONTINUE

        self._service_started_at = time.monotonic()
        state = status["state"]
        self.connect_button.set_sensitive(state not in {"connected", "connecting"})
        self.disconnect_button.set_sensitive(
            state not in {"disconnected", "stopped", "unconfigured"}
        )
        raw_peers = list(status.get("peers") or [])
        if not raw_peers and status.get("peer"):
            raw_peers = [status["peer"]]
        peers = {
            str(peer.get("name", "")).casefold(): peer
            for peer in raw_peers
            if peer.get("name")
        }
        remote_active = bool(status.get("remote_active"))
        active_name = str(
            status.get("active_remote_name") or status.get("active_remote") or ""
        )
        for card in self.machine_cards.values():
            if card.kind == MatrixCell.LOCAL:
                monitor = self._monitors[0] if self._monitors else None
                first = (
                    monitor.name
                    if monitor and len(self._monitors) == 1
                    else f"{len(self._monitors)} displays"
                )
                second = (
                    f"controlling {active_name or 'remote'}"
                    if remote_active
                    else (monitor.resolution if monitor else "local machine")
                )
                card.set_screen_on(True)
                card.set_status(first, second)
            elif card.kind == MatrixCell.REMOTE:
                peer = peers.get(card.machine_name.casefold())
                connected = peer is not None
                card.set_screen_on(connected)
                card.set_status(
                    "... ->" if connected else "...",
                    ("Connected <--" if connected else str(status["message"]))[:44],
                )
        return GLib.SOURCE_CONTINUE


class MouseWithoutBordersApplication(Adw.Application):
    def __init__(self) -> None:
        super().__init__(
            application_id=APP_ID,
            flags=Gio.ApplicationFlags.DEFAULT_FLAGS,
        )
        self.window: MainWindow | None = None
        self.indicator: TopBarIndicator | None = None
        self._held_for_indicator = False
        self._exit_started = False

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_LIGHT)
        load_css()
        register_icons()
        # A GApplication normally exits after its final window closes. Keep it
        # alive until the explicit top-bar Exit action is selected.
        self.hold()
        self._held_for_indicator = True
        self.indicator = TopBarIndicator(
            icon_name=APP_ID,
            # GNOME Shell's StatusNotifierItem host treats this as a direct
            # icon lookup directory rather than a GTK theme root.
            icon_theme_path=ICON_DIRECTORY / "hicolor" / "scalable" / "apps",
            on_open=self.open_window,
            on_settings=self.open_settings,
            on_exit=self.exit_application,
        )
        self.indicator.start()

    def do_activate(self) -> None:
        # Ensure the background service exists without blocking window startup.
        # resume_ui also remains compatible with daemons parked by an older UI.
        threading.Thread(
            target=lambda: _request("resume_ui"),
            name="mwb-service-resume",
            daemon=True,
        ).start()
        self.open_window()

    def open_window(self) -> None:
        if self.window is None:
            self.window = MainWindow(self)
        self.window.present()

    def open_settings(self) -> None:
        if self.window is None:
            self.window = MainWindow(self)
        self.window.show_settings()

    def exit_application(self) -> None:
        """Shut down every application component without blocking GTK."""

        # DBusMenu hosts occasionally deliver both Event and EventGroup for one
        # click. Cleanup must only run once, especially while the first worker
        # is waiting for the input portal to release its session.
        if self._exit_started:
            return
        self._exit_started = True
        if self.indicator is not None:
            self.indicator.stop()
            self.indicator = None
        if self.window is not None:
            self.window.set_visible(False)

        # Portal and network teardown can take several seconds. Running it in
        # this callback would stop GTK's main loop while GNOME Shell is still
        # completing the indicator menu event, making the desktop appear hung.
        threading.Thread(
            target=self._stop_service_and_finish_exit,
            name="mwb-application-exit",
            daemon=True,
        ).start()

    def _stop_service_and_finish_exit(self) -> None:
        """Request a full service stop, with systemd as a bounded fallback."""

        try:
            response = control_request("quit", timeout=2.0)
            if not response.get("ok"):
                raise OSError(str(response.get("error", "exit was rejected")))
        except (OSError, TimeoutError, ValueError) as exc:
            # Fail closed: if the control socket cannot begin shutdown, ask
            # systemd to terminate either the installed or development unit.
            LOGGER.warning("could not request a clean application shutdown: %s", exc)
            try:
                subprocess.run(
                    [
                        "systemctl",
                        "--user",
                        "stop",
                        SYSTEMD_UNIT,
                        SOURCE_SYSTEMD_UNIT,
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as stop_error:
                LOGGER.error("could not stop the sharing service: %s", stop_error)
        GLib.idle_add(self._finish_exit)

    def _finish_exit(self) -> bool:
        """Release GApplication's indicator hold from GTK's main thread."""

        if self._held_for_indicator:
            self.release()
            self._held_for_indicator = False
        self.quit()
        return GLib.SOURCE_REMOVE


def run_ui(argv: list[str] | None = None) -> int:
    return MouseWithoutBordersApplication().run(argv or [])
