"""Calendars that live only on this machine.

There is no server, so most of the interface is a no-op: the SQLite cache in
:mod:`kairos.storage` *is* the storage.  This backend exists so that local
calendars go through exactly the same code path as remote ones, which keeps
the sync worker and the UI free of "if this is a local calendar" branches.
"""

from __future__ import annotations

from datetime import datetime

from kairos.backends.base import Backend
from kairos.models import Account, Calendar, Event

#: The account every local calendar belongs to.  There is only ever one.
LOCAL_ACCOUNT_ID = "local"

#: The calendar Kairos creates on first run, so there is somewhere to put an
#: event before any server has been configured.
DEFAULT_CALENDAR_ID = "local-personal"


def local_account() -> Account:
    from kairos.models import LOCAL
    return Account(id=LOCAL_ACCOUNT_ID, name="On this computer", kind=LOCAL)


def default_calendar() -> Calendar:
    return Calendar(
        id=DEFAULT_CALENDAR_ID,
        account_id=LOCAL_ACCOUNT_ID,
        name="Personal",
        colour="#3584e4",
    )


class LocalBackend(Backend):
    """A backend that agrees with everything and stores nothing extra."""

    def discover_calendars(self) -> list[Calendar]:
        # Local calendars are created by the user in the sidebar, not
        # discovered.  The storage layer already knows about them.
        return []

    def fetch_events(self, calendar: Calendar, start: datetime, end: datetime) -> list[Event]:
        # Already in the cache; there is nowhere else to fetch from.
        return []

    def save_event(self, calendar: Calendar, event: Event) -> Event:
        # Nothing to upload.  Returning the event unchanged tells the sync
        # worker the write succeeded.
        return event

    def delete_event(self, calendar: Calendar, event: Event) -> None:
        return None

    def check_connection(self) -> None:
        return None
