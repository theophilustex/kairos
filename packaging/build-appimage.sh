#!/bin/bash
#
# Build Kairos into a single self-contained AppImage.
#
#   ./packaging/build-appimage.sh
#
# The result is Kairos-<version>-x86_64.AppImage in build/, which runs on any
# reasonably modern x86-64 Linux without installing anything.
#
# HOW IT WORKS
#
# There is no magic here, just four steps:
#
#   1. copy the app and its pure-Python dependencies into an AppDir;
#   2. copy the system's Python, PyGObject, GTK 4 and libadwaita in beside it,
#      following each library's dependencies with ldd;
#   3. copy the data GTK needs at runtime (GSettings schemas, icon themes,
#      pixbuf loaders) and rebuild the caches that index them;
#   4. hand the AppDir to appimagetool.
#
# WHERE TO RUN IT
#
# On a Debian/Ubuntu/Mint machine, and ideally the *oldest* one you want to
# support: glibc is not bundled (it cannot safely be), so an AppImage built on
# a new distribution will not start on an older one. Building on Ubuntu 22.04
# gives good coverage.
#
# WHAT YOU NEED
#
#   sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-4.0 gir1.2-adw-1 \
#                    librsvg2-common adwaita-icon-theme python3-pip \
#                    binutils file wget
#
set -euo pipefail

# ---------------------------------------------------------------------------
# Where everything is
# ---------------------------------------------------------------------------

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT="$(dirname "$HERE")"
BUILD="$PROJECT/build"
APPDIR="$BUILD/Kairos.AppDir"

APP_ID="org.kairos.Calendar"
VERSION="$(sed -n 's/^VERSION = "\(.*\)"/\1/p' "$PROJECT/kairos/__init__.py")"
ARCH="$(uname -m)"
# Override with OUTPUT=... to build somewhere else, which is handy when the
# usual file is in use.
OUTPUT="${OUTPUT:-$BUILD/Kairos-${VERSION}-${ARCH}.AppImage}"

PYTHON="${PYTHON:-python3}"
PYTHON_VERSION="$("$PYTHON" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"

say() { printf '\n\033[1;34m==>\033[0m %s\n' "$*"; }
die() { printf '\n\033[1;31mError:\033[0m %s\n' "$*" >&2; exit 1; }

# Libraries that must come from the host, never from the bundle. Mixing these
# across systems is what makes a hand-rolled AppImage crash on someone else's
# machine: the graphics stack has to match their kernel and drivers, and glibc
# has to be the one that loaded the process.
EXCLUDED_LIBRARIES='
libc.so libm.so libdl.so libpthread.so librt.so libutil.so libnsl.so
ld-linux libresolv.so libBrokenLocale.so libanl.so libcrypt.so
libGL.so libGLX.so libGLdispatch.so libEGL.so libGLESv libOpenGL.so
libdrm.so libgbm.so libglapi.so libgallium
libX11.so libXext.so libXau.so libXdmcp.so libxcb.so
libwayland-client.so libwayland-server.so libwayland-egl.so libwayland-cursor.so
libgcc_s.so libstdc++.so
libselinux.so libudev.so libsystemd.so libdbus-1.so
libasound.so libpulse
'

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

is_excluded() {
    local name; name="$(basename "$1")"
    local pattern
    for pattern in $EXCLUDED_LIBRARIES; do
        case "$name" in "$pattern"*) return 0 ;; esac
    done
    return 1
}

# Copy one library and, recursively, everything it links against.
copy_library() {
    local source="$1" target="$APPDIR/usr/lib"
    [ -f "$source" ] || return 0
    is_excluded "$source" && return 0

    local name; name="$(basename "$source")"
    [ -e "$target/$name" ] && return 0

    cp -L "$source" "$target/$name"

    # ldd prints "libfoo.so.1 => /path/to/libfoo.so.1 (0x...)"; take the path.
    local dependency
    while read -r dependency; do
        [ -n "$dependency" ] && copy_library "$dependency"
    done < <(ldd "$source" 2>/dev/null | awk '/=> \//{print $3}')
}

