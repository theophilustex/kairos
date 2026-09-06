"""The "Up next" list in the sidebar.

A short, ordered list of what is coming — a colour bar for the calendar, the
time, and the title — grouped under day headings. It answers "what is next?"
without leaving whichever view you are in, which is the one question a
calendar gets asked most often.

Deliberately short. It shows the next handful of events over the next few
weeks, both of which are preferences; the agenda view is there for the full
list.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GObject, Gtk  # noqa: E402

from kairos import formatting, recurrence
from kairos.config import settings
from kairos.models import Occurrence, local_timezone, start_of_day
from kairos.ui.widgets import clear_children, style_widget


class UpcomingList(Gtk.Box):
    """What is coming up, in order.

    Emits ``event-activated(occurrence, widget)`` like the calendar views do,
    so the window can open the same detail popover it uses everywhere else.
    """

    __gsignals__ = {
        "event-activated": (GObject.SignalFlags.RUN_FIRST, None, (object, object)),
    }

    def __init__(self, sync_manager) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.sync = sync_manager
        self.add_css_class("kairos-upcoming")
        self.refresh()

    # ------------------------------------------------------------------

    def refresh(self) -> None:
        clear_children(self)

        for widget in self._build_rows():
            self.append(widget)

    def _occurrences(self) -> list[Occurrence]:
        """The next few events, starting from now rather than from midnight.

        Something that finished an hour ago is not "up next", but something
        happening right now is — so the window starts at the top of the
        current hour and anything already over is dropped.
        """
        now = datetime.now(tz=local_timezone())
        days = settings.get_int("sidebar_upcoming_days")
        window_start = start_of_day(now.date())
        window_end = window_start + timedelta(days=days + 1)

        events = self.sync.events_between(window_start, window_end)
        found = [
            occurrence
            for occurrence in recurrence.expand(events, window_start, window_end)
            if occurrence.end > now or (occurrence.all_day and occurrence.last_day >= now.date())
        ]
        found.sort(key=lambda o: (o.start, o.summary.lower()))
        return found[: settings.get_int("sidebar_upcoming_count")]

    def _build_rows(self) -> list[Gtk.Widget]:
        occurrences = self._occurrences()
        if not occurrences:
            label = Gtk.Label(label="Nothing coming up", xalign=0)
            label.add_css_class("kairos-upcoming-empty")
            return [label]

        colours = {c.id: c.colour for c in self.sync.calendars()}
        widgets: list[Gtk.Widget] = []
        heading_shown: date | None = None

        for occurrence in occurrences:
            day = occurrence.first_day
            if day != heading_shown:
                heading_shown = day
                heading = Gtk.Label(label=self._day_label(day), xalign=0)
                heading.add_css_class("kairos-upcoming-day")
                widgets.append(heading)
            widgets.append(self._make_row(occurrence, colours))
        return widgets

    @staticmethod
    def _day_label(day: date) -> str:
        """"Today", "Tomorrow", or "Wed 9 Sep" — short enough for a sidebar."""
        today = date.today()
        if day == today:
            return "Today"
        if day == today + timedelta(days=1):
            return "Tomorrow"
        if day < today + timedelta(days=7):
            return formatting.DAY_NAMES[day.weekday()]
        return (f"{formatting.DAY_ABBREVIATIONS[day.weekday()]} {day.day} "
                f"{formatting.MONTH_NAMES[day.month - 1][:3]}")

    def _make_row(self, occurrence: Occurrence, colours: dict) -> Gtk.Widget:
        colour = colours.get(occurrence.calendar_id, "#3584e4")

        # A vertical bar in the calendar's colour, rather than a dot: at this
        # size it reads as "belongs to that calendar" more clearly, and it
        # lines the rows up.
        bar = Gtk.Box()
        bar.add_css_class("kairos-upcoming-bar")
        bar.set_size_request(3, -1)
        style_widget(bar, f"background-color: {colour};")

        when = Gtk.Label(label=self._time_label(occurrence), xalign=0)
        when.add_css_class("kairos-upcoming-time")

        title = Gtk.Label(label=occurrence.summary, xalign=0)
        title.set_ellipsize(3)          # Pango.EllipsizeMode.END
        title.set_hexpand(True)
        title.add_css_class("kairos-upcoming-title")

        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        text.append(title)
        text.append(when)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row.append(bar)
        row.append(text)

        button = Gtk.Button()
        button.set_child(row)
        button.add_css_class("flat")
        button.add_css_class("kairos-upcoming-row")
        button.set_tooltip_text(
            f"{occurrence.summary}\n{formatting.format_time_range(occurrence)}"
        )
        button.connect("clicked",
                       lambda b: self.emit("event-activated", occurrence, b))
        return button

    @staticmethod
    def _time_label(occurrence: Occurrence) -> str:
        if occurrence.all_day:
            return "All day"
        now = datetime.now(tz=local_timezone())
        if occurrence.start <= now < occurrence.end:
            return f"Now · until {formatting.format_time(occurrence.end)}"
        return formatting.format_time(occurrence.start)
