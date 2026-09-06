"""The offline cache: a small SQLite database of calendars and events.

Kairos never asks the network to draw a screen.  Everything the views need
comes from here, and :mod:`kairos.sync` refreshes it in the background.  That
is what makes the app feel instant and what makes it work on a train.

The schema is deliberately tiny — three tables, no migrations framework, no
ORM:

    accounts_meta   one row per calendar-source, for sync bookkeeping
    calendars       one row per calendar
    events          one row per event, holding both the parsed summary fields
                    (so range queries are fast) and the original iCalendar
                    text (so nothing is lost)

Every query in this file uses bound parameters.  There is no string
interpolation of user or server data into SQL anywhere, and there should
never be.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from kairos import ical
from kairos.config import DATABASE_FILE, ensure_directories
from kairos.models import Calendar, Event

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS calendars (
    id          TEXT PRIMARY KEY,
    account_id  TEXT NOT NULL,
    name        TEXT NOT NULL,
    colour      TEXT NOT NULL DEFAULT '#3584e4',
    url         TEXT NOT NULL DEFAULT '',
    read_only   INTEGER NOT NULL DEFAULT 0,
    visible     INTEGER NOT NULL DEFAULT 1,
    sync_token  TEXT NOT NULL DEFAULT ''
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

# Values for the `pending` column, named so the code reads like prose.
PENDING_NONE = 0
PENDING_SAVE = 1
PENDING_DELETE = 2


def _timestamp(moment: datetime) -> float:
    return moment.timestamp()


class Storage:
    """The database.  One instance per running application.

    All public methods take and return the dataclasses from
    :mod:`kairos.models`; callers never see a row or a cursor.  A single
    connection is shared behind a lock, which is plenty for one desktop app
    and avoids every class of "which thread owns this connection" bug.
    """

    #: How many candidate rows a search will parse before giving up. A term
    #: that appears in the iCalendar boilerplate matches every row, so this is
    #: what stops one vague search from parsing the whole calendar.
    SCAN_LIMIT = 2000

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
            self._connection.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            self._connection.commit()
        # Parsed events, keyed by (calendar_id, uid, etag-or-length).  Parsing
        # iCalendar is the expensive part of a redraw; this makes repeat views
        # of the same month essentially free.
        self._parse_cache: dict[tuple, Event] = {}

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
                                       read_only, visible, sync_token)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    account_id = excluded.account_id,
                    name       = excluded.name,
                    colour     = excluded.colour,
                    url        = excluded.url,
                    read_only  = excluded.read_only,
                    visible    = excluded.visible,
                    sync_token = excluded.sync_token
                """,
                (
                    calendar.id, calendar.account_id, calendar.name, calendar.colour,
                    calendar.url, int(calendar.read_only), int(calendar.visible),
                    calendar.sync_token,
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
                                    start_utc, end_utc, all_day, recurring, pending)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(calendar_id, uid) DO UPDATE SET
                    href      = excluded.href,
                    etag      = excluded.etag,
                    ics       = excluded.ics,
                    summary   = excluded.summary,
                    start_utc = excluded.start_utc,
                    end_utc   = excluded.end_utc,
                    all_day   = excluded.all_day,
                    recurring = excluded.recurring,
                    pending   = excluded.pending
                """,
                (
                    event.calendar_id, event.uid, event.href, event.etag, ics,
                    event.summary, _timestamp(event.start), _timestamp(event.end),
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
                event.summary, _timestamp(event.start), _timestamp(event.end),
                int(event.all_day), int(event.is_recurring), PENDING_NONE,
            ))
        if not rows:
            return
        with self._lock:
            self._connection.executemany(
                """
                INSERT INTO events (calendar_id, uid, href, etag, ics, summary,
                                    start_utc, end_utc, all_day, recurring, pending)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(calendar_id, uid) DO UPDATE SET
                    href      = excluded.href,
                    etag      = excluded.etag,
                    ics       = excluded.ics,
                    summary   = excluded.summary,
                    start_utc = excluded.start_utc,
                    end_utc   = excluded.end_utc,
                    all_day   = excluded.all_day,
                    recurring = excluded.recurring,
                    pending   = excluded.pending
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
        """Events whose title, place or notes contain ``text``.

        Two stages, and the second one matters. SQLite does the cheap part —
        narrowing to rows whose stored iCalendar mentions the text at all —
        and then each candidate is parsed and checked against the fields a
        person actually meant.

        Without that second stage every event matches almost anything, because
        the iCalendar boilerplate is searched too: every event carries
        ``CALSCALE:GREGORIAN``, so searching for "re" once returned the entire
        calendar.

        ``SCAN_LIMIT`` bounds the work: a term that prefilters to thousands of
        rows stops being examined once enough real matches are found.
        """
        text = text.strip()
        if not text or not calendar_ids:
            return []

        placeholders = ",".join("?" for _ in calendar_ids)
        # LIKE with an escaped pattern; % and _ from the user are literal.
        pattern = "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        query = f"""
            SELECT * FROM events
             WHERE calendar_id IN ({placeholders})
               AND pending != ?
               AND (summary LIKE ? ESCAPE '\\' OR ics LIKE ? ESCAPE '\\')
             ORDER BY start_utc DESC
             LIMIT ?
        """
        with self._lock:
            rows = self._connection.execute(
                query, [*calendar_ids, PENDING_DELETE, pattern, pattern, self.SCAN_LIMIT]
            ).fetchall()

        needle = text.lower()
        found: list[Event] = []
        for row in rows:
            event = self._row_to_event(row)
            if any(needle in field.lower()
                   for field in (event.summary, event.location, event.description)):
                found.append(event)
                if len(found) >= limit:
                    break
        return found

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
