"""A taskbar (system tray) icon, spoken over D-Bus directly.

Kairos keeps running after you close its window so that reminders still
arrive, and this is what shows you it is still there.

**Why there is no library here.**  The usual answer on Linux is
``libayatana-appindicator``, and the usual answer on Mint is ``XApp``. Both
are built against GTK 3, and GTK 3 and GTK 4 cannot be loaded into the same
process — importing either from a GTK 4 application fails outright with
"Requiring namespace 'Gtk' version '3.0', but '4.0' is already loaded". So
Kairos talks the tray protocol itself.

That protocol is two D-Bus interfaces:

``org.kde.StatusNotifierItem``
    the icon: what it looks like, what it is called, and what happens when it
    is clicked. We register it with ``org.kde.StatusNotifierWatcher``, which
    every tray implementation provides.

``com.canonical.dbusmenu``
    the right-click menu. Only the handful of methods a tray host actually
    calls are implemented; the layout is a flat list of items, which is all a
    tray menu ever needs.

Nothing here is required for Kairos to work. If there is no watcher on the bus
— a bare window manager, a GNOME session without an extension — the icon
simply does not appear, :attr:`TrayIcon.available` stays False, and the rest
of the application carries on. The watcher is also watched for, so an icon
appears if the tray applet starts after Kairos does, which is common at login.
"""

from __future__ import annotations

import logging
import os

from gi.repository import GdkPixbuf, Gio, GLib

log = logging.getLogger(__name__)

WATCHER_NAME = "org.kde.StatusNotifierWatcher"
WATCHER_PATH = "/StatusNotifierWatcher"
WATCHER_INTERFACE = "org.kde.StatusNotifierWatcher"

ITEM_PATH = "/StatusNotifierItem"
MENU_PATH = "/MenuBar"

#: The item interface, trimmed to what a tray host actually reads. Hosts
#: fetch properties through org.freedesktop.DBus.Properties, which GDBus
#: implements for us as long as the properties are declared here.
ITEM_XML = """
<node>
  <interface name="org.kde.StatusNotifierItem">
    <property name="Category" type="s" access="read"/>
    <property name="Id" type="s" access="read"/>
    <property name="Title" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="IconName" type="s" access="read"/>
    <property name="IconPixmap" type="a(iiay)" access="read"/>
    <property name="IconThemePath" type="s" access="read"/>
    <property name="AttentionIconName" type="s" access="read"/>
    <property name="AttentionIconPixmap" type="a(iiay)" access="read"/>
    <property name="OverlayIconName" type="s" access="read"/>
    <property name="OverlayIconPixmap" type="a(iiay)" access="read"/>
    <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
    <property name="ItemIsMenu" type="b" access="read"/>
    <property name="Menu" type="o" access="read"/>
    <property name="WindowId" type="i" access="read"/>
    <method name="Activate">
      <arg name="x" type="i" direction="in"/>
      <arg name="y" type="i" direction="in"/>
    </method>
    <method name="SecondaryActivate">
      <arg name="x" type="i" direction="in"/>
      <arg name="y" type="i" direction="in"/>
    </method>
    <method name="ContextMenu">
      <arg name="x" type="i" direction="in"/>
      <arg name="y" type="i" direction="in"/>
    </method>
    <method name="Scroll">
      <arg name="delta" type="i" direction="in"/>
      <arg name="orientation" type="s" direction="in"/>
    </method>
    <signal name="NewIcon"/>
    <signal name="NewStatus"><arg name="status" type="s"/></signal>
    <signal name="NewTitle"/>
    <signal name="NewToolTip"/>
  </interface>
</node>
"""

