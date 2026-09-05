"""Desktop reminders, driven by each event's own alarm settings.

Kairos does not poll every minute.  Instead it builds a short list of the
alarms due in the next hour or so, sleeps until the earliest one, fires it,
and rebuilds the list.  A machine with a thousand events costs exactly one
timer, which is the whole point of a lightweight calendar.

The plan is rebuilt whenever:

* an alarm fires,
* the cached events change (a sync, or the user edited something),
* the lookahead window runs out.

Alarms that have already fired are remembered in a small file, so restarting
Kairos — or suspending and resuming the laptop — does not replay this
morning's reminders.  Entries older than a day are pruned automatically.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from gi.repository import Gio, GLib

from kairos.config import CACHE_DIR, settings
from kairos.models import Occurrence, local_timezone
from kairos.recurrence import expand
from kairos.security import read_json, write_private_json

log = logging.getLogger(__name__)

#: Where fired-alarm markers are remembered between runs.
HISTORY_FILE = CACHE_DIR / "fired_alarms.json"

#: An alarm whose moment passed more than this long ago is not worth showing;
#: the user has moved on.  It is still recorded as fired.
STALE_AFTER = timedelta(minutes=30)

#: Never sleep longer than this, so that a machine waking from suspend
#: re-checks reasonably promptly.
MAX_SLEEP_SECONDS = 15 * 60


class AlarmScheduler:
    """Plans and fires reminders for the events in the cache.

    Construct one per application, call :meth:`start`, and call
    :meth:`reschedule` whenever the events change.
    """

    def __init__(self, application: Gio.Application, sync_manager) -> None:
        self.application = application
        self.sync = sync_manager
        self._timer_id = 0
        self._fired: dict[str, float] = self._load_history()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        self.reschedule()

    def stop(self) -> None:
        self._cancel_timer()
        self._save_history()

    def reschedule(self, *_args) -> None:
        """Work out the next alarm and arm a single timer for it."""
        self._cancel_timer()
        if not settings.get_bool("notifications_enabled"):
            return

        now = datetime.now(tz=local_timezone())
        upcoming = self.upcoming_alarms(now)

        # Anything already due (including things missed while suspended)
        # fires straight away.
        due = [item for item in upcoming if item[0] <= now]
        for fire_at, occurrence, minutes in due:
            if now - fire_at <= STALE_AFTER:
                self._notify(occurrence, minutes)
            self._mark_fired(fire_at, occurrence, minutes)

        remaining = [item for item in upcoming if item[0] > now]
        if remaining:
            seconds = (remaining[0][0] - now).total_seconds()
        else:
            seconds = MAX_SLEEP_SECONDS

        seconds = max(1.0, min(seconds, MAX_SLEEP_SECONDS))
        self._timer_id = GLib.timeout_add_seconds(int(seconds) + 1, self._on_timer)

    def _on_timer(self) -> bool:
        self._timer_id = 0
        self.reschedule()
        return GLib.SOURCE_REMOVE

    def _cancel_timer(self) -> None:
        if self._timer_id:
            GLib.source_remove(self._timer_id)
            self._timer_id = 0

    # ------------------------------------------------------------------
    # Planning
    # ------------------------------------------------------------------

    def upcoming_alarms(self, now: datetime) -> list[tuple[datetime, Occurrence, int]]:
        """Alarms due between "a little while ago" and the lookahead horizon.

        Returned as ``(fire_at, occurrence, minutes_before)``, earliest first,
        with anything already fired filtered out.
        """
        lookahead = timedelta(minutes=settings.get_int("notification_lookahead_minutes"))

        # An event can have a reminder a week ahead of it, so the *events* we
        # need to consider start well after the alarms we are planning.
        longest_offset = timedelta(minutes=self._longest_alarm_offset())
        window_start = now - STALE_AFTER
        window_end = now + lookahead + longest_offset

        events = self.sync.all_events_between(window_start, window_end)
        occurrences = expand(events, window_start, window_end)

        planned: list[tuple[datetime, Occurrence, int]] = []
        for occurrence in occurrences:
            for alarm in occurrence.event.alarms:
                fire_at = occurrence.start - timedelta(minutes=alarm.minutes_before)
                if fire_at < window_start or fire_at > now + lookahead:
                    continue
                if self._key(fire_at, occurrence, alarm.minutes_before) in self._fired:
                    continue
                planned.append((fire_at, occurrence, alarm.minutes_before))

        planned.sort(key=lambda item: item[0])
        return planned

    def _longest_alarm_offset(self) -> int:
        """The biggest reminder offset in use, so the window covers it."""
        try:
            now = datetime.now(tz=local_timezone())
            events = self.sync.all_events_between(now - timedelta(days=1), now + timedelta(days=30))
        except Exception:
            return 1440
        offsets = [
            alarm.minutes_before
            for event in events
            for alarm in event.alarms
            if alarm.minutes_before > 0
        ]
        return max(offsets, default=1440)

    # ------------------------------------------------------------------
    # Firing
    # ------------------------------------------------------------------

    def _notify(self, occurrence: Occurrence, minutes_before: int) -> None:
        """Show one desktop notification."""
        notification = Gio.Notification.new(occurrence.summary or "(No title)")
        notification.set_body(self._body(occurrence, minutes_before))
        notification.set_priority(Gio.NotificationPriority.HIGH)
        try:
            notification.set_category("event.reminder")
        except Exception:
            pass  # Older GLib; the category is advisory anyway.

        # Clicking the notification opens Kairos on the right day.
        try:
            notification.set_default_action_and_target(
                "app.show-day", GLib.Variant("s", occurrence.start.date().isoformat())
            )
        except Exception as exc:
            log.debug("could not attach notification action: %s", exc)

        # A stable id means a repeated reminder replaces its predecessor in
        # the shell rather than stacking up.
        self.application.send_notification(
            f"kairos-{occurrence.uid}-{int(occurrence.start.timestamp())}", notification
        )
        log.info("reminder shown for “%s”", occurrence.summary)

    def _body(self, occurrence: Occurrence, minutes_before: int) -> str:
        from kairos.formatting import format_time

        when = "Now" if minutes_before == 0 else f"In {_minutes_phrase(minutes_before)}"
        if occurrence.all_day:
            detail = occurrence.start.strftime("%A %d %B")
        else:
            detail = f"{format_time(occurrence.start)} – {format_time(occurrence.end)}"
        location = occurrence.event.location
        parts = [f"{when} · {detail}"]
        if location:
            parts.append(location)
        return "\n".join(parts)

    # ------------------------------------------------------------------
    # Remembering what already fired
    # ------------------------------------------------------------------

    @staticmethod
    def _key(fire_at: datetime, occurrence: Occurrence, minutes_before: int) -> str:
        return f"{occurrence.uid}|{int(occurrence.start.timestamp())}|{minutes_before}"

    def _mark_fired(self, fire_at: datetime, occurrence: Occurrence, minutes_before: int) -> None:
        self._fired[self._key(fire_at, occurrence, minutes_before)] = fire_at.timestamp()
        self._save_history()

    def _load_history(self) -> dict[str, float]:
        raw = read_json(HISTORY_FILE, default={})
        if not isinstance(raw, dict):
            return {}
        cutoff = (datetime.now(tz=local_timezone()) - timedelta(days=1)).timestamp()
        return {
            key: float(value)
            for key, value in raw.items()
            if isinstance(value, (int, float)) and float(value) >= cutoff
        }

    def _save_history(self) -> None:
        cutoff = (datetime.now(tz=local_timezone()) - timedelta(days=1)).timestamp()
        self._fired = {k: v for k, v in self._fired.items() if v >= cutoff}
        try:
            write_private_json(HISTORY_FILE, self._fired)
        except OSError as exc:
            log.warning("could not record fired reminders: %s", exc)


def _minutes_phrase(minutes: int) -> str:
    """"90" -> "1 hour 30 minutes", for notification bodies."""
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    hours, rest = divmod(minutes, 60)
    if hours < 24:
        phrase = f"{hours} hour{'s' if hours != 1 else ''}"
        return phrase if rest == 0 else f"{phrase} {rest} minutes"
    days, hours = divmod(hours, 24)
    phrase = f"{days} day{'s' if days != 1 else ''}"
    return phrase if hours == 0 else f"{phrase} {hours} hours"
