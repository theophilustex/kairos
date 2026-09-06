"""Starting Kairos when you log in.

A reminder that only works while you remember to open the calendar is not much
of a reminder, so Kairos can put itself in the desktop's autostart directory:

    ~/.config/autostart/org.kairos.Calendar.desktop

That is the freedesktop standard every Linux desktop reads. The file is
written and deleted by :func:`set_enabled`, and its presence *is* the setting —
there is no separate preference that could disagree with reality.

The awkward part is working out what command to write, because how Kairos was
started differs: an AppImage, an installed ``kairos`` on the PATH, or a plain
source checkout. :func:`startup_command` handles all three.
"""

from __future__ import annotations

import logging
import os
import shutil
import stat
import sys
from pathlib import Path

from kairos import APP_ID, APP_NAME

log = logging.getLogger(__name__)


def autostart_directory() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "autostart"


def desktop_file() -> Path:
    return autostart_directory() / f"{APP_ID}.desktop"


def startup_command() -> str:
    """The command that should relaunch Kairos at login.

    ``--background`` is always included: nobody wants a calendar window in
    their face the moment they log in. The point is to be running so that
    reminders arrive.
    """
    appimage = os.environ.get("APPIMAGE")
    if appimage and Path(appimage).exists():
        return f'"{appimage}" --background'

    installed = shutil.which("kairos")
    if installed:
        return f'"{installed}" --background'

    # A source checkout: run the interpreter against this package's parent.
    project = Path(__file__).resolve().parent.parent
    return f'env PYTHONPATH="{project}" "{sys.executable}" -m kairos --background'


def is_enabled() -> bool:
    return desktop_file().is_file()


def set_enabled(enabled: bool) -> bool:
    """Turn login startup on or off. Returns whether it worked."""
    path = desktop_file()
    if not enabled:
        try:
            path.unlink(missing_ok=True)
            return True
        except OSError as exc:
            log.warning("could not remove %s: %s", path, exc)
            return False

    contents = (
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={APP_NAME}\n"
        "Comment=Keep Kairos running so event reminders arrive\n"
        f"Exec={startup_command()}\n"
        f"Icon={APP_ID}\n"
        "Terminal=false\n"
        "X-GNOME-Autostart-enabled=true\n"
        # Cinnamon, KDE and XFCE read this; a short delay lets the panel's
        # tray start first, so the icon has somewhere to appear.
        "X-GNOME-Autostart-Delay=5\n"
    )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return True
    except OSError as exc:
        log.warning("could not write %s: %s", path, exc)
        return False
