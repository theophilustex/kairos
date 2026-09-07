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
from kairos.ui.week_view import (MINUTES_PER_ROW, ROWS_PER_DAY,  # noqa: E402
                                 WeekView, dragged_times)


def at(hour, minute=0):
    return datetime(2026, 9, 15, hour, minute, tzinfo=local_timezone())


def occurrence(start_hour, end_hour):
    start, end = at(start_hour), at(end_hour)
    event = Event.new("cal", start, end, "Standup")
    return Occurrence(event=event, start=start, end=end)


class Moving(unittest.TestCase):
    def test_dragging_down_moves_the_event_later(self):
        start, end = dragged_times(occurrence(9, 10), rows=4, days=0,
                                   resizing=False)
        self.assertEqual((start.hour, end.hour), (10, 11))

    def test_dragging_up_moves_it_earlier(self):
        start, end = dragged_times(occurrence(9, 10), rows=-4, days=0,
                                   resizing=False)
        self.assertEqual((start.hour, end.hour), (8, 9))

    def test_it_snaps_to_the_row(self):
        """A row is fifteen minutes; nothing lands between them."""
        start, _ = dragged_times(occurrence(9, 10), rows=1, days=0,
                                 resizing=False)
        self.assertEqual(start.minute % MINUTES_PER_ROW, 0)
        self.assertEqual(start.minute, 15)

    def test_the_length_is_unchanged(self):
        original = occurrence(9, 11)
        start, end = dragged_times(original, rows=7, days=0, resizing=False)
        self.assertEqual(end - start, original.end - original.start)

    def test_it_cannot_be_dragged_off_the_top_of_the_day(self):
        start, end = dragged_times(occurrence(1, 2), rows=-100, days=0,
                                   resizing=False)
        self.assertEqual(start.hour, 0)
        self.assertEqual(start.minute, 0)
        self.assertEqual(end.hour, 1)

    def test_it_cannot_be_dragged_off_the_bottom_of_the_day(self):
        original = occurrence(22, 23)
        start, end = dragged_times(original, rows=100, days=0, resizing=False)
        self.assertEqual(end - start, original.end - original.start)
        self.assertLessEqual(
            (end - start.replace(hour=0, minute=0)).total_seconds() / 60,
            ROWS_PER_DAY * MINUTES_PER_ROW)

    def test_dragging_sideways_changes_the_day(self):
        start, end = dragged_times(occurrence(9, 10), rows=0, days=2,
                                   resizing=False)
        self.assertEqual(start.date(), date(2026, 9, 17))
        self.assertEqual(start.hour, 9)
        self.assertEqual(end.hour, 10)

    def test_moving_by_nothing_changes_nothing(self):
        original = occurrence(9, 10)
        self.assertEqual(dragged_times(original, rows=0, days=0, resizing=False),
                         (original.start, original.end))


class Resizing(unittest.TestCase):
    def test_dragging_the_bottom_down_lengthens_it(self):
        start, end = dragged_times(occurrence(9, 10), rows=4, days=0,
                                   resizing=True)
        self.assertEqual(start.hour, 9)
        self.assertEqual(end.hour, 11)

    def test_dragging_the_bottom_up_shortens_it(self):
        start, end = dragged_times(occurrence(9, 11), rows=-4, days=0,
                                   resizing=True)
        self.assertEqual(end.hour, 10)

    def test_the_start_never_moves(self):
        original = occurrence(9, 11)
        start, _ = dragged_times(original, rows=-20, days=0, resizing=True)
        self.assertEqual(start, original.start)

    def test_it_cannot_be_shortened_past_its_start(self):
        """One row is the shortest an event can be dragged to."""
        start, end = dragged_times(occurrence(9, 10), rows=-100, days=0,
                                   resizing=True)
        self.assertGreater(end, start)
        self.assertEqual((end - start).total_seconds() / 60, MINUTES_PER_ROW)

    def test_it_cannot_be_lengthened_past_midnight(self):
        start, end = dragged_times(occurrence(22, 23), rows=100, days=0,
                                   resizing=True)
        minutes = (end - start.replace(hour=0, minute=0)).total_seconds() / 60
        self.assertLessEqual(minutes, ROWS_PER_DAY * MINUTES_PER_ROW)


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
        self.view._drag = {"resizing": False, "rows": 0, "days": 0,
                           "moved": False}
        self.view._on_drag_end(None, 0, 0, block, occurrence(9, 10))
        self.assertEqual(moved, [])

    def test_a_real_drag_reports_the_new_time(self):
        moved = []
        self.view.connect("event-moved",
                          lambda _v, o, s, e: moved.append((s, e)))
        self.view._drag = {"resizing": False, "rows": 4, "days": 0,
                           "moved": True}
        self.view._on_drag_end(None, 0, 60, self.blocks()[0], occurrence(9, 10))
        self.assertEqual(len(moved), 1)
        self.assertEqual(moved[0][0].hour, 10)


if __name__ == "__main__":
    unittest.main()


class ChoosingMoveOrResize(unittest.TestCase):
    """Where the press lands decides which one a drag is."""

    class FakeBlock:
        def __init__(self, height):
            self._height = height

        def get_allocated_height(self):
            return self._height

    def mode(self, height, press_y):
        view = WeekView.__new__(WeekView)      # no window needed for this
        view._drag = {}
        view._on_drag_begin(None, 0, press_y, self.FakeBlock(height), None)
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
