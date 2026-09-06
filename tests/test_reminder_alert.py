"""The alert window that a reminder raises, and snoozing.

A notification is easy to miss, so a due reminder also opens a window that
asks to be brought to the front. These tests cover the parts that are ours:
what the window holds, how it collapses several due reminders into one window,
and what snooze and dismiss do to the scheduler. Whether the compositor
actually grants focus is the desktop's business, not something to assert.
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

from kairos import notifications  # noqa: E402
from kairos.accounts import AccountStore  # noqa: E402
from kairos.config import settings  # noqa: E402
from kairos.models import Alarm, Event, local_timezone  # noqa: E402
from kairos.notifications import AlarmScheduler, PendingReminder  # noqa: E402
from kairos.storage import Storage  # noqa: E402
from kairos.sync import SyncManager  # noqa: E402
from kairos.ui.reminder_alert import ReminderAlertWindow  # noqa: E402


def at(hour, minute=0, days=0):
    day = date.today() + timedelta(days=days)
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=local_timezone())


def a_reminder(key="k1", summary="Standup", **kwargs) -> PendingReminder:
    fields = dict(
        key=key, uid="uid-" + key, summary=summary,
        start=at(9, 30), end=at(10, 0),
        location="Room 3", calendar_name="Work", calendar_colour="#e01b24",
        minutes_before=15, fire_at=at(9, 15),
    )
    fields.update(kwargs)
    return PendingReminder(**fields)


class ReminderWording(unittest.TestCase):
    def test_a_future_event_counts_down(self):
        now = datetime.now(tz=local_timezone())
        reminder = a_reminder(start=now + timedelta(minutes=12),
                              end=now + timedelta(minutes=42))
        self.assertIn("In", reminder.countdown_text())
        self.assertIn("minute", reminder.countdown_text())

    def test_an_event_underway_says_so(self):
        now = datetime.now(tz=local_timezone())
        reminder = a_reminder(start=now - timedelta(minutes=5),
                              end=now + timedelta(minutes=25))
        self.assertEqual(reminder.countdown_text(), "Happening now")

    def test_a_finished_event_says_how_long_ago(self):
        now = datetime.now(tz=local_timezone())
        reminder = a_reminder(start=now - timedelta(minutes=90),
                              end=now - timedelta(minutes=60))
        self.assertIn("ago", reminder.countdown_text())

    def test_the_when_line_has_the_day_and_the_times(self):
        settings.set("time_format", "24h")
        text = a_reminder().when_text()
        self.assertIn("Today", text)
        self.assertIn("09:30", text)
        self.assertIn("10:00", text)

    def test_an_all_day_event_says_all_day(self):
        self.assertIn("All day", a_reminder(all_day=True).when_text())

    def test_it_survives_being_written_and_read_back(self):
        original = a_reminder()
        restored = PendingReminder.from_json(original.to_json())
        self.assertEqual(restored.key, original.key)
        self.assertEqual(restored.summary, original.summary)
        self.assertEqual(restored.start, original.start)
        self.assertEqual(restored.end, original.end)
        self.assertEqual(restored.location, original.location)
        self.assertEqual(restored.calendar_colour, original.calendar_colour)
        self.assertEqual(restored.fire_at, original.fire_at)


class AlertWindowContents(unittest.TestCase):
    def setUp(self):
        self.application = Adw.Application(application_id="org.kairos.AlertTest")
        self.window = ReminderAlertWindow(self.application)

    def test_it_starts_empty(self):
        self.assertEqual(len(self.window._rows), 0)

    def test_showing_a_reminder_adds_a_row(self):
        self.window.show_reminder(a_reminder())
        self.assertEqual(len(self.window._rows), 1)

    def test_the_same_reminder_is_not_added_twice(self):
        reminder = a_reminder()
        self.window.show_reminder(reminder)
        self.window.show_reminder(reminder)
        self.assertEqual(len(self.window._rows), 1)

    def test_several_due_reminders_share_one_window(self):
        self.window.show_reminder(a_reminder("a", "First"))
        self.window.show_reminder(a_reminder("b", "Second"))
        self.window.show_reminder(a_reminder("c", "Third"))
        self.assertEqual(len(self.window._rows), 3)

    def test_the_title_counts_them(self):
        self.window.show_reminder(a_reminder("a"))
        self.assertEqual(self.window.get_title(), "Reminder")
        self.window.show_reminder(a_reminder("b"))
        self.assertEqual(self.window.get_title(), "2 reminders")

    def test_dismiss_all_only_appears_when_there_are_several(self):
        self.window.show_reminder(a_reminder("a"))
        self.assertFalse(self.window._dismiss_all.get_visible())
        self.window.show_reminder(a_reminder("b"))
        self.assertTrue(self.window._dismiss_all.get_visible())

    def test_dismissing_reports_which_reminder(self):
        seen = []
        self.window.connect("dismissed", lambda _w, r: seen.append(r))
        reminder = a_reminder()
        self.window.show_reminder(reminder)
        self.window._on_dismiss(reminder)
        self.assertEqual(seen, [reminder])
        self.assertEqual(len(self.window._rows), 0)

    def test_snoozing_reports_the_reminder_and_the_delay(self):
        seen = []
        self.window.connect("snoozed", lambda _w, r, m: seen.append((r, m)))
        reminder = a_reminder()
        self.window.show_reminder(reminder)
        self.window._on_snooze(reminder, 30)
        self.assertEqual(seen, [(reminder, 30)])
        self.assertEqual(len(self.window._rows), 0)

    def test_dismiss_all_reports_every_one(self):
        seen = []
        self.window.connect("dismissed", lambda _w, r: seen.append(r.key))
        for key in ("a", "b", "c"):
            self.window.show_reminder(a_reminder(key))
        self.window.dismiss_all()
        self.assertEqual(sorted(seen), ["a", "b", "c"])

    def test_dismissing_one_of_several_leaves_the_rest(self):
        first, second = a_reminder("a"), a_reminder("b")
        self.window.show_reminder(first)
        self.window.show_reminder(second)
        self.window._on_dismiss(first)
        self.assertEqual(list(self.window._rows), ["b"])


class SchedulerSnoozing(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix="kairos-snooze-"))
        self._real_history = notifications.HISTORY_FILE
        self._real_snoozes = notifications.SNOOZE_FILE
        notifications.HISTORY_FILE = self.directory / "fired.json"
        notifications.SNOOZE_FILE = self.directory / "snoozed.json"

        self.manager = SyncManager(
            store=Storage(self.directory / "cache.db"),
            accounts=AccountStore(self.directory / "accounts.json"),
        )
        self.calendar = self.manager.calendars()[0]
        self.application = Adw.Application(application_id="org.kairos.SnoozeTest")
        self.scheduler = AlarmScheduler(self.application, self.manager)
        settings.set("notifications_enabled", True)

    def tearDown(self):
        notifications.HISTORY_FILE = self._real_history
        notifications.SNOOZE_FILE = self._real_snoozes
        self.manager.storage.close()

    def test_snoozing_records_a_new_fire_time(self):
        reminder = a_reminder()
        before = datetime.now(tz=local_timezone())
        self.scheduler.snooze(reminder, 30)

        self.assertIn(reminder.key, self.scheduler._snoozed)
        stored = self.scheduler._snoozed[reminder.key]
        self.assertGreater(stored.fire_at, before + timedelta(minutes=29))
        self.assertTrue(stored.snoozed)

    def test_a_snoozed_reminder_does_not_also_fire_from_the_calendar(self):
        reminder = a_reminder()
        self.scheduler.snooze(reminder, 30)
        self.assertIn(reminder.key, self.scheduler._fired)

    def test_a_snooze_survives_a_restart(self):
        """The point of writing them to disk."""
        reminder = a_reminder(summary="Important thing")
        self.scheduler.snooze(reminder, 45)

        restarted = AlarmScheduler(self.application, self.manager)
        self.assertIn(reminder.key, restarted._snoozed)
        self.assertEqual(restarted._snoozed[reminder.key].summary, "Important thing")

    def test_dismissing_forgets_the_snooze(self):
        reminder = a_reminder()
        self.scheduler.snooze(reminder, 30)
        self.scheduler.dismiss(reminder)
        self.assertNotIn(reminder.key, self.scheduler._snoozed)

        restarted = AlarmScheduler(self.application, self.manager)
        self.assertNotIn(reminder.key, restarted._snoozed)

    def test_a_snooze_that_has_come_due_is_raised_again(self):
        raised = []
        self.scheduler.on_alert = raised.append
        settings.set("reminder_alert_window", True)

        reminder = a_reminder()
        self.scheduler.snooze(reminder, 30)
        # Wind it back so it is due now.
        self.scheduler._snoozed[reminder.key].fire_at = (
            datetime.now(tz=local_timezone()) - timedelta(seconds=1)
        )
        self.scheduler.reschedule()

        self.assertEqual([r.key for r in raised], [reminder.key])
        self.assertNotIn(reminder.key, self.scheduler._snoozed)

    def test_a_snooze_that_is_not_due_is_left_alone(self):
        raised = []
        self.scheduler.on_alert = raised.append
        self.scheduler.snooze(a_reminder(), 30)
        self.scheduler.reschedule()
        self.assertEqual(raised, [])

    def test_a_broken_snooze_file_is_ignored_rather_than_fatal(self):
        notifications.SNOOZE_FILE.write_text("[{\"nonsense\": true}]")
        scheduler = AlarmScheduler(self.application, self.manager)
        self.assertEqual(scheduler._snoozed, {})


class TheAlertCanBeTurnedOff(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix="kairos-alertoff-"))
        self._real_history = notifications.HISTORY_FILE
        self._real_snoozes = notifications.SNOOZE_FILE
        notifications.HISTORY_FILE = self.directory / "fired.json"
        notifications.SNOOZE_FILE = self.directory / "snoozed.json"
        self.manager = SyncManager(
            store=Storage(self.directory / "cache.db"),
            accounts=AccountStore(self.directory / "accounts.json"),
        )
        self.application = Adw.Application(application_id="org.kairos.AlertOffTest")
        self.scheduler = AlarmScheduler(self.application, self.manager)

    def tearDown(self):
        notifications.HISTORY_FILE = self._real_history
        notifications.SNOOZE_FILE = self._real_snoozes
        settings.set("reminder_alert_window", True)
        self.manager.storage.close()

    def test_the_window_is_not_raised_when_switched_off(self):
        raised = []
        self.scheduler.on_alert = raised.append
        settings.set("reminder_alert_window", False)
        self.scheduler._raise(a_reminder())
        self.assertEqual(raised, [], "the alert window ignored the preference")

    def test_it_is_raised_when_switched_on(self):
        raised = []
        self.scheduler.on_alert = raised.append
        settings.set("reminder_alert_window", True)
        self.scheduler._raise(a_reminder())
        self.assertEqual(len(raised), 1)

    def test_a_broken_alert_handler_does_not_stop_the_notification(self):
        """The notification is the fallback; it must go out regardless."""
        def explode(_reminder):
            raise RuntimeError("no display")

        self.scheduler.on_alert = explode
        settings.set("reminder_alert_window", True)
        sent = []
        self.scheduler._notify = lambda r: sent.append(r)
        self.scheduler._raise(a_reminder())
        self.assertEqual(len(sent), 1)


class EndToEnd(unittest.TestCase):
    """A real event, whose alarm comes due, reaches the alert."""

    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix="kairos-e2e-alert-"))
        self._real_history = notifications.HISTORY_FILE
        self._real_snoozes = notifications.SNOOZE_FILE
        notifications.HISTORY_FILE = self.directory / "fired.json"
        notifications.SNOOZE_FILE = self.directory / "snoozed.json"
        self.manager = SyncManager(
            store=Storage(self.directory / "cache.db"),
            accounts=AccountStore(self.directory / "accounts.json"),
        )
        self.calendar = self.manager.calendars()[0]
        self.application = Adw.Application(application_id="org.kairos.AlertE2E")
        self.scheduler = AlarmScheduler(self.application, self.manager)
        settings.set("notifications_enabled", True)
        settings.set("reminder_alert_window", True)

    def tearDown(self):
        notifications.HISTORY_FILE = self._real_history
        notifications.SNOOZE_FILE = self._real_snoozes
        self.manager.storage.close()

    def test_a_due_alarm_reaches_the_alert_with_the_event_details(self):
        raised = []
        self.scheduler.on_alert = raised.append
        self.scheduler._notify = lambda r: None      # no desktop in a test

        now = datetime.now(tz=local_timezone())
        event = Event.new(self.calendar.id, now + timedelta(minutes=5),
                          now + timedelta(minutes=35), "Dentist")
        event.location = "High Street"
        event.alarms = [Alarm(10)]                   # due five minutes ago
        self.manager.save_event(event)

        self.scheduler.reschedule()

        self.assertEqual(len(raised), 1)
        reminder = raised[0]
        self.assertEqual(reminder.summary, "Dentist")
        self.assertEqual(reminder.location, "High Street")
        self.assertEqual(reminder.calendar_name, self.calendar.name)
        self.assertEqual(reminder.calendar_colour, self.calendar.colour)
        self.assertEqual(reminder.minutes_before, 10)

    def test_it_is_not_raised_twice(self):
        raised = []
        self.scheduler.on_alert = raised.append
        self.scheduler._notify = lambda r: None

        now = datetime.now(tz=local_timezone())
        event = Event.new(self.calendar.id, now + timedelta(minutes=5),
                          now + timedelta(minutes=35), "Dentist")
        event.alarms = [Alarm(10)]
        self.manager.save_event(event)

        self.scheduler.reschedule()
        self.scheduler.reschedule()
        self.assertEqual(len(raised), 1)


if __name__ == "__main__":
    unittest.main()
