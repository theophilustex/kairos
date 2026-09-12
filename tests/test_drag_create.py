"""Dragging over empty space to draw out a new event.

Press at 10:00, drag down to 11:30, release: an event of exactly that
length. It shares its gestures with the click that picks a slot, so most of
what is tested here is that the two never mistake one another.
"""

import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402

Adw.init()

from kairos.accounts import AccountStore  # noqa: E402
from kairos.config import settings  # noqa: E402
from kairos.models import Event, start_of_day  # noqa: E402
from kairos.storage import Storage  # noqa: E402
from kairos.sync import SyncManager  # noqa: E402
from kairos.ui.week_view import (MINUTES_PER_DAY, SLOT_SNAP_MINUTES,  # noqa: E402
                                 WeekView, sketch_range)


class TheRange(unittest.TestCase):
    def test_dragging_down(self):
        self.assertEqual(sketch_range(600, 675), (600, 690))

    def test_dragging_up_works_too(self):
        self.assertEqual(sketch_range(675, 600), (600, 690))

    def test_no_movement_is_one_step_not_nothing(self):
        self.assertEqual(sketch_range(600, 600), (600, 600 + SLOT_SNAP_MINUTES))

    def test_it_stops_at_midnight(self):
        last = MINUTES_PER_DAY - SLOT_SNAP_MINUTES
        self.assertEqual(sketch_range(1380, last), (1380, MINUTES_PER_DAY))


class TheGesture(unittest.TestCase):
    DAY = date(2026, 9, 15)

    def setUp(self):
        settings.set("first_day_of_week", "monday")
        settings.set("hour_height", 48)
        self.directory = Path(tempfile.mkdtemp(prefix="kairos-sketch-"))
        self.sync = SyncManager(
            store=Storage(self.directory / "cache.db"),
            accounts=AccountStore(self.directory / "accounts.json"),
        )
        self.calendar = self.sync.calendars()[0]
        self.view = WeekView(self.sync)
        self.view.set_date(self.DAY)
        self.grid = self.view._day_grids[(self.DAY - self.view._first_day).days]
        self.grid.pick = lambda x, y, flags: self.grid        # empty space
        self.ranges, self.creates = [], []
        self.view.connect("create-range-requested",
                          lambda _v, s, e: self.ranges.append((s, e)))
        self.view.connect("create-requested", lambda _v, s: self.creates.append(s))

    def tearDown(self):
        self.sync.storage.close()

    def y(self, hour, minute=0):
        return (hour * 60 + minute) / 15 * self.view._row_height()

    def at(self, hour, minute=0):
        return start_of_day(self.DAY) + timedelta(hours=hour, minutes=minute)

    def drag(self, from_hour, to_hour, *, release_first=False):
        start_y = self.y(from_hour)
        dy = self.y(to_hour) - start_y
        self.view._on_sketch_begin(self.grid, 5, start_y)
        self.view._on_sketch_update(self.grid, 0, dy)
        if release_first:
            self.view._on_grid_released(self.grid, 1, 5, start_y + dy)
        self.view._on_sketch_end(self.grid, 0, dy)
        if not release_first:
            self.view._on_grid_released(self.grid, 1, 5, start_y + dy)

    def test_a_drag_creates_an_event_of_that_length(self):
        self.drag(10, 11.5)
        self.assertEqual(self.ranges, [(self.at(10), self.at(11, 45))])

    def test_dragging_upwards(self):
        self.drag(12, 10)
        self.assertEqual(self.ranges, [(self.at(10), self.at(12, 15))])

    def test_the_drag_shows_its_outline_while_it_happens(self):
        self.view._on_sketch_begin(self.grid, 5, self.y(10))
        self.view._on_sketch_update(self.grid, 0, self.y(11) - self.y(10))
        widget = self.view._sketch_widget
        self.assertIsNotNone(widget)
        self.assertTrue(widget.has_css_class("kairos-drop-indicator"))
        self.assertFalse(widget.get_can_target(),
                         "the outline would be what the next press lands on")

    def test_the_outline_goes_when_the_drag_ends(self):
        self.drag(10, 11)
        self.assertIsNone(self.view._sketch_widget)

    def test_its_release_is_not_also_a_click(self):
        """The bug to avoid: one drag making an event *and* picking a slot."""
        self.drag(10, 11)
        self.assertEqual(len(self.ranges), 1)
        self.assertIsNone(self.view._slot, "the drag's release armed a slot too")
        self.assertEqual(self.creates, [])

    def test_the_release_may_arrive_before_the_drag_ends(self):
        """GTK does not promise the order; either way it is one event."""
        self.drag(10, 11, release_first=True)
        self.assertEqual(len(self.ranges), 1)
        self.assertIsNone(self.view._slot)

    def test_a_wobble_during_a_click_is_still_a_click(self):
        y = self.y(10)
        self.view._on_sketch_begin(self.grid, 5, y)
        self.view._on_sketch_update(self.grid, 0, 2)          # two pixels
        self.view._on_sketch_end(self.grid, 0, 2)
        self.view._on_grid_released(self.grid, 1, 5, y)
        self.assertEqual(self.ranges, [])
        self.assertEqual(self.view._slot, (self.DAY, 600), "the click was lost")

    def test_a_click_after_a_drag_is_a_click_again(self):
        self.drag(10, 11)
        y = self.y(14)
        self.view._on_sketch_begin(self.grid, 5, y)
        self.view._on_sketch_end(self.grid, 0, 0)
        self.view._on_grid_released(self.grid, 1, 5, y)
        self.assertEqual(self.view._slot, (self.DAY, 14 * 60))

    def test_pressing_a_picked_slot_and_letting_go_still_creates(self):
        """The second click, now on release."""
        y = self.y(10)
        self.view._on_grid_released(self.grid, 1, 5, y)       # first click picks
        self.view._on_sketch_begin(self.grid, 5, y)
        self.view._on_sketch_end(self.grid, 0, 0)
        self.view._on_grid_released(self.grid, 1, 5, y)       # second click
        self.assertEqual(self.creates, [self.at(10)])

    def test_pressing_a_picked_slot_and_dragging_does_not_create_it_first(self):
        y = self.y(10)
        self.view._on_grid_released(self.grid, 1, 5, y)       # picks 10:00
        self.drag(10, 12)
        self.assertEqual(self.creates, [], "the press created before the drag began")
        self.assertEqual(len(self.ranges), 1)

    def test_a_press_on_an_event_is_that_event_s_drag(self):
        start = self.at(10)
        self.sync.save_event(Event.new(self.calendar.id, start,
                                       start + timedelta(hours=1), "Standup"))
        self.view.refresh()
        self.grid = self.view._day_grids[(self.DAY - self.view._first_day).days]
        button = Gtk.Button()
        self.grid.pick = lambda x, y, flags: button
        self.view._on_sketch_begin(self.grid, 5, self.y(10))
        self.view._on_sketch_update(self.grid, 0, 200)
        self.view._on_sketch_end(self.grid, 0, 200)
        self.assertEqual(self.ranges, [])
        self.assertIsNone(self.view._sketch_widget)


