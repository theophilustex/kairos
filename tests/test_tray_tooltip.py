"""What the tray icon's tooltip says, and that the tray is told when it changes."""

import unittest
from datetime import datetime, timedelta

from kairos.config import settings
from kairos.formatting import tray_summary
from kairos.models import Event, Occurrence, local_timezone, start_of_day

NOW = datetime(2026, 9, 15, 10, 35, tzinfo=local_timezone())   # a Tuesday


def occ(summary, start, hours=1.0, all_day=False):
    end = start + timedelta(hours=hours)
    return Occurrence(event=Event.new("cal", start, end, summary, all_day=all_day),
                      start=start, end=end)


def at(hour, minute=0, days=0):
    return start_of_day(NOW.date() + timedelta(days=days)) + timedelta(hours=hour, minutes=minute)


class TheWords(unittest.TestCase):
    def setUp(self):
        settings.set("time_format", "24h")

    def test_nothing_coming_up(self):
        self.assertEqual(tray_summary([], NOW), "Nothing coming up in the next week")

    def test_something_on_now(self):
        self.assertEqual(tray_summary([occ("Standup", at(10), 1)], NOW),
                         "Now: Standup, until 11:00")

    def test_next_today_counts_down(self):
        self.assertEqual(tray_summary([occ("Dentist", at(11))], NOW),
                         "Next: Dentist at 11:00 · in 25 min")

    def test_longer_waits_in_hours(self):
        self.assertEqual(tray_summary([occ("Review", at(13, 5))], NOW),
                         "Next: Review at 13:05 · in 2 h 30 min")

    def test_tomorrow(self):
        self.assertEqual(tray_summary([occ("Gym", at(7, days=1))], NOW),
                         "Next: Gym tomorrow at 07:00")

    def test_later_in_the_week_by_day_name(self):
        self.assertEqual(tray_summary([occ("Choir", at(19, days=3))], NOW),
                         "Next: Choir on Friday at 19:00")

    def test_now_and_next_together(self):
        text = tray_summary([occ("Standup", at(10)), occ("Dentist", at(11))], NOW)
        self.assertEqual(text.splitlines(), ["Now: Standup, until 11:00",
                                             "Next: Dentist at 11:00 · in 25 min"])

    def test_all_day_events_do_not_crowd_out_what_is_on(self):
        text = tray_summary([occ("Holiday", start_of_day(NOW.date()), 24, all_day=True),
                             occ("Dentist", at(11))], NOW)
        self.assertEqual(text, "Next: Dentist at 11:00 · in 25 min")

    def test_what_has_finished_is_ignored(self):
        self.assertEqual(tray_summary([occ("Earlier", at(8))], NOW),
                         "Nothing coming up in the next week")

    def test_an_untitled_event(self):
        self.assertIn("(No title)", tray_summary([occ("", at(11))], NOW))


class TellingTheTray(unittest.TestCase):
    def tray(self):
        from kairos.tray import TrayIcon
        return TrayIcon(icon_name="org.kairos.Calendar", title="Kairos",
                        tooltip="Calendar and reminders", icon_theme_path="",
                        icon_files=[], on_activate=lambda: None, items=[])

    def test_it_keeps_the_new_text_for_when_it_is_asked(self):
        tray = self.tray()
        tray.set_tooltip("Next: Dentist at 11:00")
        self.assertEqual(tray._on_item_property(None, None, None, None, "ToolTip").unpack()[3],
                         "Next: Dentist at 11:00")

    def test_it_announces_the_change_once_registered(self):
        tray = self.tray()
        signals = []

        class Connection:
            def emit_signal(self, dest, path, interface, name, params):
                signals.append(name)

        tray._connection, tray._item_registration = Connection(), 1
        tray.set_tooltip("Next: Dentist")
        self.assertEqual(signals, ["NewToolTip"])

    def test_the_same_text_again_is_not_announced(self):
        tray = self.tray()
        signals = []

        class Connection:
            def emit_signal(self, *args):
                signals.append(args)

        tray._connection, tray._item_registration = Connection(), 1
        tray.set_tooltip("Calendar and reminders")
        self.assertEqual(signals, [])


if __name__ == "__main__":
    unittest.main()
