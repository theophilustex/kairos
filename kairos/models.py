"""The plain data types Kairos passes around.

These are ordinary dataclasses with no behaviour beyond a few conveniences.
Anything that knows about iCalendar lives in :mod:`kairos.ical`; anything that
knows about SQLite lives in :mod:`kairos.storage`.  Keeping those concerns out
of here is what makes the rest of the app easy to follow.

**One rule about time, and it matters.**  Every ``datetime`` in this module is
timezone-aware, always.  All-day events are *not* stored as ``date`` objects;
they are stored as aware datetimes at local midnight, with ``all_day = True``
and an *exclusive* end — a single all-day event on the 5th runs from
``05 00:00`` to ``06 00:00``.  That matches iCalendar's own convention and it
means view code never has to ask "is this a date or a datetime?".
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, tzinfo
from typing import Iterable


def local_timezone() -> tzinfo:
    """The machine's current timezone, as an aware tzinfo."""
    return datetime.now().astimezone().tzinfo  # type: ignore[return-value]


def now_local() -> datetime:
    return datetime.now(tz=local_timezone())


def to_local(moment: datetime) -> datetime:
    """Convert any aware datetime to local time (naive input is assumed local)."""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=local_timezone())
    return moment.astimezone(local_timezone())


def start_of_day(day: date) -> datetime:
    """Local midnight at the start of ``day``."""
    return datetime(day.year, day.month, day.day, tzinfo=local_timezone())


def new_uid() -> str:
    """A fresh iCalendar UID.  Random, with our domain for good manners."""
    return f"{uuid.uuid4()}@kairos.local"


# --------------------------------------------------------------------------
# Accounts and calendars
# --------------------------------------------------------------------------

#: Account kinds Kairos understands.
CALDAV = "caldav"
LOCAL = "local"


@dataclass
class Account:
    """A server (or the local machine) that calendars come from.

    The password is *not* a field here on purpose — it lives in the keyring,
    keyed by :attr:`id`.  See :mod:`kairos.security`.
    """

    id: str
    name: str
    kind: str = CALDAV            # CALDAV or LOCAL
    url: str = ""                 # base CalDAV/WebDAV URL
    username: str = ""
    verify_tls: bool = True
    enabled: bool = True

    @property
    def is_local(self) -> bool:
        return self.kind == LOCAL

    def to_json(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "url": self.url,
            "username": self.username,
            "verify_tls": self.verify_tls,
            "enabled": self.enabled,
        }

    @classmethod
    def from_json(cls, raw: dict) -> "Account":
        return cls(
            id=str(raw.get("id") or uuid.uuid4()),
            name=str(raw.get("name") or "Account"),
            kind=raw.get("kind") if raw.get("kind") in (CALDAV, LOCAL) else CALDAV,
            url=str(raw.get("url") or ""),
            username=str(raw.get("username") or ""),
            verify_tls=bool(raw.get("verify_tls", True)),
            enabled=bool(raw.get("enabled", True)),
        )


@dataclass
class Calendar:
    """One calendar the user can see events from and (usually) write to."""

    id: str                       # stable, unique within Kairos
    account_id: str
    name: str
    colour: str = "#3584e4"
    url: str = ""                 # the calendar's own collection URL
    read_only: bool = False
    visible: bool = True
    sync_token: str = ""          # server's ctag/sync-token, for cheap polling

    @property
    def is_local(self) -> bool:
        return self.account_id == "local"

    @property
    def writable(self) -> bool:
        return not self.read_only


# --------------------------------------------------------------------------
# Events
# --------------------------------------------------------------------------

#: Offsets, largest first, used to put a reminder into words.
_OFFSET_UNITS = ((10080, "week"), (1440, "day"), (60, "hour"), (1, "minute"))


def describe_offset(minutes: int) -> str:
    """Put a reminder offset into words: 7200 -> "5 days before".

    Picks the largest unit the offset divides into exactly, so a reminder the
    user entered as "2 weeks" reads back as "2 weeks" rather than as
    "20160 minutes".  Anything that does not divide evenly stays in the
    largest unit that does, down to minutes.
    """
    if minutes == 0:
        return "At time of event"

    after = minutes < 0
    value = abs(minutes)
    for size, noun in _OFFSET_UNITS:
        if value >= size and value % size == 0:
            count = value // size
            phrase = f"{count} {noun}{'s' if count != 1 else ''}"
            break
    else:
        phrase = f"{value} minute{'s' if value != 1 else ''}"

    return f"{phrase} after the start" if after else f"{phrase} before"


