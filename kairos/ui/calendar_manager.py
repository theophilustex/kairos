"""Managing calendars and accounts.

One dialog listing every calendar grouped by the account it came from, with
a colour button and a rename field for each, and buttons to add a local
calendar or connect a new server.
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GObject, Gtk  # noqa: E402

from kairos.models import Calendar
from kairos.theming import parse_colour
from kairos.ui.account_dialog import AccountDialog
from kairos.ui.widgets import colour_swatch


class CalendarManager(Adw.Dialog):
    """The calendar and account list.

    Changes take effect immediately — there is no OK button, because there is
    nothing here that benefits from being able to cancel it.
    """

    __gsignals__ = {
        "changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self, sync_manager) -> None:
        super().__init__()
        self.sync = sync_manager
        self.set_title("Calendars")
        self.set_content_width(560)
        self.set_content_height(640)

        self._toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()

        add_button = Gtk.MenuButton(icon_name="list-add-symbolic")
        add_button.set_tooltip_text("Add a calendar")
        menu = Gtk.PopoverMenu()
        menu_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        menu_box.set_margin_top(6)
        menu_box.set_margin_bottom(6)
        menu_box.set_margin_start(6)
        menu_box.set_margin_end(6)

        local_button = Gtk.Button(label="New calendar on this computer")
        local_button.add_css_class("flat")
        local_button.connect("clicked", lambda *_: (menu.popdown(), self._add_local()))
        menu_box.append(local_button)

        server_button = Gtk.Button(label="Connect a CalDAV / WebDAV account…")
        server_button.add_css_class("flat")
        server_button.connect("clicked", lambda *_: (menu.popdown(), self._add_account()))
        menu_box.append(server_button)

        menu.set_child(menu_box)
        add_button.set_popover(menu)
        header.pack_start(add_button)

        self._toolbar.add_top_bar(header)
        self._page = Adw.PreferencesPage()
        self._toolbar.set_content(self._page)
        self.set_child(self._toolbar)

        self.rebuild()

    # ------------------------------------------------------------------
    # Building the list
    # ------------------------------------------------------------------

    def rebuild(self) -> None:
        # Adw.PreferencesPage has no "remove everything", so the page is
        # replaced wholesale.  It is a small dialog; this is cheaper to read
        # than tracking every group we added.
        self._page = Adw.PreferencesPage()
        self._toolbar.set_content(self._page)

        calendars = self.sync.calendars()
        by_account: dict[str, list[Calendar]] = {}
        for calendar in calendars:
            by_account.setdefault(calendar.account_id, []).append(calendar)

        for account in self.sync.accounts.all():
            group = Adw.PreferencesGroup(title=account.name)
            if not account.is_local:
                group.set_description(account.url)
                group.set_header_suffix(self._account_buttons(account))

            account_calendars = by_account.get(account.id, [])
            if not account_calendars:
                group.add(Adw.ActionRow(
                    title="No calendars",
                    subtitle="Nothing from this account is set up yet.",
                ))
            for calendar in sorted(account_calendars, key=lambda c: c.name.lower()):
                group.add(self._calendar_row(calendar))

            self._page.add(group)

    def _account_buttons(self, account) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)

        edit = Gtk.Button(icon_name="document-edit-symbolic")
        edit.add_css_class("flat")
        edit.set_tooltip_text("Edit this account")
        edit.connect("clicked", lambda *_: self._edit_account(account))
        box.append(edit)

        remove = Gtk.Button(icon_name="user-trash-symbolic")
        remove.add_css_class("flat")
        remove.set_tooltip_text("Remove this account")
        remove.connect("clicked", lambda *_: self._confirm_remove_account(account))
        box.append(remove)

        return box

    def _calendar_row(self, calendar: Calendar) -> Adw.ActionRow:
        row = Adw.ActionRow(title=calendar.name)
        if calendar.read_only:
            row.set_subtitle("Read-only")
        else:
            count = self.sync.storage.count_events(calendar.id)
            row.set_subtitle(f"{count} event{'s' if count != 1 else ''}")

        row.add_prefix(colour_swatch(calendar.colour))

        colour_button = Gtk.ColorDialogButton()
        colour_button.set_dialog(Gtk.ColorDialog())
        colour_button.set_rgba(parse_colour(calendar.colour))
        colour_button.set_valign(Gtk.Align.CENTER)
        colour_button.set_tooltip_text("Change this calendar's colour")
        colour_button.connect("notify::rgba", self._on_colour_changed, calendar)
        row.add_suffix(colour_button)

        visible = Gtk.Switch()
        visible.set_active(calendar.visible)
        visible.set_valign(Gtk.Align.CENTER)
        visible.set_tooltip_text("Show this calendar")
        visible.connect("state-set", self._on_visible_changed, calendar)
        row.add_suffix(visible)

        rename = Gtk.Button(icon_name="document-edit-symbolic")
        rename.add_css_class("flat")
        rename.set_valign(Gtk.Align.CENTER)
        rename.set_tooltip_text("Rename")
        rename.connect("clicked", lambda *_: self._rename(calendar))
        row.add_suffix(rename)

        if calendar.is_local:
            delete = Gtk.Button(icon_name="user-trash-symbolic")
            delete.add_css_class("flat")
            delete.set_valign(Gtk.Align.CENTER)
            delete.set_tooltip_text("Delete this calendar and its events")
            delete.connect("clicked", lambda *_: self._confirm_remove_calendar(calendar))
            row.add_suffix(delete)

        return row

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _on_colour_changed(self, button: Gtk.ColorDialogButton, _param, calendar: Calendar) -> None:
        rgba: Gdk.RGBA = button.get_rgba()
        calendar.colour = _rgba_to_hex(rgba)
        self.sync.update_calendar(calendar)
        self.emit("changed")

    def _on_visible_changed(self, _switch, state: bool, calendar: Calendar) -> bool:
        self.sync.set_calendar_visible(calendar, state)
        self.emit("changed")
        return False  # let the switch update itself

    def _rename(self, calendar: Calendar) -> None:
        dialog = Adw.AlertDialog(heading="Rename calendar")
        entry = Gtk.Entry(text=calendar.name)
        entry.set_margin_top(8)
        dialog.set_extra_child(entry)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("rename", "Rename")
        dialog.set_response_appearance("rename", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("rename")
        dialog.set_close_response("cancel")

        def on_response(_dialog, response: str) -> None:
            if response != "rename":
                return
            name = entry.get_text().strip()
            if name:
                calendar.name = name
                self.sync.update_calendar(calendar)
                self.rebuild()
                self.emit("changed")

        dialog.connect("response", on_response)
        dialog.present(self)

    def _add_local(self) -> None:
        dialog = Adw.AlertDialog(
            heading="New calendar",
            body="This calendar is stored on this computer only.",
        )
        entry = Gtk.Entry(placeholder_text="Calendar name")
        entry.set_margin_top(8)
        dialog.set_extra_child(entry)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("create", "Create")
        dialog.set_response_appearance("create", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("create")
        dialog.set_close_response("cancel")

        def on_response(_dialog, response: str) -> None:
            if response != "create":
                return
            name = entry.get_text().strip() or "New calendar"
            self.sync.add_local_calendar(name, _next_colour(self.sync.calendars()))
            self.rebuild()
            self.emit("changed")

        dialog.connect("response", on_response)
        dialog.present(self)

    def _add_account(self) -> None:
        dialog = AccountDialog()
        dialog.connect("account-added", self._on_account_added)
        dialog.present(self)

    def _edit_account(self, account) -> None:
        dialog = AccountDialog(existing=account)
        dialog.connect("account-added", self._on_account_added)
        dialog.present(self)

    def _on_account_added(self, _dialog, account, calendars: list[Calendar]) -> None:
        self.sync.accounts.add(account)
        existing = {c.id for c in self.sync.calendars()}
        for calendar in calendars:
            if calendar.id in existing:
                continue
            self.sync.storage.save_calendar(calendar)
        self.rebuild()
        self.emit("changed")
        self.sync.sync_now()

    def _confirm_remove_account(self, account) -> None:
        dialog = Adw.AlertDialog(
            heading=f"Remove “{account.name}”?",
            body=("Its calendars will disappear from Kairos and its password "
                  "will be deleted from your keyring. Nothing is deleted from "
                  "the server."),
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("remove", "Remove")
        dialog.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_close_response("cancel")

        def on_response(_dialog, response: str) -> None:
            if response == "remove":
                self.sync.remove_account(account.id)
                self.rebuild()
                self.emit("changed")

        dialog.connect("response", on_response)
        dialog.present(self)

    def _confirm_remove_calendar(self, calendar: Calendar) -> None:
        dialog = Adw.AlertDialog(
            heading=f"Delete “{calendar.name}”?",
            body="Every event in this calendar will be deleted. This cannot be undone.",
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("delete", "Delete")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_close_response("cancel")

        def on_response(_dialog, response: str) -> None:
            if response == "delete":
                self.sync.remove_calendar(calendar)
                self.rebuild()
                self.emit("changed")

        dialog.connect("response", on_response)
        dialog.present(self)


# --------------------------------------------------------------------------
# Colours
# --------------------------------------------------------------------------

#: Offered to new local calendars, in order, so two calendars made in a row
#: do not come out the same colour.
PALETTE = (
    "#3584e4", "#33d17a", "#f6d32d", "#ff7800",
    "#e01b24", "#9141ac", "#986a44", "#2190a4",
)


def _next_colour(existing: list[Calendar]) -> str:
    used = {c.colour.lower() for c in existing}
    for colour in PALETTE:
        if colour not in used:
            return colour
    return PALETTE[len(existing) % len(PALETTE)]


def _rgba_to_hex(rgba: Gdk.RGBA) -> str:
    return "#{:02x}{:02x}{:02x}".format(
        round(rgba.red * 255), round(rgba.green * 255), round(rgba.blue * 255)
    )
