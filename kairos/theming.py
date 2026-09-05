"""Appearance: the built-in stylesheet, the accent colour, and your own CSS.

Kairos loads three stylesheets, in this order, each able to override the last:

1. ``kairos/ui/style.css`` — the shipped look.
2. A tiny generated sheet holding the accent colour, font scale and spacing
   derived from your preferences.
3. ``~/.config/kairos/custom.css`` — yours.  It is watched while Kairos runs,
   so saving the file restyles the window immediately; there is no need to
   restart, and no need to rebuild anything.

That third file is the main customisation story.  Every widget Kairos creates
carries a stable CSS class (``kairos-day-cell``, ``kairos-event-chip``,
``kairos-today`` and so on), which are listed in ``custom.css`` when Kairos
first writes the example file.
"""

from __future__ import annotations

import logging
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from kairos.config import CUSTOM_CSS_FILE, ensure_directories, settings

log = logging.getLogger(__name__)

#: Priorities, lowest first.  Anything above APPLICATION beats libadwaita's
#: own theme, which is what lets a user recolour the app without patching it.
PRIORITY_BASE = Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
PRIORITY_DERIVED = Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1
PRIORITY_USER = Gtk.STYLE_PROVIDER_PRIORITY_USER

#: Written to ``custom.css`` the first time Kairos runs, as a starting point.
EXAMPLE_CSS = """/* Kairos — your own stylesheet.
 *
 * This file is loaded after everything else, so anything you put here wins.
 * Save it and the window restyles itself; there is nothing to restart.
 *
 * Useful selectors:
 *
 *   .kairos-day-cell          one day in the month grid
 *   .kairos-day-cell.today    ... when it is today
 *   .kairos-day-cell.selected ... when it is selected
 *   .kairos-day-cell.outside  ... when it belongs to the neighbouring month
 *   .kairos-day-number        the number in the corner of a day cell
 *   .kairos-event-chip        an event as drawn in the month grid
 *   .kairos-event-block       an event as drawn in the week/day grid
 *   .kairos-all-day-chip      an all-day event above the week grid
 *   .kairos-weekday-heading   "Mon", "Tue", ... above the grid
 *   .kairos-week-number       the ISO week number column
 *   .kairos-hour-label        the times down the side of the week view
 *   .kairos-hour-line         an hour rule in the week view
 *   .kairos-now-line          the red "you are here" line
 *   .kairos-agenda-day        one day heading in the agenda view
 *   .kairos-calendar-dot      the colour swatch beside a calendar's name
 *   .kairos-weekend           any weekend cell or heading
 *
 * Example: make weekends stand out more.
 *
 *   .kairos-day-cell.kairos-weekend {
 *       background-color: alpha(@accent_bg_color, 0.06);
 *   }
 *
 * Example: square, denser event chips.
 *
 *   .kairos-event-chip { border-radius: 2px; padding: 0 4px; }
 */
"""


