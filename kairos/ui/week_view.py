"""The week and day views: a timed grid with events drawn in place.

One class covers both — a day view is simply a week view showing one column.

**How events are positioned.**  Rather than doing pixel arithmetic, each day
is a :class:`Gtk.Grid` of fifteen-minute rows.  An event is attached spanning
the rows it covers, and GTK does the layout.  Overlapping events are handled
by giving the grid several columns and packing each cluster of overlapping
events into the fewest columns that fit — the standard "sweep" algorithm, in
about twenty readable lines (see :func:`assign_columns`).

The consequence is that everything here is integer row/column arithmetic, and
the only place a pixel appears is the height of a row.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, GObject, Gtk  # noqa: E402

from kairos import formatting, recurrence
from kairos.config import settings
from kairos.models import Occurrence, local_timezone, start_of_day
from kairos.ui.widgets import (clear_children, mark_event_widget, on_click,
                               style_widget, tinted_button_css)

#: Rows per hour.  Four gives fifteen-minute resolution, which is as fine as
#: anyone schedules and keeps the widget count sensible (96 rows per day).
ROWS_PER_HOUR = 4
MINUTES_PER_ROW = 60 // ROWS_PER_HOUR
ROWS_PER_DAY = 24 * ROWS_PER_HOUR

#: The most side-by-side columns a cluster of overlapping events may use.
#: Beyond this they stack, because a 1/8th-width block is unreadable anyway.
MAX_OVERLAP_COLUMNS = 4

#: The grid's rows are homogeneous, which means one child that needs more
#: height than its rows allow silently stretches *every* row and throws the
#: whole day out of alignment.  These two numbers stop that happening: a block
#: is never given fewer rows than its single line of text needs, and the
#: second line (the time range) only appears when there is room to draw it.
MIN_BLOCK_PIXELS = 22
TIME_LABEL_PIXELS = 36


def assign_banner_rows(
    banners: list[tuple[Occurrence, int, int]],
) -> list[tuple[Occurrence, int, int, int]]:
    """Stack all-day banners so that no two overlapping ones share a row.

    Takes ``(occurrence, first_column, last_column)`` — column numbers being
    days across the strip — and returns the same with a row number added.

    Widest bars are placed first and each takes the lowest row that is free
    for *every* column it covers.  Both matter: a bar has one row for its
    whole span rather than stepping down mid-week, and a week-long banner
    ends up on the top line instead of below the short events it passes.
    """
    ordered = sorted(banners, key=lambda b: (b[1] - b[2], b[1], b[0].summary))
    placed: list[tuple[Occurrence, int, int, int]] = []
    occupied: list[set[int]] = []

    for occurrence, first, last in ordered:
        columns = set(range(first, last + 1))
        for row, taken in enumerate(occupied):
            if not taken & columns:
                taken |= columns
                break
        else:
            row = len(occupied)
            occupied.append(set(columns))
        placed.append((occurrence, first, last, row))
    return placed


def assign_columns(occurrences: list[Occurrence]) -> list[tuple[Occurrence, int, int]]:
    """Work out where side-by-side overlapping events should sit.

    Returns ``(occurrence, column, columns_in_cluster)`` for each input, where
    a *cluster* is a run of events that overlap transitively.  Everything in
    one cluster is given the same width so the result lines up.

    The algorithm is the obvious one: walk the events in start order, keep the
    set of columns still busy, and put each event in the lowest free column.
    """
    ordered = sorted(occurrences, key=lambda o: (o.start, o.end))
    results: list[tuple[Occurrence, int, int]] = []

    cluster: list[tuple[Occurrence, int]] = []
    cluster_end: datetime | None = None
    column_ends: list[datetime] = []

    def flush() -> None:
        if not cluster:
            return
        width = min(max(column for _, column in cluster) + 1, MAX_OVERLAP_COLUMNS)
        for occurrence, column in cluster:
            results.append((occurrence, min(column, width - 1), width))
        cluster.clear()
        column_ends.clear()

    for occurrence in ordered:
        if cluster_end is not None and occurrence.start >= cluster_end:
            flush()
            cluster_end = None

        # The lowest column whose previous event has already finished.
        column = next(
            (index for index, end in enumerate(column_ends) if end <= occurrence.start),
            len(column_ends),
        )
        if column == len(column_ends):
            column_ends.append(occurrence.end)
        else:
            column_ends[column] = occurrence.end

        cluster.append((occurrence, column))
        cluster_end = occurrence.end if cluster_end is None else max(cluster_end, occurrence.end)

    flush()
    return results


def row_for(moment: datetime, day: date) -> int:
    """Which grid row a moment falls in, clamped to the day."""
    if moment.date() < day:
        return 0
    if moment.date() > day:
        return ROWS_PER_DAY
    return min(ROWS_PER_DAY, (moment.hour * 60 + moment.minute) // MINUTES_PER_ROW)


class WeekView(Gtk.Box):
    """A timed grid covering ``day_count`` consecutive days."""

    __gsignals__ = {
        "event-activated": (GObject.SignalFlags.RUN_FIRST, None, (object, object)),
        "create-requested": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "day-activated": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "date-selected": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
    }

    def __init__(self, sync_manager, day_count: int = 7) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.sync = sync_manager
        self.day_count = day_count
        self._anchor = date.today()
        self._first_day = date.today()
        self._day_grids: list[Gtk.Grid] = []
        self._now_line: Gtk.Widget | None = None
        self._scrolled_once = False

        self._build_skeleton()

    # ------------------------------------------------------------------
    # The view contract
    # ------------------------------------------------------------------

    @property
    def heading(self) -> str:
        last = self._first_day + timedelta(days=self.day_count - 1)
        return formatting.format_range_heading(self._first_day, last)

    @property
    def selected_day(self) -> date:
        return self._anchor

    def set_date(self, day: date) -> None:
        self._anchor = day
        self._first_day = formatting.week_start(day) if self.day_count == 7 else day
        self.refresh()

    def go_previous(self) -> None:
        self.set_date(self._anchor - timedelta(days=self.day_count))

    def go_next(self) -> None:
        self.set_date(self._anchor + timedelta(days=self.day_count))

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def _row_height(self) -> int:
        """Pixels per fifteen-minute row, derived from the hour-height setting."""
        return max(4, settings.get_int("hour_height") // ROWS_PER_HOUR)

    @staticmethod
    def _day_strip(spacer: Gtk.Widget, days: Gtk.Widget) -> Gtk.Grid:
        """A full-width row split into the hour gutter and the day columns.

        ``days`` shares out its width equally between the days, which is what
        keeps it lined up with the timed grid below, built the same way.
        """
        strip = Gtk.Grid(column_homogeneous=False)
        strip.set_margin_start(6)
        strip.set_margin_end(6)
        strip.attach(spacer, 0, 0, 1, 1)
        days.set_hexpand(True)
        strip.attach(days, 1, 0, 1, 1)
        return strip

    def _build_skeleton(self) -> None:
        """Create the parts that never change: headings, gutter, scroller."""
        # The heading row and the all-day strip each start with a blank cell
        # standing in for the hour gutter below them.  A size group keeps all
        # three exactly the same width, so the columns line up whatever the
        # font or the time format does to the gutter's width.
        self._gutter_group = Gtk.SizeGroup(mode=Gtk.SizeGroupMode.HORIZONTAL)
        # Created once and re-attached on every redraw; making fresh ones each
        # time would quietly grow the size group forever.
        self._header_spacer = Gtk.Box()
        self._all_day_spacer = Gtk.Box()
        self._gutter_group.add_widget(self._header_spacer)
        self._gutter_group.add_widget(self._all_day_spacer)

        # -- day headings, plus the all-day strip -------------------------
        # Both are laid out exactly like the timed body below: the gutter
        # spacer, then a *homogeneous* box of day columns.  They used to be
        # plain grids sized to their contents, which meant a day holding a
        # long banner claimed more width than its neighbours and pushed every
        # later day sideways — the banners then sat over the wrong columns.
        self._header_days = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL,
                                    homogeneous=True)
        self._header = self._day_strip(self._header_spacer, self._header_days)
        self.append(self._header)

        # A grid rather than a box, because a banner spanning several days is
        # one widget attached across that many columns.  No column spacing:
        # the columns have to match the timed grid's exactly, so the gaps
        # between neighbouring bars come from the chip's own margin instead.
        self._all_day_days = Gtk.Grid(column_homogeneous=True, column_spacing=0)
        self._all_day_strip = self._day_strip(self._all_day_spacer, self._all_day_days)
        self._all_day_strip.add_css_class("kairos-all-day-strip")
        self.append(self._all_day_strip)

        # -- the scrolling timed area -------------------------------------
        self._scroller = Gtk.ScrolledWindow()
        self._scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._scroller.set_vexpand(True)
        self.append(self._scroller)

        self._body = Gtk.Grid(column_homogeneous=False)
        self._body.set_margin_start(6)
        self._body.set_margin_end(6)

        # The hour labels live in their own column, aligned to the rows they
        # name by spanning the same grid.
        self._gutter = Gtk.Grid()
        self._gutter.set_valign(Gtk.Align.START)
        self._gutter_group.add_widget(self._gutter)
        self._body.attach(self._gutter, 0, 0, 1, 1)

        self._overlay = Gtk.Overlay()
        self._columns_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, homogeneous=True)
        self._columns_box.set_hexpand(True)
        self._overlay.set_child(self._columns_box)
        self._body.attach(self._overlay, 1, 0, 1, 1)

        self._scroller.set_child(self._body)

    def _build_gutter(self) -> None:
        clear_children(self._gutter)
        row_height = self._row_height()
        for hour in range(24):
            label = Gtk.Label(label=formatting.format_hour_label(hour))
            label.add_css_class("kairos-hour-label")
            label.set_halign(Gtk.Align.END)
            # The label's box is one hour tall, but the text must sit at the
            # top of it so it lines up with the hour rule in the columns
            # rather than floating in the middle of the band.
            label.set_yalign(0.0)
            label.set_size_request(-1, row_height * ROWS_PER_HOUR)
            self._gutter.attach(label, 0, hour, 1, 1)

    def _build_columns(self) -> None:
        """One grid per day, with the hour rules drawn into it."""
        clear_children(self._columns_box)
        self._day_grids.clear()
        row_height = self._row_height()

        for offset in range(self.day_count):
            day = self._first_day + timedelta(days=offset)
            grid = Gtk.Grid(row_homogeneous=True, column_homogeneous=True)
            grid.add_css_class("kairos-day-column")
            grid.set_hexpand(True)
            grid.set_size_request(-1, row_height * ROWS_PER_DAY)
            grid.day = day  # type: ignore[attr-defined]

            # An empty grid has nothing to draw, so each hour gets a thin
            # spacer that carries the rule.  24 per day is cheap.
            for hour in range(24):
                rule = Gtk.Box()
                rule.add_css_class("kairos-hour-row")
                rule.set_size_request(-1, row_height * ROWS_PER_HOUR)
                grid.attach(rule, 0, hour * ROWS_PER_HOUR, MAX_OVERLAP_COLUMNS, ROWS_PER_HOUR)

            on_click(grid, lambda n, x, y, g=grid: self._on_column_click(g, n, x, y))
            self._columns_box.append(grid)
            self._day_grids.append(grid)

    def _on_column_click(self, grid: Gtk.Grid, n_press: int, _x: float, y: float) -> None:
        """Clicking empty space in a column selects (or creates) that time."""
        day: date = grid.day  # type: ignore[attr-defined]
        row = int(y // max(1, self._row_height()))
        minutes = min(ROWS_PER_DAY - 1, max(0, row)) * MINUTES_PER_ROW
        moment = start_of_day(day) + timedelta(minutes=minutes)
        self._anchor = day
        self.emit("date-selected", day)
        if n_press >= 2:
            self.emit("create-requested", moment)

    # ------------------------------------------------------------------
    # Filling in
    # ------------------------------------------------------------------

    def refresh(self) -> None:
        self._build_gutter()
        self._build_columns()
        self._build_headings()

        window_start = start_of_day(self._first_day)
        window_end = window_start + timedelta(days=self.day_count)
        events = self.sync.events_between(window_start, window_end)
        occurrences = recurrence.expand(events, window_start, window_end)
        by_day = recurrence.group_by_day(occurrences, self._first_day, self.day_count)
        colours = {c.id: c.colour for c in self.sync.calendars()}

        self._fill_all_day(by_day, colours)
        for offset, grid in enumerate(self._day_grids):
            day = self._first_day + timedelta(days=offset)
            timed = [o for o in by_day.get(day, []) if not self._is_banner(o)]
            self._fill_day(grid, day, timed, colours)

        self._draw_now_line()
        if not self._scrolled_once and settings.get_bool("week_starts_scrolled_to_now"):
            self._scroll_when_ready()
            self._scrolled_once = True

    @staticmethod
    def _is_banner(occurrence: Occurrence) -> bool:
        return occurrence.all_day or occurrence.first_day != occurrence.last_day

    def _build_headings(self) -> None:
        clear_children(self._header_days)
        today = date.today()

        for offset in range(self.day_count):
            day = self._first_day + timedelta(days=offset)
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
            box.set_hexpand(True)

            name = Gtk.Label(label=formatting.DAY_ABBREVIATIONS[day.weekday()])
            name.add_css_class("kairos-weekday-heading")
            if day.weekday() >= 5 and settings.get_bool("highlight_weekends"):
                name.add_css_class("kairos-weekend")
            box.append(name)

            number = Gtk.Label(label=str(day.day))
            number.add_css_class("kairos-day-number")
            if day == today:
                number.add_css_class("kairos-accent-bg")
                style_widget(number, "color: #ffffff;")
            box.append(number)

            button = Gtk.Button()
            button.add_css_class("flat")
            button.set_child(box)
            button.set_hexpand(True)
            button.connect("clicked", lambda _b, d=day: self.emit("day-activated", d))
            self._header_days.append(button)

    def _fill_all_day(self, by_day: dict, colours: dict) -> None:
        """The strip of all-day and multi-day banners above the grid.

        A banner is drawn **once**, spanning every day it covers, rather than
        repeated in each day's column: a Wednesday-to-Friday trip is a single
        bar three columns wide, which is both what it is and much easier to
        read than three identical chips that may not even line up.
        """
        clear_children(self._all_day_days)

        # ``by_day`` lists an occurrence once per day it covers, so collapse
        # it back to one entry each, clipped to the days actually on screen.
        spans: dict[tuple[str, datetime], tuple[Occurrence, int, int]] = {}
        for offset in range(self.day_count):
            day = self._first_day + timedelta(days=offset)
            for occurrence in by_day.get(day, []):
                key = (occurrence.uid, occurrence.start)
                if not self._is_banner(occurrence) or key in spans:
                    continue
                spans[key] = (
                    occurrence,
                    max(0, (occurrence.first_day - self._first_day).days),
                    min(self.day_count - 1, (occurrence.last_day - self._first_day).days),
                )

        # Row 0 holds an empty box per day so the grid knows it has a column
        # for every day, including ones with no banner; without them an empty
        # Saturday would take no width and the rest would spread out to fill.
        for offset in range(self.day_count):
            self._all_day_days.attach(Gtk.Box(), offset, 0, 1, 1)

        for occurrence, first, last, row in assign_banner_rows(list(spans.values())):
            chip = self._make_chip(occurrence, colours,
                                   css_class="kairos-all-day-chip")
            self._all_day_days.attach(chip, first, row + 1, last - first + 1, 1)

        self._all_day_strip.set_visible(bool(spans))

    def _fill_day(self, grid: Gtk.Grid, day: date, occurrences: list[Occurrence], colours: dict) -> None:
        """Attach one day's timed events to its grid."""
        row_height = self._row_height()
        # How many rows one line of text needs.  A quarter-hour meeting is a
        # single row, which is far too short to draw a label in; giving it
        # this many instead keeps every row the height we intended.
        least_rows = max(1, -(-MIN_BLOCK_PIXELS // row_height))

        for occurrence, column, width in assign_columns(occurrences):
            start_row = row_for(occurrence.start, day)
            end_row = max(start_row + 1, row_for(occurrence.end, day))
            span = max(end_row - start_row, least_rows)
            span = min(span, ROWS_PER_DAY - start_row)

            show_time = span * row_height >= TIME_LABEL_PIXELS
            block = self._make_block(occurrence, colours, show_time=show_time)
            # The grid is MAX_OVERLAP_COLUMNS wide; an event that shares its
            # slot with nobody spans all of them.
            slot_width = max(1, MAX_OVERLAP_COLUMNS // width)
            grid.attach(block, column * slot_width, start_row, slot_width, span)

    def _make_block(self, occurrence: Occurrence, colours: dict, *, show_time: bool = True) -> Gtk.Widget:
        colour = colours.get(occurrence.calendar_id, "#3584e4")
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        title = Gtk.Label(label=occurrence.summary, xalign=0)
        title.set_ellipsize(3)
        content.append(title)

        # The second line only goes in when the caller has confirmed the
        # block is tall enough for it; see _fill_day.
        if show_time:
            when = Gtk.Label(label=formatting.format_time_range(occurrence), xalign=0)
            when.set_ellipsize(3)
            when.set_opacity(0.8)
            content.append(when)

        button = Gtk.Button()
        button.set_child(content)
        button.add_css_class("kairos-event-block")
        button.set_valign(Gtk.Align.FILL)
        button.set_hexpand(True)
        style_widget(button, tinted_button_css(colour))
        button.set_tooltip_text(f"{occurrence.summary}\n{formatting.format_time_range(occurrence)}")
        button.connect("clicked", lambda b: self.emit("event-activated", occurrence, b))
        mark_event_widget(button, occurrence)
        return button

    def _make_chip(self, occurrence: Occurrence, colours: dict, css_class: str) -> Gtk.Widget:
        colour = colours.get(occurrence.calendar_id, "#3584e4")
        label = Gtk.Label(label=occurrence.summary, xalign=0)
        label.set_ellipsize(3)
        button = Gtk.Button()
        button.set_child(label)
        button.add_css_class(css_class)
        style_widget(button, tinted_button_css(colour))
        button.connect("clicked", lambda b: self.emit("event-activated", occurrence, b))
        mark_event_widget(button, occurrence)
        return button

    # ------------------------------------------------------------------
    # "You are here"
    # ------------------------------------------------------------------

    def _draw_now_line(self) -> None:
        """A thin accent line across today's column at the current time."""
        if self._now_line is not None:
            self._overlay.remove_overlay(self._now_line)
            self._now_line = None

        today = date.today()
        if not (self._first_day <= today < self._first_day + timedelta(days=self.day_count)):
            return

        now = datetime.now(tz=local_timezone())
        offset_rows = (now.hour * 60 + now.minute) / MINUTES_PER_ROW

        line = Gtk.Box()
        line.add_css_class("kairos-now-line")
        line.add_css_class("kairos-accent-bg")
        line.set_valign(Gtk.Align.START)
        line.set_halign(Gtk.Align.FILL)
        line.set_can_target(False)   # never swallow a click meant for the grid
        line.set_margin_top(int(offset_rows * self._row_height()))

        self._overlay.add_overlay(line)
        self._now_line = line

    def _scroll_when_ready(self) -> None:
        """Scroll once the scroller knows how tall its contents are.

        Doing this straight after building would be too early: the adjustment
        still reads 0/0 until GTK has allocated the grid, so the scroll would
        silently do nothing.  Waiting for the adjustment to report a real
        range is the reliable moment.
        """
        adjustment = self._scroller.get_vadjustment()

        def try_scroll(*_args) -> None:
            if adjustment.get_upper() <= adjustment.get_page_size():
                return  # not laid out yet; we will be called again
            adjustment.disconnect(handler)
            self._scroll_to_interesting_hour()

        handler = adjustment.connect("changed", try_scroll)

    def _scroll_to_interesting_hour(self) -> bool:
        """Open the view somewhere useful rather than at midnight."""
        today = date.today()
        if self._first_day <= today < self._first_day + timedelta(days=self.day_count):
            hour = max(0, datetime.now().hour - 1)
        else:
            hour = settings.get_int("week_view_start_hour")
        adjustment = self._scroller.get_vadjustment()
        adjustment.set_value(min(hour * settings.get_int("hour_height"),
                                 max(0, adjustment.get_upper() - adjustment.get_page_size())))
        return GLib.SOURCE_REMOVE


class DayView(WeekView):
    """The week view, narrowed to a single day."""

    def __init__(self, sync_manager) -> None:
        super().__init__(sync_manager, day_count=1)

    @property
    def heading(self) -> str:
        return formatting.format_date(self._first_day)
