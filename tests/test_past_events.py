"""Fading events that have already ended.

The point is to tell at a glance what is over from what is still to come.
"Over" means *ended*: a meeting in progress is the one you are looking for,
so it stays at full strength, and an all-day event lasts until midnight.
"""

import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

Adw.init()

from kairos.accounts import AccountStore  # noqa: E402
from kairos.config import DEFAULTS, settings  # noqa: E402
from kairos.models import Event, Occurrence, local_timezone, start_of_day  # noqa: E402
from kairos.storage import Storage  # noqa: E402
from kairos.sync import SyncManager  # noqa: E402
from kairos.ui.widgets import (PAST_CLASS, apply_past_state,  # noqa: E402
                               mark_event_widget, refresh_past_state)


def moment(hour, minute=0, day=date(2026, 9, 15)):
    return datetime(day.year, day.month, day.day, hour, minute,
                    tzinfo=local_timezone())


def occurrence(start, end, all_day=False):
    event = Event.new("cal", start, end, "Thing", all_day=all_day)
    return Occurrence(event=event, start=start, end=end)


def widget_for(occ):
    button = Gtk.Button()
    button.kairos_ends = occ.end
    return button


class WhatCountsAsPast(unittest.TestCase):
    def setUp(self):
        settings.set("fade_past_events", True)
        self.now = moment(12)

    def tearDown(self):
        settings.set("fade_past_events", True)

    def faded(self, occ):
        widget = widget_for(occ)
        apply_past_state(widget, self.now)
        return widget.has_css_class(PAST_CLASS)

    def test_an_event_that_has_ended_is_faded(self):
        self.assertTrue(self.faded(occurrence(moment(9), moment(10))))

    def test_one_still_to_come_is_not(self):
        self.assertFalse(self.faded(occurrence(moment(14), moment(15))))

    def test_one_in_progress_is_not(self):
        """Started is not over: this is the one you are looking for."""
        self.assertFalse(self.faded(occurrence(moment(11), moment(13))))

    def test_one_ending_exactly_now_is_over(self):
        self.assertTrue(self.faded(occurrence(moment(11), moment(12))))

    def test_an_all_day_event_today_lasts_all_day(self):
        day = self.now.date()
        start = start_of_day(day)
        self.assertFalse(self.faded(occurrence(start, start + timedelta(days=1),
                                               all_day=True)))

    def test_yesterday_s_all_day_event_is_over(self):
        start = start_of_day(self.now.date() - timedelta(days=1))
        self.assertTrue(self.faded(occurrence(start, start + timedelta(days=1),
                                              all_day=True)))

    def test_switching_the_setting_off_fades_nothing(self):
        settings.set("fade_past_events", False)
        self.assertFalse(self.faded(occurrence(moment(9), moment(10))))

    def test_it_is_on_by_default(self):
        self.assertIs(DEFAULTS["fade_past_events"], True)

    def test_a_widget_that_is_not_an_event_is_left_alone(self):
        plain = Gtk.Button()
        apply_past_state(plain, self.now)
        self.assertFalse(plain.has_css_class(PAST_CLASS))


class AsTimePasses(unittest.TestCase):
    """The window ticks once a minute; nothing is rebuilt to do it."""

    def setUp(self):
        settings.set("fade_past_events", True)

    def tearDown(self):
        settings.set("fade_past_events", True)

    def test_an_event_fades_when_it_ends(self):
        occ = occurrence(moment(9), moment(10))
        box = Gtk.Box()
        widget = widget_for(occ)
        box.append(widget)

        refresh_past_state(box, now=moment(9, 30))
        self.assertFalse(widget.has_css_class(PAST_CLASS))
        refresh_past_state(box, now=moment(10, 1))
        self.assertTrue(widget.has_css_class(PAST_CLASS))

    def test_it_reaches_widgets_nested_deep_in_a_view(self):
        outer, middle = Gtk.Box(), Gtk.Box()
        outer.append(middle)
        widget = widget_for(occurrence(moment(9), moment(10)))
        middle.append(widget)
        self.assertEqual(refresh_past_state(outer, now=moment(12)), 1)

    def test_turning_the_setting_off_unfades_what_is_on_screen(self):
        widget = widget_for(occurrence(moment(9), moment(10)))
        refresh_past_state(widget, now=moment(12))
        settings.set("fade_past_events", False)
        refresh_past_state(widget, now=moment(12))
        self.assertFalse(widget.has_css_class(PAST_CLASS))

    def test_marking_a_widget_applies_the_state_at_once(self):
        """Views call this as they build; a past event must arrive faded."""
        past = occurrence(datetime.now(tz=local_timezone()) - timedelta(hours=3),
                          datetime.now(tz=local_timezone()) - timedelta(hours=2))
        widget = Gtk.Button()
        mark_event_widget(widget, past)
        self.assertTrue(widget.has_css_class(PAST_CLASS))


