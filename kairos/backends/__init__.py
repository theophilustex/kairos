"""Where calendars come from.

A *backend* knows how to list calendars, read events out of them and write
events back.  There are two:

    LocalBackend   calendars that exist only in Kairos's own database
    CalDAVBackend  calendars on a CalDAV/WebDAV server

Both satisfy the same small interface in :mod:`kairos.backends.base`, so the
rest of the app never branches on which kind it is holding.  Adding support
for another protocol means writing one new class here and adding it to
:func:`backend_for`.
"""

from __future__ import annotations

from kairos.backends.base import Backend, BackendError, AuthenticationError
from kairos.backends.local import LocalBackend
from kairos.models import Account, CALDAV, LOCAL


def backend_for(account: Account, *, password: str | None = None) -> Backend:
    """Build the right backend for ``account``.

    The CalDAV backend is imported lazily: it pulls in the ``caldav`` and
    ``requests`` stack, which we would rather not pay for at startup if the
    user only has local calendars.
    """
    if account.kind == LOCAL:
        return LocalBackend(account)
    if account.kind == CALDAV:
        from kairos.backends.caldav_backend import CalDAVBackend
        return CalDAVBackend(account, password=password)
    raise BackendError(f"Unknown account type: {account.kind!r}")


__all__ = [
    "Backend",
    "BackendError",
    "AuthenticationError",
    "LocalBackend",
    "backend_for",
]
