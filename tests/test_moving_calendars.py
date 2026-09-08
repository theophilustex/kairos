"""Changing which calendar an event is in.

In CalDAV an event *is* a resource inside a collection, so this is not an
edit: it is a create in the new collection and a delete from the old. Saving
into the new one and leaving the old resource alone left the event visible
twice, which is what was reported.
"""

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from kairos import recurrence
from kairos.accounts import AccountStore
from kairos.config import settings
from kairos.models import Event, local_timezone
from kairos.storage import Storage
from kairos.sync import SyncManager


class WithTwoCalendars(unittest.TestCase):
    def setUp(self):
        settings.set("allow_deleting_events", True)
        self.directory = Path(tempfile.mkdtemp(prefix="kairos-move-"))
        self.sync = SyncManager(
            store=Storage(self.directory / "cache.db"),
            accounts=AccountStore(self.directory / "accounts.json"),
        )
        self.family = self.sync.calendars()[0]
        self.personal = self.sync.add_local_calendar("Personal", "#3584e4")
        self.now = datetime.now(tz=local_timezone())

    def tearDown(self):
        settings.set("allow_deleting_events", True)
        self.sync.storage.close()

    def add(self, summary="Dentist", rrule="", hours=2):
        start = self.now + timedelta(hours=hours)
        event = Event.new(self.family.id, start, start + timedelta(hours=1),
                          summary)
        event.rrule = rrule
        self.sync.save_event(event)
        return self.sync.storage.get_event(self.family.id, event.uid)

    def in_calendar(self, calendar):
        return self.sync.storage.events_in_range(
            [calendar.id], self.now - timedelta(days=400),
            self.now + timedelta(days=400))

    def everywhere(self):
        return self.sync.storage.events_in_range(
            [c.id for c in self.sync.calendars()],
            self.now - timedelta(days=400), self.now + timedelta(days=400))


class MovingAPlainEvent(WithTwoCalendars):
    def move(self, event, **changes):
        edited = event.copy(calendar_id=self.personal.id, raw_ics="", **changes)
        self.sync.save_event(edited, moved_from=event.calendar_id)
        return edited

    def test_it_is_not_left_in_the_old_calendar(self):
        self.move(self.add())
        self.assertEqual(self.in_calendar(self.family), [],
                         "the event was still in the calendar it left")

    def test_it_is_in_the_new_one(self):
        self.move(self.add())
        self.assertEqual([e.summary for e in self.in_calendar(self.personal)],
                         ["Dentist"])

    def test_there_is_only_one_of_it(self):
        """The bug: it appeared in both."""
        self.move(self.add())
        self.assertEqual(len(self.everywhere()), 1)

    def test_an_edit_made_at_the_same_time_is_kept(self):
        self.move(self.add(), summary="Optician")
        self.assertEqual([e.summary for e in self.in_calendar(self.personal)],
                         ["Optician"])

    def test_it_keeps_its_identity(self):
        original = self.add()
        self.move(original)
        self.assertEqual(self.in_calendar(self.personal)[0].uid, original.uid)

    def test_the_stale_location_on_the_server_is_dropped(self):
        """href and etag describe somewhere it no longer lives."""
        original = self.add()
        original.href = "https://server/family/dentist.ics"
        original.etag = '"abc"'
        self.sync.storage.save_event(original)
        self.move(original)
        moved = self.in_calendar(self.personal)[0]
        self.assertIsNone(moved.href)
        self.assertIsNone(moved.etag)

    def test_saving_without_moving_is_unaffected(self):
        event = self.add()
        self.sync.save_event(event.copy(summary="Renamed", raw_ics=""))
        self.assertEqual([e.summary for e in self.in_calendar(self.family)],
                         ["Renamed"])
        self.assertEqual(len(self.everywhere()), 1)

    def test_moving_is_refused_when_deleting_is_switched_off(self):
        """Half a move would leave two of everything."""
        event = self.add()
        settings.set("allow_deleting_events", False)
        self.move(event)
        self.assertEqual([e.summary for e in self.in_calendar(self.family)],
                         ["Dentist"])
        self.assertEqual(len(self.everywhere()), 1, "it was copied anyway")

    def test_it_says_why_it_refused(self):
        event = self.add()
        settings.set("allow_deleting_events", False)
        told = []
        self.sync.connect("sync-finished",
                          lambda _s, ok, message: told.append((ok, message)))
        self.move(event)
        self.assertTrue(told)
        self.assertFalse(told[0][0])
        self.assertIn("deleting is switched off", told[0][1])


