"""Dragging a block in the week view to move or resize an event.

The arithmetic lives in `dragged_times`, away from the gesture handler, so
the interesting cases can be tested without a pointer: dragging past
midnight, resizing an event to nothing, and dropping something on another
day.
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

from kairos.accounts import AccountStore  # noqa: E402
from kairos.models import Event, Occurrence, local_timezone  # noqa: E402
from kairos.storage import Storage  # noqa: E402
from kairos.sync import SyncManager  # noqa: E402
from kairos.ui.week_view import (DRAG_SNAP_MINUTES, MIN_EVENT_MINUTES,  # noqa: E402
                                 MINUTES_PER_DAY, WeekView, dragged_times)


def at(hour, minute=0):
    return datetime(2026, 9, 15, hour, minute, tzinfo=local_timezone())


def occurrence(start_hour, end_hour):
    start, end = at(start_hour), at(end_hour)
    event = Event.new("cal", start, end, "Standup")
    return Occurrence(event=event, start=start, end=end)


class Moving(unittest.TestCase):
    def test_dragging_down_moves_the_event_later(self):
        start, end = dragged_times(occurrence(9, 10), minutes=60, days=0,
                                   resizing=False)
        self.assertEqual((start.hour, end.hour), (10, 11))

    def test_dragging_up_moves_it_earlier(self):
        start, end = dragged_times(occurrence(9, 10), minutes=-60, days=0,
                                   resizing=False)
        self.assertEqual((start.hour, end.hour), (8, 9))

    def test_it_snaps_to_the_row(self):
        """A row is fifteen minutes; nothing lands between them."""
        start, _ = dragged_times(occurrence(9, 10), minutes=15, days=0,
                                 resizing=False)
        self.assertEqual(start.minute % DRAG_SNAP_MINUTES, 0)
        self.assertEqual(start.minute, 15)

    def test_the_length_is_unchanged(self):
        original = occurrence(9, 11)
        start, end = dragged_times(original, minutes=105, days=0, resizing=False)
        self.assertEqual(end - start, original.end - original.start)

    def test_it_cannot_be_dragged_off_the_top_of_the_day(self):
        start, end = dragged_times(occurrence(1, 2), minutes=-6000, days=0,
                                   resizing=False)
        self.assertEqual(start.hour, 0)
        self.assertEqual(start.minute, 0)
        self.assertEqual(end.hour, 1)

    def test_it_cannot_be_dragged_off_the_bottom_of_the_day(self):
        original = occurrence(22, 23)
        start, end = dragged_times(original, minutes=6000, days=0, resizing=False)
        self.assertEqual(end - start, original.end - original.start)
        self.assertLessEqual(
            (end - start.replace(hour=0, minute=0)).total_seconds() / 60,
            MINUTES_PER_DAY)

    def test_dragging_sideways_changes_the_day(self):
        start, end = dragged_times(occurrence(9, 10), minutes=0, days=2,
                                   resizing=False)
        self.assertEqual(start.date(), date(2026, 9, 17))
        self.assertEqual(start.hour, 9)
        self.assertEqual(end.hour, 10)

    def test_moving_by_nothing_changes_nothing(self):
        original = occurrence(9, 10)
        self.assertEqual(dragged_times(original, minutes=0, days=0, resizing=False),
                         (original.start, original.end))


class Resizing(unittest.TestCase):
    def test_dragging_the_bottom_down_lengthens_it(self):
        start, end = dragged_times(occurrence(9, 10), minutes=60, days=0,
                                   resizing=True)
        self.assertEqual(start.hour, 9)
        self.assertEqual(end.hour, 11)

    def test_dragging_the_bottom_up_shortens_it(self):
        start, end = dragged_times(occurrence(9, 11), minutes=-60, days=0,
                                   resizing=True)
        self.assertEqual(end.hour, 10)

    def test_the_start_never_moves(self):
        original = occurrence(9, 11)
        start, _ = dragged_times(original, minutes=-300, days=0, resizing=True)
        self.assertEqual(start, original.start)

    def test_it_cannot_be_shortened_past_its_start(self):
        """One row is the shortest an event can be dragged to."""
        start, end = dragged_times(occurrence(9, 10), minutes=-6000, days=0,
                                   resizing=True)
        self.assertGreater(end, start)
        self.assertEqual((end - start).total_seconds() / 60, MIN_EVENT_MINUTES)

    def test_it_cannot_be_lengthened_past_midnight(self):
        start, end = dragged_times(occurrence(22, 23), minutes=6000, days=0,
                                   resizing=True)
        minutes = (end - start.replace(hour=0, minute=0)).total_seconds() / 60
        self.assertLessEqual(minutes, MINUTES_PER_DAY)


class TheGesture(unittest.TestCase):
    """The parts that need a real view."""

    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix="kairos-drag-"))
        self.sync = SyncManager(
            store=Storage(self.directory / "cache.db"),
            accounts=AccountStore(self.directory / "accounts.json"),
        )
        self.calendar = self.sync.calendars()[0]
        self.start = at(9)
        event = Event.new(self.calendar.id, self.start,
                          self.start + timedelta(hours=1), "Standup")
        self.sync.save_event(event)
        self.view = WeekView(self.sync)
        self.view.set_date(self.start.date())

    def tearDown(self):
        self.sync.storage.close()

    def blocks(self):
        found = []
        for grid in self.view._day_grids:
            child = grid.get_first_child()
            while child is not None:
                if isinstance(child, Gtk.Button):
                    found.append(child)
                child = child.get_next_sibling()
        return found

    def test_a_block_has_a_drag_gesture(self):
        block = self.blocks()[0]
        kinds = {type(c) for c in block.observe_controllers()}
        self.assertIn(Gtk.GestureDrag, kinds)

    def test_a_read_only_calendar_offers_no_drag(self):
        """A drag that silently does nothing is worse than none."""
        self.calendar.read_only = True
        self.sync.storage.save_calendar(self.calendar)
        view = WeekView(self.sync)
        view.set_date(self.start.date())
        for grid in view._day_grids:
            child = grid.get_first_child()
            while child is not None:
                if isinstance(child, Gtk.Button):
                    kinds = {type(c) for c in child.observe_controllers()}
                    self.assertNotIn(Gtk.GestureDrag, kinds)
                child = child.get_next_sibling()

    def test_a_drag_that_did_not_move_is_a_click(self):
        """Otherwise every click on an event would save it again."""
        moved = []
        self.view.connect("event-moved",
                          lambda *args: moved.append(args))
        block = self.blocks()[0]
        self.view._drag = {"resizing": False, "minutes": 0, "days": 0,
                           "moved": False, "all_day": False}
        self.view._on_drag_end(None, 0, 0, block, occurrence(9, 10))
        self.assertEqual(moved, [])

    def test_a_real_drag_reports_the_new_time(self):
        moved = []
        self.view.connect("event-moved",
                          lambda _v, o, s, e: moved.append((s, e)))
        self.view._drag = {"resizing": False, "minutes": 60, "days": 0,
                           "moved": True, "all_day": False}
        self.view._on_drag_end(None, 0, 60, self.blocks()[0], occurrence(9, 10))
        self.assertEqual(len(moved), 1)
        self.assertEqual(moved[0][0].hour, 10)


if __name__ == "__main__":
    unittest.main()


class ChoosingMoveOrResize(unittest.TestCase):
    """Where the press lands decides which one a drag is."""

    def block(self, height):
        """A real button whose height we can pretend to know."""
        button = Gtk.Button()
        button.get_allocated_height = lambda: height
        return button

    def mode(self, height, press_y):
        view = WeekView.__new__(WeekView)      # no window needed for this
        view._drag = {}
        view._on_drag_begin(None, 0, press_y, self.block(height), None, False)
        return view._drag["resizing"]

    def test_a_press_near_the_bottom_resizes(self):
        self.assertTrue(self.mode(height=60, press_y=57))

    def test_a_press_in_the_middle_moves(self):
        self.assertFalse(self.mode(height=60, press_y=30))

    def test_a_press_at_the_top_moves(self):
        self.assertFalse(self.mode(height=60, press_y=2))

    def test_an_unallocated_block_moves_rather_than_resizes(self):
        """Height is 0 before layout; "height - y <= grip" was then always true."""
        self.assertFalse(self.mode(height=0, press_y=5))

    def test_a_short_block_can_still_be_moved(self):
        """A fixed grip would swallow a block only a few pixels tall."""
        self.assertFalse(self.mode(height=12, press_y=5))

    def test_a_short_block_can_still_be_resized(self):
        self.assertTrue(self.mode(height=12, press_y=12))

    def test_most_of_any_block_is_for_moving(self):
        for height in (10, 12, 20, 48, 200):
            with self.subTest(height=height):
                self.assertFalse(self.mode(height=height,
                                           press_y=int(height * 0.5)))


class DraggingFreely(unittest.TestCase):
    """The bug: a drag moved an event one row and then no further.

    The block was re-attached at its new rows as the drag went on. A
    GestureDrag reports offsets from where the press landed *in the dragged
    widget's own coordinates*, so moving that widget moved the origin with
    it and the offset collapsed back towards zero. The block does not move
    any more; an indicator shows the target instead.
    """

    def test_a_long_drag_moves_a_long_way(self):
        start, _ = dragged_times(occurrence(9, 10), minutes=5 * 60, days=0,
                                 resizing=False)
        self.assertEqual(start.hour, 14)

    def test_every_distance_is_honoured(self):
        for minutes, expected in ((15, "09:15"), (30, "09:30"), (120, "11:00"),
                                  (7 * 60, "16:00"), (13 * 60, "22:00")):
            with self.subTest(minutes=minutes):
                start, _ = dragged_times(occurrence(9, 10), minutes=minutes,
                                         days=0, resizing=False)
                self.assertEqual(start.strftime("%H:%M"), expected)

    def test_the_arithmetic_itself_is_not_limited_to_the_quarter_hour(self):
        """Dragging snaps, but the placement maths does not have to.

        A server may put an event at 9:05 and it is drawn at 9:05; the snap
        is a decision about the gesture, not a limit of the grid.
        """
        start, _ = dragged_times(occurrence(9, 10), minutes=5, days=0,
                                 resizing=False)
        self.assertEqual(start.strftime("%H:%M"), "09:05")

    def test_the_snap_is_a_quarter_of_an_hour(self):
        self.assertEqual(DRAG_SNAP_MINUTES, 15)

    def test_a_resize_moves_in_the_same_steps(self):
        _, end = dragged_times(occurrence(9, 10), minutes=15, days=0,
                               resizing=True)
        self.assertEqual(end.strftime("%H:%M"), "10:15")


class DraggingAcrossDays(unittest.TestCase):
    def test_moving_several_days_forward(self):
        start, end = dragged_times(occurrence(9, 10), minutes=0, days=4,
                                   resizing=False)
        self.assertEqual(start.date(), date(2026, 9, 19))
        self.assertEqual((start.hour, end.hour), (9, 10))

    def test_moving_backwards(self):
        start, _ = dragged_times(occurrence(9, 10), minutes=0, days=-3,
                                 resizing=False)
        self.assertEqual(start.date(), date(2026, 9, 12))

    def test_day_and_time_can_change_together(self):
        start, _ = dragged_times(occurrence(9, 10), minutes=90, days=2,
                                 resizing=False)
        self.assertEqual(start.date(), date(2026, 9, 17))
        self.assertEqual(start.strftime("%H:%M"), "10:30")

    def test_a_resize_never_changes_the_day(self):
        """Pulling the bottom edge sideways would be meaningless."""
        start, end = dragged_times(occurrence(9, 10), minutes=60, days=3,
                                   resizing=True)
        self.assertEqual(start.date(), end.date())


class DraggingAnAllDayEvent(unittest.TestCase):
    def all_day(self, days=1):
        start = datetime(2026, 9, 15, tzinfo=local_timezone())
        end = start + timedelta(days=days)
        event = Event.new("cal", start, end, "Trip", all_day=True)
        return Occurrence(event=event, start=start, end=end)

    def test_it_moves_to_another_day(self):
        start, _ = dragged_times(self.all_day(), minutes=0, days=2,
                                 resizing=False)
        self.assertEqual(start.date(), date(2026, 9, 17))

    def test_it_keeps_its_length(self):
        original = self.all_day(days=3)
        start, end = dragged_times(original, minutes=0, days=1, resizing=False)
        self.assertEqual(end - start, original.end - original.start)

    def test_a_vertical_drag_does_not_change_its_time(self):
        """There is no time on an all-day event to change."""
        original = self.all_day()
        start, _ = dragged_times(original, minutes=240, days=0, resizing=False)
        self.assertEqual(start, original.start)


class TheDropIndicator(TheGesture):
    def test_no_indicator_before_a_drag_starts(self):
        self.assertIsNone(self.view._indicator)

    def test_dragging_shows_one(self):
        occ = occurrence(9, 10)
        self.view._drag = {"resizing": False, "minutes": 60, "days": 0,
                           "moved": True, "all_day": False, "start": (0, 0)}
        self.view._show_indicator(occ)
        self.assertIsNotNone(self.view._indicator)
        self.assertTrue(self.view._indicator.has_css_class("kairos-drop-indicator"))

    def test_the_indicator_says_the_new_time(self):
        self.view._drag = {"resizing": False, "minutes": 90, "days": 0,
                           "moved": True, "all_day": False, "start": (0, 0)}
        self.view._show_indicator(occurrence(9, 10))
        label = self.view._indicator.get_first_child()
        self.assertIn("10", label.get_label())

    def test_the_indicator_lands_in_the_target_day(self):
        self.view._drag = {"resizing": False, "minutes": 0, "days": 2,
                           "moved": True, "all_day": False, "start": (0, 0)}
        self.view._show_indicator(occurrence(9, 10))
        parent = self.view._indicator.get_parent()
        expected = self.view._day_grids[
            (self.start.date() + timedelta(days=2) - self.view._first_day).days]
        self.assertIs(parent, expected)

    def test_only_one_indicator_at_a_time(self):
        occ = occurrence(9, 10)
        for minutes in (30, 60, 90):
            self.view._drag = {"resizing": False, "minutes": minutes, "days": 0,
                               "moved": True, "all_day": False, "start": (0, 0)}
            self.view._show_indicator(occ)
        indicators = []
        for grid in self.view._day_grids:
            child = grid.get_first_child()
            while child is not None:
                if isinstance(child, Gtk.Box) and child.has_css_class(
                        "kairos-drop-indicator"):
                    indicators.append(child)
                child = child.get_next_sibling()
        self.assertEqual(len(indicators), 1)

    def test_it_is_cleared_when_the_drag_ends(self):
        self.view._drag = {"resizing": False, "minutes": 60, "days": 0,
                           "moved": True, "all_day": False, "start": (0, 0)}
        self.view._show_indicator(occurrence(9, 10))
        self.view._on_drag_end(None, 0, 60, self.blocks()[0], occurrence(9, 10))
        self.assertIsNone(self.view._indicator)


class BlockPlacement(unittest.TestCase):
    """An event need not start on a quarter hour to be drawn where it is."""

    def place(self, start_minute, length=60, row_height=12):
        start = datetime(2026, 9, 15, 9, start_minute, tzinfo=local_timezone())
        end = start + timedelta(minutes=length)
        event = Event.new("cal", start, end, "Thing")
        occ = Occurrence(event=event, start=start, end=end)
        return WeekView._placement(occ, start.date(), row_height, 1)

    def test_an_event_on_the_hour_has_no_top_margin(self):
        _, top, _, _ = self.place(0)
        self.assertEqual(top, 0)

    def test_an_event_at_five_past_is_offset(self):
        """It used to be drawn at nine o'clock, which was simply wrong."""
        _, top, _, _ = self.place(5)
        self.assertGreater(top, 0)

    def test_the_offset_grows_with_the_minutes(self):
        tops = [self.place(minute)[1] for minute in (0, 5, 10)]
        self.assertEqual(tops, sorted(tops))
        self.assertLess(tops[0], tops[2])

    def test_it_stays_inside_the_rows_it_spans(self):
        for minute in range(0, 60, 5):
            with self.subTest(minute=minute):
                _, top, span, bottom = self.place(minute, row_height=12)
                self.assertGreaterEqual(bottom, 0)
                self.assertLessEqual(top + bottom, span * 12)


