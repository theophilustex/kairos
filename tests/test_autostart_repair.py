"""A login entry that points at a copy of Kairos which is no longer there.

The entry records the exact file it was written from, and an AppImage is a
file people move and rename. When that happened the switch in Preferences
still said "on" — the entry was there — but nothing started at login.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

from kairos import autostart


class WithAConfigDirectory(unittest.TestCase):
    def setUp(self):
        self.previous = os.environ.get("XDG_CONFIG_HOME")
        self.directory = Path(tempfile.mkdtemp(prefix="kairos-autostart-"))
        os.environ["XDG_CONFIG_HOME"] = str(self.directory)

    def tearDown(self):
        if self.previous is None:
            os.environ.pop("XDG_CONFIG_HOME", None)
        else:
            os.environ["XDG_CONFIG_HOME"] = self.previous

    def write(self, exec_value):
        path = autostart.desktop_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("[Desktop Entry]\nType=Application\nName=Kairos\n"
                        f"Exec={exec_value}\n", encoding="utf-8")
        return path

    def exec_line(self):
        return [line for line in autostart.desktop_file().read_text().splitlines()
                if line.startswith("Exec=")][0]


class NoticingABrokenEntry(WithAConfigDirectory):
    def test_an_entry_pointing_at_a_moved_appimage_is_stale(self):
        self.write('"/nowhere/Kairos-0.1.0-x86_64.AppImage" --background')
        self.assertTrue(autostart.is_stale())

    def test_an_entry_pointing_at_a_real_file_is_not(self):
        self.write(f'"{sys.executable}" --background')
        self.assertFalse(autostart.is_stale())

    def test_a_source_checkout_entry_is_judged_by_its_interpreter(self):
        """Not by "env", which always exists."""
        self.write(f'env PYTHONPATH="/somewhere" "{sys.executable}" -m kairos --background')
        self.assertFalse(autostart.is_stale())
        self.write('env PYTHONPATH="/somewhere" "/no/such/python" -m kairos --background')
        self.assertTrue(autostart.is_stale())

    def test_a_bare_command_is_looked_up_on_the_path(self):
        self.write("definitely-not-a-real-command-kairos --background")
        self.assertTrue(autostart.is_stale())

    def test_an_unreadable_line_is_stale(self):
        self.write('"/unterminated --background')
        self.assertTrue(autostart.is_stale())

    def test_when_login_startup_is_off_nothing_is_stale(self):
        self.assertFalse(autostart.is_enabled())
        self.assertFalse(autostart.is_stale())


class RepairingIt(WithAConfigDirectory):
    def test_a_broken_entry_is_pointed_at_the_running_copy(self):
        self.write('"/nowhere/Kairos-0.1.0-x86_64.AppImage" --background')
        self.assertTrue(autostart.refresh_if_moved())
        self.assertEqual(self.exec_line(), f"Exec={autostart.startup_command()}")
        self.assertFalse(autostart.is_stale())

    def test_it_still_starts_in_the_background(self):
        self.write('"/nowhere/Kairos.AppImage" --background')
        autostart.refresh_if_moved()
        self.assertTrue(self.exec_line().endswith("--background"))

    def test_a_working_entry_is_left_alone(self):
        """A second copy running must not take over someone's login startup."""
        path = self.write(f'"{sys.executable}" --background')
        before = path.read_text()
        self.assertFalse(autostart.refresh_if_moved())
        self.assertEqual(path.read_text(), before)

    def test_it_never_switches_login_startup_on(self):
        self.assertFalse(autostart.refresh_if_moved())
        self.assertFalse(autostart.is_enabled())

    def test_an_appimage_points_at_itself(self):
        appimage = self.directory / "Kairos-x86_64.AppImage"
        appimage.write_text("")
        previous = os.environ.get("APPIMAGE")
        os.environ["APPIMAGE"] = str(appimage)
        try:
            self.write('"/old/place/Kairos-x86_64.AppImage" --background')
            autostart.refresh_if_moved()
            self.assertEqual(self.exec_line(), f'Exec="{appimage}" --background')
        finally:
            if previous is None:
                os.environ.pop("APPIMAGE", None)
            else:
                os.environ["APPIMAGE"] = previous


if __name__ == "__main__":
    unittest.main()
