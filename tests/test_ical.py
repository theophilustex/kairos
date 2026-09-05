"""Reading and writing iCalendar.

These are the tests worth having: the conversion in :mod:`kairos.ical` is
where a subtle mistake turns into an event on the wrong day, and the all-day
exclusive-end rule is exactly the sort of thing that gets broken by a
well-meaning edit.
"""

import unittest
from datetime import datetime, timedelta

from kairos import ical
from kairos.models import Alarm, Event, local_timezone, start_of_day
from datetime import date


def at(year, month, day, hour=0, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=local_timezone())


class TimedEventRoundTrip(unittest.TestCase):
    def setUp(self):
        self.event = Event.new(
            "cal", at(2026, 9, 5, 14, 30), at(2026, 9, 5, 15, 45), "Review"
        )
        self.event.location = "Room 3"
        self.event.description = "First line\nSecond line"
        self.event.alarms = [Alarm(15), Alarm(1440)]

    def round_trip(self) -> Event:
        text = ical.to_ical_text(self.event)
        parsed = ical.parse_calendar_text(text, "cal")
        self.assertEqual(len(parsed), 1)
        return parsed[0]

    def test_times_survive(self):
        back = self.round_trip()
        self.assertEqual(back.start, self.event.start)
        self.assertEqual(back.end, self.event.end)
        self.assertFalse(back.all_day)

    def test_text_fields_survive(self):
        back = self.round_trip()
        self.assertEqual(back.summary, "Review")
        self.assertEqual(back.location, "Room 3")
        self.assertEqual(back.description, "First line\nSecond line")

    def test_uid_is_preserved(self):
        self.assertEqual(self.round_trip().uid, self.event.uid)

    def test_alarms_survive(self):
        back = self.round_trip()
        self.assertEqual(sorted(a.minutes_before for a in back.alarms), [15, 1440])


class AllDayEvents(unittest.TestCase):
    """The end of an all-day event is exclusive, in iCalendar and in Kairos."""

    def test_three_day_event_keeps_its_span(self):
        start = start_of_day(date(2026, 3, 5))
        event = Event.new("cal", start, start + timedelta(days=3), "Trip", all_day=True)

        back = ical.parse_calendar_text(ical.to_ical_text(event), "cal")[0]

        self.assertTrue(back.all_day)
        self.assertEqual(back.start, start)
        self.assertEqual(back.end, start + timedelta(days=3))
        # Inclusive last day is what the views draw.
        self.assertEqual(back.last_day, date(2026, 3, 7))
        self.assertEqual(back.spans_days(), 3)

    def test_written_as_dates_not_datetimes(self):
        start = start_of_day(date(2026, 3, 5))
        event = Event.new("cal", start, start + timedelta(days=1), "Holiday", all_day=True)
        text = ical.to_ical_text(event)
        self.assertIn("DTSTART;VALUE=DATE:20260305", text)
        self.assertIn("DTEND;VALUE=DATE:20260306", text)

    def test_single_day_is_one_day_long(self):
        start = start_of_day(date(2026, 3, 5))
        event = Event.new("cal", start, start + timedelta(days=1), "Holiday", all_day=True)
        back = ical.parse_calendar_text(ical.to_ical_text(event), "cal")[0]
        self.assertEqual(back.first_day, back.last_day)


class AwkwardServerData(unittest.TestCase):
    """Things real servers send that must not crash us."""

    def parse(self, body: str):
        text = ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\n"
                + body + "END:VCALENDAR\r\n")
        return ical.parse_calendar_text(text, "cal")

    def test_missing_dtend_uses_duration(self):
        events = self.parse(
            "BEGIN:VEVENT\r\nUID:a\r\nDTSTART:20260905T140000Z\r\n"
            "DURATION:PT90M\r\nSUMMARY:Has duration\r\nEND:VEVENT\r\n"
        )
        self.assertEqual(events[0].duration, timedelta(minutes=90))

    def test_missing_dtend_and_duration_is_zero_length(self):
        events = self.parse(
            "BEGIN:VEVENT\r\nUID:b\r\nDTSTART:20260905T140000Z\r\n"
            "SUMMARY:Instant\r\nEND:VEVENT\r\n"
        )
        self.assertEqual(events[0].start, events[0].end)

    def test_floating_time_is_treated_as_local(self):
        events = self.parse(
            "BEGIN:VEVENT\r\nUID:c\r\nDTSTART:20260905T140000\r\n"
            "DTEND:20260905T150000\r\nSUMMARY:Floating\r\nEND:VEVENT\r\n"
        )
        self.assertEqual(events[0].start, at(2026, 9, 5, 14, 0))

    def test_end_before_start_is_repaired(self):
        events = self.parse(
            "BEGIN:VEVENT\r\nUID:d\r\nDTSTART:20260905T140000Z\r\n"
            "DTEND:20260905T120000Z\r\nSUMMARY:Backwards\r\nEND:VEVENT\r\n"
        )
        self.assertGreaterEqual(events[0].end, events[0].start)

    def test_event_with_no_dtstart_is_skipped_not_fatal(self):
        events = self.parse(
            "BEGIN:VEVENT\r\nUID:e\r\nSUMMARY:No start\r\nEND:VEVENT\r\n"
            "BEGIN:VEVENT\r\nUID:f\r\nDTSTART:20260905T140000Z\r\n"
            "SUMMARY:Fine\r\nEND:VEVENT\r\n"
        )
        self.assertEqual([e.summary for e in events], ["Fine"])

    def test_recurrence_overrides_are_ignored(self):
        """A modified instance must not appear as a second event."""
        events = self.parse(
            "BEGIN:VEVENT\r\nUID:g\r\nDTSTART:20260905T140000Z\r\n"
            "DTEND:20260905T150000Z\r\nRRULE:FREQ=DAILY\r\nSUMMARY:Series\r\nEND:VEVENT\r\n"
            "BEGIN:VEVENT\r\nUID:g\r\nRECURRENCE-ID:20260906T140000Z\r\n"
            "DTSTART:20260906T160000Z\r\nDTEND:20260906T170000Z\r\n"
            "SUMMARY:Moved\r\nEND:VEVENT\r\n"
        )
        self.assertEqual([e.summary for e in events], ["Series"])

    def test_control_characters_are_stripped(self):
        events = self.parse(
            "BEGIN:VEVENT\r\nUID:h\r\nDTSTART:20260905T140000Z\r\n"
            "SUMMARY:Bad\\x07title\r\nEND:VEVENT\r\n"
        )
        self.assertNotIn("\x07", events[0].summary)

    def test_unparseable_document_raises_parse_error(self):
        with self.assertRaises(ical.ParseError):
            ical.parse_calendar_text("this is not iCalendar at all", "cal")


