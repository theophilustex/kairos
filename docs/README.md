<div align="center">

<img src="../data/icons/hicolor/256x256/apps/org.kairos.Calendar.png" width="96" alt="Kairos">

# Kairos documentation

</div>

Kairos is a lightweight, customisable calendar for Linux desktops, written in
Python with GTK 4 and libadwaita. It reads and writes CalDAV/WebDAV calendars,
works offline, and reminds you about things in a way that is hard to ignore.

<img src="images/month-view.png" alt="The month view">

*The month view. The sidebar carries a month, an “Up next” list and your
calendars; everything is drawn from a local cache, so it is instant.*

## If you are using Kairos

| | |
|---|---|
| [Installation](installation.md) | Getting it onto your machine, on any distribution, or as an AppImage. |
| [User guide](user-guide.md) | The views, creating and editing events, reminders, running in the background, keyboard and gestures. |
| [Calendars and accounts](calendars.md) | Connecting to Nextcloud, Radicale, Fastmail and others; what happens offline; how conflicts are resolved. |
| [Configuration](configuration.md) | Every preference, the settings file, and styling Kairos with your own CSS. |
| [Troubleshooting](troubleshooting.md) | When something does not work, start here. |

## If you are working on Kairos

| | |
|---|---|
| [Architecture](architecture.md) | How the code is laid out, the two invariants everything depends on, and how to add things. |
| [Security](security.md) | The threat model, what is defended against, and where the relevant code is. |
| [Packaging](packaging.md) | Installing, building the AppImage, and replacing the icon. |
| [Contributing](contributing.md) | Running the tests, the house style, and what a good change looks like. |

## The short version

```sh
# Debian / Ubuntu / Linux Mint
sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-4.0 gir1.2-adw-1 gnome-keyring
pip install -r requirements.txt
make install          # into ~/.local
kairos
```

Then press <kbd>Ctrl</kbd>+<kbd>L</kbd> to add a calendar account.

## What Kairos is, and is not

It is a calendar for one person, with a handful of calendars, that should feel
instant and stay out of the way. It is meant to be small enough to read in an
afternoon — about 7,000 lines including comments — so that anyone can change
the parts they disagree with.

It is not a groupware client. There are no invitations, no free/busy lookups,
no attendees, no tasks. Those are listed with everything else Kairos
deliberately does not do in the [user guide](user-guide.md#what-kairos-does-not-do).

## Licence

GPL-3.0-or-later. See [LICENSE](../LICENSE).
