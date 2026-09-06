"""The window that puts a due reminder in front of you.

A desktop notification is easy to miss: it slides away after a few seconds and
lives on in a tray nobody opens.  When a reminder actually matters, Kairos
also raises this window, which asks the window manager for focus and stays put
until you dismiss or snooze it.

**On stealing focus.**  Every modern desktop has focus-stealing prevention,
and it is right to.  There is no way to override it that works everywhere, so
:func:`demand_attention` tries the polite routes in order and accepts that on
some setups the window will open behind and merely flash in the taskbar:

1. ``present_with_time`` with the current server time, which is the supported
   way to say "the user asked for this now";
2. on X11, the ``urgency hint``, which makes the taskbar entry demand
   attention when the window manager declines to raise it;
3. the desktop notification is still sent, so the reminder is never *only* in
   a window that failed to appear.

The window shows every reminder that is currently due, one row each, rather
than opening a window per event — three overlapping meetings should not bury
your screen in three windows.
"""

from __future__ import annotations

import logging

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, GObject, Gtk  # noqa: E402

from kairos.ui.widgets import colour_swatch

log = logging.getLogger(__name__)

#: What the Snooze button offers, as (minutes, label).
SNOOZE_CHOICES = (
    (5, "5 minutes"),
    (10, "10 minutes"),
    (15, "15 minutes"),
    (30, "30 minutes"),
    (60, "1 hour"),
    (180, "3 hours"),
    (1440, "Tomorrow"),
)

#: How often the "in 12 minutes" line is recomputed while the window is open.
COUNTDOWN_REFRESH_SECONDS = 30


def demand_attention(window: Gtk.Window) -> None:
    """Raise ``window`` and, failing that, make it demand attention.

    See the module docstring: this is best-effort by nature. Nothing here
    raises if the compositor declines.
    """
    try:
        window.present_with_time(Gdk.CURRENT_TIME)
    except Exception:
        window.present()

    surface = window.get_surface()
    if surface is None:
        return

    # X11 has an explicit "this window is urgent" hint, which window managers
    # honour by flashing the taskbar entry even when they refuse to raise it.
    # There is no Wayland equivalent; there, present_with_time is all we get.
    try:
        from gi.repository import GdkX11
        if isinstance(surface, GdkX11.X11Surface):
            surface.set_urgency_hint(True)
    except Exception as exc:
        log.debug("could not set the urgency hint: %s", exc)


