"""The change-token, and the way it once made a calendar permanently empty.

A CalDAV server hands out a token meaning "nothing has changed since this".
Kairos stores it and skips the download next time. The token was being saved
*before* the events behind it were fetched, so a single failed fetch left a
token claiming everything was already downloaded — and every later sync
believed it. The calendar sat there for ever with no events in it.

This was reported as "it recognises my calendars but says there are no
events for any of them", which is exactly what it looks like from outside.
"""

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from kairos.accounts import AccountStore
from kairos.models import CALDAV, Account, Calendar, Event, local_timezone
from kairos.storage import Storage
from kairos.sync import SyncManager


class FakeBackend:
    """A server with one calendar, whose fetch can be told to fail."""

    def __init__(self, token="tok-1", fail=False):
        self.calendar = Calendar(id="cal-remote", account_id="acct",
                                 name="NAS", colour="#3584e4",
                                 url="https://nas/caldav/home/",
                                 sync_token=token)
        self.fail = fail
        self.fetches = 0

    def discover_calendars(self):
        from dataclasses import replace
        return [replace(self.calendar)]

    def fetch_events(self, calendar, start, end):
        self.fetches += 1
        if self.fail:
            raise OSError("the connection dropped")
        moment = datetime.now(tz=local_timezone())
        return [Event.new(calendar.id, moment, moment + timedelta(hours=1),
                          "From the server")]


class WhenAFetchFails(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix="kairos-token-"))
        self.storage = Storage(self.directory / "cache.db")
        self.accounts = AccountStore(self.directory / "accounts.json")
        self.account = Account(id="acct", name="NAS", kind=CALDAV,
                               url="https://nas/caldav/")
        self.accounts.add(self.account)
        self.manager = SyncManager(store=self.storage, accounts=self.accounts)
        self.window = (datetime.now(tz=local_timezone()) - timedelta(days=1),
                       datetime.now(tz=local_timezone()) + timedelta(days=30))

    def tearDown(self):
        self.storage.close()

    def stored_token(self):
        found = [c for c in self.storage.list_calendars() if c.id == "cal-remote"]
        return found[0].sync_token if found else None

    def event_count(self):
        return self.storage.count_events("cal-remote")

    def test_the_token_is_not_kept_when_the_download_failed(self):
        backend = FakeBackend(fail=True)
        with self.assertRaises(OSError):
            self.manager._sync_account(backend, self.account, *self.window)
        self.assertEqual(self.stored_token(), "",
                         "a token was stored for events that never arrived")

    def test_the_next_sync_therefore_tries_again(self):
        failing = FakeBackend(fail=True)
        with self.assertRaises(OSError):
            self.manager._sync_account(failing, self.account, *self.window)

        working = FakeBackend()
        self.manager._sync_account(working, self.account, *self.window)
        self.assertEqual(working.fetches, 1, "the retry was skipped")
        self.assertEqual(self.event_count(), 1)

    def test_the_calendar_itself_is_still_saved(self):
        """The name and colour are worth keeping even when the fetch failed."""
        backend = FakeBackend(fail=True)
        with self.assertRaises(OSError):
            self.manager._sync_account(backend, self.account, *self.window)
        self.assertIn("NAS", [c.name for c in self.storage.list_calendars()])


class WhenItWorks(WhenAFetchFails):
    def test_the_token_is_kept_once_the_events_are_stored(self):
        backend = FakeBackend()
        self.manager._sync_account(backend, self.account, *self.window)
        self.assertEqual(self.stored_token(), "tok-1")
        self.assertEqual(self.event_count(), 1)

    def test_an_unchanged_calendar_is_not_downloaded_twice(self):
        backend = FakeBackend()
        self.manager._sync_account(backend, self.account, *self.window)
        self.manager._sync_account(backend, self.account, *self.window)
        self.assertEqual(backend.fetches, 1, "downloaded an unchanged calendar")

    def test_a_changed_token_downloads_again(self):
        backend = FakeBackend()
        self.manager._sync_account(backend, self.account, *self.window)
        backend.calendar.sync_token = "tok-2"
        self.manager._sync_account(backend, self.account, *self.window)
        self.assertEqual(backend.fetches, 2)


