"""Expanding repeating events, and bucketing them into days."""

import unittest
from datetime import date, datetime, timedelta

from kairos import ical, recurrence
from kairos.models import Event, local_timezone, start_of_day


def at(year, month, day, hour=0, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=local_timezone())


def make(summary, start, end, rrule="", all_day=False):
    event = Event.new("cal", start, end, summary, all_day=all_day)
    event.rrule = rrule
    return event


class SimpleEvents(unittest.TestCase):
    def test_event_inside_the_window_appears_once(self):
        event = make("One", at(2026, 9, 5, 10), at(2026, 9, 5, 11))
        found = recurrence.expand([event], at(2026, 9, 5), at(2026, 9, 6))
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].start, event.start)

    def test_event_outside_the_window_is_dropped(self):
        event = make("Away", at(2026, 1, 1, 10), at(2026, 1, 1, 11))
        self.assertEqual(recurrence.expand([event], at(2026, 9, 5), at(2026, 9, 6)), [])

    def test_event_overlapping_the_edge_is_kept(self):
        event = make("Spans", at(2026, 9, 4, 23), at(2026, 9, 5, 1))
        self.assertEqual(len(recurrence.expand([event], at(2026, 9, 5), at(2026, 9, 6))), 1)

    def test_zero_length_event_is_kept(self):
        event = make("Instant", at(2026, 9, 5, 10), at(2026, 9, 5, 10))
        self.assertEqual(len(recurrence.expand([event], at(2026, 9, 5), at(2026, 9, 6))), 1)


class RepeatingEvents(unittest.TestCase):
    def expand(self, rrule, days=28):
        event = make("Repeat", at(2026, 9, 7, 9), at(2026, 9, 7, 10), rrule)
        # raw_ics is what expansion actually reads, so build it the real way.
        event.raw_ics = ical.to_ical_text(event)
        return recurrence.expand([event], at(2026, 9, 7), at(2026, 9, 7) + timedelta(days=days))

    def test_daily(self):
        self.assertEqual(len(self.expand("FREQ=DAILY", days=7)), 7)

    def test_weekly(self):
        self.assertEqual(len(self.expand("FREQ=WEEKLY", days=28)), 4)

    def test_count_is_respected(self):
        self.assertEqual(len(self.expand("FREQ=DAILY;COUNT=3", days=28)), 3)

    def test_interval_is_respected(self):
        self.assertEqual(len(self.expand("FREQ=DAILY;INTERVAL=2", days=10)), 5)

    def test_byday_picks_the_right_weekdays(self):
        found = self.expand("FREQ=WEEKLY;BYDAY=MO,WE,FR", days=7)
        self.assertEqual(sorted(o.start.weekday() for o in found), [0, 2, 4])

    def test_until_stops_the_series(self):
        found = self.expand("FREQ=DAILY;UNTIL=20260910T235959Z", days=28)
        self.assertEqual(len(found), 4)          # 7th, 8th, 9th, 10th

    def test_occurrences_keep_the_event_duration(self):
        for occurrence in self.expand("FREQ=DAILY", days=5):
            self.assertEqual(occurrence.end - occurrence.start, timedelta(hours=1))

    def test_exdate_removes_an_occurrence(self):
        text = (
            "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//EN\r\n"
            "BEGIN:VEVENT\r\nUID:x\r\nDTSTART:20260907T090000Z\r\nDTEND:20260907T100000Z\r\n"
            "RRULE:FREQ=DAILY;COUNT=5\r\nEXDATE:20260909T090000Z\r\n"
            "SUMMARY:Skips one\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
        )
        event = ical.parse_calendar_text(text, "cal")[0]
        event.raw_ics = text
        found = recurrence.expand([event], at(2026, 9, 1), at(2026, 9, 30))
        self.assertEqual(len(found), 4)

    def test_a_broken_rule_still_shows_the_event(self):
        """A malformed RRULE must not make an event vanish."""
        event = make("Broken", at(2026, 9, 7, 9), at(2026, 9, 7, 10), "FREQ=NONSENSE")
        found = recurrence.expand([event], at(2026, 9, 1), at(2026, 9, 30))
        self.assertEqual(len(found), 1)