class TheEditor(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix="kairos-sketch-editor-"))
        self.sync = SyncManager(
            store=Storage(self.directory / "cache.db"),
            accounts=AccountStore(self.directory / "accounts.json"),
        )

    def tearDown(self):
        self.sync.storage.close()

    def saved(self, **kwargs):
        from kairos.ui.event_editor import EventEditor
        editor = EventEditor(self.sync.writable_calendars(), **kwargs)
        found = []
        editor.connect("saved", lambda _e, event: found.append(event))
        editor._on_save(None)
        return found[0]

    def test_it_opens_at_the_length_that_was_dragged(self):
        start = start_of_day(date(2026, 9, 15)) + timedelta(hours=10)
        event = self.saved(start=start, end=start + timedelta(minutes=105))
        self.assertEqual(event.end - event.start, timedelta(minutes=105))

    def test_without_an_end_it_uses_the_usual_length(self):
        settings.set("default_event_duration_minutes", 60)
        start = start_of_day(date(2026, 9, 15)) + timedelta(hours=10)
        event = self.saved(start=start)
        self.assertEqual(event.end - event.start, timedelta(hours=1))

    def test_an_end_before_the_start_is_ignored(self):
        settings.set("default_event_duration_minutes", 60)
        start = start_of_day(date(2026, 9, 15)) + timedelta(hours=10)
        event = self.saved(start=start, end=start - timedelta(hours=1))
        self.assertEqual(event.end - event.start, timedelta(hours=1))


if __name__ == "__main__":
    unittest.main()
