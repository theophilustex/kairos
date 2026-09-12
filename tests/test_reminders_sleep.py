"""Reminders across a sleep, and the buttons on a reminder's notification.

The scheduler's timer counts only time the machine is awake, and anything
that came due more than half an hour earlier used to be marked as handled
without being shown. So a reminder that fell due while the lid was shut was
lost outright. These tests hold the fix.
"""

import tempfile
import time
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gio, GLib  # noqa: E402

from kairos.accounts import AccountStore  # noqa: E402
from kairos.config import settings  # noqa: E402
from kairos.models import Alarm, Event, local_timezone  # noqa: E402
from kairos.notifications import (MISSED_WHILE_ASLEEP_LIMIT, STALE_AFTER,  # noqa: E402
                                  AlarmScheduler, clock_jumped,
                                  day_from_reminder_key)
from kairos.storage import Storage  # noqa: E402
from kairos.sync import SyncManager  # noqa: E402


class FakeApplication:
    def __init__(self):
        self.sent, self.withdrawn = [], []

    def send_notification(self, notification_id, notification):
        self.sent.append(notification_id)

    def withdraw_notification(self, notification_id):
        self.withdrawn.append(notification_id)


class TheClock(unittest.TestCase):
    def test_normal_running_is_not_a_jump(self):
        self.assertFalse(clock_jumped(60.2, 60.0))

    def test_a_suspend_is_a_jump(self):
        self.assertTrue(clock_jumped(3600, 60))

    def test_a_clock_set_backwards_is_a_jump(self):
        self.assertTrue(clock_jumped(-600, 60))


class WhatStillCountsAsDue(unittest.TestCase):
    NOW = datetime(2026, 9, 12, 12, 0, tzinfo=local_timezone())

    def test_a_recent_reminder_is_shown_even_if_its_event_ended(self):
        fire_at = self.NOW - timedelta(minutes=10)
        self.assertTrue(AlarmScheduler.should_raise(
            fire_at, self.NOW - timedelta(minutes=5), self.NOW, self.NOW - STALE_AFTER))

    def test_one_missed_in_a_sleep_is_shown_if_its_event_is_still_to_come(self):
        fire_at = self.NOW - timedelta(hours=2)
        self.assertTrue(AlarmScheduler.should_raise(
            fire_at, self.NOW + timedelta(hours=1), self.NOW, self.NOW - timedelta(hours=4)))

    def test_one_missed_in_a_sleep_is_not_shown_once_its_event_is_over(self):
        fire_at = self.NOW - timedelta(hours=3)
        self.assertFalse(AlarmScheduler.should_raise(
            fire_at, self.NOW - timedelta(hours=2), self.NOW, self.NOW - timedelta(hours=4)))

    def test_nothing_from_before_the_look_back(self):
        fire_at = self.NOW - timedelta(hours=5)
        self.assertFalse(AlarmScheduler.should_raise(
            fire_at, self.NOW + timedelta(hours=1), self.NOW, self.NOW - timedelta(hours=4)))


class HowFarBack(unittest.TestCase):
    def setUp(self):
        self.scheduler = AlarmScheduler(None, sync_manager=None)
        self.now = datetime.now(tz=local_timezone())

    def test_a_fresh_start_looks_back_half_an_hour(self):
        """Opening Kairos must not replay this morning's reminders."""
        self.assertEqual(self.scheduler._missed_since(self.now), self.now - STALE_AFTER)

    def test_after_a_sleep_it_reaches_back_to_the_last_check(self):
        self.scheduler._last_check = self.now - timedelta(hours=3)
        self.assertEqual(self.scheduler._missed_since(self.now), self.now - timedelta(hours=3))

    def test_but_never_more_than_a_day(self):
        self.scheduler._last_check = self.now - timedelta(days=5)
        self.assertEqual(self.scheduler._missed_since(self.now),
                         self.now - MISSED_WHILE_ASLEEP_LIMIT)

    def test_a_recent_check_still_covers_half_an_hour(self):
        self.scheduler._last_check = self.now - timedelta(minutes=5)
        self.assertEqual(self.scheduler._missed_since(self.now), self.now - STALE_AFTER)


