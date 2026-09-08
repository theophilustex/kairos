"""Picking a slot with two clicks, and a reminder set on a whole calendar.

The reminder half is not a parsing fix: VALARMs import correctly and always
did. It is for servers that keep reminders in their own interface and never
put a VALARM into the event at all, so the calendar arrives with nothing to
remind you about.
"""

import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw  # noqa: E402

Adw.init()

from kairos.accounts import AccountStore  # noqa: E402
from kairos.config import settings  # noqa: E402
from kairos.models import Alarm, Calendar, Event, local_timezone  # noqa: E402
from kairos.notifications import AlarmScheduler  # noqa: E402
from kairos.storage import Storage  # noqa: E402
from kairos.sync import SyncManager  # noqa: E402
from kairos.ui.month_view import MonthView  # noqa: E402
from kairos.ui.week_view import SLOT_SNAP_MINUTES, WeekView  # noqa: E402


class WithACalendar(unittest.TestCase):
    def setUp(self):
        settings.set("first_day_of_week", "monday")
        settings.set("default_event_duration_minutes", 60)
        self.directory = Path(tempfile.mkdtemp(prefix="kairos-slot-"))
        self.sync = SyncManager(
            store=Storage(self.directory / "cache.db"),
            accounts=AccountStore(self.directory / "accounts.json"),
        )
        self.calendar = self.sync.calendars()[0]
        self.day = date(2026, 9, 15)

    def tearDown(self):
        self.sync.storage.close()


class ClickingAnEmptySlot(WithACalendar):
    def view(self):
        view = WeekView(self.sync)
        view.set_date(self.day)
        return view

    def grid_for(self, view, day):
        return view._day_grids[(day - view._first_day).days]

    def click(self, view, day, y, presses=1):
        view._on_column_click(self.grid_for(view, day), presses, 0, y)

    def test_the_first_click_does_not_create_anything(self):
        view = self.view()
        created = []
        view.connect("create-requested", lambda _v, when: created.append(when))
        self.click(view, self.day, 10 * 4 * view._row_height())
        self.assertEqual(created, [], "an event appeared from one click")

    def test_the_first_click_highlights_the_slot(self):
        view = self.view()
        self.click(view, self.day, 10 * 4 * view._row_height())
        self.assertIsNotNone(view._slot)
        self.assertIsNotNone(view._slot_widget)
        self.assertTrue(view._slot_widget.has_css_class("kairos-slot-selection"))

    def test_clicking_the_same_slot_again_creates_there(self):
        view = self.view()
        created = []
        view.connect("create-requested", lambda _v, when: created.append(when))
        y = 10 * 4 * view._row_height()
        self.click(view, self.day, y)
        self.click(view, self.day, y)
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0].hour, 10)
        self.assertEqual(created[0].date(), self.day)

    def test_the_highlight_goes_away_once_it_is_used(self):
        view = self.view()
        y = 10 * 4 * view._row_height()
        self.click(view, self.day, y)
        self.click(view, self.day, y)
        self.assertIsNone(view._slot)
        self.assertIsNone(view._slot_widget)

    def test_clicking_a_different_slot_moves_the_highlight(self):
        view = self.view()
        created = []
        view.connect("create-requested", lambda _v, when: created.append(when))
        self.click(view, self.day, 10 * 4 * view._row_height())
        self.click(view, self.day, 14 * 4 * view._row_height())
        self.assertEqual(created, [], "moving the choice created an event")
        self.assertEqual(view._slot[1], 14 * 60)

    def test_a_different_day_is_a_different_slot(self):
        view = self.view()
        created = []
        view.connect("create-requested", lambda _v, when: created.append(when))
        y = 10 * 4 * view._row_height()
        self.click(view, self.day, y)
        self.click(view, self.day + timedelta(days=1), y)
        self.assertEqual(created, [])
        self.assertEqual(view._slot[0], self.day + timedelta(days=1))

    def test_a_double_click_still_creates_at_once(self):
        view = self.view()
        created = []
        view.connect("create-requested", lambda _v, when: created.append(when))
        self.click(view, self.day, 9 * 4 * view._row_height(), presses=2)
        self.assertEqual(len(created), 1)

    def test_the_slot_snaps(self):
        view = self.view()
        self.click(view, self.day, int(9.9 * 4 * view._row_height()))
        self.assertEqual(view._slot[1] % SLOT_SNAP_MINUTES, 0)

    def test_a_redraw_keeps_the_choice(self):
        """Refreshing rebuilds the grids; the highlight must come back."""
        view = self.view()
        y = 10 * 4 * view._row_height()
        self.click(view, self.day, y)
        view.refresh()
        self.assertIsNotNone(view._slot)
        self.assertIsNotNone(view._slot_widget)

    def test_changing_week_does_not_leave_a_stale_highlight(self):
        view = self.view()
        self.click(view, self.day, 10 * 4 * view._row_height())
        view.go_next()
        self.assertIsNone(view._slot_widget)


