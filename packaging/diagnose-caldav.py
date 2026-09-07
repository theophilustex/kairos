#!/usr/bin/env python3
"""Ask a CalDAV server what it actually returns, and say where it went wrong.

    ./packaging/diagnose-caldav.py https://dav.example.com/ you@example.com

Prompts for the password, then walks the same path Kairos does — discover the
calendars, ask each one for its events, parse them — printing what came back
at every step.  Written for the case where Kairos finds your calendars but
shows no events in them, which can happen for several unrelated reasons and
is impossible to tell apart from the outside.

It only ever reads.  Nothing is written to the server and nothing is written
to your Kairos configuration.  Event titles are not printed unless you pass
``--show-titles``, so the output is safe to paste into a bug report.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> int:
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
    arguments = parser.parse_args()

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

        # (c) is the calendar data actually attached to what came back?
        sample = (ranged or everything)
        if not sample:
            print("   c. nothing to inspect — the collection looks empty to us")
            continue

        item = sample[0]
        raw = getattr(item, "data", None)
        print(f"   c. first object's data: "
              f"{'present, ' + str(len(raw)) + ' bytes' if raw else 'EMPTY'}")
        if not raw:
            try:
                item.load()
                raw = getattr(item, "data", None)
                print(f"      after .load()      : "
                      f"{'present, ' + str(len(raw)) + ' bytes' if raw else 'still empty'}")
            except Exception as exc:
                print(f"      .load() FAILED     : {exc.__class__.__name__}: {exc}")

        # (d) can we parse it?
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
        print(f"   d. parsed             : {parsed_total} event(s), "
              f"{unparseable} unreadable")

        if arguments.show_titles:
            for entry in sample[:5]:
                text = getattr(entry, "data", None)
                if not text:
                    continue
                for event in ical.parse_calendar_text(text, calendar.id):
                    print(f"        {event.start:%Y-%m-%d %H:%M}  {event.summary}")

        # (e) the verdict for this calendar
        if not ranged and everything:
            print("   -> The server ignores or mishandles the date filter. "
                  "Kairos works around this by falling back to reading "
                  "everything; make sure you are on a build that does.")
        elif not ranged and not everything:
            print("   -> The server reports this calendar as empty.")
        elif parsed_total == 0:
            print("   -> Objects came back but none could be parsed; the "
                  "iCalendar is unusual. Please report this output.")
        else:
            print("   -> This calendar looks fine.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
