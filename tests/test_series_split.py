"""Splitting a repeating series: "this and all following events".

The awkward part is COUNT. A rule may carry COUNT or UNTIL but never both,
so a series limited by count cannot simply be ended with a date — the count
has to be divided between the two halves, and getting that wrong silently
changes how many occurrences exist.
"""

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from kairos import ical, recurrence
from kairos.accounts import AccountStore
from kairos.models import Event, local_timezone
from kairos.storage import Storage
from kairos.sync import SyncManager, _with_count


def at(year, month, day, hour=9):
    return datetime(year, month, day, hour, tzinfo=local_timezone())


class CountHelper(unittest.TestCase):
    def test_it_replaces_an_existing_count(self):
        self.assertEqual(_with_count("FREQ=WEEKLY;COUNT=10", 4),
                         "FREQ=WEEKLY;COUNT=4")

    def test_it_removes_until(self):
        """A rule may carry one or the other, never both."""
        rule = _with_count("FREQ=WEEKLY;UNTIL=20260901T000000Z", 4)
        self.assertNotIn("UNTIL", rule)
        self.assertIn("COUNT=4", rule)

    def test_it_keeps_the_rest_of_the_rule(self):
        rule = _with_count("FREQ=WEEKLY;BYDAY=MO,WE;COUNT=10", 3)
        self.assertIn("BYDAY=MO,WE", rule)
        self.assertIn("FREQ=WEEKLY", rule)


class WithASeries(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix="kairos-split-"))
        self.sync = SyncManager(
            store=Storage(self.directory / "cache.db"),
            accounts=AccountStore(self.directory / "accounts.json"),
        )
        self.calendar = self.sync.calendars()[0]

    def tearDown(self):
        self.sync.storage.close()

    def series(self, rule="FREQ=WEEKLY", start=None, summary="Standup"):
        start = start or at(2026, 9, 7)
        event = Event.new(self.calendar.id, start, start + timedelta(minutes=30),
                          summary)
        event.rrule = rule
        self.sync.save_event(event)
        return self.sync.storage.get_event(self.calendar.id, event.uid)

    def occurrences(self, weeks=12):
        events = self.sync.all_events_between(at(2026, 9, 1), at(2026, 9, 1)
                                              + timedelta(weeks=weeks))
        return recurrence.expand(events, at(2026, 9, 1),
                                 at(2026, 9, 1) + timedelta(weeks=weeks))

    def occurrence_at(self, event, index):
        found = recurrence.expand([event], at(2026, 9, 1),
                                  at(2026, 9, 1) + timedelta(weeks=12))
        return found[index]


class DeletingTheRest(WithASeries):
    def test_earlier_occurrences_survive(self):
        event = self.series()
        before = self.occurrences()
        self.assertGreaterEqual(len(before), 6)

        self.sync.delete_occurrence(self.occurrence_at(event, 3),
                                    scope=self.sync.THIS_AND_FOLLOWING)
        after = self.occurrences()
        self.assertEqual(len(after), 3)

    def test_the_split_occurrence_itself_is_gone(self):
        event = self.series()
        target = self.occurrence_at(event, 3)
        self.sync.delete_occurrence(target, scope=self.sync.THIS_AND_FOLLOWING)
        self.assertNotIn(target.start, [o.start for o in self.occurrences()])

    def test_splitting_at_the_first_occurrence_removes_everything(self):
        event = self.series()
        self.sync.delete_occurrence(self.occurrence_at(event, 0),
                                    scope=self.sync.THIS_AND_FOLLOWING)
        self.assertEqual(self.occurrences(), [])

    def test_a_counted_series_keeps_the_right_number(self):
        event = self.series(rule="FREQ=WEEKLY;COUNT=8")
        self.sync.delete_occurrence(self.occurrence_at(event, 3),
                                    scope=self.sync.THIS_AND_FOLLOWING)
        self.assertEqual(len(self.occurrences()), 3)

    def test_a_counted_series_does_not_end_up_with_count_and_until(self):
        """Both in one rule is invalid, and servers reject it."""
        event = self.series(rule="FREQ=WEEKLY;COUNT=8")
        self.sync.delete_occurrence(self.occurrence_at(event, 3),
                                    scope=self.sync.THIS_AND_FOLLOWING)
        stored = self.sync.storage.get_event(self.calendar.id, event.uid)
        rule = [line for line in stored.raw_ics.splitlines()
                if line.startswith("RRULE")][0]
        self.assertFalse("COUNT" in rule and "UNTIL" in rule, rule)


