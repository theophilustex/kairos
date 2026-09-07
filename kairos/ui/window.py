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
from kairos.ui.event_popover import EventPopover, ask_edit_scope, confirm_delete
from kairos.ui.month_view import MonthView
from kairos.ui.preferences import PreferencesDialog
from kairos.ui.search_view import SearchView
from kairos.ui.sidebar import Sidebar
from kairos.ui.week_view import DayView, WeekView
from kairos.ui.widgets import (
    add_horizontal_swipe, describe, find_event_widget,
)

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

    #: The stack page holding search results. Not one of VIEWS: it is shown
    #: only while searching and is never in the view switcher.
    SEARCH_PAGE = "search"

    def __init__(self, application, sync_manager, theme_manager) -> None:
        super().__init__(application=application, title=APP_NAME)
        self._view_before_search: str | None = None
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
        self._sidebar.rebuild_calendar_list()

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def _build(self) -> None:
        self._split = Adw.NavigationSplitView()
        self._sidebar = Sidebar(self.sync)
        self._sidebar.connect("date-selected", self._on_sidebar_date)
        self._sidebar.connect("event-activated", self._on_event_activated)
        self._sidebar.connect("manage-requested",
                              lambda *_: self.open_calendar_manager())
        self._split.set_sidebar(self._sidebar)
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

        self._search_entry = Gtk.SearchEntry()
        self._search_entry.set_placeholder_text("Search events")
        self._search_entry.set_hexpand(True)
        self._search_entry.connect("search-changed", self._on_search_changed)
        self._search_entry.connect("stop-search", lambda *_: self.stop_search())

        self._search_bar = Gtk.SearchBar()
        self._search_bar.set_child(self._search_entry)
        self._search_bar.connect_entry(self._search_entry)
        # Typing anywhere in the window starts a search, as it does in Files
        # and every other GNOME application.
        self._search_bar.set_key_capture_widget(self)

        self._search_view = SearchView(self.sync)
        self._search_view.connect("event-activated", self._on_search_result)
        self._stack.add_named(self._search_view, self.SEARCH_PAGE)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        body.append(self._search_bar)
        body.append(self._banner)
        body.append(self._stack)

        # Deleting is the one destructive thing in Kairos, so it gets an undo
        # rather than only a confirmation.
        self._toasts = Adw.ToastOverlay()
        self._toasts.set_child(body)
        toolbar.set_content(self._toasts)

        return Adw.NavigationPage.new(toolbar, "Calendar")

    def _build_header(self) -> Adw.HeaderBar:
        header = Adw.HeaderBar()

        new_button = Gtk.Button(icon_name="list-add-symbolic")
        describe(new_button, "New event (Ctrl+N)")
        new_button.connect("clicked", lambda *_: self.new_event())
        header.pack_start(new_button)

        navigation = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        navigation.add_css_class("linked")

        previous = Gtk.Button(icon_name="go-previous-symbolic")
        describe(previous, "Previous")
        previous.connect("clicked", lambda *_: self._navigate(-1))
        navigation.append(previous)

        today = Gtk.Button(label="Today")
        today.set_tooltip_text("Jump to today (Ctrl+T)")
        today.connect("clicked", lambda *_: self.go_to_day(date.today()))
        navigation.append(today)

        following = Gtk.Button(icon_name="go-next-symbolic")
        describe(following, "Next")
        following.connect("clicked", lambda *_: self._navigate(1))
        navigation.append(following)

        header.pack_start(navigation)

        self._search_button = Gtk.ToggleButton(icon_name="system-search-symbolic")
        describe(self._search_button, "Search events (Ctrl+F)")
        self._search_button.connect("toggled", self._on_search_toggled)
        header.pack_end(self._search_button)

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
        describe(self._view_dropdown, "Change view")
        view_menu = Gio.Menu()
        for key, label, _factory in self.VIEWS:
            view_menu.append(label, f"win.view-{key}")
        self._view_dropdown.set_menu_model(view_menu)

        header.pack_end(self._build_menu_button())
        header.pack_end(self._view_dropdown)
        header.pack_end(self._view_switcher)

        self._sync_spinner = Gtk.Spinner()
        describe(self._sync_spinner, "Syncing…")
        header.pack_end(self._sync_spinner)

        sync_button = Gtk.Button(icon_name="view-refresh-symbolic")
        describe(sync_button, "Sync now (Ctrl+R)")
        sync_button.connect("clicked", lambda *_: self.sync.sync_now())
        header.pack_end(sync_button)

        return header

    def _build_menu_button(self) -> Gtk.MenuButton:
        button = Gtk.MenuButton(icon_name="open-menu-symbolic")
        describe(button, "Main menu")

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
        self.sync.connect("calendars-changed",
                          lambda *_: self._sidebar.rebuild_calendar_list())
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
            ("search", lambda *_: self.start_search()),
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
            "sidebar_upcoming_count", "sidebar_upcoming_days",
        }
        if key in (None, "sidebar_show_upcoming"):
            self._sidebar.refresh()
        if key is None or key in layout_keys:
            self.refresh()

    # ------------------------------------------------------------------
    # Views
    # ------------------------------------------------------------------

    def show_view(self, key: str) -> None:
        if key not in self._views:
            key = "month"
        # Anything that rebuilds a view's chips has to take the popover down
        # first: it is parented to one of them, and finalising a widget that
        # still has a popover attached segfaults GTK.
        self._close_popover()
        if self._search_button.get_active():
            self._search_button.set_active(False)
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
        """The calendar view on screen, never the search results page."""
        name = self._stack.get_visible_child_name()
        if name == self.SEARCH_PAGE:
            name = self._view_before_search or settings.get("default_view")
        return self._views.get(name) or self._views["month"]

    def _navigate(self, direction: int) -> None:
        self._close_popover()
        view = self.current_view
        view.go_next() if direction > 0 else view.go_previous()
        self._current_day = view.selected_day
        self._sidebar.select_day(self._current_day)
        self._update_title()

    def go_to_day(self, day: date) -> None:
        self._close_popover()
        self._current_day = day
        self.current_view.set_date(day)
        self._sidebar.select_day(self._current_day)
        self._update_title()

    def _on_date_selected(self, _view, day: date) -> None:
        self._current_day = day
        self._sidebar.select_day(self._current_day)

    def _on_day_activated(self, _view, day: date) -> None:
        self._current_day = day
        self.show_view("day")

    def _on_sidebar_date(self, _sidebar, day: date) -> None:
        if day != self._current_day:
            self.go_to_day(day)

    def _update_title(self) -> None:
        self._title_label.set_label(self.current_view.heading)

    def refresh(self) -> None:
        """Reload the visible view and the sidebar from the cache."""
        # A redraw replaces the chips, and a popover parented to one of them
        # would be left pointing at a widget that no longer exists.
        self._close_popover()
        self.current_view.refresh()
        self._sidebar.refresh()
        self._update_title()

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def start_search(self) -> None:
        """Show the search bar and put the cursor in it."""
        self._search_button.set_active(True)
        self._search_entry.grab_focus()

    def stop_search(self) -> None:
        """Close the search and go back to the view that was showing."""
        self._search_button.set_active(False)

    def _on_search_toggled(self, button: Gtk.ToggleButton) -> None:
        searching = button.get_active()
        self._search_bar.set_search_mode(searching)

        if searching:
            self._view_before_search = self._stack.get_visible_child_name()
            self._search_view.search(self._search_entry.get_text())
            self._stack.set_visible_child_name(self.SEARCH_PAGE)
            self._search_entry.grab_focus()
        else:
            self._search_entry.set_text("")
            self.show_view(self._view_before_search or settings.get("default_view"))

    def _on_search_changed(self, entry: Gtk.SearchEntry) -> None:
        if not self._search_button.get_active():
            # Typing into the window opened the bar without the button
            # knowing; keep the two in step.
            self._search_button.set_active(True)
        self._search_view.search(entry.get_text())

    def _on_search_result(self, _view, occurrence: Occurrence, _source: Gtk.Widget) -> None:
        """Clicking a result leaves search, goes to the day, and opens the event.

        The row that was clicked is deliberately *not* used to anchor the
        bubble.  Leaving search clears the entry, which re-runs the search
        with an empty term and empties the results list — so that row is
        destroyed a moment later, and a popover parented to it took the whole
        application down with a segfault when it tried to pop up.

        The bubble is anchored to the event's own chip in the view we land on
        instead, which is also where the user is now looking.  That chip does
        not exist until the view has been rebuilt, hence the idle callback.
        """
        self.stop_search()
        self.go_to_day(occurrence.first_day)
        GLib.idle_add(self._open_from_search, occurrence)

    def _open_from_search(self, occurrence: Occurrence) -> bool:
        view = self._views.get(self._stack.get_visible_child_name())
        if view is None:
            return GLib.SOURCE_REMOVE
        # Falls back to the view itself when the event is not drawn — hidden
        # behind a "+N more", say.  Better a bubble in the middle of the
        # window than a click that appears to do nothing.
        self._on_event_activated(view, occurrence,
                                 find_event_widget(view, occurrence) or view)
        return GLib.SOURCE_REMOVE

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

        # A widget that has been taken out of the window is on its way to
        # being finalised, and parenting a popover to it segfaults the moment
        # the popover tries to appear.  Anchor to the view instead.
        if source is None or source.get_root() is None:
            source = self._views.get(self._stack.get_visible_child_name())
            if source is None:
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
        """Edit an event, asking first which occurrences an edit should touch.

        The question comes *before* the editor rather than after saving: what
        you are editing changes what the fields mean, and being asked at the
        end — having already made the change — is the wrong moment to find
        out you were editing the whole series.
        """
        def open_editor(*, whole_series: bool) -> None:
            editor = EventEditor(self.sync.writable_calendars(),
                                 event=occurrence.event)
            editor.connect("saved", lambda _e, event: self.sync.save_occurrence(
                occurrence, event, whole_series=whole_series))
            editor.present(self)

        ask_edit_scope(self, occurrence, open_editor)

    def _on_delete_requested(self, _popover, occurrence: Occurrence) -> None:
        def delete(*, whole_series: bool) -> None:
            # Snapshot before the delete: for a single occurrence the change
            # is an EXDATE inside the series' iCalendar, so putting it back
            # means restoring that document, not recreating an event.
            before = occurrence.event
            self.sync.delete_occurrence(occurrence, whole_series=whole_series)
            self._offer_undo(
                "Event deleted" if whole_series else "Occurrence deleted", before)

        confirm_delete(self, occurrence, delete)

    def _offer_undo(self, message: str, event) -> None:
        toast = Adw.Toast(title=message, timeout=6)
        toast.set_button_label("Undo")
        toast.connect("button-clicked", lambda *_: self.sync.restore_event(event))
        self._toasts.add_toast(toast)

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