class ReminderAlertWindow(Adw.ApplicationWindow):
    """A standalone window listing the reminders that are due right now.

    Deliberately *not* transient for the main window: the main window may be
    minimised or closed, and the alert still has to appear.

    Emits ``snoozed(reminder, minutes)`` and ``dismissed(reminder)``; the
    application decides what those mean.  Closing the window dismisses
    everything still in it.
    """

    __gsignals__ = {
        "snoozed": (GObject.SignalFlags.RUN_FIRST, None, (object, int)),
        "dismissed": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "opened": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
    }

    def __init__(self, application: Adw.Application) -> None:
        super().__init__(application=application, title="Reminder")
        # Height is left to the content: one reminder gets a compact window,
        # several get a taller one, and beyond a screenful the list scrolls.
        self.set_default_size(440, -1)
        self.set_resizable(True)
        self.set_hide_on_close(False)

        #: reminder key -> the row showing it.
        self._rows: dict[str, Gtk.Widget] = {}
        self._countdown_timer = 0
        self._present_queued = False

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_title_widget(Gtk.Label(label="Reminder"))

        self._dismiss_all = Gtk.Button(label="Dismiss all")
        self._dismiss_all.add_css_class("flat")
        self._dismiss_all.set_visible(False)
        self._dismiss_all.connect("clicked", lambda *_: self.dismiss_all())
        header.pack_end(self._dismiss_all)

        toolbar.add_top_bar(header)

        self._list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_vexpand(True)
        # Ask for the height the reminders actually need, up to a limit; past
        # that the list scrolls rather than the window filling the screen.
        scroller.set_propagate_natural_height(True)
        scroller.set_max_content_height(560)
        scroller.set_child(self._list)
        toolbar.set_content(scroller)

        self.set_content(toolbar)
        self.connect("close-request", self._on_close_request)

    # ------------------------------------------------------------------
    # Showing reminders
    # ------------------------------------------------------------------

    def show_reminder(self, reminder) -> None:
        """Add a reminder to the window and bring the window forward.

        A reminder already listed is not added twice; the window is simply
        raised again, which is what happens if it was buried.
        """
        if reminder.key not in self._rows:
            row = self._build_row(reminder)
            self._rows[reminder.key] = row
            self._list.append(row)

        self._update_chrome()
        self._start_countdown()

        # Reminders that fall due together arrive in one burst. Presenting on
        # an idle callback lets the whole burst land first, so the window maps
        # once, at the size its contents actually need, instead of opening on
        # the first reminder and clipping the rest.
        if not self._present_queued:
            self._present_queued = True
            GLib.idle_add(self._present_now)

    def _present_now(self) -> bool:
        self._present_queued = False
        demand_attention(self)
        return GLib.SOURCE_REMOVE

    def _update_chrome(self) -> None:
        count = len(self._rows)
        self._dismiss_all.set_visible(count > 1)
        self.set_title("Reminder" if count == 1 else f"{count} reminders")

    # ------------------------------------------------------------------
    # One reminder
    # ------------------------------------------------------------------

    def _build_row(self, reminder) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_margin_top(16)
        box.set_margin_bottom(16)
        box.set_margin_start(18)
        box.set_margin_end(18)

        title = Gtk.Label(label=reminder.summary or "(No title)", xalign=0)
        title.add_css_class("kairos-alert-title")
        title.set_wrap(True)
        title.set_max_width_chars(36)
        box.append(title)

        countdown = Gtk.Label(label=reminder.countdown_text(), xalign=0)
        countdown.add_css_class("kairos-alert-countdown")
        box.append(countdown)
        box.countdown_label = countdown        # type: ignore[attr-defined]
        box.reminder = reminder                # type: ignore[attr-defined]

        when = Gtk.Label(label=reminder.when_text(), xalign=0)
        when.add_css_class("kairos-detail-meta")
        when.set_wrap(True)
        box.append(when)

        if reminder.location:
            place = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            icon = Gtk.Image.new_from_icon_name("mark-location-symbolic")
            icon.add_css_class("dim-label")
            place.append(icon)
            label = Gtk.Label(label=reminder.location, xalign=0)
            label.set_wrap(True)
            label.add_css_class("kairos-detail-meta")
            place.append(label)
            box.append(place)

        if reminder.calendar_name:
            calendar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            calendar.append(colour_swatch(reminder.calendar_colour, size=10))
            name = Gtk.Label(label=reminder.calendar_name, xalign=0)
            name.add_css_class("dim-label")
            name.add_css_class("caption")
            calendar.append(name)
            box.append(calendar)

        box.append(self._build_buttons(reminder))

        separator = Gtk.Separator()
        wrapper = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        wrapper.append(box)
        wrapper.append(separator)
        wrapper.row_box = box                  # type: ignore[attr-defined]
        return wrapper

    def _build_buttons(self, reminder) -> Gtk.Widget:
        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        buttons.set_halign(Gtk.Align.END)
        buttons.set_margin_top(8)

        show = Gtk.Button(label="Show in calendar")
        show.add_css_class("flat")
        show.connect("clicked", lambda *_: self._on_open(reminder))
        buttons.append(show)

        snooze = Gtk.MenuButton(label="Snooze")
        snooze.set_tooltip_text("Remind me again later")
        menu = Gtk.Popover()
        options = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        options.set_margin_top(6)
        options.set_margin_bottom(6)
        options.set_margin_start(6)
        options.set_margin_end(6)
        for minutes, label in SNOOZE_CHOICES:
            option = Gtk.Button(label=label)
            option.add_css_class("flat")
            option.connect(
                "clicked",
                lambda _b, m=minutes: (menu.popdown(), self._on_snooze(reminder, m)),
            )
            options.append(option)
        menu.set_child(options)
        snooze.set_popover(menu)
        buttons.append(snooze)

        dismiss = Gtk.Button(label="Dismiss")
        dismiss.add_css_class("suggested-action")
        dismiss.connect("clicked", lambda *_: self._on_dismiss(reminder))
        buttons.append(dismiss)

        return buttons

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _remove(self, reminder) -> None:
        row = self._rows.pop(reminder.key, None)
        if row is not None:
            self._list.remove(row)
        self._update_chrome()
        if not self._rows:
            self._stop_countdown()
            self.close()

    def _on_dismiss(self, reminder) -> None:
        self.emit("dismissed", reminder)
        self._remove(reminder)

    def _on_snooze(self, reminder, minutes: int) -> None:
        self.emit("snoozed", reminder, minutes)
        self._remove(reminder)

    def _on_open(self, reminder) -> None:
        self.emit("opened", reminder)
        self._remove(reminder)

    def dismiss_all(self) -> None:
        for reminder in [row.row_box.reminder for row in self._rows.values()]:
            self.emit("dismissed", reminder)
        self._rows.clear()
        self._stop_countdown()
        self.close()

    def _on_close_request(self, _window) -> bool:
        """Closing the window means "I have seen these"."""
        for row in list(self._rows.values()):
            self.emit("dismissed", row.row_box.reminder)
        self._rows.clear()
        self._stop_countdown()
        return False        # let the close proceed

    # ------------------------------------------------------------------
    # "in 12 minutes" has to keep up
    # ------------------------------------------------------------------

    def _start_countdown(self) -> None:
        if self._countdown_timer:
            return
        self._countdown_timer = GLib.timeout_add_seconds(
            COUNTDOWN_REFRESH_SECONDS, self._tick
        )

    def _stop_countdown(self) -> None:
        if self._countdown_timer:
            GLib.source_remove(self._countdown_timer)
            self._countdown_timer = 0

    def _tick(self) -> bool:
        for row in self._rows.values():
            box = row.row_box
            box.countdown_label.set_label(box.reminder.countdown_text())
        return GLib.SOURCE_CONTINUE if self._rows else GLib.SOURCE_REMOVE
