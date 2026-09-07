"""Talking to a real CalDAV server.

These tests start `Radicale <https://radicale.org>`_ on localhost and drive the
CalDAV backend against it, so they cover the parts that a mocked server would
not: what the wire format actually looks like, whether an update lands on the
same resource, whether ETags come back, and whether a conditional write really
does fail when someone else has edited the event.

Radicale is a test-only dependency.  If it is not installed the whole module
skips, so ``make test`` still works on a machine without it::

    pip install radicale
"""

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta

# Only ever run as `python -m radicale` in a subprocess below, so this asks
# whether it is installed rather than importing it for its own sake.
HAVE_RADICALE = importlib.util.find_spec("radicale") is not None

from kairos.models import CALDAV, Account, Alarm, Event, local_timezone, start_of_day

#: Chosen to stay clear of Radicale's default 5232 in case the user is
#: running their own.
PORT = 8973


def free_port_or_skip() -> None:
    import socket
    with socket.socket() as probe:
        if probe.connect_ex(("127.0.0.1", PORT)) == 0:
            raise unittest.SkipTest(f"port {PORT} is already in use")


@unittest.skipUnless(HAVE_RADICALE, "radicale is not installed (pip install radicale)")
class CalDAVServerTestCase(unittest.TestCase):
    """Base class that runs one Radicale for the whole class."""

    server: subprocess.Popen
    directory: str

    @classmethod
    def setUpClass(cls):
        free_port_or_skip()
        cls.directory = tempfile.mkdtemp(prefix="kairos-caldav-")
        users = os.path.join(cls.directory, "users")
        with open(users, "w", encoding="utf-8") as handle:
            handle.write("tester:secret123\n")

        config = os.path.join(cls.directory, "radicale.conf")
        with open(config, "w", encoding="utf-8") as handle:
            handle.write(
                f"[server]\nhosts = 127.0.0.1:{PORT}\n"
                f"[auth]\ntype = htpasswd\nhtpasswd_filename = {users}\n"
                f"htpasswd_encryption = plain\n"
                f"[storage]\nfilesystem_folder = {os.path.join(cls.directory, 'collections')}\n"
                f"[logging]\nlevel = error\n"
            )

        cls.server = subprocess.Popen(
            [sys.executable, "-m", "radicale", "--config", config],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        cls._wait_for_server()

        # Kairos refuses plain http unless told otherwise; localhost is the
        # exact case that preference exists for.
        from kairos.config import settings
        settings.set("allow_insecure_http", True)

        cls.base = f"http://127.0.0.1:{PORT}/tester/"
        cls.account = Account(id="test-account", name="Radicale", kind=CALDAV,
                              url=cls.base, username="tester")

        import caldav
        client = caldav.DAVClient(url=cls.base, username="tester", password="secret123")
        client.principal().make_calendar(name="Home")

    @classmethod
    def _wait_for_server(cls) -> None:
        for _ in range(80):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{PORT}/", timeout=1)
                return
            except urllib.error.HTTPError:
                return                      # 401 means it is up and asking for auth
            except Exception:
                time.sleep(0.25)
        raise unittest.SkipTest("radicale did not start")

    @classmethod
    def tearDownClass(cls):
        cls.server.terminate()
        try:
            cls.server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            cls.server.kill()
        shutil.rmtree(cls.directory, ignore_errors=True)

    def setUp(self):
        from kairos.backends.caldav_backend import CalDAVBackend
        self.backend = CalDAVBackend(self.account, password="secret123")
        self.calendar = self.backend.discover_calendars()[0]
        self.now = datetime.now(tz=local_timezone()).replace(
            minute=0, second=0, microsecond=0
        )

    def tearDown(self):
        for event in self.backend.fetch_events(
            self.calendar, self.now - timedelta(days=400), self.now + timedelta(days=400)
        ):
            self.backend.delete_event(self.calendar, event)

    def make(self, summary="Event", hours_ahead=2, length=1, **kwargs):
        start = self.now + timedelta(hours=hours_ahead)
        event = Event.new(self.calendar.id, start, start + timedelta(hours=length), summary)
        for key, value in kwargs.items():
            setattr(event, key, value)
        return event

    def fetch(self):
        return self.backend.fetch_events(
            self.calendar, self.now - timedelta(days=2), self.now + timedelta(days=30)
        )


class Discovery(CalDAVServerTestCase):
    def test_finds_the_calendar(self):
        self.assertEqual(self.calendar.name, "Home")
        self.assertTrue(self.calendar.url.startswith("http://127.0.0.1"))

    def test_gives_it_a_stable_id(self):
        again = self.backend.discover_calendars()[0]
        self.assertEqual(again.id, self.calendar.id)

    def test_reports_a_wrong_password_as_an_authentication_failure(self):
        from kairos.backends.base import AuthenticationError
        from kairos.backends.caldav_backend import CalDAVBackend
        wrong = CalDAVBackend(self.account, password="not-the-password")
        with self.assertRaises(AuthenticationError):
            wrong.discover_calendars()

    def test_the_password_never_appears_in_an_error_message(self):
        from kairos.backends.base import BackendError
        from kairos.backends.caldav_backend import CalDAVBackend
        wrong = CalDAVBackend(self.account, password="hunter2-secret")
        try:
            wrong.discover_calendars()
        except BackendError as exc:
            self.assertNotIn("hunter2-secret", str(exc))


class WritingAndReading(CalDAVServerTestCase):
    def test_create_then_read_back(self):
        original = self.make("Server event", location="Room 3",
                             description="Notes", alarms=[Alarm(15), Alarm(60)])
        saved = self.backend.save_event(self.calendar, original)
        self.assertTrue(saved.href)

        found = self.fetch()
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].summary, "Server event")
        self.assertEqual(found[0].location, "Room 3")
        self.assertEqual(found[0].description, "Notes")
        self.assertEqual(found[0].start, original.start)
        self.assertEqual(sorted(a.minutes_before for a in found[0].alarms), [15, 60])

    def test_the_server_gives_us_an_etag(self):
        self.backend.save_event(self.calendar, self.make())
        self.assertTrue(self.fetch()[0].etag)

    def test_an_edit_updates_in_place_rather_than_duplicating(self):
        self.backend.save_event(self.calendar, self.make("Before"))
        existing = self.fetch()[0]
        self.backend.save_event(self.calendar, existing.copy(summary="After", raw_ics=""))

        found = self.fetch()
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].summary, "After")

    def test_all_day_events_keep_their_span(self):
        start = start_of_day(date.today() + timedelta(days=3))
        event = Event.new(self.calendar.id, start, start + timedelta(days=2),
                          "Two day trip", all_day=True)
        self.backend.save_event(self.calendar, event)

        found = [e for e in self.fetch() if e.summary == "Two day trip"]
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0].all_day)
        self.assertEqual(found[0].start, start)
        self.assertEqual(found[0].end, start + timedelta(days=2))

    def test_a_repeating_event_comes_back_as_one_master(self):
        from kairos import recurrence
        event = self.make("Weekly thing", hours_ahead=24, rrule="FREQ=WEEKLY;COUNT=4")
        self.backend.save_event(self.calendar, event)

        found = [e for e in self.fetch() if e.summary == "Weekly thing"]
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0].rrule.upper().startswith("FREQ=WEEKLY"))

        occurrences = recurrence.expand(found, self.now, self.now + timedelta(days=60))
        self.assertEqual(len(occurrences), 4)

    def test_delete(self):
        self.backend.save_event(self.calendar, self.make("Doomed"))
        self.backend.delete_event(self.calendar, self.fetch()[0])
        self.assertEqual(self.fetch(), [])

    def test_deleting_twice_is_not_an_error(self):
        self.backend.save_event(self.calendar, self.make("Doomed"))
        event = self.fetch()[0]
        self.backend.delete_event(self.calendar, event)
        self.backend.delete_event(self.calendar, event)   # must not raise

    def test_a_read_only_calendar_refuses_writes(self):
        from kairos.backends.base import BackendError
        read_only = self.calendar
        read_only.read_only = True
        try:
            with self.assertRaises(BackendError):
                self.backend.save_event(read_only, self.make())
        finally:
            read_only.read_only = False


