"""Ubuntu top-bar integration using the StatusNotifierItem protocol.

Ubuntu's AppIndicator extension consumes the KDE StatusNotifierItem and
Canonical DBusMenu interfaces.  Exporting those interfaces directly keeps the
GTK 4 application independent of the GTK 3 AppIndicator libraries.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from pathlib import Path

from gi.repository import Gio, GLib

LOGGER = logging.getLogger(__name__)

WATCHER_NAME = "org.kde.StatusNotifierWatcher"
WATCHER_PATH = "/StatusNotifierWatcher"
WATCHER_INTERFACE = "org.kde.StatusNotifierWatcher"
ITEM_PATH = "/StatusNotifierItem"
ITEM_INTERFACE = "org.kde.StatusNotifierItem"
MENU_PATH = "/StatusNotifierItem/Menu"
MENU_INTERFACE = "com.canonical.dbusmenu"

ITEM_XML = f"""
<node>
  <interface name="{ITEM_INTERFACE}">
    <property name="Category" type="s" access="read"/>
    <property name="Id" type="s" access="read"/>
    <property name="Title" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="WindowId" type="i" access="read"/>
    <property name="IconThemePath" type="s" access="read"/>
    <property name="Menu" type="o" access="read"/>
    <property name="ItemIsMenu" type="b" access="read"/>
    <property name="IconName" type="s" access="read"/>
    <property name="IconPixmap" type="a(iiay)" access="read"/>
    <property name="OverlayIconName" type="s" access="read"/>
    <property name="OverlayIconPixmap" type="a(iiay)" access="read"/>
    <property name="AttentionIconName" type="s" access="read"/>
    <property name="AttentionIconPixmap" type="a(iiay)" access="read"/>
    <property name="AttentionMovieName" type="s" access="read"/>
    <method name="ContextMenu">
      <arg name="x" type="i" direction="in"/>
      <arg name="y" type="i" direction="in"/>
    </method>
    <method name="Activate">
      <arg name="x" type="i" direction="in"/>
      <arg name="y" type="i" direction="in"/>
    </method>
    <method name="ProvideXdgActivationToken">
      <arg name="token" type="s" direction="in"/>
    </method>
    <method name="SecondaryActivate">
      <arg name="x" type="i" direction="in"/>
      <arg name="y" type="i" direction="in"/>
    </method>
    <method name="Scroll">
      <arg name="delta" type="i" direction="in"/>
      <arg name="orientation" type="s" direction="in"/>
    </method>
  </interface>
</node>
"""

MENU_XML = f"""
<node>
  <interface name="{MENU_INTERFACE}">
    <property name="Version" type="u" access="read"/>
    <property name="TextDirection" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="IconThemePath" type="as" access="read"/>
    <method name="GetLayout">
      <arg type="i" name="parentId" direction="in"/>
      <arg type="i" name="recursionDepth" direction="in"/>
      <arg type="as" name="propertyNames" direction="in"/>
      <arg type="u" name="revision" direction="out"/>
      <arg type="(ia{{sv}}av)" name="layout" direction="out"/>
    </method>
    <method name="GetGroupProperties">
      <arg type="ai" name="ids" direction="in"/>
      <arg type="as" name="propertyNames" direction="in"/>
      <arg type="a(ia{{sv}})" name="properties" direction="out"/>
    </method>
    <method name="GetProperty">
      <arg type="i" name="id" direction="in"/>
      <arg type="s" name="name" direction="in"/>
      <arg type="v" name="value" direction="out"/>
    </method>
    <method name="Event">
      <arg type="i" name="id" direction="in"/>
      <arg type="s" name="eventId" direction="in"/>
      <arg type="v" name="data" direction="in"/>
      <arg type="u" name="timestamp" direction="in"/>
    </method>
    <method name="EventGroup">
      <arg type="a(isvu)" name="events" direction="in"/>
      <arg type="ai" name="idErrors" direction="out"/>
    </method>
    <method name="AboutToShow">
      <arg type="i" name="id" direction="in"/>
      <arg type="b" name="needUpdate" direction="out"/>
    </method>
    <method name="AboutToShowGroup">
      <arg type="ai" name="ids" direction="in"/>
      <arg type="ai" name="updatesNeeded" direction="out"/>
      <arg type="ai" name="idErrors" direction="out"/>
    </method>
  </interface>
