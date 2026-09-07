#!/usr/bin/env python3
"""Capture the screenshots used in the documentation.

    ./packaging/make-screenshots.py

Writes PNGs into ``docs/images/``. Run it after a change that alters how
Kairos looks, so the documentation does not drift away from the application.

It runs Kairos against a **throwaway configuration directory** with a made-up
calendar, so nothing personal can end up in a committed screenshot, and the
result is the same whoever runs it. Dates are relative to today, so the shots
never look stale.

Needs a running X session, plus ``wmctrl`` and ``gnome-screenshot``.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
OUTPUT = PROJECT / "docs" / "images"

#: Screenshots are taken on a HiDPI display and scaled down to this width, so
#: they stay sharp on GitHub without being megabytes each.
TARGET_WIDTH = 1400

WINDOW_TITLE = "KairosShots"
WINDOW_SIZE = (1360, 880)


def require(*commands: str) -> None:
    missing = [c for c in commands if subprocess.run(
        ["which", c], capture_output=True).returncode != 0]
    if missing:
        sys.exit(f"These are needed but not installed: {', '.join(missing)}")


def main() -> int:
    require("wmctrl", "gnome-screenshot")
    if not os.environ.get("DISPLAY"):
        sys.exit("No DISPLAY; screenshots need a running X session.")

    sandbox = tempfile.mkdtemp(prefix="kairos-shots-")
    os.environ.update(
        XDG_CONFIG_HOME=f"{sandbox}/config",
        XDG_CACHE_HOME=f"{sandbox}/cache",
        XDG_DATA_HOME=f"{sandbox}/data",
    )
    sys.path.insert(0, str(PROJECT))
    OUTPUT.mkdir(parents=True, exist_ok=True)

    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, GLib  # noqa: E402

    Adw.init()

    from kairos.config import settings
    from kairos.models import Alarm, Event, local_timezone, start_of_day
    from kairos.sync import SyncManager
    from kairos.theming import ThemeManager
    from kairos.ui.event_editor import EventEditor
    from kairos.ui.window import CalendarWindow
    from datetime import datetime

    settings.set("time_format", "12h")
    settings.set("theme", "dark")
    settings.set("accent_color", "#9141ac")
    settings.set("sync_on_startup", False)
    settings.set("sync_interval_minutes", 0)

    theme = ThemeManager()
    theme.apply()
    sync = SyncManager()

    # ---- a made-up week that shows the app off ------------------------
    personal = sync.calendars()[0]
    work = sync.add_local_calendar("Work", "#e01b24")
    family = sync.add_local_calendar("Family", "#33d17a")

    # Anchor the demo week to the Monday of the week now on screen, so the
    # week view is full whatever day the script is run on. Offsets below are
    # therefore Monday = 0.
    from kairos import formatting
    today = datetime.now(tz=local_timezone()).date()
    midnight = start_of_day(formatting.week_start(today))

    def add(calendar, title, day, start_hour, hours, location="", notes="",
            alarms=()):
        start = midnight + timedelta(days=day, hours=start_hour)
        event = Event.new(calendar.id, start, start + timedelta(hours=hours), title)
        event.location = location
        event.description = notes
        event.alarms = [Alarm(minutes) for minutes in alarms]
        sync.save_event(event)
        return event

    def all_day(calendar, title, day, days=1):
        start = midnight + timedelta(days=day)
        sync.save_event(Event.new(calendar.id, start, start + timedelta(days=days),
                                  title, all_day=True))

    add(work, "Standup", 0, 9.5, 0.25, "Room 3")
    # The event the editor screenshot is taken of, so it has a full set of
    # fields rather than showing an empty "New event" form.
    review = add(work, "Design review", 0, 10, 1.5, "Room 3",
                 "Walk through the new grid layout.", alarms=(15,))
    add(work, "Platform sync", 0, 10.5, 1)
    add(personal, "Lunch with Sam", 0, 12.5, 1, "Café Rosa")
    add(work, "1:1 with Priya", 0, 15, 0.5)
    add(personal, "Yoga", 0, 18, 1, "The studio")

    add(work, "Sprint planning", 1, 9, 2, "Room 1")
    add(family, "School pickup", 1, 15.5, 0.5)
    add(personal, "Dentist", 2, 8.5, 0.75, "High Street")
    add(work, "Retro", 2, 16, 1)
    add(work, "Interview", 3, 11, 1)
    add(family, "Football practice", 3, 17, 1.5)
    add(personal, "Cinema", 4, 19, 2.5, "The Ritzy")
    add(work, "Board meeting", 5, 13, 2, "Head office")

    all_day(family, "Trip to Lisbon", 2, 3)
    all_day(personal, "Anna's birthday", 6)

    # Give "Up next" something to show whatever time of day this runs. Late in
    # the evening these go to tomorrow morning instead, so the sidebar never
    # ends up listing a meeting at midnight.
    now = datetime.now(tz=local_timezone()).replace(minute=0, second=0, microsecond=0)
    if now.hour >= 20:
        base = start_of_day(today + timedelta(days=1)) + timedelta(hours=9)
        offsets = (0, 2)
    else:
        base, offsets = now, (1, 2)
    for offset, (calendar, title, place) in zip(offsets, [
        (work, "Catch-up with Dana", "Room 2"),
        (personal, "Pick up parcel", "Post office"),
    ]):
        start = base + timedelta(hours=offset)
        event = Event.new(calendar.id, start, start + timedelta(minutes=45), title)
        event.location = place
        sync.save_event(event)

    weekly = Event.new(work.id, midnight + timedelta(days=-14, hours=9),
                       midnight + timedelta(days=-14, hours=9, minutes=30),
                       "Weekly sync")
    weekly.rrule = "FREQ=WEEKLY;BYDAY=MO,WE,FR"
    sync.save_event(weekly)

    morning = Event.new(personal.id, midnight + timedelta(days=-20, hours=7),
                        midnight + timedelta(days=-20, hours=7, minutes=45),
                        "Morning run")
    morning.rrule = "FREQ=DAILY;INTERVAL=2"
    sync.save_event(morning)

    # ---- drive the window and capture ---------------------------------
    application = Adw.Application(application_id="org.kairos.Screenshots")

    def capture(name: str, title: str = WINDOW_TITLE) -> None:
        """Focus a window by title and photograph it."""
        listing = subprocess.run(["wmctrl", "-l"], capture_output=True, text=True).stdout
        window_id = next(
            (line.split()[0] for line in listing.splitlines()
             if line.rstrip().endswith(title)), None)
        if window_id is None:
            print(f"  ! {name}: no window titled {title!r}")
            return
        subprocess.run(["wmctrl", "-i", "-a", window_id])
        subprocess.run(["sleep", "1"])
        path = OUTPUT / f"{name}.png"
        subprocess.run(["gnome-screenshot", "-w", "-f", str(path)])
        print(f"  captured {path.relative_to(PROJECT)}")

    def on_activate(app):
        window = CalendarWindow(app, sync, theme)
        window.set_title(WINDOW_TITLE)
        window.set_default_size(*WINDOW_SIZE)
        window.set_visible(True)

        def week_at_work():
            """Show the week scrolled to the working day.

            Left alone the view scrolls to the current time, which makes a
            screenshot depend on the hour it was taken — an empty 3am grid is
            not what the documentation wants to show.
            """
            window.show_view("week")
            view = window._views["week"]
            hour_height = settings.get_int("hour_height")
            GLib.timeout_add(700, lambda: (
                view._scroller.get_vadjustment().set_value(7.5 * hour_height),
                GLib.SOURCE_REMOVE)[1])

        def set_theme(name: str) -> None:
            """Switch light/dark.

            The application wires the theme manager to the settings; this
            script builds one directly, so it has to apply the change itself.
            """
            settings.set("theme", name)
            theme.apply_colour_scheme()

        def editor_on_an_event():
            """Open the editor on an event that has every field filled in.

            A blank "New event" form shows the layout but none of what the
            fields are for, so this edits the demo meeting instead.
            """
            editor = EventEditor(sync.writable_calendars(), event=review)
            editor.present(window)

        steps = [
            ("month-view", lambda: (set_theme("dark"), window.show_view("month"))),
            ("week-view", week_at_work),
            ("agenda-view", lambda: window.show_view("agenda")),
            ("search", lambda: (window.show_view("month"), window.start_search(),
                                window._search_entry.set_text("re"))),
            ("light-theme", lambda: (window.stop_search(), set_theme("light"),
                                     week_at_work())),
            # The editor last: it leaves a dialog open over the window, which
            # would sit on top of every shot taken after it.
            ("event-editor", lambda: (set_theme("dark"), window.show_view("month"),
                                      editor_on_an_event())),
        ]

        def run(index=0):
            if index >= len(steps):
                GLib.timeout_add(400, lambda: (app.quit(), GLib.SOURCE_REMOVE)[1])
                return GLib.SOURCE_REMOVE
            name, action = steps[index]
            action()
            def shoot():
                capture(name)
                GLib.timeout_add(500, lambda: run(index + 1))
                return GLib.SOURCE_REMOVE
            GLib.timeout_add(1800, shoot)
            return GLib.SOURCE_REMOVE

        GLib.timeout_add_seconds(2, run)

    application.connect("activate", on_activate)
    application.run([])

    scale_down()
    return 0


def scale_down() -> None:
    """Shrink the HiDPI captures and strip them down for the repository."""
    try:
        from PIL import Image
    except ImportError:
        print("  (install Pillow to shrink the screenshots)")
        return

    total = 0
    for path in sorted(OUTPUT.glob("*.png")):
        image = Image.open(path)
        if image.width > TARGET_WIDTH:
            height = round(image.height * TARGET_WIDTH / image.width)
            image = image.resize((TARGET_WIDTH, height), Image.LANCZOS)
        image.convert("RGB").save(path, "PNG", optimize=True)
        total += path.stat().st_size
        print(f"  {path.name}: {image.width}x{image.height}, "
              f"{path.stat().st_size / 1024:.0f} KB")
    print(f"  {total / 1024:.0f} KB in total")


if __name__ == "__main__":
    sys.exit(main())