copy_tree() {
    local source="$1" destination="$2"
    [ -e "$source" ] || return 0
    mkdir -p "$(dirname "$destination")"
    cp -rL "$source" "$destination" 2>/dev/null || cp -r "$source" "$destination"
}

# ---------------------------------------------------------------------------
# 0. Sanity checks
# ---------------------------------------------------------------------------

say "Building Kairos $VERSION for $ARCH"

"$PYTHON" -c 'import gi; gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1")' \
    2>/dev/null || die "GTK 4 and libadwaita bindings are missing. Install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1."

command -v ldd >/dev/null || die "ldd is missing (install binutils)."

rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/bin" "$APPDIR/usr/lib" "$APPDIR/usr/share"

# ---------------------------------------------------------------------------
# 1. The application itself
# ---------------------------------------------------------------------------

say "Copying Kairos"

mkdir -p "$APPDIR/usr/lib/kairos"
cp -r "$PROJECT/kairos" "$APPDIR/usr/lib/kairos/kairos"
find "$APPDIR/usr/lib/kairos" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
echo "$PYTHON_VERSION" > "$APPDIR/usr/lib/kairos/PYTHON_VERSION"

say "Vendoring the Python dependencies"
"$PYTHON" -m pip install \
    --target "$APPDIR/usr/lib/kairos/vendor" \
    --no-compile --upgrade --quiet \
    -r "$PROJECT/requirements.txt" \
    || die "pip could not install the dependencies from requirements.txt"

# ---------------------------------------------------------------------------
# 2. Python, PyGObject and the GTK stack
# ---------------------------------------------------------------------------

say "Copying the Python runtime ($PYTHON_VERSION)"

cp -L "$(command -v "$PYTHON")" "$APPDIR/usr/bin/python3"
copy_library "$(command -v "$PYTHON")"

STDLIB="$("$PYTHON" -c 'import sysconfig; print(sysconfig.get_paths()["stdlib"])')"
mkdir -p "$APPDIR/usr/lib"
cp -r "$STDLIB" "$APPDIR/usr/lib/python$PYTHON_VERSION"
# The stdlib carries a lot we will never touch inside a calendar.
rm -rf "$APPDIR/usr/lib/python$PYTHON_VERSION"/{test,tests,idlelib,tkinter,turtledemo,ensurepip,lib2to3,pydoc_data}
find "$APPDIR/usr/lib/python$PYTHON_VERSION" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true