</node>
"""

MENU_ITEMS = {
    1: "Open",
    2: "Settings",
    3: "Exit UI (keep sharing)",
}


def menu_properties(item_id: int, requested: Iterable[str] = ()) -> dict[str, GLib.Variant]:
    """Return DBusMenu properties for one static menu item."""

    if item_id == 0:
        properties = {"children-display": GLib.Variant("s", "submenu")}
    elif item_id in MENU_ITEMS:
        properties = {
            "label": GLib.Variant("s", MENU_ITEMS[item_id]),
            "enabled": GLib.Variant("b", True),
            "visible": GLib.Variant("b", True),
        }
    else:
        return {}
    names = set(requested)
    return (
        properties
        if not names
        else {key: value for key, value in properties.items() if key in names}
    )


def _menu_item(item_id: int) -> GLib.Variant:
    return GLib.Variant(
        "(ia{sv}av)",
        (item_id, menu_properties(item_id), []),
    )


def menu_layout() -> GLib.Variant:
    """Build the complete immutable top-bar DBusMenu layout."""

    root = GLib.Variant(
        "(ia{sv}av)",
        (0, menu_properties(0), [_menu_item(item_id) for item_id in MENU_ITEMS]),
    )
    return GLib.Variant.new_tuple(GLib.Variant.new_uint32(1), root)


class TopBarIndicator:
    """Export and register a top-bar item for the lifetime of the UI app."""

    def __init__(
        self,
        *,
        icon_name: str,
        on_open: Callable[[], None],
        on_settings: Callable[[], None],
        on_exit: Callable[[], None],
        icon_theme_path: Path,
    ) -> None:
        self.icon_name = icon_name
        self.icon_theme_path = str(icon_theme_path)
        self._actions = {1: on_open, 2: on_settings, 3: on_exit}
        self._connection: Gio.DBusConnection | None = None
        self._registrations: list[int] = []
        self._watch_id = 0
        self._registered = False
        self._activation_token = ""
        self._item_node: Gio.DBusNodeInfo | None = None
        self._menu_node: Gio.DBusNodeInfo | None = None

    @property
    def registered(self) -> bool:
        return self._registered

    def start(self) -> None:
        if self._connection is not None:
            return
        connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self._connection = connection
        self._item_node = Gio.DBusNodeInfo.new_for_xml(ITEM_XML)
        self._menu_node = Gio.DBusNodeInfo.new_for_xml(MENU_XML)
        self._registrations = [
            connection.register_object(
                ITEM_PATH,
                self._item_node.interfaces[0],
                self._on_item_method,
                self._on_item_property,
                None,
            ),
            connection.register_object(
                MENU_PATH,
                self._menu_node.interfaces[0],
                self._on_menu_method,
                self._on_menu_property,
                None,
            ),
        ]
        self._watch_id = Gio.bus_watch_name_on_connection(
            connection,
            WATCHER_NAME,
            Gio.BusNameWatcherFlags.NONE,
            self._on_watcher_appeared,
            self._on_watcher_vanished,
        )

    def stop(self) -> None:
        if self._watch_id:
            Gio.bus_unwatch_name(self._watch_id)
            self._watch_id = 0
        if self._connection is not None:
            for registration in self._registrations:
                self._connection.unregister_object(registration)
        self._registrations.clear()
        self._connection = None
        self._registered = False

    def _on_watcher_appeared(
        self, connection: Gio.DBusConnection, _name: str, _owner: str
    ) -> None:
        connection.call(
            WATCHER_NAME,
            WATCHER_PATH,
            WATCHER_INTERFACE,
            "RegisterStatusNotifierItem",
            GLib.Variant("(s)", (ITEM_PATH,)),
            None,
            Gio.DBusCallFlags.NONE,
            2000,
            None,
            self._on_registered,
            None,
        )

    def _on_registered(
        self, connection: Gio.DBusConnection, result: Gio.AsyncResult, _data: object
    ) -> None:
        try:
            connection.call_finish(result)
            self._registered = True
        except GLib.Error as exc:
            self._registered = False
            LOGGER.warning("could not register Ubuntu top-bar indicator: %s", exc)

    def _on_watcher_vanished(self, *_args: object) -> None:
        self._registered = False

    def _on_item_property(
        self,
        _connection: Gio.DBusConnection,
        _sender: str,
        _path: str,
        _interface: str,
        name: str,
    ) -> GLib.Variant | None:
        strings = {
            "Category": "Communications",
            "Id": "mouse-without-borders",
            "Title": "Mouse Without Borders",
            "Status": "Active",
            "IconThemePath": self.icon_theme_path,
            "Menu": MENU_PATH,
            "IconName": self.icon_name,
            "OverlayIconName": "",
            "AttentionIconName": "",
            "AttentionMovieName": "",
        }
        if name == "Menu":
            return GLib.Variant("o", strings[name])
        if name in strings:
            return GLib.Variant("s", strings[name])
        if name == "WindowId":
            return GLib.Variant("i", 0)
        if name == "ItemIsMenu":
            return GLib.Variant("b", False)
        if name in {"IconPixmap", "OverlayIconPixmap", "AttentionIconPixmap"}:
            return GLib.Variant("a(iiay)", [])
        return None

    def _on_item_method(
        self,
        _connection: Gio.DBusConnection,
        _sender: str,
        _path: str,
        _interface: str,
        method: str,
        parameters: GLib.Variant,
        invocation: Gio.DBusMethodInvocation,
    ) -> None:
        if method == "Activate":
            self._actions[1]()
        elif method == "ProvideXdgActivationToken":
            self._activation_token = parameters.unpack()[0]
        elif method in {"ContextMenu", "SecondaryActivate", "Scroll"}:
            pass
        else:
            invocation.return_dbus_error(
                "org.freedesktop.DBus.Error.UnknownMethod", f"Unknown method {method}"
            )
            return
        invocation.return_value(None)

    def _on_menu_property(
        self,
        _connection: Gio.DBusConnection,
        _sender: str,
        _path: str,
        _interface: str,
        name: str,
    ) -> GLib.Variant | None:
        if name == "Version":
            return GLib.Variant("u", 4)
        if name == "TextDirection":
            return GLib.Variant("s", "ltr")
        if name == "Status":
            return GLib.Variant("s", "normal")
        if name == "IconThemePath":
            return GLib.Variant("as", [self.icon_theme_path])
        return None

    def _on_menu_method(
        self,
        _connection: Gio.DBusConnection,
        _sender: str,
        _path: str,
        _interface: str,
        method: str,
        parameters: GLib.Variant,
        invocation: Gio.DBusMethodInvocation,
    ) -> None:
        values = parameters.unpack()
        if method == "GetLayout":
            invocation.return_value(menu_layout())
        elif method == "GetGroupProperties":
            ids, requested = values
            properties = [
                (item_id, menu_properties(item_id, requested))
                for item_id in ids
                if item_id == 0 or item_id in MENU_ITEMS
            ]
            invocation.return_value(GLib.Variant("(a(ia{sv}))", (properties,)))
        elif method == "GetProperty":
            item_id, name = values
            value = menu_properties(item_id).get(name)
            if value is None:
                invocation.return_dbus_error(
                    "com.canonical.dbusmenu.Error.UnknownProperty",
                    f"Menu item {item_id} has no property {name}",
                )
                return
            invocation.return_value(GLib.Variant("(v)", (value,)))
        elif method == "Event":
            item_id, event, _data, _timestamp = values
            invocation.return_value(None)
            GLib.idle_add(self.activate_menu_item, item_id, event)
        elif method == "EventGroup":
            events = values[0]
            errors = [
                item_id
                for item_id, event, _data, _timestamp in events
                if not self.can_activate(item_id, event)
            ]
            invocation.return_value(GLib.Variant("(ai)", (errors,)))
            for item_id, event, _data, _timestamp in events:
                if item_id not in errors:
                    GLib.idle_add(self.activate_menu_item, item_id, event)
        elif method == "AboutToShow":
            invocation.return_value(GLib.Variant("(b)", (False,)))
        elif method == "AboutToShowGroup":
            invocation.return_value(GLib.Variant("(aiai)", ([], [])))
        else:
            invocation.return_dbus_error(
                "org.freedesktop.DBus.Error.UnknownMethod", f"Unknown method {method}"
            )

    def activate_menu_item(self, item_id: int, event: str = "clicked") -> bool:
        """Run a menu action, returning whether the event was handled."""

        if not self.can_activate(item_id, event):
            return False
        self._actions[item_id]()
        return True

    def can_activate(self, item_id: int, event: str = "clicked") -> bool:
        """Return whether a DBusMenu event maps to an application action."""

        return item_id in self._actions and event in {"clicked", "activated"}
