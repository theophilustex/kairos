"""Turning repeating events into the concrete occurrences a view can draw.

Views ask one question — "what is on screen between these two moments?" — and
this module answers it.  Non-repeating events are simply clipped to the
window.  Repeating ones are expanded by the ``recurring-ical-events`` library,
which handles the parts of RFC 5545 that are genuinely hard: EXDATE, RDATE,
UNTIL/COUNT, and monthly rules that land on the 31st.

Expansion runs against the event's *original* iCalendar text (see
:attr:`kairos.models.Event.raw_ics`), so nothing the server said is lost in
translation.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

import icalendar
import recurring_ical_events

from kairos import ical
from kairos.models import Event, Occurrence, local_timezone

log = logging.getLogger(__name__)

#: A repeating event with no end date could in principle produce millions of
#: occurrences.  Views only ever show weeks or months, so this cap simply
#: protects us from a pathological rule; it is never reached in normal use.
MAX_OCCURRENCES_PER_EVENT = 2000


def _clip(event: Event, window_start: datetime, window_end: datetime) -> list[Occurrence]:
    """The single occurrence of a non-repeating event, if it is in range."""
    if event.end > window_start and event.start < window_end:
        return [Occurrence(event=event, start=event.start, end=event.end)]
    # A zero-length event (start == end) still deserves to be shown.
    if event.start == event.end and window_start <= event.start < window_end:
        return [Occurrence(event=event, start=event.start, end=event.end)]
    return []


def _expand_recurring(event: Event, window_start: datetime, window_end: datetime) -> list[Occurrence]:
    """Ask ``recurring-ical-events`` for every instance inside the window."""
    text = event.raw_ics or ical.to_ical_text(event)
    try:
        calendar = icalendar.Calendar.from_ical(text)
    except Exception as exc:
        log.warning("cannot expand %s: unparseable iCalendar (%s)", event.uid, exc)
        return _clip(event, window_start, window_end)

    try:
        instances = recurring_ical_events.of(calendar).between(window_start, window_end)
    except Exception as exc:
        # Malformed rules are common in the wild.  Show the first occurrence
        # rather than dropping the event entirely.
        log.warning("cannot expand recurrence for %s (%s)", event.uid, exc)
        return _clip(event, window_start, window_end)

    occurrences: list[Occurrence] = []
    for instance in instances[:MAX_OCCURRENCES_PER_EVENT]:
        try:
            start, all_day = ical._as_datetime(instance["DTSTART"].dt)
        except (KeyError, ical.ParseError):
            continue

        dtend = instance.get("DTEND")
        if dtend is not None:
            end, _ = ical._as_datetime(dtend.dt)
        elif all_day:
            end = start + timedelta(days=1)
        else:
            end = start + event.duration

        occurrences.append(Occurrence(event=event, start=start, end=end))

    if len(instances) > MAX_OCCURRENCES_PER_EVENT:
        log.warning("event %s produced more than %d occurrences; truncated",
                    event.uid, MAX_OCCURRENCES_PER_EVENT)
    return occurrences


def expand(events: list[Event], window_start: datetime, window_end: datetime) -> list[Occurrence]:
    """Every occurrence of ``events`` that overlaps the window, in order.

    ``window_start`` is inclusive and ``window_end`` exclusive, both aware
    datetimes.  The result is sorted the way the views want to draw it: whole-
    day and multi-day banners first, then timed events by start time.
    """
    occurrences: list[Occurrence] = []
    for event in events:
        if event.is_recurring:
            occurrences.extend(_expand_recurring(event, window_start, window_end))
        else:
            occurrences.extend(_clip(event, window_start, window_end))
    occurrences.sort(key=Occurrence.sort_key)
    return occurrences


def expand_for_days(events: list[Event], first_day: date, day_count: int) -> list[Occurrence]:
    """Convenience wrapper for the views, which think in whole days."""
    start = datetime(first_day.year, first_day.month, first_day.day, tzinfo=local_timezone())
    return expand(events, start, start + timedelta(days=day_count))


def group_by_day(occurrences: list[Occurrence], first_day: date, day_count: int) -> dict[date, list[Occurrence]]:
    """Bucket occurrences into the days they appear on.

    A three-day event appears in all three buckets — that is what a calendar
    grid needs, since each day cell draws its own slice of the banner.
    """
    buckets: dict[date, list[Occurrence]] = {
        first_day + timedelta(days=offset): [] for offset in range(day_count)
    }
    for occurrence in occurrences:
        day = occurrence.first_day
        last = occurrence.last_day
        while day <= last:
            if day in buckets:
                buckets[day].append(occurrence)
            day += timedelta(days=1)
    for day_occurrences in buckets.values():
        day_occurrences.sort(key=Occurrence.sort_key)
    return buckets


def next_occurrence_after(event: Event, moment: datetime, horizon_days: int = 400) -> Occurrence | None:
    """The first occurrence of ``event`` starting at or after ``moment``.

    Used by the notification scheduler, which needs to know when a repeating
    event next comes round without expanding the whole series.
    """
    window_end = moment + timedelta(days=horizon_days)
    candidates = expand([event], moment - timedelta(days=1), window_end)
    for occurrence in sorted(candidates, key=lambda o: o.start):
        if occurrence.start >= moment:
            return occurrence
    return None
