# Security

Kairos handles two things worth protecting: the passwords to your calendar
accounts, and the contents of your calendar. This is what it does about them.

Nearly all of the relevant code is in one file,
[`kairos/security.py`](../kairos/security.py), so it can be reviewed in a
sitting. The rest of the codebase is expected to call into it rather than
rolling its own version.

---

## What Kairos is defending against

- **Someone reading your passwords off disk.** They are never written to a
  Kairos file.
- **Someone on the network reading or altering your calendar.** TLS is
  verified, and plain http is refused except to your own machine or LAN.
- **A hostile or broken server.** Everything a server sends is treated as
  untrusted input.
- **Another user on the same machine reading your config.** Files are `0600`.

It is not defending against someone who already has your user account: a
process running as you can read your keyring, like every other application on
the desktop.

---

## Passwords

Passwords go to the **system keyring** — GNOME Keyring, KWallet, or whatever
implements the Secret Service API — under the service name
`org.kairos.Calendar`, keyed by the account's id.

They are never written to `accounts.json`, never logged, and never put in an
exception message. Error text from the CalDAV backend is passed through a
scrubber that removes the password if it appears, and
[a test asserts it](../tests/test_caldav.py).

**If there is no keyring**, Kairos says so — in the account dialog and in
Preferences → Sync & security — and keeps the password in memory for the
session only. It does not quietly fall back to a file. You will be asked again
next time you start it.

Removing an account deletes its password from the keyring.

---

## Network

### https by default

Calendar URLs go through `validate_calendar_url()` before any connection.
The rules:

- The scheme must be `https`, or `http` under the conditions below. Anything
  else — `file:`, `ftp:`, `gopher:` — is refused.
- A string with a scheme but no `//`, such as `javascript:alert(1)`, is
  refused rather than being turned into a URL by the "assume https"
  convenience. A colon that introduces a port, as in `example.com:8443`, is
  correctly not treated as a scheme.
- Credentials embedded in the URL (`https://user:pass@host/`) are refused:
  they end up in logs and config files.
- Fragments are stripped, so what is stored is exactly what is used.

### Plain http

Refused by default. Turning on *Allow unencrypted http* is not enough on its
own — Kairos then still requires the host to resolve **only** to loopback,
private or link-local addresses. A public host over http is refused whatever
the setting says, because that would put your password on the wire in clear.

The preference exists for a self-hosted server on your own LAN, and for
nothing else.

### TLS certificates

Verified by default, per account. The switch that disables it is labelled as
dangerous in the interface, and disabling it is logged at warning level every
time a connection is made.

### Other network hygiene

- Every request has a timeout, so a wedged server cannot hang a sync forever.
- A single calendar resource larger than 8 MiB is skipped rather than loaded.
- Kairos talks only to the servers you configure. There is no telemetry, no
  update check, and no other outbound traffic of any kind.

---

## Data from servers is untrusted

Text fields — titles, locations, descriptions — are passed through
`sanitise_text()`, which strips control characters, normalises line endings
and caps the length. Event text is never rendered as Pango markup, so there is
no markup injection to escape; the sanitising is about keeping the display
sane and preventing one absurd field from locking up the interface.

An event that cannot be parsed is skipped with a warning rather than taking
the calendar down with it. A cached event that cannot be parsed still appears,
with a placeholder title, so you can select and delete it.

---

## Files

- Config files are written **atomically** — to a temporary file in the same
  directory, then renamed over the target — so a crash or power cut cannot
  leave a half-written file.
- They are created mode **0600**, and the directories mode **0700**.
- A stray temporary file is removed if anything goes wrong mid-write.

---

## The database

Every SQL statement uses **bound parameters**. There is no string
interpolation of user or server data into SQL anywhere in
[`kairos/storage.py`](../kairos/storage.py), including the search, where the
user's `%` and `_` are escaped so they match literally rather than acting as
wildcards.

---

## Things Kairos does not do

- No `eval`, no `exec`, no `pickle`.
- No shelling out. The one exception is opening your config folder or
  `custom.css`, which is handed to the desktop's own file launcher.
- No code downloaded or executed at runtime.
- No setuid, no privileged helper, no system-wide daemon.

---

## Reporting a problem

If you find a security issue, please report it privately to the maintainers
rather than opening a public issue.

---

## Reviewing it yourself

```sh
# The security-relevant code, in reading order
kairos/security.py                 # 320 lines: URLs, passwords, safe writes
kairos/backends/caldav_backend.py  # the only file that touches the network
kairos/storage.py                  # the only file that touches SQL

# The tests that hold it in place
python3 -m unittest tests.test_security -v
```

---

## A server whose certificate is not publicly trusted

A self-hosted box — a NAS, a home server — usually presents a certificate no
public root signs, and Kairos refuses it:

    the server's security certificate could not be verified

Three ways out, best first:

1. **Point Kairos at the authority that signed it.** Put the CA's PEM file
   somewhere readable and set `ca_certificate_path` (Preferences → Sync &
   security). The connection is still verified, against that authority
   instead of the public ones — you lose nothing. A path that is not a file
   is ignored with a warning and Kairos goes on verifying normally, so a
   typo cannot silently stop the checking.
2. **Get a certificate the machine already trusts.** Let's Encrypt is free,
   and a Synology NAS can request one for you.
3. **Turn off "Verify the security certificate" for that account.** It
   works, and it means anyone on the network between you and the server can
   read and alter your calendar. It is per-account, so the rest keep
   checking.

`kairos --diagnose` says which of these you are looking at.

---

## Links in events

The **Join** button opens a URL taken from an event, and an event may have
been written by anyone who can add an entry to a calendar you subscribe to.
`security.safe_external_url` therefore accepts only `http` and `https`;
`file:///`, `javascript:`, `data:` and any scheme another application has
registered on the desktop are refused, on the way in from the server *and*
again before anything is launched. A link typed into the editor is held to
the same rule.

---

## OAuth accounts

Google is signed in to with the authorization-code flow and PKCE, over a
redirect to `127.0.0.1` on a port chosen for the occasion. Three consequences
worth stating:

- **Your password is never typed into Kairos.** It goes into Google's own page
  in your own browser. Kairos receives a token, and cannot see the password
  even briefly.
- **PKCE, not the client secret, is what makes the exchange safe.** A desktop
  application cannot keep a secret — anyone can read it out of the config — so
  the secret proves nothing on its own. The verifier is generated per sign-in
  and never leaves the process, so no other program on the machine can spend a
  code it did not ask for.
- **The listener is loopback-only and serves one request.** It binds
  `127.0.0.1`, so nothing off the machine can reach it, and it checks the
  `state` value before accepting the code, so a redirect aimed at that port by
  something else is rejected.

The refresh token and client secret live in the keyring; access tokens stay in
memory. `AccountStore.remove` deletes all three.
