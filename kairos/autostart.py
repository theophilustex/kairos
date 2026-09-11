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
import shlex
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


def _exec_value() -> str | None:
    """The Exec= line of the login entry, if there is one."""
    try:
        for line in desktop_file().read_text(encoding="utf-8").splitlines():
            if line.startswith("Exec="):
                return line[len("Exec="):]
    except OSError:
        return None
    return None


def _program(exec_value: str) -> str | None:
    """The program an Exec= line runs, looking past a leading ``env VAR=...``."""
    try:
        parts = shlex.split(exec_value)
    except ValueError:
        return None
    if parts and parts[0] == "env":
        parts = [p for p in parts[1:] if "=" not in p.split("/")[0]]
    return parts[0] if parts else None


def is_stale() -> bool:
    """Whether login startup is on but points at something that is not there.

    The entry records the exact file it was written from, and an AppImage
    is just a file people move and rename. When that happens the switch in
    Preferences still says "on" — the file is there — but nothing starts.
    """
    if not is_enabled():
        return False
    program = _program(_exec_value() or "")
    if not program:
        return True
    if os.path.isabs(program):
        return not Path(program).exists()
    return shutil.which(program) is None


def refresh_if_moved() -> bool:
    """Point a broken login entry at the copy of Kairos running now.

    Only ever repairs an entry whose target has gone. A working entry is
    left alone, so running a second copy — a source checkout, a newer
    download — does not quietly take over login startup. Returns whether
    the file was rewritten.
    """
    if not is_stale():
        return False
    log.info("login startup pointed at something that no longer exists; "
             "repointing it at %s", startup_command())
    return set_enabled(True)


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
