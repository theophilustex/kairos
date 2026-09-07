"""Creating and editing an event.

An :class:`Adw.Dialog` full of preference rows.  It owns no state beyond the
widgets: :meth:`EventEditor.build_event` reads the form and hands back an
:class:`~kairos.models.Event`, and the window is what actually saves it.

Reminders are the interesting part.  An event can have any number, each an
offset before the start, and they are what :mod:`kairos.notifications` later
turns into desktop notifications.  The drop-down lists the common offsets, but
"Custom…" takes any number of minutes, hours, days or weeks — and an offset
that arrived from a server is shown in the list too, in words, whether or not
Kairos would have offered it.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GObject, Gtk  # noqa: E402

from kairos.config import settings
from kairos.ical import REPEAT_PRESETS
from kairos.models import (
    Alarm, Calendar, Event, describe_offset, local_timezone, to_local,
)
from kairos.security import find_url
from kairos.ui.widgets import DateTimeRow, colour_swatch, describe


class EventEditor(Adw.Dialog):
    """The new/edit event dialog.

    Emits ``saved(Event)`` when the user confirms.  Construct it with an
    existing event to edit, or with ``event=None`` and a start time to create.
    """

    __gsignals__ = {
        "saved": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
    }

    #: Sentinel put in a reminder drop-down for the "Custom…" entry. An object
    #: rather than a number, so it can never collide with a real offset.
    CUSTOM = object()

    #: What "Custom…" offers, as (minutes per unit, plural name).
    CUSTOM_UNITS = ((1, "minutes"), (60, "hours"), (1440, "days"), (10080, "weeks"))

    #: A year is as far ahead as a reminder can sensibly be set.
    MAX_CUSTOM_MINUTES = 366 * 24 * 60

    def __init__(self, calendars: list[Calendar], *, event: Event | None = None,
                 start: datetime | None = None) -> None:
        super().__init__()
        self.set_title("Edit event" if event else "New event")
        self.set_content_width(480)
        self.set_content_height(640)

        self._calendars = [c for c in calendars if c.writable] or calendars
        self._original = event
        self._alarm_rows: list[Adw.ActionRow] = []
        self._alarm_minutes: list[int] = []

        # An event from a server keeps whatever zone the server used, usually
        # UTC. The editor must show the times the user actually sees on their
        # own clock, so convert on the way in; build_event writes local times
        # back out, which denote the same instant.
        start = start or (to_local(event.start) if event else self._default_start())
        end = to_local(event.end) if event else start + timedelta(
            minutes=settings.get_int("default_event_duration_minutes")
        )

        self._build(event, start, end)

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @staticmethod
    def _default_start() -> datetime:
        """The next round half-hour, which is what people usually want."""
        now = datetime.now(tz=local_timezone())
        minute = 30 if now.minute < 30 else 0
        hour = now.hour if now.minute < 30 else now.hour + 1
        return now.replace(hour=hour % 24, minute=minute, second=0, microsecond=0) + (
            timedelta(days=1) if hour >= 24 else timedelta(0)
        )

    def _build(self, event: Event | None, start: datetime, end: datetime) -> None:
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_show_end_title_buttons(False)
        header.set_show_start_title_buttons(False)

        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        header.pack_start(cancel)

        self._save_button = Gtk.Button(label="Save")
        self._save_button.add_css_class("suggested-action")
        self._save_button.connect("clicked", self._on_save)
        header.pack_end(self._save_button)

        toolbar.add_top_bar(header)

        page = Adw.PreferencesPage()
        page.add(self._details_group(event))
        page.add(self._when_group(event, start, end))
        page.add(self._reminders_group(event))
        page.add(self._notes_group(event))

        toolbar.set_content(page)
        self.set_child(toolbar)

    # -- what ------------------------------------------------------------

    def _details_group(self, event: Event | None) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup()

        self._title_row = Adw.EntryRow(title="Title")
        self._title_row.set_text(event.summary if event else "")
        # Typing a title and pressing Enter is the fastest way to add an event.
        self._title_row.connect("entry-activated", lambda *_: self._on_save(None))
        group.add(self._title_row)

        self._location_row = Adw.EntryRow(title="Location")
        self._location_row.set_text(event.location if event else "")
        group.add(self._location_row)

        self._url_row = Adw.EntryRow(title="Link")
        self._url_row.set_text(event.url if event else "")
        group.add(self._url_row)

        self._calendar_row = Adw.ComboRow(title="Calendar")
        names = Gtk.StringList()
        for calendar in self._calendars:
            names.append(calendar.name)
        self._calendar_row.set_model(names)
        if event is not None:
            for index, calendar in enumerate(self._calendars):
                if calendar.id == event.calendar_id:
                    self._calendar_row.set_selected(index)
                    break
        self._calendar_row.add_prefix(self._calendar_swatch())
        self._calendar_row.connect("notify::selected", self._on_calendar_changed)
        group.add(self._calendar_row)

        return group

    def _calendar_swatch(self) -> Gtk.Widget:
        self._swatch_holder = Gtk.Box()
        self._swatch_holder.set_valign(Gtk.Align.CENTER)
        self._refresh_swatch()
        return self._swatch_holder

    def _refresh_swatch(self) -> None:
        child = self._swatch_holder.get_first_child()
        if child is not None:
            self._swatch_holder.remove(child)
        calendar = self._selected_calendar()
        if calendar is not None:
            self._swatch_holder.append(colour_swatch(calendar.colour))

    def _on_calendar_changed(self, *_args) -> None:
        self._refresh_swatch()

    def _selected_calendar(self) -> Calendar | None:
        index = self._calendar_row.get_selected() if hasattr(self, "_calendar_row") else 0
        if 0 <= index < len(self._calendars):
            return self._calendars[index]
        return self._calendars[0] if self._calendars else None

    # -- when ------------------------------------------------------------

    def _when_group(self, event: Event | None, start: datetime, end: datetime) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(title="When")

        self._all_day_row = Adw.SwitchRow(title="All day")
        self._all_day_row.set_active(bool(event and event.all_day))
        self._all_day_row.connect("notify::active", self._on_all_day_toggled)
        group.add(self._all_day_row)

        # An all-day event's stored end is exclusive; the editor shows the
        # inclusive last day, because that is what people mean.
        display_end = end - timedelta(days=1) if (event and event.all_day) else end

        self._start_row = DateTimeRow("Starts", start)
        self._start_row.connect("changed", self._on_start_changed)
        group.add(self._start_row)

        self._end_row = DateTimeRow("Ends", display_end)
        group.add(self._end_row)

        self._repeat_row = Adw.ComboRow(title="Repeats")
        labels = Gtk.StringList()
        for _rule, label in REPEAT_PRESETS:
            labels.append(label)
        self._custom_rrule = ""
        if event is not None and event.rrule:
            known = [rule for rule, _ in REPEAT_PRESETS]
            if event.rrule.upper() not in [k.upper() for k in known]:
                self._custom_rrule = event.rrule
                from kairos.ical import describe_rrule
                labels.append(f"{describe_rrule(event.rrule)} (from the server)")
        self._repeat_row.set_model(labels)
        if event is not None and event.rrule:
            self._repeat_row.set_selected(self._repeat_index(event.rrule))
        group.add(self._repeat_row)

        self._on_all_day_toggled()
        return group

    def _repeat_index(self, rrule: str) -> int:
        for index, (rule, _label) in enumerate(REPEAT_PRESETS):
            if rule.upper() == rrule.upper():
                return index
        return len(REPEAT_PRESETS)  # the appended "from the server" entry

    def _on_all_day_toggled(self, *_args) -> None:
        timed = not self._all_day_row.get_active()
        self._start_row.set_time_visible(timed)
        self._end_row.set_time_visible(timed)

    def _on_start_changed(self, *_args) -> None:
        """Keep the end after the start, the way every calendar app does."""
        start = self._start_row.get_value()
        end = self._end_row.get_value()
        if end <= start:
            duration = timedelta(minutes=settings.get_int("default_event_duration_minutes"))
            self._end_row.set_value(start + (timedelta(0) if self._all_day_row.get_active() else duration))

    # -- reminders --------------------------------------------------------

    def _reminders_group(self, event: Event | None) -> Adw.PreferencesGroup:
        self._reminders = Adw.PreferencesGroup(title="Reminders")
        self._reminders.set_description(
            "Kairos shows a desktop notification at each of these times."
        )

        add_button = Gtk.Button(label="Add")
        add_button.add_css_class("flat")
        add_button.connect("clicked", lambda *_: self._add_alarm(
            settings.get_int("default_alarm_minutes")
        ))
        self._reminders.set_header_suffix(add_button)

        existing = event.alarms if event else [Alarm(settings.get_int("default_alarm_minutes"))]
        for alarm in existing:
            self._add_alarm(alarm.minutes_before)

        self._empty_reminder_row = Adw.ActionRow(
            title="No reminders",
            subtitle="This event will not notify you.",
        )
        self._reminders.add(self._empty_reminder_row)
        self._update_reminder_placeholder()
        return self._reminders

    def _add_alarm(self, minutes: int) -> None:
        """Add one reminder row, preselected to ``minutes`` before the start."""
        row = Adw.ComboRow(title="Remind me")
        row.values = []          # type: ignore[attr-defined]
        row.updating = False     # type: ignore[attr-defined]
        row.connect("notify::selected", self._on_alarm_choice_changed)
        self._fill_alarm_row(row, minutes)

        remove = Gtk.Button(icon_name="user-trash-symbolic")
        remove.add_css_class("flat")
        remove.set_valign(Gtk.Align.CENTER)
        describe(remove, "Remove this reminder")
        remove.connect("clicked", lambda *_: self._remove_alarm(row))
        row.add_suffix(remove)

        self._reminders.add(row)
        self._alarm_rows.append(row)
        self._update_reminder_placeholder()

    def _fill_alarm_row(self, row: Adw.ComboRow, minutes: int) -> None:
        """(Re)build one reminder drop-down so ``minutes`` is in it and chosen.

        The list is the presets, plus this event's own offset when it is not
        one of them — an event from a server may carry any offset at all, and
        so may one the user typed — plus a "Custom…" entry at the end.
        """
        values = [value for value, _ in Alarm.PRESETS]
        labels = [label for _, label in Alarm.PRESETS]

        if minutes not in values:
            position = len([v for v in values if v < minutes])
            values.insert(position, minutes)
            labels.insert(position, describe_offset(minutes))

        values.append(self.CUSTOM)
        labels.append("Custom…")

        options = Gtk.StringList()
        for label in labels:
            options.append(label)

        row.updating = True      # type: ignore[attr-defined]
        try:
            row.set_model(options)
            row.set_selected(values.index(minutes))
            row.values = values  # type: ignore[attr-defined]
        finally:
            row.updating = False  # type: ignore[attr-defined]

    def _on_alarm_choice_changed(self, row: Adw.ComboRow, _param) -> None:
        if getattr(row, "updating", False):
            return
        values = getattr(row, "values", [])
        index = row.get_selected()
        if 0 <= index < len(values) and values[index] is self.CUSTOM:
            self._ask_for_custom_offset(row)

    def _ask_for_custom_offset(self, row: Adw.ComboRow) -> None:
        """Let the user type any offset they like: "5 days", "2 weeks"."""
        previous = self._previous_alarm_value(row)

        amount = Gtk.SpinButton.new_with_range(1, 999, 1)
        amount.set_value(1)
        amount.set_hexpand(True)

        units = Gtk.DropDown.new_from_strings(
            [label for _factor, label in self.CUSTOM_UNITS]
        )
        units.set_selected(2)          # days, the most likely thing to want

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        box.set_margin_top(8)
        box.append(amount)
        box.append(units)
        box.append(Gtk.Label(label="before"))

        dialog = Adw.AlertDialog(
            heading="Custom reminder",
            body="How long before the event should Kairos remind you?",
        )
        dialog.set_extra_child(box)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("set", "Set")
        dialog.set_response_appearance("set", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("set")
        dialog.set_close_response("cancel")

        def on_response(_dialog, response: str) -> None:
            if response != "set":
                self._fill_alarm_row(row, previous)
                return
            factor = self.CUSTOM_UNITS[units.get_selected()][0]
            minutes = int(amount.get_value()) * factor
            self._fill_alarm_row(row, min(minutes, self.MAX_CUSTOM_MINUTES))

        dialog.connect("response", on_response)
        dialog.present(self)

    @staticmethod
    def _previous_alarm_value(row: Adw.ComboRow) -> int:
        """What the row was set to before "Custom…" was picked.

        Used to put the row back if the custom dialog is cancelled, so that
        opening and dismissing it does not silently change the reminder.
        """
        values = [v for v in getattr(row, "values", []) if isinstance(v, int)]
        return values[0] if values else 10

    def _remove_alarm(self, row: Adw.ActionRow) -> None:
        self._reminders.remove(row)
        if row in self._alarm_rows:
            self._alarm_rows.remove(row)
        self._update_reminder_placeholder()

    def _update_reminder_placeholder(self) -> None:
        if hasattr(self, "_empty_reminder_row"):
            self._empty_reminder_row.set_visible(not self._alarm_rows)

    def _collect_alarms(self) -> list[Alarm]:
        alarms: list[Alarm] = []
        for row in self._alarm_rows:
            values = getattr(row, "values", [value for value, _ in Alarm.PRESETS])
            index = row.get_selected()
            if 0 <= index < len(values) and isinstance(values[index], int):
                alarms.append(Alarm(minutes_before=values[index]))
        # Keep them in a predictable order and drop duplicates.
        seen: set[int] = set()
        unique = []
        for alarm in sorted(alarms, key=lambda a: -a.minutes_before):
            if alarm.minutes_before not in seen:
                seen.add(alarm.minutes_before)
                unique.append(alarm)
        return unique

    # -- notes ------------------------------------------------------------

    def _notes_group(self, event: Event | None) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(title="Notes")

        self._notes_view = Gtk.TextView()
        self._notes_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self._notes_view.set_top_margin(8)
        self._notes_view.set_bottom_margin(8)
        self._notes_view.set_left_margin(8)
        self._notes_view.set_right_margin(8)
        self._notes_view.get_buffer().set_text(event.description if event else "")

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_min_content_height(110)
        scroller.set_child(self._notes_view)
        scroller.add_css_class("card")

        group.add(scroller)
        return group

    def _notes_text(self) -> str:
        buffer = self._notes_view.get_buffer()
        return buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False).strip()

    # ------------------------------------------------------------------
    # Saving
    # ------------------------------------------------------------------

    def build_event(self) -> Event | None:
        """Read the form into an Event, or return None if it is not valid."""
        calendar = self._selected_calendar()
        if calendar is None:
            return None

        all_day = self._all_day_row.get_active()
        start = self._start_row.get_value()
        end = self._end_row.get_value()

        if all_day:
            start = start.replace(hour=0, minute=0, second=0, microsecond=0)
            # The editor shows the inclusive last day; storage wants the
            # exclusive one, so add a day back on.
            end = end.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
            if end <= start:
                end = start + timedelta(days=1)
        elif end < start:
            end = start

        index = self._repeat_row.get_selected()
        if index < len(REPEAT_PRESETS):
            rrule = REPEAT_PRESETS[index][0]
        else:
            rrule = self._custom_rrule

        title = self._title_row.get_text().strip() or "(No title)"

        if self._original is not None:
            return self._original.copy(
                calendar_id=calendar.id,
                summary=title,
                location=self._location_row.get_text().strip(),
                url=self._clean_url(),
                description=self._notes_text(),
                start=start,
                end=end,
                all_day=all_day,
                rrule=rrule,
                alarms=self._collect_alarms(),
                # The text is regenerated on save, so the cached original
                # must not linger and win.
                raw_ics="",
            )

        event = Event.new(calendar.id, start, end, summary=title, all_day=all_day)
        event.location = self._location_row.get_text().strip()
        event.url = self._clean_url()
        event.description = self._notes_text()
        event.rrule = rrule
        event.alarms = self._collect_alarms()
        return event

    def _clean_url(self) -> str:
        """What was typed in the Link row, if it is safe to open later.

        Anything that is not http or https is dropped rather than stored:
        the detail bubble hands this straight to the desktop's browser.
        """
        return find_url(self._url_row.get_text().strip()) or ""

    def _on_save(self, _button) -> None:
        event = self.build_event()
        if event is None:
            return
        self.emit("saved", event)
        self.close()
