"""The preferences dialog.

Every row here maps to exactly one key in :data:`kairos.config.settings`, and
writes it the moment it changes.  Adding a preference means adding it to
``DEFAULTS`` in :mod:`kairos.config` and adding one row here; there is no
schema to compile and no glue in between.

The Appearance page also has a button that opens ``custom.css`` in the user's
editor, because that file is where the real customisation happens.
"""

from __future__ import annotations

from datetime import datetime

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from kairos import autostart
from kairos.config import CONFIG_DIR, CUSTOM_CSS_FILE, settings
from kairos.security import credentials
from kairos.theming import parse_colour


class PreferencesDialog(Adw.PreferencesDialog):
    """Preferences, in four pages."""

    def __init__(self, sync_manager, theme_manager) -> None:
        super().__init__()
        self.sync = sync_manager
        self.theme = theme_manager
        self.set_title("Preferences")

        self.add(self._general_page())
        self.add(self._appearance_page())
        self.add(self._notifications_page())
        self.add(self._sync_page())

    # ------------------------------------------------------------------
    # Row helpers — each one binds a widget to a settings key
    # ------------------------------------------------------------------

    def _switch(self, title: str, key: str, subtitle: str = "") -> Adw.SwitchRow:
        row = Adw.SwitchRow(title=title, subtitle=subtitle)
        row.set_active(settings.get_bool(key))
        row.connect("notify::active", lambda r, _p: settings.set(key, r.get_active()))
        return row

    def _choice(self, title: str, key: str, options: list[tuple[str, str]],
                subtitle: str = "") -> Adw.ComboRow:
        """``options`` is a list of ``(stored value, label)`` pairs."""
        row = Adw.ComboRow(title=title, subtitle=subtitle)
        model = Gtk.StringList()
        for _value, label in options:
            model.append(label)
        row.set_model(model)

        values = [value for value, _ in options]
        current = settings.get(key)
        row.set_selected(values.index(current) if current in values else 0)
        row.connect(
            "notify::selected",
            lambda r, _p: settings.set(key, values[r.get_selected()]),
        )
        return row

    def _number(self, title: str, key: str, low: float, high: float, step: float = 1,
                subtitle: str = "") -> Adw.SpinRow:
        row = Adw.SpinRow.new_with_range(low, high, step)
        row.set_title(title)
        if subtitle:
            row.set_subtitle(subtitle)
        row.set_value(float(settings.get(key)))
        row.connect("notify::value", lambda r, _p: settings.set(key, r.get_value()))
        return row

    # ------------------------------------------------------------------
    # Pages
    # ------------------------------------------------------------------

    def _general_page(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage(title="General", icon_name="preferences-system-symbolic")
        # First on the page: it decides whether reminders arrive at all, and
        # at the bottom of a long page it was easy to miss entirely.
        page.add(self._background_group())

        layout = Adw.PreferencesGroup(title="Calendar")
        layout.add(self._choice("Default view", "default_view", [
            ("month", "Month"), ("week", "Week"), ("day", "Day"), ("agenda", "Agenda"),
        ]))
        layout.add(self._choice("Week starts on", "first_day_of_week", [
            ("monday", "Monday"), ("sunday", "Sunday"), ("saturday", "Saturday"),
        ]))
        layout.add(self._choice("Time format", "time_format", [
            ("24h", "24-hour"), ("12h", "12-hour (AM/PM)"),
        ]))
        layout.add(self._switch("Show week numbers", "show_week_numbers"))
        layout.add(self._switch("Shade weekends", "highlight_weekends"))
        page.add(layout)

        events = Adw.PreferencesGroup(title="New events")
        events.add(self._number(
            "Default length", "default_event_duration_minutes", 5, 480, 5,
            "Minutes, for an event created by double-clicking.",
        ))
        events.add(self._number(
            "Default reminder", "default_alarm_minutes", 0, 1440, 5,
            "Minutes before the event starts.",
        ))
        page.add(events)

        grid = Adw.PreferencesGroup(title="Grid")
        grid.add(self._number(
            "Events shown per day", "max_chips_per_day", 1, 12, 1,
            "In the month view, before “+N more”.",
        ))
        grid.add(self._number(
            "Hour height", "hour_height", 24, 160, 4,
            "Pixels per hour in the week and day views.",
        ))
        grid.add(self._number(
            "Days in the agenda", "agenda_days", 1, 365, 1,
        ))
        grid.add(self._switch(
            "Scroll to the current time", "week_starts_scrolled_to_now",
            "Open the week view near now rather than at midnight.",
        ))
        page.add(grid)

        return page

    def _background_group(self) -> Adw.PreferencesGroup:
        """Whether Kairos stays running, and whether it starts itself.

        Both matter for reminders: a calendar that is not running cannot
        remind you of anything.
        """
        group = Adw.PreferencesGroup(
            title="Startup and background",
            description=(
                "Kairos has to be running to remind you about anything. Left "
                "in the background it costs a few megabytes and one timer."
            ),
        )
        group.add(self._switch(
            "Keep running when the window is closed", "run_in_background",
            "Closing the window leaves Kairos in the taskbar. Use its icon, or "
            "open Kairos again, to bring the window back.",
        ))

        login = Adw.SwitchRow(
            title="Start in the background when you log in",
            subtitle=("Kairos starts with your desktop, quietly in the "
                      "taskbar, so reminders arrive without opening the window."),
        )
        login.set_active(autostart.is_enabled())
        if autostart.is_stale():
            login.set_subtitle(
                "On, but it points at a copy of Kairos that is no longer "
                "there. Switch it off and on again to point it at this one.")
        login.connect("notify::active", self._on_autostart_changed)
        group.add(login)
        self._autostart_row = login

        return group

    def _on_autostart_changed(self, row: Adw.SwitchRow, _param) -> None:
        """Write or delete the autostart file, and own up if that fails."""
        wanted = row.get_active()
        if autostart.set_enabled(wanted):
            return

        row.set_active(not wanted)          # put the switch back
        row.set_subtitle(
            f"Could not write {autostart.desktop_file()}. Check the "
            "permissions on that directory."
        )

    def _appearance_page(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage(title="Appearance", icon_name="applications-graphics-symbolic")

        theme = Adw.PreferencesGroup(title="Theme")
        theme.add(self._choice("Colour scheme", "theme", [
            ("system", "Follow the system"), ("light", "Light"), ("dark", "Dark"),
        ]))

        accent = Adw.ActionRow(
            title="Accent colour",
            subtitle="Used for today, selections and the current-time line.",
        )
        colour_button = Gtk.ColorDialogButton()
        colour_button.set_dialog(Gtk.ColorDialog())
        colour_button.set_rgba(parse_colour(settings.get("accent_color")))
        colour_button.set_valign(Gtk.Align.CENTER)
        colour_button.connect("notify::rgba", self._on_accent_changed)
        accent.add_suffix(colour_button)
        theme.add(accent)

        theme.add(self._number(
            "Text size", "font_scale", 0.75, 1.5, 0.05,
            "A multiplier applied to the whole window.",
        ))
        theme.add(self._switch(
            "Compact spacing", "compact_mode",
            "Tighter padding, so more fits on screen.",
        ))
        theme.add(self._switch("Rounded event chips", "rounded_event_chips"))
        theme.add(self._switch(
            "Fade events that have ended", "fade_past_events",
            "So what is still to come stands out from what is already over.",
        ))
        page.add(theme)

        custom = Adw.PreferencesGroup(
            title="Your own stylesheet",
            description=(
                "Kairos loads ~/.config/kairos/custom.css last, so anything you "
                "put there overrides the built-in look. It is reloaded as soon "
                "as you save it — no restart needed. The file lists the class "
                "names you can target."
            ),
        )

        open_css = Adw.ActionRow(title="Edit custom.css", subtitle=str(CUSTOM_CSS_FILE))
        open_button = Gtk.Button(label="Open")
        open_button.set_valign(Gtk.Align.CENTER)
        open_button.connect("clicked", lambda *_: _open_path(CUSTOM_CSS_FILE))
        open_css.add_suffix(open_button)
        open_css.set_activatable_widget(open_button)
        custom.add(open_css)

        reload_row = Adw.ActionRow(
            title="Reload stylesheets now",
            subtitle="Only needed if the automatic reload did not fire.",
        )
        reload_button = Gtk.Button(label="Reload")
        reload_button.set_valign(Gtk.Align.CENTER)
        reload_button.connect("clicked", lambda *_: self.theme.apply())
        reload_row.add_suffix(reload_button)
        custom.add(reload_row)

        config_row = Adw.ActionRow(
            title="Open the configuration folder",
            subtitle=str(CONFIG_DIR),
        )
        config_button = Gtk.Button(label="Open")
        config_button.set_valign(Gtk.Align.CENTER)
        config_button.connect("clicked", lambda *_: _open_path(CONFIG_DIR))
        config_row.add_suffix(config_button)
        custom.add(config_row)

        page.add(custom)
        return page

    def _on_accent_changed(self, button: Gtk.ColorDialogButton, _param) -> None:
        rgba: Gdk.RGBA = button.get_rgba()
        settings.set("accent_color", "#{:02x}{:02x}{:02x}".format(
            round(rgba.red * 255), round(rgba.green * 255), round(rgba.blue * 255)
        ))

    def _notifications_page(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage(title="Reminders", icon_name="alarm-symbolic")

        group = Adw.PreferencesGroup(
            title="Desktop notifications",
            description=(
                "Each event carries its own reminders. Kairos shows a "
                "notification at every one of them."
            ),
        )
        group.add(self._switch(
            "Show reminders", "notifications_enabled",
            "Turn this off to silence every event's reminders at once.",
        ))
        group.add(self._number(
            "Plan ahead", "notification_lookahead_minutes", 5, 1440, 5,
            "Minutes of reminders scheduled at a time. Larger uses very "
            "slightly more memory and wakes up less often.",
        ))
        page.add(group)

        alert = Adw.PreferencesGroup(
            title="Alert window",
            description=(
                "A notification slides away after a few seconds. The alert "
                "window stays until you answer it, and asks your desktop to "
                "bring it to the front even when Kairos is in the background."
            ),
        )
        alert.add(self._switch(
            "Show an alert window", "reminder_alert_window",
            "Turn this off to be reminded by notifications alone.",
        ))
        alert.add(self._number(
            "Default snooze", "reminder_snooze_minutes", 1, 1440, 5,
            "Minutes. The Snooze button also offers other lengths.",
        ))
        page.add(alert)

        test = Adw.PreferencesGroup(title="Check it works")
        row = Adw.ActionRow(
            title="Send a test notification",
            subtitle="Confirms your desktop is showing Kairos's notifications.",
        )
        button = Gtk.Button(label="Send")
        button.set_valign(Gtk.Align.CENTER)
        button.connect("clicked", lambda *_: self._send_test())
        row.add_suffix(button)
        test.add(row)
        page.add(test)

        return page

    def _send_test(self) -> None:
        """Fire a reminder for a pretend event, by every route in use.

        Worth having: whether a notification actually reaches the desktop, and
        whether the alert window is allowed to steal focus, both depend on the
        desktop rather than on Kairos. This is how you find out.
        """
        from datetime import timedelta

        from kairos.models import local_timezone
        from kairos.notifications import PendingReminder

        application = Gio.Application.get_default()
        if application is None:
            return

        now = datetime.now(tz=local_timezone())
        reminder = PendingReminder(
            key=f"kairos-test-{int(now.timestamp())}",
            uid="kairos-test",
            summary="Kairos reminders are working",
            start=now + timedelta(minutes=10),
            end=now + timedelta(minutes=40),
            location="This is a test reminder",
            calendar_name="Kairos",
            minutes_before=10,
            fire_at=now,
        )

        notification = Gio.Notification.new(reminder.summary)
        notification.set_body("This is what an event reminder will look like.")
        application.send_notification("kairos-test", notification)

        if settings.get_bool("reminder_alert_window") and hasattr(application, "present_reminder"):
            application.present_reminder(reminder)

    def _sync_page(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage(title="Sync & security", icon_name="network-transmit-receive-symbolic")

        sync = Adw.PreferencesGroup(title="Syncing")
        interval = self._number(
            "Sync every", "sync_interval_minutes", 0, 1440, 5,
            "Minutes. Set to 0 to sync only when you ask.",
        )
        interval.connect("notify::value", lambda *_: self.sync.reschedule())
        sync.add(interval)
        sync.add(self._switch("Sync when Kairos starts", "sync_on_startup"))
        sync.add(self._number(
            "Keep this much history", "sync_window_past_days", 0, 3650, 30,
            "Days of past events kept in the offline cache.",
        ))
        sync.add(self._number(
            "Look this far ahead", "sync_window_future_days", 1, 3650, 30,
            "Days of future events downloaded.",
        ))
        sync.add(self._number(
            "Network timeout", "network_timeout_seconds", 5, 300, 5,
            "Seconds to wait for a server before giving up.",
        ))
        page.add(sync)

        safety = Adw.PreferencesGroup(
            title="Protecting your calendar",
            description=(
                "Kairos writes to the same calendars your other devices read. "
                "This decides how much it is allowed to take away."
            ),
        )
        safety.add(self._switch(
            "Allow deleting events", "allow_deleting_events",
            "Turn this off and Kairos will never remove an event — the Delete "
            "button disappears, and any deletion still waiting to be sent is "
            "left unsent. You can still create and edit.",
        ))
        page.add(safety)

        security = Adw.PreferencesGroup(
            title="Security",
            description=(
                "These are here for self-hosted servers. Both defaults are the "
                "safe ones, and there is rarely a good reason to change them."
            ),
        )

        verify = self._switch(
            "Verify security certificates", "verify_tls_certificates",
            "Turning this off lets anyone on your network read and alter your "
            "calendar data. Leave it on.",
        )
        security.add(verify)

        insecure = self._switch(
            "Allow unencrypted http", "allow_insecure_http",
            "Even then, Kairos will only use http for servers on your own "
            "local network.",
        )
        security.add(insecure)

        ca_row = Adw.EntryRow(title="Certificate authority file")
        ca_row.set_text(settings.get("ca_certificate_path"))
        ca_row.connect("changed", lambda row: settings.set(
            "ca_certificate_path", row.get_text().strip()))
        security.add(ca_row)

        ca_hint = Adw.ActionRow(
            subtitle=(
                "A PEM file for the authority that signed your server's "
                "certificate. For a self-hosted box this is the right answer "
                "to “the certificate could not be verified” — it keeps the "
                "connection checked, rather than turning the check off."
            ),
        )
        ca_hint.set_subtitle_lines(0)
        security.add(ca_hint)

        keyring_row = Adw.ActionRow(title="Password storage")
        if credentials.available:
            keyring_row.set_subtitle("Passwords are stored in your system keyring.")
            keyring_row.add_prefix(Gtk.Image.new_from_icon_name("security-high-symbolic"))
        else:
            keyring_row.set_subtitle(
                "No keyring found, so passwords are kept in memory only and "
                "must be re-entered each session. Installing gnome-keyring or "
                "kwalletmanager fixes this."
            )
            keyring_row.add_prefix(Gtk.Image.new_from_icon_name("dialog-warning-symbolic"))
        security.add(keyring_row)

        page.add(security)
        return page


def _open_path(path) -> None:
    """Hand a file or folder to the desktop's default handler."""
    try:
        launcher = Gtk.FileLauncher.new(Gio.File.new_for_path(str(path)))
        launcher.launch(None, None, None, None)
    except (AttributeError, GLib.Error):
        # GTK < 4.10, or no portal: fall back to the URI opener.
        Gio.AppInfo.launch_default_for_uri(Gio.File.new_for_path(str(path)).get_uri(), None)