class ThemeManager:
    """Applies preferences to the widget tree and watches the user's CSS."""

    def __init__(self) -> None:
        self._display = Gdk.Display.get_default()
        self._base_provider = Gtk.CssProvider()
        self._derived_provider = Gtk.CssProvider()
        self._user_provider = Gtk.CssProvider()
        self._monitor: Gio.FileMonitor | None = None
        self._loaded = False

    # ------------------------------------------------------------------
    # Set-up
    # ------------------------------------------------------------------

    def apply(self) -> None:
        """Load or reload every stylesheet, and set light/dark mode."""
        if self._display is None:
            log.warning("no display; skipping stylesheet setup")
            return

        if not self._loaded:
            self._add(self._base_provider, PRIORITY_BASE)
            self._add(self._derived_provider, PRIORITY_DERIVED)
            self._add(self._user_provider, PRIORITY_USER)
            self._loaded = True

        self._load_base_css()
        self._load_derived_css()
        self.reload_user_css()
        self.apply_colour_scheme()

    def _add(self, provider: Gtk.CssProvider, priority: int) -> None:
        Gtk.StyleContext.add_provider_for_display(self._display, provider, priority)

    # ------------------------------------------------------------------
    # The three stylesheets
    # ------------------------------------------------------------------

    def _load_base_css(self) -> None:
        path = Path(__file__).parent / "ui" / "style.css"
        try:
            self._base_provider.load_from_path(str(path))
        except GLib.Error as exc:
            log.error("could not load the built-in stylesheet: %s", exc.message)

    def _load_derived_css(self) -> None:
        """Build the sheet that turns preferences into actual CSS."""
        accent = _safe_colour(settings.get("accent_color"), "#3584e4")
        scale = float(settings.get("font_scale"))
        compact = settings.get_bool("compact_mode")
        chip_radius = "6px" if settings.get_bool("rounded_event_chips") else "2px"

        cell_padding = "2px" if compact else "4px"
        chip_padding = "0 4px" if compact else "1px 6px"

        css = f"""
        /* Generated from your preferences — edit those, not this. */
        .kairos-accent-fg {{ color: {accent}; }}
        .kairos-accent-bg {{ background-color: {accent}; }}

        .kairos-day-cell.today .kairos-day-number {{
            background-color: {accent};
            color: #ffffff;
        }}
        .kairos-day-cell.selected {{
            box-shadow: inset 0 0 0 2px {accent};
        }}
        .kairos-now-line {{ background-color: {accent}; }}

        .kairos-event-chip, .kairos-all-day-chip {{
            border-radius: {chip_radius};
            padding: {chip_padding};
        }}
        .kairos-event-block {{ border-radius: {chip_radius}; }}
        .kairos-day-cell {{ padding: {cell_padding}; }}

        window, .kairos-root {{ font-size: {scale:.2f}em; }}
        """
        try:
            self._derived_provider.load_from_string(css)
        except AttributeError:
            # GTK < 4.12 has no load_from_string.
            self._derived_provider.load_from_data(css.encode("utf-8"))
        except GLib.Error as exc:
            log.error("generated stylesheet is invalid: %s", exc.message)

    def reload_user_css(self) -> None:
        """(Re)load ``~/.config/kairos/custom.css``, if it exists."""
        ensure_directories()
        if not CUSTOM_CSS_FILE.exists():
            try:
                CUSTOM_CSS_FILE.write_text(EXAMPLE_CSS, encoding="utf-8")
            except OSError as exc:
                log.warning("could not write the example stylesheet: %s", exc)
                return

        try:
            self._user_provider.load_from_path(str(CUSTOM_CSS_FILE))
        except GLib.Error as exc:
            # A syntax error in the user's CSS must not break the app; GTK
            # already ignores the bad rule, we just say so.
            log.warning("custom.css: %s", exc.message)

    def watch_user_css(self) -> None:
        """Restyle the window whenever the user saves their stylesheet."""
        if self._monitor is not None:
            return
        try:
            file = Gio.File.new_for_path(str(CUSTOM_CSS_FILE))
            self._monitor = file.monitor_file(Gio.FileMonitorFlags.NONE, None)
            self._monitor.connect("changed", self._on_css_changed)
        except GLib.Error as exc:
            log.debug("cannot watch custom.css: %s", exc.message)

    def _on_css_changed(self, _monitor, _file, _other, event_type) -> None:
        if event_type in (
            Gio.FileMonitorEvent.CHANGES_DONE_HINT,
            Gio.FileMonitorEvent.CREATED,
            Gio.FileMonitorEvent.RENAMED,
        ):
            log.info("custom.css changed; reloading")
            self.reload_user_css()

    # ------------------------------------------------------------------
    # Light / dark
    # ------------------------------------------------------------------

    def apply_colour_scheme(self) -> None:
        style_manager = Adw.StyleManager.get_default()
        scheme = {
            "light": Adw.ColorScheme.FORCE_LIGHT,
            "dark": Adw.ColorScheme.FORCE_DARK,
            "system": Adw.ColorScheme.DEFAULT,
        }[settings.get("theme")]
        style_manager.set_color_scheme(scheme)

    # ------------------------------------------------------------------
    # Reacting to preference changes
    # ------------------------------------------------------------------

    def on_settings_changed(self, key: str | None) -> None:
        """Hooked up to :data:`kairos.config.settings` by the application."""
        if key in (None, "theme"):
            self.apply_colour_scheme()
        if key in (None, "accent_color", "font_scale", "compact_mode", "rounded_event_chips"):
            self._load_derived_css()


# --------------------------------------------------------------------------
# Colours
# --------------------------------------------------------------------------

def _safe_colour(value: str, fallback: str) -> str:
    """Only let a colour GTK can actually parse into the generated CSS.

    The accent colour is interpolated into a stylesheet, so it is the one
    preference where a malformed value could do more than look wrong.  Parsing
    it first means only real colours ever get through.
    """
    rgba = Gdk.RGBA()
    if isinstance(value, str) and rgba.parse(value.strip()):
        return rgba.to_string()
    return fallback


def parse_colour(value: str, fallback: str = "#3584e4") -> Gdk.RGBA:
    """A :class:`Gdk.RGBA` for a calendar colour, never raising."""
    rgba = Gdk.RGBA()
    if not (isinstance(value, str) and rgba.parse(value.strip())):
        rgba.parse(fallback)
    return rgba


def readable_text_colour(background: Gdk.RGBA) -> str:
    """Black or white, whichever is legible on ``background``.

    Uses the usual relative-luminance test so that a pale yellow calendar
    gets dark text and a navy one gets light text.
    """
    def channel(value: float) -> float:
        return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4

    luminance = (
        0.2126 * channel(background.red)
        + 0.7152 * channel(background.green)
        + 0.0722 * channel(background.blue)
    )
    return "#000000" if luminance > 0.45 else "#ffffff"


def chip_css(colour: str) -> str:
    """Inline CSS for one event chip, given its calendar's colour."""
    rgba = parse_colour(colour)
    return (
        f"background-color: {rgba.to_string()};"
        f" color: {readable_text_colour(rgba)};"
    )
