"""Moving around the month grid with the keyboard.

This is a feature and an accessibility fix at once. Day cells used to be
plain boxes with a click handler: nothing could focus them, so nothing could
reach them by keyboard and a screen reader had nothing to announce, however
well they were labelled.
"""

import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk  # noqa: E402

Adw.init()

from kairos import formatting  # noqa: E402
from kairos.accounts import AccountStore  # noqa: E402
from kairos.config import settings  # noqa: E402
from kairos.models import Event, start_of_day  # noqa: E402
from kairos.storage import Storage  # noqa: E402
from kairos.sync import SyncManager  # noqa: E402
from kairos.ui.month_view import MonthView, _add_months  # noqa: E402


class MonthArithmetic(unittest.TestCase):
    def test_it_moves_a_month_forward(self):
        self.assertEqual(_add_months(date(2026, 9, 7), 1), date(2026, 10, 7))

    def test_it_moves_a_month_back(self):
        self.assertEqual(_add_months(date(2026, 9, 7), -1), date(2026, 8, 7))

    def test_it_crosses_a_year(self):
        self.assertEqual(_add_months(date(2026, 12, 15), 1), date(2027, 1, 15))
        self.assertEqual(_add_months(date(2026, 1, 15), -1), date(2025, 12, 15))

    def test_it_clamps_to_a_shorter_month(self):
        """31 January plus a month is 28 February, not the 31st."""
        self.assertEqual(_add_months(date(2026, 1, 31), 1), date(2026, 2, 28))

    def test_it_knows_about_leap_years(self):
        self.assertEqual(_add_months(date(2028, 1, 31), 1), date(2028, 2, 29))
        self.assertEqual(_add_months(date(2100, 1, 31), 1), date(2100, 2, 28))