class ClickingAnEmptyDayCell(WithACalendar):
    def view(self):
        view = MonthView(self.sync)
        view.set_date(self.day)
        return view

    def test_the_first_click_only_selects(self):
        view = self.view()
        created = []
        view.connect("create-requested", lambda _v, when: created.append(when))
        view._cells[self.day]._on_click(1, 0, 0)
        self.assertEqual(created, [])
        self.assertEqual(view.selected_day, self.day)

    def test_the_second_click_creates(self):
        view = self.view()
        created = []
        view.connect("create-requested", lambda _v, when: created.append(when))
        view._cells[self.day]._on_click(1, 0, 0)
        view._cells[self.day]._on_click(1, 0, 0)
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0].date(), self.day)

    def test_an_already_selected_day_still_needs_two_clicks(self):
        """Arriving on a day by another route must not arm it."""
        view = self.view()
        view.select_day(self.day)          # e.g. from the sidebar
        created = []
        view.connect("create-requested", lambda _v, when: created.append(when))
        view._cells[self.day]._on_click(1, 0, 0)
        self.assertEqual(created, [], "one click created on a pre-selected day")

    def test_clicking_a_different_day_disarms_the_first(self):
        view = self.view()
        created = []
        view.connect("create-requested", lambda _v, when: created.append(when))
        other = self.day + timedelta(days=1)
        view._cells[self.day]._on_click(1, 0, 0)
        view._cells[other]._on_click(1, 0, 0)
        self.assertEqual(created, [])

    def test_paging_disarms(self):
        view = self.view()
        view._cells[self.day]._on_click(1, 0, 0)
        view.set_date(date(2026, 10, 15))
        self.assertIsNone(view.armed_day)


class ACalendarWideReminder(WithACalendar):
    """For servers that never send a VALARM."""

    def add(self, hours_ahead=0.5, alarms=()):
        start = datetime.now(tz=local_timezone()) + timedelta(hours=hours_ahead)
        event = Event.new(self.calendar.id, start, start + timedelta(hours=1),
                          "Thing")
        event.alarms = list(alarms)
        self.sync.save_event(event)
        return event

    def set_default(self, minutes):
        self.calendar.default_alarm_minutes = minutes
        self.sync.storage.save_calendar(self.calendar)

    def planned(self):
        """Alarms the scheduler would fire in the next lookahead window.

        The events are half an hour out so that both a 10-minute and a
        30-minute reminder land inside it; an alarm further off than
        ``notification_lookahead_minutes`` is correctly not planned yet.
        """
        scheduler = AlarmScheduler(None, self.sync)
        return scheduler.upcoming_alarms(datetime.now(tz=local_timezone()))

    def test_without_a_default_an_event_with_no_alarm_is_silent(self):
        self.add()
        self.assertEqual(self.planned(), [])

    def test_the_calendar_default_fills_the_gap(self):
        self.add()
        self.set_default(30)
        planned = self.planned()
        self.assertEqual(len(planned), 1)
        self.assertEqual(planned[0][2], 30)

    def test_an_event_with_its_own_reminder_keeps_it(self):
        self.add(alarms=[Alarm(10)])
        self.set_default(30)
        planned = self.planned()
        self.assertEqual([p[2] for p in planned], [10],
                         "the calendar default overrode the event's own")

    def test_minus_one_means_no_default(self):
        self.add()
        self.set_default(-1)
        self.assertEqual(self.planned(), [])

    def test_it_survives_being_stored(self):
        self.set_default(15)
        again = [c for c in self.sync.calendars() if c.id == self.calendar.id][0]
        self.assertEqual(again.default_alarm_minutes, 15)
        self.assertEqual(again.default_alarm, Alarm(15))

    def test_it_is_never_written_into_the_event(self):
        """It is a preference about being told, not a change to the data."""
        event = self.add()
        self.set_default(30)
        stored = self.sync.storage.get_event(self.calendar.id, event.uid)
        self.assertEqual(stored.alarms, [])
        self.assertNotIn("VALARM", stored.raw_ics)

    def test_a_calendar_with_no_default_reports_none(self):
        self.assertIsNone(Calendar(id="x", account_id="a", name="n").default_alarm)


if __name__ == "__main__":
    unittest.main()
