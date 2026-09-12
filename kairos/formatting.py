"""Turning dates and times into the words shown on screen.

Every user-visible date string in Kairos comes from here, so changing how the
app words things — or teaching it a new preference — is a change to one file.
The functions all read :mod:`kairos.config` themselves rather than taking a
format argument, which keeps call sites short.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from kairos.config import first_weekday_index, settings
from kairos.models import Occurrence, local_timezone

#: Long and short day names, Monday first, matching Python's weekday numbers.
DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
DAY_ABBREVIATIONS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
DAY_INITIALS = ("M", "T", "W", "T", "F", "S", "S")

MONTH_NAMES = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)


# --------------------------------------------------------------------------
# Times
# --------------------------------------------------------------------------

def use_24_hour() -> bool:
    return settings.get("time_format") == "24h"


def format_time(moment: datetime, *, drop_zero_minutes: bool = False) -> str:
    """"14:30", or "2:30 PM" — and "2 PM" when minutes are not interesting."""
    if use_24_hour():
        return moment.strftime("%H:%M")
    if drop_zero_minutes and moment.minute == 0:
        return moment.strftime("%-I %p")
    return moment.strftime("%-I:%M %p")


def format_hour_label(hour: int) -> str:
    """The label down the side of the week and day views."""
    if use_24_hour():
        return f"{hour:02d}:00"
    if hour == 0:
        return "12 AM"
    if hour == 12:
        return "12 PM"
    return f"{hour % 12} {'AM' if hour < 12 else 'PM'}"


def format_time_range(occurrence: Occurrence) -> str:
    """The time part of an event, as shown in lists and the detail popover."""
    if occurrence.all_day:
        return "All day"
    if occurrence.start == occurrence.end:
        return format_time(occurrence.start)
    return f"{format_time(occurrence.start)} – {format_time(occurrence.end)}"


# --------------------------------------------------------------------------
# Dates
# --------------------------------------------------------------------------

def format_date(day: date) -> str:
    """"Friday 5 September 2026"."""
    return f"{DAY_NAMES[day.weekday()]} {day.day} {MONTH_NAMES[day.month - 1]} {day.year}"


def format_date_short(day: date) -> str:
    """"5 Sep 2026"."""
    return f"{day.day} {MONTH_NAMES[day.month - 1][:3]} {day.year}"


def format_month_year(day: date) -> str:
    return f"{MONTH_NAMES[day.month - 1]} {day.year}"


def format_day_heading(day: date) -> str:
    """A heading for the agenda view: "Today", "Tomorrow", or a full date."""
    today = date.today()
    if day == today:
        return f"Today · {DAY_NAMES[day.weekday()]} {day.day} {MONTH_NAMES[day.month - 1]}"
    if day == today + timedelta(days=1):
        return f"Tomorrow · {DAY_NAMES[day.weekday()]} {day.day} {MONTH_NAMES[day.month - 1]}"
    if day == today - timedelta(days=1):
        return f"Yesterday · {DAY_NAMES[day.weekday()]} {day.day} {MONTH_NAMES[day.month - 1]}"
    if day.year == today.year:
        return f"{DAY_NAMES[day.weekday()]} {day.day} {MONTH_NAMES[day.month - 1]}"
    return format_date(day)


def format_range_heading(first: date, last: date) -> str:
    """The title bar text for a week: "1 – 7 September 2026".

    The year is left off when the range is in the current year, which keeps
    the window title short enough to survive a narrow header bar.
    """
    if first == last:
        return format_date(first)
    if first.year != last.year:
        return f"{format_date_short(first)} – {format_date_short(last)}"

    year = "" if first.year == date.today().year else f" {first.year}"
    if first.month != last.month:
        return (f"{first.day} {MONTH_NAMES[first.month - 1][:3]} – "
                f"{last.day} {MONTH_NAMES[last.month - 1][:3]}{year}")
    return f"{first.day} – {last.day} {MONTH_NAMES[first.month - 1]}{year}"


def relative_phrase(moment: datetime) -> str:
    """"in 20 minutes", "3 hours ago" — used in the event popover."""
    now = datetime.now(tz=local_timezone())
    delta = moment - now
    seconds = int(abs(delta.total_seconds()))
    future = delta.total_seconds() >= 0

    if seconds < 60:
        return "now"
    minutes = seconds // 60
    if minutes < 60:
        phrase = f"{minutes} minute{'s' if minutes != 1 else ''}"
    elif minutes < 60 * 24:
        hours = minutes // 60
        phrase = f"{hours} hour{'s' if hours != 1 else ''}"
    else:
        days = minutes // (60 * 24)
        phrase = f"{days} day{'s' if days != 1 else ''}"
    return f"in {phrase}" if future else f"{phrase} ago"


# --------------------------------------------------------------------------
# Week arithmetic
# --------------------------------------------------------------------------

def week_start(day: date) -> date:
    """The first day of the week containing ``day``, per the preference."""
    offset = (day.weekday() - first_weekday_index()) % 7
    return day - timedelta(days=offset)


def weekday_order() -> list[int]:
    """Python weekday numbers in the order the grid shows them."""
    start = first_weekday_index()
    return [(start + column) % 7 for column in range(7)]


def weekday_headings(style: str = "abbreviation") -> list[str]:
    """Column headings for the month and week grids."""
    names = {"full": DAY_NAMES, "abbreviation": DAY_ABBREVIATIONS, "initial": DAY_INITIALS}[style]
    return [names[index] for index in weekday_order()]


def month_grid_start(month_day: date) -> date:
    """The first cell of the month grid — usually in the previous month."""
    return week_start(month_day.replace(day=1))


def iso_week_number(day: date) -> int:
    return day.isocalendar().week


# --------------------------------------------------------------------------
# The tray icon's tooltip
# --------------------------------------------------------------------------

def tray_summary(occurrences, now) -> str:
    """What the tray icon's tooltip says: what is on now, and what is next.

    Timed events only. An all-day event is "on" from midnight to midnight,
    and letting it answer "what is happening now" would push the meeting
    that is actually about to start out of sight.
    """
    timed = sorted((o for o in occurrences if not o.all_day and o.end > now),
                   key=lambda o: (o.start, o.summary.lower()))
    current = [o for o in timed if o.start <= now]
    upcoming = [o for o in timed if o.start > now]

    lines = []
    if current:
        # The most recently started is the one you are most likely in.
        occurrence = current[-1]
        lines.append(f"Now: {occurrence.summary or '(No title)'}, "
                     f"until {format_time(occurrence.end)}")
    if upcoming:
        occurrence = upcoming[0]
        lines.append(f"Next: {occurrence.summary or '(No title)'} "
                     f"{_when_next(occurrence.start, now)}")
    return "\n".join(lines) or "Nothing coming up in the next week"


def _when_next(start, now) -> str:
    """ "at 14:00 · in 25 min", "tomorrow at 09:00", or "on Tuesday at 09:00". """
    if start.date() == now.date():
        minutes = max(1, round((start - now).total_seconds() / 60))
        return f"at {format_time(start)} · in {_duration_phrase(minutes)}"
    if start.date() == now.date() + timedelta(days=1):
        return f"tomorrow at {format_time(start)}"
    return f"on {DAY_NAMES[start.weekday()]} at {format_time(start)}"


def _duration_phrase(minutes: int) -> str:
    if minutes < 60:
        return f"{minutes} min"
    hours, rest = divmod(minutes, 60)
    return f"{hours} h" if rest == 0 else f"{hours} h {rest} min"
