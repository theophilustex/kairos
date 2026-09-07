"""Presentation logic: overlap packing, banner spans, and date formatting.

These need GTK, because the layout algorithms live in the week view module.
Most of it is pure arithmetic; the one class that builds a real ``WeekView``
still never opens a window, it only inspects where things were attached.
"""

import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402

Adw.init()

from kairos import formatting  # noqa: E402
from kairos.accounts import AccountStore  # noqa: E402
from kairos.config import settings  # noqa: E402
from kairos.models import Event, Occurrence, local_timezone, start_of_day  # noqa: E402
from kairos.storage import Storage  # noqa: E402
from kairos.sync import SyncManager  # noqa: E402
from kairos.ui.week_view import (MAX_OVERLAP_COLUMNS, ROWS_PER_DAY,  # noqa: E402
                                 WeekView, assign_banner_rows, assign_columns,
                                 row_for)


def at(hour, minute=0):
    return datetime(2026, 9, 5, hour, minute, tzinfo=local_timezone())


def occurrence(summary, start_hour, end_hour):
    start, end = at(start_hour), at(end_hour)
    return Occurrence(
        event=Event.new("cal", start, end, summary), start=start, end=end
    )


class OverlapPacking(unittest.TestCase):
    def columns(self, *pairs):
        items = [occurrence(f"e{i}", a, b) for i, (a, b) in enumerate(pairs)]
        return {o.summary: (col, width) for o, col, width in assign_columns(items)}

    def test_a_lone_event_takes_the_full_width(self):
        self.assertEqual(self.columns((9, 10)), {"e0": (0, 1)})

    def test_two_overlapping_events_sit_side_by_side(self):
        placed = self.columns((9, 11), (10, 12))
        self.assertEqual(placed["e0"], (0, 2))
        self.assertEqual(placed["e1"], (1, 2))

    def test_events_that_only_touch_do_not_overlap(self):
        placed = self.columns((9, 10), (10, 11))
        self.assertEqual({v[1] for v in placed.values()}, {1})

    def test_three_way_overlap(self):
        placed = self.columns((9, 12), (10, 12), (11, 12))
        self.assertEqual({v[1] for v in placed.values()}, {3})
        self.assertEqual(sorted(v[0] for v in placed.values()), [0, 1, 2])

    def test_a_freed_column_is_reused(self):
        # e0 finishes before e2 starts, so e2 can go back in column 0.
        placed = self.columns((9, 10), (9, 13), (11, 12))
        self.assertEqual(placed["e2"][0], 0)

    def test_separate_clusters_are_sized_independently(self):
        placed = self.columns((9, 11), (10, 12), (15, 16))
        self.assertEqual(placed["e2"], (0, 1))

    def test_width_is_capped(self):
        placed = self.columns(*[(9, 17)] * (MAX_OVERLAP_COLUMNS + 3))
        for column, width in placed.values():
            self.assertLessEqual(width, MAX_OVERLAP_COLUMNS)
            self.assertLess(column, MAX_OVERLAP_COLUMNS)

    def test_every_event_is_placed_exactly_once(self):
        items = [occurrence(f"e{i}", 9 + i % 4, 11 + i % 4) for i in range(20)]
        placed = assign_columns(items)
        self.assertEqual(len(placed), 20)

    def test_empty_input(self):
        self.assertEqual(assign_columns([]), [])


class BannerPacking(unittest.TestCase):
    """All-day bars in the strip above the week grid.

    The bug these pin shut: a multi-day event used to be drawn as one chip
    per day in seven independently-sized columns, so a three-day trip became
    three chips that could sit on different rows and, because a column grew
    to fit its contents, no longer lined up with the day below it.
    """

    def rows(self, *spans):
        """``(first_column, last_column)`` in, ``{name: (first, last, row)}`` out."""
        banners = [(occurrence(f"b{i}", 9, 10), a, b)
                   for i, (a, b) in enumerate(spans)]
        return {o.summary: (first, last, row)
                for o, first, last, row in assign_banner_rows(banners)}

    def test_a_lone_banner_goes_on_the_top_row(self):
        self.assertEqual(self.rows((2, 4)), {"b0": (2, 4, 0)})

    def test_a_banner_keeps_one_row_across_its_whole_span(self):
        """It must not step down mid-week; that is what looked like bleeding."""
        placed = assign_banner_rows([(occurrence("trip", 9, 10), 2, 4)])
        _, first, last, row = placed[0]
        self.assertEqual((first, last, row), (2, 4, 0))

    def test_overlapping_banners_take_different_rows(self):
        placed = self.rows((2, 4), (2, 2))
        self.assertNotEqual(placed["b0"][2], placed["b1"][2])

    def test_the_wider_banner_takes_the_top_row(self):
        placed = self.rows((3, 3), (0, 6))
        self.assertEqual(placed["b1"][2], 0)
        self.assertEqual(placed["b0"][2], 1)

    def test_banners_that_do_not_touch_share_a_row(self):
        placed = self.rows((0, 1), (3, 4), (6, 6))
        self.assertEqual({p[2] for p in placed.values()}, {0})

    def test_banners_that_only_abut_share_a_row(self):
        """Monday–Tuesday and Wednesday–Friday do not overlap."""
        placed = self.rows((0, 1), (2, 4))
        self.assertEqual(placed["b0"][2], placed["b1"][2])

    def test_a_third_overlapping_banner_takes_a_third_row(self):
        placed = self.rows((0, 6), (0, 6), (0, 6))
        self.assertEqual({p[2] for p in placed.values()}, {0, 1, 2})

    def test_every_banner_is_placed_exactly_once(self):
        placed = assign_banner_rows(
            [(occurrence(f"b{i}", 9, 10), i % 7, min(6, i % 7 + 2)) for i in range(20)])
        self.assertEqual(len(placed), 20)

    def test_empty_input(self):
        self.assertEqual(assign_banner_rows([]), [])


