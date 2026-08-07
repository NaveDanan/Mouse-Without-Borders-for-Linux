"""Shared GTK helpers for the classic Mouse Without Borders form."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, GdkPixbuf, GLib, Gtk  # noqa: E402

ASSET_DIRECTORY = Path(__file__).resolve().parent

#: The bundled icon theme also ships as the package's hicolor icon, so the
#: window, the task bar, and the desktop entry all use the same artwork.
ICON_DIRECTORY = ASSET_DIRECTORY / "icons"
APP_ID = "io.github.NaveDanan.MouseWithoutBorders"

#: Half of a 12px line, so a single-line legend is centred on the frame border
#: the way an HTML ``legend`` element cuts through a ``fieldset``.
LEGEND_OFFSET = 8


def asset_path(name: str) -> Path:
    return ASSET_DIRECTORY / name


def load_css() -> None:
    provider = Gtk.CssProvider()
    provider.load_from_path(str(asset_path("style.css")))
    display = Gdk.Display.get_default()
    if display is not None:
        Gtk.StyleContext.add_provider_for_display(
            display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )


def register_icons() -> None:
    """Make the bundled application icon resolvable when running uninstalled."""

    display = Gdk.Display.get_default()
    if display is None:
        return
    theme = Gtk.IconTheme.get_for_display(display)
    directory = str(ICON_DIRECTORY)
    if directory not in theme.get_search_path():
        theme.add_search_path(directory)
    Gtk.Window.set_default_icon_name(APP_ID)


def app_icon(size: int) -> Gtk.Image:
    image = Gtk.Image.new_from_icon_name(APP_ID)
    image.set_pixel_size(size)
    return image


def image_from_asset(name: str, size: int) -> Gtk.Image:
    """Load a bundled SVG at an exact pixel size."""

    image = Gtk.Image()
    try:
        pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_size(str(asset_path(name)), size, size)
        image.set_from_pixbuf(pixbuf)
    except GLib.Error:
        image.set_from_icon_name("input-mouse-symbolic")
    image.set_pixel_size(size)
    return image


#: Wrapping labels report their whole sentence as the natural width, which
#: would stretch the window well past the form's 580px. Cap it instead.
DEFAULT_WRAP_CHARS = 64


def label(
    text: str,
    *,
    css: str | tuple[str, ...] = (),
    xalign: float = 0.0,
    wrap: bool = False,
    markup: bool = False,
    justify: Gtk.Justification = Gtk.Justification.LEFT,
    max_chars: int = DEFAULT_WRAP_CHARS,
) -> Gtk.Label:
    widget = Gtk.Label(xalign=xalign)
    if markup:
        widget.set_markup(text)
    else:
        widget.set_text(text)
    widget.set_wrap(wrap)
    if wrap:
        widget.set_wrap_mode(2)  # Pango.WrapMode.WORD_CHAR
        widget.set_max_width_chars(max_chars)
    widget.set_justify(justify)
    for name in (css,) if isinstance(css, str) else css:
        widget.add_css_class(name)
    return widget


def link_button(text: str, on_click: Callable[[], None], *, css: str = "mwb-link") -> Gtk.Button:
    """A flat, underlined, blue label that behaves like an anchor."""

    button = Gtk.Button()
    button.set_child(label(f"<u>{GLib.markup_escape_text(text)}</u>", markup=True))
    button.add_css_class(css)
    button.set_cursor(Gdk.Cursor.new_from_name("pointer", None))
    button.connect("clicked", lambda *_: on_click())
    return button


def push_button(text: str, on_click: Callable[[], None], *, dialog: bool = False) -> Gtk.Button:
    button = Gtk.Button(label=text)
    button.add_css_class("mwb-button")
    if dialog:
        button.add_css_class("mwb-dialog-button")
    button.connect("clicked", lambda *_: on_click())
    return button


def check_button(text: str, active: bool = False) -> Gtk.CheckButton:
    button = Gtk.CheckButton(label=text)
    button.set_active(active)
    return button


def dropdown(values: list[str], selected: str, *, width: int = 54) -> Gtk.DropDown:
    widget = Gtk.DropDown.new_from_strings(values)
    widget.set_selected(values.index(selected) if selected in values else 0)
    widget.add_css_class("mwb-select")
    widget.set_size_request(width, -1)
    widget.set_valign(Gtk.Align.CENTER)
    return widget


def dropdown_value(widget: Gtk.DropDown) -> str:
    item = widget.get_selected_item()
    return item.get_string() if item else ""


def horizontal(spacing: int = 0, **kwargs) -> Gtk.Box:
    return Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=spacing, **kwargs)


def vertical(spacing: int = 0, **kwargs) -> Gtk.Box:
    return Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=spacing, **kwargs)


class Fieldset(Gtk.Overlay):
    """A bordered group whose caption sits on the border, like an HTML fieldset.

    Long captions wrap and are laid out inside the frame instead, because a
    multi-line caption would otherwise hide the whole top border.
    """

    def __init__(self, caption: str, *, spacing: int = 6, wrap: bool = False) -> None:
        super().__init__()
        self.frame = vertical(spacing)
        self.frame.add_css_class("mwb-fieldset")
        self.content = vertical(spacing)
        if wrap:
            caption_label = label(caption, css="mwb-legend", wrap=True)
            caption_label.set_margin_bottom(2)
            self.frame.append(caption_label)
        else:
            self.frame.set_margin_top(LEGEND_OFFSET)
            caption_label = label(caption, css="mwb-legend")
            caption_label.set_halign(Gtk.Align.START)
            caption_label.set_valign(Gtk.Align.START)
            caption_label.set_margin_start(6)
            self.add_overlay(caption_label)
        self.frame.append(self.content)
        self.set_child(self.frame)

    def append(self, child: Gtk.Widget) -> None:
        self.content.append(child)
