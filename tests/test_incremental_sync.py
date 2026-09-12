"""Syncing only what changed, and falling back to a full download safely.

A full download is always correct, only slower. So every uncertain case —
no token yet, a server that cannot do it, a token it has forgotten, a reply
that made no sense — must end in a full download, never in stale data.
"""

import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

from kairos.accounts import AccountStore
from kairos.backends import AuthenticationError, BackendError, SyncChanges, SyncTokenRejected
from kairos.models import CALDAV, Account, Calendar, Event, local_timezone
from kairos.storage import PENDING_SAVE, Storage
from kairos.sync import SyncManager

BASE = "https://nas.example/caldav/home/"


class FakeServer:
    """One calendar; records every call; can be told how to misbehave."""

    def __init__(self):
        self.calls = []
        self.ctag = "ctag-1"
        self.dav_token = "tok-1"
        self.full_events = []
        self.changes = SyncChanges(token="tok-2")
        self.changes_error = None
        self.supports_incremental_sync = True

    def discover_calendars(self):
        return [Calendar(id="cal", account_id="acct", name="NAS", url=BASE,
                         sync_token=self.ctag)]

    def dav_sync_token(self, calendar):
        self.calls.append("token")
        return self.dav_token

    def fetch_events(self, calendar, start, end):
        self.calls.append("full")
        return [replace(e) for e in self.full_events]

    def fetch_changes(self, calendar, token):
        self.calls.append(("changes", token))
        if self.changes_error:
            raise self.changes_error
        return self.changes


class WithACache(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix="kairos-incremental-"))
        self.storage = Storage(self.directory / "cache.db")
        self.accounts = AccountStore(self.directory / "accounts.json")
        self.account = Account(id="acct", name="NAS", kind=CALDAV, url=BASE)
        self.accounts.add(self.account)
        self.manager = SyncManager(store=self.storage, accounts=self.accounts)
        self.server = FakeServer()
        now = datetime.now(tz=local_timezone())
        self.window = (now - timedelta(days=30), now + timedelta(days=30))
        self.now = now

    def tearDown(self):
        self.storage.close()

    def event(self, summary, uid=None, href=None):
        start = self.now + timedelta(days=1)
        event = Event.new("cal", start, start + timedelta(hours=1), summary)
        if uid:
            event.uid = uid
        event.href = href or f"{BASE}{event.uid}.ics"
        return event

    def sync(self):
        self.manager._sync_account(self.server, self.account, *self.window)

    def calendar(self):
        return [c for c in self.storage.list_calendars() if c.id == "cal"][0]

    def summaries(self):
        return sorted(e.summary for e in self.storage.events_in_range(["cal"], *self.window))


class TheFirstSync(WithACache):
    def test_is_a_full_download(self):
        self.server.full_events = [self.event("Standup")]
        self.sync()
        self.assertIn("full", self.server.calls)
        self.assertEqual(self.summaries(), ["Standup"])

    def test_keeps_the_token_it_read_before_downloading(self):
        """So a change made during the download is not lost in the gap."""
        self.sync()
        self.assertLess(self.server.calls.index("token"), self.server.calls.index("full"))
        self.assertEqual(self.calendar().dav_sync_token, "tok-1")


