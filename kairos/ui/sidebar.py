"""The sidebar: a month, what is coming up, and the calendar list.

Split out of :mod:`kairos.ui.window`, which was becoming the one file you
had to read in order to understand any part of the interface.  The sidebar is
a genuine component rather than an arbitrary slice: it owns three widgets
that only talk to each other, and everything it needs from the window it asks
for through signals.

    date-selected(date)             a day was clicked in the month
    event-activated(Occurrence, Gtk.Widget)   an "Up next" entry was clicked
    manage-requested()              the calendars button in its header
"""

from __future__ import annotations

from datetime import date

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, GObject, Gtk  # noqa: E402

from kairos import APP_NAME
from kairos.config import settings
from kairos.ui.upcoming import UpcomingList
from kairos.ui.widgets import SidebarSection, colour_swatch, describe


class Sidebar(Adw.NavigationPage):
    """The whole left-hand pane."""

    __gsignals__ = {
        "date-selected": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "event-activated": (GObject.SignalFlags.RUN_FIRST, None, (object, object)),
        "manage-requested": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self, sync_manager) -> None:
        super().__init__()
        self.sync = sync_manager
        self._updating = False

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_title_widget(Gtk.Label(label=APP_NAME))

        manage = Gtk.Button(icon_name="view-list-bullet-symbolic")
        describe(manage, "Manage calendars")
        manage.connect("clicked", lambda *_: self.emit("manage-requested"))
        header.pack_end(manage)
        toolbar.add_top_bar(header)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        self._mini_calendar = Gtk.Calendar()
        self._mini_calendar.add_css_class("kairos-mini-calendar")
        self._mini_calendar.connect("day-selected", self._on_day_selected)
        box.append(self._mini_calendar)

        self.upcoming = UpcomingList(self.sync)
        self.upcoming.connect(
            "event-activated",
            lambda _list, occurrence, widget: self.emit(
                "event-activated", occurrence, widget))
        self._upcoming_section = SidebarSection(
            "Up next", self.upcoming, settings_key="sidebar_upcoming_expanded")
        self._upcoming_section.set_visible(settings.get_bool("sidebar_show_upcoming"))
        box.append(self._upcoming_section)

        self._calendar_list = Gtk.ListBox()
        self._calendar_list.set_selection_mode(Gtk.SelectionMode.NONE)
        self._calendar_list.add_css_class("navigation-sidebar")
        box.append(SidebarSection(
            "Calendars", self._calendar_list,
            settings_key="sidebar_calendars_expanded"))

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_vexpand(True)
        scroller.set_child(box)
        toolbar.set_content(scroller)

        self.set_child(toolbar)
        self.set_title(APP_NAME)
        self.rebuild_calendar_list()

    # ------------------------------------------------------------------
    # The month
    # ------------------------------------------------------------------

    def select_day(self, day: date) -> None:
        """Move the month's selection without emitting ``date-selected``.

        The window calls this whenever the view moves, and without the guard
        that would come straight back as a user selection and fight whatever
        the user was actually doing.
        """
        self._updating = True
        try:
            self._mini_calendar.select_day(
                GLib.DateTime.new_local(day.year, day.month, day.day, 12, 0, 0))
        finally:
            self._updating = False

    def _on_day_selected(self, calendar: Gtk.Calendar) -> None:
        if self._updating:
            return
        chosen = calendar.get_date()
        self.emit("date-selected",
                  date(chosen.get_year(), chosen.get_month(),
                       chosen.get_day_of_month()))

    # ------------------------------------------------------------------
    # The lists
    # ------------------------------------------------------------------

    def refresh(self) -> None:
        self.rebuild_calendar_list()
        self.upcoming.refresh()
        self._upcoming_section.set_visible(
            settings.get_bool("sidebar_show_upcoming"))

    def rebuild_calendar_list(self) -> None:
        child = self._calendar_list.get_first_child()
        while child is not None:
            next_child = child.get_next_sibling()
            self._calendar_list.remove(child)
            child = next_child

        for calendar in self.sync.calendars():
            row = Gtk.ListBoxRow()
            row.set_activatable(False)

            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            box.set_margin_top(4)
            box.set_margin_bottom(4)
            box.set_margin_start(8)
            box.set_margin_end(8)

            check = Gtk.CheckButton()
            check.set_active(calendar.visible)
            # The row's name is a plain label beside the checkbox, so the
            # checkbox itself would be announced as just "check box".
            describe(check, f"Show {calendar.name}", tooltip=False)
            check.connect("toggled", self._on_calendar_toggled, calendar)
            box.append(check)

            box.append(colour_swatch(calendar.colour, size=10))

            label = Gtk.Label(label=calendar.name, xalign=0)
            label.set_ellipsize(3)
            label.set_hexpand(True)
            label.set_tooltip_text(calendar.name)
            box.append(label)

            row.set_child(box)
            self._calendar_list.append(row)

    def _on_calendar_toggled(self, check: Gtk.CheckButton, calendar) -> None:
        self.sync.set_calendar_visible(calendar, check.get_active())
