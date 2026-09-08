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
gi.require_version("Graphene", "1.0")
from gi.repository import GLib, GObject, Graphene, Gtk  # noqa: E402

from kairos import formatting, recurrence
from kairos.config import settings
from kairos.models import Occurrence, local_timezone, start_of_day
from kairos.ui.widgets import (assign_banner_rows, clear_children,
                               mark_event_widget, on_click, style_widget,
                               tinted_button_css)

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


#: How close to a block's bottom edge a press has to be to mean "resize"
#: rather than "move".  Small enough not to steal ordinary drags, big enough
#: to hit without care.
RESIZE_GRIP_PIXELS = 8

#: ...but never more than this share of a block's height.  A quarter-hour
#: meeting in a dense grid is only a dozen pixels tall, and a fixed grip
#: would swallow the whole of it — leaving an event that could be resized
#: but never moved.
RESIZE_GRIP_SHARE = 1 / 3

#: How far the pointer must travel before a press counts as a drag rather
#: than a click.
DRAG_THRESHOLD_PIXELS = 4

#: What a drag snaps to.  A block can be *drawn* at any minute (see
#: :meth:`WeekView._placement`, which is what lets a 9:05 meeting from a
#: server appear at 9:05), so this is not a limit of the grid — it is a
#: judgement about the gesture.  Five-minute steps made a drag fiddly to
#: land where you meant, and almost nobody schedules on the odd five.
DRAG_SNAP_MINUTES = 15

#: The shortest an event can be dragged down to.
MIN_EVENT_MINUTES = 5

#: What clicking an empty part of the grid snaps to. Coarser than a drag:
#: a click is a rougher gesture, and a quarter past is a likelier thing to
#: mean than seven minutes past.
SLOT_SNAP_MINUTES = 15

MINUTES_PER_DAY = 24 * 60


