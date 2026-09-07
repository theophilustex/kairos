"""The list of calendar accounts, saved as plain JSON.

``~/.config/kairos/accounts.json`` looks like this, and you are welcome to
edit it by hand::

    [
      {
        "id": "a1b2c3d4",
        "name": "Fastmail",
        "kind": "caldav",
        "url": "https://caldav.fastmail.com/dav/calendars/user/me@example.com/",
        "username": "me@example.com",
        "verify_tls": true,
        "enabled": true
      }
    ]

Note what is *not* in there: the password.  That lives in the system keyring,
looked up by the account's ``id``.  See :mod:`kairos.security`.
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

from kairos.backends.local import LOCAL_ACCOUNT_ID, local_account
from kairos.config import ACCOUNTS_FILE, ensure_directories
from kairos.models import Account
from kairos.security import credentials, read_json, write_private_json

log = logging.getLogger(__name__)


class AccountStore:
    """Loads, saves and hands out :class:`~kairos.models.Account` records.

    The built-in "On this computer" account is always present and is never
    written to the file — it has nothing worth persisting.
    """

    def __init__(self, path: Path | str = ACCOUNTS_FILE) -> None:
        self.path = Path(path)
        self._accounts: dict[str, Account] = {}
        self.load()

    # -- access -----------------------------------------------------------

    def all(self) -> list[Account]:
        """Every account, with the local one first."""
        remote = sorted(self._accounts.values(), key=lambda a: a.name.lower())
        return [local_account(), *remote]

    def remote(self) -> list[Account]:
        """Only the accounts that involve a network."""
        return [a for a in self.all() if not a.is_local]

    def get(self, account_id: str) -> Account | None:
        if account_id == LOCAL_ACCOUNT_ID:
            return local_account()
        return self._accounts.get(account_id)

    # -- mutation ---------------------------------------------------------

    def add(self, account: Account, password: str | None = None) -> Account:
        """Save a new account, storing its password in the keyring."""
        if not account.id or account.id == LOCAL_ACCOUNT_ID:
            account.id = uuid.uuid4().hex
        self._accounts[account.id] = account
        if password is not None:
            credentials.set_password(account.id, password)
        self.save()
        return account

    def update(self, account: Account, password: str | None = None) -> None:
        if account.is_local:
            return
        self._accounts[account.id] = account
        if password is not None:
            credentials.set_password(account.id, password)
        self.save()

    def remove(self, account_id: str) -> None:
        """Forget an account and wipe its password from the keyring."""
        if account_id == LOCAL_ACCOUNT_ID:
            return
        self._accounts.pop(account_id, None)
        # Not just the password: an OAuth account also has a refresh token and
        # a client secret in the keyring, and leaving those behind would be a
        # live credential for a server the user has told us to forget.
        credentials.forget_account(account_id)
        self.save()

    # -- persistence ------------------------------------------------------

    def load(self) -> None:
        raw = read_json(self.path, default=[])
        if not isinstance(raw, list):
            log.warning("%s is not a JSON list; ignoring it", self.path)
            return
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            try:
                account = Account.from_json(entry)
            except Exception as exc:
                log.warning("skipping malformed account entry: %s", exc)
                continue
            if account.id == LOCAL_ACCOUNT_ID:
                continue
            self._accounts[account.id] = account

    def save(self) -> None:
        ensure_directories()
        write_private_json(
            self.path,
            [account.to_json() for account in sorted(self._accounts.values(), key=lambda a: a.name.lower())],
        )
