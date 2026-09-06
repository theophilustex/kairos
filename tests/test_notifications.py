"""Planning reminders.

The scheduler is the part of Kairos most likely to go wrong quietly — a
missed reminder produces no error, just a meeting you did not go to.  These
tests pin down the planning logic without waiting for real time to pass.
"""

import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gio  # noqa: E402

from kairos import notifications
from kairos.accounts import AccountStore
from kairos.config import settings
from kairos.models import Alarm, Event, local_timezone
from kairos.storage import Storage
from kairos.sync import SyncManager


def at(hour, minute=0, days=0):
    """A time today, or ``days`` from today.

    Relative to today on purpose: these tests once used fixed dates in
    September 2026 and quietly started failing when that date passed, because
    the fired-alarm history prunes anything older than a day.
    """
    day = date.today() + timedelta(days=days)
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=local_timezone())


class SchedulerTestCase(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix="kairos-alarms-"))
        storage = Storage(self.directory / "cache.db")
        accounts = AccountStore(self.directory / "accounts.json")
        self.manager = SyncManager(store=storage, accounts=accounts)
        self.calendar = self.manager.calendars()[0]

        # Point the fired-alarm history at the sandbox, and put it back after.
        self._real_history = notifications.HISTORY_FILE
        notifications.HISTORY_FILE = self.directory / "fired.json"

        self.application = Gio.Application(application_id="org.kairos.Tests")
        self.scheduler = notifications.AlarmScheduler(self.application, self.manager)

        settings.set("notifications_enabled", True)
        settings.set("notification_lookahead_minutes", 60)

    def tearDown(self):
        notifications.HISTORY_FILE = self._real_history
        self.manager.storage.close()

    def add(self, summary, start, alarms, rrule=""):
        event = Event.new(self.calendar.id, start, start + timedelta(hours=1), summary)
        event.alarms = alarms
        event.rrule = rrule
        self.manager.save_event(event)
        return event


class Planning(SchedulerTestCase):
    def test_an_alarm_inside_the_window_is_planned(self):
        now = at(9, 0)
        self.add("Meeting", at(9, 30), [Alarm(15)])
        planned = self.scheduler.upcoming_alarms(now)
        self.assertEqual(len(planned), 1)
        self.assertEqual(planned[0][0], at(9, 15))
        self.assertEqual(planned[0][1].summary, "Meeting")

    def test_an_event_with_no_alarms_plans_nothing(self):
        self.add("Quiet", at(9, 30), [])
        self.assertEqual(self.scheduler.upcoming_alarms(at(9, 0)), [])

    def test_several_alarms_on_one_event_are_all_planned(self):
        now = at(9, 0)
        self.add("Meeting", at(9, 50), [Alarm(10), Alarm(30), Alarm(45)])
        planned = self.scheduler.upcoming_alarms(now)
        self.assertEqual(len(planned), 3)

    def test_alarms_come_back_in_time_order(self):
        now = at(9, 0)
        self.add("Later", at(9, 55), [Alarm(5)])
        self.add("Sooner", at(9, 20), [Alarm(5)])
        planned = self.scheduler.upcoming_alarms(now)
        self.assertEqual([p[1].summary for p in planned], ["Sooner", "Later"])

    def test_an_alarm_beyond_the_lookahead_is_not_planned_yet(self):
        settings.set("notification_lookahead_minutes", 30)
        self.add("Distant", at(14, 0), [Alarm(5)])
        self.assertEqual(self.scheduler.upcoming_alarms(at(9, 0)), [])

    def test_a_long_reminder_is_found_even_though_the_event_is_far_off(self):
        """A week-ahead reminder must not be missed because the event is."""
        now = at(9, 0)
        self.add("Far away", at(9, 30, days=7), [Alarm(10080)])   # 7 days
        planned = self.scheduler.upcoming_alarms(now)
        self.assertEqual(len(planned), 1)
        self.assertEqual(planned[0][1].summary, "Far away")

    def test_an_alarm_at_the_moment_of_the_event(self):
        now = at(9, 0)
        self.add("Now", at(9, 30), [Alarm(0)])
        planned = self.scheduler.upcoming_alarms(now)
        self.assertEqual(planned[0][0], at(9, 30))

    def test_events_from_hidden_calendars_still_notify(self):
        """Hiding a calendar changes the view, not whether you are reminded."""
        self.add("Hidden", at(9, 30), [Alarm(15)])
        self.manager.set_calendar_visible(self.calendar, False)
        self.assertEqual(len(self.scheduler.upcoming_alarms(at(9, 0))), 1)

    def test_nothing_is_planned_when_reminders_are_switched_off(self):
        """The switch guards the scheduling, which is what stops the timer."""
        self.add("Meeting", at(9, 30), [Alarm(15)])
        settings.set("notifications_enabled", False)
        self.scheduler.reschedule()
        self.assertEqual(self.scheduler._timer_id, 0)
        settings.set("notifications_enabled", True)


