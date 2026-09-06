# Contributing

Kairos is meant to be changed. It is deliberately small and heavily commented
so that you can read the part you care about, change it, and be reasonably
confident about what you broke.

Start with [architecture](architecture.md) for the map.

---

## Getting set up

```sh
git clone <your fork>
cd Kairos
make check-deps        # tells you what is missing
pip install -r requirements.txt
pip install radicale pyflakes    # for the full test suite and the linter
./run.py
```

Running from a checkout needs no installation, and finds its icons and
stylesheet relative to the source tree.

---

## Tests

```sh
make test                                   # everything
python3 -m unittest tests.test_ical -v      # one module
python3 -m unittest tests.test_ical.AllDayEvents.test_written_as_dates_not_datetimes
```

263 tests, all standard-library `unittest`. There is no framework to install
and no configuration file.

| Module | |
|---|---|
| `test_ical.py` | iCalendar in and out, including the awkward shapes real servers send. |
| `test_recurrence.py` | Expanding repeat rules; EXDATE, COUNT, UNTIL, BYDAY. |
| `test_timezones.py` | Server times shown on the user's clock, across four zones. |
| `test_storage.py` | The cache, and the offline queue surviving a refresh. |
| `test_security.py` | URL rules, file permissions, settings validation. |
| `test_caldav.py` | A real Radicale server on localhost. |
| `test_notifications.py` | Planning reminders. |
| `test_reminder_alert.py` | The alert window and snoozing. |
| `test_background.py` | The tray protocol and starting at login. |
| `test_editor.py` | The 12-hour picker and custom reminders. |
| `test_layout.py` | Overlap packing and date formatting. |
| `test_gestures.py` | Swiping. |

`tests/__init__.py` redirects the XDG directories into a temporary sandbox and
installs an in-memory keyring, so the suite never touches your real settings,
cache or passwords. Anything that writes files should go through those.

**The CalDAV tests start a real Radicale** on localhost and drive the backend
against it — discovery, create, read back, update in place, delete, ETags, a
genuine 412 conflict, and the offline queue. They skip cleanly if Radicale is
not installed, so `make test` still works without it. They are worth keeping
that way: a mocked CalDAV server tests your idea of the protocol rather than
the protocol.

---

## Linting

```sh
make lint
```

pyflakes only — unused imports and undefined names. There is no formatter and
no style checker; match the surrounding code.

---

## House style

Nothing unusual, but a few things are consistent throughout and worth keeping:

**Comments say why, not what.** The code says what it does. A comment earns
its place by explaining a decision, a constraint, or a trap:

```python
# The hicolor theme copied from the system brought its icon-theme.cache with
# it, and GTK trusts that cache over what is actually on disk. Left alone it
# would hide the icons we just added, and the app would show a generic one.
rm -f "$APPDIR/usr/share/icons/hicolor/icon-theme.cache"
```

**Every module has a docstring** saying what it is for and what it deliberately
does not do.

**Names are words.** `occurrence`, not `occ`. `minutes_before`, not `mins`.
British spelling in prose and in identifiers that are ours (`colour`), because
that is what the codebase already uses.

**Errors that reach a user are sentences.** Backends translate every
underlying exception into a `BackendError` whose message could go straight
into a dialog — because it does.

**Failures are not silent.** If something cannot work, say so: log it, or tell
the user. The one exception is genuinely optional machinery like the tray
icon, which logs at debug level and carries on.

---

## What a good change looks like

- **One thing at a time.** A bug fix or a feature, not both.
- **A test that fails before and passes after.** For a bug, the test should
  describe the bug: `test_a_two_hour_evening_event_is_not_drawn_as_all_day`.
- **Comments where you had to think.** If it took you an hour to work out why
  the popover closed immediately, write that down.
- **The docs updated** if behaviour changed. `docs/configuration.md` lists
  every setting; `docs/user-guide.md` describes what things do.
- **Limitations stated.** If your change works except in some case, say so in
  the code and in [what Kairos does not do](user-guide.md#what-kairos-does-not-do).

---

## Places that will bite you

**Timezones.** Read [the invariants](architecture.md#two-invariants) before
touching anything with a datetime in it. All-day events are aware datetimes at
local midnight with an exclusive end, and `Occurrence` is always local.

**The UI thread.** If your code can block — anything network — it belongs in a
backend, called from the sync worker. Report results with GObject signals;
GTK delivers those on the main thread.

**GTK 3 libraries.** You cannot import anything built against GTK 3, which
rules out `libayatana-appindicator`, `XApp` and a good deal of Stack Overflow.

**PyGObject callback signatures.** They do not always match the C ones —
`GError**` out-parameters are dropped. Getting a `register_object` property
callback wrong makes every read fail *silently*. There is a comment about this
in `tray.py`.

**Dates in tests.** Use dates relative to `date.today()`. A test that
hard-codes a date will pass until that date goes by; this has already happened
once here.

---

## Commits

Explain the *why*. A message that says what a diff already shows has not
earned its place. If you found something surprising on the way, that belongs
in the message too — it is the only place it will be recorded.

---

## Licence

Kairos is GPL-3.0-or-later. By contributing you agree your changes are under
the same licence.
