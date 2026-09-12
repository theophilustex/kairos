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
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta

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

#: How far back, when the machine wakes, a reminder that fell due while it
#: slept is still shown. Longer than any ordinary sleep; short enough that a
#: laptop left in a drawer for a week does not open with a week of them.
MISSED_WHILE_ASLEEP_LIMIT = timedelta(hours=24)

#: How often to check whether the wall clock jumped, and by how much more than
#: awake time it has to have moved to count.
HEARTBEAT_SECONDS = 60
CLOCK_JUMP_TOLERANCE_SECONDS = 90


def clock_jumped(wall_elapsed: float, awake_elapsed: float) -> bool:
    """Whether more real time passed than this process was awake for.

    GLib's timers, like ``time.monotonic``, stop while the machine sleeps, so
    after a suspend the wall clock has moved on further than they have. A
    system clock set backwards shows up here too.
    """
    return abs(wall_elapsed - awake_elapsed) > CLOCK_JUMP_TOLERANCE_SECONDS


def day_from_reminder_key(key: str) -> date | None:
    """The day a reminder is about, read from its key alone.

    A notification's Open button carries only the key, and Kairos may have
    restarted since it was shown; the key still says when the event starts.
    """
    try:
        _uid, stamp, _minutes = key.rsplit("|", 2)
        return datetime.fromtimestamp(int(stamp), tz=local_timezone()).date()
    except (ValueError, OverflowError, OSError):
        return None


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
        #: When reminders were last checked, by the wall clock. After a sleep
        #: this is how far back a reminder that was missed is looked for.
        self._last_check: datetime | None = None
        self._heartbeat_id = 0
        self._heartbeat_wall = 0.0
        self._heartbeat_awake = 0.0
        self._system_bus = None
        self._sleep_subscription = 0
        #: Reminders shown recently, by key: a notification button can only
        #: carry a string, and this is how its action finds the reminder.
        self._recent: dict[str, PendingReminder] = {}
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
        self._watch_for_sleep()
        self._start_heartbeat()
        self.reschedule()

    def stop(self) -> None:
        self._cancel_timer()
        self._stop_heartbeat()
        self._unwatch_sleep()
        self._save_history()
        self._save_snoozes()

    def reschedule(self, *_args) -> None:
        """Fire anything due, then arm a single timer for whatever is next."""
        self._cancel_timer()
        if not settings.get_bool("notifications_enabled"):
            return

        now = datetime.now(tz=local_timezone())
        since = self._missed_since(now)
        next_moments = []

        # -- alarms that come from the calendar ---------------------------
        for fire_at, occurrence, minutes in self.upcoming_alarms(now, since=since):
            if fire_at > now:
                next_moments.append(fire_at)
                continue
            if self.should_raise(fire_at, occurrence.end, now, since):
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
        self._last_check = now

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

    def upcoming_alarms(self, now: datetime, *, since: datetime | None = None
                        ) -> list[tuple[datetime, Occurrence, int]]:
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
        window_start = since if since is not None else now - STALE_AFTER
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
        self._remember(reminder)
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

        # Two things you can do without opening anything. The target is the
        # reminder's key, which is all a notification button can carry.
        try:
            minutes = settings.get_int("reminder_snooze_minutes")
            notification.add_button_with_target(
                f"Snooze {_minutes_phrase(minutes)}", "app.snooze-reminder",
                GLib.Variant("s", reminder.key))
            notification.add_button_with_target(
                "Open", "app.open-reminder", GLib.Variant("s", reminder.key))
        except Exception as exc:
            log.debug("could not add notification buttons: %s", exc)

        # A stable id means a repeated reminder replaces its predecessor in
        # the shell rather than stacking up.
        self.application.send_notification(self.notification_id(reminder), notification)
        log.info("reminder shown for “%s”", reminder.summary)

    # -- a notification's buttons ---------------------------------------

    #: How many shown reminders to keep findable by key.
    RECENT_LIMIT = 50

    def _remember(self, reminder: PendingReminder) -> None:
        self._recent[reminder.key] = reminder
        while len(self._recent) > self.RECENT_LIMIT:
            self._recent.pop(next(iter(self._recent)))

    def reminder_by_key(self, key: str) -> PendingReminder | None:
        """The reminder a notification button refers to, if still held."""
        return self._recent.get(key) or self._snoozed.get(key)

    @staticmethod
    def notification_id(reminder: PendingReminder) -> str:
        return f"kairos-{reminder.uid}-{int(reminder.start.timestamp())}"

    def withdraw(self, reminder: PendingReminder) -> None:
        """Take a reminder's notification off screen once it has been dealt with."""
        if self.application is None:
            return
        try:
            self.application.withdraw_notification(self.notification_id(reminder))
        except Exception as exc:
            log.debug("could not withdraw the notification: %s", exc)

    # -- a machine that was asleep ---------------------------------------

    def _missed_since(self, now: datetime) -> datetime:
        """How far back a due reminder still counts as worth showing.

        Normally half an hour. After a sleep it reaches back to the last time
        anything was checked — so a reminder that came due while the lid was
        shut is shown on waking instead of being quietly marked as done — but
        never further back than a day.
        """
        recent = now - STALE_AFTER
        if self._last_check is None:
            return recent
        return max(now - MISSED_WHILE_ASLEEP_LIMIT, min(recent, self._last_check))

    @staticmethod
    def should_raise(fire_at: datetime, event_end: datetime, now: datetime,
                     since: datetime) -> bool:
        """Whether a reminder that is already due should still be shown.

        Anything that came due in the last half hour is shown, as before.
        Something older — missed while the machine slept — is shown only if
        its event has not finished: finding a reminder on waking for a
        meeting that is already over is noise, not help.
        """
        if fire_at < since:
            return False
        if now - fire_at <= STALE_AFTER:
            return True
        return event_end > now

    def _watch_for_sleep(self) -> None:
        """Reschedule the moment the machine wakes.

        The timer counts only time the machine is awake, so on its own it
        would notice a reminder that came due during a sleep up to fifteen
        minutes after waking. logind announces sleep and wake on the system
        bus; the once-a-minute clock check covers anywhere it does not.
        """
        try:
            self._system_bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        except GLib.Error as exc:
            log.debug("no system bus, relying on the clock check: %s", exc.message)
            return
        self._sleep_subscription = self._system_bus.signal_subscribe(
            "org.freedesktop.login1", "org.freedesktop.login1.Manager",
            "PrepareForSleep", "/org/freedesktop/login1", None,
            Gio.DBusSignalFlags.NONE, self._on_prepare_for_sleep)

    def _unwatch_sleep(self) -> None:
        if self._system_bus is not None and self._sleep_subscription:
            self._system_bus.signal_unsubscribe(self._sleep_subscription)
        self._sleep_subscription = 0

    def _on_prepare_for_sleep(self, *args) -> None:
        going_to_sleep = args[5].unpack()[0]
        if going_to_sleep:
            self._save_history()
            self._save_snoozes()
            return
        log.info("the machine woke up; checking reminders")
        self.reschedule()

    def _start_heartbeat(self) -> None:
        self._heartbeat_wall = time.time()
        self._heartbeat_awake = time.monotonic()
        self._heartbeat_id = GLib.timeout_add_seconds(HEARTBEAT_SECONDS, self._on_heartbeat)

    def _stop_heartbeat(self) -> None:
        if self._heartbeat_id:
            GLib.source_remove(self._heartbeat_id)
            self._heartbeat_id = 0

    def _on_heartbeat(self) -> bool:
        """Once a minute: did more real time pass than the process was awake for?"""
        wall, awake = time.time(), time.monotonic()
        if clock_jumped(wall - self._heartbeat_wall, awake - self._heartbeat_awake):
            log.info("the clock jumped (a sleep, or a changed clock); checking reminders")
            self.reschedule()
        self._heartbeat_wall, self._heartbeat_awake = wall, awake
        return GLib.SOURCE_CONTINUE

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
