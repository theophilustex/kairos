"""The bubble that appears when an event is clicked.

Read-only, with Edit and Delete buttons.  Keeping the detail view separate
from the editor means a glance at an event costs one small popover rather
than a modal dialog.
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GObject, Gtk  # noqa: E402

from kairos import formatting
from kairos.ical import describe_rrule
from kairos.config import settings
from kairos.models import Occurrence
from kairos.security import find_url, is_meeting_url
from kairos.ui.widgets import colour_swatch


class EventPopover(Gtk.Popover):
    """Details of one occurrence.

    Emits ``edit-requested`` and ``delete-requested``; the window decides what
    those actually do.
    """

    __gsignals__ = {
        "edit-requested": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
        "delete-requested": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
    }

    def __init__(self, occurrence: Occurrence, calendar_name: str, calendar_colour: str,
                 *, editable: bool = True, calendar_alarm: object = None) -> None:
        super().__init__()
        self.occurrence = occurrence
        self.set_autohide(True)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_top(14)
        box.set_margin_bottom(14)
        box.set_margin_start(14)
        box.set_margin_end(14)
        box.set_size_request(300, -1)

        title = Gtk.Label(label=occurrence.summary, xalign=0)
        title.add_css_class("kairos-detail-title")
        title.set_wrap(True)
        title.set_max_width_chars(34)
        box.append(title)

        box.append(self._meta_row(
            "x-office-calendar-symbolic",
            f"{formatting.format_day_heading(occurrence.first_day)}\n"
            f"{formatting.format_time_range(occurrence)}",
        ))

        if occurrence.event.location:
            box.append(self._meta_row("mark-location-symbolic", occurrence.event.location))

        if occurrence.event.rrule:
            box.append(self._meta_row("media-playlist-repeat-symbolic",
                                      describe_rrule(occurrence.event.rrule)))

        if occurrence.event.alarms:
            reminders = ", ".join(alarm.label() for alarm in occurrence.event.alarms)
            box.append(self._meta_row("alarm-symbolic", reminders))
        elif calendar_alarm is not None:
            # Say where it came from. A reminder appearing on an event that
            # plainly has none would look like a bug, and knowing it is the
            # calendar's is what tells you where to go and change it.
            box.append(self._meta_row(
                "alarm-symbolic",
                f"{calendar_alarm.label()} · from this calendar"))

        if occurrence.event.description:
            separator = Gtk.Separator()
            box.append(separator)
            notes = Gtk.Label(label=occurrence.event.description, xalign=0)
            notes.set_wrap(True)
            notes.set_max_width_chars(40)
            notes.add_css_class("kairos-detail-meta")
            scroller = Gtk.ScrolledWindow()
            scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            scroller.set_max_content_height(140)
            scroller.set_propagate_natural_height(True)
            scroller.set_child(notes)
            box.append(scroller)

        # Which calendar it belongs to.
        calendar_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        calendar_row.append(colour_swatch(calendar_colour, size=10))
        name = Gtk.Label(label=calendar_name, xalign=0)
        name.add_css_class("dim-label")
        name.add_css_class("caption")
        calendar_row.append(name)
        if occurrence.event.dirty:
            badge = Gtk.Label(label="not yet synced")
            badge.add_css_class("kairos-offline-badge")
            calendar_row.append(badge)
        box.append(calendar_row)

        # Actions.
        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        buttons.set_halign(Gtk.Align.END)
        buttons.set_margin_top(4)

        # A link to join, if there is one. The URL property is checked first
        # because that is where it belongs; in practice most invitations put
        # it in the location or somewhere in the notes instead.
        self._link = find_url(occurrence.event.url,
                              occurrence.event.location,
                              occurrence.event.description)
        if self._link:
            join = Gtk.Button(label="Join" if is_meeting_url(self._link) else "Open link")
            join.add_css_class("suggested-action")
            join.connect("clicked", self._on_join)
            buttons.append(join)

        if editable:
            # Not merely disabled: a button that is always greyed out is
            # clutter, and the setting exists for people who never want to
            # see the option.
            if settings.get_bool("allow_deleting_events"):
                delete = Gtk.Button(label="Delete")
                delete.add_css_class("destructive-action")
                delete.connect("clicked", self._on_delete)
                buttons.append(delete)

            edit = Gtk.Button(label="Edit")
            # Only one primary action per bubble. When there is a meeting to
            # join, that is the one you came for, and two blue buttons side
            # by side say nothing about which.
            if not self._link:
                edit.add_css_class("suggested-action")
            edit.connect("clicked", self._on_edit)
            buttons.append(edit)
        else:
            note = Gtk.Label(label="This calendar is read-only")
            note.add_css_class("dim-label")
            note.add_css_class("caption")
            buttons.append(note)

        box.append(buttons)
        self.set_child(box)

    def _meta_row(self, icon_name: str, text: str) -> Gtk.Widget:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        icon = Gtk.Image.new_from_icon_name(icon_name)
        icon.set_valign(Gtk.Align.START)
        icon.add_css_class("dim-label")
        row.append(icon)
        label = Gtk.Label(label=text, xalign=0)
        label.set_wrap(True)
        label.set_max_width_chars(34)
        label.add_css_class("kairos-detail-meta")
        row.append(label)
        return row

    def _on_join(self, button) -> None:
        """Hand the link to the desktop's browser.

        ``find_url`` has already refused anything that is not http or https,
        so this cannot be talked into opening a local file or some other
        application's scheme by an event someone else put in the calendar.
        """
        self.popdown()
        Gtk.UriLauncher(uri=self._link).launch(
            button.get_root(), None, None, None)

    def _on_edit(self, _button) -> None:
        self.popdown()
        self.emit("edit-requested", self.occurrence)

    def _on_delete(self, _button) -> None:
        self.popdown()
        self.emit("delete-requested", self.occurrence)


#: The three answers to "which occurrences?", matching SyncManager's names.
THIS_EVENT = "this"
THIS_AND_FOLLOWING = "following"
ALL_EVENTS = "all"
SCOPES = (THIS_EVENT, THIS_AND_FOLLOWING, ALL_EVENTS)


def _scope_dialog(*, heading: str, summary: str, day, verb: str,
                  destructive: bool) -> Adw.AlertDialog:
    """The "this / this and following / all" question.

    Shared by editing and deleting so the wording and the button order cannot
    drift apart — with three choices and a destructive one among them, a user
    reading the same sentence twice is worth more than a little duplication.
    """
    dialog = Adw.AlertDialog(
        heading=heading,
        body=(f"“{summary}” repeats. {verb} only the occurrence on "
              f"{formatting.format_date(day)}, this one and the rest of the "
              f"series, or every occurrence?"))
    dialog.add_response("cancel", "Cancel")
    dialog.add_response(THIS_EVENT, "This event")
    dialog.add_response(THIS_AND_FOLLOWING, "This and following")
    dialog.add_response(ALL_EVENTS, "All events")
    if destructive:
        for response in SCOPES:
            dialog.set_response_appearance(response,
                                           Adw.ResponseAppearance.DESTRUCTIVE)
    dialog.set_default_response("cancel" if destructive else THIS_EVENT)
    dialog.set_close_response("cancel")
    return dialog


def confirm_delete(parent: Gtk.Widget, occurrence: Occurrence, on_confirm) -> None:
    """Ask before deleting, since there is no undo.

    ``on_confirm`` is called with ``scope=`` one of :data:`SCOPES`.  For an
    event that does not repeat there is nothing to choose and it is always
    ``ALL_EVENTS`` — deleting the one occurrence *is* deleting the event.
    """
    repeating = occurrence.recurrence_id is not None and occurrence.event.is_recurring
    if not repeating:
        dialog = Adw.AlertDialog(
            heading="Delete this event?",
            body=f"“{occurrence.summary}” will be removed from your calendar.")
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("delete", "Delete")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", lambda _d, response: (
            on_confirm(scope=ALL_EVENTS) if response == "delete" else None))
        dialog.present(parent)
        return

    dialog = _scope_dialog(
        heading="Delete a repeating event",
        summary=occurrence.summary,
        day=occurrence.start.date(),
        verb="Delete",
        destructive=True,
    )
    dialog.connect("response", lambda _d, response: (
        on_confirm(scope=response) if response in SCOPES else None))
    dialog.present(parent)


def ask_edit_scope(parent: Gtk.Widget, occurrence: Occurrence, on_choice,
                   *, on_cancel=None) -> None:
    """Ask whether an edit applies to one occurrence or the whole series.

    Calls ``on_choice(scope=...)`` with one of :data:`SCOPES`, and does not
    ask at all when the event does not repeat — there is only one answer.
    """
    repeating = occurrence.recurrence_id is not None and occurrence.event.is_recurring
    if not repeating:
        on_choice(scope=ALL_EVENTS)
        return

    dialog = _scope_dialog(
        heading="Edit a repeating event",
        summary=occurrence.summary,
        day=occurrence.start.date(),
        verb="Change",
        destructive=False,
    )

    def on_response(_dialog, response: str) -> None:
        if response in SCOPES:
            on_choice(scope=response)
        elif on_cancel is not None:
            # A drag that is cancelled has already moved the block on screen;
            # the caller uses this to put it back.
            on_cancel()

    dialog.connect("response", on_response)
    dialog.present(parent)