class InTheViews(unittest.TestCase):
    """Every view builds its chips through the same hook."""

    def setUp(self):
        settings.set("fade_past_events", True)
        settings.set("first_day_of_week", "monday")
        self.directory = Path(tempfile.mkdtemp(prefix="kairos-past-"))
        self.sync = SyncManager(
            store=Storage(self.directory / "cache.db"),
            accounts=AccountStore(self.directory / "accounts.json"),
        )
        self.calendar = self.sync.calendars()[0]
        now = datetime.now(tz=local_timezone())
        self.today = now.date()
        # One event well over, one well to come — both inside this week.
        for summary, offset in (("Over", -26), ("To come", 26)):
            start = (now + timedelta(hours=offset)).replace(minute=0, second=0,
                                                             microsecond=0)
            self.sync.save_event(Event.new(self.calendar.id, start,
                                           start + timedelta(hours=1), summary))

    def tearDown(self):
        self.sync.storage.close()

    def states(self, root):
        """{summary: faded?} for the event widgets under ``root``."""
        found = {}
        stack = [root]
        while stack:
            widget = stack.pop()
            if hasattr(widget, "kairos_ends"):
                text = []
                inner = [widget]
                while inner:
                    w = inner.pop()
                    if isinstance(w, Gtk.Label) and w.get_label():
                        text.append(w.get_label())
                    c = w.get_first_child()
                    while c is not None:
                        inner.append(c)
                        c = c.get_next_sibling()
                for summary in ("Over", "To come"):
                    if any(summary in t for t in text):
                        found[summary] = widget.has_css_class(PAST_CLASS)
            child = widget.get_first_child()
            while child is not None:
                stack.append(child)
                child = child.get_next_sibling()
        return found

    def check(self, view_class, **kwargs):
        view = view_class(self.sync, **kwargs)
        view.set_date(self.today)
        found = self.states(view)
        for summary in ("Over", "To come"):
            if summary in found:            # a view may not show both days
                self.assertEqual(found[summary], summary == "Over",
                                 f"{view_class.__name__}: {summary!r}")
        return found

    def test_the_week_view(self):
        from kairos.ui.week_view import WeekView
        self.assertTrue(self.check(WeekView))

    def test_the_month_view(self):
        from kairos.ui.month_view import MonthView
        self.assertTrue(self.check(MonthView))

    def test_search_results(self):
        from kairos.ui.search_view import SearchView
        view = SearchView(self.sync)
        view.search("over")
        found = self.states(view)
        self.assertEqual(found.get("Over"), True,
                         "a past search result was not faded")


class TheClock(unittest.TestCase):
    def test_a_tick_keeps_ticking(self):
        from kairos.ui.window import CalendarWindow

        class Stub:
            current_view = Gtk.Box()
            _sidebar = Gtk.Box()
            _search_view = Gtk.Box()

        self.assertEqual(CalendarWindow._tick(Stub()), GLib.SOURCE_CONTINUE)

    def test_the_setting_redraws_the_views(self):
        from kairos.ui.window import CalendarWindow
        redrawn = []

        class Stub:
            _sidebar = type("S", (), {"refresh": lambda self: None})()
            refresh = lambda self: redrawn.append(True)  # noqa: E731

        CalendarWindow._on_settings_changed(Stub(), "fade_past_events")
        self.assertEqual(redrawn, [True])


if __name__ == "__main__":
    unittest.main()