class WithAScheduler(unittest.TestCase):
    """A real scheduler over a real cache, with what it shows captured."""

    def setUp(self):
        settings.set("notifications_enabled", True)
        self.directory = Path(tempfile.mkdtemp(prefix="kairos-sleep-"))
        self.sync = SyncManager(
            store=Storage(self.directory / "cache.db"),
            accounts=AccountStore(self.directory / "accounts.json"),
        )
        self.calendar = self.sync.calendars()[0]
        self.scheduler = AlarmScheduler(FakeApplication(), self.sync)
        self.shown = []
        self.scheduler._notify = lambda reminder: self.shown.append(reminder.summary)
        self.now = datetime.now(tz=local_timezone())

    def tearDown(self):
        self.scheduler._cancel_timer()
        self.sync.storage.close()

    def add(self, summary, start, length, minutes_before):
        event = Event.new(self.calendar.id, start, start + length, summary)
        event.alarms = [Alarm(minutes_before)]
        self.sync.save_event(event)


class WakingUp(WithAScheduler):
    """The real scheduler, over a real cache, with the lid shut in between."""

    def test_a_reminder_that_came_due_while_asleep_is_shown_on_waking(self):
        # Due two and a half hours ago; the event is still half an hour away.
        self.add("Missed in the sleep", self.now + timedelta(minutes=30),
                 timedelta(hours=1), minutes_before=180)
        self.scheduler._last_check = self.now - timedelta(hours=4)
        self.scheduler.reschedule()
        self.assertEqual(self.shown, ["Missed in the sleep"])

    def test_the_old_behaviour_was_to_drop_it(self):
        """Without a last check to reach back to — a fresh start — it is not replayed."""
        self.add("From before Kairos started", self.now + timedelta(minutes=30),
                 timedelta(hours=1), minutes_before=180)
        self.scheduler.reschedule()
        self.assertEqual(self.shown, [])

    def test_an_event_that_finished_during_the_sleep_is_not_shown(self):
        self.add("Already over", self.now - timedelta(hours=3),
                 timedelta(hours=1), minutes_before=10)
        self.scheduler._last_check = self.now - timedelta(hours=4)
        self.scheduler.reschedule()
        self.assertEqual(self.shown, [])

    def test_it_is_shown_only_once(self):
        self.add("Once", self.now + timedelta(minutes=30), timedelta(hours=1), 180)
        self.scheduler._last_check = self.now - timedelta(hours=4)
        self.scheduler.reschedule()
        self.scheduler.reschedule()
        self.assertEqual(self.shown, ["Once"])

    def test_waking_triggers_a_check(self):
        calls = []
        self.scheduler.reschedule = lambda *a: calls.append("checked")
        self.scheduler._on_prepare_for_sleep(None, None, None, None, None,
                                             GLib.Variant("(b)", (False,)))
        self.assertEqual(calls, ["checked"])

    def test_going_to_sleep_does_not(self):
        calls = []
        self.scheduler.reschedule = lambda *a: calls.append("checked")
        self.scheduler._on_prepare_for_sleep(None, None, None, None, None,
                                             GLib.Variant("(b)", (True,)))
        self.assertEqual(calls, [])

    def test_a_clock_jump_triggers_a_check(self):
        calls = []
        self.scheduler.reschedule = lambda *a: calls.append("checked")
        self.scheduler._heartbeat_wall = time.time() - 3600
        self.scheduler._heartbeat_awake = time.monotonic() - 60
        self.assertEqual(self.scheduler._on_heartbeat(), GLib.SOURCE_CONTINUE)
        self.assertEqual(calls, ["checked"])

    def test_an_ordinary_minute_does_not(self):
        calls = []
        self.scheduler.reschedule = lambda *a: calls.append("checked")
        self.scheduler._heartbeat_wall = time.time() - 60
        self.scheduler._heartbeat_awake = time.monotonic() - 60
        self.scheduler._on_heartbeat()
        self.assertEqual(calls, [])

    def test_starting_and_stopping_leaves_nothing_running(self):
        self.scheduler.start()
        self.scheduler.stop()
        self.assertEqual(self.scheduler._heartbeat_id, 0)
        self.assertEqual(self.scheduler._sleep_subscription, 0)
        self.assertEqual(self.scheduler._timer_id, 0)


