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
from datetime import datetime, timedelta

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, GObject  # noqa: E402

from kairos.accounts import AccountStore
from kairos.backends import AuthenticationError, BackendError, backend_for
from kairos.backends.local import LOCAL_ACCOUNT_ID, default_calendar
from kairos.config import settings
from kairos.models import Calendar, Event, local_timezone
from kairos.storage import PENDING_DELETE, PENDING_SAVE, Storage

log = logging.getLogger(__name__)


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
