"""Keeping the local cache and the remote servers in step.

Everything here exists to protect one invariant: **the UI thread never blocks
on the network**.  Views read from :mod:`kairos.storage`, which is local and
fast.  This module runs the slow parts on a worker thread and reports back
through GObject signals, which GTK delivers on the main thread.

The flow for a sync is:

1. push anything the user changed while offline (``pending`` rows);
2. for each account, ask which calendars exist;
3. for each calendar whose change-token moved, download the window of events
   we care about and replace the cached copy.

A write the user makes goes to the cache *immediately* and to the server in
the background, so the calendar redraws instantly and still works on a train.
If the push fails the row stays marked pending and the next sync retries it.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import replace
from datetime import datetime, timedelta

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, GObject  # noqa: E402

from kairos.accounts import AccountStore
from kairos.backends import AuthenticationError, BackendError, backend_for
from kairos.backends.local import LOCAL_ACCOUNT_ID, default_calendar
from kairos.config import settings
from kairos.models import Calendar, Event, Occurrence, local_timezone
from kairos.storage import PENDING_DELETE, PENDING_SAVE, Storage

log = logging.getLogger(__name__)


def _with_count(rule: str, count: int) -> str:
    """An RRULE limited to ``count`` occurrences.

    UNTIL is dropped along with any existing COUNT: a rule may carry one or
    the other, never both.
    """
    parts = [part for part in rule.split(";")
             if part and part.split("=")[0].strip().upper() not in ("COUNT", "UNTIL")]
    parts.append(f"COUNT={count}")
    return ";".join(parts)


class SyncManager(GObject.Object):
    """Owns the cache, the accounts and the background worker.

    Signals (all delivered on the GTK main thread):

    ``sync-started()``
        A sync run has begun; show the spinner.
    ``sync-finished(bool ok, str message)``
        It ended.  ``message`` is empty on success, otherwise a sentence for
        the user.
    ``calendars-changed()``
        A calendar was added, removed, renamed or recoloured.
    ``events-changed()``
        Cached events changed and any visible view should redraw.
    ``auth-failed(str account_id, str message)``
        A server rejected our credentials; the UI re-prompts.
    """

    __gsignals__ = {
        "sync-started": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "sync-finished": (GObject.SignalFlags.RUN_FIRST, None, (bool, str)),
        "calendars-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "events-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "auth-failed": (GObject.SignalFlags.RUN_FIRST, None, (str, str)),
    }

    def __init__(self, store: Storage | None = None, accounts: AccountStore | None = None) -> None:
        super().__init__()
        self.storage = store or Storage()
        self.accounts = accounts or AccountStore()
        self._worker: threading.Thread | None = None
        self._timer_id: int = 0
        self._ensure_default_calendar()

    # ------------------------------------------------------------------
    # Start-up and shutdown
    # ------------------------------------------------------------------

    def _ensure_default_calendar(self) -> None:
        """Guarantee there is somewhere to create an event on first run."""
        if not self.storage.list_calendars():
            self.storage.save_calendar(default_calendar())

    def start(self) -> None:
        """Begin periodic syncing, and sync once now if configured to."""
        self._schedule_next_sync()
        if settings.get_bool("sync_on_startup"):
            # Let the window finish drawing before we hit the network.
            GLib.timeout_add_seconds(2, lambda: (self.sync_now(), False)[1])

    def stop(self) -> None:
        if self._timer_id:
            GLib.source_remove(self._timer_id)
            self._timer_id = 0

    def _schedule_next_sync(self) -> None:
        if self._timer_id:
            GLib.source_remove(self._timer_id)
            self._timer_id = 0
        minutes = settings.get_int("sync_interval_minutes")
        if minutes <= 0:
            return  # Automatic syncing is switched off.
        self._timer_id = GLib.timeout_add_seconds(minutes * 60, self._on_timer)

    def _on_timer(self) -> bool:
        self.sync_now()
        return GLib.SOURCE_CONTINUE

    def reschedule(self) -> None:
        """Called when the sync interval preference changes."""
        self._schedule_next_sync()

    # ------------------------------------------------------------------
    # Reading (main thread, from the cache)
    # ------------------------------------------------------------------

    def visible_calendars(self) -> list[Calendar]:
        return [c for c in self.storage.list_calendars() if c.visible]

    def calendars(self) -> list[Calendar]:
        return self.storage.list_calendars()

    def writable_calendars(self) -> list[Calendar]:
        return [c for c in self.storage.list_calendars() if c.writable]

    def events_between(self, start: datetime, end: datetime) -> list[Event]:
        ids = [c.id for c in self.visible_calendars()]
        return self.storage.events_in_range(ids, start, end)

    def all_events_between(self, start: datetime, end: datetime) -> list[Event]:
        """Ignores visibility — used by the notification scheduler."""
        ids = [c.id for c in self.storage.list_calendars()]
        return self.storage.events_in_range(ids, start, end)

    def search(self, text: str) -> list[Event]:
        ids = [c.id for c in self.visible_calendars()]
        return self.storage.search_events(text, ids)

    # ------------------------------------------------------------------
    # Writing (cache first, server second)
    # ------------------------------------------------------------------

    def save_event(self, event: Event) -> None:
        """Store an event locally and push it in the background."""
        calendar = self.storage.get_calendar(event.calendar_id)
        if calendar is None:
            log.error("refusing to save event into unknown calendar %s", event.calendar_id)
            return

        # Regenerate the iCalendar text so the cache matches the edit.
        from kairos import ical
        event.raw_ics = ical.to_ical_text(event)

        needs_push = not calendar.is_local
        self.storage.save_event(event, pending=PENDING_SAVE if needs_push else 0)
        self.emit("events-changed")
        if needs_push:
            self._run_in_background(self._push_pending, "saving your change")

    def delete_event(self, event: Event) -> None:
        """Remove an event locally and push the deletion in the background."""
        calendar = self.storage.get_calendar(event.calendar_id)
        if calendar is None:
            return

        if calendar.is_local or not event.href:
            self.storage.forget_event(event.calendar_id, event.uid)
            self.emit("events-changed")
            return

        self.storage.mark_event_deleted(event.calendar_id, event.uid)
        self.emit("events-changed")
        self._run_in_background(self._push_pending, "deleting the event")

    # ------------------------------------------------------------------
    # One occurrence of a repeating series
    # ------------------------------------------------------------------
    #
    # A repeating event lives in one resource on the server: a master VEVENT
    # plus an override per changed instance.  Changing a single occurrence
    # therefore rewrites that resource rather than writing a new one, and it
    # must not go through :meth:`save_event`, which regenerates the whole
    # document from the master and would throw every override away.

    #: What an edit or a delete applies to.
    THIS_EVENT = "this"
    THIS_AND_FOLLOWING = "following"
    ALL_EVENTS = "all"

    def save_occurrence(self, occurrence: Occurrence, edited: Event, *,
                        scope: str) -> None:
        """Apply an edit to one occurrence, the rest of the series, or all."""
        original = occurrence.event
        if scope == self.ALL_EVENTS or not self._is_one_of_a_series(occurrence):
            self.save_event(edited)
            return

        from kairos import ical
        if scope == self.THIS_AND_FOLLOWING:
            self._split_series(occurrence, edited)
            return

        try:
            text = ical.override_occurrence(
                original.raw_ics or ical.to_ical_text(original),
                occurrence.recurrence_id, edited)
        except ical.ParseError as exc:
            # Better to change the series than to lose the user's edit.
            log.warning("cannot write an override for %s (%s); "
                        "saving the whole series instead", original.uid, exc)
            self.save_event(edited)
            return
        self._save_series_text(original, text)

    def delete_occurrence(self, occurrence: Occurrence, *, scope: str) -> None:
        """Remove one occurrence, the rest of the series, or all of it."""
        original = occurrence.event
        if scope == self.ALL_EVENTS or not self._is_one_of_a_series(occurrence):
            self.delete_event(original)
            return

        from kairos import ical
        if scope == self.THIS_AND_FOLLOWING:
            # Deleting the rest is the same as ending the series here, with
            # no new event to carry the far side.
            succeeded, _ = self._truncate(occurrence)
            if not succeeded:
                self.delete_event(original)
            return

        try:
            text = ical.exclude_occurrence(
                original.raw_ics or ical.to_ical_text(original),
                occurrence.recurrence_id)
        except ical.ParseError as exc:
            log.warning("cannot exclude an occurrence of %s (%s); "
                        "deleting the series instead", original.uid, exc)
            self.delete_event(original)
            return
        self._save_series_text(original, text)

    def _truncate(self, occurrence: Occurrence) -> tuple[bool, int | None]:
        """End the series just before ``occurrence``.

        Returns ``(succeeded, kept)``. ``kept`` is how many occurrences the
        truncated series holds when the rule is limited by COUNT, and
        ``None`` when it is not — the two are different answers, and running
        them together once meant an ordinary weekly series was truncated and
        then deleted outright.
        """
        from kairos import ical, recurrence
        original = occurrence.event
        text = original.raw_ics or ical.to_ical_text(original)

        keep = None
        total = recurrence.counted_occurrences(original)
        if total is not None:
            # COUNT and UNTIL may not both appear, so a counted series is
            # divided by count rather than ended by date.
            keep = recurrence.occurrences_before(original,
                                                 occurrence.recurrence_id)
        try:
            truncated = ical.truncate_series(text, occurrence.recurrence_id,
                                             keep_count=keep)
        except ical.ParseError as exc:
            log.warning("cannot split %s (%s)", original.uid, exc)
            return False, None

        if keep == 0 or occurrence.recurrence_id <= original.start:
            # Nothing is left on this side: the split is at the very first
            # occurrence, so "this and following" means the whole series.
            self.delete_event(original)
            return True, 0

        self._save_series_text(original, truncated)
        return True, keep

    def _split_series(self, occurrence: Occurrence, edited: Event) -> None:
        """End the series before this occurrence and start a new one here.

        The far side is a separate event with its own UID, which is what
        every other client does and what makes it a resource the server can
        hold independently.
        """
        from kairos import recurrence
        from kairos.models import new_uid

        original = occurrence.event
        total = recurrence.counted_occurrences(original)
        succeeded, kept = self._truncate(occurrence)
        if not succeeded:
            log.warning("could not split %s; changing the whole series",
                        original.uid)
            self.save_event(edited)
            return
        if kept == 0:
            # The whole series moved; save the edit as the series itself.
            self.save_event(replace(edited, raw_ics=""))
            return

        remaining = None
        if total is not None and kept is not None:
            remaining = max(1, total - kept)

        # The new series begins at the occurrence that was split at, not at
        # the old series' start — and carries whatever the user moved the
        # times by in the editor, which is the difference between the edit
        # and the master it was opened on.
        shift = edited.start - original.start
        start = occurrence.start + shift
        end = start + (edited.end - edited.start)

        rule = edited.rrule or original.rrule
        if remaining is not None:
            rule = _with_count(rule, remaining)

        self.save_event(replace(
            edited,
            uid=new_uid(),
            href=None,
            etag=None,
            sequence=0,
            raw_ics="",
            start=start,
            end=end,
            rrule=rule,
        ))

    def restore_event(self, event: Event) -> None:
        """Put back an event, or a series, exactly as it was before a delete.

        Deleting is the one thing here that loses work, so it is undoable.
        The event carries the iCalendar it had beforehand, which is what makes
        one undo cover both cases: a whole event comes back, and a series
        comes back without the EXDATE that removed one occurrence from it.

        If the deletion already reached the server the resource is gone, and
        this writes it again — the conditional PUT fails, and the backend
        falls back to an unconditional one, which is the behaviour that
        exists so a user's change is never the thing that gets dropped.
        """
        from kairos import ical
        self._save_series_text(event, event.raw_ics or ical.to_ical_text(event))

    @staticmethod
    def _is_one_of_a_series(occurrence: Occurrence) -> bool:
        """Whether "just this one" is even a meaningful choice here."""
        return (occurrence.recurrence_id is not None
                and occurrence.event.is_recurring)

    def _save_series_text(self, event: Event, text: str) -> None:
        """Store a series' iCalendar exactly as given.

        Unlike :meth:`save_event` this does not rebuild the document from the
        event, because the document is the point: it carries the EXDATEs and
        overrides that the :class:`Event` itself has no room for.
        """
        calendar = self.storage.get_calendar(event.calendar_id)
        if calendar is None:
            log.error("refusing to save into unknown calendar %s", event.calendar_id)
            return

        needs_push = not calendar.is_local
        self.storage.save_event(replace(event, raw_ics=text),
                                pending=PENDING_SAVE if needs_push else 0)
        self.emit("events-changed")
        if needs_push:
            self._run_in_background(self._push_pending, "saving your change")

    # ------------------------------------------------------------------
    # Calendars
    # ------------------------------------------------------------------

    def set_calendar_visible(self, calendar: Calendar, visible: bool) -> None:
        calendar.visible = visible
        self.storage.save_calendar(calendar)
        self.emit("events-changed")

    def update_calendar(self, calendar: Calendar) -> None:
        self.storage.save_calendar(calendar)
        self.emit("calendars-changed")
        self.emit("events-changed")

    def add_local_calendar(self, name: str, colour: str) -> Calendar:
        import uuid
        calendar = Calendar(
            id="local-" + uuid.uuid4().hex[:12],
            account_id=LOCAL_ACCOUNT_ID,
            name=name or "New calendar",
            colour=colour,
        )
        self.storage.save_calendar(calendar)
        self.emit("calendars-changed")
        return calendar

    def remove_calendar(self, calendar: Calendar) -> None:
        self.storage.delete_calendar(calendar.id)
        self.emit("calendars-changed")
        self.emit("events-changed")

    def remove_account(self, account_id: str) -> None:
        self.storage.delete_calendars_for_account(account_id)
        self.accounts.remove(account_id)
        self.emit("calendars-changed")
        self.emit("events-changed")

    # ------------------------------------------------------------------
    # The worker thread
    # ------------------------------------------------------------------

    @property
    def busy(self) -> bool:
        return self._worker is not None and self._worker.is_alive()

    def sync_now(self) -> None:
        """Kick off a full sync, unless one is already running."""
        self._run_in_background(self._full_sync, "syncing")

    def _run_in_background(self, work, description: str) -> None:
        if self.busy:
            return  # One sync at a time is plenty; the next timer tick retries.
        self.emit("sync-started")

        def runner() -> None:
            ok, message = True, ""
            try:
                work()
            except AuthenticationError as exc:
                ok, message = False, str(exc)
            except BackendError as exc:
                ok, message = False, str(exc)
            except Exception:  # never let the worker die silently
                log.exception("unexpected failure while %s", description)
                ok, message = False, f"Something went wrong while {description}."
            GLib.idle_add(self._finish, ok, message)

        self._worker = threading.Thread(target=runner, name="kairos-sync", daemon=True)
        self._worker.start()

    def _finish(self, ok: bool, message: str) -> bool:
        self.emit("events-changed")
        self.emit("calendars-changed")
        self.emit("sync-finished", ok, message)
        return GLib.SOURCE_REMOVE

    # -- the actual work --------------------------------------------------

    def _sync_window(self) -> tuple[datetime, datetime]:
        """How much of the calendar we keep cached."""
        now = datetime.now(tz=local_timezone())
        return (
            now - timedelta(days=settings.get_int("sync_window_past_days")),
            now + timedelta(days=settings.get_int("sync_window_future_days")),
        )

    def _backend_for_account(self, account):
        return backend_for(account)

    def _push_pending(self) -> None:
        """Send every locally-changed event to its server.

        Runs on the worker thread.  Failures for one account do not stop the
        others; the first error is re-raised at the end so the user sees it.
        """
        first_error: Exception | None = None
        backends: dict[str, object] = {}

        for event, pending in self.storage.pending_changes():
            calendar = self.storage.get_calendar(event.calendar_id)
            if calendar is None or calendar.is_local:
                self.storage.clear_pending(event.calendar_id, event.uid)
                continue

            account = self.accounts.get(calendar.account_id)
            if account is None or not account.enabled:
                continue

            try:
                if account.id not in backends:
                    backends[account.id] = self._backend_for_account(account)
                backend = backends[account.id]

                if pending == PENDING_DELETE:
                    backend.delete_event(calendar, event)
                    self.storage.forget_event(event.calendar_id, event.uid)
                else:
                    saved = backend.save_event(calendar, event)
                    self.storage.save_event(saved, pending=0)
            except AuthenticationError as exc:
                GLib.idle_add(self.emit, "auth-failed", account.id, str(exc))
                first_error = first_error or exc
            except BackendError as exc:
                log.warning("could not push %s: %s", event.uid, exc)
                first_error = first_error or exc

        if first_error is not None:
            raise first_error

    def _full_sync(self) -> None:
        """Push local changes, then refresh every calendar we know about."""
        self._push_pending()

        first_error: Exception | None = None
        window_start, window_end = self._sync_window()

        for account in self.accounts.remote():
            if not account.enabled:
                continue
            try:
                backend = self._backend_for_account(account)
                self._sync_account(backend, account, window_start, window_end)
            except AuthenticationError as exc:
                GLib.idle_add(self.emit, "auth-failed", account.id, str(exc))
                first_error = first_error or exc
            except BackendError as exc:
                log.warning("sync failed for %s: %s", account.name, exc)
                first_error = first_error or exc

        if first_error is not None:
            raise first_error

    def _sync_account(self, backend, account, window_start, window_end) -> None:
        """Refresh one account's calendars.  Worker thread."""
        discovered = backend.discover_calendars()
        known = {c.id: c for c in self.storage.list_calendars() if c.account_id == account.id}

        for calendar in discovered:
            existing = known.pop(calendar.id, None)
            if existing is not None:
                # Keep the user's own choices; take everything else from the
                # server.  Renaming a calendar in Kairos is deliberate, so we
                # do not let the server overwrite it back.
                calendar.visible = existing.visible
                calendar.colour = existing.colour if existing.colour else calendar.colour
                if existing.name:
                    calendar.name = existing.name
                unchanged = bool(calendar.sync_token) and calendar.sync_token == existing.sync_token
            else:
                unchanged = False

            self.storage.save_calendar(calendar)
            if unchanged:
                log.debug("“%s” is unchanged; skipping download", calendar.name)
                continue

            events = backend.fetch_events(calendar, window_start, window_end)
            self.storage.replace_calendar_events(calendar.id, events)
            log.info("synced “%s”: %d events", calendar.name, len(events))

        # Calendars that vanished from the server are dropped locally too.
        for stale in known.values():
            log.info("calendar “%s” is gone from the server; removing it", stale.name)
            self.storage.delete_calendar(stale.id)
