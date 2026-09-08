# Troubleshooting

Start with the log. Almost everything below is diagnosable from:

```sh
kairos --debug
```

---

## My events have no reminders on them

Check first whether the reminders exist in the data at all. Kairos reads
VALARM components and always has; what it cannot do is show a reminder the
server never sent. Some servers keep reminders in their own web interface
and put nothing into the event.

`kairos --debug` will show what arrived. If the events genuinely carry no
reminder, set one for the whole calendar: **Calendars**
(<kbd>Ctrl</kbd>+<kbd>L</kbd>), the alarm button on the row. It applies to
every event in that calendar without a reminder of its own. See
[the user guide](user-guide.md#when-your-server-sends-no-reminders).

---

## My calendars appear but they have no events in them

Kairos found the calendars, so the address and password are right; something
is going wrong one step later. Ask the server directly — this only reads,
and prints no event titles unless you add `--show-titles`, so the output is
safe to paste into a bug report:

```sh
kairos --diagnose

# or, running the AppImage:
./Kairos-x86_64.AppImage --diagnose
```

With no arguments it asks about the accounts you have already set up, using
their own settings and stored passwords — which is what you want, because
typing a URL instead builds a *fresh* account with the defaults. That is how
someone whose server has a self-signed certificate gets a certificate error
from the diagnostic while the application itself works: the saved account has
verification turned off and the new one does not.

To ask about a server you have not added yet:

```sh
kairos --diagnose https://dav.example.com/ you@example.com
```

It walks the same path Kairos does and says which step produced nothing.

**One cause is worth checking first**, because it is self-sustaining. A
server hands out a *change-token* meaning "nothing has changed since this",
and Kairos skips the download when the token it holds still matches. Until
this was fixed, the token was saved before the events behind it were
fetched — so one failed download (a dropped connection, a certificate not
yet trusted) left a token claiming everything had already arrived, and every
sync afterwards believed it. The calendar stayed empty for good.

Kairos now saves the token only once the events are in the cache, and on
start it forgets the token of any calendar that has one but no events — a
contradiction that can only mean this happened. So an install already stuck
this way fixes itself the next time it syncs; there is nothing to delete by
hand.

**Why else this happens.** Kairos asks for events with a CalDAV `REPORT`. A
server that does not support one properly answers with *nothing* rather than
with an error, which is indistinguishable from an empty calendar. Kairos
therefore tries three routes in turn, stopping at the first that returns
anything:

1. a `calendar-query` for the date window — one request, what almost every
   server answers correctly;
2. a `calendar-query` for the whole collection, for servers that mishandle
   the date filter specifically;
3. a plain `PROPFIND` listing and one `GET` per resource, which uses no
   `REPORT` at all and works against anything that is a WebDAV server. This
   is the route a Synology NAS needs.

So the answers the diagnostic gives:

- **"…falls back to reading everything"** or **"…falls back to a plain
  PROPFIND listing"** — a current build already handles it. Update.
- **"The server reports this calendar as empty."** It really does look empty.
  Check you are pointing at the right account, and that your events are not
  outside the sync window (`sync_window_past_days` and
  `sync_window_future_days` in [configuration](configuration.md)).
- **"none of them could be read or parsed"** or **"Objects came back but none
  could be parsed"** — the server is sending iCalendar Kairos cannot read.
  Please report the output.

`kairos --debug` also logs one line per calendar per sync saying how many
objects came back and how many events were parsed from them.

## It will not start

### `Namespace Gtk not available` or `ValueError: Namespace Adw not available`

The GTK 4 or libadwaita introspection data is missing. See
[installation](installation.md#1-system-packages) — you want `gir1.2-gtk-4.0`
and `gir1.2-adw-1`, or your distribution's equivalents.

### `ModuleNotFoundError: No module named 'caldav'`

The same goes for `icalendar`, `recurring_ical_events` and `keyring`.

`pip install -r requirements.txt`. Run `make check-deps` to see all of them at
once.

### `Namespace Gtk is already loaded with version 3.0`

Something in the same process pulled in GTK 3. Kairos cannot run under a GTK 3
plugin or wrapper.

---

## Calendars and syncing

### "The server rejected the username or password"

Most likely you need an **app-specific password** rather than your account
password — Fastmail, iCloud, Zoho and anything with two-factor authentication
require one. Generate it in the provider's settings.

### "Connected to the server, but it did not offer any calendars"

The address is usually one level too high or too low. Try the alternatives in
[calendars](calendars.md#server-addresses). For Nextcloud the address ends in
`/remote.php/dav/`, not `/remote.php/dav/calendars/you/`.

### "the server's security certificate could not be verified"

A self-signed certificate. If it is your own server and you trust the network,
turn off *Verify the security certificate* for that account. Better, install
the certificate authority on your machine.

### Google Calendar will not connect

It is not supported: Google requires OAuth, not a password. Its CalDAV
endpoint refuses plain credentials.

### Changes are not reaching the server

Look for *not yet synced* on the event. Press <kbd>Ctrl</kbd>+<kbd>R</kbd> and
watch the banner across the top of the calendar, which shows the reason. If it
says the calendar is read-only, that is the server's own permission.

### Events from another client are missing

Kairos keeps a year of history and two years ahead by default. Anything
outside that window is not downloaded — widen `sync_window_past_days` and
`sync_window_future_days` in [settings](configuration.md#syncing).

---

## Reminders

### No reminders at all

In order:

1. Is Kairos running? It has to be. See
   [running in the background](user-guide.md#running-in-the-background).
2. Does the event have a reminder? Open it and look.
3. Is *Show reminders* on in Preferences → Reminders?
4. Press **Send a test reminder** on that page. If the test works and real
   reminders do not, the events have no alarms on them.

### Notifications do not appear but the alert window does

Your desktop is filtering them. Check its notification settings for Kairos,
and that Do Not Disturb is off. Kairos sends notifications at *urgent*
priority, which most desktops show even in Do Not Disturb — but not all.

### The alert window opens behind what I am doing

Focus-stealing prevention, and it is your window manager's decision rather
than Kairos's. There is no portable way to override it. Kairos asks properly
(`present_with_time`) and sets the X11 urgency hint as a fallback, which makes
the taskbar entry flash; the notification is always sent as well.

On Wayland there is no urgency hint at all, so the fallback is the taskbar
entry and the notification.

Some window managers can be told to allow it — look for "focus stealing
prevention" in their settings and make an exception for Kairos.

### A snoozed reminder never came back

Snoozes live in `~/.cache/kairos/snoozed_reminders.json`. If a cleanup tool
emptied your cache directory, they are gone. So is the fired-alarm history,
which means this morning's reminders may replay once.

---

## The taskbar icon

### There is no icon

Kairos is probably still running fine — the icon is optional. Check the log
for `tray icon registered`.

- **GNOME** has no system tray. Install the
  [AppIndicator extension](https://extensions.gnome.org/extension/615/appindicator-support/).
- **Cinnamon, KDE, XFCE, MATE, Budgie** have one built in; make sure the
  systray or notification-area applet is actually on your panel.
- **A bare window manager** (i3, sway, dwm) needs a tray program such as
  `snixembed`, `stalonetray` or your bar's own tray module.

Without an icon, launch Kairos again from your applications menu to bring the
window back — it is single-instance, so that raises the existing one.

### The menu opens but nothing happens when I click it

Fixed in the current version. If you are on an older build, the tray menu
declared only the singular `Event` method, while libdbusmenu — the client
behind most panels, including Mint's `xapp-sn-watcher` — uses the batched
`EventGroup` whenever a server reports dbusmenu version 3. The call was
rejected before Kairos saw it and the failure was discarded, so every item
drew correctly and did nothing.

If it happens again, `kairos --debug` logs every menu method the panel calls;
an "unhandled tray menu method" line names the culprit.

### The icon is blank

Kairos sends the icon as raw pixels as well as by name, so this should not
happen; if it does, please report it with your desktop and version.

---

## Appearance

### My `custom.css` does nothing

- Check the log with `--debug`: a CSS syntax error is reported there, and GTK
  ignores only the offending rule.
- Confirm the class name against the
  [list](configuration.md#class-names). GTK is silent about selectors that
  match nothing.
- The file is `~/.config/kairos/custom.css`, and is reloaded on save. If it is
  not reloading, use *Reload stylesheets now* in Preferences → Appearance.

### The sidebar month starts its week on the wrong day

It follows your system locale, not the *Week starts on* preference. GTK's
calendar widget has no setting for it. The main grid does follow the
preference.

### Everything is too big or too small

*Text size* in Preferences → Appearance scales the whole window (0.75–1.5).

---

## Data

### Where is everything?

See [configuration](configuration.md#where-things-live).

### Can I start over?

```sh
rm -rf ~/.cache/kairos          # cached events; server calendars re-download
rm -rf ~/.config/kairos         # settings and accounts
```

Deleting the cache also deletes **local calendars**, which exist nowhere else.

### The cache looks corrupt

Delete `~/.cache/kairos/cache.db` and restart. Server calendars re-download;
anything local is lost. An event Kairos cannot parse still appears with a
placeholder title so you can delete it.

---

## Reporting a bug

Include:

- what you did and what happened
- the output of `kairos --debug` around the problem
- `kairos --version`, your distribution, and your desktop
- your CalDAV server, if it is involved

For anything security-related, please report privately instead — see
[security](security.md#reporting-a-problem).
