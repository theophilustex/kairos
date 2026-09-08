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
from gi.repository import Gdk, GObject, Gtk  # noqa: E402

from kairos import formatting, recurrence
from kairos.config import settings
from kairos.models import Occurrence, start_of_day
from kairos.ui.week_view import _is_empty_space
from kairos.ui.widgets import (assign_banner_rows, clear_children, describe,
                               mark_event_widget, on_click,
                               style_widget, tinted_button_css)

#: A month grid is always six weeks tall.  Some months only need five, but a
#: grid that changes height as you page through the year is worse than one
#: with a spare row.
WEEKS_SHOWN = 6

#: How round a chip's corners are.  Matches ``.kairos-event-chip`` in the
#: stylesheet; the edges of a bar that continue into the next day are squared
#: off to this instead, so the pieces meet flush.
CHIP_RADIUS = "5px"


def _squared(starts: bool, ends: bool) -> str:
    """Corner radii for one piece of a multi-day bar."""
    left = CHIP_RADIUS if starts else "0"
    right = CHIP_RADIUS if ends else "0"
    return (f" border-radius: {left} {right} {right} {left};"
            f" margin-left: {'1px' if starts else '0'};"
            f" margin-right: {'1px' if ends else '0'};")


def is_banner(occurrence: Occurrence) -> bool:
    """Whether an occurrence is drawn as a bar rather than a dot and a time."""
    return occurrence.all_day or occurrence.first_day != occurrence.last_day


def banner_lines(by_day: dict, days: list[date]) -> list[dict]:
    """Work out where each multi-day bar sits across one week of cells.

    Returns one dict per day, mapping a line number to
    ``(occurrence, starts, ends, label)``.  A bar keeps the same line for the
    whole week, which is what stops it stepping up and down between cells.

    ``starts`` and ``ends`` mark the event's *real* first and last day, and
    decide which corners stay rounded.  ``label`` is separate and marks the
    first cell of this row: an event running across two week rows is named
    again at the start of the second, because the name in the row above is
    nowhere near it — but its left edge is still square, because it continues.
    """
    spans: dict = {}
    for day in days:
        for occurrence in by_day.get(day, []):
            key = (occurrence.uid, occurrence.start)
            if not is_banner(occurrence) or key in spans:
                continue
            spans[key] = (
                occurrence,
                max(0, (occurrence.first_day - days[0]).days),
                min(len(days) - 1, (occurrence.last_day - days[0]).days),
            )

    per_day: list[dict] = [{} for _ in days]
    for occurrence, first, last, line in assign_banner_rows(list(spans.values())):
        for column in range(first, last + 1):
            per_day[column][line] = (
                occurrence,
                occurrence.first_day == days[column],
                occurrence.last_day == days[column],
                column == first,
            )
    return per_day


