"""Small widgets and helpers shared by the views.

Nothing here knows about calendars or syncing — these are generic pieces that
happen to be useful more than once.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, GObject, Gtk  # noqa: E402

from kairos import formatting
from kairos.models import local_timezone
from kairos.theming import parse_colour, readable_text_colour

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Inline styling
# --------------------------------------------------------------------------

def style_widget(widget: Gtk.Widget, css_body: str) -> None:
    """Apply a little CSS to one widget only.

    GTK has no per-widget style attribute, so we attach a tiny provider to the
    widget's own style context.  Used for event colours, which come from data
    rather than from the stylesheet.
    """
    provider = Gtk.CssProvider()
    css = "* {" + css_body + "}"
    try:
        provider.load_from_string(css)
    except AttributeError:
        provider.load_from_data(css.encode("utf-8"))
    except GLib.Error as exc:
        log.debug("inline css rejected (%s): %s", exc.message, css_body)
        return
    widget.get_style_context().add_provider(provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 2)


def colour_swatch(colour: str, size: int = 12) -> Gtk.Widget:
    """A small rounded square in ``colour``, for calendar lists."""
    swatch = Gtk.Box()
    swatch.add_css_class("kairos-calendar-dot")
    swatch.set_size_request(size, size)
    swatch.set_valign(Gtk.Align.CENTER)
    rgba = parse_colour(colour)
    style_widget(swatch, f"background-color: {rgba.to_string()};")
    return swatch


def tinted_button_css(colour: str) -> str:
    """Background and legible foreground for an event chip or block."""
    rgba = parse_colour(colour)
    return (
        f"background-color: {rgba.to_string()};"
        f" color: {readable_text_colour(rgba)};"
        " background-image: none;"
    )


# --------------------------------------------------------------------------
# Date and time entry
# --------------------------------------------------------------------------

class DateButton(Gtk.MenuButton):
    """A button showing a date, with a calendar popover to change it.

    GTK has no date-entry widget, so this is the standard combination: a
    button whose label is the date and a :class:`Gtk.Calendar` in a popover.
    """

    __gsignals__ = {
        "date-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self, value: date | None = None) -> None:
        super().__init__()
        self._value = value or date.today()
        self._updating = False

        self._calendar = Gtk.Calendar()
        self._calendar.connect("day-selected", self._on_day_selected)

        popover = Gtk.Popover()
        popover.set_child(self._calendar)
        self.set_popover(popover)

        self._sync_label()

    @property
    def value(self) -> date:
        return self._value

    def set_value(self, value: date) -> None:
        if value == self._value:
            return
        self._value = value
        self._sync_label()
        self.emit("date-changed")

    def _sync_label(self) -> None:
        self.set_label(formatting.format_date_short(self._value))
        self._updating = True
        try:
            self._calendar.select_day(
                GLib.DateTime.new_local(self._value.year, self._value.month, self._value.day, 12, 0, 0)
            )
        finally:
            self._updating = False

    def _on_day_selected(self, calendar: Gtk.Calendar) -> None:
        if self._updating:
            return
        selected = calendar.get_date()
        new_value = date(selected.get_year(), selected.get_month(), selected.get_day_of_month())
        if new_value != self._value:
            self._value = new_value
            self.set_label(formatting.format_date_short(new_value))
            self.emit("date-changed")
        popover = self.get_popover()
        if popover is not None:
            popover.popdown()


class TimeButton(Gtk.MenuButton):
    """A button showing a time, with hour/minute spinners in a popover."""

    __gsignals__ = {
        "time-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    #: Minutes snap to this, which is what people actually schedule on.
    MINUTE_STEP = 5

    def __init__(self, value: datetime | None = None) -> None:
        super().__init__()
        moment = value or datetime.now(tz=local_timezone())
        self._hour = moment.hour
        self._minute = (moment.minute // self.MINUTE_STEP) * self.MINUTE_STEP
        self._updating = False

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        box.set_margin_top(8)
        box.set_margin_bottom(8)
        box.set_margin_start(8)
        box.set_margin_end(8)

        self._hour_spin = Gtk.SpinButton.new_with_range(0, 23, 1)
        self._hour_spin.set_wrap(True)
        self._hour_spin.set_orientation(Gtk.Orientation.VERTICAL)
        self._hour_spin.set_value(self._hour)
        self._hour_spin.connect("value-changed", self._on_spin_changed)

        self._minute_spin = Gtk.SpinButton.new_with_range(0, 59, self.MINUTE_STEP)
        self._minute_spin.set_wrap(True)
        self._minute_spin.set_orientation(Gtk.Orientation.VERTICAL)
        self._minute_spin.set_value(self._minute)
        self._minute_spin.connect("value-changed", self._on_spin_changed)

        box.append(self._hour_spin)
        box.append(Gtk.Label(label=":"))
        box.append(self._minute_spin)

        popover = Gtk.Popover()
        popover.set_child(box)
        self.set_popover(popover)

        self._sync_label()

    @property
    def hour(self) -> int:
        return self._hour

    @property
    def minute(self) -> int:
        return self._minute

    def set_time(self, hour: int, minute: int) -> None:
        hour = max(0, min(23, int(hour)))
        minute = max(0, min(59, int(minute)))
        if (hour, minute) == (self._hour, self._minute):
            return
        self._hour, self._minute = hour, minute
        self._updating = True
        try:
            self._hour_spin.set_value(hour)
            self._minute_spin.set_value(minute)
        finally:
            self._updating = False
        self._sync_label()
        self.emit("time-changed")

    def _on_spin_changed(self, _spin) -> None:
        if self._updating:
            return
        self._hour = int(self._hour_spin.get_value())
        self._minute = int(self._minute_spin.get_value())
        self._sync_label()
        self.emit("time-changed")

    def _sync_label(self) -> None:
        moment = datetime(2000, 1, 1, self._hour, self._minute)
        self.set_label(formatting.format_time(moment))


class DateTimeRow(Adw.ActionRow):
    """A preferences row holding a date button and (optionally) a time button.

    The event editor uses two of these, for the start and the end.  Hiding the
    time half is how it switches to all-day mode.
    """

    __gsignals__ = {
        "changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self, title: str, value: datetime) -> None:
        super().__init__(title=title)
        self.date_button = DateButton(value.date())
        self.time_button = TimeButton(value)
        self.date_button.connect("date-changed", lambda *_: self.emit("changed"))
        self.time_button.connect("time-changed", lambda *_: self.emit("changed"))

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        box.set_valign(Gtk.Align.CENTER)
        box.append(self.date_button)
        box.append(self.time_button)
        self.add_suffix(box)

    def get_value(self) -> datetime:
        day = self.date_button.value
        return datetime(
            day.year, day.month, day.day,
            self.time_button.hour, self.time_button.minute,
            tzinfo=local_timezone(),
        )

    def set_value(self, value: datetime) -> None:
        self.date_button.set_value(value.date())
        self.time_button.set_time(value.hour, value.minute)

    def set_time_visible(self, visible: bool) -> None:
        self.time_button.set_visible(visible)


# --------------------------------------------------------------------------
# Misc helpers
# --------------------------------------------------------------------------

def on_click(widget: Gtk.Widget, callback, *, button: int = Gdk.BUTTON_PRIMARY) -> Gtk.GestureClick:
    """Call ``callback(n_press, x, y)`` when ``widget`` is clicked.

    A one-liner around :class:`Gtk.GestureClick`, because every view needs it
    and the three-step set-up is easy to get subtly wrong.
    """
    gesture = Gtk.GestureClick()
    gesture.set_button(button)
    gesture.connect("pressed", lambda _g, n, x, y: callback(n, x, y))
    widget.add_controller(gesture)
    return gesture


def empty_state(title: str, subtitle: str = "", icon: str = "x-office-calendar-symbolic") -> Gtk.Widget:
    """The "nothing to show here" placeholder used by several views."""
    status = Adw.StatusPage(title=title)
    if subtitle:
        status.set_description(subtitle)
    status.set_icon_name(icon)
    status.add_css_class("kairos-empty-state")
    status.set_vexpand(True)
    return status


def clear_children(widget: Gtk.Widget) -> None:
    """Remove every child of a container widget.

    GTK4 removed ``foreach``, so this walks the sibling chain instead.  Views
    call it at the top of every redraw.
    """
    child = widget.get_first_child()
    while child is not None:
        next_child = child.get_next_sibling()
        if isinstance(widget, Gtk.Grid):
            widget.remove(child)
        elif isinstance(widget, Gtk.Box):
            widget.remove(child)
        elif isinstance(widget, Gtk.ListBox):
            widget.remove(child)
        else:
            child.unparent()
        child = next_child


def days_between(first: date, last: date) -> list[date]:
    return [first + timedelta(days=offset) for offset in range((last - first).days + 1)]
