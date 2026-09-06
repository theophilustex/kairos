"""The month grid — Kairos's default view.

Six rows of seven day cells, each cell showing a few event chips and a
"+N more" link when there are too many to fit.  The number of chips is a
preference rather than a measurement, so the grid never reflows while you
look at it.

All three views share the same small contract, which :mod:`kairos.ui.window`
relies on:

    set_date(day)     move to the period containing ``day``
    refresh()         reload from the cache and redraw
    heading           the text for the window title
    signals           ``event-activated(Occurrence, Gtk.Widget)``,
                      ``create-requested(datetime)``,
                      ``day-activated(date)``
"""

from __future__ import annotations

from datetime import date, timedelta

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GObject, Gtk  # noqa: E402

from kairos import formatting, recurrence
from kairos.config import settings
from kairos.models import Occurrence, start_of_day
from kairos.ui.widgets import clear_children, on_click, style_widget, tinted_button_css

#: A month grid is always six weeks tall.  Some months only need five, but a
#: grid that changes height as you page through the year is worse than one
#: with a spare row.
WEEKS_SHOWN = 6


class DayCell(Gtk.Box):
    """One day in the grid: a number, then a stack of event chips."""

    def __init__(self, view: "MonthView", day: date) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.view = view
        self.day = day
        self.add_css_class("kairos-day-cell")

        self._number = Gtk.Label(label=str(day.day))
        self._number.add_css_class("kairos-day-number")
        self._number.set_halign(Gtk.Align.END)
        self.append(self._number)

        self._chips = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._chips.set_vexpand(True)
        self.append(self._chips)

        on_click(self, self._on_click)

    # -- state ------------------------------------------------------------

    def set_flags(self, *, in_month: bool, is_today: bool, is_selected: bool) -> None:
        for name, wanted in (
            ("outside", not in_month),
            ("today", is_today),
            ("selected", is_selected),
            ("kairos-weekend", self.day.weekday() >= 5 and settings.get_bool("highlight_weekends")),
        ):
            if wanted:
                self.add_css_class(name)
            else:
                self.remove_css_class(name)

    def set_occurrences(self, occurrences: list[Occurrence], colours: dict[str, str]) -> None:
        clear_children(self._chips)
        limit = settings.get_int("max_chips_per_day")

        for occurrence in occurrences[:limit]:
            self._chips.append(self._make_chip(occurrence, colours))

        hidden = len(occurrences) - limit
        if hidden > 0:
            more = Gtk.Button(label=f"+{hidden} more")
            more.add_css_class("kairos-chip-more")
            more.add_css_class("flat")
            more.set_halign(Gtk.Align.START)
            more.connect("clicked", lambda *_: self.view.emit("day-activated", self.day))
            self._chips.append(more)

    def _make_chip(self, occurrence: Occurrence, colours: dict[str, str]) -> Gtk.Widget:
        """One event, drawn as a coloured pill or as a dot plus a time.

        Timed events get the lighter dot treatment so that all-day banners,
        which really are all-day, are the things that stand out.
        """
        colour = colours.get(occurrence.calendar_id, "#3584e4")
        button = Gtk.Button()
        button.add_css_class("kairos-event-chip")
        button.add_css_class("flat")
        button.set_halign(Gtk.Align.FILL)

        is_banner = occurrence.all_day or occurrence.first_day != occurrence.last_day
        if is_banner:
            label = Gtk.Label(label=occurrence.summary, xalign=0)
            label.set_ellipsize(3)  # Pango.EllipsizeMode.END
            button.set_child(label)
            style_widget(button, tinted_button_css(colour))
        else:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
            dot = Gtk.Box()
            dot.add_css_class("kairos-chip-dot")
            dot.set_valign(Gtk.Align.CENTER)
            style_widget(dot, f"background-color: {colour};")
            row.append(dot)

            # Time and title share one label rather than two.  A single
            # ellipsising label can shrink to "…", which is what lets a day
            # cell get narrow enough for seven of them to fit a small window.
            when = formatting.format_time(occurrence.start, drop_zero_minutes=True)
            text = Gtk.Label(label=f"{when}  {occurrence.summary}", xalign=0)
            text.set_ellipsize(3)
            text.set_hexpand(True)
            row.append(text)

            button.set_child(row)
            button.add_css_class("kairos-chip-timed")

        button.set_tooltip_text(f"{occurrence.summary}\n{formatting.format_time_range(occurrence)}")
        button.connect("clicked",
                       lambda b: self.view.emit("event-activated", occurrence, b))
        return button

    # -- input ------------------------------------------------------------

    def _on_click(self, n_press: int, _x: float, _y: float) -> None:
        self.view.select_day(self.day)
        if n_press >= 2:
            # Double-click on empty space starts a new event at a sensible
            # hour rather than at midnight.
            start = start_of_day(self.day) + timedelta(hours=9)
            self.view.emit("create-requested", start)


