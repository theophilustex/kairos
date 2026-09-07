"""The security-sensitive bits of Kairos, gathered in one readable place.

There are only four things to know:

1. **Passwords never touch our config files.**  They go to the system keyring
   (GNOME Keyring, KWallet, ...) through :class:`CredentialStore`.  If no
   keyring is available Kairos says so and asks for the password each session
   rather than silently writing it to disk.

2. **Calendar URLs are validated before we ever connect** — see
   :func:`validate_calendar_url`.  Only ``https`` is accepted unless the user
   deliberately allows plain http, and only for a host that looks private.

3. **TLS certificates are verified** by default.  The one place this can be
   turned off is a preference, and the UI labels it as dangerous.

4. **Files we write are private** (mode 0600) and written atomically, so a
   crash mid-save cannot leave a half-written config behind.

Everything else in the codebase is expected to call into this module rather
than rolling its own version.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import socket
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

log = logging.getLogger(__name__)

#: Keyring service name under which Kairos stores calendar passwords.
KEYRING_SERVICE = "org.kairos.Calendar"

#: Schemes we are willing to talk to.  Anything else (file://, gopher://,
#: ftp://) is rejected outright rather than handed to the HTTP library.
ALLOWED_SCHEMES = ("https", "http")

#: Matches a URL that names a scheme but has no "//" after it, such as
#: "javascript:alert(1)" or "file:/etc/passwd".  The lookahead deliberately
#: does not match "example.com:8443/dav", where the colon introduces a port.
_SCHEME_WITHOUT_SLASHES = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:(?!\d)")

#: Hard ceiling on a downloaded calendar object, so a hostile or broken
#: server cannot exhaust memory.  Individual events are tiny; 8 MiB is
#: generous even for a calendar with embedded attachments.
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


class SecurityError(Exception):
    """Raised when a request would violate one of the rules above."""


# --------------------------------------------------------------------------
# URL validation
# --------------------------------------------------------------------------

def _is_private_host(hostname: str) -> bool:
    """True if ``hostname`` resolves only to loopback/private addresses.

    Used to decide whether plain http is tolerable.  Resolution failures
    return False: if we cannot prove the host is private, we treat it as
    public and hold it to the stricter rule.
    """
    if hostname in ("localhost", "localhost.localdomain"):
        return True
    try:
        infos = socket.getaddrinfo(hostname, None)
    except (socket.gaierror, UnicodeError):
        return False
    addresses = {info[4][0] for info in infos}
    if not addresses:
        return False
    for address in addresses:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            return False
        if not (ip.is_private or ip.is_loopback or ip.is_link_local):
            return False
    return True


def validate_calendar_url(url: str, *, allow_insecure_http: bool = False) -> str:
    """Check a user-supplied CalDAV/WebDAV URL and return it normalised.

    Raises :class:`SecurityError` with a message suitable for showing to the
    user if the URL is unusable.  The rules are intentionally boring:

    * it must parse, and have a scheme and a host;
    * the scheme must be https, or http when the user has opted in *and* the
      host is on the local machine or a private network;
    * no embedded credentials (``https://user:pass@host``), because those end
      up in logs and config files.
    """
    url = (url or "").strip()
    if not url:
        raise SecurityError("Please enter a server address.")

    # A bare "example.com/dav" is a very common thing to type, so assume https
    # rather than rejecting it — but only when there is genuinely no scheme.
    # Prepending blindly would turn "javascript:alert(1)" into a URL that
    # parses as https with a host of "javascript", which must not happen.
    lowered = url.lower()
    if lowered.startswith(("http://", "https://")):
        pass
    elif "://" in url:
        scheme = url.split("://", 1)[0]
        raise SecurityError(
            f"Kairos only connects over https (or http on a local network); "
            f"“{scheme}” is not supported."
        )
    elif _SCHEME_WITHOUT_SLASHES.match(url):
        # "javascript:", "file:", "mailto:" — a scheme with no "//" after it.
        # The negative lookahead in the pattern keeps "example.com:8443/dav"
        # out of this branch, since that colon introduces a port.
        raise SecurityError(
            f"“{url.split(':', 1)[0]}” is not a web address Kairos can use."
        )
    else:
        url = "https://" + url

    try:
        parts = urlparse(url)
    except ValueError as exc:
        raise SecurityError(f"That does not look like a web address: {exc}") from exc

    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise SecurityError(
            f"Kairos only connects over https (or http on a local network); "
            f"“{parts.scheme}” is not supported."
        )

    if not parts.hostname:
        raise SecurityError("The address is missing a server name.")

    if parts.username or parts.password:
        raise SecurityError(
            "Please put the username and password in their own fields rather "
            "than in the address."
        )

    if scheme == "http":
        if not allow_insecure_http:
            raise SecurityError(
                "This address uses unencrypted http. Enable “Allow unencrypted "
                "http” in Preferences → Security if this is a server on your "
                "own network."
            )
        if not _is_private_host(parts.hostname):
            raise SecurityError(
                f"“{parts.hostname}” is not on your local network, so Kairos "
                "will not send your password to it unencrypted. Use https."
            )

    # Drop fragments and rebuild, so what we store is exactly what we use.
    normalised = urlunparse((scheme, parts.netloc, parts.path or "/", parts.params, parts.query, ""))
    return normalised


# --------------------------------------------------------------------------
# Password storage
# --------------------------------------------------------------------------

class CredentialStore:
    """Reads and writes calendar passwords via the system keyring.

    The keyring is imported lazily: it pulls in D-Bus machinery we would
    rather not pay for at startup if the user has only local calendars.
    """

    def __init__(self) -> None:
        self._keyring = None
        self._unavailable_reason: str | None = None
        # Passwords typed this session when there is no keyring to put them
        # in.  Deliberately in memory only, and gone when Kairos exits.
        self._session_only: dict[str, str] = {}

    # -- availability -----------------------------------------------------

    @property
    def keyring(self):
        if self._keyring is None and self._unavailable_reason is None:
            try:
                import keyring
                from keyring.backends.fail import Keyring as FailKeyring

                backend = keyring.get_keyring()
                if isinstance(backend, FailKeyring):
                    raise RuntimeError("no usable keyring backend is installed")
                self._keyring = keyring
            except Exception as exc:
                self._unavailable_reason = str(exc)
                log.warning("system keyring unavailable: %s", exc)
        return self._keyring

    @property
    def available(self) -> bool:
        return self.keyring is not None

    @property
    def unavailable_reason(self) -> str | None:
        _ = self.keyring  # force the probe
        return self._unavailable_reason

    # -- operations -------------------------------------------------------

    def set_password(self, account_id: str, password: str) -> bool:
        """Store a password.  Returns True if it reached the keyring."""
        if not password:
            self.delete_password(account_id)
            return True
        if self.available:
            try:
                self.keyring.set_password(KEYRING_SERVICE, account_id, password)
                self._session_only.pop(account_id, None)
                return True
            except Exception as exc:
                log.error("could not write password to keyring: %s", exc)
        self._session_only[account_id] = password
        return False

    def get_password(self, account_id: str) -> str | None:
        if self.available:
            try:
                stored = self.keyring.get_password(KEYRING_SERVICE, account_id)
                if stored is not None:
                    return stored
            except Exception as exc:
                log.error("could not read password from keyring: %s", exc)
        return self._session_only.get(account_id)

    # OAuth accounts keep two more secrets: the refresh token, which is a
    # long-lived credential and belongs in the keyring exactly as a password
    # does, and the client secret. Both are keyed off the account id with a
    # suffix, so one account's entries stay together.

    def set_refresh_token(self, account_id: str, token: str) -> bool:
        return self.set_password(f"{account_id}:refresh", token)

    def get_refresh_token(self, account_id: str) -> str | None:
        return self.get_password(f"{account_id}:refresh")

    def set_client_secret(self, account_id: str, secret: str) -> bool:
        return self.set_password(f"{account_id}:client-secret", secret)

    def get_client_secret(self, account_id: str) -> str | None:
        return self.get_password(f"{account_id}:client-secret")

    def forget_account(self, account_id: str) -> None:
        """Remove every secret belonging to one account."""
        for key in (account_id, f"{account_id}:refresh",
                    f"{account_id}:client-secret"):
            self.delete_password(key)

    def delete_password(self, account_id: str) -> None:
        self._session_only.pop(account_id, None)
        if not self.available:
            return
        try:
            self.keyring.delete_password(KEYRING_SERVICE, account_id)
        except Exception:
            # Nothing stored, or the backend refused; either way there is no
            # password left for us to worry about.
            pass


#: Shared credential store; there is no reason to have more than one.
credentials = CredentialStore()


# --------------------------------------------------------------------------
# Safe file writes
# --------------------------------------------------------------------------

def write_private_json(path: Path | str, data: Any) -> None:
    """Write ``data`` as pretty JSON, readable only by the current user.

    The write is atomic: we write a temporary file in the same directory and
    rename it over the target, so a power cut cannot corrupt the original.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    text = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"

    handle, temporary_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
    )
    try:
        os.fchmod(handle, 0o600)
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        # Never leave a stray .tmp behind if anything went wrong.
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


def read_json(path: Path | str, default: Any) -> Any:
    """Read a JSON file, returning ``default`` if it is missing or broken."""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as exc:
        log.warning("could not read %s (%s); using defaults", path, exc)
        return default


# --------------------------------------------------------------------------
# Text hygiene
# --------------------------------------------------------------------------

def sanitise_text(value: str | None, *, max_length: int = 4096) -> str:
    """Clean a text field that came from a calendar server.

    Server data is untrusted input.  We strip control characters (which can
    scramble a terminal or a label), normalise line endings, and cap the
    length so one absurd field cannot lock up the UI.  Note that Kairos never
    renders event text as Pango markup, so there is no injection to escape —
    this is purely about keeping the display sane.
    """
    if not value:
        return ""
    value = str(value).replace("\r\n", "\n").replace("\r", "\n")
    cleaned = "".join(
        character
        for character in value
        if character in "\n\t" or (ord(character) >= 32 and ord(character) != 127)
    )
    if len(cleaned) > max_length:
        cleaned = cleaned[:max_length] + "…"
    return cleaned