class Grouping(unittest.TestCase):
    def test_multi_day_event_appears_on_every_day_it_covers(self):
        start = start_of_day(date(2026, 9, 8))
        event = make("Trip", start, start + timedelta(days=3), all_day=True)
        found = recurrence.expand([event], start_of_day(date(2026, 9, 7)),
                                  start_of_day(date(2026, 9, 14)))
        buckets = recurrence.group_by_day(found, date(2026, 9, 7), 7)

        self.assertEqual(len(buckets[date(2026, 9, 7)]), 0)
        for day in (8, 9, 10):
            self.assertEqual(len(buckets[date(2026, 9, day)]), 1, f"day {day}")
        self.assertEqual(len(buckets[date(2026, 9, 11)]), 0)

    def test_every_day_in_the_range_gets_a_bucket(self):
        buckets = recurrence.group_by_day([], date(2026, 9, 1), 30)
        self.assertEqual(len(buckets), 30)

    def test_banners_sort_before_timed_events(self):
        day = start_of_day(date(2026, 9, 8))
        banner = make("All day", day, day + timedelta(days=1), all_day=True)
        timed = make("At nine", at(2026, 9, 8, 9), at(2026, 9, 8, 10))
        found = recurrence.expand([timed, banner], day, day + timedelta(days=1))
        self.assertEqual([o.summary for o in found], ["All day", "At nine"])

    def test_timed_events_sort_by_start(self):
        day = start_of_day(date(2026, 9, 8))
        events = [
            make("Third", at(2026, 9, 8, 15), at(2026, 9, 8, 16)),
            make("First", at(2026, 9, 8, 9), at(2026, 9, 8, 10)),
            make("Second", at(2026, 9, 8, 11), at(2026, 9, 8, 12)),
        ]
        found = recurrence.expand(events, day, day + timedelta(days=1))
        self.assertEqual([o.summary for o in found], ["First", "Second", "Third"])


class NextOccurrence(unittest.TestCase):
    def test_finds_the_next_instance_of_a_series(self):
        event = make("Weekly", at(2026, 9, 7, 9), at(2026, 9, 7, 10), "FREQ=WEEKLY")
        event.raw_ics = ical.to_ical_text(event)
        found = recurrence.next_occurrence_after(event, at(2026, 9, 8))
        self.assertIsNotNone(found)
        self.assertEqual(found.start, at(2026, 9, 14, 9))

    def test_returns_none_when_the_series_has_ended(self):
        event = make("Done", at(2026, 9, 7, 9), at(2026, 9, 7, 10), "FREQ=DAILY;COUNT=2")
        event.raw_ics = ical.to_ical_text(event)
        self.assertIsNone(recurrence.next_occurrence_after(event, at(2026, 10, 1)))


if __name__ == "__main__":
    unittest.main()