class TheNotificationsButtons(WithAScheduler):
    def setUp(self):
        super().setUp()
        del self.scheduler._notify              # the real one this time
        self.buttons = []
        self.original = Gio.Notification.add_button_with_target

        def spy(notification, label, action, target=None):
            self.buttons.append((label, action, target.get_string() if target else None))

        Gio.Notification.add_button_with_target = spy

    def tearDown(self):
        Gio.Notification.add_button_with_target = self.original
        super().tearDown()

    def raise_one(self):
        self.add("Standup", self.now + timedelta(minutes=5), timedelta(hours=1), 10)
        self.scheduler.reschedule()
        return next(iter(self.scheduler._recent.values()))

    def test_it_has_snooze_and_open(self):
        reminder = self.raise_one()
        settings.set("reminder_snooze_minutes", 10)
        self.assertEqual([action for _l, action, _t in self.buttons],
                         ["app.snooze-reminder", "app.open-reminder"])
        self.assertTrue(all(target == reminder.key for _l, _a, target in self.buttons))

    def test_snooze_says_how_long(self):
        settings.set("reminder_snooze_minutes", 10)
        self.raise_one()
        self.assertIn("10 minutes", self.buttons[0][0])

    def test_the_button_can_find_its_reminder(self):
        reminder = self.raise_one()
        self.assertIs(self.scheduler.reminder_by_key(reminder.key), reminder)

    def test_withdrawing_takes_down_the_notification_that_was_sent(self):
        reminder = self.raise_one()
        self.scheduler.withdraw(reminder)
        self.assertEqual(self.scheduler.application.withdrawn,
                         self.scheduler.application.sent)


class TheKey(unittest.TestCase):
    def test_it_gives_the_day(self):
        stamp = int(datetime(2026, 9, 15, 10, 0, tzinfo=local_timezone()).timestamp())
        self.assertEqual(day_from_reminder_key(f"abc@example|{stamp}|10"), date(2026, 9, 15))

    def test_a_uid_containing_the_separator_still_works(self):
        stamp = int(datetime(2026, 9, 15, 10, 0, tzinfo=local_timezone()).timestamp())
        self.assertEqual(day_from_reminder_key(f"odd|uid|{stamp}|10"), date(2026, 9, 15))

    def test_nonsense_is_none(self):
        self.assertIsNone(day_from_reminder_key("not a key"))


class TheApplicationsActions(unittest.TestCase):
    """What pressing a button does, with everything around it stubbed."""

    def stub(self, reminder=None):
        from kairos.notifications import PendingReminder
        start = datetime(2026, 9, 15, 10, 0, tzinfo=local_timezone())
        held = reminder or PendingReminder(key="k", uid="u", summary="Standup",
                                           start=start, end=start + timedelta(hours=1))
        calls = []

        class Alarms:
            def reminder_by_key(self, key):
                return held if key == held.key else None

            def withdraw(self, r):
                calls.append(("withdraw", r.key))

            def snooze(self, r, minutes):
                calls.append(("snooze", r.key, minutes))

        class Alert:
            def forget(self, r):
                calls.append(("forget", r.key))

        class Window:
            def go_to_day(self, day):
                calls.append(("go_to_day", day))

        class Stub:
            alarms, alert_window, window = Alarms(), Alert(), Window()

            def activate(self):
                calls.append(("activate",))

            def _on_reminder_opened(self, _w, r):
                calls.append(("opened", r.key))

        return Stub(), held, calls

    def test_snooze_snoozes_withdraws_and_clears_the_alert(self):
        from kairos.app import KairosApplication
        settings.set("reminder_snooze_minutes", 10)
        stub, held, calls = self.stub()
        KairosApplication._on_snooze_reminder(stub, None, GLib.Variant("s", held.key))
        self.assertIn(("snooze", "k", 10), calls)
        self.assertIn(("withdraw", "k"), calls)
        self.assertIn(("forget", "k"), calls)

    def test_snooze_for_an_unknown_reminder_does_nothing(self):
        from kairos.app import KairosApplication
        stub, _held, calls = self.stub()
        KairosApplication._on_snooze_reminder(stub, None, GLib.Variant("s", "gone"))
        self.assertEqual(calls, [])

    def test_open_opens_and_clears(self):
        from kairos.app import KairosApplication
        stub, held, calls = self.stub()
        KairosApplication._on_open_reminder(stub, None, GLib.Variant("s", held.key))
        self.assertIn(("opened", "k"), calls)
        self.assertIn(("forget", "k"), calls)

    def test_open_after_a_restart_still_finds_the_day(self):
        from kairos.app import KairosApplication
        stub, _held, calls = self.stub()
        stamp = int(datetime(2026, 9, 15, 10, 0, tzinfo=local_timezone()).timestamp())
        KairosApplication._on_open_reminder(stub, None, GLib.Variant("s", f"x|{stamp}|10"))
        self.assertIn(("go_to_day", date(2026, 9, 15)), calls)


if __name__ == "__main__":
    unittest.main()
