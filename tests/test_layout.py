"""Presentation logic: overlap packing and date formatting.

These need GTK, because the overlap algorithm lives in the week view module.
The algorithm itself is pure, so nothing here opens a window.
"""

import unittest
from datetime import date, datetime, timedelta

import gi

gi.require_version("Gtk", "4.0")

from kairos import formatting
from kairos.config import settings
from kairos.models import Event, Occurrence, local_timezone
from kairos.ui.week_view import MAX_OVERLAP_COLUMNS, ROWS_PER_DAY, assign_columns, row_for


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
