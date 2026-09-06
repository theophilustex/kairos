# Installation

Kairos needs **Python 3.10 or newer**, **GTK 4** and **libadwaita 1**.

There are three ways to run it, in rough order of how much you have to think
about: the AppImage, a user install, or straight from a checkout.

---

## The AppImage

One file, no dependencies, nothing installed. Good for trying Kairos out, and
for distributions whose GTK 4 is older than Kairos wants.

```sh
chmod +x Kairos-0.1.0-x86_64.AppImage
./Kairos-0.1.0-x86_64.AppImage
```

Building one yourself is covered in [packaging](packaging.md#appimage). The
AppImage carries its own Python, GTK 4, libadwaita and dependencies — about
70 MB — and uses your system's graphics stack and glibc.

If you want it in your applications menu, tools like
[AppImageLauncher](https://github.com/TheAssassin/AppImageLauncher) will do
that for you; Kairos does not install itself.

---

## Installing from source

### 1. System packages

PyGObject has to come from your distribution: it is built against the exact
GTK on your machine, and a pip-installed copy will not match.

**Debian, Ubuntu, Linux Mint, Pop!\_OS**

```sh
sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-4.0 gir1.2-adw-1 \
                 gnome-keyring
```

**Fedora**

```sh
sudo dnf install python3-gobject gtk4 libadwaita gnome-keyring
```

**Arch, Manjaro, EndeavourOS**

```sh
sudo pacman -S python-gobject gtk4 libadwaita gnome-keyring
```

**openSUSE**

```sh
sudo zypper install python3-gobject python3-gobject-Gdk typelib-1_0-Gtk-4_0 \
                    typelib-1_0-Adw-1 gnome-keyring
```

`gnome-keyring` is where calendar passwords are stored. On KDE, install
`kwalletmanager` instead — either works. Without one, Kairos will ask for the
password again each session and will say so; see
[security](security.md#passwords).

### 2. Python packages

```sh
pip install -r requirements.txt
```

Four of them: `caldav`, `icalendar`, `recurring-ical-events`, `keyring`.

On a distribution with [PEP 668](https://peps.python.org/pep-0668/) protection
(most current ones), pip will refuse to touch the system Python. Either use a
virtual environment, or install the distribution's packages:

```sh
# Debian / Ubuntu / Mint
sudo apt install python3-caldav python3-icalendar python3-keyring
pip install --user recurring-ical-events
```

### 3. Check, then install

```sh
make check-deps       # says exactly what is missing, if anything
make install          # into ~/.local
```

`make check-deps` prints something like:

```
Checking dependencies...
  ok   GTK 4.14, libadwaita 1.5
  ok   caldav
  ok   icalendar
  ok   recurring_ical_events
  ok   keyring
  --   radicale not installed; CalDAV integration tests will skip
```

`make install` copies the package to `~/.local/share/kairos`, puts a `kairos`
launcher in `~/.local/bin`, and installs the desktop entry, icons and
AppStream metadata. Install system-wide with `sudo make install PREFIX=/usr/local`.

If `kairos` is not found afterwards, `~/.local/bin` is not on your `PATH`:

```sh
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
```

### Uninstalling

```sh
make uninstall
```

That removes the program and leaves your calendars and settings alone. To
remove those too:

```sh
rm -rf ~/.config/kairos ~/.cache/kairos
```

Passwords in the keyring are removed when you delete the account inside
Kairos, not by uninstalling.

---

## Running from a checkout

Nothing to install:

```sh
./run.py
# or, identically
python3 -m kairos
```

The icon and stylesheet are found relative to the source tree, so a checkout
looks the same as an installed copy.

---

## Command line

```
kairos                 start normally
kairos --background    start without a window, so reminders still arrive
kairos --new-event     open straight into the new-event dialog
kairos --debug         log everything, loudly
kairos --version       print the version and exit
kairos --help          this list
```

Kairos is a single-instance application: running it again raises the existing
window rather than starting a second copy. That matters because two copies
would fight over the same cache.

---

## Next

- [User guide](user-guide.md) — what everything does
- [Calendars and accounts](calendars.md) — connect it to your server
