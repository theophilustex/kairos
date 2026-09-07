"""The SQLite cache in :mod:`kairos.storage`.

The interesting behaviour here is the offline story: a change made while
disconnected has to survive a later refresh from the server, or the user
silently loses work.
"""

import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from kairos.models import Alarm, Calendar, Event, local_timezone
from kairos.storage import PENDING_DELETE, PENDING_SAVE, Storage


def at(year, month, day, hour=0, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=local_timezone())


class StorageTestCase(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix="kairos-db-"))
        self.storage = Storage(self.directory / "cache.db")
        self.calendar = Calendar(id="cal", account_id="acct", name="Work", colour="#e01b24")
        self.storage.save_calendar(self.calendar)

    def tearDown(self):
        self.storage.close()

    def event(self, summary="Thing", start=None, hours=1, **kwargs):
        start = start or at(2026, 9, 5, 10)
        event = Event.new("cal", start, start + timedelta(hours=hours), summary)
        for key, value in kwargs.items():
            setattr(event, key, value)
        return event


class Calendars(StorageTestCase):
    def test_saved_calendar_comes_back(self):
        found = self.storage.get_calendar("cal")
        self.assertEqual(found.name, "Work")
        self.assertEqual(found.colour, "#e01b24")

    def test_saving_again_updates_rather_than_duplicates(self):
        self.calendar.name = "Renamed"
        self.storage.save_calendar(self.calendar)
        self.assertEqual(len(self.storage.list_calendars()), 1)
        self.assertEqual(self.storage.get_calendar("cal").name, "Renamed")

    def test_deleting_a_calendar_takes_its_events_with_it(self):
        self.storage.save_event(self.event())
        self.storage.delete_calendar("cal")
        self.assertIsNone(self.storage.get_calendar("cal"))
        self.assertEqual(self.storage.count_events("cal"), 0)

    def test_deleting_by_account_removes_every_calendar_of_that_account(self):
        self.storage.save_calendar(Calendar(id="cal2", account_id="acct", name="Second"))
        self.storage.save_calendar(Calendar(id="other", account_id="elsewhere", name="Other"))
        self.storage.delete_calendars_for_account("acct")
        self.assertEqual([c.id for c in self.storage.list_calendars()], ["other"])


class Events(StorageTestCase):
    def test_round_trip(self):
        original = self.event("Standup", alarms=[Alarm(10)], location="Room 3")
        self.storage.save_event(original)

        found = self.storage.get_event("cal", original.uid)
        self.assertEqual(found.summary, "Standup")
        self.assertEqual(found.location, "Room 3")
        self.assertEqual([a.minutes_before for a in found.alarms], [10])
        self.assertEqual(found.start, original.start)

    def test_range_query_finds_overlapping_events(self):
        self.storage.save_event(self.event("Inside", at(2026, 9, 5, 10)))
        self.storage.save_event(self.event("Before", at(2026, 1, 1, 10)))
        self.storage.save_event(self.event("After", at(2026, 12, 1, 10)))

        found = self.storage.events_in_range(["cal"], at(2026, 9, 5), at(2026, 9, 6))
        self.assertEqual([e.summary for e in found], ["Inside"])

    def test_recurring_events_are_always_returned(self):
        """Only the recurrence code can say whether a rule hits the window."""
        event = self.event("Weekly", at(2020, 1, 1, 10), rrule="FREQ=WEEKLY")
        self.storage.save_event(event)
        found = self.storage.events_in_range(["cal"], at(2026, 9, 5), at(2026, 9, 6))
        self.assertEqual([e.summary for e in found], ["Weekly"])

    def test_events_from_other_calendars_are_not_returned(self):
        self.storage.save_calendar(Calendar(id="cal2", account_id="acct", name="Other"))
        other = Event.new("cal2", at(2026, 9, 5, 10), at(2026, 9, 5, 11), "Elsewhere")
        self.storage.save_event(other)
        found = self.storage.events_in_range(["cal"], at(2026, 9, 5), at(2026, 9, 6))
        self.assertEqual(found, [])

    def test_saving_the_same_uid_updates_in_place(self):
        event = self.event("First")
        self.storage.save_event(event)
        self.storage.save_event(event.copy(summary="Second", raw_ics=""))
        self.assertEqual(self.storage.count_events("cal"), 1)
        self.assertEqual(self.storage.get_event("cal", event.uid).summary, "Second")

    def test_forget_removes_the_row(self):
        event = self.event()
        self.storage.save_event(event)
        self.storage.forget_event("cal", event.uid)
        self.assertIsNone(self.storage.get_event("cal", event.uid))

    def test_a_corrupt_row_still_yields_something_selectable(self):
        """A user must be able to see and delete an event we cannot parse."""
        event = self.event("Broken")
        self.storage.save_event(event)
        with self.storage._lock:
            self.storage._connection.execute(
                "UPDATE events SET ics = ? WHERE uid = ?", ("not iCalendar", event.uid)
            )
            self.storage._connection.commit()
        self.storage._parse_cache.clear()

        found = self.storage.get_event("cal", event.uid)
        self.assertIsNotNone(found)
        self.assertEqual(found.uid, event.uid)


