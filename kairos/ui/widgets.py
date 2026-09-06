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
from kairos.config import settings
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
        self._picker_is_24_hour: bool | None = None

        self._picker = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._picker.set_margin_top(8)
        self._picker.set_margin_bottom(8)
        self._picker.set_margin_start(8)
        self._picker.set_margin_end(8)

        popover = Gtk.Popover()
        popover.set_child(self._picker)
        # Built when the popover is first opened, and rebuilt if the clock
        # preference has changed since — so the picker always matches the
        # format the button is showing.
        popover.connect("show", lambda *_: self._build_picker())
        self.set_popover(popover)

        self._build_picker()
        self._sync_label()

    # -- the picker -------------------------------------------------------

    def _build_picker(self) -> None:
        """Lay out hour/minute spinners to match the clock preference.

        In 24-hour mode that is a plain 0–23 hour spinner. In 12-hour mode the
        hours run 1–12 with an AM/PM chooser beside them, because a 12-hour
        clock face with a 0–23 spinner underneath is exactly the mismatch this
        widget existed to avoid.
        """
        twenty_four = formatting.use_24_hour()
        if twenty_four == self._picker_is_24_hour:
            return
        self._picker_is_24_hour = twenty_four

        clear_children(self._picker)

        low, high = (0, 23) if twenty_four else (1, 12)
        self._hour_spin = Gtk.SpinButton.new_with_range(low, high, 1)
        self._hour_spin.set_wrap(True)
        self._hour_spin.set_orientation(Gtk.Orientation.VERTICAL)
        self._hour_spin.connect("value-changed", self._on_picker_changed)

        self._minute_spin = Gtk.SpinButton.new_with_range(0, 59, self.MINUTE_STEP)
        self._minute_spin.set_wrap(True)
        self._minute_spin.set_orientation(Gtk.Orientation.VERTICAL)
        self._minute_spin.connect("value-changed", self._on_picker_changed)

        self._picker.append(self._hour_spin)
        self._picker.append(Gtk.Label(label=":"))
        self._picker.append(self._minute_spin)

        if twenty_four:
            self._meridiem = None
        else:
            self._meridiem = Gtk.DropDown.new_from_strings(["AM", "PM"])
            self._meridiem.set_valign(Gtk.Align.CENTER)
            self._meridiem.connect("notify::selected", self._on_picker_changed)
            self._picker.append(self._meridiem)

        self._push_to_picker()

    def _push_to_picker(self) -> None:
        """Copy the stored 24-hour time into whatever widgets are on screen."""
        self._updating = True
        try:
            if self._picker_is_24_hour:
                self._hour_spin.set_value(self._hour)
            else:
                self._hour_spin.set_value(self._hour % 12 or 12)
                self._meridiem.set_selected(0 if self._hour < 12 else 1)
            self._minute_spin.set_value(self._minute)
        finally:
            self._updating = False

    def _read_from_picker(self) -> tuple[int, int]:
        """The picker's current value, always as a 24-hour time."""
        hour = int(self._hour_spin.get_value())
        if not self._picker_is_24_hour:
            hour = (hour % 12) + (12 if self._meridiem.get_selected() else 0)
        return hour, int(self._minute_spin.get_value())

    # -- state ------------------------------------------------------------

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
        self._push_to_picker()
        self._sync_label()
        self.emit("time-changed")

    def _on_picker_changed(self, *_args) -> None:
        if self._updating:
            return
        self._hour, self._minute = self._read_from_picker()
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


#: How much horizontal scrolling counts as one deliberate swipe. Touchpads
#: deliver a stream of small deltas, so this is summed rather than compared to
#: a single event.
SWIPE_THRESHOLD = 2.5

#: A touchscreen flick has to be at least this fast, in pixels per second.
SWIPE_VELOCITY = 250.0

#: One gesture should turn one page, not ten. Further swipes are ignored until
#: this long has passed.
SWIPE_COOLDOWN_MS = 350


