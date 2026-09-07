"""Server times must be shown on the user's own clock.

A calendar server almost always stores events in UTC. Displayed unconverted,
an evening event in the Americas lands on the following day, and — because its
UTC start and end straddle midnight — the views decide it spans two days and
draw it as an all-day banner. That is the bug these tests exist to prevent
coming back.
"""

import os
import time
import unittest
from datetime import date, datetime, timedelta

from kairos import ical, recurrence
from kairos.models import local_timezone, to_local


class InTimezone:
    """Run a test as if the machine were somewhere else."""

    def __init__(self, name: str):
        self.name = name
        self.previous = os.environ.get("TZ")

    def __enter__(self):
        os.environ["TZ"] = self.name
        time.tzset()
        return self

    def __exit__(self, *exc):
        if self.previous is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self.previous
        time.tzset()


def utc_event(summary: str, start: str, end: str, rrule: str = "") -> str:
    rule = f"RRULE:{rrule}\r\n" if rrule else ""
    return (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Server//EN\r\n"
        f"BEGIN:VEVENT\r\nUID:{summary}\r\nDTSTAMP:20260101T000000Z\r\n"
        f"DTSTART:{start}\r\nDTEND:{end}\r\n{rule}"
        f"SUMMARY:{summary}\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    )


def parse(text: str):
    event = ical.parse_calendar_text(text, "cal")[0]
    event.raw_ics = text
    return event


class EveningEventStoredInUTC(unittest.TestCase):
    """19:00-21:00 in New York is 23:00-01:00 UTC, i.e. across midnight."""

    def setUp(self):
        self.tz = InTimezone("America/New_York")
        self.tz.__enter__()
        self.event = parse(utc_event("Evening", "20260907T230000Z", "20260908T010000Z"))

    def tearDown(self):
        self.tz.__exit__()

    def expand(self):
        start = datetime(2026, 9, 1, tzinfo=local_timezone())
        return recurrence.expand([self.event], start, start + timedelta(days=30))

    def test_the_occurrence_is_in_local_time(self):
        occurrence = self.expand()[0]
        self.assertEqual(occurrence.start.hour, 19)
        self.assertEqual(occurrence.end.hour, 21)

    def test_it_is_not_mistaken_for_an_all_day_banner(self):
        occurrence = self.expand()[0]
        is_banner = occurrence.all_day or occurrence.first_day != occurrence.last_day
        self.assertFalse(is_banner, "a two-hour evening event drew as all-day")

    def test_it_lands_on_one_day_only(self):
        occurrences = self.expand()
        buckets = recurrence.group_by_day(occurrences, date(2026, 9, 7), 7)
        days = [day for day, items in buckets.items() if items]
        self.assertEqual(days, [date(2026, 9, 7)])

    def test_the_week_view_puts_it_at_the_right_hour(self):
        from kairos.ui.week_view import row_for, ROWS_PER_HOUR
        occurrence = self.expand()[0]
        row = row_for(occurrence.start, occurrence.first_day)
        self.assertEqual(row // ROWS_PER_HOUR, 19)


class RepeatingEveningEvent(unittest.TestCase):
    """The same, for a weekly series — how the bug was first noticed."""

    def setUp(self):
        self.tz = InTimezone("America/New_York")
        self.tz.__enter__()
        self.event = parse(utc_event(
            "Weekly", "20260907T230000Z", "20260908T010000Z", "FREQ=WEEKLY;BYDAY=MO"
        ))

    def tearDown(self):
        self.tz.__exit__()

    def test_no_occurrence_looks_like_an_all_day_event(self):
        start = datetime(2026, 9, 1, tzinfo=local_timezone())
        occurrences = recurrence.expand([self.event], start, start + timedelta(days=60))
        self.assertGreaterEqual(len(occurrences), 4)
        for occurrence in occurrences:
            with self.subTest(start=occurrence.start):
                self.assertFalse(occurrence.all_day)
                self.assertEqual(occurrence.first_day, occurrence.last_day)
                self.assertEqual(occurrence.start.hour, 19)


class AllDayEventsStayWhereTheyAre(unittest.TestCase):
    """Converting timed events must not disturb genuine all-day ones."""

    def test_an_all_day_event_keeps_its_dates(self):
        with InTimezone("America/New_York"):
            text = (
                "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//S//EN\r\n"
                "BEGIN:VEVENT\r\nUID:holiday\r\nDTSTAMP:20260101T000000Z\r\n"
                "DTSTART;VALUE=DATE:20260907\r\nDTEND;VALUE=DATE:20260910\r\n"
                "SUMMARY:Holiday\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
            )
            event = parse(text)
            start = datetime(2026, 9, 1, tzinfo=local_timezone())
            occurrence = recurrence.expand([event], start, start + timedelta(days=30))[0]

            self.assertTrue(occurrence.all_day)
            self.assertEqual(occurrence.first_day, date(2026, 9, 7))
            self.assertEqual(occurrence.last_day, date(2026, 9, 9))


class AcrossSeveralZones(unittest.TestCase):
    """The same UTC event should read correctly wherever you are."""

    def test_a_noon_utc_meeting_shows_at_the_right_local_hour(self):
        expected = {
            "UTC": 12,
            "America/New_York": 8,      # UTC-4 in September
            "Europe/Berlin": 14,        # UTC+2
            "Asia/Tokyo": 21,           # UTC+9
        }
        for zone, hour in expected.items():
            with self.subTest(zone=zone), InTimezone(zone):
                event = parse(utc_event("Noon", "20260907T120000Z", "20260907T130000Z"))
                start = datetime(2026, 9, 1, tzinfo=local_timezone())
                occurrence = recurrence.expand([event], start, start + timedelta(days=10))[0]
                self.assertEqual(occurrence.start.hour, hour)

    def test_editing_a_server_event_does_not_move_it(self):
        """Round-tripping through the editor must preserve the instant."""
        from kairos.ui.event_editor import EventEditor
        from kairos.models import Calendar

        with InTimezone("America/New_York"):
            event = parse(utc_event("Evening", "20260907T230000Z", "20260908T010000Z"))
            calendars = [Calendar(id="cal", account_id="a", name="Test")]
            editor = EventEditor(calendars, event=event)
            rebuilt = editor.build_event()

            self.assertEqual(to_local(rebuilt.start), to_local(event.start))
            self.assertEqual(to_local(rebuilt.end), to_local(event.end))


if __name__ == "__main__":
    unittest.main()


class TheSystemZone(unittest.TestCase):
    """``local_timezone`` must be a real zone, not a fixed offset.

    It used to return ``datetime.now().astimezone().tzinfo``, which is a
    snapshot: a fixed offset named for whichever DST period happens to be in
    force. Two things broke.

    A weekly 9am meeting written against a summer offset kept that offset all
    winter, so the instant Kairos reminded you at moved by an hour once the
    clocks changed. And the offset's *name* went onto the wire as
    ``TZID=EDT``, which no server can resolve and which Radicale rejects with
    HTTP 400.
    """

    def setUp(self):
        import kairos.models
        kairos.models._ZONE_CACHE = None
        self._old_tz = os.environ.get("TZ")

    def tearDown(self):
        import kairos.models
        kairos.models._ZONE_CACHE = None
        if self._old_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self._old_tz

    def zone(self, name):
        os.environ["TZ"] = name
        import kairos.models
        kairos.models._ZONE_CACHE = None
        return kairos.models.local_timezone()

    def test_it_is_a_named_zone(self):
        import zoneinfo
        self.assertIsInstance(self.zone("Europe/Lisbon"), zoneinfo.ZoneInfo)

    def test_it_honours_the_TZ_variable(self):
        self.assertEqual(str(self.zone("Asia/Tokyo")), "Asia/Tokyo")

    def test_an_unknown_zone_falls_back_rather_than_raising(self):
        """Better a fixed offset than refusing to start."""
        self.assertIsNotNone(self.zone("Not/ARealPlace"))

    def test_the_written_tzid_is_resolvable(self):
        """TZID=EDT is not a timezone; TZID=America/New_York is."""
        import zoneinfo
        from kairos import ical
        from kairos.models import Event
        tz = self.zone("America/New_York")
        start = datetime(2026, 10, 20, 9, tzinfo=tz)
        event = Event.new("cal", start, start + timedelta(minutes=30), "Standup")
        line = next(l for l in ical.to_ical_text(event).splitlines()
                    if l.startswith("DTSTART"))
        self.assertIn("TZID=", line)
        name = line.split("TZID=")[1].split(":")[0]
        zoneinfo.ZoneInfo(name)          # raises if the server could not either

    def test_a_weekly_event_keeps_its_wall_clock_across_dst(self):
        from kairos import ical, recurrence
        from kairos.models import Event
        tz = self.zone("America/New_York")
        start = datetime(2026, 10, 20, 9, tzinfo=tz)
        event = Event.new("cal", start, start + timedelta(minutes=30), "Standup")
        event.rrule = "FREQ=WEEKLY"
        event.raw_ics = ical.to_ical_text(event)

        found = recurrence.expand(
            [event], datetime(2026, 10, 19, tzinfo=tz),
            datetime(2026, 11, 30, tzinfo=tz))
        self.assertTrue(found)
        self.assertEqual({o.start.hour for o in found}, {9},
                         "the meeting drifted off 9am across the DST change")

    def test_the_instant_shifts_across_dst(self):
        """9am local is a different instant either side of the change."""
        from datetime import timezone
        from kairos import ical, recurrence
        from kairos.models import Event
        tz = self.zone("America/New_York")
        start = datetime(2026, 10, 20, 9, tzinfo=tz)
        event = Event.new("cal", start, start + timedelta(minutes=30), "Standup")
        event.rrule = "FREQ=WEEKLY"
        event.raw_ics = ical.to_ical_text(event)

        found = recurrence.expand(
            [event], datetime(2026, 10, 19, tzinfo=tz),
            datetime(2026, 11, 30, tzinfo=tz))
        in_utc = {o.start.astimezone(timezone.utc).hour for o in found}
        self.assertEqual(in_utc, {13, 14},
                         "every instance sat at the same UTC hour, so the "
                         "fixed offset is back")