class WhatADragSnapsTo(unittest.TestCase):
    """The step the pointer actually moves in.

    `dragged_times` is given a number of minutes; this is the part that
    decides what that number can be. Five-minute steps made a drag fiddly to
    land where you meant, so it is a quarter of an hour.
    """

    def view(self, hour_height=48):
        view = WeekView.__new__(WeekView)
        view._row_height = lambda: max(1, hour_height // 4)
        return view

    def snap(self, pixels, hour_height=48):
        return self.view(hour_height)._snapped_minutes(pixels)

    def test_an_hour_of_travel_is_an_hour(self):
        self.assertEqual(self.snap(48), 60)

    def test_every_result_is_a_whole_number_of_steps(self):
        for pixels in range(-200, 201, 3):
            with self.subTest(pixels=pixels):
                self.assertEqual(self.snap(pixels) % DRAG_SNAP_MINUTES, 0)

    def test_a_small_movement_snaps_to_nothing(self):
        self.assertEqual(self.snap(1), 0)

    def test_it_rounds_to_the_nearest_step_not_downwards(self):
        # Ten pixels is 12.5 minutes at the default height: nearer 15 than 0.
        self.assertEqual(self.snap(10), 15)

    def test_it_works_upwards_too(self):
        self.assertEqual(self.snap(-48), -60)

    def test_a_denser_grid_still_snaps_the_same(self):
        """The step is in minutes, not pixels, so zoom must not change it."""
        for height in (24, 48, 96, 160):
            with self.subTest(hour_height=height):
                self.assertEqual(self.snap(height, height), 60)
                self.assertEqual(self.snap(height // 2, height), 30)
