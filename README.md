# Kairos

A lightweight, customisable calendar for Linux, written in Python with GTK 4
and libadwaita. It talks to any CalDAV/WebDAV server — Nextcloud, Radicale,
Fastmail, Posteo, mailbox.org, your own box — and reads and writes your events
there.

It is meant to feel like GNOME Calendar, but to be small enough that you can
read the whole thing in an afternoon and change the bits you disagree with.

* **Free software** — GPL-3.0-or-later, no telemetry, no accounts, no network
  traffic except to the calendar servers you configure.
* **Read *and* write CalDAV/WebDAV**, with proper conditional writes.
* **Works offline.** Everything is drawn from a local cache; changes queue up
  and go out on the next sync.
* **Desktop reminders** driven by each event's own alarm settings.
* **Customisable** through a plain JSON settings file and your own CSS.
* **Light.** About 7,000 lines of heavily commented Python (5,300 of actual
  code), four runtime dependencies, one SQLite file, and a single timer for
  all your reminders.
* **Runs as an AppImage** if you would rather not install anything.

---

## Contents

- [Installing](#installing)
- [Running it](#running-it)
- [Adding a calendar](#adding-a-calendar)
- [Customising it](#customising-it)
- [Keyboard shortcuts](#keyboard-shortcuts)
- [Building an AppImage](#building-an-appimage)
- [How the code is laid out](#how-the-code-is-laid-out)
- [Security](#security)
- [Running the tests](#running-the-tests)
- [Known limitations](#known-limitations)
- [Licence](#licence)

---

## Installing

Kairos needs **Python 3.10+**, **GTK 4** and **libadwaita 1**.

PyGObject has to come from your distribution, because it is built against the
exact GTK on your system. Everything else comes from pip.

**Debian / Ubuntu / Linux Mint**

```sh
sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-4.0 gir1.2-adw-1 \
                 gnome-keyring
pip install -r requirements.txt
```

**Fedora**

```sh
sudo dnf install python3-gobject gtk4 libadwaita gnome-keyring
pip install -r requirements.txt
```

**Arch**

```sh
sudo pacman -S python-gobject gtk4 libadwaita gnome-keyring
pip install -r requirements.txt
```

Then check everything is in place and install it for your user:

```sh
make check-deps
make install          # into ~/.local; override with PREFIX=/usr/local
```

`make uninstall` takes it away again and leaves your calendars alone.

## Running it

```sh
kairos                # once installed
./run.py              # straight from a checkout, nothing installed
python3 -m kairos     # the same thing
kairos --debug        # with verbose logging, when something misbehaves
kairos --new-event    # straight into the new-event dialog
```

## Adding a calendar

Open the sidebar's list button, or press <kbd>Ctrl</kbd>+<kbd>L</kbd>, then
**Connect a CalDAV / WebDAV account**. You need the server address, your
username and your password.

Common addresses:

| Provider   | Address |
|------------|---------|
| Nextcloud  | `https://your-server/remote.php/dav/` |
| Radicale   | `https://your-server/` |
| Fastmail   | `https://caldav.fastmail.com/dav/` |
| Posteo     | `https://posteo.de:8443/` |
| mailbox.org| `https://dav.mailbox.org/` |

Press **Connect**, tick the calendars you want, press **Add**. Kairos
discovers everything the account offers; you can hide, recolour and rename
them afterwards without affecting the server.

Calendars stored **on this computer** need no server at all — use *New
calendar on this computer* in the same menu.

## Customising it

This is the part Kairos takes seriously. There are three levels, and none of
them involve recompiling anything.

### 1. Preferences

<kbd>Ctrl</kbd>+<kbd>,</kbd>. First day of the week, 12/24-hour clock, accent
colour, text size, compact spacing, hour height, how many events a day cell
shows, sync interval, how much history to keep, and so on.

### 2. The settings file

Everything the preferences dialog writes lives in a file you can edit
yourself:

```
~/.config/kairos/settings.json
```

It is plain JSON with one key per preference. A few are only reachable by
editing the file — `week_view_start_hour`, for instance. If you write
something Kairos does not understand, it falls back to the default rather
than refusing to start, and keys it does not recognise are preserved.

The full list with defaults is at the top of
[`kairos/config.py`](kairos/config.py) — adding a new preference means adding
one line there and one row in `kairos/ui/preferences.py`.

### 3. Your own stylesheet

```
~/.config/kairos/custom.css
```

Loaded last, so anything in it wins. **It is reloaded the moment you save
it** — no restart. The file Kairos writes on first run lists every class name
you can target. For example:

```css
/* Bigger, squarer event chips */
.kairos-event-chip {
    border-radius: 2px;
    font-size: 0.9em;
}

/* Make weekends obvious */
.kairos-day-cell.kairos-weekend {
    background-color: alpha(@accent_bg_color, 0.08);
}

/* A red current-time line instead of the accent colour */
.kairos-now-line { background-color: #e01b24; }
```

The stable class names are `kairos-day-cell` (plus `.today`, `.selected`,
`.outside`), `kairos-day-number`, `kairos-event-chip`, `kairos-event-block`,
`kairos-all-day-chip`, `kairos-weekday-heading`, `kairos-week-number`,
`kairos-hour-label`, `kairos-now-line`, `kairos-agenda-day`,
`kairos-calendar-dot` and `kairos-weekend`.

### Where everything lives

```
~/.config/kairos/settings.json   preferences
~/.config/kairos/accounts.json   your accounts, without passwords
~/.config/kairos/custom.css      your stylesheet
~/.cache/kairos/cache.db         the offline copy of your events
```

Passwords are the one thing not in a file — they go to your system keyring.

## Keyboard shortcuts

| | |
|---|---|
| <kbd>Ctrl</kbd>+<kbd>N</kbd> | New event |
| <kbd>Ctrl</kbd>+<kbd>T</kbd> | Go to today |
| <kbd>Ctrl</kbd>+<kbd>R</kbd> / <kbd>F5</kbd> | Sync now |
| <kbd>Ctrl</kbd>+<kbd>1…4</kbd> | Month / week / day / agenda |
| <kbd>Alt</kbd>+<kbd>←</kbd> / <kbd>→</kbd> | Previous / next period |
| <kbd>Ctrl</kbd>+<kbd>L</kbd> | Manage calendars |
| <kbd>Ctrl</kbd>+<kbd>,</kbd> | Preferences |
| <kbd>Ctrl</kbd>+<kbd>W</kbd> / <kbd>Q</kbd> | Close / quit |

They are all in one table at the top of [`kairos/app.py`](kairos/app.py) if
you want different ones.

## Building an AppImage

```sh
make appimage
```

This produces `build/Kairos-<version>-x86_64.AppImage`, around 70 MB, which
runs on any reasonably modern x86-64 Linux with nothing installed.

The script is [`packaging/build-appimage.sh`](packaging/build-appimage.sh) and
is commented step by step. Two things to know:

* Build it on the **oldest** distribution you want to support. glibc is not
  bundled — it cannot safely be — so an AppImage built on a new distribution
  will not start on an older one. Ubuntu 22.04 is a good choice.
* It checks the bundle actually starts before packing it, so a broken build
  fails loudly rather than producing an AppImage that dies on someone else's
  machine.

## How the code is laid out

Each module does one job and can be read on its own.

```
kairos/
  config.py         where settings live and how they load  (start here)
  security.py       URL checks, password storage, safe file writes
  models.py         the plain data types: Calendar, Event, Alarm, Occurrence
  ical.py           translation between models and iCalendar text
  storage.py        the SQLite cache
  recurrence.py     turning repeating events into concrete occurrences
  backends/
    base.py         the five-method interface a calendar source implements
    caldav_backend.py  the only file that imports `caldav`
    local.py        calendars that live only on this machine
  accounts.py       the account list (accounts.json)
  sync.py           the background worker that keeps cache and server in step
  notifications.py  planning and firing desktop reminders
  formatting.py     every user-visible date string
  theming.py        stylesheets, accent colour, watching custom.css
  ui/
    window.py       the main window; the only place that knows all the views
    month_view.py   the month grid
    week_view.py    the week and day grids
    agenda_view.py  the scrolling list
    event_editor.py the create/edit dialog
    ...
```

**Two conventions worth knowing before you change anything:**

1. **Every `datetime` is timezone-aware.** All-day events are stored as aware
   local-midnight datetimes with an *exclusive* end, matching iCalendar. A
   one-day event on the 5th runs `05 00:00 → 06 00:00`. This is why view code
   never has to ask "is this a date or a datetime?".

2. **The UI thread never touches the network.** Views read from the SQLite
   cache; `sync.py` does the slow work on a worker thread and reports back
   through GObject signals. If you add a feature that talks to a server, it
   goes through a backend and gets called from the worker.

### Adding things

* **A new preference** — one entry in `DEFAULTS` in `config.py`, one row in
  `ui/preferences.py`.
* **A new view** — a widget with `set_date()`, `refresh()`, a `heading`
  property and the four standard signals, plus one line in
  `CalendarWindow.VIEWS`.
* **A new repeat option** — one tuple in `REPEAT_PRESETS` in `ical.py`.
* **A new reminder offset** — one tuple in `Alarm.PRESETS` in `models.py`.
* **Another protocol** — a class implementing `backends/base.py`, plus a case
  in `backend_for()`.

## Security

The security-relevant code is deliberately gathered in
[`kairos/security.py`](kairos/security.py) so it can be reviewed in one sitting.

* **Passwords never reach a config file.** They go to the system keyring
  (GNOME Keyring, KWallet, …). If no keyring is available Kairos *says so* in
  the account dialog and keeps the password in memory for the session only,
  rather than silently writing it to disk.
* **Plain http is refused** unless you deliberately enable it *and* the server
  resolves to a loopback or private address. Turning the preference on will
  still not send your password unencrypted to a public host.
* **TLS certificates are verified** by default, per account, and the
  preference that disables it is labelled as dangerous.
* **URLs are validated** before any connection: only http/https, no embedded
  `user:password@`, no `file:`/`javascript:` sneaking through the
  "assume https" convenience.
* **Config files are written 0600 and atomically**, so a crash cannot leave a
  half-written file and other users cannot read them.
* **Server data is untrusted input.** Text fields are stripped of control
  characters and length-capped, oversized resources are skipped, and event
  text is never rendered as Pango markup.
* **Every SQL statement uses bound parameters.** There is no string
  interpolation of user or server data into SQL anywhere.
* **No `eval`, no `pickle`, no shelling out** to anything.

### Conflicts

Kairos sends `If-Match` when updating an event and `If-None-Match: *` when
creating one, so it knows when someone else has changed the same event.

When that happens, **your edit wins**: Kairos logs a warning and writes your
version anyway. That is a deliberate choice — there is no merge UI, and
throwing away what you just typed would be worse than overwriting the other
change. If you would rather it refused, the behaviour is about ten lines in
`CalDAVBackend.save_event`.

## Running the tests

```sh
make test
```

161 tests, all standard-library `unittest`, no framework to install. They
cover the iCalendar conversion, recurrence expansion, the storage layer's
offline behaviour, the security rules, the overlap-packing algorithm and the
reminder scheduler.

The CalDAV tests start a real [Radicale](https://radicale.org) server on
localhost and drive the backend against it — discovery, create, read back,
update in place, delete, ETags, a genuine 412 conflict, and the offline queue.
They skip cleanly if Radicale is not installed:

```sh
pip install radicale
```

The suite never touches your real settings, cache or keyring; `tests/__init__.py`
redirects the XDG directories into a temporary sandbox first.

## Known limitations

Stated plainly, because finding these out later is annoying:

* **Modified instances of a repeating event are ignored.** If you move just
  one occurrence of a weekly meeting in another client, Kairos shows the
  series as if you had not. Editing or deleting a repeating event in Kairos
  affects the whole series, and the delete dialog says so.
* **No task (VTODO) or journal support.** Calendars that hold only tasks are
  skipped during discovery.
* **No invitations or free/busy.** Kairos reads and writes events; it does
  not do scheduling, attendees or RSVPs.
* **No timezone editor.** Events are read in their own timezone and displayed
  in yours, which is right; but you cannot author an event *in* another
  timezone.
* **Custom repeat rules are read, not composed.** A rule the editor's presets
  do not cover is preserved untouched, and shown as "(from the server)", but
  you cannot build an arbitrary RRULE in the UI.
* **The sidebar's mini-calendar starts its week where your locale says**,
  which may differ from the main grid if you have overridden the preference.
  GTK's calendar widget has no setting for it.
* The homepage URLs in `data/org.kairos.Calendar.metainfo.xml` are
  placeholders — point them at your own repository if you fork this.

## Licence

GPL-3.0-or-later. The full text is in [LICENSE](LICENSE).

Kairos depends on [caldav](https://github.com/python-caldav/caldav),
[icalendar](https://github.com/collective/icalendar),
[recurring-ical-events](https://github.com/niccokunzmann/python-recurring-ical-events)
and [keyring](https://github.com/jaraco/keyring), all free software, plus GTK 4
and libadwaita.