class MonthView(Gtk.Box):
    """A whole month of :class:`DayCell` widgets."""

    __gsignals__ = {
        "event-activated": (GObject.SignalFlags.RUN_FIRST, None, (object, object)),
        "create-requested": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "day-activated": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "date-selected": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
    }

    def __init__(self, sync_manager) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.sync = sync_manager
        self._anchor = date.today()          # any day in the displayed month
        self._selected = date.today()
        self._cells: dict[date, DayCell] = {}
        self._layout_signature: tuple | None = None

        # Deliberately not row-homogeneous: the weekday heading row keeps its
        # natural height, and the six day rows (which all expand and share a
        # minimum height) divide the rest evenly between them.
        self._grid = Gtk.Grid(row_homogeneous=False, column_homogeneous=False)
        self._grid.set_row_spacing(2)
        self._grid.set_column_spacing(2)
        self._grid.set_vexpand(True)
        self._grid.set_hexpand(True)
        self._grid.add_css_class("kairos-grid")
        self._grid.set_margin_start(6)
        self._grid.set_margin_end(6)
        self._grid.set_margin_bottom(6)

        # A full month of cells, each holding several chips, has a large
        # minimum height.  Wrapping it in a scroller means a short window
        # scrolls instead of forcing itself taller than the screen; at any
        # ordinary size the grid simply fills the space and never scrolls.
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_vexpand(True)
        scroller.set_child(self._grid)
        self.append(scroller)

    # ------------------------------------------------------------------
    # The view contract
    # ------------------------------------------------------------------

    @property
    def heading(self) -> str:
        return formatting.format_month_year(self._anchor)

    @property
    def selected_day(self) -> date:
        return self._selected

    def set_date(self, day: date) -> None:
        self._anchor = day
        self._selected = day
        self.refresh()

    def go_previous(self) -> None:
        first = self._anchor.replace(day=1)
        self.set_date((first - timedelta(days=1)).replace(day=1))

    def go_next(self) -> None:
        first = self._anchor.replace(day=1)
        # Day 28 exists in every month, so this never overflows.
        self.set_date((first + timedelta(days=32)).replace(day=1))

    def select_day(self, day: date) -> None:
        if day == self._selected:
            return
        previous = self._selected
        self._selected = day
        for candidate in (previous, day):
            cell = self._cells.get(candidate)
            if cell is not None:
                cell.set_flags(
                    in_month=candidate.month == self._anchor.month,
                    is_today=candidate == date.today(),
                    is_selected=candidate == self._selected,
                )
        self.emit("date-selected", day)

    def refresh(self) -> None:
        """Rebuild the grid if its shape changed, then fill in the events."""
        signature = (
            self._anchor.year, self._anchor.month,
            settings.get("first_day_of_week"),
            settings.get_bool("show_week_numbers"),
        )
        if signature != self._layout_signature:
            self._build_grid()
            self._layout_signature = signature
        self._populate()

    # ------------------------------------------------------------------
    # Building
    # ------------------------------------------------------------------

    def _first_cell_day(self) -> date:
        return formatting.month_grid_start(self._anchor)

    def _build_grid(self) -> None:
        clear_children(self._grid)
        self._cells.clear()

        show_weeks = settings.get_bool("show_week_numbers")
        day_column_offset = 1 if show_weeks else 0

        for column, heading in enumerate(formatting.weekday_headings("abbreviation")):
            label = Gtk.Label(label=heading)
            label.add_css_class("kairos-weekday-heading")
            if formatting.weekday_order()[column] >= 5:
                label.add_css_class("kairos-weekend")
            self._grid.attach(label, column + day_column_offset, 0, 1, 1)

        first_day = self._first_cell_day()
        for week in range(WEEKS_SHOWN):
            if show_weeks:
                week_day = first_day + timedelta(days=week * 7)
                number = Gtk.Label(label=str(formatting.iso_week_number(week_day)))
                number.add_css_class("kairos-week-number")
                number.set_valign(Gtk.Align.START)
                self._grid.attach(number, 0, week + 1, 1, 1)

            for column in range(7):
                day = first_day + timedelta(days=week * 7 + column)
                cell = DayCell(self, day)
                cell.set_hexpand(True)
                cell.set_vexpand(True)
                cell.set_size_request(-1, 76)
                self._cells[day] = cell
                self._grid.attach(cell, column + day_column_offset, week + 1, 1, 1)

        # With no week-number column the seven days can share the width
        # evenly.  With one, homogeneous columns would make that thin column
        # as wide as a day, so the cells expand instead and it stays narrow.
        self._grid.set_column_homogeneous(not show_weeks)

    def _populate(self) -> None:
        """Load the visible window from the cache and hand chips to the cells."""
        first_day = self._first_cell_day()
        day_count = WEEKS_SHOWN * 7

        window_start = start_of_day(first_day)
        window_end = window_start + timedelta(days=day_count)
        events = self.sync.events_between(window_start, window_end)
        occurrences = recurrence.expand(events, window_start, window_end)
        by_day = recurrence.group_by_day(occurrences, first_day, day_count)

        colours = {c.id: c.colour for c in self.sync.calendars()}
        today = date.today()
        for day, cell in self._cells.items():
            cell.set_flags(
                in_month=day.month == self._anchor.month,
                is_today=day == today,
                is_selected=day == self._selected,
            )
            cell.set_occurrences(by_day.get(day, []), colours)
