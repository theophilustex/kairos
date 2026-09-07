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
from datetime import date, datetime, timedelta, timezone

import icalendar

from kairos import APP_NAME, VERSION
from kairos.models import Alarm, Event, local_timezone, new_uid
from kairos.security import find_url as security_find_url, sanitise_text

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
        # Only http(s) survives: this is launched in a browser, and the
        # value came from whoever could write to the calendar.
        url=security_find_url(str(component.get("URL", ""))) or "",
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

    if event.url:
        component.add("URL", event.url)
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
# Single occurrences of a repeating series
# --------------------------------------------------------------------------
#
# One CalDAV resource holds a whole series: a master VEVENT, plus one extra
# VEVENT per modified instance carrying a RECURRENCE-ID naming the slot it
# replaces.  Changing one occurrence therefore means rewriting the resource,
# not writing a new one — which is why both of these take and return the
# whole ``.ics`` document.


def _master_of(calendar: icalendar.Calendar) -> icalendar.Event | None:
    """The VEVENT describing the series itself, rather than an override."""
    for component in calendar.walk("VEVENT"):
        if component.get("RECURRENCE-ID") is None:
            return component
    return None


def _same_slot(component, recurrence_id: datetime) -> bool:
    """Whether an override component replaces ``recurrence_id``.

    Compared as instants, so that a server naming the slot in UTC and one
    naming it with a TZID agree.
    """
    value = component.get("RECURRENCE-ID")
    if value is None:
        return False
    try:
        moment, _ = _as_datetime(value.dt)
    except ParseError:
        return False
    return moment == recurrence_id


def exclude_occurrence(text: str, recurrence_id: datetime) -> str:
    """Return ``text`` with one occurrence removed from the series.

    Adds an ``EXDATE`` to the master and drops any override for that slot,
    which is how iCalendar says "this instance does not happen" without
    disturbing the rest of the series.
    """
    calendar = _parse(text)
    master = _master_of(calendar)
    if master is None:
        raise ParseError("no master event to exclude an occurrence from")

    # An override for the slot is now meaningless, and leaving it behind
    # would resurrect the instance on clients that read it first.
    for component in [c for c in calendar.walk("VEVENT")
                      if _same_slot(c, recurrence_id)]:
        calendar.subcomponents.remove(component)

    existing = master.get("EXDATE")
    if existing is not None and any(
            _matches_exdate(existing, recurrence_id)):
        return calendar.to_ical().decode("utf-8")   # already excluded

    master.add("EXDATE", _as_utc(recurrence_id))
    return calendar.to_ical().decode("utf-8")


def _as_utc(moment: datetime) -> datetime:
    """The same instant, written to go on the wire as UTC.

    Slots are named in UTC rather than in the series' own zone because
    Python's local timezone often carries an *abbreviation* like "EDT" as its
    name.  Written out that becomes ``TZID=EDT``, which is not a timezone
    identifier any server can resolve and has no VTIMEZONE to define it —
    Radicale rejects such a document outright with HTTP 400.  UTC needs no
    definition, and every client compares these as instants anyway.
    """
    return moment.astimezone(timezone.utc)


def _matches_exdate(existing, recurrence_id: datetime):
    """Every already-excluded date equal to ``recurrence_id``."""
    entries = existing if isinstance(existing, list) else [existing]
    for entry in entries:
        for value in getattr(entry, "dts", []):
            try:
                moment, _ = _as_datetime(value.dt)
            except ParseError:
                continue
            if moment == recurrence_id:
                yield moment


def override_occurrence(text: str, recurrence_id: datetime, event: Event) -> str:
    """Return ``text`` with one occurrence replaced by ``event``.

    The override is a VEVENT sharing the series' UID, carrying a
    ``RECURRENCE-ID`` naming the slot it replaces and *no* ``RRULE`` of its
    own — a repeat rule on an override would mean a second series.
    """
    calendar = _parse(text)
    if _master_of(calendar) is None:
        raise ParseError("no master event to override an occurrence of")

    for component in [c for c in calendar.walk("VEVENT")
                      if _same_slot(c, recurrence_id)]:
        calendar.subcomponents.remove(component)

    component = build_component(event)
    for name in ("RRULE", "EXDATE", "RDATE"):
        component.pop(name, None)
    component.add("RECURRENCE-ID", _as_utc(recurrence_id))
    calendar.add_component(component)
    return calendar.to_ical().decode("utf-8")


def truncate_series(text: str, before: datetime, *, keep_count: int | None = None) -> str:
    """Return ``text`` with the series ending just before ``before``.

    This is half of "this and all following events": the existing resource
    keeps the occurrences up to the split, and a *separate* event carries the
    ones after it.

    ``COUNT`` and ``UNTIL`` may not both appear in a rule, so a counted
    series has its ``COUNT`` replaced — by ``keep_count`` when the caller has
    worked out how many occurrences remain on this side, and otherwise by an
    ``UNTIL``.  Anything that belonged to the far side of the split — an
    override, an excluded date — is dropped, because it now belongs to the
    new series rather than to this one.
    """
    calendar = _parse(text)
    master = _master_of(calendar)
    if master is None:
        raise ParseError("no master event to truncate")
    rule = master.get("RRULE")
    if rule is None:
        raise ParseError("that event does not repeat")

    for component in [c for c in calendar.walk("VEVENT")
                      if _slot_at_or_after(c, before)]:
        calendar.subcomponents.remove(component)
    _drop_exdates_from(master, before)

    values = dict(rule)
    values.pop("COUNT", None)
    values.pop("UNTIL", None)
    if keep_count is not None:
        values["COUNT"] = [keep_count]
    else:
        # UNTIL is inclusive, so step back to stay clear of the split itself.
        values["UNTIL"] = [_as_utc(before) - timedelta(seconds=1)]

    master.pop("RRULE")
    master.add("RRULE", icalendar.prop.vRecur(values))
    return calendar.to_ical().decode("utf-8")


def _slot_at_or_after(component, moment: datetime) -> bool:
    """Whether an override replaces a slot on or after ``moment``."""
    value = component.get("RECURRENCE-ID")
    if value is None:
        return False
    try:
        slot, _ = _as_datetime(value.dt)
    except ParseError:
        return False
    return slot >= moment


def _drop_exdates_from(master, moment: datetime) -> None:
    """Remove excluded dates on or after ``moment``."""
    existing = master.get("EXDATE")
    if existing is None:
        return
    entries = existing if isinstance(existing, list) else [existing]
    kept = []
    for entry in entries:
        for value in getattr(entry, "dts", []):
            try:
                when, _ = _as_datetime(value.dt)
            except ParseError:
                continue
            if when < moment:
                kept.append(_as_utc(when))
    master.pop("EXDATE")
    for when in kept:
        master.add("EXDATE", when)


def _parse(text: str) -> icalendar.Calendar:
    try:
        return icalendar.Calendar.from_ical(text)
    except Exception as exc:
        raise ParseError(f"could not parse calendar data: {exc}") from exc


def bump_sequence(text: str) -> str:
    """Return ``text`` with every VEVENT's ``SEQUENCE`` advanced by one.

    Uploading a document as it stands still has to advertise a new revision,
    and a series has a SEQUENCE per VEVENT — the master and each override.
    """
    calendar = _parse(text)
    for component in calendar.walk("VEVENT"):
        try:
            current = int(component.get("SEQUENCE", 0))
        except (TypeError, ValueError):
            current = 0
        component.pop("SEQUENCE", None)
        component.add("SEQUENCE", current + 1)
    return calendar.to_ical().decode("utf-8")


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
