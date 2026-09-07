"""Ask a CalDAV server what it actually returns, and say where it went wrong.

    kairos --diagnose https://dav.example.com/ you@example.com

For the case where Kairos finds your calendars but shows no events in them,
which can happen for several unrelated reasons that are impossible to tell
apart from the outside.  It walks the same path Kairos does — discover the
calendars, ask each one for its events, parse them — printing what came back
at every step.

It only ever reads.  Nothing is written to the server and nothing is written
to your configuration.  Event titles are not printed unless you ask for them,
so the output is safe to paste into a bug report.
"""

from __future__ import annotations

import argparse
import getpass
from datetime import datetime, timedelta


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="the CalDAV address, as given to Kairos")
    parser.add_argument("username")
    parser.add_argument("--days", type=int, default=365,
                        help="how far either side of today to look (default 365)")
    parser.add_argument("--show-titles", action="store_true",
                        help="print event titles; off by default so the output "
                             "can be shared")
    parser.add_argument("--insecure", action="store_true",
                        help="do not verify the TLS certificate")
    arguments = parser.parse_args(argv)

    password = getpass.getpass("Password (not echoed, not stored): ")

    import caldav

    from kairos import ical
    from kairos.backends.caldav_backend import CalDAVBackend
    from kairos.config import settings
    from kairos.models import CALDAV, Account, local_timezone

    settings.set("allow_insecure_http", True)   # this tool is for diagnosing

    account = Account(id="diagnose", name="Diagnostic", kind=CALDAV,
                      url=arguments.url, username=arguments.username,
                      verify_tls=not arguments.insecure)
    backend = CalDAVBackend(account, password=password)

    now = datetime.now(tz=local_timezone())
    start = now - timedelta(days=arguments.days)
    end = now + timedelta(days=arguments.days)
    print(f"\nWindow: {start:%Y-%m-%d} to {end:%Y-%m-%d}\n")

    print("1. Discovering calendars...")
    try:
        calendars = backend.discover_calendars()
    except Exception as exc:
        print(f"   FAILED: {exc.__class__.__name__}: {exc}")
        return 1
    print(f"   {len(calendars)} calendar(s)")
    for calendar in calendars:
        print(f"     - {calendar.name}  read_only={calendar.read_only}")
        print(f"       {calendar.url}")
    if not calendars:
        print("\n   No calendars, so there is nothing to read events from.")
        return 1

    client = backend._connect()
    for calendar in calendars:
        print(f"\n2. Reading “{calendar.name}”")
        collection = caldav.Calendar(client=client, url=calendar.url)

        # (a) the query Kairos actually makes
        try:
            ranged = collection.search(start=start, end=end, event=True,
                                       expand=False)
            print(f"   a. ranged search      : {len(ranged)} object(s)")
        except Exception as exc:
            ranged = []
            print(f"   a. ranged search      : FAILED {exc.__class__.__name__}: {exc}")

        # (b) everything the collection holds, ignoring dates
        try:
            everything = collection.events()
            print(f"   b. every event        : {len(everything)} object(s)")
        except Exception as exc:
            everything = []
            print(f"   b. every event        : FAILED {exc.__class__.__name__}: {exc}")

        # (c) no REPORT at all: list the collection and read one resource
        try:
            children = collection.children()
            print(f"   c. PROPFIND listing   : {len(children)} resource(s)")
        except Exception as exc:
            children = []
            print(f"   c. PROPFIND listing   : FAILED {exc.__class__.__name__}: {exc}")

        listed = []
        if children and not (ranged or everything):
            listed = backend._list_the_hard_way(collection, calendar)
            print(f"      readable by GET    : {len(listed)}")

        # (d) is the calendar data actually attached to what came back?
        sample = (ranged or everything or listed)
        if not sample:
            print("   -> Nothing came back by any route; the collection looks "
                  "empty to us.")
            continue

        item = sample[0]
        raw = getattr(item, "data", None)
        print(f"   d. first object's data: "
              f"{'present, ' + str(len(raw)) + ' bytes' if raw else 'EMPTY'}")
        if not raw:
            try:
                item.load()
                raw = getattr(item, "data", None)
                print(f"      after .load()      : "
                      f"{'present, ' + str(len(raw)) + ' bytes' if raw else 'still empty'}")
            except Exception as exc:
                print(f"      .load() FAILED     : {exc.__class__.__name__}: {exc}")

        # (e) can we parse it?
        parsed_total, unparseable = 0, 0
        for entry in sample:
            text = getattr(entry, "data", None)
            if not text:
                unparseable += 1
                continue
            try:
                events = ical.parse_calendar_text(text, calendar.id)
                parsed_total += len(events)
            except ical.ParseError:
                unparseable += 1
        print(f"   e. parsed             : {parsed_total} event(s), "
              f"{unparseable} unreadable")

        if arguments.show_titles:
            for entry in sample[:5]:
                text = getattr(entry, "data", None)
                if not text:
                    continue
                for event in ical.parse_calendar_text(text, calendar.id):
                    print(f"        {event.start:%Y-%m-%d %H:%M}  {event.summary}")

        # (f) the verdict for this calendar
        if not ranged and everything:
            print("   -> The server ignores or mishandles the date filter. "
                  "Kairos falls back to reading everything, so a current "
                  "build handles this.")
        elif not ranged and not everything and listed:
            print("   -> This server's calendar-query REPORT returns nothing. "
                  "Kairos falls back to a plain PROPFIND listing, so a "
                  "current build handles this.")
        elif not ranged and not everything and children and not listed:
            print("   -> The collection lists resources but none of them could "
                  "be read or parsed. Please report this output.")
        elif not ranged and not everything and not children:
            print("   -> The server reports this calendar as empty.")
        elif parsed_total == 0:
            print("   -> Objects came back but none could be parsed; the "
                  "iCalendar is unusual. Please report this output.")
        else:
            print("   -> This calendar looks fine.")
    return 0