class BannerStrip(unittest.TestCase):
    """The all-day strip as the widget actually builds it.

    Checks the thing a user sees: one bar per event, attached across exactly
    the days it covers and no others.
    """

    def setUp(self):
        settings.set("first_day_of_week", "monday")
        self.directory = Path(tempfile.mkdtemp(prefix="kairos-banners-"))
        self.sync = SyncManager(
            store=Storage(self.directory / "cache.db"),
            accounts=AccountStore(self.directory / "accounts.json"),
        )
        self.calendar = self.sync.calendars()[0]
        self.monday = date(2026, 8, 31)

    def tearDown(self):
        self.sync.storage.close()

    def all_day(self, summary, day_offset, days=1):
        start = start_of_day(self.monday + timedelta(days=day_offset))
        self.sync.save_event(Event.new(
            self.calendar.id, start, start + timedelta(days=days),
            summary, all_day=True))

    def bars(self):
        """``{summary: (first_column, span)}`` for what the strip attached."""
        view = WeekView(self.sync)
        view.set_date(self.monday)
        view.refresh()
        found = {}
        child = view._all_day_days.get_first_child()
        while child is not None:
            if isinstance(child, Gtk.Button):
                column, _row, width, _height = view._all_day_days.query_child(child)
                found[child.get_child().get_label()] = (column, width)
            child = child.get_next_sibling()
        return found

    def test_a_one_day_event_occupies_one_column(self):
        self.all_day("Birthday", 6)
        self.assertEqual(self.bars(), {"Birthday": (6, 1)})

    def test_a_multi_day_event_is_one_bar_spanning_its_days(self):
        """Not three separate chips, which is what made this confusing."""
        self.all_day("Trip to Lisbon", 2, days=3)
        self.assertEqual(self.bars(), {"Trip to Lisbon": (2, 3)})

    def test_a_bar_does_not_reach_into_the_next_day(self):
        self.all_day("Quarter close", 0, days=2)
        column, width = self.bars()["Quarter close"]
        self.assertEqual(column + width, 2, "the bar ran past Tuesday")

    def test_an_event_starting_before_the_week_is_clipped(self):
        self.all_day("Long haul", -3, days=6)
        self.assertEqual(self.bars(), {"Long haul": (0, 3)})

    def test_an_event_running_past_the_week_is_clipped(self):
        self.all_day("Long haul", 5, days=9)
        self.assertEqual(self.bars(), {"Long haul": (5, 2)})

    def test_an_event_spanning_the_whole_week_fills_it(self):
        self.all_day("Sabbatical", -2, days=30)
        self.assertEqual(self.bars(), {"Sabbatical": (0, 7)})

    def test_each_event_is_attached_once(self):
        self.all_day("Trip", 2, days=3)
        view = WeekView(self.sync)
        view.set_date(self.monday)
        view.refresh()
        buttons = 0
        child = view._all_day_days.get_first_child()
        while child is not None:
            buttons += isinstance(child, Gtk.Button)
            child = child.get_next_sibling()
        self.assertEqual(buttons, 1)

    def test_the_strip_hides_itself_when_there_is_nothing_to_show(self):
        view = WeekView(self.sync)
        view.set_date(self.monday)
        view.refresh()
        self.assertFalse(view._all_day_strip.get_visible())

    def test_a_repeated_refresh_does_not_accumulate_bars(self):
        self.all_day("Trip", 2, days=3)
        view = WeekView(self.sync)
        view.set_date(self.monday)
        for _ in range(3):
            view.refresh()
        buttons = 0
        child = view._all_day_days.get_first_child()
        while child is not None:
            buttons += isinstance(child, Gtk.Button)
            child = child.get_next_sibling()
        self.assertEqual(buttons, 1)

    def test_clicking_a_bar_opens_that_event(self):
        """One bar now stands for several days, so it must still identify itself."""
        self.all_day("Trip to Lisbon", 2, days=3)
        view = WeekView(self.sync)
        view.set_date(self.monday)
        view.refresh()
        seen = []
        view.connect("event-activated", lambda _v, o, _w: seen.append(o.summary))

        child = view._all_day_days.get_first_child()
        while not isinstance(child, Gtk.Button):
            child = child.get_next_sibling()
        child.emit("clicked")
        self.assertEqual(seen, ["Trip to Lisbon"])

    def test_every_day_keeps_a_column_even_when_empty(self):
        """Otherwise the days with no banner would collapse to no width."""
        self.all_day("Birthday", 6)
        view = WeekView(self.sync)
        view.set_date(self.monday)
        view.refresh()
        for offset in range(7):
            self.assertIsNotNone(view._all_day_days.get_child_at(offset, 0),
                                 f"day {offset} has no column placeholder")