class Search(StorageTestCase):
    def setUp(self):
        super().setUp()
        self.storage.save_event(self.event("Dentist appointment"))
        self.storage.save_event(self.event("Team standup", location="Room 3"))

    def test_matches_the_title(self):
        found = self.storage.search_events("dentist", ["cal"])
        self.assertEqual([e.summary for e in found], ["Dentist appointment"])

    def test_is_case_insensitive(self):
        self.assertEqual(len(self.storage.search_events("DENTIST", ["cal"])), 1)

    def test_matches_other_fields(self):
        self.assertEqual(len(self.storage.search_events("Room 3", ["cal"])), 1)

    def test_wildcards_are_treated_literally(self):
        """A "%" typed by the user must not match everything."""
        self.assertEqual(self.storage.search_events("%", ["cal"]), [])

    def test_empty_search_returns_nothing(self):
        self.assertEqual(self.storage.search_events("   ", ["cal"]), [])


class OfflineChanges(StorageTestCase):
    def test_a_pending_save_is_listed(self):
        event = self.event("Made offline")
        self.storage.save_event(event, pending=PENDING_SAVE)
        pending = self.storage.pending_changes()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0][1], PENDING_SAVE)

    def test_clearing_pending_takes_it_off_the_list(self):
        event = self.event()
        self.storage.save_event(event, pending=PENDING_SAVE)
        self.storage.clear_pending("cal", event.uid)
        self.assertEqual(self.storage.pending_changes(), [])

    def test_a_pending_delete_hides_the_event(self):
        event = self.event()
        self.storage.save_event(event)
        self.storage.mark_event_deleted("cal", event.uid)

        found = self.storage.events_in_range(["cal"], at(2026, 9, 5), at(2026, 9, 6))
        self.assertEqual(found, [])
        self.assertEqual(self.storage.pending_changes()[0][1], PENDING_DELETE)

    def test_a_refresh_does_not_discard_unpushed_work(self):
        """The heart of the offline story."""
        mine = self.event("My unsaved edit")
        self.storage.save_event(mine, pending=PENDING_SAVE)
        theirs = self.event("From the server", at(2026, 9, 5, 14))
        self.storage.save_event(theirs)

        # A full sync arrives that knows nothing about either.
        replacement = self.event("Server only", at(2026, 9, 5, 16))
        self.storage.replace_calendar_events("cal", [replacement])

        summaries = {e.summary for e in
                     self.storage.events_in_range(["cal"], at(2026, 9, 5), at(2026, 9, 6))}
        self.assertIn("My unsaved edit", summaries)     # kept
        self.assertIn("Server only", summaries)         # added
        self.assertNotIn("From the server", summaries)  # cleanly removed

    def test_a_refresh_does_not_resurrect_a_pending_delete(self):
        event = self.event("Deleted while offline")
        self.storage.save_event(event)
        self.storage.mark_event_deleted("cal", event.uid)

        # The server still has it, and sends it back.
        self.storage.replace_calendar_events("cal", [event])

        self.assertEqual(self.storage.pending_changes()[0][1], PENDING_DELETE)
        self.assertEqual(
            self.storage.events_in_range(["cal"], at(2026, 9, 5), at(2026, 9, 6)), []
        )

    def test_dirty_flag_is_visible_on_the_event(self):
        event = self.event()
        self.storage.save_event(event, pending=PENDING_SAVE)
        self.assertTrue(self.storage.get_event("cal", event.uid).dirty)
        self.storage.clear_pending("cal", event.uid)
        self.storage._parse_cache.clear()
        self.assertFalse(self.storage.get_event("cal", event.uid).dirty)


class Persistence(unittest.TestCase):
    def test_data_survives_reopening(self):
        directory = Path(tempfile.mkdtemp(prefix="kairos-db-"))
        path = directory / "cache.db"

        first = Storage(path)
        first.save_calendar(Calendar(id="cal", account_id="a", name="Kept"))
        event = Event.new("cal", at(2026, 9, 5, 10), at(2026, 9, 5, 11), "Kept event")
        first.save_event(event)
        first.close()

        second = Storage(path)
        self.assertEqual(second.get_calendar("cal").name, "Kept")
        self.assertEqual(second.get_event("cal", event.uid).summary, "Kept event")
        second.close()


if __name__ == "__main__":
    unittest.main()


