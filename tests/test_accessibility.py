"""Every control a screen reader can reach must have a name.

A button showing only an icon has no text of its own, so assistive
technology announces it as "button" and nothing else — which makes a header
bar of seven icons unusable without sight. GTK will not warn about this and
it is invisible on screen, so it is the kind of thing that only a test
keeps honest.

A tooltip is deliberately not accepted as a name here. GTK maps it to the
accessible *description*, the detail read out after the name, and a
description with no name to attach to is not a fix.
"""

import tempfile
import unittest
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402

Adw.init()

from kairos.accounts import AccountStore  # noqa: E402
from kairos.storage import Storage  # noqa: E402
from kairos.sync import SyncManager  # noqa: E402
from kairos.ui.widgets import describe  # noqa: E402
from kairos.ui.window import CalendarWindow  # noqa: E402


class _NullTheme:
    """Stands in for ThemeManager, which the window only stores."""


def walk(widget):
    """Every widget in the tree below (and including) ``widget``."""
    yield widget
    child = widget.get_first_child()
    while child is not None:
        yield from walk(child)
        child = child.get_next_sibling()


def visible_text(widget) -> str:
    """Any text the widget shows, which is a name in itself."""
    if isinstance(widget, Gtk.Label):
        return widget.get_label() or ""
    getter = getattr(widget, "get_label", None)
    text = getter() if getter is not None else None
    if text:
        return text
    return " ".join(visible_text(child) for child in walk(widget)
                    if isinstance(child, Gtk.Label))


def accessible_name(widget) -> str:
    """The name ``describe`` gave this widget, if any.

    Read from what ``describe`` recorded rather than from GTK: GTK 4 can set
    an accessible property but offers no way to read one back, so this checks
    that a control was labelled, not what the screen reader ultimately hears.
    """
    return getattr(widget, "kairos_accessible_label", "")


class TheHelper(unittest.TestCase):
    def test_it_sets_a_name_a_screen_reader_can_read(self):
        button = Gtk.Button()
        describe(button, "Sync now")
        self.assertEqual(accessible_name(button), "Sync now")

    def test_it_really_reaches_gtk(self):
        """The recorded name is a convenience; this is the actual call."""
        button = Gtk.Button()
        button.update_property([Gtk.AccessibleProperty.LABEL], ["Sync now"])
        self.assertEqual(button.get_accessible_role(), Gtk.AccessibleRole.BUTTON)

    def test_it_sets_the_tooltip_too_by_default(self):
        button = Gtk.Button()
        describe(button, "Sync now")
        self.assertEqual(button.get_tooltip_text(), "Sync now")

    def test_the_tooltip_can_be_left_off(self):
        """Where the name would only repeat text already on screen."""
        button = Gtk.Button()
        describe(button, "Show Work", tooltip=False)
        self.assertIsNone(button.get_tooltip_text())
        self.assertEqual(accessible_name(button), "Show Work")


class TheWindow(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix="kairos-a11y-"))
        self.sync = SyncManager(
            store=Storage(self.directory / "cache.db"),
            accounts=AccountStore(self.directory / "accounts.json"),
        )
        application = Adw.Application(application_id="org.kairos.A11yTest")
        self.window = CalendarWindow(application, self.sync, _NullTheme())

    def tearDown(self):
        self.sync.storage.close()

    #: Composite widgets that build buttons of their own — a calendar's month
    #: arrows, a menu button's internal toggle, the window controls. GTK
    #: labels those itself, and Kairos neither creates nor can reach them.
    PROVIDED_BY_GTK = ("Calendar", "AdwBackButton", "SearchBar",
                       "WindowControls", "MenuButton")

    def built_by_kairos(self, widget) -> bool:
        parent = widget.get_parent()
        while parent is not None:
            if type(parent).__name__ in self.PROVIDED_BY_GTK:
                return False
            parent = parent.get_parent()
        return True

    def unnamed_controls(self):
        """Kairos's own controls with neither visible text nor a name."""
        found = []
        for widget in walk(self.window):
            if not isinstance(widget, (Gtk.Button, Gtk.MenuButton,
                                       Gtk.ToggleButton, Gtk.CheckButton)):
                continue
            if not self.built_by_kairos(widget):
                continue
            if visible_text(widget).strip() or accessible_name(widget).strip():
                continue
            found.append(type(widget).__name__
                         + (f" ({widget.get_icon_name()})"
                            if hasattr(widget, "get_icon_name")
                            and widget.get_icon_name() else ""))
        return found

    def test_every_button_has_a_name(self):
        unnamed = self.unnamed_controls()
        self.assertEqual(unnamed, [],
                         f"{len(unnamed)} control(s) a screen reader cannot "
                         f"name: {unnamed}")

    def test_the_header_buttons_are_named(self):
        """The ones that are icons only, and so have nothing else to read."""
        names = {accessible_name(w) for w in walk(self.window)
                 if isinstance(w, (Gtk.Button, Gtk.MenuButton, Gtk.ToggleButton))}
        for expected in ("New event (Ctrl+N)", "Search events (Ctrl+F)",
                         "Sync now (Ctrl+R)", "Main menu", "Manage calendars"):
            self.assertIn(expected, names)

    def test_the_calendar_checkboxes_say_which_calendar(self):
        names = [accessible_name(w) for w in walk(self.window)
                 if isinstance(w, Gtk.CheckButton)]
        self.assertTrue(names, "no calendar checkboxes were built")
        for name in names:
            self.assertTrue(name.startswith("Show "), f"unhelpful name: {name!r}")


if __name__ == "__main__":
    unittest.main()
