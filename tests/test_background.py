"""Running in the background: the taskbar icon and starting at login.

The tray is spoken over D-Bus by hand (see :mod:`kairos.tray` for why), so
these tests drive the interface callbacks directly. That covers the parts that
can actually be wrong — the menu layout, the property values, dispatching a
click — without needing a real tray host, which no test machine is guaranteed
to have.
"""

import os
import tempfile
import unittest
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GdkPixbuf, GLib  # noqa: E402

Adw.init()

from kairos import APP_ID, autostart  # noqa: E402
from kairos.tray import MenuItem, TrayIcon, _to_argb  # noqa: E402


def a_tray(items=None, **kwargs) -> TrayIcon:
    return TrayIcon(
        icon_name=APP_ID,
        title="Kairos",
        tooltip="Calendar and reminders",
        items=items if items is not None else [
            MenuItem("Open Kairos", lambda: None),
            MenuItem("New event", lambda: None),
            MenuItem(separator=True),
            MenuItem("Quit Kairos", lambda: None),
        ],
        **kwargs,
    )


class ItemProperties(unittest.TestCase):
    """What a tray host reads to decide how to draw us."""

    def setUp(self):
        self.tray = a_tray()

    def get(self, name):
        return self.tray._on_item_property(None, None, None, None, name)

    def test_every_declared_property_has_a_value(self):
        """A missing one makes the whole icon fail to appear, silently."""
        declared = [
            "Category", "Id", "Title", "Status", "IconName", "IconPixmap",
            "IconThemePath", "AttentionIconName", "AttentionIconPixmap",
            "OverlayIconName", "OverlayIconPixmap", "ToolTip", "ItemIsMenu",
            "Menu", "WindowId",
        ]
        for name in declared:
            with self.subTest(property=name):
                self.assertIsNotNone(self.get(name), f"{name} returned nothing")

    def test_the_identity_is_our_application(self):
        self.assertEqual(self.get("Id").get_string(), APP_ID)
        self.assertEqual(self.get("IconName").get_string(), APP_ID)
        self.assertEqual(self.get("Title").get_string(), "Kairos")

    def test_it_advertises_itself_as_active(self):
        self.assertEqual(self.get("Status").get_string(), "Active")

    def test_it_points_at_its_menu(self):
        self.assertEqual(self.get("Menu").get_string(), "/MenuBar")
        self.assertFalse(self.get("ItemIsMenu").get_boolean())

    def test_the_tooltip_carries_the_text(self):
        _icon, _pixmaps, title, body = self.get("ToolTip").unpack()
        self.assertEqual(title, "Kairos")
        self.assertEqual(body, "Calendar and reminders")

    def test_an_unknown_property_returns_nothing_rather_than_raising(self):
        self.assertIsNone(self.get("NoSuchProperty"))


class MenuLayout(unittest.TestCase):
    def setUp(self):
        self.tray = a_tray()

    def test_the_layout_packs_as_the_protocol_expects(self):
        """If this signature is wrong the menu never appears."""
        variant = GLib.Variant("(u(ia{sv}av))", (1, self.tray._layout()))
        revision, root = variant.unpack()
        self.assertEqual(revision, 1)
        self.assertEqual(root[0], 0, "the root item must have id 0")

    def test_every_item_becomes_a_child(self):
        _root_id, _properties, children = self.tray._layout()
        self.assertEqual(len(children), 4)

    def test_children_are_numbered_from_one(self):
        _root_id, _properties, children = self.tray._layout()
        ids = [child.unpack()[0] for child in children]
        self.assertEqual(ids, [1, 2, 3, 4])

    def test_labels_come_through(self):
        _root_id, _properties, children = self.tray._layout()
        labels = [child.unpack()[1].get("label") for child in children]
        self.assertEqual(labels, ["Open Kairos", "New event", None, "Quit Kairos"])

    def test_a_separator_is_marked_as_one(self):
        _root_id, _properties, children = self.tray._layout()
        self.assertEqual(children[2].unpack()[1].get("type"), "separator")

    def test_group_properties_covers_every_item(self):
        captured = []
        self.tray._on_menu_method(
            None, None, None, None, "GetGroupProperties",
            GLib.Variant("(aias)", ([], [])),
            _Invocation(captured),
        )
        entries = captured[0].unpack()[0]
        self.assertEqual(len(entries), 4)

    def test_the_menu_version_is_declared(self):
        version = self.tray._on_menu_property(None, None, None, None, "Version")
        self.assertEqual(version.get_uint32(), 3)