class ConcurrentEdits(CalDAVServerTestCase):
    def test_a_stale_etag_is_detected_and_the_users_edit_still_wins(self):
        """Kairos sends If-Match, notices the 412, and keeps the user's work.

        Overwriting the other change is a deliberate choice: Kairos has no
        merge UI, and silently discarding what the person just typed would be
        the worse outcome.  See README, "Conflicts".
        """
        mine = self.backend.save_event(self.calendar, self.make("Contested"))
        self.assertTrue(mine.etag)

        # Someone else edits the same resource, moving the ETag on.
        self.backend.save_event(
            self.calendar, mine.copy(summary="Their version", etag=None, raw_ics="")
        )

        with self.assertLogs("kairos.backends.caldav_backend", level="WARNING") as logs:
            result = self.backend.save_event(
                self.calendar, mine.copy(summary="My version", raw_ics="")
            )
        self.assertTrue(any("changed on the server" in line for line in logs.output))

        found = [e for e in self.fetch() if e.uid == mine.uid]
        self.assertEqual(len(found), 1, "the conflict created a duplicate")
        self.assertEqual(found[0].summary, "My version")
        self.assertTrue(result.etag)


class ChangeDetection(CalDAVServerTestCase):
    def test_the_sync_token_only_moves_when_something_changes(self):
        before = self.backend.current_sync_token(self.calendar)
        self.assertTrue(before, "server did not provide a ctag")
        self.assertEqual(self.backend.current_sync_token(self.calendar), before)

        self.backend.save_event(self.calendar, self.make("Moves the token"))
        self.assertNotEqual(self.backend.current_sync_token(self.calendar), before)


