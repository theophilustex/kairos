"""The application object: start-up, shutdown, actions and shortcuts.

Kairos is a single-instance :class:`Adw.Application`.  Launching it a second
time raises the existing window instead of starting a second copy, which
matters because two processes sharing one cache would fight over it.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
from datetime import date
from pathlib import Path

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
    "win.search": ["<Control>f"],
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
        self.alert_window = None
        self.tray = None

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
        self.add_main_option(
            "background", ord("b"), GLib.OptionFlags.NONE, GLib.OptionArg.NONE,
            "Start without showing the window, so reminders still arrive", None,
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

        settings.connect(self._on_settings_changed)

        self.sync = SyncManager()
        self.alarms = AlarmScheduler(self, self.sync)
        # A due reminder raises a window that asks for focus; the scheduler
        # knows nothing about windows, so the wiring lives here.
        self.alarms.on_alert = self.present_reminder
        self.sync.connect("events-changed", self.alarms.reschedule)
        settings.connect(lambda key: self.alarms.reschedule() if key in
                         (None, "notifications_enabled", "notification_lookahead_minutes") else None)

        self._install_actions()

    def _ensure_running(self) -> None:
        """Build the window and start the background machinery, once.

        Separate from :meth:`do_activate` because ``--background`` needs
        everything running *without* showing the window.
        """
        if self.window is not None:
            return
        self.window = CalendarWindow(self, self.sync, self.theme)
        self.apply_background_mode()
        self.sync.start()
        self.alarms.start()
        self._start_tray()

    def do_activate(self) -> None:
        self._ensure_running()
        self.window.present()

    def do_command_line(self, command_line: Gio.ApplicationCommandLine) -> int:
        options = command_line.get_options_dict().end().unpack()
        if options.get("version"):
            command_line.print_literal(f"{APP_NAME} {VERSION}\n")
            return 0
        _configure_logging(bool(options.get("debug")))

        if options.get("background") and self.window is None:
            # Start up complete but out of sight. Launching Kairos again, or
            # clicking the tray icon, brings the window up.
            self._ensure_running()
            log.info("started in the background")
            return 0

        self.activate()
        if options.get("new-event") and self.window is not None:
            self.window.new_event()
        return 0

    def do_shutdown(self) -> None:
        if self.tray is not None:
            self.tray.stop()
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

    # ------------------------------------------------------------------
    # Running in the background
    # ------------------------------------------------------------------

    def apply_background_mode(self) -> None:
        """Decide what closing the window means.

        With background running on, closing hides the window instead of
        destroying it. The window object still exists, so GTK keeps the
        application alive, the sync timer keeps ticking and reminders keep
        arriving. With it off, closing destroys the last window and Kairos
        exits, which is what most applications do.
        """
        if self.window is not None:
            self.window.set_hide_on_close(settings.get_bool("run_in_background"))

    def _start_tray(self) -> None:
        """Put an icon in the taskbar, if this desktop has one."""
        from kairos.tray import MenuItem, TrayIcon

        icon_directory = _icon_directory()
        self.tray = TrayIcon(
            icon_name=APP_ID,
            title=APP_NAME,
            tooltip="Calendar and reminders",
            icon_theme_path=str(icon_directory) if icon_directory else "",
            icon_files=_tray_icon_files(),
            on_activate=self.toggle_window,
            items=[
                MenuItem("Open Kairos", self.activate),
                MenuItem("New event", self._tray_new_event),
                MenuItem("Sync now", lambda: self.sync.sync_now()),
                MenuItem(separator=True),
                MenuItem("Quit Kairos", self.quit),
            ],
        )
        self.tray.start()

    def toggle_window(self) -> None:
        """What clicking the taskbar icon does.

        Show the window if it is hidden, raise it if it is behind something,
        and hide it if it is already in front — the usual behaviour for a
        tray icon, and the reason "visible" alone is not the right test: a
        window buried under a browser is visible, and clicking the icon then
        should bring it forward rather than hide it.
        """
        if self.window is None:
            self.activate()
            return
        if self.window.get_visible() and self.window.is_active():
            self.window.set_visible(False)
        else:
            self.window.present()

    def _tray_new_event(self) -> None:
        self.activate()
        if self.window is not None:
            self.window.new_event()

    # ------------------------------------------------------------------
    # Reminder alerts
    # ------------------------------------------------------------------

    def present_reminder(self, reminder) -> None:
        """Put a due reminder on screen, in front of whatever is there.

        One window holds every reminder that is currently due, so three
        overlapping meetings raise one window with three rows rather than
        three windows.
        """
        from kairos.ui.reminder_alert import ReminderAlertWindow

        if self.alert_window is None:
            window = ReminderAlertWindow(self)
            window.connect("snoozed", lambda _w, r, m: self.alarms.snooze(r, m))
            window.connect("dismissed", lambda _w, r: self.alarms.dismiss(r))
            window.connect("opened", self._on_reminder_opened)
            window.connect("close-request", self._on_alert_closed)
            self.alert_window = window

        self.alert_window.show_reminder(reminder)

    def _on_alert_closed(self, _window) -> bool:
        # Let the window finish closing, then forget it; the next reminder
        # builds a fresh one.
        self.alert_window = None
        return False

    def _on_reminder_opened(self, _window, reminder) -> None:
        """"Show in calendar" — bring up the day the event is on."""
        self.alarms.dismiss(reminder)
        self.activate()
        if self.window is not None:
            self.window.go_to_day(reminder.start.date())

    def _on_settings_changed(self, key: str | None) -> None:
        if key in (None, "run_in_background"):
            self.apply_background_mode()

    def _on_show_day(self, _action, parameter: GLib.Variant) -> None:
        self.activate()
        try:
            day = date.fromisoformat(parameter.get_string())
        except ValueError:
            return
        if self.window is not None:
            self.window.go_to_day(day)


def _icon_roots() -> list:
    """Every directory that might hold our installed icons.

    Kairos runs three ways and the icons land somewhere different each time:
    beside the source in a checkout, under the install prefix once installed,
    and inside the bundle in an AppImage. The XDG data directories cover the
    last two — an AppImage puts its own ``usr/share`` at the front of
    ``XDG_DATA_DIRS`` — and the checkout is checked explicitly.
    """
    roots = [Path(__file__).resolve().parent.parent / "data" / "icons" / "hicolor"]

    data_dirs = os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share"
    data_home = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local/share")
    for directory in [data_home, *data_dirs.split(":")]:
        if directory:
            roots.append(Path(directory) / "icons" / "hicolor")
    return roots


def _icon_directory():
    """Where our icons live, for a tray that wants to look them up itself."""
    for root in _icon_roots():
        if root.is_dir():
            return root
    return None


def _tray_icon_files() -> list:
    """Icon files to hand to the tray as raw pixels.

    Two sizes, because panels differ. Naming the icon is not enough: the tray
    looks that name up in *its own* icon theme, which will not contain ours
    unless Kairos has been installed system-wide.
    """
    for root in _icon_roots():
        found = [root / f"{size}x{size}" / "apps" / f"{APP_ID}.png" for size in (24, 48)]
        if all(path.is_file() for path in found):
            return found
    return []


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

    # Asking the version should not need a display. Answering it here, rather
    # than inside the GApplication, keeps "kairos --version" quiet on a server
    # or in a build script, where starting GTK prints a page of complaints.
    if "--version" in argv or "-v" in argv:
        print(f"{APP_NAME} {VERSION}")
        return 0

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
