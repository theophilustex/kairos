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

A due reminder is delivered two ways: a desktop notification, always, and —
unless it is switched off — an alert window that asks to be brought to the
front, because a notification that slides away after four seconds is very easy
to miss.  This module does not know what that window looks like; it calls
:attr:`AlarmScheduler.on_alert`, which the application wires up.

Snoozed reminders are kept in their own file rather than recreated from the
calendar, so that "remind me in an hour" survives a restart even if the event
itself has been edited or the laptop was asleep.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from gi.repository import Gio, GLib

from kairos.config import CACHE_DIR, settings
from kairos.models import Occurrence, local_timezone
from kairos.recurrence import expand
from kairos.security import read_json, write_private_json

log = logging.getLogger(__name__)

#: Where fired-alarm markers are remembered between runs.
HISTORY_FILE = CACHE_DIR / "fired_alarms.json"

#: Where snoozed reminders wait. Separate from the fired-alarm history because
#: these have to survive a restart with enough detail to be shown again.
SNOOZE_FILE = CACHE_DIR / "snoozed_reminders.json"

#: An alarm whose moment passed more than this long ago is not worth showing;
#: the user has moved on.  It is still recorded as fired.
STALE_AFTER = timedelta(minutes=30)

#: Never sleep longer than this, so that a machine waking from suspend
#: re-checks reasonably promptly.
MAX_SLEEP_SECONDS = 15 * 60


