"""Moving around the week and day grids with the keyboard.

The month grid has had this for a while; the timed grid had none, so the
views you are most likely to plan a day in could not be reached without a
mouse. A cursor moves over the grid — the same outline a click draws — and
Enter does what a second click does: open the event there, or create one.
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

from kairos.accounts import AccountStore  # noqa: E402
from kairos.config import settings  # noqa: E402
from kairos.models import Event, start_of_day  # noqa: E402
from kairos.storage import Storage  # noqa: E402
from kairos.sync import SyncManager  # noqa: E402
from kairos.ui.week_view import (MINUTES_PER_DAY, SLOT_SNAP_MINUTES,  # noqa: E402
                                 DayView, WeekView, scroll_to_reveal)

SHIFT = Gdk.ModifierType.SHIFT_MASK
CTRL = Gdk.ModifierType.CONTROL_MASK


class WithAWeek(unittest.TestCase):
    #: A Tuesday, well away from today so the default cursor is predictable.
    DAY = date(2026, 9, 15)

    def setUp(self):
        settings.set("first_day_of_week", "monday")
        settings.set("week_view_start_hour", 9)
        settings.set("default_event_duration_minutes", 60)
        self.directory = Path(tempfile.mkdtemp(prefix="kairos-weekkeys-"))
        self.sync = SyncManager(
            store=Storage(self.directory / "cache.db"),
            accounts=AccountStore(self.directory / "accounts.json"),
        )
        self.calendar = self.sync.calendars()[0]
        self.view = self.make_view()

    def tearDown(self):
        settings.set("week_view_start_hour", 7)
        self.sync.storage.close()

    def make_view(self, cls=WeekView):
        view = cls(self.sync)
        view.set_date(self.DAY)
        return view

    def press(self, keyval, state=0, view=None):
        return (view or self.view)._on_key_pressed(None, keyval, 0, state)

    def cursor(self, view=None):
        return (view or self.view)._slot

    def add(self, hour, length=1, summary="Standup", day=None):
        start = start_of_day(day or self.DAY) + timedelta(hours=hour)
        event = Event.new(self.calendar.id, start, start + timedelta(hours=length),
                          summary)
        self.sync.save_event(event)
        self.view.refresh()
        return event


class Reaching(WithAWeek):
    def test_the_grid_can_take_focus(self):
        self.assertTrue(self.view.get_focusable())

    def test_arriving_by_keyboard_shows_the_cursor(self):
        self.view.is_focus = lambda: True
        self.view._on_focus_enter()
        self.assertEqual(self.cursor(), (self.DAY, 9 * 60))
        self.assertIsNotNone(self.view._slot_widget)

    def test_the_controller_is_believed_over_the_window(self):
        """The real ordering: GTK says "focused" before the window records it."""
        class Controller:
            class props:
                is_focus = True
        self.view.is_focus = lambda: False          # the window has not caught up
        self.view._on_focus_enter(Controller())
        self.assertEqual(self.cursor(), (self.DAY, 9 * 60))

    def test_arriving_on_an_event_inside_the_grid_does_not(self):
        """Tab onto an event block: that is focus on the event, not the grid."""
        self.view.is_focus = lambda: False
        self.view._on_focus_enter()
        self.assertIsNone(self.cursor())

    def test_arriving_again_keeps_the_cursor_where_it_was(self):
        self.view.place_cursor(self.DAY, 14 * 60)
        self.view.is_focus = lambda: True
        self.view._on_focus_enter()
        self.assertEqual(self.cursor(), (self.DAY, 14 * 60))

    def test_the_first_key_starts_at_a_sensible_time(self):
        self.press(Gdk.KEY_Down)
        self.assertEqual(self.cursor(), (self.DAY, 9 * 60 + SLOT_SNAP_MINUTES))


class Moving(WithAWeek):
    def setUp(self):
        super().setUp()
        self.view.place_cursor(self.DAY, 10 * 60)

    def test_down_is_a_quarter_hour_later(self):
        self.press(Gdk.KEY_Down)
        self.assertEqual(self.cursor(), (self.DAY, 10 * 60 + 15))

    def test_up_is_a_quarter_hour_earlier(self):
        self.press(Gdk.KEY_Up)
        self.assertEqual(self.cursor(), (self.DAY, 10 * 60 - 15))

    def test_shift_moves_an_hour(self):
        self.press(Gdk.KEY_Down, SHIFT)
        self.assertEqual(self.cursor(), (self.DAY, 11 * 60))
        self.press(Gdk.KEY_Up, SHIFT)
        self.press(Gdk.KEY_Up, SHIFT)
        self.assertEqual(self.cursor(), (self.DAY, 9 * 60))

    def test_right_is_the_next_day_at_the_same_time(self):
        self.press(Gdk.KEY_Right)
        self.assertEqual(self.cursor(), (self.DAY + timedelta(days=1), 10 * 60))

    def test_left_is_the_day_before(self):
        self.press(Gdk.KEY_Left)
        self.assertEqual(self.cursor(), (self.DAY - timedelta(days=1), 10 * 60))

    def test_it_stops_at_the_top_of_the_day(self):
        self.view.place_cursor(self.DAY, 0)
        self.press(Gdk.KEY_Up)
        self.assertEqual(self.cursor(), (self.DAY, 0))

    def test_it_stops_at_the_bottom_of_the_day(self):
        last = MINUTES_PER_DAY - SLOT_SNAP_MINUTES
        self.view.place_cursor(self.DAY, last)
        self.press(Gdk.KEY_Down)
        self.assertEqual(self.cursor(), (self.DAY, last))

    def test_home_and_end_are_the_ends_of_the_week(self):
        self.press(Gdk.KEY_Home)
        self.assertEqual(self.cursor(), (date(2026, 9, 14), 10 * 60))
        self.press(Gdk.KEY_End)
        self.assertEqual(self.cursor(), (date(2026, 9, 20), 10 * 60))

    def test_the_arrows_are_handled_not_passed_on(self):
        """Otherwise the scrolled window takes them to mean "scroll"."""
        for key in (Gdk.KEY_Up, Gdk.KEY_Down, Gdk.KEY_Left, Gdk.KEY_Right):
            self.assertTrue(self.press(key))

    def test_ctrl_and_alt_are_left_to_the_window(self):
        self.assertFalse(self.press(Gdk.KEY_Right, CTRL))
        self.assertFalse(self.press(Gdk.KEY_Page_Down, CTRL))
        self.assertEqual(self.cursor(), (self.DAY, 10 * 60))

    def test_an_unrelated_key_is_passed_on(self):
        self.assertFalse(self.press(Gdk.KEY_a))


class Paging(WithAWeek):
    def test_walking_off_the_week_brings_the_next_one_in(self):
        self.view.place_cursor(date(2026, 9, 20), 10 * 60)      # Sunday
        self.press(Gdk.KEY_Right)
        self.assertEqual(self.cursor(), (date(2026, 9, 21), 10 * 60))
        self.assertEqual(self.view._first_day, date(2026, 9, 21))

    def test_page_down_is_the_same_slot_next_week(self):
        self.view.place_cursor(self.DAY, 10 * 60)
        self.press(Gdk.KEY_Page_Down)
        self.assertEqual(self.cursor(), (self.DAY + timedelta(days=7), 10 * 60))

    def test_page_up_is_the_same_slot_last_week(self):
        self.view.place_cursor(self.DAY, 10 * 60)
        self.press(Gdk.KEY_Page_Up)
        self.assertEqual(self.cursor(), (self.DAY - timedelta(days=7), 10 * 60))

    def test_the_window_is_told_the_day_moved(self):
        seen = []
        self.view.connect("date-selected", lambda _v, day: seen.append(day))
        self.view.place_cursor(self.DAY, 10 * 60)
        self.press(Gdk.KEY_Right)
        self.assertEqual(seen[-1], self.DAY + timedelta(days=1))

    def test_a_cursor_left_in_another_week_does_not_drag_the_view_back(self):
        """Navigate with the header, then press an arrow: stay in the new week."""
        self.view.place_cursor(self.DAY, 10 * 60)
        self.view.go_next()
        self.press(Gdk.KEY_Down)
        self.assertEqual(self.view._first_day, date(2026, 9, 21))
        self.assertEqual(self.cursor()[0], date(2026, 9, 22))

    def test_the_day_view_moves_a_day_at_a_time(self):
        day_view = self.make_view(DayView)
        day_view.place_cursor(self.DAY, 10 * 60)
        self.press(Gdk.KEY_Right, view=day_view)
        self.assertEqual(self.cursor(day_view), (self.DAY + timedelta(days=1), 10 * 60))
        self.press(Gdk.KEY_Page_Down, view=day_view)
        self.assertEqual(self.cursor(day_view)[0], self.DAY + timedelta(days=2))


class Acting(WithAWeek):
    def test_enter_on_empty_space_creates_there(self):
        created = []
        self.view.connect("create-requested", lambda _v, when: created.append(when))
        self.view.place_cursor(self.DAY, 14 * 60 + 30)
        self.press(Gdk.KEY_Return)
        self.assertEqual(created, [start_of_day(self.DAY) + timedelta(hours=14, minutes=30)])
        self.assertIsNone(self.view._slot, "the outline outlived its use")

    def test_enter_on_an_event_opens_it(self):
        self.add(10, summary="Standup")
        opened = []
        self.view.connect("event-activated", lambda _v, occ, _w: opened.append(occ.summary))
        created = []
        self.view.connect("create-requested", lambda _v, when: created.append(when))
        self.view.place_cursor(self.DAY, 10 * 60 + 30)     # inside the event
        self.press(Gdk.KEY_Return)
        self.assertEqual(opened, ["Standup"])
        self.assertEqual(created, [])

    def test_space_does_what_enter_does(self):
        created = []
        self.view.connect("create-requested", lambda _v, when: created.append(when))
        self.view.place_cursor(self.DAY, 16 * 60)
        self.press(Gdk.KEY_space)
        self.assertEqual(len(created), 1)

    def test_enter_on_a_focused_button_is_left_to_the_button(self):
        """Tab can reach an event or a day heading; Enter should press *that*."""
        self.view._focus_is_inside_an_event = lambda: True
        self.assertFalse(self.press(Gdk.KEY_Return))

    def test_escape_lets_go_of_the_cursor(self):
        self.view.place_cursor(self.DAY, 10 * 60)
        self.assertTrue(self.press(Gdk.KEY_Escape))
        self.assertIsNone(self.view._slot)

    def test_escape_with_nothing_picked_is_passed_on(self):
        """Escape also closes search and dialogs; do not swallow it for nothing."""
        self.assertFalse(self.press(Gdk.KEY_Escape))

    def test_a_click_then_the_arrows_carry_on_from_the_click(self):
        grid = self.view._day_grids[(self.DAY - self.view._first_day).days]
        grid.pick = lambda x, y, flags: grid                # empty space
        self.view._on_column_click(grid, 1, 5, 10 * 4 * self.view._row_height())
        self.press(Gdk.KEY_Down)
        self.assertEqual(self.cursor(), (self.DAY, 10 * 60 + 15))


class Announcing(WithAWeek):
    """A screen reader hears where the cursor is, and what is there."""

    def test_it_says_the_day_and_the_time(self):
        self.view.place_cursor(self.DAY, 10 * 60)
        said = self.view._last_announcement
        self.assertIn("15", said)
        self.assertIn("10", said)

    def test_it_names_the_event_under_the_cursor(self):
        self.add(10, summary="Standup")
        self.view.place_cursor(self.DAY, 10 * 60 + 15)
        self.assertIn("Standup", self.view._last_announcement)

    def test_it_is_sent_as_an_announcement_when_gtk_can(self):
        said = []
        self.view.announce = lambda text, priority: said.append(text)
        self.view.place_cursor(self.DAY, 10 * 60)
        self.assertEqual(said, [self.view._last_announcement])


class Scrolling(unittest.TestCase):
    """How far to scroll to keep the cursor in sight."""

    def test_already_visible_does_not_move(self):
        self.assertEqual(scroll_to_reveal(100, 400, 2000, 200, 250, 20), 100)

    def test_above_the_top_scrolls_up_with_a_margin(self):
        self.assertEqual(scroll_to_reveal(500, 400, 2000, 300, 350, 20), 280)

    def test_below_the_bottom_scrolls_down_with_a_margin(self):
        self.assertEqual(scroll_to_reveal(0, 400, 2000, 500, 550, 20), 170)

    def test_it_never_scrolls_past_either_end(self):
        self.assertEqual(scroll_to_reveal(500, 400, 2000, 5, 50, 20), 0)
        self.assertEqual(scroll_to_reveal(0, 400, 2000, 1950, 2000, 20), 1600)

    def test_an_unlaid_out_scroller_is_left_alone(self):
        self.assertEqual(scroll_to_reveal(0, 0, 0, 500, 550, 20), 0)


if __name__ == "__main__":
    unittest.main()
