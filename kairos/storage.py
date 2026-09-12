"""The offline cache: a small SQLite database of calendars and events.

Kairos never asks the network to draw a screen.  Everything the views need
comes from here, and :mod:`kairos.sync` refreshes it in the background.  That
is what makes the app feel instant and what makes it work on a train.

The schema is deliberately tiny — three tables, no migrations framework, no
ORM:

    accounts_meta   one row per calendar-source, for sync bookkeeping
    calendars       one row per calendar
    events          one row per event, holding both the parsed fields (so
                    range queries and search are fast) and the original
                    iCalendar text (so nothing is lost)
    events_fts      a full-text index over the three fields people search,
                    kept in step with `events` by SQL triggers

Every query in this file uses bound parameters.  There is no string
interpolation of user or server data into SQL anywhere, and there should
never be.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlparse

from kairos import ical
from kairos.config import DATABASE_FILE, ensure_directories
from kairos.models import Calendar, Event

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1

#: Bumped when the full-text index's columns or tokeniser change, which
#: makes the next start rebuild it.
SEARCH_INDEX_VERSION = 1

#: Bumped when a fix means the *events already cached* are wrong or
#: incomplete, so they have to be downloaded again. A cached copy is only as
#: good as the code that fetched it, and a change-token cannot know that the
#: code has since been corrected — it says "the server has not changed",
#: which is true and unhelpful. Raising this clears every change-token once,
#: so the next sync refetches, and nobody has to be told to delete a cache.
#:
#: 2: calendar-query returns an abridged copy of an event on some servers,
#:    and Synology's leaves out the VALARMs. Bodies now come from a
#:    calendar-multiget, so everything fetched before this is missing its
#:    reminders.
#: 3: the same again. Version 2 was stamped on at least one cache by a
#:    development run *before* the corrected build reached it, and an
#:    instance of the old app then kept syncing over the good data. The
#:    stamp is one-shot by design, so those caches were left wrong with no
#:    way back. Re-running it costs one download and is the only way to be
#:    sure; if this happens again the answer is another bump, not a
#:    cleverer check.
FETCH_VERSION = 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS calendars (
    id          TEXT PRIMARY KEY,
    account_id  TEXT NOT NULL,
    name        TEXT NOT NULL,
    colour      TEXT NOT NULL DEFAULT '#3584e4',
    url         TEXT NOT NULL DEFAULT '',
    read_only   INTEGER NOT NULL DEFAULT 0,
    visible     INTEGER NOT NULL DEFAULT 1,
    sync_token  TEXT NOT NULL DEFAULT '',
    default_alarm_minutes INTEGER NOT NULL DEFAULT -1,
    dav_sync_token TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS events (
    calendar_id TEXT NOT NULL,
    uid         TEXT NOT NULL,
    href        TEXT,
    etag        TEXT,
    ics         TEXT NOT NULL,
    summary     TEXT NOT NULL DEFAULT '',
    start_utc   REAL NOT NULL,
    end_utc     REAL NOT NULL,
    all_day     INTEGER NOT NULL DEFAULT 0,
    recurring   INTEGER NOT NULL DEFAULT 0,
    -- Local changes waiting to be pushed: 0 none, 1 created/updated, 2 deleted.
    pending     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (calendar_id, uid)
);

CREATE INDEX IF NOT EXISTS events_by_range
    ON events (calendar_id, start_utc, end_utc);
CREATE INDEX IF NOT EXISTS events_recurring
    ON events (recurring);
CREATE INDEX IF NOT EXISTS events_pending
    ON events (pending);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

#: Full-text search over the three fields a person actually searches.
#:
#: This is an *external content* index: it stores no copy of the text, only
#: the index, and reads the columns back out of ``events`` by rowid.  The
#: triggers below are what keep the two in step — doing it in Python instead
#: would mean remembering to update the index in every method that writes an
#: event, and the one that got forgotten would silently stop being findable.
SEARCH_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS events_fts USING fts5(
    summary, location, description,
    content='events',
    content_rowid='rowid',
    tokenize="unicode61 remove_diacritics 2"
);

CREATE TRIGGER IF NOT EXISTS events_fts_insert AFTER INSERT ON events BEGIN
    INSERT INTO events_fts (rowid, summary, location, description)
    VALUES (new.rowid, new.summary, new.location, new.description);
END;

CREATE TRIGGER IF NOT EXISTS events_fts_delete AFTER DELETE ON events BEGIN
    INSERT INTO events_fts (events_fts, rowid, summary, location, description)
    VALUES ('delete', old.rowid, old.summary, old.location, old.description);
END;

CREATE TRIGGER IF NOT EXISTS events_fts_update AFTER UPDATE ON events BEGIN
    INSERT INTO events_fts (events_fts, rowid, summary, location, description)
    VALUES ('delete', old.rowid, old.summary, old.location, old.description);
    INSERT INTO events_fts (rowid, summary, location, description)
    VALUES (new.rowid, new.summary, new.location, new.description);
END;
"""

