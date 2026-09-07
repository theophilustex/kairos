"""Switching off deletion, for people who would rather it never happened.

The switch is enforced in :mod:`kairos.sync`, not only by hiding the button.
Hiding a button stops the obvious route; the guard stops every other one — a
deletion queued before the setting changed, a keyboard shortcut, some future
caller that has not thought about it.
"""

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402

Adw.init()

from kairos import recurrence  # noqa: E402
from kairos.accounts import AccountStore  # noqa: E402
from kairos.config import settings  # noqa: E402
from kairos.models import Event, Occurrence, local_timezone  # noqa: E402
from kairos.storage import Storage  # noqa: E402
from kairos.sync import SyncManager  # noqa: E402


class WithAnEvent(unittest.TestCase):
    def setUp(self):
        settings.set("allow_deleting_events", True)
        self.directory = Path(tempfile.mkdtemp(prefix="kairos-nodelete-"))
        self.sync = SyncManager(
            store=Storage(self.directory / "cache.db"),
            accounts=AccountStore(self.directory / "accounts.json"),
        )
        self.calendar = self.sync.calendars()[0]
        start = datetime.now(tz=local_timezone()) + timedelta(days=1)
        self.event = Event.new(self.calendar.id, start,
                               start + timedelta(hours=1), "Dentist")
        self.sync.save_event(self.event)

    def tearDown(self):
        settings.set("allow_deleting_events", True)
        self.sync.storage.close()

    def stored(self):
        return self.sync.storage.get_event(self.calendar.id, self.event.uid)


class TheSwitch(WithAnEvent):
    def test_deleting_works_when_it_is_on(self):
        self.sync.delete_event(self.stored())
        self.assertIsNone(self.stored())

    def test_deleting_is_refused_when_it_is_off(self):
        settings.set("allow_deleting_events", False)
        self.sync.delete_event(self.stored())
        self.assertIsNotNone(self.stored(), "the event was deleted anyway")

    def test_it_says_why_rather_than_failing_silently(self):
        settings.set("allow_deleting_events", False)
        told = []
        self.sync.connect("sync-finished",
                          lambda _s, ok, message: told.append((ok, message)))
        self.sync.delete_event(self.stored())
        self.assertTrue(told, "nothing was reported to the user")
        self.assertFalse(told[0][0])
        self.assertIn("switched off", told[0][1])

    def test_saving_still_works(self):
        """Only deleting is blocked; the calendar is not read-only."""
        settings.set("allow_deleting_events", False)
        self.sync.save_event(self.stored().copy(summary="Optician", raw_ics=""))
        self.assertEqual(self.stored().summary, "Optician")

    def test_the_default_is_to_allow_it(self):
        """Off by default would surprise everyone who never opens settings."""
        from kairos.config import DEFAULTS
        self.assertIs(DEFAULTS["allow_deleting_events"], True)


class Occurrences(WithAnEvent):
    def series(self):
        start = datetime.now(tz=local_timezone()) - timedelta(days=7)
        event = Event.new(self.calendar.id, start, start + timedelta(hours=1),
                          "Standup")
        event.rrule = "FREQ=WEEKLY"
        self.sync.save_event(event)
        stored = self.sync.storage.get_event(self.calendar.id, event.uid)
        found = recurrence.expand(
            [stored], start - timedelta(days=1),
            start + timedelta(days=30))
        return stored, found

    def test_deleting_one_occurrence_is_refused(self):
        stored, found = self.series()
        settings.set("allow_deleting_events", False)
        before = len(found)
        self.sync.delete_occurrence(found[1], scope=self.sync.THIS_EVENT)
        again = self.sync.storage.get_event(self.calendar.id, stored.uid)
        after = recurrence.expand([again], found[0].start - timedelta(days=1),
                                  found[0].start + timedelta(days=30))
        self.assertEqual(len(after), before)

    def test_deleting_the_rest_of_a_series_is_refused(self):
        stored, found = self.series()
        settings.set("allow_deleting_events", False)
        before = len(found)
        self.sync.delete_occurrence(found[1],
                                    scope=self.sync.THIS_AND_FOLLOWING)
        again = self.sync.storage.get_event(self.calendar.id, stored.uid)
        after = recurrence.expand([again], found[0].start - timedelta(days=1),
                                  found[0].start + timedelta(days=30))
        self.assertEqual(len(after), before)

    def test_deleting_a_whole_series_is_refused(self):
        stored, found = self.series()
        settings.set("allow_deleting_events", False)
        self.sync.delete_occurrence(found[1], scope=self.sync.ALL_EVENTS)
        self.assertIsNotNone(
            self.sync.storage.get_event(self.calendar.id, stored.uid))


class TheDeleteButton(WithAnEvent):
    def popover(self):
        from kairos.ui.event_popover import EventPopover
        start = self.event.start
        occurrence = Occurrence(event=self.event, start=start,
                                end=start + timedelta(hours=1))
        return EventPopover(occurrence, calendar_name="Personal",
                            calendar_colour="#3584e4")

    def labels(self):
        def walk(widget):
            yield widget
            child = widget.get_first_child()
            while child is not None:
                yield from walk(child)
                child = child.get_next_sibling()

        return [w.get_label() for w in walk(self.popover())
                if isinstance(w, Gtk.Button) and w.get_label()]

    def test_the_button_is_there_when_deleting_is_allowed(self):
        self.assertIn("Delete", self.labels())

    def test_the_button_is_gone_when_it_is_not(self):
        settings.set("allow_deleting_events", False)
        self.assertNotIn("Delete", self.labels())

    def test_edit_is_still_offered(self):
        settings.set("allow_deleting_events", False)
        self.assertIn("Edit", self.labels())


if __name__ == "__main__":
    unittest.main()