@dataclass
class PendingReminder:
    """One reminder that is due, with everything needed to show it.

    Deliberately self-contained: a snoozed reminder is written to disk and may
    be shown an hour later, by which time the occurrence it came from could
    have been edited away.  Copying the handful of fields we display is far
    simpler than trying to find it again.
    """

    key: str                      # identifies the alarm, for the fired history
    uid: str
    summary: str
    start: datetime
    end: datetime
    all_day: bool = False
    location: str = ""
    calendar_id: str = ""
    calendar_name: str = ""
    calendar_colour: str = "#3584e4"
    minutes_before: int = 0
    fire_at: datetime | None = None
    snoozed: bool = False

    # -- wording ----------------------------------------------------------

    def countdown_text(self) -> str:
        """The headline: how long until this starts, or how long since."""
        now = datetime.now(tz=local_timezone())
        if self.start > now:
            return f"In {_minutes_phrase(max(1, round((self.start - now).total_seconds() / 60)))}"
        if self.end > now:
            return "Happening now"
        ago = max(1, round((now - self.start).total_seconds() / 60))
        return f"Started {_minutes_phrase(ago)} ago"

    def when_text(self) -> str:
        """The date and time, spelled out."""
        from kairos import formatting
        day = formatting.format_day_heading(self.start.date())
        if self.all_day:
            return f"{day} · All day"
        return (f"{day} · {formatting.format_time(self.start)}"
                f" – {formatting.format_time(self.end)}")

    # -- persistence ------------------------------------------------------

    def to_json(self) -> dict:
        return {
            "key": self.key,
            "uid": self.uid,
            "summary": self.summary,
            "start": self.start.timestamp(),
            "end": self.end.timestamp(),
            "all_day": self.all_day,
            "location": self.location,
            "calendar_id": self.calendar_id,
            "calendar_name": self.calendar_name,
            "calendar_colour": self.calendar_colour,
            "minutes_before": self.minutes_before,
            "fire_at": self.fire_at.timestamp() if self.fire_at else None,
            "snoozed": self.snoozed,
        }

    @classmethod
    def from_json(cls, raw: dict) -> "PendingReminder":
        def moment(value):
            return datetime.fromtimestamp(float(value), tz=local_timezone())

        return cls(
            key=str(raw["key"]),
            uid=str(raw.get("uid", "")),
            summary=str(raw.get("summary", "")),
            start=moment(raw["start"]),
            end=moment(raw["end"]),
            all_day=bool(raw.get("all_day", False)),
            location=str(raw.get("location", "")),
            calendar_id=str(raw.get("calendar_id", "")),
            calendar_name=str(raw.get("calendar_name", "")),
            calendar_colour=str(raw.get("calendar_colour", "#3584e4")),
            minutes_before=int(raw.get("minutes_before", 0)),
            fire_at=moment(raw["fire_at"]) if raw.get("fire_at") else None,
            snoozed=bool(raw.get("snoozed", False)),
        )


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
        self._snoozed: dict[str, PendingReminder] = self._load_snoozes()

        #: Called with a :class:`PendingReminder` when one comes due. The
        #: application sets this to raise the alert window; left unset (in
        #: tests, or with the window switched off) only the desktop
        #: notification is sent.
        self.on_alert = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        self.reschedule()

    def stop(self) -> None:
        self._cancel_timer()
        self._save_history()
        self._save_snoozes()

    def reschedule(self, *_args) -> None:
        """Fire anything due, then arm a single timer for whatever is next."""
        self._cancel_timer()
        if not settings.get_bool("notifications_enabled"):
            return

        now = datetime.now(tz=local_timezone())
        next_moments = []

        # -- alarms that come from the calendar ---------------------------
        for fire_at, occurrence, minutes in self.upcoming_alarms(now):
            if fire_at > now:
                next_moments.append(fire_at)
                continue
            # Due, including anything missed while the laptop was asleep.
            if now - fire_at <= STALE_AFTER:
                self._raise(self._reminder_for(occurrence, minutes, fire_at))
            self._mark_fired(fire_at, occurrence, minutes)

        # -- reminders the user snoozed -----------------------------------
        for reminder in list(self._snoozed.values()):
            if reminder.fire_at is not None and reminder.fire_at > now:
                next_moments.append(reminder.fire_at)
                continue
            self._snoozed.pop(reminder.key, None)
            self._raise(reminder)
        self._save_snoozes()

        seconds = min((moment - now).total_seconds() for moment in next_moments) \
            if next_moments else MAX_SLEEP_SECONDS
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
        longest_offset = timedelta(minutes=max(
            self._longest_alarm_offset(),
            max((c.default_alarm_minutes for c in self.sync.calendars()),
                default=0)))
        window_start = now - STALE_AFTER
        window_end = now + lookahead + longest_offset

        events = self.sync.all_events_between(window_start, window_end)
        occurrences = expand(events, window_start, window_end)

        defaults = {c.id: c.default_alarm for c in self.sync.calendars()}

        planned: list[tuple[datetime, Occurrence, int]] = []
        for occurrence in occurrences:
            # An event with no reminder of its own falls back to the one set
            # on its calendar, if any. Some servers keep reminders in their
            # own interface and never send a VALARM, so without this a whole
            # calendar arrives with nothing to remind you about.
            alarms = occurrence.event.alarms
            if not alarms:
                fallback = defaults.get(occurrence.calendar_id)
                alarms = [fallback] if fallback is not None else []
            for alarm in alarms:
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

    def _reminder_for(self, occurrence: Occurrence, minutes_before: int,
                      fire_at: datetime) -> PendingReminder:
        """Capture everything the alert needs about one due occurrence."""
        calendar = self.sync.storage.get_calendar(occurrence.calendar_id)
        return PendingReminder(
            key=self._key(fire_at, occurrence, minutes_before),
            uid=occurrence.uid,
            summary=occurrence.summary,
            start=occurrence.start,
            end=occurrence.end,
            all_day=occurrence.all_day,
            location=occurrence.event.location,
            calendar_id=occurrence.calendar_id,
            calendar_name=calendar.name if calendar else "",
            calendar_colour=calendar.colour if calendar else "#3584e4",
            minutes_before=minutes_before,
            fire_at=fire_at,
        )

    def _raise(self, reminder: PendingReminder) -> None:
        """Deliver one reminder, by every route the user has left switched on.

        The notification always goes out. The alert window is additional, and
        is what makes a reminder hard to miss; if the application has not
        wired one up, or the user has turned it off, the notification stands
        on its own as before.
        """
        self._notify(reminder)
        if self.on_alert is not None and settings.get_bool("reminder_alert_window"):
            try:
                self.on_alert(reminder)
            except Exception:
                log.exception("could not show the reminder alert window")

    def snooze(self, reminder: PendingReminder, minutes: int) -> None:
        """Show this reminder again in ``minutes`` minutes."""
        minutes = max(1, int(minutes))
        reminder.fire_at = datetime.now(tz=local_timezone()) + timedelta(minutes=minutes)
        reminder.snoozed = True
        self._snoozed[reminder.key] = reminder
        # Snoozed reminders are re-raised from this list, not from the
        # calendar, so the original alarm must stay marked as fired.
        self._fired[reminder.key] = _now_stamp()
        self._save_snoozes()
        self._save_history()
        log.info("snoozed “%s” for %d minutes", reminder.summary, minutes)
        self.reschedule()

    def dismiss(self, reminder: PendingReminder) -> None:
        """Stop reminding about this one."""
        self._snoozed.pop(reminder.key, None)
        self._fired[reminder.key] = _now_stamp()
        self._save_snoozes()
        self._save_history()

    def _notify(self, reminder: PendingReminder) -> None:
        """Show one desktop notification."""
        notification = Gio.Notification.new(reminder.summary or "(No title)")
        notification.set_body(self._body(reminder))
        notification.set_priority(Gio.NotificationPriority.URGENT)
        try:
            notification.set_category("event.reminder")
        except Exception:
            pass  # Older GLib; the category is advisory anyway.

        # Clicking the notification opens Kairos on the right day.
        try:
            notification.set_default_action_and_target(
                "app.show-day", GLib.Variant("s", reminder.start.date().isoformat())
            )
        except Exception as exc:
            log.debug("could not attach notification action: %s", exc)

        # A stable id means a repeated reminder replaces its predecessor in
        # the shell rather than stacking up.
        self.application.send_notification(
            f"kairos-{reminder.uid}-{int(reminder.start.timestamp())}", notification
        )
        log.info("reminder shown for “%s”", reminder.summary)

    def _body(self, reminder: PendingReminder) -> str:
        parts = [f"{reminder.countdown_text()} · {reminder.when_text()}"]
        if reminder.location:
            parts.append(reminder.location)
        return "\n".join(parts)

    # ------------------------------------------------------------------
    # Remembering what already fired
    # ------------------------------------------------------------------

    @staticmethod
    def _key(fire_at: datetime, occurrence: Occurrence, minutes_before: int) -> str:
        return f"{occurrence.uid}|{int(occurrence.start.timestamp())}|{minutes_before}"

    def _mark_fired(self, fire_at: datetime, occurrence: Occurrence, minutes_before: int) -> None:
        """Record that this alarm has been dealt with.

        The value stored is *when we dealt with it*, not when it was due. The
        history is pruned by age, so stamping it with the alarm's own time
        would make a reminder for a long-past event expire from the history
        the moment it was written — and then fire again.
        """
        self._fired[self._key(fire_at, occurrence, minutes_before)] = _now_stamp()
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

    def _load_snoozes(self) -> dict[str, PendingReminder]:
        """Read back reminders that were snoozed before Kairos last exited."""
        raw = read_json(SNOOZE_FILE, default=[])
        if not isinstance(raw, list):
            return {}
        snoozed: dict[str, PendingReminder] = {}
        for entry in raw:
            try:
                reminder = PendingReminder.from_json(entry)
            except (KeyError, TypeError, ValueError) as exc:
                log.warning("discarding an unreadable snoozed reminder: %s", exc)
                continue
            snoozed[reminder.key] = reminder
        return snoozed

    def _save_snoozes(self) -> None:
        try:
            write_private_json(
                SNOOZE_FILE, [r.to_json() for r in self._snoozed.values()]
            )
        except OSError as exc:
            log.warning("could not record snoozed reminders: %s", exc)

    def _save_history(self) -> None:
        cutoff = (datetime.now(tz=local_timezone()) - timedelta(days=1)).timestamp()
        self._fired = {k: v for k, v in self._fired.items() if v >= cutoff}
        try:
            write_private_json(HISTORY_FILE, self._fired)
        except OSError as exc:
            log.warning("could not record fired reminders: %s", exc)


def _now_stamp() -> float:
    return datetime.now(tz=local_timezone()).timestamp()


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