class RepairingACacheAlreadySpoiled(unittest.TestCase):
    """Installs stuck in that state must heal themselves, not stay broken."""

    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix="kairos-repair-"))
        self.path = self.directory / "cache.db"

    def stuck_calendar(self, storage, name="NAS", token="tok"):
        storage.save_calendar(Calendar(id=f"cal-{name}", account_id="acct",
                                       name=name, colour="#3584e4",
                                       url=f"https://nas/{name}/",
                                       sync_token=token))

    def test_a_token_with_no_events_is_forgotten(self):
        storage = Storage(self.path)
        self.stuck_calendar(storage)
        storage.close()

        reopened = Storage(self.path)          # the repair runs on open
        try:
            self.assertEqual(
                [c.sync_token for c in reopened.list_calendars()], [""])
        finally:
            reopened.close()

    def test_a_calendar_with_events_keeps_its_token(self):
        storage = Storage(self.path)
        self.stuck_calendar(storage)
        moment = datetime.now(tz=local_timezone())
        storage.save_event(Event.new("cal-NAS", moment,
                                     moment + timedelta(hours=1), "Real"))
        storage.close()

        reopened = Storage(self.path)
        try:
            self.assertEqual(
                [c.sync_token for c in reopened.list_calendars()], ["tok"])
        finally:
            reopened.close()

    def test_only_the_empty_ones_are_repaired(self):
        storage = Storage(self.path)
        self.stuck_calendar(storage, "Empty", "tok-empty")
        self.stuck_calendar(storage, "Full", "tok-full")
        moment = datetime.now(tz=local_timezone())
        storage.save_event(Event.new("cal-Full", moment,
                                     moment + timedelta(hours=1), "Real"))
        storage.close()

        reopened = Storage(self.path)
        try:
            tokens = {c.name: c.sync_token for c in reopened.list_calendars()}
            self.assertEqual(tokens["Empty"], "")
            self.assertEqual(tokens["Full"], "tok-full")
        finally:
            reopened.close()

    def test_it_reports_how_many_it_repaired(self):
        storage = Storage(self.path)
        self.stuck_calendar(storage, "One", "a")
        self.stuck_calendar(storage, "Two", "b")
        try:
            self.assertEqual(storage.clear_tokens_for_empty_calendars(), 2)
            self.assertEqual(storage.clear_tokens_for_empty_calendars(), 0)
        finally:
            storage.close()


if __name__ == "__main__":
    unittest.main()


class RefetchingAfterAFetchFix(unittest.TestCase):
    """A cached copy is only as good as the code that fetched it.

    A change-token says "the server has not changed", which stays true and
    unhelpful when the bug was on our side. FETCH_VERSION clears the tokens
    once so the next sync downloads the events properly, without anyone
    having to be told to delete a cache.
    """

    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix="kairos-refetch-"))
        self.path = self.directory / "cache.db"

    def seed(self, storage, token="tok"):
        from kairos.models import Calendar as C
        storage.save_calendar(C(id="cal", account_id="acct", name="NAS",
                                colour="#3584e4", url="https://nas/c/",
                                sync_token=token))
        moment = datetime.now(tz=local_timezone())
        storage.save_event(Event.new("cal", moment,
                                     moment + timedelta(hours=1), "Cached"))

    def stored_version(self, storage):
        row = storage._connection.execute(
            "SELECT value FROM meta WHERE key = 'fetch_version'").fetchone()
        return int(row["value"]) if row else 0

    def test_an_old_cache_has_its_tokens_cleared(self):
        storage = Storage(self.path)
        self.seed(storage)
        storage._connection.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('fetch_version', '1')")
        storage._connection.commit()
        storage.close()

        reopened = Storage(self.path)
        try:
            self.assertEqual([c.sync_token for c in reopened.list_calendars()], [""])
        finally:
            reopened.close()

    def test_the_events_are_left_alone_meanwhile(self):
        """Nothing should vanish before the replacements arrive."""
        storage = Storage(self.path)
        self.seed(storage)
        storage._connection.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('fetch_version', '1')")
        storage._connection.commit()
        storage.close()

        reopened = Storage(self.path)
        try:
            self.assertEqual(reopened.count_events("cal"), 1)
        finally:
            reopened.close()

    def test_it_only_happens_once(self):
        storage = Storage(self.path)
        self.seed(storage)
        storage._connection.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('fetch_version', '1')")
        storage._connection.commit()
        storage.close()

        first = Storage(self.path)
        from kairos.storage import FETCH_VERSION
        self.assertEqual(self.stored_version(first), FETCH_VERSION)
        # A token earned after the refetch must survive the next start.
        for calendar in first.list_calendars():
            calendar.sync_token = "fresh"
            first.save_calendar(calendar)
        first.close()

        again = Storage(self.path)
        try:
            self.assertEqual([c.sync_token for c in again.list_calendars()],
                             ["fresh"], "the refetch ran a second time")
        finally:
            again.close()

    def test_a_new_cache_is_stamped_and_not_disturbed(self):
        storage = Storage(self.path)
        try:
            from kairos.storage import FETCH_VERSION
            self.assertEqual(self.stored_version(storage), FETCH_VERSION)
        finally:
            storage.close()
