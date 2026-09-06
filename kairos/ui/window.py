"""The main window: a sidebar, a header bar and whichever view is showing.

This is the only place that knows about all the views at once.  It holds the
"current day" and pushes it into whichever view becomes visible, so switching
from month to week keeps your place.

Every view satisfies the same contract:

    set_date(day)   move to the period containing ``day``
    refresh()       reload from the cache and redraw
    heading         text for the title
    go_previous() / go_next()

and emits ``event-activated``, ``create-requested``, ``day-activated`` and
``date-selected``.  Adding a fourth view means writing that and adding one
entry to :attr:`CalendarWindow.VIEWS`.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from kairos import APP_NAME, VERSION
from kairos.config import settings
from kairos.models import Occurrence, start_of_day
from kairos.ui.agenda_view import AgendaView
from kairos.ui.calendar_manager import CalendarManager
from kairos.ui.event_editor import EventEditor
from kairos.ui.event_popover import EventPopover, confirm_delete
from kairos.ui.month_view import MonthView
from kairos.ui.preferences import PreferencesDialog
from kairos.ui.week_view import DayView, WeekView
from kairos.ui.widgets import add_horizontal_swipe, colour_swatch

log = logging.getLogger(__name__)


class CalendarWindow(Adw.ApplicationWindow):
    """Kairos's window."""

    #: ``key -> (label, factory)``.  The order is the order in the switcher.
    VIEWS = (
        ("month", "Month", MonthView),
        ("week", "Week", WeekView),
        ("day", "Day", DayView),
        ("agenda", "Agenda", AgendaView),
    )

    def __init__(self, application, sync_manager, theme_manager) -> None:
        super().__init__(application=application, title=APP_NAME)
        self.sync = sync_manager
        self.theme = theme_manager
        self._current_day = date.today()
        self._views: dict[str, Gtk.Widget] = {}
        self._open_popover: EventPopover | None = None

        self.set_default_size(1100, 720)
        self.set_size_request(420, 400)

        self._build()
        self._connect_signals()

        self.show_view(settings.get("default_view"))
        # Fill the sidebar now rather than waiting for the first sync to say
        # something changed; with only local calendars that might never come.
        self._rebuild_calendar_list()

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def _build(self) -> None:
        self._split = Adw.NavigationSplitView()
        self._split.set_sidebar(self._build_sidebar())
        self._split.set_content(self._build_content())
        self._split.set_min_sidebar_width(220)
        self._split.set_max_sidebar_width(300)
        # When collapsed, show the calendar rather than the sidebar; the
        # split view puts a back button in the header to reach the sidebar.
        self._split.set_show_content(True)

        # On a narrow window the sidebar folds into its own page and the row
        # of view buttons becomes a dropdown, so the header still fits.
        self._breakpoint = Adw.Breakpoint.new(
            Adw.BreakpointCondition.parse("max-width: 1000px")
        )
        self._breakpoint.add_setter(self._split, "collapsed", True)
        self._breakpoint.add_setter(self._view_switcher, "visible", False)
        self._breakpoint.add_setter(self._view_dropdown, "visible", True)
        self.add_breakpoint(self._breakpoint)

        self.set_content(self._split)

    # -- sidebar ----------------------------------------------------------

    def _build_sidebar(self) -> Adw.NavigationPage:
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_title_widget(Gtk.Label(label=APP_NAME))

        manage = Gtk.Button(icon_name="view-list-bullet-symbolic")
        manage.set_tooltip_text("Manage calendars")
        manage.connect("clicked", lambda *_: self.open_calendar_manager())
        header.pack_end(manage)

        toolbar.add_top_bar(header)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        self._mini_calendar = Gtk.Calendar()
        self._mini_calendar.add_css_class("kairos-mini-calendar")
        self._mini_calendar.connect("day-selected", self._on_mini_calendar_selected)
        box.append(self._mini_calendar)

        heading = Gtk.Label(label="Calendars", xalign=0)
        heading.add_css_class("kairos-sidebar-heading")
        box.append(heading)

        self._calendar_list = Gtk.ListBox()
        self._calendar_list.set_selection_mode(Gtk.SelectionMode.NONE)
        self._calendar_list.add_css_class("navigation-sidebar")
        box.append(self._calendar_list)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_vexpand(True)
        scroller.set_child(box)
        toolbar.set_content(scroller)

        return Adw.NavigationPage.new(toolbar, APP_NAME)

    def _rebuild_calendar_list(self) -> None:
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

    def _on_mini_calendar_selected(self, calendar: Gtk.Calendar) -> None:
        chosen = calendar.get_date()
        day = date(chosen.get_year(), chosen.get_month(), chosen.get_day_of_month())
        if day != self._current_day:
            self.go_to_day(day)

    # -- content ----------------------------------------------------------

    def _build_content(self) -> Adw.NavigationPage:
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(self._build_header())

        self._banner = Adw.Banner()
        self._banner.set_revealed(False)
        self._banner.set_button_label("Try again")
        self._banner.connect("button-clicked", lambda *_: self.sync.sync_now())

        self._stack = Gtk.Stack()
        self._stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self._stack.set_transition_duration(120)
        self._stack.set_vexpand(True)

        for key, label, factory in self.VIEWS:
            view = factory(self.sync)
            view.connect("event-activated", self._on_event_activated)
            view.connect("create-requested", self._on_create_requested)
            view.connect("day-activated", self._on_day_activated)
            view.connect("date-selected", self._on_date_selected)
            # Swipe (touchpad or touchscreen) to page through time, in
            # whatever unit this view shows: a day, a week, a month.
            add_horizontal_swipe(view, self._navigate)
            self._views[key] = view
            self._stack.add_titled(view, key, label)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        body.append(self._banner)
        body.append(self._stack)
        toolbar.set_content(body)

        return Adw.NavigationPage.new(toolbar, "Calendar")

    def _build_header(self) -> Adw.HeaderBar:
        header = Adw.HeaderBar()

        new_button = Gtk.Button(icon_name="list-add-symbolic")
        new_button.set_tooltip_text("New event (Ctrl+N)")
        new_button.connect("clicked", lambda *_: self.new_event())
        header.pack_start(new_button)

        navigation = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        navigation.add_css_class("linked")

        previous = Gtk.Button(icon_name="go-previous-symbolic")
        previous.set_tooltip_text("Previous")
        previous.connect("clicked", lambda *_: self._navigate(-1))
        navigation.append(previous)

        today = Gtk.Button(label="Today")
        today.set_tooltip_text("Jump to today (Ctrl+T)")
        today.connect("clicked", lambda *_: self.go_to_day(date.today()))
        navigation.append(today)

        following = Gtk.Button(icon_name="go-next-symbolic")
        following.set_tooltip_text("Next")
        following.connect("clicked", lambda *_: self._navigate(1))
        navigation.append(following)

        header.pack_start(navigation)

        self._title_label = Gtk.Label()
        self._title_label.add_css_class("title")
        self._title_label.set_ellipsize(3)
        header.set_title_widget(self._title_label)

        # Two ways to change view.  The row of buttons is the good one; the
        # dropdown replaces it on a narrow window, swapped by the breakpoint
        # in _build().  Only ever one of them is visible.
        self._view_switcher = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self._view_switcher.add_css_class("linked")
        self._view_buttons: dict[str, Gtk.ToggleButton] = {}
        for key, label, _factory in self.VIEWS:
            button = Gtk.ToggleButton(label=label)
            button.connect("toggled", self._on_view_button_toggled, key)
            self._view_switcher.append(button)
            self._view_buttons[key] = button

        self._view_dropdown = Gtk.MenuButton()
        self._view_dropdown.set_visible(False)
        self._view_dropdown.set_tooltip_text("Change view")
        view_menu = Gio.Menu()
        for key, label, _factory in self.VIEWS:
            view_menu.append(label, f"win.view-{key}")
        self._view_dropdown.set_menu_model(view_menu)

        header.pack_end(self._build_menu_button())
        header.pack_end(self._view_dropdown)
        header.pack_end(self._view_switcher)

        self._sync_spinner = Gtk.Spinner()
        self._sync_spinner.set_tooltip_text("Syncing…")
        header.pack_end(self._sync_spinner)

        sync_button = Gtk.Button(icon_name="view-refresh-symbolic")
        sync_button.set_tooltip_text("Sync now (Ctrl+R)")
        sync_button.connect("clicked", lambda *_: self.sync.sync_now())
        header.pack_end(sync_button)

        return header

    def _build_menu_button(self) -> Gtk.MenuButton:
        button = Gtk.MenuButton(icon_name="open-menu-symbolic")
        button.set_tooltip_text("Main menu")

        menu = Gio.Menu()
        section = Gio.Menu()
        section.append("Manage calendars…", "win.calendars")
        section.append("Sync now", "win.sync")
        menu.append_section(None, section)

        section = Gio.Menu()
        section.append("Preferences", "win.preferences")
        section.append("Keyboard shortcuts", "win.shortcuts")
        section.append(f"About {APP_NAME}", "win.about")
        menu.append_section(None, section)

        button.set_menu_model(menu)
        return button

    # ------------------------------------------------------------------
    # Wiring
    # ------------------------------------------------------------------

    def _connect_signals(self) -> None:
        self.sync.connect("events-changed", lambda *_: self.refresh())
        self.sync.connect("calendars-changed", lambda *_: self._rebuild_calendar_list())
        self.sync.connect("sync-started", lambda *_: self._set_syncing(True))
        self.sync.connect("sync-finished", self._on_sync_finished)
        settings.connect(self._on_settings_changed)

        for name, callback in (
            ("calendars", lambda *_: self.open_calendar_manager()),
            ("sync", lambda *_: self.sync.sync_now()),
            ("preferences", lambda *_: self.open_preferences()),
            ("about", lambda *_: self.open_about()),
            ("shortcuts", lambda *_: self.open_shortcuts()),
            ("new-event", lambda *_: self.new_event()),
            ("today", lambda *_: self.go_to_day(date.today())),
            ("next", lambda *_: self._navigate(1)),
            ("previous", lambda *_: self._navigate(-1)),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", callback)
            self.add_action(action)

        for key, _label, _factory in self.VIEWS:
            action = Gio.SimpleAction.new(f"view-{key}", None)
            action.connect("activate", lambda _a, _p, k=key: self.show_view(k))
            self.add_action(action)

    def _on_settings_changed(self, key: str | None) -> None:
        """Redraw when a preference that affects layout changes."""
        layout_keys = {
            "first_day_of_week", "show_week_numbers", "time_format", "hour_height",
            "max_chips_per_day", "agenda_days", "highlight_weekends", "compact_mode",
        }
        if key is None or key in layout_keys:
            self.refresh()

    # ------------------------------------------------------------------
    # Views
    # ------------------------------------------------------------------

    def show_view(self, key: str) -> None:
        if key not in self._views:
            key = "month"
        self._stack.set_visible_child_name(key)
        view = self._views[key]
        view.set_date(self._current_day)
        for name, button in self._view_buttons.items():
            button.set_active(name == key)
        self._view_dropdown.set_label(dict((k, l) for k, l, _ in self.VIEWS)[key])
        self._update_title()

    def _on_view_button_toggled(self, button: Gtk.ToggleButton, key: str) -> None:
        if button.get_active():
            self.show_view(key)

    @property
    def current_view(self):
        return self._views[self._stack.get_visible_child_name()]

    def _navigate(self, direction: int) -> None:
        view = self.current_view
        view.go_next() if direction > 0 else view.go_previous()
        self._current_day = view.selected_day
        self._sync_mini_calendar()
        self._update_title()

    def go_to_day(self, day: date) -> None:
        self._current_day = day
        self.current_view.set_date(day)
        self._sync_mini_calendar()
        self._update_title()

    def _on_date_selected(self, _view, day: date) -> None:
        self._current_day = day
        self._sync_mini_calendar()

    def _on_day_activated(self, _view, day: date) -> None:
        self._current_day = day
        self.show_view("day")

    def _sync_mini_calendar(self) -> None:
        self._mini_calendar.select_day(GLib.DateTime.new_local(
            self._current_day.year, self._current_day.month, self._current_day.day, 12, 0, 0
        ))

    def _update_title(self) -> None:
        self._title_label.set_label(self.current_view.heading)

    def refresh(self) -> None:
        """Reload the visible view and the sidebar from the cache."""
        # A redraw replaces the chips, and a popover parented to one of them
        # would be left pointing at a widget that no longer exists.
        self._close_popover()
        self.current_view.refresh()
        self._rebuild_calendar_list()
        self._update_title()

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    def new_event(self, start: datetime | None = None) -> None:
        calendars = self.sync.writable_calendars()
        if not calendars:
            self._show_message(
                "There is nowhere to put an event",
                "Add a calendar first, from the calendars button in the sidebar.",
            )
            return

        if start is None:
            # Put it on the day you are looking at, at a plausible hour.
            if self._current_day == date.today():
                start = None  # the editor picks the next half hour
            else:
                start = start_of_day(self._current_day) + timedelta(hours=9)

        editor = EventEditor(calendars, start=start)
        editor.connect("saved", lambda _e, event: self.sync.save_event(event))
        editor.present(self)

    def _on_create_requested(self, _view, start: datetime) -> None:
        calendars = self.sync.writable_calendars()
        if not calendars:
            return
        editor = EventEditor(calendars, start=start)
        editor.connect("saved", lambda _e, event: self.sync.save_event(event))
        editor.present(self)

    def _on_event_activated(self, _view, occurrence: Occurrence, source: Gtk.Widget) -> None:
        """Show the detail bubble for the event the user just clicked.

        ``source`` is the chip or block that was clicked, and the popover is
        parented to *that* rather than to the view.  Two reasons, one of them
        a bug we had:

        * it points at the event instead of at the middle of the month;
        * parented to the view, the popover's own grab treats the click that
          opened it as a click outside itself, so it closed again immediately
          and events could not be opened at all.

        The pop-up is also deferred to an idle callback, so the click that
        triggered it has finished being delivered first.
        """
        calendar = self.sync.storage.get_calendar(occurrence.calendar_id)
        if calendar is None:
            return

        self._close_popover()

        popover = EventPopover(
            occurrence,
            calendar_name=calendar.name,
            calendar_colour=calendar.colour,
            editable=calendar.writable,
        )
        popover.connect("edit-requested", self._on_edit_requested)
        popover.connect("delete-requested", self._on_delete_requested)
        popover.connect("closed", self._on_popover_closed)

        popover.set_parent(source)
        popover.set_position(Gtk.PositionType.BOTTOM)
        popover.set_has_arrow(True)
        self._open_popover = popover
        GLib.idle_add(self._popup, popover)

    @staticmethod
    def _popup(popover: Gtk.Popover) -> bool:
        # It may have been closed and unparented before the idle callback ran.
        if popover.get_parent() is not None:
            popover.popup()
        return GLib.SOURCE_REMOVE

    def _close_popover(self) -> None:
        """Take the popover down now, rather than on the next idle.

        Deliberate teardown has to be synchronous: a redraw destroys the chip
        the popover is parented to, and GTK complains (rightly) if a widget is
        finalised while it still has a child.
        """
        popover = self._open_popover
        self._open_popover = None
        if popover is None:
            return
        popover.popdown()
        if popover.get_parent() is not None:
            popover.unparent()

    def _on_popover_closed(self, popover: EventPopover) -> None:
        # A popover must be unparented or it leaks into the widget tree, but
        # only after GTK has finished closing it.
        if self._open_popover is popover:
            self._open_popover = None
        GLib.idle_add(self._release, popover)

    @staticmethod
    def _release(popover: Gtk.Popover) -> bool:
        if popover.get_parent() is not None:
            popover.unparent()
        return GLib.SOURCE_REMOVE

    def _on_edit_requested(self, _popover, occurrence: Occurrence) -> None:
        editor = EventEditor(self.sync.writable_calendars(), event=occurrence.event)
        editor.connect("saved", lambda _e, event: self.sync.save_event(event))
        editor.present(self)

    def _on_delete_requested(self, _popover, occurrence: Occurrence) -> None:
        confirm_delete(self, occurrence, lambda: self.sync.delete_event(occurrence.event))

    # ------------------------------------------------------------------
    # Dialogs
    # ------------------------------------------------------------------

    def open_calendar_manager(self) -> None:
        manager = CalendarManager(self.sync)
        manager.connect("changed", lambda *_: self.refresh())
        manager.present(self)

    def open_preferences(self) -> None:
        PreferencesDialog(self.sync, self.theme).present(self)

    def open_about(self) -> None:
        about = Adw.AboutDialog(
            application_name=APP_NAME,
            application_icon="org.kairos.Calendar",
            version=VERSION,
            developer_name="The Kairos contributors",
            license_type=Gtk.License.GPL_3_0,
            comments=(
                "A lightweight, customisable calendar for Linux, with CalDAV "
                "and WebDAV support."
            ),
            website="https://github.com/",
        )
        about.present(self)

    def open_shortcuts(self) -> None:
        rows = [
            ("Ctrl+N", "New event"),
            ("Ctrl+T", "Go to today"),
            ("Ctrl+R", "Sync now"),
            ("Ctrl+1 … Ctrl+4", "Month, week, day, agenda"),
            ("Ctrl+comma", "Preferences"),
            ("Alt+Left / Alt+Right", "Previous / next period"),
            ("Ctrl+W", "Close the window"),
        ]
        body = "\n".join(f"{keys}\t{description}" for keys, description in rows)
        dialog = Adw.AlertDialog(heading="Keyboard shortcuts", body=body)
        dialog.add_response("close", "Close")
        dialog.present(self)

    def _show_message(self, heading: str, body: str) -> None:
        dialog = Adw.AlertDialog(heading=heading, body=body)
        dialog.add_response("close", "Close")
        dialog.present(self)

    # ------------------------------------------------------------------
    # Sync feedback
    # ------------------------------------------------------------------

    def _set_syncing(self, busy: bool) -> None:
        self._sync_spinner.set_visible(busy)
        if busy:
            self._sync_spinner.start()
            self._banner.set_revealed(False)
        else:
            self._sync_spinner.stop()

    def _on_sync_finished(self, _manager, ok: bool, message: str) -> None:
        self._set_syncing(False)
        if ok:
            self._banner.set_revealed(False)
        else:
            self._banner.set_title(message or "Sync failed.")
            self._banner.set_revealed(True)
