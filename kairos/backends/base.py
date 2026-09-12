"""The interface every calendar backend implements.

Five methods, no state beyond the account.  Backends are used from the sync
worker thread only — never from the UI thread — so they are free to block.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime

from kairos.models import Account, Calendar, Event


class BackendError(Exception):
    """Something went wrong talking to a calendar source.

    The message is shown to the user, so it should read like a sentence
    rather than like a stack trace.
    """


class AuthenticationError(BackendError):
    """The server rejected the username or password.

    Kept separate because the UI reacts differently: it re-prompts for the
    password instead of just showing a red banner.
    """


class SyncTokenRejected(BackendError):
    """The server no longer accepts the sync-token Kairos held.

    Servers may forget old tokens. Not something to report: it only means
    this calendar has to be downloaded in full once more.
    """


@dataclass
class SyncChanges:
    """What changed in a calendar since a sync-token was issued."""

    changed: list[Event] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)      # hrefs
    token: str = ""


class Backend(ABC):
    """Read and write calendars for one :class:`~kairos.models.Account`."""

    def __init__(self, account: Account) -> None:
        self.account = account

    #: Whether :meth:`fetch_changes` can be used. A backend that cannot ask
    #: for "only what changed" is downloaded in full whenever it changes.
    supports_incremental_sync = False

    def dav_sync_token(self, calendar: Calendar) -> str:
        """The calendar's current RFC 6578 sync-token, or "" if there is none."""
        return ""

    def fetch_changes(self, calendar: Calendar, token: str) -> SyncChanges:
        """What changed since ``token``. Only called when supported."""
        raise NotImplementedError

    # -- discovery --------------------------------------------------------

    @abstractmethod
    def discover_calendars(self) -> list[Calendar]:
        """List the calendars this account offers.

        Called when adding an account and on every full sync, so that
        calendars created elsewhere show up.
        """

    # -- reading ----------------------------------------------------------

    @abstractmethod
    def fetch_events(self, calendar: Calendar, start: datetime, end: datetime) -> list[Event]:
        """Every event in ``calendar`` overlapping the window.

        Repeating events must be returned as their master component, not
        expanded — :mod:`kairos.recurrence` does the expanding.
        """

    # -- writing ----------------------------------------------------------

    @abstractmethod
    def save_event(self, calendar: Calendar, event: Event) -> Event:
        """Create or update ``event`` on the server.

        Returns the event with :attr:`~kairos.models.Event.href` and
        :attr:`~kairos.models.Event.etag` filled in from the server's reply,
        so the next edit can be conditional.
        """

    @abstractmethod
    def delete_event(self, calendar: Calendar, event: Event) -> None:
        """Remove ``event`` from the server.

        Deleting something that is already gone is not an error.
        """

    # -- optional ---------------------------------------------------------

    def check_connection(self) -> None:
        """Raise :class:`BackendError` if the account is not usable.

        The default implementation just tries a discovery call, which is
        enough for both backends we ship.
        """
        self.discover_calendars()
