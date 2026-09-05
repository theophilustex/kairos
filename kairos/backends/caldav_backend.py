"""Reading and writing calendars on a CalDAV/WebDAV server.

This is the only module that imports ``caldav``.  Everything it raises is a
:class:`~kairos.backends.base.BackendError` with a message fit to show the
user, so the sync worker and the UI never have to know what a
``PropfindError`` is.

Security notes, since this is the code that touches the network:

* the URL has already been through :func:`kairos.security.validate_calendar_url`
  before an account is saved, and is checked again here;
* TLS verification is on unless the user turned it off for this account;
* every request has a timeout, so a wedged server cannot hang a sync forever;
* the password is fetched from the keyring at connection time and is never
  written to a log line or an exception message.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime
from urllib.parse import quote, unquote, urlparse

import caldav
from caldav.elements import dav, ical as ical_elements
from caldav.lib import error as caldav_error

from kairos import ical
from kairos.backends.base import AuthenticationError, Backend, BackendError
from kairos.config import settings
from kairos.models import Account, Calendar, Event
from kairos.security import (
    MAX_RESPONSE_BYTES,
    SecurityError,
    credentials,
    sanitise_text,
    validate_calendar_url,
)

log = logging.getLogger(__name__)

#: WebDAV property holding a collection's "has anything changed?" token.
CTAG_PROPERTY = "{http://calendarserver.org/ns/}getctag"
ETAG_PROPERTY = "{DAV:}getetag"

#: Colours handed out to newly discovered calendars that do not publish one.
FALLBACK_COLOURS = (
    "#3584e4", "#33d17a", "#f6d32d", "#ff7800",
    "#e01b24", "#9141ac", "#986a44", "#2190a4",
)


def calendar_id_for(url: str) -> str:
    """A stable local id for a calendar, derived from its URL.

    Using a hash rather than the URL itself keeps ids short, keeps them out
    of log messages in readable form, and means a calendar keeps its identity
    (and therefore its colour and visibility) across restarts.
    """
    return "cal-" + hashlib.sha256(url.encode("utf-8")).hexdigest()[:20]


class CalDAVBackend(Backend):
    """Talks to one CalDAV account."""

    def __init__(self, account: Account, *, password: str | None = None) -> None:
        super().__init__(account)
        self._password = password if password is not None else credentials.get_password(account.id)
        self._client: caldav.DAVClient | None = None
        self._principal = None

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def _connect(self) -> caldav.DAVClient:
        """Build the DAV client, reusing it for the life of this backend."""
        if self._client is not None:
            return self._client

        try:
            url = validate_calendar_url(
                self.account.url,
                allow_insecure_http=settings.get_bool("allow_insecure_http"),
            )
        except SecurityError as exc:
            raise BackendError(str(exc)) from exc

        verify = bool(self.account.verify_tls) and settings.get_bool("verify_tls_certificates")
        if not verify:
            log.warning("TLS verification is disabled for account %s", self.account.name)

        self._client = caldav.DAVClient(
            url=url,
            username=self.account.username or None,
            password=self._password or None,
            ssl_verify_cert=verify,
            timeout=settings.get_int("network_timeout_seconds"),
        )
        return self._client

    def _get_principal(self):
        if self._principal is None:
            client = self._connect()
            self._principal = self._guarded(client.principal, "connect to the server")
        return self._principal

    def _guarded(self, call, description: str, *args, **kwargs):
        """Run a caldav call and translate its failures into ours.

        Every network call in this module goes through here, so error
        handling lives in exactly one place.
        """
        try:
            return call(*args, **kwargs)
        except caldav_error.AuthorizationError as exc:
            raise AuthenticationError(
                f"The server rejected the username or password for “{self.account.name}”."
            ) from exc
        except caldav_error.NotFoundError as exc:
            raise BackendError(f"Could not {description}: the server returned “not found”.") from exc
        except caldav_error.DAVError as exc:
            raise BackendError(f"Could not {description}: {self._tidy(exc)}") from exc
        except Exception as exc:  # requests timeouts, DNS failures, TLS errors
            message = self._tidy(exc)
            if "certificate" in message.lower() or "ssl" in message.lower():
                raise BackendError(
                    f"Could not {description}: the server's security certificate "
                    f"could not be verified ({message})."
                ) from exc
            raise BackendError(f"Could not {description}: {message}") from exc

    def _tidy(self, exc: Exception) -> str:
        """A short, password-free description of an exception."""
        message = sanitise_text(str(exc) or exc.__class__.__name__, max_length=300)
        if self._password:
            message = message.replace(self._password, "***")
        return message

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def discover_calendars(self) -> list[Calendar]:
        """Ask the server which calendars this account can see.

        Most servers expose a *principal* that lists them.  Some setups (and
        some URLs people paste) point straight at a single calendar
        collection instead, so we fall back to treating the URL that way.
        """
        try:
            principal = self._get_principal()
            collections = self._guarded(principal.calendars, "list your calendars")
        except BackendError:
            collections = self._single_collection_fallback()

        calendars: list[Calendar] = []
        for index, collection in enumerate(collections):
            calendar = self._describe(collection, index)
            if calendar is not None:
                calendars.append(calendar)

        if not calendars:
            raise BackendError(
                "Connected to the server, but it did not offer any calendars "
                "for this account."
            )
        return calendars

    def _single_collection_fallback(self):
        """Treat the account URL itself as one calendar collection."""
        client = self._connect()
        collection = caldav.Calendar(client=client, url=self.account.url)
        # Touching a property proves the URL really is a calendar.
        self._guarded(collection.get_properties, "read the calendar", [dav.DisplayName()])
        return [collection]

    def _describe(self, collection, index: int) -> Calendar | None:
        """Turn a caldav collection into one of our Calendar records."""
        url = str(collection.url)

        # Skip collections that hold tasks or notes rather than events.
        try:
            components = collection.get_supported_components()
            if components and "VEVENT" not in components:
                return None
        except Exception:
            pass  # Servers that do not advertise this are assumed to hold events.

        name = ""
        colour = ""
        try:
            properties = collection.get_properties(
                [dav.DisplayName(), ical_elements.CalendarColor()]
            )
            name = sanitise_text(str(properties.get(dav.DisplayName.tag) or ""), max_length=120)
            colour = str(properties.get(ical_elements.CalendarColor.tag) or "").strip()
        except Exception as exc:
            log.debug("could not read properties of %s: %s", url, exc)

        if not name:
            name = url.rstrip("/").rsplit("/", 1)[-1] or self.account.name

        return Calendar(
            id=calendar_id_for(url),
            account_id=self.account.id,
            name=name,
            colour=_normalise_colour(colour) or FALLBACK_COLOURS[index % len(FALLBACK_COLOURS)],
            url=url,
            read_only=False,
            visible=True,
            sync_token=self._read_ctag(collection),
        )

    def _read_ctag(self, collection) -> str:
        """The collection's change token, or "" if the server has none.

        When this is unchanged since the last sync we can skip downloading
        the calendar entirely — the single biggest thing that keeps Kairos
        cheap to run.
        """
        try:
            properties = collection.get_properties([_CTag()])
            return sanitise_text(str(properties.get(CTAG_PROPERTY) or ""), max_length=200)
        except Exception:
            # Plenty of servers do not implement getctag.  That only costs us
            # a full download each sync, so it is not worth complaining about.
            return ""

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    def _collection(self, calendar: Calendar):
        client = self._connect()
        return caldav.Calendar(client=client, url=calendar.url)

    def current_sync_token(self, calendar: Calendar) -> str:
        return self._read_ctag(self._collection(calendar))

    def fetch_events(self, calendar: Calendar, start: datetime, end: datetime) -> list[Event]:
        """Download every event in the window, as master components."""
        collection = self._collection(calendar)
        results = self._guarded(
            collection.search,
            f"read “{calendar.name}”",
            start=start,
            end=end,
            event=True,
            expand=False,
        )
        etags = self._etags_in(collection)

        events: list[Event] = []
        for item in results:
            text = getattr(item, "data", None)
            if not text:
                continue
            if len(text) > MAX_RESPONSE_BYTES:
                log.warning("skipping oversized event resource at %s", item.url)
                continue
            href = str(item.url)
            try:
                parsed = ical.parse_calendar_text(
                    text, calendar.id, href=href, etag=etags.get(_path_of(href)) or None,
                )
            except ical.ParseError as exc:
                log.warning("server sent an unreadable event (%s)", exc)
                continue
            for event in parsed:
                event.raw_ics = text
                events.append(event)
        return events

    def _etags_in(self, collection) -> dict[str, str]:
        """Every resource's ETag in the collection, keyed by path.

        The calendar-query report that :meth:`fetch_events` uses returns the
        event data but not the ETags, so this is one extra PROPFIND per
        calendar per sync — cheap, and it is what makes the conditional write
        in :meth:`save_event` possible.  A server that will not answer simply
        leaves us without ETags, which costs correctness nothing: writes then
        fall back to unconditional.
        """
        request = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<d:propfind xmlns:d="DAV:"><d:prop><d:getetag/></d:prop></d:propfind>'
        )
        try:
            response = self._connect().propfind(str(collection.url), request, depth=1)
            found = response.expand_simple_props([dav.GetEtag()])
        except Exception as exc:
            log.debug("no ETags available for %s: %s", collection.url, exc)
            return {}

        etags: dict[str, str] = {}
        for href, properties in (found or {}).items():
            value = _clean_etag(properties.get(ETAG_PROPERTY, ""))
            if value:
                etags[_path_of(href)] = value
        return etags

    # ------------------------------------------------------------------
    # Writing
    # ------------------------------------------------------------------

    def save_event(self, calendar: Calendar, event: Event) -> Event:
        """Upload a new or edited event.

        Kairos issues the PUT itself rather than going through caldav's own
        save, because that is the only way to send the conditional headers:

        * a **new** event is written with ``If-None-Match: *``, so we cannot
          clobber something that turns out to be there already;
        * an **edited** event is written with ``If-Match: <etag>``, so the
          server refuses the write if somebody else changed it since we last
          looked.

        If the server does refuse (HTTP 412), the user's edit still wins — we
        log it, note it, and write again unconditionally.  Losing the edit the
        person just typed would be worse than overwriting the other change,
        and Kairos has no merge UI to offer instead.  See the README.
        """
        if calendar.read_only:
            raise BackendError(f"“{calendar.name}” is read-only.")

        text = ical.to_ical_text(event.copy(sequence=event.sequence + 1))
        href = event.href or self._url_for(calendar, event)

        headers = {"Content-Type": "text/calendar; charset=utf-8"}
        if event.href and event.etag:
            headers["If-Match"] = _quote_etag(event.etag)
        elif not event.href:
            headers["If-None-Match"] = "*"

        response = self._put(href, text, headers, event.summary)

        if response.status == 412:
            log.warning("“%s” changed on the server since Kairos last read it; "
                        "writing our version anyway", event.summary)
            response = self._put(href, text, {"Content-Type": headers["Content-Type"]},
                                 event.summary)

        if response.status not in (200, 201, 204):
            raise BackendError(
                f"The server refused to save “{event.summary}” "
                f"(HTTP {response.status})."
            )

        return event.copy(
            href=href,
            etag=_clean_etag(response.headers.get("ETag", "")) or None,
            sequence=event.sequence + 1,
            raw_ics=text,
            dirty=False,
        )

    def _put(self, url: str, text: str, headers: dict, summary: str):
        """One conditional PUT, with our usual error translation."""
        return self._guarded(
            self._connect().put, f"save “{summary}”", url, text.encode("utf-8"), headers
        )

    def _url_for(self, calendar: Calendar, event: Event) -> str:
        """Where a brand-new event should live on the server.

        Choosing the URL ourselves (collection + UID + ".ics") is what every
        CalDAV client does, and it means a retry after a dropped connection
        lands on the same resource instead of creating a duplicate.
        """
        base = calendar.url if calendar.url.endswith("/") else calendar.url + "/"
        return base + quote(event.uid, safe="") + ".ics"

    def delete_event(self, calendar: Calendar, event: Event) -> None:
        """Delete an event, treating "already gone" as success."""
        if calendar.read_only:
            raise BackendError(f"“{calendar.name}” is read-only.")
        if not event.href:
            return  # Never reached the server in the first place.

        client = self._connect()
        remote = caldav.Event(client=client, url=event.href, parent=self._collection(calendar))
        try:
            remote.delete()
        except caldav_error.NotFoundError:
            return
        except caldav_error.AuthorizationError as exc:
            raise AuthenticationError(
                f"The server would not let this account delete from “{calendar.name}”."
            ) from exc
        except Exception as exc:
            raise BackendError(
                f"Could not delete “{event.summary}”: {self._tidy(exc)}"
            ) from exc


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

class _CTag(dav.ValuedBaseElement):
    """The calendar-server ``getctag`` property, which caldav does not define."""
    tag = CTAG_PROPERTY


def _path_of(url: str) -> str:
    """The path part of a URL, unescaped, for matching hrefs to resources.

    A calendar-query returns absolute URLs while a PROPFIND returns paths, and
    either may percent-escape differently.  Comparing unescaped paths is the
    only thing that reliably matches the two up.
    """
    path = urlparse(str(url)).path or str(url)
    return unquote(path).rstrip("/")


def _clean_etag(value: str) -> str:
    """Strip the quotes and any weak-comparison marker from an ETag."""
    text = str(value or "").strip()
    if text.startswith("W/"):
        text = text[2:]
    return text.strip('"').strip()


def _quote_etag(value: str) -> str:
    """Put an ETag back into the form a header wants."""
    text = _clean_etag(value)
    return f'"{text}"' if text else "*"


def _normalise_colour(value: str) -> str:
    """Accept the colour forms CalDAV servers actually send.

    Apple-flavoured servers append an alpha channel (``#RRGGBBAA``); some send
    a bare name.  We return a plain ``#RRGGBB`` or "" if we cannot tell.
    """
    value = (value or "").strip()
    if not value:
        return ""
    if value.startswith("#"):
        digits = value[1:]
        if len(digits) in (6, 8) and all(c in "0123456789abcdefABCDEF" for c in digits):
            return "#" + digits[:6].lower()
        if len(digits) == 3 and all(c in "0123456789abcdefABCDEF" for c in digits):
            return "#" + "".join(c * 2 for c in digits).lower()
        return ""
    # A CSS colour name; let GTK decide whether it is real.
    if value.replace(" ", "").isalpha() and len(value) <= 24:
        return value.lower()
    return ""
