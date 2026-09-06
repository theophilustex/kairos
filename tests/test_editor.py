"""The event editor's inputs: the clock, and reminders.

Both of these are places where the widget can disagree with what the user
asked for — a 12-hour clock with a 0-23 spinner underneath it, or a reminder
offset the presets cannot express.
"""

import unittest
from datetime import datetime

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw  # noqa: E402

Adw.init()

from kairos.config import settings  # noqa: E402
from kairos.models import Alarm, Calendar, Event, describe_offset, local_timezone  # noqa: E402
from kairos.ui.event_editor import EventEditor  # noqa: E402
from kairos.ui.widgets import TimeButton  # noqa: E402


def at(hour, minute=0):
    return datetime(2026, 9, 5, hour, minute, tzinfo=local_timezone())


CALENDARS = [Calendar(id="cal", account_id="a", name="Test")]


class TwentyFourHourPicker(unittest.TestCase):
    def setUp(self):
        settings.set("time_format", "24h")

    def test_the_hour_spinner_covers_the_whole_day(self):
        button = TimeButton(at(14, 30))
        adjustment = button._hour_spin.get_adjustment()
        self.assertEqual((adjustment.get_lower(), adjustment.get_upper()), (0, 23))

    def test_no_am_pm_chooser(self):
        self.assertIsNone(TimeButton(at(14, 30))._meridiem)

    def test_the_label_reads_as_24_hour(self):
        self.assertEqual(TimeButton(at(14, 30)).get_label(), "14:30")


class TwelveHourPicker(unittest.TestCase):
    """The bug: the label honoured the preference but the picker did not."""

    def setUp(self):
        settings.set("time_format", "12h")

    def tearDown(self):
        settings.set("time_format", "24h")

    def test_the_hour_spinner_runs_one_to_twelve(self):
        button = TimeButton(at(14, 30))
        adjustment = button._hour_spin.get_adjustment()
        self.assertEqual((adjustment.get_lower(), adjustment.get_upper()), (1, 12))

    def test_there_is_an_am_pm_chooser(self):
        self.assertIsNotNone(TimeButton(at(14, 30))._meridiem)

    def test_afternoon_shows_as_pm(self):
        button = TimeButton(at(14, 30))
        self.assertEqual(int(button._hour_spin.get_value()), 2)
        self.assertEqual(button._meridiem.get_selected(), 1)

    def test_morning_shows_as_am(self):
        button = TimeButton(at(9, 5))
        self.assertEqual(int(button._hour_spin.get_value()), 9)
        self.assertEqual(button._meridiem.get_selected(), 0)

    def test_midnight_is_twelve_am(self):
        button = TimeButton(at(0, 0))
        self.assertEqual(int(button._hour_spin.get_value()), 12)
        self.assertEqual(button._meridiem.get_selected(), 0)

    def test_noon_is_twelve_pm(self):
        button = TimeButton(at(12, 0))
        self.assertEqual(int(button._hour_spin.get_value()), 12)
        self.assertEqual(button._meridiem.get_selected(), 1)

    def test_every_time_of_day_survives_the_round_trip(self):
        for hour in range(24):
            for minute in (0, 5, 30, 55):
                with self.subTest(hour=hour, minute=minute):
                    button = TimeButton(at(hour, minute))
                    self.assertEqual(button._read_from_picker(), (hour, minute))

    def test_choosing_pm_moves_the_time_into_the_afternoon(self):
        button = TimeButton(at(9, 0))
        button._meridiem.set_selected(1)
        self.assertEqual(button.hour, 21)

    def test_the_editor_uses_the_twelve_hour_picker(self):
        editor = EventEditor(CALENDARS, start=at(14, 30))
        self.assertIsNotNone(editor._start_row.time_button._meridiem)
        self.assertEqual(editor._start_row.time_button.get_label(), "2:30 PM")


class CustomReminders(unittest.TestCase):
    """Presets are not enough: "5 days before" has to be reachable."""

    def editor(self, event=None, start=None):
        return EventEditor(CALENDARS, event=event, start=start or at(10, 0))

    def test_the_dropdown_offers_a_custom_entry(self):
        editor = self.editor()
        row = editor._alarm_rows[0]
        labels = [row.get_model().get_string(i)
                  for i in range(row.get_model().get_n_items())]
        self.assertIn("Custom…", labels)

    def test_the_custom_entry_is_not_a_real_offset(self):
        """Saving must never write the sentinel out as a reminder."""
        editor = self.editor()
        row = editor._alarm_rows[0]
        row.set_selected(len(row.values) - 1)          # the "Custom…" entry
        self.assertEqual(editor._collect_alarms(), [])

    def test_an_arbitrary_offset_can_be_set_and_is_kept(self):
        editor = self.editor()
        row = editor._alarm_rows[0]
        editor._fill_alarm_row(row, 7200)              # five days
        self.assertEqual([a.minutes_before for a in editor._collect_alarms()], [7200])

    def test_an_arbitrary_offset_is_shown_in_words(self):
        editor = self.editor()
        row = editor._alarm_rows[0]
        editor._fill_alarm_row(row, 20160)             # two weeks
        self.assertEqual(row.get_model().get_string(row.get_selected()), "2 weeks before")

    def test_a_custom_offset_sorts_in_among_the_presets(self):
        editor = self.editor()
        row = editor._alarm_rows[0]
        editor._fill_alarm_row(row, 45)                # between 30 min and 1 hour
        offsets = [v for v in row.values if isinstance(v, int)]
        self.assertEqual(offsets, sorted(offsets))

    def test_an_offset_from_a_server_survives_being_opened(self):
        event = Event.new("cal", at(10, 0), at(11, 0), "From a server")
        event.alarms = [Alarm(7200)]                   # five days, not a preset
        editor = self.editor(event=event)
        self.assertEqual([a.minutes_before for a in editor._collect_alarms()], [7200])

    def test_several_reminders_including_a_custom_one(self):
        event = Event.new("cal", at(10, 0), at(11, 0), "Many")
        event.alarms = [Alarm(15), Alarm(7200), Alarm(10080)]
        editor = self.editor(event=event)
        self.assertEqual(
            sorted(a.minutes_before for a in editor._collect_alarms()),
            [15, 7200, 10080],
        )

    def test_the_custom_units_are_all_expressible(self):
        for factor, name in EventEditor.CUSTOM_UNITS:
            with self.subTest(unit=name):
                self.assertIn(name.rstrip("s"), describe_offset(factor * 3))


if __name__ == "__main__":
    unittest.main()