class SyncRoundTrip(CalDAVServerTestCase):
    """The SyncManager's offline queue, against a real server."""

    def setUp(self):
        super().setUp()
        import tempfile as tf
        from kairos.accounts import AccountStore
        from kairos.security import credentials
        from kairos.storage import Storage
        from kairos.sync import SyncManager

        credentials.set_password(self.account.id, "secret123")
        accounts = AccountStore(path=os.path.join(self.directory, "accounts.json"))
        accounts.add(self.account)
        storage = Storage(os.path.join(tf.mkdtemp(prefix="kairos-sync-"), "cache.db"))
        self.manager = SyncManager(store=storage, accounts=accounts)
        self.manager.storage.save_calendar(self.calendar)

    def tearDown(self):
        self.manager.storage.close()
        super().tearDown()

    def cached_calendar(self):
        return [c for c in self.manager.storage.list_calendars()
                if c.account_id == self.account.id][0]

    def test_a_full_sync_pulls_the_servers_events_into_the_cache(self):
        self.backend.save_event(self.calendar, self.make("From the server"))
        self.manager._full_sync()
        self.assertEqual(self.manager.storage.count_events(self.cached_calendar().id), 1)

    def test_an_event_written_offline_is_pushed_on_the_next_sync(self):
        from kairos.storage import PENDING_SAVE
        calendar = self.cached_calendar()
        event = Event.new(calendar.id, self.now + timedelta(hours=6),
                          self.now + timedelta(hours=7), "Made in Kairos")
        event.alarms = [Alarm(30)]
        self.manager.storage.save_event(event, pending=PENDING_SAVE)

        self.manager._push_pending()

        self.assertEqual(self.manager.storage.pending_changes(), [])
        found = [e for e in self.fetch() if e.summary == "Made in Kairos"]
        self.assertEqual(len(found), 1)
        self.assertEqual([a.minutes_before for a in found[0].alarms], [30])

    def test_a_deletion_made_offline_is_pushed(self):
        self.backend.save_event(self.calendar, self.make("To be removed"))
        self.manager._full_sync()

        calendar = self.cached_calendar()
        cached = self.manager.storage.events_in_range(
            [calendar.id], self.now, self.now + timedelta(days=1)
        )
        self.manager.storage.mark_event_deleted(calendar.id, cached[0].uid)
        self.manager._push_pending()

        self.assertEqual(self.fetch(), [])