#: The menu interface.
#:
#: Every method here is required, including the ones that look like
#: duplicates. ``EventGroup`` and ``AboutToShowGroup`` are the version-3 batch
#: forms, and libdbusmenu — the client behind most panels, including Mint's
#: xapp-sn-watcher — uses them in preference to the singular forms whenever
#: the server says ``Version = 3``, as we do.
#:
#: Leaving one out does not produce an error anyone sees. GDBus rejects a call
#: to an undeclared method before it reaches our handler, and libdbusmenu
#: discards the failure, so the menu draws perfectly and clicking it does
#: nothing at all.
MENU_XML = """
<node>
  <interface name="com.canonical.dbusmenu">
    <property name="Version" type="u" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="TextDirection" type="s" access="read"/>
    <property name="IconThemePath" type="as" access="read"/>
    <method name="GetLayout">
      <arg name="parentId" type="i" direction="in"/>
      <arg name="recursionDepth" type="i" direction="in"/>
      <arg name="propertyNames" type="as" direction="in"/>
      <arg name="revision" type="u" direction="out"/>
      <arg name="layout" type="(ia{sv}av)" direction="out"/>
    </method>
    <method name="GetGroupProperties">
      <arg name="ids" type="ai" direction="in"/>
      <arg name="propertyNames" type="as" direction="in"/>
      <arg name="properties" type="a(ia{sv})" direction="out"/>
    </method>
    <method name="GetProperty">
      <arg name="id" type="i" direction="in"/>
      <arg name="name" type="s" direction="in"/>
      <arg name="value" type="v" direction="out"/>
    </method>
    <method name="Event">
      <arg name="id" type="i" direction="in"/>
      <arg name="eventId" type="s" direction="in"/>
      <arg name="data" type="v" direction="in"/>
      <arg name="timestamp" type="u" direction="in"/>
    </method>
    <method name="EventGroup">
      <arg name="events" type="a(isvu)" direction="in"/>
      <arg name="idErrors" type="ai" direction="out"/>
    </method>
    <method name="AboutToShow">
      <arg name="id" type="i" direction="in"/>
      <arg name="needUpdate" type="b" direction="out"/>
    </method>
    <method name="AboutToShowGroup">
      <arg name="ids" type="ai" direction="in"/>
      <arg name="updatesNeeded" type="ai" direction="out"/>
      <arg name="idErrors" type="ai" direction="out"/>
    </method>
    <signal name="LayoutUpdated">
      <arg name="revision" type="u"/>
      <arg name="parent" type="i"/>
    </signal>
  </interface>
</node>
"""


class MenuItem:
    """One entry in the tray menu.

    ``callback`` takes no arguments. A separator is an item with no label.
    """

    def __init__(self, label: str = "", callback=None, separator: bool = False) -> None:
        self.label = label
        self.callback = callback
        self.separator = separator


