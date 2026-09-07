# Calendars and accounts

- [Adding an account](#adding-an-account)
- [Server addresses](#server-addresses)
- [Managing calendars](#managing-calendars)
- [Local calendars](#local-calendars)
- [How syncing works](#how-syncing-works)
- [Working offline](#working-offline)
- [Conflicts](#conflicts)

---

## Adding an account

Press <kbd>Ctrl</kbd>+<kbd>L</kbd> (or the list button in the sidebar), then
**+ → Connect a CalDAV / WebDAV account**.

You need three things: the server address, your username, and your password.
Fill them in and press **Connect**. Kairos asks the server what calendars the
account has, lists them, and you tick the ones you want. Press **Add**.

Everything after that is automatic. Calendars created later on the server
appear at the next sync.

### If connecting fails

The dialog says why, in words rather than in HTTP codes. The common ones:

| Message | What to do |
|---|---|
| "The server rejected the username or password" | Check them. Many providers need an *app password* rather than your account password — see below. |
| "This address uses unencrypted http" | Use `https://`. If it is a server on your own network, see [security](security.md#plain-http). |
| "the server's security certificate could not be verified" | A self-signed certificate. Turn off *Verify the security certificate* for this account only if you trust the network. |
| "it did not offer any calendars for this account" | The address is probably a level too high or too low. Try the alternatives in the table below. |

---

## Server addresses

| Provider | Address |
|---|---|
| Nextcloud / ownCloud | `https://your-server/remote.php/dav/` |
| Radicale | `https://your-server/` |
| Fastmail | `https://caldav.fastmail.com/dav/` |
| Posteo | `https://posteo.de:8443/` |
| mailbox.org | `https://dav.mailbox.org/` |
| Zoho | `https://calendar.zoho.com/caldav/` |
| Baïkal | `https://your-server/dav.php/` |
| Synology Calendar | `https://your-nas:5001/caldav/` (see the note below) |
| iCloud | `https://caldav.icloud.com/` (needs an app-specific password) |

**Synology.** Its CalDAV does not answer a `calendar-query` REPORT the way
most servers do, so a client that relies on one finds your calendars and
then shows them all as empty. Kairos falls back to listing the collection
and reading each event, which needs no REPORT. If a Synology calendar still
looks empty, run `kairos --diagnose` (see
[troubleshooting](troubleshooting.md#my-calendars-appear-but-they-have-no-events-in-them)).

**App passwords.** Fastmail, iCloud, Zoho and anything with two-factor
authentication will reject your normal password. Generate an app-specific
password in the provider's settings and use that. It is also safer: it can be
revoked without changing your account password.

**Google** needs a sign-in rather than a password — see below.

A bare host is fine — type `dav.example.com/calendars/` and Kairos will assume
`https://`.

---

## Google

Google's CalDAV endpoint refuses Basic authentication, so there is no password
or app password that will work. Kairos signs in with OAuth instead: you type
your password on **Google's own page** in your browser, and Kairos only ever
receives a token.

Google requires every application to be registered, and Kairos is not
registered on your behalf — a shipped client would be shared by every user of
every copy, and Google's caps and consent screen are per-application. So you
create one, once:

1. In the [Google Cloud console](https://console.cloud.google.com/), create a
   project (any name).
2. Enable the **Google Calendar API** for it.
3. Under *APIs & Services → Credentials*, create an **OAuth client ID** of
   type **Desktop app**. Google shows you a client ID and a client secret.
4. If it asks you to configure a consent screen, choose *External*, fill in
   the required names, and add your own address under *Test users*.

Then in Kairos: <kbd>Ctrl</kbd>+<kbd>L</kbd> → **Add**, set *Account type* to
**Google**, and enter your Google address along with that ID and secret.
Kairos opens your browser; approve the request and the window comes back with
your calendars.

**What is stored where.** The client ID goes in `accounts.json`; it is not
secret. The client secret and the refresh token go in the system keyring
beside your passwords. Access tokens last about an hour and are never written
to disk. Removing the account deletes all of it.

**If it stops working**, the refresh token has been withdrawn — changing your
Google password does that, as does revoking access at
[myaccount.google.com/permissions](https://myaccount.google.com/permissions),
and a consent screen left in *Testing* expires them after seven days. Edit the
account and sign in again.

> Kairos's OAuth flow is tested against a local authorisation server, not
> against Google. If you hit something the documentation above does not
> explain, that is worth reporting.

---

## Managing calendars

<kbd>Ctrl</kbd>+<kbd>L</kbd> opens the calendar list, grouped by account.
Changes take effect immediately.

Per calendar you can:

- **Show or hide** it, with the switch. Hidden calendars still fire reminders.
- **Recolour** it. The colour is yours and is not sent to the server; a colour
  the server publishes is used only as the initial value.
- **Rename** it. Also local — Kairos will not let the server rename it back.
- **Delete** it, if it is a local calendar.

Per account you can edit the address, username and password, or remove it.
**Removing an account deletes nothing from the server**; it removes the
calendars from Kairos and deletes the password from your keyring.

---

## Local calendars

**+ → New calendar on this computer** makes a calendar that never leaves the
machine. Useful for things you do not want on a server, and it is what Kairos
starts with on first run so there is somewhere to put an event before any
account exists.

Local calendars live in the same SQLite file as the cached copies of your
server calendars: `~/.cache/kairos/cache.db`.

> That file is in the *cache* directory, which some cleanup tools will happily
> delete. Server calendars would simply re-download, but a local calendar
> exists nowhere else. If you keep anything important in one, back that file
> up, or keep it on a server instead.

---

## How syncing works

Kairos never asks the network to draw a screen. Everything on screen comes
from the local SQLite cache; the network happens on a background thread.

A sync run:

1. **pushes** anything you changed while it could not reach the server;
2. **asks each account** which calendars it has, so new ones appear;
3. **downloads** each calendar whose change-token has moved since last time.

That last point is what keeps it cheap. Most servers publish a token that
changes whenever anything in the calendar does; when it has not moved, Kairos
downloads nothing at all.

By default it syncs every 15 minutes, and on startup. Both are configurable,
and 0 minutes means "only when I press the button". <kbd>Ctrl</kbd>+<kbd>R</kbd>
syncs now.

Kairos keeps a year of history and two years ahead, which is a setting. Events
outside that window are not downloaded.

---

## Working offline

Everything keeps working. The calendar draws from the cache, and events you
create, edit or delete are queued.

An event with unsent changes shows *not yet synced* in its detail bubble. The
queue is retried on every sync, and a refresh from the server will never
discard a change you have not managed to send — that is
[explicitly tested](../tests/test_storage.py).

---

## Conflicts

Kairos sends `If-Match` when updating an event and `If-None-Match: *` when
creating one, so it knows when somebody else has changed the same event since
it last looked.

When that happens, **your edit wins**: Kairos logs a warning and writes your
version anyway.

That is a deliberate choice. There is no merge interface, and throwing away
what you have just typed is worse than overwriting a change you may not have
known about. If you would rather it refused, the behaviour is about ten lines
in `CalDAVBackend.save_event` — see
[architecture](architecture.md#backends).