if __name__ == "__main__":
    unittest.main()


class SingleOccurrences(CalDAVServerTestCase):
    """Changing one instance of a repeating series, against a real server.

    A series is one resource holding a master VEVENT plus an override per
    changed instance, so "just this one" means rewriting that resource — not
    writing a second one. These check the round trip through Radicale, since
    that is the only way to know the document we produce is one a server will
    accept and hand back unchanged.
    """

    def weekly(self, summary="Standup"):
        """A weekly series starting a week ago, saved to the server."""
        start = (self.now - timedelta(days=7)).replace(hour=9)
        event = Event.new(self.calendar.id, start, start + timedelta(minutes=30),
                          summary)
        event.rrule = "FREQ=WEEKLY"
        return self.backend.save_event(self.calendar, event)

    def occurrences(self, event, days=21):
        from kairos import recurrence
        return recurrence.expand([event],
                                 self.now - timedelta(days=8),
                                 self.now + timedelta(days=days))

    def stored(self):
        """The single event resource, freshly fetched."""
        events = self.fetch()
        self.assertEqual(len(events), 1, "expected exactly one resource")
        return events[0]

    def push(self, event, text):
        """Save a rewritten series document through the backend."""
        from dataclasses import replace as dataclass_replace
        return self.backend.save_event(self.calendar,
                                       dataclass_replace(event, raw_ics=text))

    # -- deleting one -------------------------------------------------

    def test_excluding_one_occurrence_leaves_the_rest(self):
        from kairos import ical
        saved = self.weekly()
        before = self.occurrences(saved)
        self.assertGreaterEqual(len(before), 3)

        target = before[1]
        self.push(saved, ical.exclude_occurrence(saved.raw_ics,
                                                 target.recurrence_id))

        after = self.occurrences(self.stored())
        self.assertEqual(len(after), len(before) - 1)
        self.assertNotIn(target.recurrence_id, [o.recurrence_id for o in after])

    def test_excluding_does_not_delete_the_resource(self):
        from kairos import ical
        saved = self.weekly()
        target = self.occurrences(saved)[1]
        self.push(saved, ical.exclude_occurrence(saved.raw_ics,
                                                 target.recurrence_id))
        self.assertEqual(len(self.fetch()), 1)

    # -- editing one --------------------------------------------------

    def test_overriding_one_occurrence_changes_only_that_one(self):
        from dataclasses import replace as dataclass_replace
        from kairos import ical
        saved = self.weekly()
        before = self.occurrences(saved)
        target = before[1]

        edited = dataclass_replace(
            saved, summary="Moved this week", rrule="", raw_ics="",
            start=target.start + timedelta(hours=5),
            end=target.end + timedelta(hours=5))
        self.push(saved, ical.override_occurrence(
            saved.raw_ics, target.recurrence_id, edited))

        after = self.occurrences(self.stored())
        self.assertEqual(len(after), len(before))
        moved = [o for o in after if o.summary == "Moved this week"]
        self.assertEqual(len(moved), 1, "the override did not come back")
        self.assertEqual(moved[0].start, target.start + timedelta(hours=5))

    def test_the_other_occurrences_keep_the_series_name(self):
        from dataclasses import replace as dataclass_replace
        from kairos import ical
        saved = self.weekly()
        target = self.occurrences(saved)[1]
        edited = dataclass_replace(saved, summary="Moved this week",
                                   rrule="", raw_ics="")
        self.push(saved, ical.override_occurrence(
            saved.raw_ics, target.recurrence_id, edited))

        after = self.occurrences(self.stored())
        others = [o for o in after if o.summary != "Moved this week"]
        self.assertEqual({o.summary for o in others}, {"Standup"})

    def test_an_override_can_be_replaced(self):
        """Editing the same occurrence twice must not stack up overrides."""
        from dataclasses import replace as dataclass_replace
        from kairos import ical
        saved = self.weekly()
        target = self.occurrences(saved)[1]

        for name in ("First change", "Second change"):
            current = self.stored()
            edited = dataclass_replace(current, summary=name, rrule="", raw_ics="")
            self.push(current, ical.override_occurrence(
                current.raw_ics, target.recurrence_id, edited))

        after = self.occurrences(self.stored())
        self.assertEqual([o.summary for o in after].count("Second change"), 1)
        self.assertEqual([o.summary for o in after].count("First change"), 0)

    def test_an_overridden_occurrence_can_then_be_excluded(self):
        from dataclasses import replace as dataclass_replace
        from kairos import ical
        saved = self.weekly()
        before = self.occurrences(saved)
        target = before[1]

        edited = dataclass_replace(saved, summary="Moved", rrule="", raw_ics="")
        self.push(saved, ical.override_occurrence(
            saved.raw_ics, target.recurrence_id, edited))

        current = self.stored()
        self.push(current, ical.exclude_occurrence(current.raw_ics,
                                                   target.recurrence_id))

        after = self.occurrences(self.stored())
        self.assertEqual(len(after), len(before) - 1)
        self.assertNotIn("Moved", [o.summary for o in after])