def _add_months(day: date, months: int) -> date:
    """The same day-of-month a month away, clamped to a month that has it."""
    total = day.year * 12 + (day.month - 1) + months
    year, month = divmod(total, 12)
    month += 1
    last = [31, 29 if year % 4 == 0 and (year % 100 or year % 400 == 0) else 28,
            31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1]
    return date(year, month, min(day.day, last))


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

        # Reachable by keyboard, and named for a screen reader. Without both
        # the grid is a picture: nothing can be focused, so nothing can be
        # announced however carefully it is labelled.
        self.set_focusable(True)
        self.set_focus_on_click(True)
        describe(self, formatting.format_date(day), tooltip=False)

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

    def set_occurrences(self, banners: dict, timed: list[Occurrence],
                        colours: dict[str, str]) -> None:
        """Fill the cell in.

        ``banners`` maps a *line number* to ``(occurrence, starts, ends)`` for
        the multi-day bars crossing this day.  The line number is assigned
        across the whole week, so a bar sits on the same line in every cell it
        passes through and the pieces read as one bar rather than a staircase.
        Lines this day has no bar on are held open by a blank placeholder,
        which is what keeps the ones below it lined up too.
        """
        clear_children(self._chips)
        limit = settings.get_int("max_chips_per_day")

        lines = max(banners) + 1 if banners else 0
        rows_used = 0
        events_shown = 0
        for line in range(lines):
            if rows_used >= limit:
                break
            entry = banners.get(line)
            if entry is None:
                self._chips.append(self._spacer())
            else:
                occurrence, starts, ends, label = entry
                self._chips.append(self._make_chip(
                    occurrence, colours, starts=starts, ends=ends, label=label))
                events_shown += 1
            rows_used += 1

        for occurrence in timed:
            if rows_used >= limit:
                break
            self._chips.append(self._make_chip(occurrence, colours))
            rows_used += 1
            events_shown += 1

        # Re-announce with what is actually on the day, so a screen reader
        # reads "Monday 7 September, 2 events" rather than a bare date.
        count = len(banners) + len(timed)
        describe(self, formatting.format_date(self.day) if not count else
                 f"{formatting.format_date(self.day)}, {count} "
                 f"event{'s' if count != 1 else ''}", tooltip=False)

        hidden = len(banners) + len(timed) - events_shown
        if hidden > 0:
            more = Gtk.Button(label=f"+{hidden} more")
            more.add_css_class("kairos-chip-more")
            more.add_css_class("flat")
            more.set_halign(Gtk.Align.START)
            more.connect("clicked", lambda *_: self.view.emit("day-activated", self.day))
            self._chips.append(more)

    def _spacer(self) -> Gtk.Widget:
        """An invisible chip, holding a banner line open on a day it skips."""
        spacer = Gtk.Button()
        spacer.add_css_class("kairos-event-chip")
        spacer.add_css_class("flat")
        spacer.set_child(Gtk.Label(label=" ", xalign=0))
        spacer.set_opacity(0)
        spacer.set_can_target(False)
        spacer.set_can_focus(False)
        return spacer

    def _make_chip(self, occurrence: Occurrence, colours: dict[str, str],
                   *, starts: bool = True, ends: bool = True,
                   label: bool = True) -> Gtk.Widget:
        """One event, drawn as a coloured pill or as a dot plus a time.

        Timed events get the lighter dot treatment so that all-day banners,
        which really are all-day, are the things that stand out.

        ``starts`` and ``ends`` say whether this is the event's real first or
        last day, and square off the edges that continue into the next cell so
        the pieces meet flush. ``label`` says whether to write the title here:
        once per row rather than once per day, so a week-long event reads as
        one bar with a name at the front instead of seven repetitions of it.
        """
        colour = colours.get(occurrence.calendar_id, "#3584e4")
        button = Gtk.Button()
        button.add_css_class("kairos-event-chip")
        button.add_css_class("flat")
        button.set_halign(Gtk.Align.FILL)

        is_banner = occurrence.all_day or occurrence.first_day != occurrence.last_day
        if is_banner:
            text = Gtk.Label(label=occurrence.summary if label else "", xalign=0)
            text.set_ellipsize(3)  # Pango.EllipsizeMode.END
            button.set_child(text)
            style_widget(button, tinted_button_css(colour) + _squared(starts, ends))
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
        mark_event_widget(button, occurrence)
        return button

    # -- input ------------------------------------------------------------

    def _on_click(self, n_press: int, x: float, y: float) -> None:
        """First click selects the day; clicking the selected day creates.

        The same two-step as the week grid, so the gesture means the same
        thing wherever you are: one click to say where, a second to commit.
        """
        if not _is_empty_space(self, x, y):
            # A click on one of the day's event chips. Opening it is that
            # chip's job; the cell must not also arm itself for creating.
            return

        # Armed by a previous click *on this cell*, not merely "this day is
        # the selected one" — arriving on a day by any other route and
        # clicking it once would otherwise create an event immediately.
        armed = self.view.armed_day == self.day
        self.view.select_day(self.day)
        self.view.armed_day = None if armed else self.day
        if n_press >= 2 or armed:
            # A new event on a day cell has no time attached to it, so it
            # starts at a plausible hour rather than at midnight.
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

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key_pressed)
        # CAPTURE so the arrows move the selection before the scrolled window
        # takes them to mean "scroll", which is not what a grid should do.
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        self.add_controller(keys)

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
        self.armed_day = None
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

    #: What each key moves the selection by, in days.  Up and down are a
    #: week because that is what a row of this grid is.
    KEY_MOVES = {
        Gdk.KEY_Left: -1, Gdk.KEY_KP_Left: -1,
        Gdk.KEY_Right: 1, Gdk.KEY_KP_Right: 1,
        Gdk.KEY_Up: -7, Gdk.KEY_KP_Up: -7,
        Gdk.KEY_Down: 7, Gdk.KEY_KP_Down: 7,
    }

    def _on_key_pressed(self, _controller, keyval, _keycode, state) -> bool:
        """Move around the grid with the arrows, open a day with Enter.

        Returns ``True`` to stop the key going any further, which is what
        keeps the arrows from scrolling the view instead of moving.
        """
        if state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK):
            return False        # Ctrl+arrow and Alt+arrow belong to the window

        step = self.KEY_MOVES.get(keyval)
        if step is not None:
            self.focus_day(self._selected + timedelta(days=step))
            return True

        if keyval in (Gdk.KEY_Home, Gdk.KEY_KP_Home):
            self.focus_day(formatting.week_start(self._selected))
            return True
        if keyval in (Gdk.KEY_End, Gdk.KEY_KP_End):
            self.focus_day(formatting.week_start(self._selected) + timedelta(days=6))
            return True
        if keyval in (Gdk.KEY_Page_Up, Gdk.KEY_KP_Page_Up):
            self.focus_day(_add_months(self._selected, -1))
            return True
        if keyval in (Gdk.KEY_Page_Down, Gdk.KEY_KP_Page_Down):
            self.focus_day(_add_months(self._selected, 1))
            return True
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_space):
            self.emit("day-activated", self._selected)
            return True
        return False

    def focus_day(self, day: date) -> None:
        """Select ``day``, paging the grid if it is not on screen, and focus it.

        Paging is what makes the arrows feel like a calendar rather than a
        table: walking off the end of the month brings the next one in rather
        than stopping dead.
        """
        self.armed_day = None
        if day in self._cells:
            self.select_day(day)          # emits date-selected if it changed
        else:
            # Paging: set_date rebuilds the grid but says nothing, so the
            # window would not learn the day had moved.
            self.set_date(day)
            self.emit("date-selected", day)
        cell = self._cells.get(day)
        if cell is not None:
            cell.grab_focus()

    #: The day a click has picked, waiting for a second click to create on
    #: it.  Cleared whenever the grid is rebuilt or the selection moves by
    #: any other means.
    armed_day: date | None = None

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
        for week in range(WEEKS_SHOWN):
            days = [first_day + timedelta(days=week * 7 + column)
                    for column in range(7)]
            # Bars are placed a week at a time, because a week is one row of
            # cells and a bar cannot continue past the end of one.
            lines = banner_lines(by_day, days)
            for column, day in enumerate(days):
                cell = self._cells.get(day)
                if cell is None:
                    continue
                cell.set_flags(
                    in_month=day.month == self._anchor.month,
                    is_today=day == today,
                    is_selected=day == self._selected,
                )
                timed = [o for o in by_day.get(day, []) if not is_banner(o)]
                cell.set_occurrences(lines[column], timed, colours)
