"""The agenda: a plain scrolling list of what is coming up.

The most useful view on a small window, and the cheapest to draw — no grid,
no positioning, just a heading per day and a row per event.  Days with nothing
in them are skipped rather than shown empty, so the list stays dense.
"""

from __future__ import annotations

from datetime import date, timedelta

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GObject, Gtk  # noqa: E402

from kairos import formatting, recurrence
from kairos.config import settings
from kairos.models import Occurrence, start_of_day
from kairos.ui.widgets import (clear_children, colour_swatch, empty_state,
                               mark_event_widget)


class AgendaView(Gtk.Box):
    """Upcoming events, grouped by day."""

    __gsignals__ = {
        "event-activated": (GObject.SignalFlags.RUN_FIRST, None, (object, object)),
        "create-requested": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "day-activated": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "date-selected": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
    }

    def __init__(self, sync_manager) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.sync = sync_manager
        self._first_day = date.today()

        self._scroller = Gtk.ScrolledWindow()
        self._scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._scroller.set_vexpand(True)
        self.append(self._scroller)

        self._list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._list.set_margin_bottom(24)
        self._scroller.set_child(self._list)

    # ------------------------------------------------------------------
    # The view contract
    # ------------------------------------------------------------------

    @property
    def heading(self) -> str:
        last = self._first_day + timedelta(days=self._span() - 1)
        return formatting.format_range_heading(self._first_day, last)

    @property
    def selected_day(self) -> date:
        return self._first_day

    def _span(self) -> int:
        return settings.get_int("agenda_days")

    def set_date(self, day: date) -> None:
        self._first_day = day
        self.refresh()

    def go_previous(self) -> None:
        self.set_date(self._first_day - timedelta(days=self._span()))

    def go_next(self) -> None:
        self.set_date(self._first_day + timedelta(days=self._span()))

    # ------------------------------------------------------------------
    # Drawing
    # ------------------------------------------------------------------

    def refresh(self) -> None:
        clear_children(self._list)

        span = self._span()
        window_start = start_of_day(self._first_day)
        window_end = window_start + timedelta(days=span)
        events = self.sync.events_between(window_start, window_end)
        occurrences = recurrence.expand(events, window_start, window_end)
        by_day = recurrence.group_by_day(occurrences, self._first_day, span)
        colours = {c.id: c.colour for c in self.sync.calendars()}

        if not occurrences:
            self._list.append(empty_state(
                "Nothing scheduled",
                f"There are no events in the next {span} days.",
            ))
            return

        today = date.today()
        for offset in range(span):
            day = self._first_day + timedelta(days=offset)
            day_occurrences = by_day.get(day, [])
            if not day_occurrences:
                continue

            heading = Gtk.Label(label=formatting.format_day_heading(day), xalign=0)
            heading.add_css_class("kairos-agenda-day")
            if day == today:
                heading.add_css_class("today")
            self._list.append(heading)

            for occurrence in day_occurrences:
                self._list.append(self._make_row(occurrence, colours))

    def _make_row(self, occurrence: Occurrence, colours: dict) -> Gtk.Widget:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        row.add_css_class("kairos-agenda-row")

        row.append(colour_swatch(colours.get(occurrence.calendar_id, "#3584e4")))

        when = Gtk.Label(label=formatting.format_time_range(occurrence), xalign=0)
        when.add_css_class("kairos-agenda-time")
        row.append(when)

        details = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        details.set_hexpand(True)

        title = Gtk.Label(label=occurrence.summary, xalign=0)
        title.set_ellipsize(3)
        details.append(title)

        if occurrence.event.location:
            location = Gtk.Label(label=occurrence.event.location, xalign=0)
            location.set_ellipsize(3)
            location.add_css_class("dim-label")
            location.add_css_class("caption")
            details.append(location)

        row.append(details)

        button = Gtk.Button()
        button.add_css_class("flat")
        button.set_child(row)
        button.connect("clicked", lambda b: self.emit("event-activated", occurrence, b))
        mark_event_widget(button, occurrence)
        return button