class TrayIcon:
    """The icon Kairos puts in the taskbar, if the desktop has one.

    Construct it with the menu you want and call :meth:`start`. Everything is
    best-effort: any failure leaves :attr:`available` False and is logged at
    debug level, because a missing tray is a normal state of affairs, not an
    error the user needs to hear about.
    """

    def __init__(self, *, icon_name: str, title: str, tooltip: str,
                 items: list[MenuItem], on_activate=None,
                 icon_theme_path: str = "", icon_files: list = ()) -> None:
        self.icon_name = icon_name
        self.title = title
        self.tooltip = tooltip
        self.items = items
        self.on_activate = on_activate
        self.icon_theme_path = icon_theme_path
        #: PNG files to hand over as raw pixels. Naming the icon is not
        #: enough: the tray looks that name up in *its own* icon theme, which
        #: will not contain ours when Kairos runs from a checkout or an
        #: AppImage — and the icon then silently comes out blank. Sending the
        #: pixels as well means it draws whatever happens.
        self.icon_files = list(icon_files)
        self._pixmaps: list | None = None

        self.available = False
        self._connection: Gio.DBusConnection | None = None
        self._bus_name = f"org.kde.StatusNotifierItem-{os.getpid()}-1"
        self._name_id = 0
        self._watch_id = 0
        self._item_registration = 0
        self._menu_registration = 0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Claim a bus name and wait for a tray to register with."""
        try:
            self._connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        except GLib.Error as exc:
            log.debug("no session bus, so no tray icon: %s", exc.message)
            return

        try:
            self._export()
        except GLib.Error as exc:
            log.debug("could not export the tray interfaces: %s", exc.message)
            return

        self._name_id = Gio.bus_own_name_on_connection(
            self._connection, self._bus_name,
            Gio.BusNameOwnerFlags.NONE, None, None,
        )

        # Register now if a tray is already there, and again whenever one
        # appears — at login the panel often starts after the apps do.
        self._watch_id = Gio.bus_watch_name_on_connection(
            self._connection, WATCHER_NAME, Gio.BusNameWatcherFlags.NONE,
            lambda *_: self._register_with_watcher(), None,
        )

    def stop(self) -> None:
        if self._watch_id:
            Gio.bus_unwatch_name(self._watch_id)
            self._watch_id = 0
        if self._name_id:
            Gio.bus_unown_name(self._name_id)
            self._name_id = 0
        if self._connection is not None:
            for registration in (self._item_registration, self._menu_registration):
                if registration:
                    self._connection.unregister_object(registration)
            self._item_registration = self._menu_registration = 0
        self.available = False

    # ------------------------------------------------------------------
    # Exporting the two interfaces
    # ------------------------------------------------------------------

    def _export(self) -> None:
        item_node = Gio.DBusNodeInfo.new_for_xml(ITEM_XML)
        self._item_registration = self._connection.register_object(
            ITEM_PATH, item_node.interfaces[0],
            self._on_item_method, self._on_item_property, None,
        )

        menu_node = Gio.DBusNodeInfo.new_for_xml(MENU_XML)
        self._menu_registration = self._connection.register_object(
            MENU_PATH, menu_node.interfaces[0],
            self._on_menu_method, self._on_menu_property, None,
        )

    def _register_with_watcher(self) -> None:
        """Tell the tray we exist. Harmless to repeat."""
        self._connection.call(
            WATCHER_NAME, WATCHER_PATH, WATCHER_INTERFACE,
            "RegisterStatusNotifierItem",
            GLib.Variant("(s)", (self._bus_name,)),
            None, Gio.DBusCallFlags.NONE, 3000, None,
            self._on_registered,
        )

    def _on_registered(self, connection, result) -> None:
        try:
            connection.call_finish(result)
        except GLib.Error as exc:
            log.debug("tray did not accept our icon: %s", exc.message)
            return
        self.available = True
        log.info("tray icon registered")

    # ------------------------------------------------------------------
    # StatusNotifierItem
    # ------------------------------------------------------------------

    # PyGObject drops the GError out-parameter, so this takes five arguments
    # and not the six the C signature suggests. Getting it wrong makes every
    # property read fail silently and no icon ever appears.
    def _on_item_property(self, _conn, _sender, _path, _iface, name):
        values = {
            "Category": GLib.Variant("s", "ApplicationStatus"),
            "Id": GLib.Variant("s", self.icon_name),
            "Title": GLib.Variant("s", self.title),
            "Status": GLib.Variant("s", "Active"),
            "IconName": GLib.Variant("s", self.icon_name),
            "IconPixmap": GLib.Variant("a(iiay)", self._icon_pixmaps()),
            "IconThemePath": GLib.Variant("s", self.icon_theme_path),
            "AttentionIconName": GLib.Variant("s", ""),
            "AttentionIconPixmap": GLib.Variant("a(iiay)", []),
            "OverlayIconName": GLib.Variant("s", ""),
            "OverlayIconPixmap": GLib.Variant("a(iiay)", []),
            # (icon name, pixmaps, title, body). Pixmaps stay empty; the icon
            # name is enough when the icon is installed in the theme.
            "ToolTip": GLib.Variant("(sa(iiay)ss)",
                                    (self.icon_name, [], self.title, self.tooltip)),
            "ItemIsMenu": GLib.Variant("b", False),
            "Menu": GLib.Variant("o", MENU_PATH),
            "WindowId": GLib.Variant("i", 0),
        }
        return values.get(name)

    def _icon_pixmaps(self) -> list:
        """Our icon as ``(width, height, ARGB bytes)``, one entry per size.

        The tray protocol wants ARGB32 in network byte order, which is not a
        layout any image library hands you, so the conversion is done here
        once and cached.
        """
        if self._pixmaps is not None:
            return self._pixmaps

        self._pixmaps = []
        for path in self.icon_files:
            try:
                pixbuf = GdkPixbuf.Pixbuf.new_from_file(str(path))
            except Exception as exc:
                log.debug("could not read the tray icon %s: %s", path, exc)
                continue
            self._pixmaps.append(_to_argb(pixbuf))
        return self._pixmaps

    def _on_item_method(self, _conn, _sender, _path, _iface, method, _params, invocation):
        if method in ("Activate", "SecondaryActivate"):
            if self.on_activate is not None:
                self.on_activate()
        # ContextMenu and Scroll need no reply beyond an acknowledgement; the
        # host draws the menu itself from the dbusmenu we export.
        invocation.return_value(None)

    # ------------------------------------------------------------------
    # com.canonical.dbusmenu
    # ------------------------------------------------------------------

    def _on_menu_property(self, _conn, _sender, _path, _iface, name):
        return {
            "Version": GLib.Variant("u", 3),
            "Status": GLib.Variant("s", "normal"),
            "TextDirection": GLib.Variant("s", "ltr"),
            "IconThemePath": GLib.Variant("as", []),
        }.get(name)

    def _item_properties(self, item: MenuItem) -> dict:
        if item.separator:
            return {"type": GLib.Variant("s", "separator")}
        return {
            "label": GLib.Variant("s", item.label),
            "enabled": GLib.Variant("b", True),
            "visible": GLib.Variant("b", True),
        }

    def _layout(self) -> tuple:
        """The whole menu: a root item with one child per entry.

        Item ids are one-based indices into :attr:`items`, so an Event can be
        turned straight back into the entry that was clicked. Returned as a
        plain tuple so the caller packs it exactly once — round-tripping it
        through a Variant would strip the types out of the ``av`` children.
        """
        children = [
            GLib.Variant("(ia{sv}av)", (index + 1, self._item_properties(item), []))
            for index, item in enumerate(self.items)
        ]
        return (0, {"children-display": GLib.Variant("s", "submenu")}, children)

    def _activate(self, identifier: int, event_id: str) -> bool:
        """Run the action for a clicked item. Returns whether it was ours.

        The return value is what ``EventGroup`` reports back as ``idErrors``,
        so a host can tell which of a batch of events went nowhere.
        """
        if event_id != "clicked" or not 1 <= identifier <= len(self.items):
            return False
        item = self.items[identifier - 1]
        if item.callback is None:
            return False
        # Run it from the main loop rather than from inside the D-Bus call, so
        # a slow action cannot make the tray host think we have hung.
        GLib.idle_add(_run_once, item.callback)
        return True

    def _on_menu_method(self, _conn, _sender, _path, _iface, method, params, invocation):
        if method == "GetLayout":
            invocation.return_value(GLib.Variant("(u(ia{sv}av))", (1, self._layout())))

        elif method == "GetGroupProperties":
            ids = params.unpack()[0]
            wanted = ids or list(range(1, len(self.items) + 1))
            entries = [
                (identifier, self._item_properties(self.items[identifier - 1]))
                for identifier in wanted
                if 1 <= identifier <= len(self.items)
            ]
            invocation.return_value(GLib.Variant("(a(ia{sv}))", (entries,)))

        elif method == "GetProperty":
            identifier, name = params.unpack()
            properties = ({} if not 1 <= identifier <= len(self.items)
                          else self._item_properties(self.items[identifier - 1]))
            invocation.return_value(
                GLib.Variant("(v)", (properties.get(name, GLib.Variant("s", "")),))
            )

        elif method == "Event":
            identifier, event_id = params.unpack()[:2]
            self._activate(identifier, event_id)
            invocation.return_value(None)

        elif method == "EventGroup":
            # The batched form. This is the one libdbusmenu actually uses.
            failed = [
                identifier
                for identifier, event_id, _data, _timestamp in params.unpack()[0]
                if not self._activate(identifier, event_id)
            ]
            invocation.return_value(GLib.Variant("(ai)", (failed,)))

        elif method == "AboutToShow":
            invocation.return_value(GLib.Variant("(b)", (False,)))

        elif method == "AboutToShowGroup":
            # Nothing ever needs updating: the menu is built once, and its
            # items do not change while it is open.
            invocation.return_value(GLib.Variant("(aiai)", ([], [])))

        else:
            log.debug("unhandled tray menu method %s", method)
            invocation.return_value(None)


def _to_argb(pixbuf: GdkPixbuf.Pixbuf) -> tuple:
    """Convert a pixbuf to the (width, height, ARGB bytes) the tray expects."""
    width = pixbuf.get_width()
    height = pixbuf.get_height()
    rowstride = pixbuf.get_rowstride()
    channels = pixbuf.get_n_channels()
    pixels = pixbuf.get_pixels()

    argb = bytearray(width * height * 4)
    out = 0
    for y in range(height):
        row = y * rowstride
        for x in range(width):
            offset = row + x * channels
            alpha = pixels[offset + 3] if channels == 4 else 255
            argb[out] = alpha
            argb[out + 1] = pixels[offset]
            argb[out + 2] = pixels[offset + 1]
            argb[out + 3] = pixels[offset + 2]
            out += 4
    return (width, height, bytes(argb))


def _run_once(callback) -> bool:
    try:
        callback()
    except Exception:
        log.exception("a tray menu action failed")
    return GLib.SOURCE_REMOVE
