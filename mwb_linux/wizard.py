"""The blue "setup experience" screens shown from the settings form."""

from __future__ import annotations

from typing import Protocol

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402

from .widgets import (
    horizontal,
    image_from_asset,
    label,
    link_button,
    vertical,
)

LET_S_GET_STARTED = (
    "We need to know if you have already set up Mouse w/o Borders on the "
    "computer you want to link to."
)
ALREADY_INSTALLED = (
    "Have you already installed Mouse without Borders on another computer?"
)
ALMOST_DONE_TEXT = (
    "Just keep this window open or write down the information below. Then head "
    "over to your other computer and install Mouse w/o Borders. You can finish "
    "the setup and configuration over there."
)
ALL_DONE_TEXT = "Now you're all done over here!"
SECURITY_CODE_HELP = (
    "The security key is shown in the Machine Setup tab of Mouse without "
    "Borders on the other computer."
)


class WizardHost(Protocol):
    """Callbacks the wizard needs from the settings form."""

    def close_wizard(self) -> None: ...

    def show_wizard_page(self, name: str) -> None: ...

    def link_to_machine(self, security_code: str, machine_name: str) -> None: ...

    def take_security_key(self) -> str: ...

    def local_machine_name(self) -> str: ...

    def report(self, message: str) -> None: ...


def _divider() -> Gtk.Box:
    line = Gtk.Box()
    line.add_css_class("mwb-setup-divider")
    line.set_size_request(-1, 1)
    return line


def _hero() -> Gtk.Image:
    return image_from_asset("hero.svg", 52)


def _circle_button(text: str, on_click) -> Gtk.Button:
    button = Gtk.Button(label=text)
    button.add_css_class("mwb-setup-circle")
    button.set_halign(Gtk.Align.CENTER)
    button.set_valign(Gtk.Align.CENTER)
    button.connect("clicked", lambda *_: on_click())
    return button


def _paragraph(text: str) -> Gtk.Label:
    widget = label(text, css="mwb-setup-text", wrap=True, xalign=0.5, justify=Gtk.Justification.CENTER)
    widget.set_max_width_chars(52)
    return widget


class _Screen(Gtk.Box):
    """Shared frame: top navigation, centred content, bottom actions."""

    def __init__(self, host: WizardHost, *, back_page: str | None) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.host = host
        self.add_css_class("mwb-setup-body")

        top = vertical(4)
        top.set_halign(Gtk.Align.START)
        if back_page:
            back = Gtk.Button()
            back.add_css_class("mwb-setup-back")
            back.set_child(label("‹", xalign=0.5))
            back.connect("clicked", lambda *_: host.show_wizard_page(back_page))
            top.append(back)
        skip = link_button("Skip", host.close_wizard, css="mwb-setup-link")
        skip.set_halign(Gtk.Align.START)
        skip.set_margin_start(2)
        top.append(skip)
        self.append(top)

        self.middle = vertical(0)
        self.middle.set_vexpand(True)
        self.middle.set_valign(Gtk.Align.CENTER)
        self.append(self.middle)

        self.actions = horizontal(24)
        self.actions.set_halign(Gtk.Align.CENTER)
        self.actions.set_margin_top(12)
        self.append(self.actions)


class StartScreen(_Screen):
    def __init__(self, host: WizardHost) -> None:
        super().__init__(host, back_page=None)
        hero = _hero()
        hero.set_margin_bottom(10)
        self.middle.append(hero)
        self.middle.append(_divider())
        heading = label("Let's get started", css="mwb-setup-heading", xalign=0.5)
        heading.set_margin_top(12)
        heading.set_margin_bottom(12)
        self.middle.append(heading)
        self.middle.append(_divider())
        text = vertical(10)
        text.set_margin_top(14)
        text.append(_paragraph(LET_S_GET_STARTED))
        text.append(_paragraph(ALREADY_INSTALLED))
        self.middle.append(text)
        self.actions.append(_circle_button("YES", lambda: host.show_wizard_page("link")))
        self.actions.append(_circle_button("NO", lambda: host.show_wizard_page("almost-done")))