class ChangingTheRest(WithASeries):
    def edited(self, event, **changes):
        from dataclasses import replace
        return replace(event, **changes)

    def test_earlier_occurrences_keep_the_old_details(self):
        event = self.series(summary="Standup")
        target = self.occurrence_at(event, 3)
        self.sync.save_occurrence(target,
                                  self.edited(event, summary="Team sync"),
                                  scope=self.sync.THIS_AND_FOLLOWING)
        summaries = [o.summary for o in self.occurrences()]
        self.assertEqual(summaries[:3], ["Standup"] * 3)

    def test_the_rest_get_the_new_details(self):
        event = self.series(summary="Standup")
        target = self.occurrence_at(event, 3)
        self.sync.save_occurrence(target,
                                  self.edited(event, summary="Team sync"),
                                  scope=self.sync.THIS_AND_FOLLOWING)
        summaries = [o.summary for o in self.occurrences()]
        self.assertEqual(set(summaries[3:]), {"Team sync"})

    def test_the_total_number_of_occurrences_is_unchanged(self):
        event = self.series()
        before = len(self.occurrences())
        target = self.occurrence_at(event, 3)
        self.sync.save_occurrence(target, self.edited(event, summary="Team sync"),
                                  scope=self.sync.THIS_AND_FOLLOWING)
        self.assertEqual(len(self.occurrences()), before)

    def test_the_new_series_is_a_separate_event(self):
        event = self.series()
        target = self.occurrence_at(event, 3)
        self.sync.save_occurrence(target, self.edited(event, summary="Team sync"),
                                  scope=self.sync.THIS_AND_FOLLOWING)
        uids = {o.uid for o in self.occurrences()}
        self.assertEqual(len(uids), 2, "the split did not create a second event")

    def test_a_counted_series_keeps_its_total(self):
        event = self.series(rule="FREQ=WEEKLY;COUNT=8")
        target = self.occurrence_at(event, 3)
        self.sync.save_occurrence(target, self.edited(event, summary="Team sync"),
                                  scope=self.sync.THIS_AND_FOLLOWING)
        self.assertEqual(len(self.occurrences()), 8)

    def test_splitting_at_the_first_occurrence_changes_the_whole_series(self):
        event = self.series()
        before = len(self.occurrences())
        target = self.occurrence_at(event, 0)
        self.sync.save_occurrence(target, self.edited(event, summary="Team sync"),
                                  scope=self.sync.THIS_AND_FOLLOWING)
        summaries = {o.summary for o in self.occurrences()}
        self.assertEqual(summaries, {"Team sync"})
        self.assertEqual(len(self.occurrences()), before)


class TruncatingTheDocument(unittest.TestCase):
    """ical.truncate_series on its own."""

    def document(self, rule="FREQ=WEEKLY", extra=""):
        return f"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//EN
BEGIN:VEVENT
UID:series-1
DTSTAMP:20260907T090000Z
DTSTART:20260907T090000Z
DTEND:20260907T093000Z
SUMMARY:Standup
RRULE:{rule}
{extra}END:VEVENT
END:VCALENDAR
"""

    def rule_of(self, text):
        return [line for line in text.splitlines() if line.startswith("RRULE")][0]

    def test_it_sets_an_until(self):
        out = ical.truncate_series(self.document(), at(2026, 9, 28))
        self.assertIn("UNTIL=", self.rule_of(out))

    def test_the_until_is_before_the_split(self):
        """UNTIL is inclusive, so the split occurrence must fall outside it."""
        split = at(2026, 9, 28)
        out = ical.truncate_series(self.document(), split)
        events = ical.parse_calendar_text(out, "cal")
        for event in events:
            event.raw_ics = out
        found = recurrence.expand(events, at(2026, 9, 1), at(2026, 11, 1))
        self.assertTrue(all(o.start < split for o in found))

    def test_count_is_replaced_rather_than_joined(self):
        out = ical.truncate_series(self.document("FREQ=WEEKLY;COUNT=8"),
                                   at(2026, 9, 28), keep_count=3)
        rule = self.rule_of(out)
        self.assertIn("COUNT=3", rule)
        self.assertNotIn("UNTIL", rule)

    def test_an_exdate_after_the_split_is_dropped(self):
        """It belongs to the new series now, not to this one."""
        out = ical.truncate_series(
            self.document(extra="EXDATE:20261005T090000Z\n"), at(2026, 9, 28))
        self.assertNotIn("20261005", out)

    def test_an_exdate_before_the_split_is_kept(self):
        out = ical.truncate_series(
            self.document(extra="EXDATE:20260914T090000Z\n"), at(2026, 9, 28))
        self.assertIn("20260914", out)

    def test_an_event_that_does_not_repeat_is_refused(self):
        text = self.document().replace("RRULE:FREQ=WEEKLY\n", "")
        with self.assertRaises(ical.ParseError):
            ical.truncate_series(text, at(2026, 9, 28))


if __name__ == "__main__":
    unittest.main()