class RowMath(unittest.TestCase):
    def test_midnight_is_the_first_row(self):
        self.assertEqual(row_for(at(0, 0), date(2026, 9, 5)), 0)

    def test_quarter_hours_map_to_rows(self):
        self.assertEqual(row_for(at(1, 0), date(2026, 9, 5)), 4)
        self.assertEqual(row_for(at(1, 15), date(2026, 9, 5)), 5)
        self.assertEqual(row_for(at(1, 30), date(2026, 9, 5)), 6)

    def test_an_earlier_day_clamps_to_the_top(self):
        self.assertEqual(row_for(at(10), date(2026, 9, 6)), 0)

    def test_a_later_day_clamps_to_the_bottom(self):
        self.assertEqual(row_for(at(10), date(2026, 9, 4)), ROWS_PER_DAY)

    def test_no_row_is_ever_out_of_range(self):
        for hour in range(24):
            for minute in (0, 14, 15, 59):
                row = row_for(at(hour, minute), date(2026, 9, 5))
                self.assertTrue(0 <= row <= ROWS_PER_DAY)


class WeekArithmetic(unittest.TestCase):
    def tearDown(self):
        settings.set("first_day_of_week", "monday")
        settings.set("time_format", "24h")

    def test_week_starts_on_monday(self):
        settings.set("first_day_of_week", "monday")
        self.assertEqual(formatting.week_start(date(2026, 9, 5)), date(2026, 8, 31))

    def test_week_starts_on_sunday(self):
        settings.set("first_day_of_week", "sunday")
        self.assertEqual(formatting.week_start(date(2026, 9, 5)), date(2026, 8, 30))

    def test_week_starts_on_saturday(self):
        settings.set("first_day_of_week", "saturday")
        self.assertEqual(formatting.week_start(date(2026, 9, 5)), date(2026, 9, 5))

    def test_headings_follow_the_first_day(self):
        settings.set("first_day_of_week", "sunday")
        self.assertEqual(formatting.weekday_headings()[0], "Sun")
        settings.set("first_day_of_week", "monday")
        self.assertEqual(formatting.weekday_headings()[0], "Mon")

    def test_month_grid_starts_on_a_week_boundary(self):
        settings.set("first_day_of_week", "monday")
        start = formatting.month_grid_start(date(2026, 9, 15))
        self.assertEqual(start.weekday(), 0)
        self.assertLessEqual(start, date(2026, 9, 1))
        self.assertGreater(start, date(2026, 8, 24))


class TimeFormatting(unittest.TestCase):
    def tearDown(self):
        settings.set("time_format", "24h")

    def test_24_hour(self):
        settings.set("time_format", "24h")
        self.assertEqual(formatting.format_time(at(14, 30)), "14:30")
        self.assertEqual(formatting.format_hour_label(0), "00:00")

    def test_12_hour(self):
        settings.set("time_format", "12h")
        self.assertEqual(formatting.format_time(at(14, 30)), "2:30 PM")
        self.assertEqual(formatting.format_time(at(9, 5)), "9:05 AM")

    def test_12_hour_edges(self):
        settings.set("time_format", "12h")
        self.assertEqual(formatting.format_hour_label(0), "12 AM")
        self.assertEqual(formatting.format_hour_label(12), "12 PM")
        self.assertEqual(formatting.format_hour_label(23), "11 PM")

    def test_round_hours_can_drop_their_minutes(self):
        settings.set("time_format", "12h")
        self.assertEqual(formatting.format_time(at(14, 0), drop_zero_minutes=True), "2 PM")


class Headings(unittest.TestCase):
    def test_a_single_day_range(self):
        text = formatting.format_range_heading(date(2026, 3, 5), date(2026, 3, 5))
        self.assertIn("March", text)

    def test_a_range_crossing_a_month(self):
        text = formatting.format_range_heading(date(2026, 8, 31), date(2026, 9, 6))
        self.assertIn("Aug", text)
        self.assertIn("Sep", text)

    def test_a_range_crossing_a_year_keeps_both_years(self):
        text = formatting.format_range_heading(date(2026, 12, 28), date(2027, 1, 3))
        self.assertIn("2026", text)
        self.assertIn("2027", text)

    def test_today_and_tomorrow_are_named(self):
        today = date.today()
        self.assertTrue(formatting.format_day_heading(today).startswith("Today"))
        self.assertTrue(
            formatting.format_day_heading(today + timedelta(days=1)).startswith("Tomorrow")
        )


if __name__ == "__main__":
    unittest.main()
