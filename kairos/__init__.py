"""Kairos - a lightweight, customisable calendar for Linux.

The package is deliberately organised so that each module has one job and can
be read on its own:

    config.py         where settings live and how they are loaded/saved
    security.py       URL checks, password storage, safe file writes
    models.py         the plain data types (Calendar, Event, Alarm)
    ical.py           translation between models and iCalendar text
    storage.py        the on-disk SQLite cache
    recurrence.py     turning repeating events into concrete occurrences
    backends/         talking to a CalDAV/WebDAV server (or to nothing at all)
    sync.py           the background thread that keeps cache and server in step
    notifications.py  firing desktop notifications for event reminders
    theming.py        stylesheet loading, including the user's own CSS
    ui/               every widget the user actually sees
"""

APP_ID = "org.kairos.Calendar"
APP_NAME = "Kairos"
VERSION = "0.1.0"

__all__ = ["APP_ID", "APP_NAME", "VERSION"]
