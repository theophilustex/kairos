"""The application object: start-up, shutdown, actions and shortcuts.

Kairos is a single-instance :class:`Adw.Application`.  Launching it a second
time raises the existing window instead of starting a second copy, which
matters because two processes sharing one cache would fight over it.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
from datetime import date

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib  # noqa: E402

from kairos import APP_ID, APP_NAME, VERSION
from kairos.config import ensure_directories, settings
from kairos.notifications import AlarmScheduler
from kairos.sync import SyncManager
from kairos.theming import ThemeManager
from kairos.ui.window import CalendarWindow

log = logging.getLogger(__name__)

#: ``action -> keyboard shortcuts``.  Editing this table is the whole story
#: for rebinding a key.
SHORTCUTS = {
    "win.new-event": ["<Control>n"],
    "win.today": ["<Control>t"],
    "win.sync": ["<Control>r", "F5"],
    "win.preferences": ["<Control>comma"],
    "win.calendars": ["<Control>l"],
    "win.previous": ["<Alt>Left", "<Control>Page_Up"],
    "win.next": ["<Alt>Right", "<Control>Page_Down"],
    "win.view-month": ["<Control>1"],
    "win.view-week": ["<Control>2"],
    "win.view-day": ["<Control>3"],
    "win.view-agenda": ["<Control>4"],
    "app.quit": ["<Control>q"],
    "win.close": ["<Control>w"],
}


class KairosApplication(Adw.Application):
    """Owns the long-lived pieces: the cache, the sync worker, the alarms."""

    def __init__(self) -> None:
        super().__init__(
            application_id=APP_ID,
            flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE,
        )
        self.sync: SyncManager | None = None
        self.theme: ThemeManager | None = None
        self.alarms: AlarmScheduler | None = None
        self.window: CalendarWindow | None = None

        self.add_main_option(
            "version", ord("v"), GLib.OptionFlags.NONE, GLib.OptionArg.NONE,
            "Show the version and exit", None,
        )
        self.add_main_option(
            "debug", ord("d"), GLib.OptionFlags.NONE, GLib.OptionArg.NONE,
            "Log everything, loudly", None,
        )
        self.add_main_option(
            "new-event", ord("n"), GLib.OptionFlags.NONE, GLib.OptionArg.NONE,
            "Open the new-event dialog straight away", None,
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        ensure_directories()

        self.theme = ThemeManager()
        self.theme.apply()
        self.theme.watch_user_css()
        settings.connect(self.theme.on_settings_changed)

        self.sync = SyncManager()
        self.alarms = AlarmScheduler(self, self.sync)
        self.sync.connect("events-changed", self.alarms.reschedule)
        settings.connect(lambda key: self.alarms.reschedule() if key in
                         (None, "notifications_enabled", "notification_lookahead_minutes") else None)

        self._install_actions()

    def do_activate(self) -> None:
        if self.window is None:
            self.window = CalendarWindow(self, self.sync, self.theme)
            self.sync.start()
            self.alarms.start()
        self.window.present()

    def do_command_line(self, command_line: Gio.ApplicationCommandLine) -> int:
        options = command_line.get_options_dict().end().unpack()
        if options.get("version"):
            command_line.print_literal(f"{APP_NAME} {VERSION}\n")
            return 0
        _configure_logging(bool(options.get("debug")))
        self.activate()
        if options.get("new-event") and self.window is not None:
            self.window.new_event()
        return 0

    def do_shutdown(self) -> None:
        if self.alarms is not None:
            self.alarms.stop()
        if self.sync is not None:
            self.sync.stop()
            self.sync.storage.close()
        Adw.Application.do_shutdown(self)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _install_actions(self) -> None:
        quit_action = Gio.SimpleAction.new("quit", None)
        quit_action.connect("activate", lambda *_: self.quit())
        self.add_action(quit_action)

        # Clicking a reminder notification opens the day it belongs to.
        show_day = Gio.SimpleAction.new("show-day", GLib.VariantType.new("s"))
        show_day.connect("activate", self._on_show_day)
        self.add_action(show_day)

        for action, keys in SHORTCUTS.items():
            self.set_accels_for_action(action, keys)

    def _on_show_day(self, _action, parameter: GLib.Variant) -> None:
        self.activate()
        try:
            day = date.fromisoformat(parameter.get_string())
        except ValueError:
            return
        if self.window is not None:
            self.window.go_to_day(day)


def _configure_logging(debug: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    if not debug:
        # These two are chatty at INFO and have nothing to say to a user.
        logging.getLogger("urllib3").setLevel(logging.WARNING)
        logging.getLogger("caldav").setLevel(logging.WARNING)


def main(argv: list[str] | None = None) -> int:
    """Entry point for both ``python -m kairos`` and the ``kairos`` command."""
    argv = list(sys.argv if argv is None else argv)

    # Argparse handles --help nicely before GTK gets involved.
    if "--help" in argv or "-h" in argv:
        parser = argparse.ArgumentParser(
            prog="kairos", description=f"{APP_NAME} — a calendar for Linux."
        )
        parser.add_argument("-v", "--version", action="store_true", help="show the version and exit")
        parser.add_argument("-d", "--debug", action="store_true", help="log everything, loudly")
        parser.add_argument("-n", "--new-event", action="store_true",
                            help="open the new-event dialog straight away")
        parser.print_help()
        return 0

    _configure_logging("--debug" in argv or "-d" in argv)

    # GTK derives the window's WM_CLASS from the program name, and the desktop
    # shell uses WM_CLASS to decide which .desktop file — and therefore which
    # icon — a window belongs to. Without this the class comes out as
    # "__main__.py" or "python3", the match fails, and the taskbar shows a
    # generic icon however well the icon itself is installed. It has to happen
    # before the first window is realised.
    GLib.set_prgname(APP_ID)
    GLib.set_application_name(APP_NAME)

    # Ctrl+C at a terminal should close the app, not leave it wedged.
    signal.signal(signal.SIGINT, signal.SIG_DFL)

    return KairosApplication().run(argv)