class ALaterSync(WithACache):
    def setUp(self):
        super().setUp()
        self.server.full_events = [self.event("Standup", uid="a"), self.event("Dentist", uid="b")]
        self.sync()
        self.server.calls.clear()
        self.server.ctag = "ctag-2"            # something changed

    def test_fetches_only_the_changes(self):
        self.server.changes = SyncChanges(changed=[self.event("Standup, moved", uid="a")],
                                          token="tok-2")
        self.sync()
        self.assertEqual(self.server.calls, [("changes", "tok-1")])
        self.assertEqual(self.summaries(), ["Dentist", "Standup, moved"])

    def test_applies_a_removal(self):
        self.server.changes = SyncChanges(removed=["/caldav/home/b.ics"], token="tok-2")
        self.sync()
        self.assertEqual(self.summaries(), ["Standup"])

    def test_a_removal_is_matched_by_path_not_by_exact_text(self):
        """Stored as a full URL, reported as a percent-escaped path."""
        self.server.changes = SyncChanges(removed=["/caldav/%68ome/b.ics"], token="tok-2")
        self.sync()
        self.assertEqual(self.summaries(), ["Standup"])

    def test_carries_the_new_token_forward(self):
        self.sync()
        self.assertEqual(self.calendar().dav_sync_token, "tok-2")
        self.assertEqual(self.calendar().sync_token, "ctag-2")

    def test_an_unchanged_calendar_asks_for_nothing(self):
        self.server.ctag = "ctag-1"
        self.sync()
        self.assertEqual(self.server.calls, [])

    def test_a_forgotten_token_means_a_full_download(self):
        self.server.changes_error = SyncTokenRejected("forgotten")
        self.server.dav_token = "tok-fresh"
        self.sync()
        self.assertIn("full", self.server.calls)
        self.assertEqual(self.calendar().dav_sync_token, "tok-fresh")

    def test_any_other_failure_also_means_a_full_download(self):
        self.server.changes_error = BackendError("confusing reply")
        self.sync()
        self.assertIn("full", self.server.calls)

    def test_a_reply_without_a_token_means_a_full_download(self):
        self.server.changes = SyncChanges(token="")
        self.sync()
        self.assertIn("full", self.server.calls)

    def test_a_wrong_password_is_not_swallowed(self):
        self.server.changes_error = AuthenticationError("no")
        with self.assertRaises(AuthenticationError):
            self.sync()

    def test_a_failure_does_not_advance_the_token(self):
        self.server.changes_error = BackendError("confusing reply")
        self.server.fetch_events = lambda *a: (_ for _ in ()).throw(BackendError("down"))
        with self.assertRaises(BackendError):
            self.sync()
        self.assertEqual(self.calendar().dav_sync_token, "tok-1")
        self.assertEqual(self.calendar().sync_token, "ctag-1")

    def test_a_server_without_support_downloads_in_full(self):
        self.server.supports_incremental_sync = False
        self.sync()
        self.assertEqual(self.server.calls, ["full"])


class LocalEditsSurvive(WithACache):
    """An unpushed edit must outlive whatever the server reports."""

    def setUp(self):
        super().setUp()
        self.server.full_events = [self.event("Standup", uid="a")]
        self.sync()
        # raw_ics="" so the stored text is rebuilt; otherwise the old
        # iCalendar is kept and the "edit" never takes.
        mine = self.storage.get_event("cal", "a").copy(summary="My edit", raw_ics="")
        self.storage.save_event(mine, pending=PENDING_SAVE)
        self.server.ctag = "ctag-2"

    def test_against_a_change_from_the_server(self):
        self.server.changes = SyncChanges(changed=[self.event("Theirs", uid="a")], token="t")
        self.sync()
        self.assertEqual(self.summaries(), ["My edit"])

    def test_against_a_removal_from_the_server(self):
        self.server.changes = SyncChanges(removed=[f"{BASE}a.ics"], token="t")
        self.sync()
        self.assertEqual(self.summaries(), ["My edit"])


class TheColumn(unittest.TestCase):
    def test_an_older_cache_gains_it(self):
        path = Path(tempfile.mkdtemp(prefix="kairos-oldcache-")) / "cache.db"
        connection = sqlite3.connect(path)
        connection.execute("""CREATE TABLE calendars (id TEXT PRIMARY KEY, account_id TEXT NOT NULL,
            name TEXT NOT NULL, colour TEXT NOT NULL DEFAULT '#3584e4', url TEXT NOT NULL DEFAULT '',
            read_only INTEGER NOT NULL DEFAULT 0, visible INTEGER NOT NULL DEFAULT 1,
            sync_token TEXT NOT NULL DEFAULT '')""")
        connection.execute("INSERT INTO calendars (id, account_id, name, sync_token) "
                           "VALUES ('c', 'a', 'Old', 'x')")
        connection.commit()
        connection.close()

        storage = Storage(path)
        try:
            calendar = storage.list_calendars()[0]
            self.assertEqual(calendar.dav_sync_token, "")
            calendar.dav_sync_token = "tok"
            storage.save_calendar(calendar)
            self.assertEqual(storage.list_calendars()[0].dav_sync_token, "tok")
        finally:
            storage.close()

    def test_a_forced_refetch_clears_it_too(self):
        path = Path(tempfile.mkdtemp(prefix="kairos-refetch2-")) / "cache.db"
        storage = Storage(path)
        storage.save_calendar(Calendar(id="c", account_id="a", name="NAS",
                                       sync_token="ctag", dav_sync_token="tok"))
        storage._connection.execute("UPDATE meta SET value = '1' WHERE key = 'fetch_version'")
        storage._connection.commit()
        storage.close()
        again = Storage(path)
        try:
            calendar = again.list_calendars()[0]
            self.assertEqual((calendar.sync_token, calendar.dav_sync_token), ("", ""))
        finally:
            again.close()


if __name__ == "__main__":
    unittest.main()