class LinkScreen(_Screen):
    def __init__(self, host: WizardHost) -> None:
        super().__init__(host, back_page="start")
        heading = label(
            "Just one more step\nand your computers\nwill be linked!",
            css=("mwb-setup-heading", "small"),
            xalign=0.5,
            justify=Gtk.Justification.CENTER,
        )
        heading.set_margin_bottom(16)
        self.middle.append(heading)
        self.middle.append(_divider())

        form = vertical(14)
        form.set_margin_top(18)
        form.set_size_request(330, -1)
        form.set_halign(Gtk.Align.CENTER)

        code_group = vertical(4)
        code_header = horizontal(0)
        code_header.append(label("SECURITY CODE", css="mwb-setup-info-label"))
        help_link = link_button(
            "Where can I find this? \u2295",
            lambda: host.report(SECURITY_CODE_HELP),
            css="mwb-setup-link",
        )
        help_link.add_css_class("mwb-setup-help-link")
        help_link.set_halign(Gtk.Align.END)
        help_link.set_hexpand(True)
        code_header.append(help_link)
        code_group.append(code_header)
        self.security_code = Gtk.Entry()
        self.security_code.add_css_class("mwb-setup-input")
        code_group.append(self.security_code)
        form.append(code_group)

        name_group = vertical(4)
        name_group.append(label("OTHER COMPUTER'S NAME", css="mwb-setup-info-label"))
        self.machine_name = Gtk.Entry()
        self.machine_name.add_css_class("mwb-setup-input")
        self.machine_name.add_css_class("secondary")
        name_group.append(self.machine_name)
        form.append(name_group)
        self.middle.append(form)

        self.actions.append(_circle_button("LINK", self._link))

    def _link(self) -> None:
        self.host.link_to_machine(
            self.security_code.get_text().strip(),
            self.machine_name.get_text().strip(),
        )

    def prepare(self, security_code: str, machine_name: str) -> None:
        self.security_code.set_text(security_code)
        self.machine_name.set_text(machine_name)
        self.security_code.grab_focus()


class AlmostDoneScreen(_Screen):
    def __init__(self, host: WizardHost) -> None:
        super().__init__(host, back_page="start")
        heading = label("Almost done", css="mwb-setup-heading", xalign=0.5)
        heading.set_margin_bottom(10)
        self.middle.append(heading)
        self.middle.append(_divider())
        text = vertical(10)
        text.set_margin_top(12)
        text.set_margin_bottom(12)
        text.append(_paragraph(ALMOST_DONE_TEXT))
        text.append(_paragraph(ALL_DONE_TEXT))
        self.middle.append(text)

        self.security_code = label("", css="mwb-setup-info-value", xalign=0.5)
        self.machine_name = label("", css="mwb-setup-info-value", xalign=0.5)
        for caption, value in (
            ("SECURITY CODE", self.security_code),
            ("THIS COMPUTER'S NAME", self.machine_name),
        ):
            group = vertical(2)
            group.set_margin_top(14)
            group.append(label(caption, css="mwb-setup-info-label", xalign=0.5))
            group.append(value)
            self.middle.append(group)

        divider = _divider()
        divider.set_margin_top(16)
        self.middle.append(divider)
        hero = _hero()
        hero.set_margin_top(6)
        self.actions.append(hero)

    def prepare(self, security_code: str, machine_name: str) -> None:
        self.security_code.set_text(security_code)
        self.machine_name.set_text(machine_name)


class SetupWizard(Gtk.Stack):
    """The three-page setup experience reached from the Machine Setup tab."""

    def __init__(self, host: WizardHost) -> None:
        super().__init__()
        self.host = host
        self.start = StartScreen(host)
        self.link = LinkScreen(host)
        self.almost_done = AlmostDoneScreen(host)
        self.add_named(self.start, "start")
        self.add_named(self.link, "link")
        self.add_named(self.almost_done, "almost-done")

    def show_page(self, name: str) -> None:
        if name == "almost-done":
            self.almost_done.prepare(
                self.host.take_security_key(), self.host.local_machine_name()
            )
        elif name == "link":
            self.link.prepare("", "")
        self.set_visible_child_name(name)
