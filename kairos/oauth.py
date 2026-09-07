"""OAuth 2.0 for calendar providers that will not take a password.

Google is the reason this exists.  Its CalDAV endpoint refuses Basic
authentication outright, so an app password is not an option the way it is
with Fastmail or Nextcloud — a bearer token is the only way in.

The flow implemented here is **authorization code with PKCE over a loopback
redirect**, which is what Google (and RFC 8252) specify for desktop
applications:

1. Kairos opens the provider's consent page in the user's own browser.  The
   password is typed there, into the provider's own page, and never passes
   through Kairos.
2. The browser is sent back to ``http://127.0.0.1:<port>/`` — a one-request
   server started for the occasion, listening on the loopback interface only.
3. The code that arrives is exchanged for an access token and a refresh
   token.

PKCE matters here even though the flow also uses a client secret: a desktop
application cannot keep a secret, so the secret proves nothing, and the
verifier is what actually ties the code back to the process that asked for
it.  Nothing else on the machine can spend a code it did not generate.

Only the refresh token is kept, in the keyring beside the passwords.  Access
tokens live for an hour and are held in memory.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import logging
import secrets
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass
from datetime import datetime, timedelta

from kairos.models import local_timezone

log = logging.getLogger(__name__)


class OAuthError(Exception):
    """Anything that went wrong signing in, phrased for a user."""


@dataclass(frozen=True)
class Provider:
    """One OAuth provider's endpoints and scope."""

    name: str
    authorize_url: str
    token_url: str
    scope: str
    #: Where the CalDAV collections live, ``{user}`` filled in with the
    #: account's email address.
    caldav_url: str = ""


GOOGLE = Provider(
    name="Google",
    authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
    token_url="https://oauth2.googleapis.com/token",
    scope="https://www.googleapis.com/auth/calendar",
    caldav_url="https://apidata.googleusercontent.com/caldav/v2/{user}/user",
)

PROVIDERS = {"google": GOOGLE}

#: How early to treat an access token as expired.  Clocks drift and a request
#: takes time; refreshing a minute early is cheaper than a failed sync.
EXPIRY_MARGIN = timedelta(seconds=60)


@dataclass
class Token:
    """An access token and when it stops being usable."""

    access_token: str
    expires_at: datetime
    refresh_token: str = ""

    @property
    def expired(self) -> bool:
        return datetime.now(tz=local_timezone()) >= self.expires_at - EXPIRY_MARGIN


def make_verifier() -> str:
    """A fresh PKCE code verifier: 43–128 characters of URL-safe randomness."""
    return base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode("ascii")


def challenge_for(verifier: str) -> str:
    """The S256 challenge derived from a verifier."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def authorization_url(provider: Provider, client_id: str, redirect_uri: str,
                      verifier: str, state: str) -> str:
    """The consent page to open in the user's browser."""
    query = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": provider.scope,
        "code_challenge": challenge_for(verifier),
        "code_challenge_method": "S256",
        "state": state,
        # Without these two Google returns an access token but no refresh
        # token on a second authorisation, and the account silently stops
        # working an hour later.
        "access_type": "offline",
        "prompt": "consent",
    }
    return f"{provider.authorize_url}?{urllib.parse.urlencode(query)}"


class _RedirectHandler(http.server.BaseHTTPRequestHandler):
    """Catches the one redirect the browser makes after consent."""

    def do_GET(self) -> None:                     # noqa: N802 (http.server API)
        parsed = urllib.parse.urlparse(self.path)
        self.server.query = urllib.parse.parse_qs(parsed.query)  # type: ignore[attr-defined]

        body = (b"<html><body style='font-family:sans-serif;padding:3em'>"
                b"<h2>You can close this tab.</h2>"
                b"<p>Kairos has what it needs.</p></body></html>")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        """Silence http.server's stderr logging."""


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class LoopbackReceiver:
    """Catches the single redirect the browser makes after consent.

    Listening has to start *before* the browser is sent anywhere, or the
    redirect can arrive before anything is there to answer it — hence a
    class rather than one blocking call. It binds to the loopback interface
    only, so nothing off this machine can reach it, and serves exactly one
    request.

    Use it as a context manager so the socket is closed even when the user
    abandons the sign-in.
    """

    def __init__(self, timeout: float = 300.0) -> None:
        self.timeout = timeout
        port = _free_port()
        self.redirect_uri = f"http://127.0.0.1:{port}/"
        self._server = http.server.HTTPServer(("127.0.0.1", port), _RedirectHandler)
        self._server.query = {}                   # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._server.handle_request,
                                        daemon=True)

    def __enter__(self) -> "LoopbackReceiver":
        self._thread.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self._server.server_close()

    def wait_for_code(self, state: str) -> str:
        """Block until the browser comes back, and validate what it brought."""
        self._thread.join(self.timeout)
        query = getattr(self._server, "query", {})

        if not query:
            raise OAuthError("Timed out waiting for the browser to come back.")
        if "error" in query:
            raise OAuthError(f"The provider refused the sign-in: {query['error'][0]}")
        if query.get("state", [""])[0] != state:
            # Someone else's redirect, or one aimed at this port on purpose.
            raise OAuthError("The sign-in response did not match the request.")
        code = query.get("code", [""])[0]
        if not code:
            raise OAuthError("The provider sent no authorisation code.")
        return code


def _post(url: str, fields: dict, timeout: float) -> dict:
    """POST form fields and read a JSON reply."""
    data = urllib.parse.urlencode(fields).encode("ascii")
    request = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("error", "")
        except Exception:
            pass
        raise OAuthError(
            f"The provider rejected the sign-in ({exc.code}"
            + (f": {detail}" if detail else "") + ").") from exc
    except urllib.error.URLError as exc:
        raise OAuthError(f"Could not reach the provider: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise OAuthError("The provider sent a reply Kairos could not read.") from exc


def _token_from(payload: dict, previous_refresh: str = "") -> Token:
    access = payload.get("access_token")
    if not access:
        raise OAuthError("The provider sent no access token.")
    try:
        lifetime = int(payload.get("expires_in", 3600))
    except (TypeError, ValueError):
        lifetime = 3600
    # A refresh response usually omits the refresh token, meaning "keep the
    # one you have"; treating that as "you have none" would sign the user
    # out an hour later.
    return Token(
        access_token=access,
        expires_at=datetime.now(tz=local_timezone()) + timedelta(seconds=lifetime),
        refresh_token=payload.get("refresh_token") or previous_refresh,
    )


def exchange_code(provider: Provider, client_id: str, client_secret: str,
                  code: str, verifier: str, redirect_uri: str,
                  timeout: float = 30.0) -> Token:
    """Turn an authorisation code into tokens."""
    return _token_from(_post(provider.token_url, {
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "code_verifier": verifier,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
    }, timeout))


def refresh(provider: Provider, client_id: str, client_secret: str,
            refresh_token: str, timeout: float = 30.0) -> Token:
    """Get a new access token from a refresh token."""
    if not refresh_token:
        raise OAuthError("This account has no saved sign-in; add it again.")
    return _token_from(_post(provider.token_url, {
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }, timeout), previous_refresh=refresh_token)


def open_in_browser(url: str) -> bool:
    """Show the consent page. False if there is no browser to show it in."""
    try:
        return webbrowser.open(url)
    except Exception as exc:            # a headless box, or no handler
        log.warning("could not open a browser (%s)", exc)
        return False