SERIES_WITH_OVERRIDE = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Kairos tests//EN
BEGIN:VEVENT
UID:weekly-1
DTSTAMP:20260901T090000Z
DTSTART;TZID=UTC:20260901T090000
DTEND;TZID=UTC:20260901T093000
SUMMARY:Standup
LOCATION:Room 1
RRULE:FREQ=WEEKLY
BEGIN:VALARM
ACTION:DISPLAY
DESCRIPTION:Reminder
TRIGGER:-PT10M
END:VALARM
END:VEVENT
BEGIN:VEVENT
UID:weekly-1
RECURRENCE-ID;TZID=UTC:20260908T090000
DTSTAMP:20260901T090000Z
DTSTART;TZID=UTC:20260908T150000
DTEND;TZID=UTC:20260908T153000
SUMMARY:Standup (moved)
LOCATION:Room 9
BEGIN:VALARM
ACTION:DISPLAY
DESCRIPTION:Reminder
TRIGGER:-PT45M
END:VALARM
END:VEVENT
END:VCALENDAR
"""


class ModifiedInstances(unittest.TestCase):
    """A single occurrence changed in another client — RECURRENCE-ID.

    ``recurring-ical-events`` applies the override's *times* by itself, so
    those were always right. The occurrence's text was not: it is read from
    the event object, and every instance pointed at the master, so a meeting
    moved and renamed for one week appeared at the new time under the old
    name. These pin the whole override.
    """

    def setUp(self):
        self.events = ical.parse_calendar_text(SERIES_WITH_OVERRIDE, "cal")
        for event in self.events:
            event.raw_ics = SERIES_WITH_OVERRIDE
        self.found = recurrence.expand(
            self.events, at(2026, 9, 1), at(2026, 9, 22))

    def instance_on(self, day):
        matches = [o for o in self.found if o.start.date() == day]
        self.assertEqual(len(matches), 1, f"expected one occurrence on {day}")
        return matches[0]

    def moved(self):
        """The overridden instance, whichever local day it landed on."""
        matches = [o for o in self.found if "moved" in o.summary]
        self.assertEqual(len(matches), 1, "the overridden instance is missing")
        return matches[0]

    def test_only_the_master_becomes_an_event(self):
        """The override is part of the series, not a second entry."""
        self.assertEqual(len(self.events), 1)

    def test_the_series_still_produces_three_instances(self):
        self.assertEqual(len(self.found), 3)

    def test_the_override_keeps_its_own_summary(self):
        self.assertEqual(self.moved().summary, "Standup (moved)")

    def test_the_override_keeps_its_own_location(self):
        self.assertEqual(self.moved().event.location, "Room 9")

    def test_the_override_keeps_its_own_reminder(self):
        self.assertEqual([a.minutes_before for a in self.moved().event.alarms], [45])

    def test_the_override_moves_to_its_new_time(self):
        """15:00 UTC, shown on the user's clock like every other occurrence."""
        from datetime import timezone
        expected = datetime(2026, 9, 8, 15, tzinfo=timezone.utc).astimezone(
            local_timezone())
        self.assertEqual(self.moved().start, expected)

    def test_the_unmoved_instances_keep_the_series_time(self):
        from datetime import timezone
        expected = datetime(2026, 9, 1, 9, tzinfo=timezone.utc).astimezone(
            local_timezone())
        self.assertEqual(self.instance_on(expected.date()).start, expected)

    def test_the_other_instances_keep_the_series_summary(self):
        others = [o for o in self.found if "moved" not in o.summary]
        self.assertEqual({o.summary for o in others}, {"Standup"})

    def test_the_other_instances_keep_the_series_location(self):
        others = [o for o in self.found if "moved" not in o.summary]
        self.assertEqual({o.event.location for o in others}, {"Room 1"})

    def test_the_other_instances_keep_the_series_reminder(self):
        others = [o for o in self.found if "moved" not in o.summary]
        for occurrence in others:
            self.assertEqual([a.minutes_before for a in occurrence.event.alarms], [10])

    def test_the_master_event_is_not_mutated(self):
        """The override must not leak into the series it came from."""
        self.assertEqual(self.events[0].summary, "Standup")
        self.assertEqual(self.events[0].location, "Room 1")

    def test_every_instance_still_refers_to_the_same_resource(self):
        """Editing or deleting one must still find the right thing on the server."""
        self.assertEqual({o.uid for o in self.found}, {"weekly-1"})


class UnmodifiedSeries(unittest.TestCase):
    """The common case must not pay for the override handling."""

    def test_instances_share_the_master_event_object(self):
        event = make("Daily", at(2026, 9, 1, 9), at(2026, 9, 1, 10), "FREQ=DAILY")
        found = recurrence.expand([event], at(2026, 9, 1), at(2026, 9, 5))
        self.assertTrue(found)
        for occurrence in found:
            self.assertIs(occurrence.event, event)
