# Packaging

- [make targets](#make-targets)
- [Installing](#installing)
- [AppImage](#appimage)
- [The icon](#the-icon)
- [The screenshots](#the-screenshots)
- [Desktop integration files](#desktop-integration-files)

---

## make targets

```
make help         list these
make run          run from this checkout
make test         the whole test suite
make lint         unused imports and undefined names (needs pyflakes)
make check-deps   report what is missing
make install      install for the current user into ~/.local
make uninstall    remove it again
make icons SRC=x.png   regenerate the icon set from an image
make appimage     build a self-contained AppImage into build/
make clean        delete build artefacts and __pycache__
```

---

## Installing

```sh
make install                      # ~/.local
sudo make install PREFIX=/usr/local
```

It puts:

```
$PREFIX/share/kairos/kairos/                 the package
$PREFIX/bin/kairos                           launcher (from packaging/kairos.in)
$PREFIX/share/applications/                  the desktop entry
$PREFIX/share/icons/hicolor/<size>/apps/     eight icon sizes
$PREFIX/share/metainfo/                      AppStream metadata
```

and refreshes the desktop and icon caches. `make uninstall` removes exactly
those and leaves `~/.config/kairos` and `~/.cache/kairos` alone.

The launcher is generated from `packaging/kairos.in` by substituting `@PREFIX@`
and `@PYTHON@`, so the installed copy does not depend on `PYTHONPATH`.

---

## AppImage

```sh
make appimage
```

Produces `build/Kairos-<version>-x86_64.AppImage`, about 70 MB, which runs on
any reasonably modern x86-64 Linux with nothing installed.

### What the script does

[`packaging/build-appimage.sh`](../packaging/build-appimage.sh) is commented
step by step:

1. copy the app and `pip install` its dependencies into an AppDir;
2. copy the system's Python, PyGObject, GTK 4 and libadwaita in beside it,
   following each library's dependencies with `ldd`;
3. copy the data GTK needs at runtime — GSettings schemas, icon themes,
   pixbuf loaders — and rebuild the caches that index them;
4. **run the bundle** to check it works, then hand the AppDir to
   `appimagetool`.

Step 4 matters: a broken bundle fails loudly on your machine rather than
producing an AppImage that dies on somebody else's.

### Where to build it

On the **oldest** distribution you want to support. glibc is not bundled — it
cannot safely be — so an AppImage built on a new distribution will not start
on an older one. Ubuntu 22.04 gives good coverage.

### Build dependencies

```sh
sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-4.0 gir1.2-adw-1 \
                 librsvg2-common adwaita-icon-theme python3-pip \
                 binutils file wget
```

### Gotchas the script already handles

- **"Text file busy"** — you cannot overwrite an AppImage that is currently
  running. The script checks up front and says so, rather than failing several
  minutes in. Build somewhere else with `OUTPUT=/path/to/other.AppImage make appimage`.
- **A stale icon cache** — the hicolor theme copied from the system brings its
  `icon-theme.cache`, and GTK trusts that over what is on disk. The script
  deletes and regenerates it, or the app icon comes out blank.
- **`WM_CLASS`** — set from the program name in `app.py`, so the desktop can
  match the window to the `.desktop` file. Without it the taskbar shows a
  generic icon however well the icons are installed.

---

## The icon

The icon set is generated from one source image:

```sh
make icons SRC=path/to/icon.png
```

[`packaging/make-icons.py`](../packaging/make-icons.py) crops the artwork away
from whatever background it was drawn on, squares it, cuts transparent rounded
corners, and writes every size a desktop asks for
(`data/icons/hicolor/<size>x<size>/apps/`).

It measures the crop from the image rather than assuming coordinates, so a
redraw at a different size still works. Three things it is fixing:

- **an opaque background**, which would show as a light rectangle behind the
  icon on a dark panel;
- **a non-square source**, which icon themes assume against;
- **one large bitmap**, which turns to mush when the toolkit scales it to
  16px — each size is rendered deliberately instead.

Reinstall or rebuild the AppImage afterwards to pick it up.

---

## The screenshots

The images in `docs/images/` are generated, not taken by hand:

```sh
make screenshots
```

[`packaging/make-screenshots.py`](../packaging/make-screenshots.py) starts
Kairos against a **throwaway XDG directory** with a made-up week of events, so
no one's real calendar can end up in a committed PNG, drives the window through
each view, photographs it with `gnome-screenshot`, and scales the results to
1400px wide.

Two things it goes out of its way to do, both so the images do not depend on
when the script happens to run:

- **the demo week is anchored to the Monday of the current week**, so the week
  view is full whatever day it is, and dates never look stale;
- **the week view is scrolled explicitly** rather than to the current time,
  which would otherwise photograph an empty 3am grid overnight.

It needs a running X session plus `wmctrl` and `gnome-screenshot`, and Pillow
for the downscale. Rerun it after any change to how Kairos looks, so the
documentation does not drift away from the application.

---

## Desktop integration files

| File | |
|---|---|
| `data/org.kairos.Calendar.desktop` | The applications-menu entry, with a *New Event* action. Validated with `desktop-file-validate`. |
| `data/org.kairos.Calendar.metainfo.xml` | AppStream metadata for software centres. Validated with `appstreamcli validate`. |
| `data/icons/hicolor/…` | Eight PNG sizes, 16 to 512. |

The metainfo file also carries the screenshots software centres show. Those
have to be absolute URLs — they point at the images in this repository — so if
you fork Kairos, change them and the three `<url>` entries to your own
repository, or `appstreamcli` will warn that they are not reachable.

---

## Version numbers

The version lives in one place, `kairos/__init__.py`:

```python
VERSION = "0.1.0"
```

The Makefile, the AppImage build script and `pyproject.toml` read or mirror
it. Update `pyproject.toml` and add a `<release>` to the metainfo file when
you bump it.