class SearchIndex(StorageTestCase):
    """The full-text index, and that it never drifts from the events.

    Search is answered entirely from the index now, so an event the index
    has forgotten is an event that cannot be found. The index is maintained
    by SQL triggers rather than by Python precisely so that no write path can
    forget it — these check the ones that would have been easy to miss.
    """

    def summaries(self, term):
        return sorted(e.summary for e in self.storage.search_events(term, ["cal"]))

    def test_a_new_event_is_findable(self):
        self.storage.save_event(self.event("Dentist"))
        self.assertEqual(self.summaries("dentist"), ["Dentist"])

    def test_renaming_updates_the_index(self):
        event = self.event("Dentist")
        self.storage.save_event(event)
        self.storage.save_event(event.copy(summary="Optician", raw_ics=""))
        self.assertEqual(self.summaries("dentist"), [])
        self.assertEqual(self.summaries("optician"), ["Optician"])

    def test_changing_the_location_updates_the_index(self):
        event = self.event("Meeting", location="Room 3")
        self.storage.save_event(event)
        self.storage.save_event(event.copy(location="Boardroom", raw_ics=""))
        self.assertEqual(self.summaries("room"), [])
        self.assertEqual(self.summaries("boardroom"), ["Meeting"])

    def test_forgetting_an_event_removes_it_from_the_index(self):
        event = self.event("Dentist")
        self.storage.save_event(event)
        self.storage.forget_event("cal", event.uid)
        self.assertEqual(self.summaries("dentist"), [])

    def test_an_event_queued_for_deletion_is_not_found(self):
        event = self.event("Dentist")
        self.storage.save_event(event)
        self.storage.mark_event_deleted("cal", event.uid)
        self.assertEqual(self.summaries("dentist"), [])

    def test_a_full_refresh_keeps_the_index_correct(self):
        """replace_calendar_events deletes in bulk, straight through SQL."""
        gone = self.event("Old thing")
        self.storage.save_event(gone)
        self.storage.replace_calendar_events("cal", [self.event("New thing")])
        self.assertEqual(self.summaries("old"), [])
        self.assertEqual(self.summaries("new"), ["New thing"])

    def test_the_notes_are_searchable(self):
        self.storage.save_event(self.event("Review", description="bring the grid"))
        self.assertEqual(self.summaries("grid"), ["Review"])

    def test_icalendar_boilerplate_is_not_searchable(self):
        """The index holds three fields, not the whole document."""
        self.storage.save_event(self.event("Dentist"))
        for term in ("gregorian", "vcalendar", "dtstart", "prodid", "kairos"):
            with self.subTest(term=term):
                self.assertEqual(self.summaries(term), [])

    def test_results_are_not_silently_truncated(self):
        """The old scan gave up after SCAN_LIMIT rows without saying so."""
        for index in range(300):
            self.storage.save_event(self.event(f"Standup {index}"))
        self.assertEqual(len(self.storage.search_events("standup", ["cal"], limit=500)),
                         300)

    def test_another_calendar_is_not_searched(self):
        self.storage.save_calendar(
            Calendar(id="other", account_id="acct", name="Other", colour="#000000"))
        event = self.event("Dentist")
        event.calendar_id = "other"
        self.storage.save_event(event)
        self.assertEqual(self.summaries("dentist"), [])


class SearchIndexMigration(unittest.TestCase):
    """A cache written by a version with no location/description columns."""

    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix="kairos-migrate-"))
        self.path = self.directory / "cache.db"

    def test_an_older_cache_is_upgraded_and_indexed(self):
        from kairos import ical
        from kairos.models import Event, local_timezone

        # Build a database the old way: no location or description columns.
        connection = sqlite3.connect(self.path)
        connection.executescript("""
            CREATE TABLE events (
                calendar_id TEXT NOT NULL, uid TEXT NOT NULL, href TEXT,
                etag TEXT, ics TEXT NOT NULL, summary TEXT NOT NULL DEFAULT '',
                start_utc REAL NOT NULL, end_utc REAL NOT NULL,
                all_day INTEGER NOT NULL DEFAULT 0,
                recurring INTEGER NOT NULL DEFAULT 0,
                pending INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (calendar_id, uid));
        """)
        start = datetime.now(tz=local_timezone())
        event = Event.new("cal", start, start + timedelta(hours=1), "Dentist")
        event.location = "High Street"
        event.description = "bring the referral"
        connection.execute(
            "INSERT INTO events (calendar_id, uid, ics, summary, start_utc,"
            " end_utc, all_day, recurring, pending)"
            " VALUES (?, ?, ?, ?, ?, ?, 0, 0, 0)",
            ("cal", event.uid, ical.to_ical_text(event), event.summary,
             start.timestamp(), (start + timedelta(hours=1)).timestamp()))
        connection.commit()
        connection.close()

        storage = Storage(self.path)
        try:
            self.assertEqual(
                [e.summary for e in storage.search_events("dentist", ["cal"])],
                ["Dentist"])
            self.assertEqual(
                [e.summary for e in storage.search_events("high street", ["cal"])],
                ["Dentist"],
                "the location was not recovered from the stored iCalendar")
            self.assertEqual(
                [e.summary for e in storage.search_events("referral", ["cal"])],
                ["Dentist"])
        finally:
            storage.close()
