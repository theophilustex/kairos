"""Where Kairos keeps its files, and the settings the user can change.

Kairos stores everything in plain, hand-editable files under the standard XDG
directories.  Nothing is hidden in a binary blob, and no setting is written
anywhere you cannot open in a text editor:

    ~/.config/kairos/settings.json    preferences (this module)
    ~/.config/kairos/accounts.json    calendar accounts, minus passwords
    ~/.config/kairos/custom.css       your own stylesheet, loaded last
    ~/.cache/kairos/cache.db          the offline SQLite copy of your events

Passwords are the one exception: they go to the system keyring, never to disk
in cleartext.  See :mod:`kairos.security`.

Adding a new preference is a two-step job: add it to ``DEFAULTS`` below with a
sensible value, then read it with ``settings.get("your_key")``.  Unknown keys
found in the file are preserved rather than discarded, so a settings file
written by a newer Kairos will survive being opened by an older one.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Locations
# --------------------------------------------------------------------------

def _xdg(env_var: str, fallback: str) -> Path:
    """Return an XDG base directory, honouring the environment variable."""
    value = os.environ.get(env_var)
    base = Path(value) if value else Path.home() / fallback
    return base / "kairos"


CONFIG_DIR = _xdg("XDG_CONFIG_HOME", ".config")
CACHE_DIR = _xdg("XDG_CACHE_HOME", ".cache")
DATA_DIR = _xdg("XDG_DATA_HOME", ".local/share")

SETTINGS_FILE = CONFIG_DIR / "settings.json"
ACCOUNTS_FILE = CONFIG_DIR / "accounts.json"
CUSTOM_CSS_FILE = CONFIG_DIR / "custom.css"
DATABASE_FILE = CACHE_DIR / "cache.db"


def ensure_directories() -> None:
    """Create our config/cache/data directories with private permissions."""
    for directory in (CONFIG_DIR, CACHE_DIR, DATA_DIR):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)


# --------------------------------------------------------------------------
# Defaults
# --------------------------------------------------------------------------

#: Every preference Kairos understands, with its default value.  The comment
#: beside each one is what the preferences dialog shows.
DEFAULTS: dict[str, Any] = {
    # -- Appearance -------------------------------------------------------
    "theme": "system",              # "system" | "light" | "dark"
    "accent_color": "#3584e4",      # any CSS colour; tints selections & chips
    "font_scale": 1.0,              # 0.75 - 1.5, multiplies the UI font size
    "compact_mode": False,          # tighter padding everywhere
    "rounded_event_chips": True,    # square chips look denser
    # -- Calendar layout --------------------------------------------------
    "default_view": "month",        # "month" | "week" | "day" | "agenda"
    "first_day_of_week": "monday",  # "monday" | "sunday" | "saturday"
    "time_format": "24h",           # "24h" | "12h"
    "show_week_numbers": False,
    "highlight_weekends": True,
    "week_view_start_hour": 7,      # first hour scrolled into view
    "week_view_end_hour": 21,
    "hour_height": 48,              # pixels per hour in the week/day views
    "max_chips_per_day": 4,         # month view: chips before "+N more"
    "agenda_days": 30,              # how far the agenda view looks ahead
    # -- Events -----------------------------------------------------------
    "default_event_duration_minutes": 60,
    "default_alarm_minutes": 10,    # reminder offset for new events
    "week_starts_scrolled_to_now": True,
    # -- Notifications ----------------------------------------------------
    "notifications_enabled": True,
    "notification_lookahead_minutes": 60,   # how far ahead alarms are planned
    # -- Syncing ----------------------------------------------------------
    "sync_interval_minutes": 15,    # 0 disables automatic syncing
    "sync_on_startup": True,
    "sync_window_past_days": 365,   # how much history to keep cached
    "sync_window_future_days": 730,
    "network_timeout_seconds": 30,
    # -- Security ---------------------------------------------------------
    # Kairos refuses plain-http calendar URLs unless you deliberately turn
    # this on.  It exists for self-hosted servers on a trusted LAN only.
    "allow_insecure_http": False,
    # Turning this off disables TLS certificate checking for *all* accounts
    # and is a genuinely bad idea; it is here because some self-signed
    # home servers leave people no alternative.
    "verify_tls_certificates": True,
}

#: Keys whose values must be one of a fixed set.  Anything else falls back to
#: the default, so a typo in the JSON file cannot break the app.
_CHOICES: dict[str, tuple[str, ...]] = {
    "theme": ("system", "light", "dark"),
    "default_view": ("month", "week", "day", "agenda"),
    "first_day_of_week": ("monday", "sunday", "saturday"),
    "time_format": ("24h", "12h"),
}

#: Numeric keys clamped to a sane range, for the same reason.
_RANGES: dict[str, tuple[float, float]] = {
    "font_scale": (0.75, 1.5),
    "week_view_start_hour": (0, 23),
    "week_view_end_hour": (1, 24),
    "hour_height": (24, 160),
    "max_chips_per_day": (1, 12),
    "agenda_days": (1, 365),
    "default_event_duration_minutes": (5, 1440),
    "default_alarm_minutes": (0, 40320),
    "notification_lookahead_minutes": (5, 1440),
    "sync_interval_minutes": (0, 1440),
    "sync_window_past_days": (0, 3650),
    "sync_window_future_days": (1, 3650),
    "network_timeout_seconds": (5, 300),
}


class Settings:
    """The preferences file, loaded once and saved whenever it changes.

    Use it like a dictionary::

        settings.get("time_format")          # -> "24h"
        settings.set("time_format", "12h")   # writes settings.json

    Register a callback with :meth:`connect` to react to changes; the UI uses
    this to restyle itself the moment a preference is touched.
    """

    def __init__(self, path: Path | str = SETTINGS_FILE) -> None:
        self.path = Path(path)
        self._values: dict[str, Any] = dict(DEFAULTS)
        self._listeners: list = []
        self.load()

    # -- reading ----------------------------------------------------------

    def get(self, key: str, default: Any = None) -> Any:
        if key in self._values:
            return self._values[key]
        return DEFAULTS.get(key, default)

    def get_int(self, key: str) -> int:
        return int(self.get(key))

    def get_bool(self, key: str) -> bool:
        return bool(self.get(key))

    def as_dict(self) -> dict[str, Any]:
        return dict(self._values)

    # -- writing ----------------------------------------------------------

    def set(self, key: str, value: Any) -> None:
        """Set one preference and persist the file (no-op if unchanged)."""
        value = self._coerce(key, value)
        if self._values.get(key) == value:
            return
        self._values[key] = value
        self.save()
        self._notify(key)

    def reset_to_defaults(self) -> None:
        self._values = dict(DEFAULTS)
        self.save()
        self._notify(None)

    # -- change notification ---------------------------------------------

    def connect(self, callback) -> None:
        """Call ``callback(key_or_None)`` whenever a preference changes."""
        self._listeners.append(callback)

    def _notify(self, key: str | None) -> None:
        for callback in list(self._listeners):
            try:
                callback(key)
            except Exception:  # a broken listener must not break saving
                log.exception("settings listener failed for key %r", key)

    # -- persistence ------------------------------------------------------

    def load(self) -> None:
        """Read settings.json, ignoring (but keeping) anything unexpected."""
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("could not read %s (%s); using defaults", self.path, exc)
            return
        if not isinstance(raw, dict):
            log.warning("%s is not a JSON object; using defaults", self.path)
            return
        for key, value in raw.items():
            self._values[key] = self._coerce(key, value)

    def save(self) -> None:
        from kairos.security import write_private_json

        ensure_directories()
        write_private_json(self.path, self._values)

    # -- validation -------------------------------------------------------

    def _coerce(self, key: str, value: Any) -> Any:
        """Force ``value`` into the shape the rest of the app expects.

        A bad value in the file is replaced by the default rather than raising,
        so a hand-edit typo degrades gracefully instead of refusing to start.
        """
        default = DEFAULTS.get(key)

        if key in _CHOICES:
            return value if value in _CHOICES[key] else default

        if isinstance(default, bool):
            return bool(value)

        if key in _RANGES:
            low, high = _RANGES[key]
            try:
                number = float(value)
            except (TypeError, ValueError):
                return default
            number = max(low, min(high, number))
            return number if isinstance(default, float) else int(number)

        if isinstance(default, str) and not isinstance(value, str):
            return default

        return value


#: The single Settings instance the whole application shares.
settings = Settings()


def first_weekday_index() -> int:
    """Return the first day of the week as a Python weekday number.

    Python numbers weekdays Monday=0 ... Sunday=6, and so do we.
    """
    return {"monday": 0, "saturday": 5, "sunday": 6}[settings.get("first_day_of_week")]