class ClickingTheMenu(unittest.TestCase):
    def setUp(self):
        self.clicked = []
        self.tray = a_tray(items=[
            MenuItem("First", lambda: self.clicked.append("first")),
            MenuItem(separator=True),
            MenuItem("Second", lambda: self.clicked.append("second")),
        ])

    def send_event(self, identifier, event="clicked"):
        self.tray._on_menu_method(
            None, None, None, None, "Event",
            GLib.Variant("(isvu)", (identifier, event, GLib.Variant("i", 0), 0)),
            _Invocation([]),
        )
        # Callbacks are deferred to the main loop so a slow action cannot
        # block the tray; drain it.
        context = GLib.MainContext.default()
        while context.pending():
            context.iteration(False)

    def test_clicking_an_item_runs_its_action(self):
        self.send_event(1)
        self.assertEqual(self.clicked, ["first"])

    def test_the_right_item_runs(self):
        self.send_event(3)
        self.assertEqual(self.clicked, ["second"])

    def test_clicking_a_separator_does_nothing(self):
        self.send_event(2)
        self.assertEqual(self.clicked, [])

    def test_an_out_of_range_id_is_ignored(self):
        self.send_event(99)
        self.assertEqual(self.clicked, [])

    def test_other_events_do_not_trigger_actions(self):
        self.send_event(1, event="hovered")
        self.assertEqual(self.clicked, [])

    def test_a_failing_action_does_not_escape(self):
        tray = a_tray(items=[MenuItem("Boom", lambda: 1 / 0)])
        tray._on_menu_method(
            None, None, None, None, "Event",
            GLib.Variant("(isvu)", (1, "clicked", GLib.Variant("i", 0), 0)),
            _Invocation([]),
        )
        context = GLib.MainContext.default()
        while context.pending():
            context.iteration(False)      # must not raise


class LeftClickingTheIcon(unittest.TestCase):
    def test_activate_calls_the_handler(self):
        opened = []
        tray = a_tray(on_activate=lambda: opened.append(True))
        tray._on_item_method(None, None, None, None, "Activate",
                             GLib.Variant("(ii)", (0, 0)), _Invocation([]))
        self.assertEqual(opened, [True])

    def test_scrolling_is_harmless(self):
        tray = a_tray(on_activate=lambda: None)
        tray._on_item_method(None, None, None, None, "Scroll",
                             GLib.Variant("(is)", (1, "vertical")), _Invocation([]))


class IconPixels(unittest.TestCase):
    """Naming the icon is not enough; the pixels have to be right too."""

    def test_a_pixbuf_becomes_argb(self):
        pixbuf = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, True, 8, 2, 2)
        pixbuf.fill(0xFF8000FF)          # opaque orange, RGBA
        width, height, data = _to_argb(pixbuf)

        self.assertEqual((width, height), (2, 2))
        self.assertEqual(len(data), 2 * 2 * 4)
        self.assertEqual(tuple(data[:4]), (0xFF, 0xFF, 0x80, 0x00),
                         "expected alpha, red, green, blue in that order")

    def test_the_real_icons_convert(self):
        root = Path(__file__).resolve().parent.parent / "data" / "icons" / "hicolor"
        files = [root / f"{size}x{size}" / "apps" / f"{APP_ID}.png" for size in (24, 48)]
        tray = a_tray(icon_files=files)
        pixmaps = tray._icon_pixmaps()

        self.assertEqual([(w, h) for w, h, _ in pixmaps], [(24, 24), (48, 48)])
        for width, height, data in pixmaps:
            self.assertEqual(len(data), width * height * 4)

    def test_a_missing_icon_file_is_skipped_rather_than_fatal(self):
        tray = a_tray(icon_files=["/nowhere/at/all.png"])
        self.assertEqual(tray._icon_pixmaps(), [])

    def test_pixmaps_are_only_built_once(self):
        root = Path(__file__).resolve().parent.parent / "data" / "icons" / "hicolor"
        tray = a_tray(icon_files=[root / "24x24" / "apps" / f"{APP_ID}.png"])
        self.assertIs(tray._icon_pixmaps(), tray._icon_pixmaps())


class StartingAtLogin(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="kairos-autostart-")
        self.previous = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = self.directory

    def tearDown(self):
        if self.previous is None:
            os.environ.pop("XDG_CONFIG_HOME", None)
        else:
            os.environ["XDG_CONFIG_HOME"] = self.previous

    def test_it_starts_switched_off(self):
        self.assertFalse(autostart.is_enabled())

    def test_enabling_writes_the_desktop_file(self):
        self.assertTrue(autostart.set_enabled(True))
        self.assertTrue(autostart.is_enabled())
        self.assertTrue(autostart.desktop_file().is_file())

    def test_the_file_is_a_valid_desktop_entry(self):
        autostart.set_enabled(True)
        text = autostart.desktop_file().read_text()
        self.assertTrue(text.startswith("[Desktop Entry]"))
        for key in ("Type=Application", "Name=Kairos", "Exec=", f"Icon={APP_ID}"):
            self.assertIn(key, text)

    def test_it_starts_hidden(self):
        """Logging in should not throw a calendar window at you."""
        autostart.set_enabled(True)
        self.assertIn("--background", autostart.desktop_file().read_text())

    def test_disabling_removes_it(self):
        autostart.set_enabled(True)
        self.assertTrue(autostart.set_enabled(False))
        self.assertFalse(autostart.is_enabled())
        self.assertFalse(autostart.desktop_file().exists())

    def test_disabling_when_already_off_is_fine(self):
        self.assertTrue(autostart.set_enabled(False))

    def test_enabling_twice_is_fine(self):
        autostart.set_enabled(True)
        self.assertTrue(autostart.set_enabled(True))

    def test_the_command_is_absolute_or_on_the_path(self):
        command = autostart.startup_command()
        self.assertIn("--background", command)
        self.assertTrue(
            command.startswith(("/", '"', "env ")),
            f"a login command must not depend on the working directory: {command}",
        )


class _Invocation:
    """The bit of Gio.DBusMethodInvocation the code under test uses."""

    def __init__(self, captured):
        self.captured = captured

    def return_value(self, variant):
        self.captured.append(variant)


if __name__ == "__main__":
    unittest.main()
