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
    parser.add_argument("url", nargs="?",
                        help="the CalDAV address. Leave it out to diagnose "
                             "the accounts already configured, using their "
                             "own settings and stored passwords")
    parser.add_argument("username", nargs="?")
    parser.add_argument("--days", type=int, default=365,
                        help="how far either side of today to look (default 365)")
    parser.add_argument("--show-titles", action="store_true",
                        help="print event titles; off by default so the output "
                             "can be shared")
    parser.add_argument("--insecure", action="store_true",
                        help="do not verify the TLS certificate")
    arguments = parser.parse_args(argv)

    if arguments.url is None:
        return _diagnose_configured_accounts(arguments)
    if arguments.username is None:
        print("Give a username as well, or no arguments at all to use the "
              "accounts you have already set up.")
        return 2

    password = getpass.getpass("Password (not echoed, not stored): ")

    from kairos.config import settings

    # A plain-http server is exactly the case people need to diagnose, and
    # Kairos refuses those by default. Relax it for the length of this run
    # only: leaving a security setting turned on behind someone's back
    # because they once ran a diagnostic would be indefensible.
    was_allowed = settings.get_bool("allow_insecure_http")
    relaxed = arguments.url.strip().lower().startswith("http://")
    if relaxed and not was_allowed:
        settings.set("allow_insecure_http", True)
        print("(temporarily allowing plain http for this run)")
    try:
        return _walk(arguments, password)
    finally:
        if relaxed and not was_allowed:
            settings.set("allow_insecure_http", was_allowed)


def _diagnose_configured_accounts(arguments) -> int:
    """Ask about the accounts Kairos already has, exactly as Kairos does.

    This is the form to reach for. Typing a URL again builds a *fresh*
    account with default settings, which is how someone whose server has a
    self-signed certificate gets a certificate error from the diagnostic
    while the application itself works — the saved account has verification
    turned off and the new one does not.
    """
    from kairos.accounts import AccountStore
    from kairos.security import credentials

    accounts = [a for a in AccountStore().all() if not a.is_local]
    if not accounts:
        print("No calendar accounts are configured. Pass a URL and username "
              "to ask about a server directly.")
        return 1

    worst = 0
    for account in accounts:
        print(f"\n=== {account.name} ===")
        print(f"    {account.url}")
        print(f"    verify certificate: {account.verify_tls}"
              f"{'   (OAuth)' if account.uses_oauth else ''}")
        password = credentials.get_password(account.id) or ""
        if not password and not account.uses_oauth:
            print("    No stored password — is there a keyring on this machine?")
        worst = max(worst, _walk(arguments, password, account=account))
    return worst


def _walk(arguments, password: str, account=None) -> int:
    """The actual questions, once the URL is allowed to be asked about."""
    import caldav

    from kairos import ical
    from kairos.backends.caldav_backend import CalDAVBackend
    from kairos.models import CALDAV, Account, local_timezone

    if account is None:
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
        if "certificate" in str(exc).lower() or "SSL" in str(exc):
            print("\n   This is a TLS problem, not a calendar one. The server "
                  "is presenting a\n   certificate this machine cannot trace "
                  "to an authority it trusts —\n   usually a self-signed one, "
                  "or a NAS serving no intermediate.\n")
            print("   Three ways out, best first:")
            print("     * point Kairos at the certificate authority that "
                  "signed it:\n"
                  "       set ca_certificate_path in settings.json, or the "
                  "matching row\n"
                  "       in Preferences → Sync & security;")
            print("     * get a certificate the machine already trusts "
                  "(Let's Encrypt is\n       free, and Synology can request "
                  "one for you);")
            print("     * turn off \"Verify the security certificate\" for "
                  "this account —\n       it works, and it means anyone on "
                  "the network between you and the\n       server can read "
                  "and alter your calendar.")
            print("\n   To carry on diagnosing right now, add --insecure.")
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