class SplittingASeries(CalDAVServerTestCase):
    """"This and all following" against a real server.

    The split writes a truncated rule to one resource and a whole new event
    to another, so this checks the server accepts both and hands them back
    describing the same set of occurrences.
    """

    def weekly(self, summary="Standup", rule="FREQ=WEEKLY"):
        start = (self.now - timedelta(days=7)).replace(hour=9)
        event = Event.new(self.calendar.id, start, start + timedelta(minutes=30),
                          summary)
        event.rrule = rule
        return self.backend.save_event(self.calendar, event)

    def occurrences(self, events=None, days=60):
        from kairos import recurrence
        return recurrence.expand(events if events is not None else self.fetch_all(),
                                 self.now - timedelta(days=8),
                                 self.now + timedelta(days=days))

    def fetch_all(self):
        return self.backend.fetch_events(
            self.calendar, self.now - timedelta(days=30),
            self.now + timedelta(days=90))

    def push(self, event, text):
        from dataclasses import replace as dataclass_replace
        return self.backend.save_event(self.calendar,
                                       dataclass_replace(event, raw_ics=text))

    def test_a_truncated_series_is_accepted_and_read_back(self):
        from kairos import ical
        saved = self.weekly()
        before = self.occurrences([saved])
        self.assertGreaterEqual(len(before), 4)

        split = before[2].recurrence_id
        self.push(saved, ical.truncate_series(saved.raw_ics, split))

        after = self.occurrences()
        self.assertEqual(len(after), 2)
        self.assertTrue(all(o.recurrence_id < split for o in after))

    def test_a_counted_series_splits_without_count_and_until_together(self):
        """Radicale rejects a rule carrying both."""
        from kairos import ical
        saved = self.weekly(rule="FREQ=WEEKLY;COUNT=6")
        split = self.occurrences([saved])[2].recurrence_id
        self.push(saved, ical.truncate_series(saved.raw_ics, split, keep_count=2))
        self.assertEqual(len(self.occurrences()), 2)

    def test_the_two_halves_come_back_as_two_resources(self):
        from dataclasses import replace as dataclass_replace
        from kairos import ical
        from kairos.models import new_uid

        saved = self.weekly("Standup")
        before = self.occurrences([saved])
        split_at = before[2]

        self.push(saved, ical.truncate_series(saved.raw_ics,
                                              split_at.recurrence_id))
        self.backend.save_event(self.calendar, dataclass_replace(
            saved, uid=new_uid(), href=None, etag=None, raw_ics="",
            summary="Team sync", start=split_at.start,
            end=split_at.start + timedelta(minutes=30)))

        found = self.fetch_all()
        self.assertEqual(len(found), 2)
        after = self.occurrences(found)
        self.assertEqual(len(after), len(before))
        self.assertEqual({o.summary for o in after[:2]}, {"Standup"})
        self.assertEqual({o.summary for o in after[2:]}, {"Team sync"})
