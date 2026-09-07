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


class TheMenuInterface(unittest.TestCase):
    """Every method a real client might call must be *declared*.

    This is the class of bug that cost an afternoon: GDBus rejects a call to a
    method missing from the interface XML before our handler ever runs, and
    libdbusmenu discards the failure. The menu drew perfectly and every item
    did nothing, with not one line in any log.

    So these check the declaration, not just the handler. A handler-level test
    would have passed throughout.
    """

    #: What libdbusmenu and the JS clients between them call. The batch forms
    #: are not optional: libdbusmenu prefers them whenever the server reports
    #: Version 3, which we do.
    REQUIRED = (
        "GetLayout", "GetGroupProperties", "GetProperty",
        "Event", "EventGroup", "AboutToShow", "AboutToShowGroup",
    )

    def setUp(self):
        from gi.repository import Gio
        from kairos.tray import MENU_XML
        self.interface = Gio.DBusNodeInfo.new_for_xml(MENU_XML).interfaces[0]
        self.declared = {m.name for m in self.interface.methods}

    def test_every_required_method_is_declared(self):
        for name in self.REQUIRED:
            with self.subTest(method=name):
                self.assertIn(name, self.declared,
                              f"{name} is missing from the interface, so GDBus "
                              f"will reject it before the handler runs")

    def test_the_batch_forms_have_the_right_signatures(self):
        """A wrong signature fails just as silently as a missing method."""
        expected = {
            "EventGroup": ("a(isvu)", "ai"),
            "AboutToShowGroup": ("ai", "aiai"),
            "Event": ("isvu", ""),
            "AboutToShow": ("i", "b"),
        }
        by_name = {m.name: m for m in self.interface.methods}
        for name, (args_in, args_out) in expected.items():
            with self.subTest(method=name):
                method = by_name[name]
                self.assertEqual("".join(a.signature for a in method.in_args), args_in)
                self.assertEqual("".join(a.signature for a in method.out_args), args_out)

    def test_the_version_we_claim_matches_what_we_implement(self):
        """Claiming 3 is what makes clients use the batch forms."""
        tray = a_tray()
        version = tray._on_menu_property(None, None, None, None, "Version").get_uint32()
        self.assertEqual(version, 3)
        if version >= 3:
            self.assertIn("EventGroup", self.declared)
            self.assertIn("AboutToShowGroup", self.declared)

    def test_every_declared_method_is_answered(self):
        """No declared method may fall through and silently do nothing."""
        from gi.repository import GLib
        payloads = {
            "GetLayout": GLib.Variant("(iias)", (0, -1, [])),
            "GetGroupProperties": GLib.Variant("(aias)", ([], [])),
            "GetProperty": GLib.Variant("(is)", (1, "label")),
            "Event": GLib.Variant("(isvu)", (1, "clicked", GLib.Variant("i", 0), 0)),
            "EventGroup": GLib.Variant("(a(isvu))",
                                       ([(1, "clicked", GLib.Variant("i", 0), 0)],)),
            "AboutToShow": GLib.Variant("(i)", (0,)),
            "AboutToShowGroup": GLib.Variant("(ai)", ([1],)),
        }
        tray = a_tray()
        for name in self.REQUIRED:
            with self.subTest(method=name):
                captured = []
                tray._on_menu_method(None, None, None, None, name,
                                     payloads[name], _Invocation(captured))
                self.assertEqual(len(captured), 1,
                                 f"{name} did not reply at all")


class ClickingTheMenuInBatches(unittest.TestCase):
    """EventGroup is the path a real panel takes."""

    def setUp(self):
        self.clicked = []
        self.tray = a_tray(items=[
            MenuItem("First", lambda: self.clicked.append("first")),
            MenuItem(separator=True),
            MenuItem("Second", lambda: self.clicked.append("second")),
        ])

    def send_group(self, events):
        captured = []
        self.tray._on_menu_method(
            None, None, None, None, "EventGroup",
            GLib.Variant("(a(isvu))",
                         ([(i, e, GLib.Variant("i", 0), 0) for i, e in events],)),
            _Invocation(captured),
        )
        context = GLib.MainContext.default()
        while context.pending():
            context.iteration(False)
        return captured[0].unpack()[0]        # idErrors

    def test_a_batched_click_runs_the_action(self):
        self.assertEqual(self.send_group([(1, "clicked")]), [])
        self.assertEqual(self.clicked, ["first"])

    def test_several_at_once(self):
        self.send_group([(1, "clicked"), (3, "clicked")])
        self.assertEqual(self.clicked, ["first", "second"])

    def test_unhandled_ids_are_reported_back(self):
        self.assertEqual(self.send_group([(99, "clicked")]), [99])

    def test_a_separator_is_reported_rather_than_run(self):
        self.assertEqual(self.send_group([(2, "clicked")]), [2])
        self.assertEqual(self.clicked, [])

    def test_non_click_events_are_reported(self):
        self.assertEqual(self.send_group([(1, "hovered")]), [1])
        self.assertEqual(self.clicked, [])

    def test_a_mixed_batch_runs_what_it_can(self):
        errors = self.send_group([(1, "clicked"), (99, "clicked"), (3, "clicked")])
        self.assertEqual(errors, [99])
        self.assertEqual(self.clicked, ["first", "second"])


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


class ClickingTheTrayIcon(unittest.TestCase):
    """The icon toggles: show, raise, hide.

    The distinction that matters is between "visible" and "in front". A window
    buried under a browser is visible, and clicking the tray icon then should
    bring it forward — not hide something the user cannot even see.
    """

    def setUp(self):
        import kairos.app as app_module
        self.app_module = app_module

        class FakeWindow:
            def __init__(self):
                self.visible = False
                self.active = False
                self.presented = 0

            def get_visible(self):
                return self.visible

            def is_active(self):
                return self.active

            def present(self):
                self.presented += 1
                self.visible = True
                self.active = True

            def set_visible(self, visible):
                self.visible = visible
                if not visible:
                    self.active = False

        self.window = FakeWindow()
        self.app = app_module.KairosApplication.__new__(app_module.KairosApplication)
        self.app.window = self.window

    def toggle(self):
        self.app_module.KairosApplication.toggle_window(self.app)

    def test_a_hidden_window_is_shown(self):
        self.toggle()
        self.assertTrue(self.window.visible)
        self.assertEqual(self.window.presented, 1)

    def test_a_window_in_front_is_hidden(self):
        self.window.visible = self.window.active = True
        self.toggle()
        self.assertFalse(self.window.visible)

    def test_a_buried_window_is_raised_not_hidden(self):
        self.window.visible, self.window.active = True, False
        self.toggle()
        self.assertTrue(self.window.visible, "a buried window was hidden")
        self.assertEqual(self.window.presented, 1)

    def test_clicking_twice_returns_to_where_it_started(self):
        self.toggle()
        self.assertTrue(self.window.visible)
        self.toggle()
        self.assertFalse(self.window.visible)
        self.toggle()
        self.assertTrue(self.window.visible)


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