@dataclass(frozen=True)
class Alarm:
    """A reminder, expressed as "N minutes before the event starts".

    iCalendar allows far more than this (absolute triggers, offsets from the
    end, repeating alarms).  Kairos reads those without complaint but only
    *writes* the relative-to-start form, because that is the only kind the
    notification scheduler can reason about clearly.
    """

    minutes_before: int
    description: str = ""

    #: The offsets offered in the event editor's drop-down.
    PRESETS = (
        (0, "At time of event"),
        (5, "5 minutes before"),
        (10, "10 minutes before"),
        (15, "15 minutes before"),
        (30, "30 minutes before"),
        (60, "1 hour before"),
        (120, "2 hours before"),
        (1440, "1 day before"),
        (2880, "2 days before"),
        (10080, "1 week before"),
    )

    def label(self) -> str:
        """How this reminder is written in the editor and the popover."""
        for minutes, text in self.PRESETS:
            if minutes == self.minutes_before:
                return text
        return describe_offset(self.minutes_before)


@dataclass
class Event:
    """A calendar entry, as Kairos holds it in memory.

    For a repeating event this describes the *series*: :attr:`start` and
    :attr:`end` are the first occurrence and :attr:`rrule` says how it
    repeats.  Concrete dates come out of :mod:`kairos.recurrence` as
    :class:`Occurrence` objects.
    """

    uid: str
    calendar_id: str
    summary: str
    start: datetime
    end: datetime
    all_day: bool = False
    description: str = ""
    location: str = ""
    alarms: list[Alarm] = field(default_factory=list)
    rrule: str = ""               # e.g. "FREQ=WEEKLY;BYDAY=MO,WE"
    # Bookkeeping for talking to the server.  None means "not on a server yet".
    href: str | None = None       # path of the .ics resource on the server
    etag: str | None = None       # server's version marker, for safe writes
    sequence: int = 0
    last_modified: datetime | None = None
    # The original iCalendar text, exactly as the server sent it.  Kept so
    # that recurrence expansion sees the real rule set — EXDATE, RDATE and
    # UNTIL clauses included — rather than our simplified view of it.  Empty
    # for events Kairos has just created; :mod:`kairos.ical` can regenerate it.
    raw_ics: str = ""
    # Set when a change could not be pushed (offline, or the server said no).
    # The sync worker retries these.
    dirty: bool = False

    # -- convenience ------------------------------------------------------

    @property
    def duration(self) -> timedelta:
        return self.end - self.start

    @property
    def is_recurring(self) -> bool:
        return bool(self.rrule)

    @property
    def first_day(self) -> date:
        return self.start.date()

    @property
    def last_day(self) -> date:
        """Inclusive last day, accounting for the exclusive all-day end."""
        if self.all_day:
            return (self.end - timedelta(days=1)).date()
        return self.end.date()

    def spans_days(self) -> int:
        return (self.last_day - self.first_day).days + 1

    def copy(self, **changes) -> "Event":
        """A modified copy.  ``event.copy(summary="New title")``."""
        return replace(self, **changes)

    @classmethod
    def new(
        cls,
        calendar_id: str,
        start: datetime,
        end: datetime,
        summary: str = "",
        all_day: bool = False,
    ) -> "Event":
        """A blank event, ready for the editor to fill in."""
        return cls(
            uid=new_uid(),
            calendar_id=calendar_id,
            summary=summary,
            start=start,
            end=end,
            all_day=all_day,
        )


@dataclass(frozen=True)
class Occurrence:
    """One concrete appearance of an event on the calendar.

    A non-repeating event has exactly one occurrence with the same times as
    the event itself.  A weekly meeting has one per week.  Views only ever
    deal in occurrences; they never expand recurrence rules themselves.

    **Occurrence times are always local**, unlike :class:`Event`, which keeps
    whatever zone the server used.  :func:`kairos.recurrence.expand` does the
    conversion, so a view can treat ``start.date()`` as "the day cell this
    belongs in" without further thought.
    """

    event: Event
    start: datetime
    end: datetime

    # Pass-through accessors so view code can stay short.
    @property
    def summary(self) -> str:
        return self.event.summary

    @property
    def all_day(self) -> bool:
        return self.event.all_day

    @property
    def calendar_id(self) -> str:
        return self.event.calendar_id

    @property
    def uid(self) -> str:
        return self.event.uid

    @property
    def first_day(self) -> date:
        return self.start.date()

    @property
    def last_day(self) -> date:
        if self.all_day:
            return (self.end - timedelta(days=1)).date()
        return self.end.date()

    def covers(self, day: date) -> bool:
        return self.first_day <= day <= self.last_day

    def sort_key(self) -> tuple:
        """Order occurrences the way people expect to read them.

        All-day and multi-day entries first (they act like banners), then
        timed events by start, then alphabetically so the order is stable
        between redraws.
        """
        is_banner = self.all_day or self.first_day != self.last_day
        return (0 if is_banner else 1, self.start, self.summary.lower())


def occurrences_on(occurrences: Iterable[Occurrence], day: date) -> list[Occurrence]:
    """Filter to the occurrences visible on ``day``, in display order."""
    return sorted((o for o in occurrences if o.covers(day)), key=Occurrence.sort_key)