class RepeatingReminders(SchedulerTestCase):
    def test_the_next_instance_of_a_series_is_planned(self):
        # A daily 09:30 meeting, first held a week ago.
        self.add("Daily standup", at(9, 30, days=-4), [Alarm(10)], rrule="FREQ=DAILY")
        planned = self.scheduler.upcoming_alarms(at(9, 0))
        self.assertEqual(len(planned), 1)
        self.assertEqual(planned[0][0], at(9, 20))
        self.assertEqual(planned[0][1].start, at(9, 30))

    def test_only_the_instances_inside_the_window_are_planned(self):
        self.add("Daily standup", at(9, 30, days=-4), [Alarm(10)], rrule="FREQ=DAILY")
        planned = self.scheduler.upcoming_alarms(at(23, 0))
        self.assertEqual(planned, [])


class NotRepeatingItself(SchedulerTestCase):
    def test_an_alarm_that_already_fired_is_not_planned_again(self):
        now = at(9, 0)
        self.add("Meeting", at(9, 30), [Alarm(15)])

        planned = self.scheduler.upcoming_alarms(now)
        self.assertEqual(len(planned), 1)

        fire_at, occurrence, minutes = planned[0]
        self.scheduler._mark_fired(fire_at, occurrence, minutes)

        self.assertEqual(self.scheduler.upcoming_alarms(now), [])

    def test_the_history_survives_a_restart(self):
        now = at(9, 0)
        self.add("Meeting", at(9, 30), [Alarm(15)])
        fire_at, occurrence, minutes = self.scheduler.upcoming_alarms(now)[0]
        self.scheduler._mark_fired(fire_at, occurrence, minutes)

        # A fresh scheduler, as if Kairos had been restarted.
        restarted = notifications.AlarmScheduler(self.application, self.manager)
        self.assertEqual(restarted.upcoming_alarms(now), [])

    def test_two_instances_of_a_series_are_tracked_separately(self):
        self.add("Daily", at(9, 30, days=-4), [Alarm(10)], rrule="FREQ=DAILY")

        today = self.scheduler.upcoming_alarms(at(9, 0))[0]
        self.scheduler._mark_fired(*today)

        tomorrow = self.scheduler.upcoming_alarms(at(9, 0, days=1))
        self.assertEqual(len(tomorrow), 1, "tomorrow's reminder was suppressed too")


class Wording(unittest.TestCase):
    def test_minute_phrases(self):
        self.assertEqual(notifications._minutes_phrase(1), "1 minute")
        self.assertEqual(notifications._minutes_phrase(15), "15 minutes")

    def test_hour_phrases(self):
        self.assertEqual(notifications._minutes_phrase(60), "1 hour")
        self.assertEqual(notifications._minutes_phrase(90), "1 hour 30 minutes")
        self.assertEqual(notifications._minutes_phrase(120), "2 hours")

    def test_day_phrases(self):
        self.assertEqual(notifications._minutes_phrase(1440), "1 day")
        self.assertEqual(notifications._minutes_phrase(2880), "2 days")


if __name__ == "__main__":
    unittest.main()
