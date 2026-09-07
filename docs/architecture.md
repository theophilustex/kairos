# Architecture

Kairos is about 7,000 lines of Python including comments. This is the map.

- [Two invariants](#two-invariants)
- [The modules](#the-modules)
- [How a screen gets drawn](#how-a-screen-gets-drawn)
- [How a change gets saved](#how-a-change-gets-saved)
- [How a reminder fires](#how-a-reminder-fires)
- [Backends](#backends)
- [Adding things](#adding-things)

---

## Two invariants

Almost every design decision follows from these. Break either and things go
wrong in ways that are hard to see.

### 1. Every datetime is timezone-aware

There are no naive datetimes anywhere in Kairos. Beyond that:

- **All-day events are not `date` objects.** They are aware datetimes at local
  midnight with `all_day = True` and an *exclusive* end. A one-day event on the
  5th runs from `05 00:00` to `06 00:00`. That matches iCalendar's own
  convention, and it means view code never has to ask "is this a date or a
  datetime?".
- **`Event` keeps whatever zone the server used** — usually UTC.
- **`Occurrence` is always local.** `kairos.recurrence` converts. This matters:
  a 7pm meeting in the Americas is stored at 23:00 UTC, and before the
  conversion existed the views saw a span across two UTC dates and drew it as
  an all-day banner on both. See [`tests/test_timezones.py`](../tests/test_timezones.py).

So: views deal in `Occurrence`, and can treat `.date()` as "the day cell this
belongs in" without further thought.

### 2. The UI thread never touches the network

Views read from the local SQLite cache, which is fast and always available.
`kairos.sync` does the slow work on a worker thread and reports back through
GObject signals, which GTK delivers on the main thread.

Anything that talks to a server goes through a backend, and backends are
called from the worker thread only. They are free to block.

---

## The modules

```
kairos/
  config.py         where settings live and how they load        ← start here
  security.py       URL checks, password storage, safe writes
  models.py         the data types: Account, Calendar, Event, Alarm, Occurrence
  ical.py           the only module that knows what a VEVENT is
  storage.py        the SQLite cache and search index; the only SQL
  recurrence.py     repeat rules → concrete occurrences, in local time
  accounts.py       the account list (accounts.json)
  backends/
    base.py           the five-method interface a calendar source implements
    caldav_backend.py the only module that touches the network
    local.py          calendars that live only on this machine
  sync.py           the worker thread; owns the cache and the accounts
  notifications.py  planning and firing reminders; PendingReminder
  tray.py           the taskbar icon, spoken over D-Bus by hand
  autostart.py      the login .desktop file
  formatting.py     every user-visible date string
  theming.py        stylesheets, accent colour, icon search path
  app.py            the application: startup, actions, shortcuts, lifecycle
  ui/
    window.py         the main window; the only place that knows all the views
    month_view.py     the month grid
    week_view.py      the week and day grids (one class, different day counts)
    agenda_view.py    the scrolling list
    event_editor.py   the create/edit dialog
    event_popover.py  the detail bubble
    reminder_alert.py the window a due reminder raises
    calendar_manager.py / account_dialog.py / preferences.py
    widgets.py        shared pieces: date and time buttons, swipe, swatches
    style.css         the built-in stylesheet
```

Dependencies run one way: `ui/` depends on the layer below it, and nothing in
the lower layer imports from `ui/`. The one place they meet is `app.py`, which
wires the reminder scheduler to the alert window — deliberately, so that
`notifications.py` does not need to know what a window is.

---

## How a screen gets drawn

```
view.refresh()
  └─ sync.events_between(start, end)        ← visible calendars only
       └─ storage.events_in_range(...)      ← SQLite; non-recurring filtered
                                              by date, recurring always returned
  └─ recurrence.expand(events, start, end)  ← rules → Occurrences, in local time
  └─ recurrence.group_by_day(...)           ← bucketed per day cell
  └─ ... build widgets ...
```

Nothing here can block. `storage` keeps a parse cache keyed by
`(calendar, uid, etag)` so paging back to a month you have already seen does
not re-parse its iCalendar.

Recurring events are always returned by the range query because only the
recurrence code can say whether a rule produces an occurrence in the window —
and there are rarely enough of them for it to matter.

---

## How a change gets saved

```
editor emits "saved"
  └─ sync.save_event(event)
       ├─ storage.save_event(event, pending=SAVE)   ← immediate, local
       ├─ emit "events-changed"                     ← the view redraws now
       └─ worker thread: _push_pending()
            └─ backend.save_event(calendar, event)  ← conditional PUT
                 └─ storage.save_event(saved, pending=NONE)
```

The screen updates before the network is touched, which is why Kairos feels
instant and why it works offline. A push that fails leaves the row marked
pending, and the next sync retries it.

`replace_calendar_events()` — the full refresh from a server — deliberately
leaves pending rows alone. Otherwise a refresh would silently discard an edit
you had not managed to send.

---

## How a reminder fires

`AlarmScheduler` does not poll. It builds the list of alarms due in the next
hour or so, sleeps until the earliest, fires it, and rebuilds. A thousand
events cost one timer.

```
reschedule()
  ├─ upcoming_alarms(now)          ← expand events, subtract each alarm offset
  │    ├─ due now  → _raise()
  │    └─ later    → candidate for the next wake-up
  ├─ snoozed reminders             ← same treatment, from their own file
  └─ arm one timer for the earliest
```

`_raise()` sends the desktop notification and calls `on_alert`, which `app.py`
points at the alert window.

Two files keep state: `fired_alarms.json`, so restarting does not replay this
morning's reminders, and `snoozed_reminders.json`, which carries enough of the
event to be shown again later without looking it up — the event may have been
edited by then.

---

## Backends

A backend is five methods (`kairos/backends/base.py`):

```python
discover_calendars() -> list[Calendar]
fetch_events(calendar, start, end) -> list[Event]
save_event(calendar, event) -> Event
delete_event(calendar, event) -> None
check_connection() -> None
```

Everything they raise is a `BackendError` whose message is fit to show a user,
so the sync worker and the UI never have to know what a `PropfindError` is.

`CalDAVBackend` issues its own conditional PUTs rather than going through
caldav's `save()`, because that is the only way to send `If-Match` and
`If-None-Match`. ETags are fetched with one extra PROPFIND per calendar per
sync, because the calendar-query report returns event data but not ETags.

`LocalBackend` agrees with everything and stores nothing extra: the SQLite
cache *is* the storage. It exists so local calendars take the same code path
as remote ones, which keeps `if calendar.is_local` out of the sync worker and
the UI.

---

## Adding things

| To add | Change |
|---|---|
| A preference | One entry in `DEFAULTS` in `config.py`, one row in `ui/preferences.py`. |
| A view | A widget with `set_date()`, `refresh()`, a `heading` property, `go_previous()`/`go_next()`, and the four standard signals; then one line in `CalendarWindow.VIEWS`. |
| A repeat option | One tuple in `REPEAT_PRESETS` in `ical.py`. |
| A reminder preset | One tuple in `Alarm.PRESETS` in `models.py`. |
| A keyboard shortcut | One line in `SHORTCUTS` in `app.py`. |
| An icon-only button | Wrap it in `describe()` from `ui/widgets.py`, or `test_accessibility.py` fails. |
| A tray menu entry | One `MenuItem` in `_start_tray()` in `app.py`. |
| Another protocol | A class implementing `backends/base.py`, and a case in `backend_for()`. |

### The view contract

Each view is a `Gtk.Widget` that:

- has `set_date(day)`, `refresh()`, `go_previous()`, `go_next()`
- has `heading` and `selected_day` properties
- emits `event-activated(occurrence, widget)` — the widget is the chip that
  was clicked, so the detail popover can point at it
- emits `create-requested(datetime)`, `day-activated(date)`,
  `date-selected(date)`

`window.py` connects those and knows nothing else about any view.

---

## Notes on some choices

**Why is search a separate index rather than a query over the events?**
Because the events are stored as iCalendar text, and the fields a person
searches are buried in it. Matching the raw text matches everything — every
event carries `CALSCALE:GREGORIAN` — so the old search parsed each candidate
to check properly, could not use an index, and gave up after a fixed number
of rows *without telling anyone*. `events_fts` is an FTS5 index over the
summary, location and notes, maintained by SQL triggers rather than by
Python: a write path that forgot to update it would make events silently
unfindable, and there is no way to forget a trigger.

Two things about it are worth keeping in mind. The matches are gathered in a
**subquery**, not a join — written as a join, SQLite drives the query from
`events` and rescans the entire index once per row, which measured 80ms
against 1ms on five thousand events. And words match by **prefix**, which is
what an index can answer quickly; matching the middle of a word would mean
going back to reading every event.

**Why no `.ui` files or GResource?** Building widgets in Python keeps the
whole definition of a screen in one file, and removes a compile step from the
build. For an app this size the tradeoff is worth it.

**Why hand-written D-Bus for the tray?** The usual libraries
(`libayatana-appindicator`, `XApp`) are built against GTK 3, and GTK 3 and
GTK 4 cannot be loaded into the same process. `tray.py` therefore speaks
`org.kde.StatusNotifierItem` and `com.canonical.dbusmenu` directly. It is
about 400 lines and adds no dependency.

The cost of that choice is that the interface XML has to be *complete*.
`com.canonical.dbusmenu` has singular and batched forms of two methods —
`Event`/`EventGroup` and `AboutToShow`/`AboutToShowGroup` — and libdbusmenu,
the client behind most panels, uses the batched ones whenever the server
reports version 3. Omitting one is invisible: GDBus rejects the call before
the handler runs, libdbusmenu discards the error, and the menu draws perfectly
while doing nothing. `tests/test_background.py` therefore asserts on the
*declaration*, not just the handler.

**Why is the week grid made of rows rather than pixels?** Each day is a
`Gtk.Grid` of fifteen-minute rows, and an event is attached spanning the rows
it covers, so GTK does the layout. The only pixel value involved is the height
of a row. Overlapping events are packed into columns by `assign_columns()`,
which is the standard sweep algorithm in about twenty lines.

**The three rows of the week view must share one layout rule.** The day
headings, the all-day strip and the timed grid are separate widgets stacked
vertically, and nothing in GTK makes their columns agree — that is the
view's job. All three are built the same way: the hour gutter in a size
group, then a container that divides the rest into equal day columns.

Getting this wrong is easy and looks like a different bug entirely. The
strip and the headings were once plain `Gtk.Grid`s sized to their contents,
so a day holding a long banner claimed more width than its neighbours and
shoved every later day sideways — by half a column in the worst case. The
banners then sat over the wrong days, which reads as an event "bleeding"
into days it has nothing to do with. `tests/test_layout.py` pins the spans;
the alignment itself is structural, and stays correct as long as the day
columns keep being shared out equally in all three rows.

Multi-day banners are attached **once**, spanning their days, and packed
into lines by `assign_banner_rows()` — widest first, each taking the lowest
line free across its whole span, so a bar never steps down mid-week.