# Values for the `pending` column, named so the code reads like prose.
PENDING_NONE = 0
PENDING_SAVE = 1
PENDING_DELETE = 2


def _timestamp(moment: datetime) -> float:
    return moment.timestamp()


def _fts_query(text: str) -> str:
    """Turn what the user typed into an FTS5 MATCH expression.

    Everything typed is treated as literal words, never as query syntax: FTS5
    reads bare ``AND``, ``NOT``, ``*``, ``^``, ``:`` and parentheses as
    operators, so an innocent search for "R&D (draft)" would either match the
    wrong thing or raise a syntax error out of the database. Each word is
    wrapped in double quotes — the FTS5 escape, doubling any quote inside —
    and given a trailing ``*`` so results appear while the word is still
    being typed.

    Returns "" when nothing searchable is left, which the caller reads as "no
    results" rather than "match everything".
    """
    words = [word for word in re.split(r"\s+", text.strip()) if word]
    terms = []
    for word in words:
        cleaned = word.replace('"', '""')
        if cleaned:
            terms.append(f'"{cleaned}"*')
    return " ".join(terms)


def _resource_path(href: str) -> str:
    """An href reduced to its unescaped path, so a URL and a bare path compare equal.

    A server reports a removed event by path; Kairos stored its href as a
    full URL. Only their paths can be matched up.
    """
    return unquote(urlparse(str(href)).path or str(href)).rstrip("/")