class MovingThroughTheEditor(WithTwoCalendars):
    """save_occurrence is the path an edit actually takes."""

    def occurrence_of(self, event):
        return recurrence.expand([event], self.now - timedelta(days=400),
                                 self.now + timedelta(days=400))[0]

    def test_a_plain_event_moves(self):
        event = self.add()
        edited = event.copy(calendar_id=self.personal.id, raw_ics="")
        self.sync.save_occurrence(self.occurrence_of(event), edited,
                                  scope=self.sync.ALL_EVENTS)
        self.assertEqual(len(self.everywhere()), 1)
        self.assertEqual(self.everywhere()[0].calendar_id, self.personal.id)

    def test_a_whole_series_moves(self):
        event = self.add(rrule="FREQ=WEEKLY")
        edited = event.copy(calendar_id=self.personal.id, raw_ics="")
        self.sync.save_occurrence(self.occurrence_of(event), edited,
                                  scope=self.sync.ALL_EVENTS)
        self.assertEqual(self.in_calendar(self.family), [])
        self.assertEqual(len(self.in_calendar(self.personal)), 1)

    def test_moving_one_occurrence_takes_it_out_of_the_series(self):
        event = self.add(rrule="FREQ=WEEKLY", hours=-24 * 7)
        occurrences = recurrence.expand(
            [event], self.now - timedelta(days=14), self.now + timedelta(days=21))
        before = len(occurrences)
        self.assertGreaterEqual(before, 3)

        target = occurrences[1]
        edited = event.copy(calendar_id=self.personal.id, raw_ics="")
        self.sync.save_occurrence(target, edited, scope=self.sync.THIS_EVENT)

        series = self.in_calendar(self.family)
        self.assertEqual(len(series), 1, "the series should still be there")
        left = recurrence.expand(series, self.now - timedelta(days=14),
                                 self.now + timedelta(days=21))
        self.assertEqual(len(left), before - 1,
                         "the moved occurrence is still in the old series")

    def test_the_moved_occurrence_becomes_its_own_event(self):
        event = self.add(rrule="FREQ=WEEKLY", hours=-24 * 7)
        target = recurrence.expand(
            [event], self.now - timedelta(days=14),
            self.now + timedelta(days=21))[1]
        edited = event.copy(calendar_id=self.personal.id, raw_ics="")
        self.sync.save_occurrence(target, edited, scope=self.sync.THIS_EVENT)

        moved = self.in_calendar(self.personal)
        self.assertEqual(len(moved), 1)
        self.assertEqual(moved[0].rrule, "", "it took the repeat rule with it")
        self.assertNotEqual(moved[0].uid, event.uid,
                            "it must be its own event, not the series again")

    def test_this_and_following_moves_the_rest(self):
        event = self.add(rrule="FREQ=WEEKLY", hours=-24 * 7)
        occurrences = recurrence.expand(
            [event], self.now - timedelta(days=14), self.now + timedelta(days=28))
        target = occurrences[1]
        edited = event.copy(calendar_id=self.personal.id, raw_ics="")
        self.sync.save_occurrence(target, edited,
                                  scope=self.sync.THIS_AND_FOLLOWING)

        self.assertEqual(len(self.in_calendar(self.family)), 1)
        self.assertEqual(len(self.in_calendar(self.personal)), 1,
                         "the rest of the series did not land in the new calendar")


if __name__ == "__main__":
    unittest.main()


class ThroughTheWindow(WithTwoCalendars):
    """The whole path an edit really takes: editor -> window -> sync.

    Worth doing at this level because the bug was about which path is
    taken, not about the arithmetic once you are on it.
    """

    def setUp(self):
        super().setUp()
        import gi
        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        from gi.repository import Adw
        Adw.init()
        from kairos.ui.window import CalendarWindow

        class _NullTheme:
            def connect(self, *a, **k):
                pass

        self.application = Adw.Application(application_id="org.kairos.MoveTest")
        self.window = CalendarWindow(self.application, self.sync, _NullTheme())

    def test_the_editor_moving_an_event_leaves_one_copy(self):
        from kairos.ui.event_editor import EventEditor
        event = self.add()
        occurrence = recurrence.expand(
            [event], self.now - timedelta(days=400),
            self.now + timedelta(days=400))[0]

        editor = EventEditor(self.sync.writable_calendars(), event=event)
        # Choose the other calendar, exactly as clicking the row would.
        for index, calendar in enumerate(editor._calendars):
            if calendar.id == self.personal.id:
                editor._calendar_row.set_selected(index)
                break
        else:
            self.fail("the other calendar was not offered")

        editor.connect("saved", lambda _e, edited: self.sync.save_occurrence(
            occurrence, edited, scope=self.sync.ALL_EVENTS))
        editor._on_save(None)

        self.assertEqual(len(self.everywhere()), 1, "the event was copied")
        self.assertEqual(self.everywhere()[0].calendar_id, self.personal.id)