# Compiled stdlib extension modules (_ssl, _sqlite3, ...) and their libraries.
DYNLOAD="$STDLIB/lib-dynload"
if [ -d "$DYNLOAD" ]; then
    for module in "$DYNLOAD"/*.so; do
        [ -e "$module" ] || continue
        while read -r dependency; do
            [ -n "$dependency" ] && copy_library "$dependency"
        done < <(ldd "$module" 2>/dev/null | awk '/=> \//{print $3}')
    done
fi

say "Copying PyGObject"

GI_PATH="$("$PYTHON" -c 'import gi, os; print(os.path.dirname(gi.__file__))')"
DIST_PACKAGES="$APPDIR/usr/lib/python$PYTHON_VERSION/dist-packages"
mkdir -p "$DIST_PACKAGES"
copy_tree "$GI_PATH" "$DIST_PACKAGES/gi"
find "$DIST_PACKAGES" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
for module in $(find "$DIST_PACKAGES/gi" -name '*.so' 2>/dev/null); do
    while read -r dependency; do
        [ -n "$dependency" ] && copy_library "$dependency"
    done < <(ldd "$module" 2>/dev/null | awk '/=> \//{print $3}')
done

say "Copying GTK 4, libadwaita and their dependencies"

# Ask the loader where each library actually is, rather than guessing a path.
for library in libgtk-4.so.1 libadwaita-1.so.0 libgirepository-1.0.so.1 \
               libgio-2.0.so.0 libglib-2.0.so.0 libgobject-2.0.so.0 \
               libpango-1.0.so.0 libpangocairo-1.0.so.0 libcairo.so.2 \
               libgdk_pixbuf-2.0.so.0 librsvg-2.so.2 libharfbuzz.so.0 \
               libepoxy.so.0 libgraphene-1.0.so.0 libssl.so.3 libcrypto.so.3; do
    # No "exit" in the awk program: closing the pipe early kills ldconfig with
    # SIGPIPE, which under "set -o pipefail" would abort the whole build.
    found="$(ldconfig -p 2>/dev/null | awk -v lib="$library" '$1 == lib {print $NF}' | tail -n1 || true)"
    [ -n "$found" ] && copy_library "$found"
done

say "Copying the typelibs"

mkdir -p "$APPDIR/usr/lib/girepository-1.0"
for directory in /usr/lib/*/girepository-1.0 /usr/lib/girepository-1.0; do
    [ -d "$directory" ] || continue
    cp -n "$directory"/*.typelib "$APPDIR/usr/lib/girepository-1.0/" 2>/dev/null || true
done
[ -f "$APPDIR/usr/lib/girepository-1.0/Gtk-4.0.typelib" ] \
    || die "Gtk-4.0.typelib was not found. Install gir1.2-gtk-4.0."
[ -f "$APPDIR/usr/lib/girepository-1.0/Adw-1.typelib" ] \
    || die "Adw-1.typelib was not found. Install gir1.2-adw-1."

# ---------------------------------------------------------------------------
# 3. Runtime data GTK expects to find
# ---------------------------------------------------------------------------

say "Copying GSettings schemas, icon themes and pixbuf loaders"

mkdir -p "$APPDIR/usr/share/glib-2.0/schemas"
cp /usr/share/glib-2.0/schemas/*.xml "$APPDIR/usr/share/glib-2.0/schemas/" 2>/dev/null || true
if command -v glib-compile-schemas >/dev/null; then
    glib-compile-schemas --quiet "$APPDIR/usr/share/glib-2.0/schemas" || true
else
    cp /usr/share/glib-2.0/schemas/gschemas.compiled \
       "$APPDIR/usr/share/glib-2.0/schemas/" 2>/dev/null \
       || die "glib-compile-schemas is missing (install libglib2.0-dev-bin)."
fi

mkdir -p "$APPDIR/usr/share/icons"
copy_tree /usr/share/icons/Adwaita "$APPDIR/usr/share/icons/Adwaita"
copy_tree /usr/share/icons/hicolor "$APPDIR/usr/share/icons/hicolor"
copy_tree /usr/share/mime "$APPDIR/usr/share/mime"

# The SVG pixbuf loader, so the app icon renders.
LOADER_DIR="$(find /usr/lib -maxdepth 4 -type d -name 'loaders' -path '*gdk-pixbuf-2.0*' 2>/dev/null | head -1)"
if [ -n "$LOADER_DIR" ]; then
    mkdir -p "$APPDIR/usr/lib/gdk-pixbuf-2.0/loaders"
    cp "$LOADER_DIR"/*.so "$APPDIR/usr/lib/gdk-pixbuf-2.0/loaders/" 2>/dev/null || true
    for module in "$APPDIR/usr/lib/gdk-pixbuf-2.0/loaders"/*.so; do
        [ -e "$module" ] || continue
        while read -r dependency; do
            [ -n "$dependency" ] && copy_library "$dependency"
        done < <(ldd "$module" 2>/dev/null | awk '/=> \//{print $3}')
    done
    if command -v gdk-pixbuf-query-loaders >/dev/null; then
        GDK_PIXBUF_MODULEDIR="$APPDIR/usr/lib/gdk-pixbuf-2.0/loaders" \
            gdk-pixbuf-query-loaders \
            | sed "s|$APPDIR||g" > "$APPDIR/usr/lib/gdk-pixbuf-2.0/loaders.cache"
    fi
fi

# CA certificates, so https works whatever the host keeps its store.
for bundle in /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt; do
    if [ -f "$bundle" ]; then
        mkdir -p "$APPDIR/usr/share/ca-certificates"
        cp "$bundle" "$APPDIR/usr/share/ca-certificates/bundle.crt"
        break
    fi
done

# ---------------------------------------------------------------------------
# 4. Desktop integration and AppRun
# ---------------------------------------------------------------------------

say "Adding the desktop entry and icon"

cp "$PROJECT/data/$APP_ID.desktop" "$APPDIR/$APP_ID.desktop"
# Inside the bundle the binary is AppRun, not the installed "kairos" command.
sed -i 's|^Exec=kairos|Exec=AppRun|' "$APPDIR/$APP_ID.desktop"
mkdir -p "$APPDIR/usr/share/applications"
cp "$APPDIR/$APP_ID.desktop" "$APPDIR/usr/share/applications/"

mkdir -p "$APPDIR/usr/share/metainfo"
cp -r "$PROJECT/data/icons/hicolor" "$APPDIR/usr/share/icons/"

# The hicolor theme copied from the system brought its icon-theme.cache with
# it, and GTK trusts that cache over what is actually on disk. Left alone it
# would hide the icons we just added, and the app would show a generic one.
rm -f "$APPDIR/usr/share/icons/hicolor/icon-theme.cache"
for updater in gtk4-update-icon-cache gtk-update-icon-cache; do
    if command -v "$updater" >/dev/null; then
        "$updater" -f -q -t "$APPDIR/usr/share/icons/hicolor" 2>/dev/null && break
    fi
done

# AppImage looks for an icon named after the app at the top of the AppDir,
# with .DirIcon pointing at it. 256px is the size file managers expect.
cp "$PROJECT/data/icons/hicolor/256x256/apps/$APP_ID.png" "$APPDIR/$APP_ID.png"
ln -sf "$APP_ID.png" "$APPDIR/.DirIcon"
cp "$PROJECT/data/$APP_ID.metainfo.xml" "$APPDIR/usr/share/metainfo/"

cp "$HERE/AppRun" "$APPDIR/AppRun"
chmod +x "$APPDIR/AppRun"

# ---------------------------------------------------------------------------
# 5. Check the bundle actually runs before packing it
# ---------------------------------------------------------------------------

say "Testing the bundle"

if ! "$APPDIR/AppRun" --version; then
    die "The AppDir does not run. Nothing was packed."
fi

printf 'AppDir size: %s\n' "$(du -sh "$APPDIR" | cut -f1)"

# ---------------------------------------------------------------------------
# 6. Pack it
# ---------------------------------------------------------------------------

say "Packing the AppImage"

# An AppImage that is currently running cannot be overwritten — the kernel
# holds the file — and mksquashfs fails late with a bare "Text file busy"
# after several minutes of work. Say so now, and say what to do about it.
if [ -e "$OUTPUT" ] && ! : > /dev/null 2>&1 < "$OUTPUT"; then
    : # unreadable for some other reason; let the build try anyway
fi
if [ -e "$OUTPUT" ] && ! (exec 3<> "$OUTPUT") 2>/dev/null; then
    die "$OUTPUT is in use. Close the running copy of Kairos, or set
       OUTPUT=/some/other/path.AppImage to build alongside it."
fi

APPIMAGETOOL="$BUILD/appimagetool-$ARCH.AppImage"
if [ ! -x "$APPIMAGETOOL" ]; then
    echo "Downloading appimagetool..."
    wget -q --show-progress -O "$APPIMAGETOOL" \
        "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-$ARCH.AppImage" \
        || die "Could not download appimagetool. Put it at $APPIMAGETOOL yourself and re-run."
    chmod +x "$APPIMAGETOOL"
fi

# --appimage-extract-and-run means appimagetool works on machines without FUSE,
# which includes most containers and CI runners.
ARCH="$ARCH" "$APPIMAGETOOL" --appimage-extract-and-run "$APPDIR" "$OUTPUT" \
    || die "appimagetool failed."

chmod +x "$OUTPUT"

say "Done"
printf '  %s  (%s)\n\n' "$OUTPUT" "$(du -h "$OUTPUT" | cut -f1)"
printf 'Run it with:\n  %s\n\n' "$OUTPUT"