class Storage:
    """The database.  One instance per running application.

    All public methods take and return the dataclasses from
    :mod:`kairos.models`; callers never see a row or a cursor.  A single
    connection is shared behind a lock, which is plenty for one desktop app
    and avoids every class of "which thread owns this connection" bug.
    """

    def __init__(self, path: Path | str = DATABASE_FILE) -> None:
        ensure_directories()
        self.path = Path(path)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(str(self.path), check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        # WAL keeps the background sync thread from blocking the UI thread's
        # reads; the busy timeout covers the rare write-write overlap.
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=NORMAL")
        self._connection.execute("PRAGMA busy_timeout=5000")
        self._connection.execute("PRAGMA foreign_keys=ON")
        with self._lock:
            self._connection.executescript(SCHEMA)
            self._add_missing_columns()
            # Order matters. The backfill runs *before* the triggers exist:
            # an UPDATE with the triggers in place would ask the index to
            # remove entries it never had, and FTS5 answers that with
            # "database disk image is malformed".
            self._backfill_search_columns()
            self._connection.executescript(SEARCH_SCHEMA)
            self._rebuild_search_index()
            self._connection.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            self._connection.commit()
        self._refetch_if_stale()
        self.clear_tokens_for_empty_calendars()
        # Parsed events, keyed by (calendar_id, uid, etag-or-length).  Parsing
        # iCalendar is the expensive part of a redraw; this makes repeat views
        # of the same month essentially free.
        self._parse_cache: dict[tuple, Event] = {}

    # ------------------------------------------------------------------
    # Schema upgrades
    # ------------------------------------------------------------------

    def _add_missing_columns(self) -> None:
        """Bring an older database up to date, in place.

        ``location`` and ``description`` were once only inside the stored
        iCalendar. Searching them meant parsing every candidate row, which is
        why search had to be capped and truncated silently. They are columns
        now so the full-text index can reach them.
        """
        present = {row["name"] for row in
                   self._connection.execute("PRAGMA table_info(events)")}
        for column in ("location", "description"):
            if column not in present:
                self._connection.execute(
                    f"ALTER TABLE events ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")

        on_calendars = {row["name"] for row in
                        self._connection.execute("PRAGMA table_info(calendars)")}
        if "default_alarm_minutes" not in on_calendars:
            self._connection.execute(
                "ALTER TABLE calendars ADD COLUMN default_alarm_minutes "
                "INTEGER NOT NULL DEFAULT -1")
        if "dav_sync_token" not in on_calendars:
            self._connection.execute(
                "ALTER TABLE calendars ADD COLUMN dav_sync_token TEXT NOT NULL DEFAULT ''")

    def _backfill_search_columns(self) -> None:
        """Fill in the new columns for events cached by an older version.

        Runs once: afterwards there are no rows left to find. Parsing the
        whole cache is the same work the old search did on a single vague
        query, so even a large calendar pays it only this once.
        """
        rows = self._connection.execute(
            "SELECT rowid, calendar_id, uid, ics FROM events"
            " WHERE location = '' AND description = ''"
        ).fetchall()
        if not rows:
            return

        updates = []
        for row in rows:
            try:
                events = ical.parse_calendar_text(row["ics"], row["calendar_id"])
            except ical.ParseError:
                continue
            if not events:
                continue
            event = events[0]
            if event.location or event.description:
                updates.append((event.location, event.description, row["rowid"]))

        if not updates:
            return
        log.info("indexing %d cached events for search", len(updates))
        self._connection.executemany(
            "UPDATE events SET location = ?, description = ? WHERE rowid = ?",
            updates,
        )

    def _refetch_if_stale(self) -> None:
        """Force one re-download when cached events predate a fetch fix.

        See :data:`FETCH_VERSION`. Clearing the change-tokens is enough: the
        events themselves are left alone until the replacements arrive, so
        nothing disappears in the meantime.
        """
        with self._lock:
            row = self._connection.execute(
                "SELECT value FROM meta WHERE key = 'fetch_version'").fetchone()
            try:
                stored = int(row["value"]) if row else 0
            except (TypeError, ValueError):
                stored = 0
            if stored >= FETCH_VERSION:
                return
            changed = self._connection.execute(
                "UPDATE calendars SET sync_token = '', dav_sync_token = ''"
                " WHERE sync_token != '' OR dav_sync_token != ''").rowcount
            self._connection.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('fetch_version', ?)",
                (str(FETCH_VERSION),))
            self._connection.commit()
        if changed:
            log.info("cached events were fetched by an older version; %d "
                     "calendar(s) will be downloaded again", changed)

    def clear_tokens_for_empty_calendars(self) -> int:
        """Forget the change-token of any calendar that holds no events.

        A token says "you already have everything behind this". A calendar
        with a token and nothing in it is therefore a contradiction, and it
        is a self-sustaining one: the token makes every sync skip the
        download, so the calendar can never fill up.

        It was reachable through a bug — the token was saved before the
        events were fetched, so a single failed fetch stuck the calendar
        that way permanently. The bug is fixed; this repairs the caches it
        already spoiled, and costs one query on start otherwise.
        """
        with self._lock:
            stuck = self._connection.execute(
                "SELECT id, name FROM calendars"
                " WHERE (sync_token != '' OR dav_sync_token != '')"
                " AND id NOT IN (SELECT DISTINCT calendar_id FROM events)"
            ).fetchall()
            if not stuck:
                return 0
            self._connection.executemany(
                "UPDATE calendars SET sync_token = '', dav_sync_token = '' WHERE id = ?",
                [(row["id"],) for row in stuck],
            )
            self._connection.commit()
        for row in stuck:
            log.info("“%s” has a change-token but no events; forgetting the "
                     "token so the next sync downloads them", row["name"])
        return len(stuck)

    def _rebuild_search_index(self) -> None:
        """Populate the index from the events, when it is not already.

        A database written before the index existed has rows the index has
        never seen, and an external-content FTS5 table cannot notice that by
        itself — it would simply never find them. The version marker means
        this costs one query per start rather than a full rebuild, and gives
        a way to force one later if the indexed columns ever change.
        """
        row = self._connection.execute(
            "SELECT value FROM meta WHERE key = 'search_index_version'").fetchone()
        if row is not None and row["value"] == str(SEARCH_INDEX_VERSION):
            return
        self._connection.execute(
            "INSERT INTO events_fts (events_fts) VALUES ('rebuild')")
        self._connection.execute(
            "INSERT OR REPLACE INTO meta (key, value)"
            " VALUES ('search_index_version', ?)", (str(SEARCH_INDEX_VERSION),))

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    # ------------------------------------------------------------------
    # Calendars
    # ------------------------------------------------------------------

    def list_calendars(self) -> list[Calendar]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM calendars ORDER BY name COLLATE NOCASE"
            ).fetchall()
        return [self._row_to_calendar(row) for row in rows]

    def get_calendar(self, calendar_id: str) -> Calendar | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM calendars WHERE id = ?", (calendar_id,)
            ).fetchone()
        return self._row_to_calendar(row) if row else None

    def save_calendar(self, calendar: Calendar) -> None:
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO calendars (id, account_id, name, colour, url,
                                       read_only, visible, sync_token,
                                       default_alarm_minutes, dav_sync_token)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    account_id = excluded.account_id,
                    name       = excluded.name,
                    colour     = excluded.colour,
                    url        = excluded.url,
                    read_only  = excluded.read_only,
                    visible    = excluded.visible,
                    sync_token = excluded.sync_token,
                    default_alarm_minutes = excluded.default_alarm_minutes,
                    dav_sync_token = excluded.dav_sync_token
                """,
                (
                    calendar.id, calendar.account_id, calendar.name, calendar.colour,
                    calendar.url, int(calendar.read_only), int(calendar.visible),
                    calendar.sync_token, int(calendar.default_alarm_minutes),
                    calendar.dav_sync_token,
                ),
            )
            self._connection.commit()

    def delete_calendar(self, calendar_id: str) -> None:
        """Forget a calendar and everything cached for it."""
        with self._lock:
            self._connection.execute("DELETE FROM events WHERE calendar_id = ?", (calendar_id,))
            self._connection.execute("DELETE FROM calendars WHERE id = ?", (calendar_id,))
            self._connection.commit()
        self._parse_cache = {
            key: value for key, value in self._parse_cache.items() if key[0] != calendar_id
        }

    def delete_calendars_for_account(self, account_id: str) -> None:
        for calendar in self.list_calendars():
            if calendar.account_id == account_id:
                self.delete_calendar(calendar.id)

    @staticmethod
    def _row_to_calendar(row: sqlite3.Row) -> Calendar:
        return Calendar(
            id=row["id"],
            account_id=row["account_id"],
            name=row["name"],
            colour=row["colour"],
            url=row["url"],
            read_only=bool(row["read_only"]),
            visible=bool(row["visible"]),
            sync_token=row["sync_token"],
            default_alarm_minutes=(row["default_alarm_minutes"]
                                   if "default_alarm_minutes" in row.keys() else -1),
            dav_sync_token=(row["dav_sync_token"]
                            if "dav_sync_token" in row.keys() else ""),
        )

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    def save_event(self, event: Event, *, pending: int = PENDING_NONE) -> None:
        """Insert or update one event.

        ``pending`` records whether this change still needs pushing to a
        server; :mod:`kairos.sync` looks for those rows on its next run.
        """
        ics = event.raw_ics or ical.to_ical_text(event)
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO events (calendar_id, uid, href, etag, ics, summary,
                                    location, description,
                                    start_utc, end_utc, all_day, recurring, pending)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(calendar_id, uid) DO UPDATE SET
                    href        = excluded.href,
                    etag        = excluded.etag,
                    ics         = excluded.ics,
                    summary     = excluded.summary,
                    location    = excluded.location,
                    description = excluded.description,
                    start_utc   = excluded.start_utc,
                    end_utc     = excluded.end_utc,
                    all_day     = excluded.all_day,
                    recurring   = excluded.recurring,
                    pending     = excluded.pending
                """,
                (
                    event.calendar_id, event.uid, event.href, event.etag, ics,
                    event.summary, event.location, event.description,
                    _timestamp(event.start), _timestamp(event.end),
                    int(event.all_day), int(event.is_recurring), pending,
                ),
            )
            self._connection.commit()
        self._parse_cache.pop((event.calendar_id, event.uid), None)

    def save_events(self, events: list[Event]) -> None:
        """Bulk-insert events fetched from a server (never pending)."""
        rows = []
        for event in events:
            ics = event.raw_ics or ical.to_ical_text(event)
            rows.append((
                event.calendar_id, event.uid, event.href, event.etag, ics,
                event.summary, event.location, event.description,
                _timestamp(event.start), _timestamp(event.end),
                int(event.all_day), int(event.is_recurring), PENDING_NONE,
            ))
        if not rows:
            return
        with self._lock:
            self._connection.executemany(
                """
                INSERT INTO events (calendar_id, uid, href, etag, ics, summary,
                                    location, description,
                                    start_utc, end_utc, all_day, recurring, pending)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(calendar_id, uid) DO UPDATE SET
                    href        = excluded.href,
                    etag        = excluded.etag,
                    ics         = excluded.ics,
                    summary     = excluded.summary,
                    location    = excluded.location,
                    description = excluded.description,
                    start_utc   = excluded.start_utc,
                    end_utc     = excluded.end_utc,
                    all_day     = excluded.all_day,
                    recurring   = excluded.recurring,
                    pending     = excluded.pending
                """,
                rows,
            )
            self._connection.commit()
        for event in events:
            self._parse_cache.pop((event.calendar_id, event.uid), None)

    def get_event(self, calendar_id: str, uid: str) -> Event | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM events WHERE calendar_id = ? AND uid = ?",
                (calendar_id, uid),
            ).fetchone()
        return self._row_to_event(row) if row else None

    def mark_event_deleted(self, calendar_id: str, uid: str) -> None:
        """Hide an event locally and queue the deletion for the next sync."""
        with self._lock:
            self._connection.execute(
                "UPDATE events SET pending = ? WHERE calendar_id = ? AND uid = ?",
                (PENDING_DELETE, calendar_id, uid),
            )
            self._connection.commit()
        self._parse_cache.pop((calendar_id, uid), None)

    def forget_event(self, calendar_id: str, uid: str) -> None:
        """Remove an event from the cache outright (it is gone server-side)."""
        with self._lock:
            self._connection.execute(
                "DELETE FROM events WHERE calendar_id = ? AND uid = ?",
                (calendar_id, uid),
            )
            self._connection.commit()
        self._parse_cache.pop((calendar_id, uid), None)

    def clear_pending(self, calendar_id: str, uid: str) -> None:
        with self._lock:
            self._connection.execute(
                "UPDATE events SET pending = ? WHERE calendar_id = ? AND uid = ?",
                (PENDING_NONE, calendar_id, uid),
            )
            self._connection.commit()

    def pending_changes(self) -> list[tuple[Event, int]]:
        """Every locally-changed event still waiting to reach a server."""
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM events WHERE pending != ?", (PENDING_NONE,)
            ).fetchall()
        return [(self._row_to_event(row), row["pending"]) for row in rows]

    def replace_calendar_events(self, calendar_id: str, events: list[Event]) -> None:
        """Make the cache for one calendar match a full server download.

        Rows with local changes are left alone: an unpushed edit must survive
        a refresh, or the user silently loses work.
        """
        keep = {event.uid for event in events}
        with self._lock:
            existing = self._connection.execute(
                "SELECT uid, pending FROM events WHERE calendar_id = ?", (calendar_id,)
            ).fetchall()
            stale = [
                row["uid"] for row in existing
                if row["uid"] not in keep and row["pending"] == PENDING_NONE
            ]
            if stale:
                self._connection.executemany(
                    "DELETE FROM events WHERE calendar_id = ? AND uid = ?",
                    [(calendar_id, uid) for uid in stale],
                )
            self._connection.commit()
        for uid in stale:
            self._parse_cache.pop((calendar_id, uid), None)

        locally_changed = {
            row["uid"] for row in existing if row["pending"] != PENDING_NONE
        }
        self.save_events([e for e in events if e.uid not in locally_changed])

    def apply_remote_changes(self, calendar_id: str, changed: list[Event],
                             removed_hrefs: list[str]) -> tuple[int, int]:
        """Apply what an incremental sync reported: some events changed, some gone.

        Rows with local changes are left alone, exactly as a full refresh
        leaves them — an unpushed edit must survive whatever the server says,
        including that the event was deleted. Returns ``(updated, removed)``.
        """
        gone = {_resource_path(href) for href in removed_hrefs}
        with self._lock:
            rows = self._connection.execute(
                "SELECT uid, href, pending FROM events WHERE calendar_id = ?",
                (calendar_id,)).fetchall()
            locally_changed = {row["uid"] for row in rows if row["pending"] != PENDING_NONE}
            doomed = [row["uid"] for row in rows
                      if row["href"] and row["pending"] == PENDING_NONE
                      and _resource_path(row["href"]) in gone]
            if doomed:
                self._connection.executemany(
                    "DELETE FROM events WHERE calendar_id = ? AND uid = ?",
                    [(calendar_id, uid) for uid in doomed])
            self._connection.commit()
        for uid in doomed:
            self._parse_cache.pop((calendar_id, uid), None)

        fresh = [event for event in changed if event.uid not in locally_changed]
        if fresh:
            self.save_events(fresh)
        return len(fresh), len(doomed)

    def events_in_range(
        self, calendar_ids: list[str], start: datetime, end: datetime
    ) -> list[Event]:
        """Events that might appear between ``start`` and ``end``.

        Non-repeating events are filtered by the database.  Repeating ones are
        always returned — only :mod:`kairos.recurrence` can say whether a rule
        actually produces an occurrence in the window, and there are rarely
        enough of them for it to matter.
        """
        if not calendar_ids:
            return []
        placeholders = ",".join("?" for _ in calendar_ids)
        query = f"""
            SELECT * FROM events
             WHERE calendar_id IN ({placeholders})
               AND pending != ?
               AND (recurring = 1 OR (start_utc < ? AND end_utc > ?))
        """
        arguments = [*calendar_ids, PENDING_DELETE, _timestamp(end), _timestamp(start)]
        with self._lock:
            rows = self._connection.execute(query, arguments).fetchall()
        return [self._row_to_event(row) for row in rows]

    def search_events(self, text: str, calendar_ids: list[str], limit: int = 200) -> list[Event]:
        """Events whose title, place or notes match ``text``.

        Answered from the full-text index, so the database does all of it and
        no event is parsed to find out whether it matched.

        This used to be a ``LIKE '%term%'`` over the stored iCalendar followed
        by parsing each candidate to check the fields a person actually meant
        — because the raw text matches almost anything, every event carrying
        ``CALSCALE:GREGORIAN``. That could not use an index, and it gave up
        after ``SCAN_LIMIT`` rows *without saying so*, quietly returning some
        of the matches as though they were all of them.

        Words match from the start rather than anywhere inside: "cin" finds
        "Cinema", but "ine" no longer does. That is what an index can answer
        quickly, and what people expect of a search box.
        """
        query = _fts_query(text)
        if not query or not calendar_ids:
            return []

        placeholders = ",".join("?" for _ in calendar_ids)
        with self._lock:
            rows = self._connection.execute(
                # The matches are gathered by a subquery rather than joined.
                # Written as a join, SQLite drives the query from `events`
                # using the calendar index and rescans the whole full-text
                # table for every row — 80ms where this takes 1ms, and worse
                # the larger the calendar. As a subquery the index is
                # consulted exactly once.
                f"""
                SELECT * FROM events
                 WHERE rowid IN (SELECT rowid FROM events_fts
                                  WHERE events_fts MATCH ?)
                   AND calendar_id IN ({placeholders})
                   AND pending != ?
                 ORDER BY start_utc DESC
                 LIMIT ?
                """,
                [query, *calendar_ids, PENDING_DELETE, limit],
            ).fetchall()
        return [self._row_to_event(row) for row in rows]

    def count_events(self, calendar_id: str) -> int:
        with self._lock:
            row = self._connection.execute(
                "SELECT COUNT(*) AS n FROM events WHERE calendar_id = ? AND pending != ?",
                (calendar_id, PENDING_DELETE),
            ).fetchone()
        return int(row["n"])

    # ------------------------------------------------------------------
    # Row -> Event
    # ------------------------------------------------------------------

    def _row_to_event(self, row: sqlite3.Row) -> Event:
        """Rebuild an Event from its stored iCalendar text, with caching."""
        key = (row["calendar_id"], row["uid"])
        stamp = (row["etag"], len(row["ics"]), row["pending"])
        cached = self._parse_cache.get(key)
        if cached is not None and getattr(cached, "_cache_stamp", None) == stamp:
            return cached

        try:
            events = ical.parse_calendar_text(
                row["ics"], row["calendar_id"], href=row["href"], etag=row["etag"]
            )
            event = events[0] if events else None
        except ical.ParseError as exc:
            log.warning("cached event %s is unreadable (%s)", row["uid"], exc)
            event = None

        if event is None:
            # Fall back to the indexed columns so a corrupt row still shows
            # something the user can select and delete.
            from kairos.models import local_timezone
            event = Event(
                uid=row["uid"],
                calendar_id=row["calendar_id"],
                summary=row["summary"] or "(Unreadable event)",
                start=datetime.fromtimestamp(row["start_utc"], tz=local_timezone()),
                end=datetime.fromtimestamp(row["end_utc"], tz=local_timezone()),
                all_day=bool(row["all_day"]),
            )

        event.raw_ics = row["ics"]
        event.href = row["href"]
        event.etag = row["etag"]
        event.dirty = row["pending"] != PENDING_NONE
        # Attribute rather than a dataclass field: it is a cache detail, not
        # part of what an Event *is*.
        event._cache_stamp = stamp  # type: ignore[attr-defined]

        if len(self._parse_cache) > 5000:
            self._parse_cache.clear()
        self._parse_cache[key] = event
        return event
