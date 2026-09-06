"""Searching for an event.

The results page. It is an ordinary view in the same stack as the month and
week grids, shown while a search is running and swapped back out when it ends.

**Which date a result shows.** A search matches an *event*, but an event that
repeats has no single date. Showing the master's start would be unhelpful — a
weekly meeting set up two years ago would be listed under a date two years
ago — so for a repeating event the next occurrence from today is found and
shown instead, falling back to the most recent one if the series has ended.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import GObject, Gtk  # noqa: E402

from kairos import formatting, recurrence
from kairos.models import Event, Occurrence, local_timezone
from kairos.ui.widgets import clear_children, colour_swatch, empty_state

#: More than this and the list stops being useful; refine the search instead.
MAX_RESULTS = 60


class SearchView(Gtk.Box):
    """A list of events matching what was typed."""

    __gsignals__ = {
        "event-activated": (GObject.SignalFlags.RUN_FIRST, None, (object, object)),
    }

    def __init__(self, sync_manager) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.sync = sync_manager
        self._query = ""

        self._scroller = Gtk.ScrolledWindow()
        self._scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._scroller.set_vexpand(True)
        self.append(self._scroller)

        self._list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._list.set_margin_bottom(24)
        self._scroller.set_child(self._list)

        self.search("")

    # ------------------------------------------------------------------

    @property
    def query(self) -> str:
        return self._query

    def search(self, text: str) -> None:
        self._query = text.strip()
        clear_children(self._list)

        if not self._query:
            self._list.append(empty_state(
                "Search your calendars",
                "Type a title, a place, or anything in an event's notes.",
                icon="system-search-symbolic",
            ))
            return

        results = self._results(self._query)
        if not results:
            self._list.append(empty_state(
                "No events found",
                f"Nothing matches “{self._query}”.",
                icon="system-search-symbolic",
            ))
            return

        heading = Gtk.Label(
            label=f"{len(results)} result{'s' if len(results) != 1 else ''}",
            xalign=0,
        )
        heading.add_css_class("kairos-agenda-day")
        self._list.append(heading)

        colours = {c.id: c.colour for c in self.sync.calendars()}
        names = {c.id: c.name for c in self.sync.calendars()}
        for occurrence in results:
            self._list.append(self._make_row(occurrence, colours, names))

    # ------------------------------------------------------------------

    def _results(self, text: str) -> list[Occurrence]:
        """Matching events, each dated by the occurrence worth showing."""
        found = []
        for event in self.sync.search(text)[:MAX_RESULTS]:
            occurrence = self._representative(event)
            if occurrence is not None:
                found.append(occurrence)

        # Soonest first among things still to come, then the past, most
        # recent first — what you are looking for is usually ahead of you.
        now = datetime.now(tz=local_timezone())
        future = sorted((o for o in found if o.end >= now), key=lambda o: o.start)
        past = sorted((o for o in found if o.end < now), key=lambda o: o.start, reverse=True)
        return future + past

    def _representative(self, event: Event) -> Occurrence | None:
        """The occurrence of ``event`` a search result should be dated by."""
        if not event.is_recurring:
            return Occurrence(event=event, start=event.start, end=event.end)

        now = datetime.now(tz=local_timezone())
        upcoming = recurrence.next_occurrence_after(event, now)
        if upcoming is not None:
            return upcoming

        # The series has finished; show its most recent occurrence.
        past = recurrence.expand([event], now - timedelta(days=730), now)
        return max(past, key=lambda o: o.start) if past else None

    def _make_row(self, occurrence: Occurrence, colours: dict, names: dict) -> Gtk.Widget:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        row.add_css_class("kairos-agenda-row")

        row.append(colour_swatch(colours.get(occurrence.calendar_id, "#3584e4")))

        when = Gtk.Label(label=formatting.format_date_short(occurrence.first_day), xalign=0)
        when.add_css_class("kairos-agenda-time")
        row.append(when)

        details = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        details.set_hexpand(True)

        title = Gtk.Label(label=occurrence.summary, xalign=0)
        title.set_ellipsize(3)
        details.append(title)

        parts = [formatting.format_time_range(occurrence)]
        if occurrence.event.location:
            parts.append(occurrence.event.location)
        name = names.get(occurrence.calendar_id)
        if name:
            parts.append(name)
        if occurrence.event.is_recurring:
            parts.append("repeats")

        subtitle = Gtk.Label(label=" · ".join(parts), xalign=0)
        subtitle.set_ellipsize(3)
        subtitle.add_css_class("dim-label")
        subtitle.add_css_class("caption")
        details.append(subtitle)

        row.append(details)

        button = Gtk.Button()
        button.add_css_class("flat")
        button.set_child(row)
        button.connect("clicked", lambda b: self.emit("event-activated", occurrence, b))
        return button