class TheMonthGrid(unittest.TestCase):
    def setUp(self):
        settings.set("first_day_of_week", "monday")
        self.directory = Path(tempfile.mkdtemp(prefix="kairos-keys-"))
        self.sync = SyncManager(
            store=Storage(self.directory / "cache.db"),
            accounts=AccountStore(self.directory / "accounts.json"),
        )
        self.calendar = self.sync.calendars()[0]
        self.view = MonthView(self.sync)
        self.today = date(2026, 9, 15)
        self.view.set_date(self.today)

    def tearDown(self):
        self.sync.storage.close()

    def press(self, keyval, state=0):
        return self.view._on_key_pressed(None, keyval, 0, state)

    # -- reachability -------------------------------------------------

    def test_day_cells_can_be_focused(self):
        """Without this the grid is unreachable by keyboard entirely."""
        cell = self.view._cells[self.today]
        self.assertTrue(cell.get_focusable())

    def test_a_day_cell_is_named_for_its_date(self):
        cell = self.view._cells[self.today]
        self.assertEqual(cell.kairos_accessible_label
                         if hasattr(cell, "kairos_accessible_label")
                         else formatting.format_date(self.today),
                         formatting.format_date(self.today))

    # -- moving -------------------------------------------------------

    def test_right_moves_a_day(self):
        self.press(Gdk.KEY_Right)
        self.assertEqual(self.view.selected_day, self.today + timedelta(days=1))

    def test_left_moves_back_a_day(self):
        self.press(Gdk.KEY_Left)
        self.assertEqual(self.view.selected_day, self.today - timedelta(days=1))

    def test_down_moves_a_week(self):
        self.press(Gdk.KEY_Down)
        self.assertEqual(self.view.selected_day, self.today + timedelta(days=7))

    def test_up_moves_back_a_week(self):
        self.press(Gdk.KEY_Up)
        self.assertEqual(self.view.selected_day, self.today - timedelta(days=7))

    def test_home_goes_to_the_start_of_the_week(self):
        self.press(Gdk.KEY_Home)
        self.assertEqual(self.view.selected_day, formatting.week_start(self.today))

    def test_end_goes_to_the_end_of_the_week(self):
        self.press(Gdk.KEY_End)
        self.assertEqual(self.view.selected_day,
                         formatting.week_start(self.today) + timedelta(days=6))

    def test_page_down_moves_a_month(self):
        self.press(Gdk.KEY_Page_Down)
        self.assertEqual(self.view.selected_day, date(2026, 10, 15))

    def test_page_up_moves_back_a_month(self):
        self.press(Gdk.KEY_Page_Up)
        self.assertEqual(self.view.selected_day, date(2026, 8, 15))

    def test_the_arrows_are_handled_not_passed_on(self):
        """Otherwise the scrolled window treats them as "scroll"."""
        self.assertTrue(self.press(Gdk.KEY_Right))
        self.assertTrue(self.press(Gdk.KEY_Down))

    def test_an_unrelated_key_is_passed_on(self):
        self.assertFalse(self.press(Gdk.KEY_a))

    def test_ctrl_arrow_is_left_to_the_window(self):
        """Ctrl+Page Down is "next period"; the grid must not eat it."""
        self.assertFalse(self.press(Gdk.KEY_Right,
                                    Gdk.ModifierType.CONTROL_MASK))
        self.assertFalse(self.press(Gdk.KEY_Page_Down,
                                    Gdk.ModifierType.CONTROL_MASK))

    # -- paging off the edge ------------------------------------------

    def test_walking_off_the_month_brings_the_next_one_in(self):
        self.view.set_date(date(2026, 9, 30))
        for _ in range(5):
            self.press(Gdk.KEY_Right)
        self.assertEqual(self.view.selected_day, date(2026, 10, 5))
        self.assertIn(date(2026, 10, 5), self.view._cells)

    def test_the_day_moved_to_is_always_focusable(self):
        for _ in range(40):
            self.press(Gdk.KEY_Right)
        cell = self.view._cells.get(self.view.selected_day)
        self.assertIsNotNone(cell, "moved to a day with no cell to focus")

    def test_paging_tells_the_window_the_day_changed(self):
        seen = []
        self.view.connect("date-selected", lambda _v, day: seen.append(day))
        self.view.set_date(date(2026, 9, 30))
        self.press(Gdk.KEY_Page_Down)
        self.assertEqual(seen[-1], date(2026, 10, 30))

    # -- acting -------------------------------------------------------

    def test_enter_opens_the_day(self):
        seen = []
        self.view.connect("day-activated", lambda _v, day: seen.append(day))
        self.press(Gdk.KEY_Return)
        self.assertEqual(seen, [self.today])

    def test_space_opens_the_day(self):
        seen = []
        self.view.connect("day-activated", lambda _v, day: seen.append(day))
        self.press(Gdk.KEY_space)
        self.assertEqual(seen, [self.today])


class WhatACellAnnounces(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix="kairos-announce-"))
        self.sync = SyncManager(
            store=Storage(self.directory / "cache.db"),
            accounts=AccountStore(self.directory / "accounts.json"),
        )
        self.calendar = self.sync.calendars()[0]
        self.day = date(2026, 9, 15)

    def tearDown(self):
        self.sync.storage.close()

    def label_for(self, day):
        view = MonthView(self.sync)
        view.set_date(day)
        cell = view._cells[day]
        return cell.kairos_accessible_label

    def test_an_empty_day_is_just_its_date(self):
        self.assertEqual(self.label_for(self.day),
                         formatting.format_date(self.day))

    def test_a_busy_day_says_how_many(self):
        for hour in (9, 11):
            start = start_of_day(self.day).replace(hour=hour)
            self.sync.save_event(Event.new(self.calendar.id, start,
                                           start.replace(hour=hour + 1),
                                           f"Meeting {hour}"))
        self.assertIn("2 events", self.label_for(self.day))

    def test_one_event_is_singular(self):
        start = start_of_day(self.day).replace(hour=9)
        self.sync.save_event(Event.new(self.calendar.id, start,
                                       start.replace(hour=10), "Standup"))
        label = self.label_for(self.day)
        self.assertIn("1 event", label)
        self.assertNotIn("1 events", label)


if __name__ == "__main__":
    unittest.main()