def dragged_times(occurrence: Occurrence, *, minutes: int, days: int,
                  resizing: bool) -> tuple[datetime, datetime]:
    """Where a drag of ``minutes`` and ``days`` leaves an event.

    Pure arithmetic, kept out of the gesture handler so it can be tested
    without a pointer.  Two rules it enforces, both of which come from
    dragging past an edge:

    * an event cannot be dragged out of its day — the grid has nowhere to
      draw it, so it stops at midnight either end;
    * a resize cannot make an event end before it starts, so it stops at
      :data:`MIN_EVENT_MINUTES`.

    An all-day event has no time to change, only a day, so ``minutes`` is
    ignored for one and its whole span moves together.
    """
    day = occurrence.first_day
    if occurrence.all_day:
        shift = timedelta(days=days)
        return occurrence.start + shift, occurrence.end + shift

    start_minutes = occurrence.start.hour * 60 + occurrence.start.minute
    length = max(MIN_EVENT_MINUTES,
                 int((occurrence.end - occurrence.start).total_seconds() // 60))

    if resizing:
        new_start = start_minutes
        new_end = min(MINUTES_PER_DAY,
                      max(start_minutes + MIN_EVENT_MINUTES,
                          start_minutes + length + minutes))
    else:
        new_start = min(MINUTES_PER_DAY - length,
                        max(0, start_minutes + minutes))
        new_end = new_start + length

    midnight = start_of_day(day + timedelta(days=days))
    return (midnight + timedelta(minutes=new_start),
            midnight + timedelta(minutes=new_end))


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


def _is_empty_space(container: Gtk.Widget, x: float, y: float) -> bool:
    """Whether a click at this point hit the background rather than an event.

    A view puts its click handler on the whole day column, so a press on an
    event reaches the column's gesture as well as the event's own. Asking
    GTK what is actually under the pointer is the reliable way to tell them
    apart — the alternative, stopping propagation from every chip, has to be
    remembered in each of the several places a chip is built.
    """
    picked = container.pick(x, y, Gtk.PickFlags.DEFAULT)
    while picked is not None and picked is not container:
        if isinstance(picked, Gtk.Button):
            return False
        picked = picked.get_parent()
    return True


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
        # (occurrence, new start, new end) — emitted when a block is dragged.
        "event-moved": (GObject.SignalFlags.RUN_FIRST, None, (object, object, object)),
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
        self._drag: dict = {}
        self._indicator: Gtk.Widget | None = None
        #: The slot a first click picked, waiting for a second to confirm it.
        self._slot: tuple[date, int] | None = None
        self._slot_widget: Gtk.Widget | None = None
        self._calendar_cache = None
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

    def _on_column_click(self, grid: Gtk.Grid, n_press: int, x: float, y: float) -> None:
        """Clicking empty space picks a time; clicking it again creates there.

        One click is not enough on its own — the pointer lands somewhere
        approximate, and an event silently appearing at a time nobody chose
        is worse than one more click. So the first click shows exactly the
        slot a new event would fill, and the second confirms it. A
        double-click skips the wait, which is what people used to it expect.
        """
        if not _is_empty_space(grid, x, y):
            # The click landed on an event. Its own handler opens it; the
            # grid must not also treat it as "somewhere to put a new event",
            # which is what made a slot outline appear behind every event
            # anyone clicked.
            return

        day: date = grid.day  # type: ignore[attr-defined]
        row = int(y // max(1, self._row_height()))
        minutes = min(ROWS_PER_DAY - 1, max(0, row)) * MINUTES_PER_ROW
        minutes -= minutes % SLOT_SNAP_MINUTES
        moment = start_of_day(day) + timedelta(minutes=minutes)

        self._anchor = day
        self.emit("date-selected", day)

        if n_press >= 2 or self._slot == (day, minutes):
            self.clear_slot()
            self.emit("create-requested", moment)
            return

        self._slot = (day, minutes)
        self._show_slot()

    # -- the pending "new event here" slot -----------------------------

    def clear_slot(self) -> None:
        """Forget the highlighted slot, and take its highlight down."""
        self._slot = None
        if self._slot_widget is not None and self._slot_widget.get_parent() is not None:
            self._slot_widget.get_parent().remove(self._slot_widget)
        self._slot_widget = None

    def _show_slot(self) -> None:
        """Outline the slot a new event would occupy.

        Sized to the length a new event actually gets, so the highlight is a
        preview rather than a marker — you can see it is an hour before you
        commit to it.
        """
        chosen = self._slot
        self.clear_slot()
        self._slot = chosen
        if chosen is None:
            return
        day, minutes = chosen
        column = (day - self._first_day).days
        if not 0 <= column < len(self._day_grids):
            return

        row_height = self._row_height()
        length = max(SLOT_SNAP_MINUTES,
                     settings.get_int("default_event_duration_minutes"))
        start_row = minutes // MINUTES_PER_ROW
        top = round((minutes % MINUTES_PER_ROW) / MINUTES_PER_ROW * row_height)
        height = max(MIN_BLOCK_PIXELS, round(length / MINUTES_PER_ROW * row_height))
        span = max(1, min(-(-(top + height) // max(1, row_height)),
                          ROWS_PER_DAY - start_row))

        label = Gtk.Label(label=formatting.format_time(
            start_of_day(day) + timedelta(minutes=minutes)), xalign=0.5)
        label.set_ellipsize(3)
        label.set_hexpand(True)
        label.set_valign(Gtk.Align.CENTER)
        widget = Gtk.Box()
        widget.append(label)
        widget.add_css_class("kairos-slot-selection")
        widget.set_margin_top(top)
        widget.set_margin_bottom(max(0, span * row_height - top - height))
        self._day_grids[column].attach(widget, 0, start_row,
                                       MAX_OVERLAP_COLUMNS, span)
        self._slot_widget = widget

    # ------------------------------------------------------------------
    # Filling in
    # ------------------------------------------------------------------

    def refresh(self) -> None:
        self._calendar_cache = None      # colours and read-only flags may have moved
        chosen, self._slot_widget = self._slot, None
        self._slot = None
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

        # The grids were rebuilt, so the old highlight widget is gone; put a
        # fresh one back if the choice it stood for is still on screen.
        if chosen is not None:
            self._slot = chosen
            self._show_slot()

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
            start_row, top, span, bottom = self._placement(occurrence, day,
                                                           row_height, least_rows)
            show_time = (span * row_height - top - bottom) >= TIME_LABEL_PIXELS
            block = self._make_block(occurrence, colours, show_time=show_time)
            # A grid row is fifteen minutes, but an event need not begin on
            # one: the margins place it exactly inside the rows it spans, so
            # a 9:05 meeting is drawn at 9:05 rather than at 9:00.
            block.set_margin_top(top)
            block.set_margin_bottom(bottom)
            # The grid is MAX_OVERLAP_COLUMNS wide; an event that shares its
            # slot with nobody spans all of them.
            slot_width = max(1, MAX_OVERLAP_COLUMNS // width)
            grid.attach(block, column * slot_width, start_row, slot_width, span)

    @staticmethod
    def _placement(occurrence: Occurrence, day: date, row_height: int,
                   least_rows: int) -> tuple[int, int, int, int]:
        """``(first row, top margin, rows spanned, bottom margin)`` for a block.

        The rows put the block roughly in place and the margins put it
        exactly, which is what lets an event start and end at any minute on
        a grid whose rows are quarter hours.
        """
        start_minutes = (occurrence.start.hour * 60 + occurrence.start.minute
                         if occurrence.start.date() == day else 0)
        end_minutes = (occurrence.end.hour * 60 + occurrence.end.minute
                       if occurrence.end.date() == day else MINUTES_PER_DAY)
        length = max(1, end_minutes - start_minutes)

        start_row = start_minutes // MINUTES_PER_ROW
        top = round((start_minutes % MINUTES_PER_ROW) / MINUTES_PER_ROW * row_height)
        height = max(MIN_BLOCK_PIXELS, round(length / MINUTES_PER_ROW * row_height))

        span = max(least_rows, -(-(top + height) // max(1, row_height)))
        span = max(1, min(span, ROWS_PER_DAY - start_row))
        bottom = max(0, span * row_height - top - height)
        return start_row, top, span, bottom

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
        self._make_draggable(button, occurrence)
        return button

    # ------------------------------------------------------------------
    # Dragging a block to move or resize it
    # ------------------------------------------------------------------
    #
    # The dragged widget is deliberately *not* moved while the drag is in
    # progress.  Gtk.GestureDrag reports offsets from where the press landed
    # in the dragged widget's own coordinates, so moving that widget moves
    # the origin with it and the offset collapses back towards zero — which
    # is why an earlier version could shift an event by one row and then no
    # further, however far the pointer went.
    #
    # Instead the block stays put and dims, and a separate indicator shows
    # where it will land.

    def _make_draggable(self, widget: Gtk.Widget, occurrence: Occurrence, *,
                        all_day: bool = False) -> None:
        """Let a block or banner be dragged to a new time or day.

        Read-only calendars are left alone: offering a drag that silently
        does nothing is worse than not offering one.
        """
        calendar = self._calendars_by_id().get(occurrence.calendar_id)
        if calendar is not None and calendar.read_only:
            return

        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._on_drag_begin, widget, occurrence, all_day)
        drag.connect("drag-update", self._on_drag_update, widget, occurrence, all_day)
        drag.connect("drag-end", self._on_drag_end, widget, occurrence, all_day)
        widget.add_controller(drag)

    def _calendars_by_id(self) -> dict:
        if getattr(self, "_calendar_cache", None) is None:
            self._calendar_cache = {c.id: c for c in self.sync.calendars()}
        return self._calendar_cache

    # -- the gesture ---------------------------------------------------

    def _on_drag_begin(self, _gesture, start_x, start_y, widget, occurrence,
                       all_day=False) -> None:
        height = widget.get_allocated_height()
        self._drag = {
            # A press near the bottom edge means "change when this ends";
            # anywhere else means "move the whole thing".  A block whose
            # height is not known yet is treated as a move: that is the
            # commoner action, and resizing by an unknown amount is the
            # worse thing to guess at.  An all-day banner has no end to
            # drag, only a day.
            "resizing": (not all_day and height > 0
                         and height - start_y <= min(RESIZE_GRIP_PIXELS,
                                                     height * RESIZE_GRIP_SHARE)),
            "all_day": all_day,
            "start": (start_x, start_y),
            "minutes": 0,
            "days": 0,
            "moved": False,
        }
        widget.add_css_class("kairos-dragging")

    def _on_drag_update(self, _gesture, offset_x, offset_y, widget, occurrence,
                        all_day=False) -> None:
        if not self._drag:
            return

        if abs(offset_x) > DRAG_THRESHOLD_PIXELS or abs(offset_y) > DRAG_THRESHOLD_PIXELS:
            self._drag["moved"] = True

        minutes = 0 if all_day else self._snapped_minutes(offset_y)
        days = 0 if self._drag["resizing"] else self._target_day(
            widget, occurrence, offset_x, offset_y, all_day)

        if (minutes, days) == (self._drag["minutes"], self._drag["days"]):
            return
        self._drag["minutes"], self._drag["days"] = minutes, days
        if self._drag["moved"]:
            self._show_indicator(occurrence)

    def _on_drag_end(self, _gesture, _offset_x, _offset_y, widget, occurrence,
                     all_day=False) -> None:
        state, self._drag = self._drag, {}
        widget.remove_css_class("kairos-dragging")
        self._clear_indicator()
        if not state or not state.get("moved"):
            # A press that never really moved is a click, and the button's
            # own "clicked" handler will open the event.
            return

        start, end = dragged_times(occurrence, minutes=state["minutes"],
                                   days=state["days"],
                                   resizing=state["resizing"])
        if (start, end) == (occurrence.start, occurrence.end):
            return
        self.emit("event-moved", occurrence, start, end)

    # -- where the pointer is ------------------------------------------

    def _snapped_minutes(self, offset_y: float) -> int:
        """How far the drag has moved in time, snapped to the grid's step."""
        row_height = max(1, self._row_height())
        minutes = offset_y / row_height * MINUTES_PER_ROW
        return int(round(minutes / DRAG_SNAP_MINUTES) * DRAG_SNAP_MINUTES)

    def _target_day(self, widget, occurrence, offset_x, offset_y,
                    all_day: bool) -> int:
        """How many days sideways the pointer now is, from the event's own day.

        Worked out from where the pointer actually *is* rather than from how
        far it has travelled, so it cannot drift out of step with the columns
        it is being dragged over.
        """
        container = self._all_day_days if all_day else self._columns_box
        if container is None or self.day_count <= 0:
            return 0
        width = container.get_allocated_width()
        if width <= 0:
            return 0

        start_x, start_y = self._drag["start"]
        point = Graphene.Point()
        point.init(start_x + offset_x, start_y + offset_y)
        found, here = widget.compute_point(container, point)
        if not found:
            return 0

        column_width = width / self.day_count
        column = int(max(0, min(self.day_count - 1, here.x // column_width)))
        origin = (occurrence.first_day - self._first_day).days
        return column - origin

    # -- showing where it will land ------------------------------------

    def _clear_indicator(self) -> None:
        if self._indicator is not None and self._indicator.get_parent() is not None:
            self._indicator.get_parent().remove(self._indicator)
        self._indicator = None

    def _show_indicator(self, occurrence: Occurrence) -> None:
        """Draw an outline where the event would end up.

        Without this the only feedback was the block itself moving, which it
        no longer does — and which never showed the *day* it would land on.
        """
        self._clear_indicator()
        start, end = dragged_times(occurrence, minutes=self._drag["minutes"],
                                   days=self._drag["days"],
                                   resizing=self._drag["resizing"])
        column = (start.date() - self._first_day).days
        if not 0 <= column < len(self._day_grids):
            return

        # The time is what a drop needs to tell you; the column already
        # says which day, so the weekday is added only when the drag has
        # actually left the day it started in.
        when = formatting.format_time(start)
        if self._drag["days"]:
            when = f"{formatting.DAY_ABBREVIATIONS[start.weekday()]} {when}"
        label = Gtk.Label(label=when, xalign=0.5)
        label.set_ellipsize(3)
        label.set_hexpand(True)
        label.set_valign(Gtk.Align.CENTER)
        indicator = Gtk.Box()
        indicator.append(label)
        indicator.add_css_class("kairos-drop-indicator")

        if self._drag["all_day"]:
            label.set_label(formatting.format_date_short(start.date()))

            self._all_day_days.attach(indicator, column, 0, 1, 1)
        else:
            grid = self._day_grids[column]
            day = start.date()
            first_row = row_for(start, day)
            span = max(1, min(ROWS_PER_DAY - first_row,
                              row_for(end, day) - first_row))
            grid.attach(indicator, 0, first_row, MAX_OVERLAP_COLUMNS, span)
        self._indicator = indicator

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
        # All-day banners drag sideways between days; there is no time on
        # them to change.
        self._make_draggable(button, occurrence, all_day=True)
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
