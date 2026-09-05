"""Translation between :class:`~kairos.models.Event` and iCalendar text.

This is the only module that knows what a ``VEVENT`` looks like.  It has two
halves that mirror each other:

    parse_event()      iCalendar component  ->  Event
    build_calendar()   Event                ->  a full VCALENDAR ready to PUT

The awkward parts of RFC 5545 are handled here so nothing else has to think
about them:

* ``DTSTART;VALUE=DATE`` means an all-day event, and its ``DTEND`` is
  *exclusive*.  We keep that convention (see :mod:`kairos.models`) but convert
  the bare dates to aware local-midnight datetimes.
* A missing ``DTEND`` means "use ``DURATION``", and a missing duration means
  the event ends when it starts (or covers one whole day, if all-day).
* Floating times — no timezone at all — are interpreted as local time, which
  is what every other calendar client does.
* ``VALARM`` triggers can be durations relative to start/end or absolute
  timestamps.  We normalise all of them to "minutes before start".
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

import icalendar

from kairos import APP_NAME, VERSION
from kairos.models import Alarm, Event, local_timezone, new_uid
from kairos.security import sanitise_text

log = logging.getLogger(__name__)

#: Written into every calendar object we upload, so servers and other clients
#: can tell who made it.
PRODID = f"-//Kairos//{APP_NAME} {VERSION}//EN"


class ParseError(Exception):
    """Raised when a VEVENT is too broken to turn into an Event."""


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------

def _as_datetime(value) -> tuple[datetime, bool]:
    """Normalise an icalendar date/datetime to ``(aware datetime, all_day)``.

    A bare ``date`` becomes local midnight.  Nothing is added for a DTEND:
    iCalendar already writes all-day ends exclusively (an event covering the
    5th to the 7th has ``DTEND;VALUE=DATE:20260308``), which is exactly the
    convention :mod:`kairos.models` uses.
    """
    if isinstance(value, datetime):
        if value.tzinfo is None:
            # A "floating" time: means local wall-clock time everywhere.
            value = value.replace(tzinfo=local_timezone())
        return value, False
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=local_timezone()), True
    raise ParseError(f"unsupported date value: {value!r}")


def _trigger_to_minutes_before(trigger, event_start: datetime, event_end: datetime) -> int | None:
    """Convert any VALARM trigger into "minutes before the event starts".

    Returns ``None`` for triggers we cannot express that way, which the caller
    then skips rather than guessing at.
    """
    value = getattr(trigger, "dt", trigger)

    if isinstance(value, timedelta):
        # Relative triggers are almost always negative ("-PT15M").  The
        # RELATED=END parameter moves the anchor to the event's end.
        related = str(trigger.params.get("RELATED", "START")).upper() if hasattr(trigger, "params") else "START"
        anchor = event_end if related == "END" else event_start
        fire_at = anchor + value
        return int(round((event_start - fire_at).total_seconds() / 60))

    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=local_timezone())
        return int(round((event_start - value).total_seconds() / 60))

    return None


def _read_alarms(component, start: datetime, end: datetime) -> list[Alarm]:
    alarms: list[Alarm] = []
    for sub in component.walk("VALARM"):
        action = str(sub.get("ACTION", "DISPLAY")).upper()
        # EMAIL and AUDIO alarms are the server's business, not ours; we only
        # raise desktop notifications.
        if action not in ("DISPLAY", "AUDIO"):
            continue
        trigger = sub.get("TRIGGER")
        if trigger is None:
            continue
        minutes = _trigger_to_minutes_before(trigger, start, end)
        if minutes is None:
            continue
        alarms.append(Alarm(minutes_before=minutes,
                            description=sanitise_text(str(sub.get("DESCRIPTION", "")), max_length=200)))

    # De-duplicate while keeping order; some servers repeat alarms.
    unique: list[Alarm] = []
    for alarm in alarms:
        if alarm.minutes_before not in [a.minutes_before for a in unique]:
            unique.append(alarm)
    return unique


def parse_event(component, calendar_id: str, *, href: str | None = None,
                etag: str | None = None) -> Event:
    """Turn one ``VEVENT`` component into an :class:`Event`."""
    dtstart = component.get("DTSTART")
    if dtstart is None:
        raise ParseError("event has no DTSTART")
    start, all_day = _as_datetime(dtstart.dt)

    dtend = component.get("DTEND")
    duration = component.get("DURATION")
    if dtend is not None:
        end, _ = _as_datetime(dtend.dt)
    elif duration is not None:
        end = start + duration.dt
    elif all_day:
        end = start + timedelta(days=1)
    else:
        end = start

    # Defend against servers that send an end before the start.
    if end < start:
        end = start + (timedelta(days=1) if all_day else timedelta(0))

    rrule = component.get("RRULE")
    if rrule is not None:
        rrule_text = rrule.to_ical().decode("utf-8", "replace")
    else:
        rrule_text = ""

    last_modified = component.get("LAST-MODIFIED")
    if last_modified is not None:
        try:
            modified_at, _ = _as_datetime(last_modified.dt)
        except ParseError:
            modified_at = None
    else:
        modified_at = None

    try:
        sequence = int(component.get("SEQUENCE", 0))
    except (TypeError, ValueError):
        sequence = 0

    return Event(
        uid=sanitise_text(str(component.get("UID") or ""), max_length=255) or new_uid(),
        calendar_id=calendar_id,
        summary=sanitise_text(str(component.get("SUMMARY", "")), max_length=500) or "(No title)",
        start=start,
        end=end,
        all_day=all_day,
        description=sanitise_text(str(component.get("DESCRIPTION", ""))),
        location=sanitise_text(str(component.get("LOCATION", "")), max_length=500),
        alarms=_read_alarms(component, start, end),
        rrule=rrule_text,
        href=href,
        etag=etag,
        sequence=sequence,
        last_modified=modified_at,
    )


def parse_calendar_text(text: str, calendar_id: str, *, href: str | None = None,
                        etag: str | None = None) -> list[Event]:
    """Parse a whole ``.ics`` document into events.

    A single resource on a CalDAV server may hold several VEVENTs — a
    recurring series plus its modified instances.  We keep the master (the one
    without ``RECURRENCE-ID``) and ignore the overrides, which is a documented
    limitation rather than an accident; see the README.
    """
    try:
        calendar = icalendar.Calendar.from_ical(text)
    except Exception as exc:
        raise ParseError(f"could not parse calendar data: {exc}") from exc

    events: list[Event] = []
    for component in calendar.walk("VEVENT"):
        if component.get("RECURRENCE-ID") is not None:
            continue
        try:
            events.append(parse_event(component, calendar_id, href=href, etag=etag))
        except ParseError as exc:
            log.warning("skipping unreadable event in %s: %s", href or calendar_id, exc)
    return events


# --------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------

def build_component(event: Event) -> icalendar.Event:
    """Turn an :class:`Event` back into a ``VEVENT`` component."""
    component = icalendar.Event()
    component.add("UID", event.uid)
    component.add("SUMMARY", event.summary or "(No title)")
    component.add("DTSTAMP", datetime.now(tz=local_timezone()))
    component.add("SEQUENCE", event.sequence)

    if event.all_day:
        # Write bare dates, and remember that DTEND is exclusive.
        component.add("DTSTART", event.start.date())
        component.add("DTEND", event.end.date())
    else:
        component.add("DTSTART", event.start)
        component.add("DTEND", event.end)

    if event.description:
        component.add("DESCRIPTION", event.description)
    if event.location:
        component.add("LOCATION", event.location)
    if event.rrule:
        # A rule we cannot re-serialise came from a server that sent us
        # something odd.  Dropping the repeat is much better than refusing to
        # write the event at all, which would strand the user's edit.
        try:
            component.add("RRULE", icalendar.prop.vRecur.from_ical(event.rrule))
        except (ValueError, TypeError) as exc:
            log.warning("dropping unusable repeat rule %r on %s (%s)",
                        event.rrule, event.uid, exc)

    for alarm in event.alarms:
        sub = icalendar.Alarm()
        sub.add("ACTION", "DISPLAY")
        sub.add("DESCRIPTION", alarm.description or event.summary or "Reminder")
        sub.add("TRIGGER", timedelta(minutes=-alarm.minutes_before))
        component.add_component(sub)

    return component


def build_calendar(events: Event | list[Event]) -> icalendar.Calendar:
    """Wrap one or more events in a ``VCALENDAR``."""
    if isinstance(events, Event):
        events = [events]
    calendar = icalendar.Calendar()
    calendar.add("PRODID", PRODID)
    calendar.add("VERSION", "2.0")
    calendar.add("CALSCALE", "GREGORIAN")
    for event in events:
        calendar.add_component(build_component(event))
    return calendar


def to_ical_text(events: Event | list[Event]) -> str:
    """The finished ``.ics`` document, as text ready to upload or cache."""
    return build_calendar(events).to_ical().decode("utf-8")


# --------------------------------------------------------------------------
# Recurrence rules, in human words
# --------------------------------------------------------------------------

#: The repeat options offered in the editor, as (RRULE, label) pairs.  Adding
#: another is a one-line change here and nowhere else.
REPEAT_PRESETS: tuple[tuple[str, str], ...] = (
    ("", "Does not repeat"),
    ("FREQ=DAILY", "Every day"),
    ("FREQ=WEEKLY", "Every week"),
    ("FREQ=WEEKLY;INTERVAL=2", "Every 2 weeks"),
    ("FREQ=MONTHLY", "Every month"),
    ("FREQ=YEARLY", "Every year"),
    ("FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR", "Every weekday"),
)


def describe_rrule(rrule: str) -> str:
    """A short human description of a repeat rule, for showing in lists."""
    if not rrule:
        return "Does not repeat"
    for preset, label in REPEAT_PRESETS:
        if preset and preset == rrule.upper().rstrip(";"):
            return label

    parts = dict(
        piece.split("=", 1)
        for piece in rrule.upper().split(";")
        if "=" in piece
    )
    frequency = parts.get("FREQ", "")
    interval = parts.get("INTERVAL", "1")
    nouns = {"DAILY": "day", "WEEKLY": "week", "MONTHLY": "month",
             "YEARLY": "year", "HOURLY": "hour", "MINUTELY": "minute"}
    noun = nouns.get(frequency)
    if not noun:
        return "Custom repeat"

    if interval == "1":
        description = f"Every {noun}"
    else:
        description = f"Every {interval} {noun}s"
    if "COUNT" in parts:
        description += f", {parts['COUNT']} times"
    elif "UNTIL" in parts:
        description += f", until {parts['UNTIL'][:8]}"
    return description