class AlarmTriggers(unittest.TestCase):
    """Every VALARM shape gets normalised to "minutes before the start"."""

    def parse_alarm(self, trigger_line: str):
        text = (
            "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\n"
            "BEGIN:VEVENT\r\nUID:x\r\nDTSTART:20260905T140000Z\r\n"
            "DTEND:20260905T150000Z\r\nSUMMARY:With alarm\r\n"
            "BEGIN:VALARM\r\nACTION:DISPLAY\r\nDESCRIPTION:Ping\r\n"
            + trigger_line + "\r\nEND:VALARM\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
        )
        return ical.parse_calendar_text(text, "cal")[0].alarms

    def test_relative_to_start(self):
        self.assertEqual(self.parse_alarm("TRIGGER:-PT15M")[0].minutes_before, 15)

    def test_at_start(self):
        self.assertEqual(self.parse_alarm("TRIGGER:PT0S")[0].minutes_before, 0)

    def test_relative_to_end(self):
        # Event is an hour long; 15 minutes before the end is 45 after the start.
        alarms = self.parse_alarm("TRIGGER;RELATED=END:-PT15M")
        self.assertEqual(alarms[0].minutes_before, -45)

    def test_absolute_trigger(self):
        alarms = self.parse_alarm("TRIGGER;VALUE=DATE-TIME:20260905T133000Z")
        self.assertEqual(alarms[0].minutes_before, 30)

    def test_email_alarms_are_left_to_the_server(self):
        text = (
            "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//EN\r\n"
            "BEGIN:VEVENT\r\nUID:y\r\nDTSTART:20260905T140000Z\r\n"
            "BEGIN:VALARM\r\nACTION:EMAIL\r\nTRIGGER:-PT30M\r\n"
            "DESCRIPTION:d\r\nSUMMARY:s\r\nEND:VALARM\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
        )
        self.assertEqual(ical.parse_calendar_text(text, "cal")[0].alarms, [])

    def test_duplicate_alarms_are_collapsed(self):
        text = (
            "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//EN\r\n"
            "BEGIN:VEVENT\r\nUID:z\r\nDTSTART:20260905T140000Z\r\n"
            "BEGIN:VALARM\r\nACTION:DISPLAY\r\nTRIGGER:-PT10M\r\nDESCRIPTION:a\r\nEND:VALARM\r\n"
            "BEGIN:VALARM\r\nACTION:DISPLAY\r\nTRIGGER:-PT10M\r\nDESCRIPTION:b\r\nEND:VALARM\r\n"
            "END:VEVENT\r\nEND:VCALENDAR\r\n"
        )
        self.assertEqual(len(ical.parse_calendar_text(text, "cal")[0].alarms), 1)


class RepeatDescriptions(unittest.TestCase):
    def test_known_presets(self):
        self.assertEqual(ical.describe_rrule(""), "Does not repeat")
        self.assertEqual(ical.describe_rrule("FREQ=WEEKLY"), "Every week")
        self.assertEqual(ical.describe_rrule("FREQ=WEEKLY;INTERVAL=2"), "Every 2 weeks")

    def test_derived_descriptions(self):
        self.assertEqual(ical.describe_rrule("FREQ=DAILY;INTERVAL=3"), "Every 3 days")
        self.assertIn("5 times", ical.describe_rrule("FREQ=DAILY;COUNT=5"))

    def test_nonsense_is_described_rather_than_crashing(self):
        self.assertEqual(ical.describe_rrule("FREQ=NONSENSE"), "Custom repeat")
        self.assertEqual(ical.describe_rrule("not a rule"), "Custom repeat")


if __name__ == "__main__":
    unittest.main()
