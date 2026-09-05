"""The SQLite cache in :mod:`kairos.storage`.

The interesting behaviour here is the offline story: a change made while
disconnected has to survive a later refresh from the server, or the user
silently loses work.
"""

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