def add_horizontal_swipe(widget: Gtk.Widget, callback) -> None:
    """Call ``callback(+1)`` on a swipe left and ``callback(-1)`` on a swipe right.

    Two input styles, because people have both:

    * **touchpads** send horizontal scroll deltas, which are summed until they
      pass :data:`SWIPE_THRESHOLD`;
    * **touchscreens** send a swipe gesture, which is judged on velocity.

    The scroll controller runs in the capture phase, so it sees the gesture
    before the view's scrolled window does, but it returns ``False`` for
    anything vertical — otherwise it would eat ordinary scrolling. Note the
    sign convention: scrolling *right* (positive dx) moves *forward* in time,
    which is the same direction the content would move under your finger.
    """
    state = {"accumulated": 0.0, "blocked_until": 0}

    def ready() -> bool:
        return GLib.get_monotonic_time() // 1000 >= state["blocked_until"]

    def fire(direction: int) -> None:
        state["accumulated"] = 0.0
        state["blocked_until"] = GLib.get_monotonic_time() // 1000 + SWIPE_COOLDOWN_MS
        callback(direction)

    def on_scroll(_controller, delta_x: float, delta_y: float) -> bool:
        if abs(delta_x) <= abs(delta_y):
            state["accumulated"] = 0.0
            return False                      # a vertical scroll; not ours
        if not ready():
            return True
        state["accumulated"] += delta_x
        if abs(state["accumulated"]) >= SWIPE_THRESHOLD:
            fire(1 if state["accumulated"] > 0 else -1)
        return True

    scroll = Gtk.EventControllerScroll.new(Gtk.EventControllerScrollFlags.BOTH_AXES)
    scroll.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
    scroll.connect("scroll", on_scroll)
    scroll.connect("scroll-end", lambda *_: state.update(accumulated=0.0))
    widget.add_controller(scroll)

    def on_swipe(_gesture, velocity_x: float, velocity_y: float) -> None:
        if abs(velocity_x) < SWIPE_VELOCITY or abs(velocity_x) <= abs(velocity_y):
            return
        if ready():
            # A flick leftwards drags the content left, revealing what is next.
            fire(1 if velocity_x < 0 else -1)

    gesture = Gtk.GestureSwipe()
    gesture.set_touch_only(True)
    gesture.connect("swipe", on_swipe)
    widget.add_controller(gesture)


class SidebarSection(Gtk.Box):
    """A sidebar heading you can click to fold the section away.

    A disclosure triangle, a label, and an optional widget on the right (a
    button, usually). Whether it is open is remembered in a settings key, so
    the sidebar looks the same next time Kairos starts.
    """

    def __init__(self, title: str, child: Gtk.Widget, *,
                 settings_key: str = "", suffix: Gtk.Widget | None = None) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.settings_key = settings_key
        expanded = settings.get_bool(settings_key) if settings_key else True

        self._arrow = Gtk.Image.new_from_icon_name("pan-down-symbolic")
        self._arrow.add_css_class("kairos-section-arrow")

        label = Gtk.Label(label=title, xalign=0)
        label.add_css_class("kairos-sidebar-heading")
        label.set_hexpand(True)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        row.append(self._arrow)
        row.append(label)

        self._toggle = Gtk.Button()
        self._toggle.set_child(row)
        self._toggle.add_css_class("flat")
        self._toggle.add_css_class("kairos-section-header")
        self._toggle.connect("clicked", lambda *_: self.set_expanded(not self.expanded))

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self._toggle.set_hexpand(True)
        header.append(self._toggle)
        if suffix is not None:
            suffix.set_valign(Gtk.Align.CENTER)
            header.append(suffix)
        self.append(header)

        self._revealer = Gtk.Revealer()
        self._revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        self._revealer.set_transition_duration(150)
        self._revealer.set_child(child)
        self.append(self._revealer)

        self.set_expanded(expanded, remember=False)

    @property
    def expanded(self) -> bool:
        return self._revealer.get_reveal_child()

    def set_expanded(self, expanded: bool, *, remember: bool = True) -> None:
        self._revealer.set_reveal_child(expanded)
        self._arrow.set_from_icon_name(
            "pan-down-symbolic" if expanded else "pan-end-symbolic"
        )
        self._toggle.set_tooltip_text("Collapse" if expanded else "Expand")
        if remember and self.settings_key:
            settings.set(self.settings_key, expanded)


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
