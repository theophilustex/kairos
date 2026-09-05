"""The rules in :mod:`kairos.security`.

If any of these start failing, Kairos has become less safe, so they are worth
keeping strict.
"""

import json
import stat
import tempfile
import unittest
from pathlib import Path

from kairos.security import (
    SecurityError,
    read_json,
    sanitise_text,
    validate_calendar_url,
    write_private_json,
)


class UrlValidation(unittest.TestCase):
    def test_https_is_accepted(self):
        self.assertEqual(
            validate_calendar_url("https://dav.example.com/cal/"),
            "https://dav.example.com/cal/",
        )

    def test_bare_host_is_assumed_https(self):
        self.assertTrue(validate_calendar_url("dav.example.com/cal").startswith("https://"))

    def test_fragment_is_dropped(self):
        self.assertNotIn("#", validate_calendar_url("https://x.example.com/cal#frag"))

    def test_query_is_kept(self):
        self.assertIn("?a=b", validate_calendar_url("https://x.example.com/cal?a=b"))

    def test_empty_is_rejected(self):
        with self.assertRaises(SecurityError):
            validate_calendar_url("")

    def test_non_web_schemes_are_rejected(self):
        for url in ("file:///etc/passwd", "ftp://example.com/", "gopher://x/",
                    "javascript:alert(1)"):
            with self.subTest(url=url), self.assertRaises(SecurityError):
                validate_calendar_url(url)

    def test_scheme_without_slashes_is_rejected(self):
        """"javascript:x" must not become "https://javascript:x"."""
        for url in ("javascript:alert(1)", "mailto:me@example.com", "data:text/plain,x"):
            with self.subTest(url=url), self.assertRaises(SecurityError):
                validate_calendar_url(url)

    def test_a_port_is_not_mistaken_for_a_scheme(self):
        url = validate_calendar_url("dav.example.com:8443/cal")
        self.assertEqual(url, "https://dav.example.com:8443/cal")

    def test_embedded_credentials_are_rejected(self):
        with self.assertRaises(SecurityError):
            validate_calendar_url("https://user:secret@example.com/cal")

    def test_plain_http_is_rejected_by_default(self):
        with self.assertRaises(SecurityError):
            validate_calendar_url("http://example.com/cal")

    def test_plain_http_to_a_public_host_stays_rejected_even_when_allowed(self):
        """Opting in must not send a password unencrypted across the internet."""
        with self.assertRaises(SecurityError):
            validate_calendar_url("http://example.com/cal", allow_insecure_http=True)

    def test_plain_http_to_localhost_is_allowed_when_opted_in(self):
        url = validate_calendar_url("http://localhost:5232/dav/", allow_insecure_http=True)
        self.assertEqual(url, "http://localhost:5232/dav/")

    def test_plain_http_to_a_private_address_is_allowed_when_opted_in(self):
        url = validate_calendar_url("http://192.168.1.10:5232/dav/", allow_insecure_http=True)
        self.assertTrue(url.startswith("http://192.168.1.10"))

    def test_missing_host_is_rejected(self):
        with self.assertRaises(SecurityError):
            validate_calendar_url("https:///just/a/path")


class TextSanitising(unittest.TestCase):
    def test_control_characters_go(self):
        self.assertEqual(sanitise_text("a\x00b\x1bc\x7f"), "abc")

    def test_newlines_and_tabs_stay(self):
        self.assertEqual(sanitise_text("a\nb\tc"), "a\nb\tc")

    def test_line_endings_are_normalised(self):
        self.assertEqual(sanitise_text("a\r\nb\rc"), "a\nb\nc")

    def test_long_text_is_capped(self):
        self.assertLessEqual(len(sanitise_text("x" * 10000, max_length=100)), 101)

    def test_none_becomes_empty(self):
        self.assertEqual(sanitise_text(None), "")


class PrivateFiles(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix="kairos-files-"))

    def test_file_is_written_and_readable_back(self):
        path = self.directory / "thing.json"
        write_private_json(path, {"a": 1, "b": [2, 3]})
        self.assertEqual(json.loads(path.read_text()), {"a": 1, "b": [2, 3]})

    def test_file_is_private_to_the_user(self):
        path = self.directory / "secret.json"
        write_private_json(path, {"token": "value"})
        mode = stat.S_IMODE(path.stat().st_mode)
        self.assertEqual(mode, 0o600, f"expected 0600, got {mode:o}")

    def test_no_temporary_file_is_left_behind(self):
        path = self.directory / "clean.json"
        write_private_json(path, {"x": 1})
        leftovers = [p.name for p in self.directory.iterdir() if p.name.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_overwrite_replaces_the_content(self):
        path = self.directory / "twice.json"
        write_private_json(path, {"first": True})
        write_private_json(path, {"second": True})
        self.assertEqual(json.loads(path.read_text()), {"second": True})

    def test_reading_a_missing_file_returns_the_default(self):
        self.assertEqual(read_json(self.directory / "nope.json", default={"d": 1}), {"d": 1})

    def test_reading_broken_json_returns_the_default(self):
        path = self.directory / "broken.json"
        path.write_text("{not json")
        self.assertEqual(read_json(path, default=[]), [])


class SettingsValidation(unittest.TestCase):
    """A hand-edited settings file must never stop Kairos from starting."""

    def setUp(self):
        from kairos.config import Settings
        self.directory = Path(tempfile.mkdtemp(prefix="kairos-settings-"))
        self.path = self.directory / "settings.json"
        self.Settings = Settings

    def load(self, raw):
        self.path.write_text(json.dumps(raw))
        return self.Settings(self.path)

    def test_unknown_choice_falls_back_to_the_default(self):
        self.assertEqual(self.load({"theme": "banana"}).get("theme"), "system")

    def test_out_of_range_numbers_are_clamped(self):
        self.assertEqual(self.load({"font_scale": 99}).get("font_scale"), 1.5)
        self.assertEqual(self.load({"hour_height": -5}).get("hour_height"), 24)

    def test_wrong_type_falls_back(self):
        self.assertEqual(self.load({"hour_height": "tall"}).get("hour_height"), 48)

    def test_a_broken_file_still_gives_defaults(self):
        self.path.write_text("{{{ not json")
        self.assertEqual(self.Settings(self.path).get("theme"), "system")

    def test_a_json_list_instead_of_an_object_is_ignored(self):
        self.path.write_text("[1, 2, 3]")
        self.assertEqual(self.Settings(self.path).get("default_view"), "month")

    def test_unknown_keys_are_preserved(self):
        """A file written by a newer Kairos must survive an older one."""
        settings = self.load({"from_the_future": "keep me"})
        settings.set("theme", "dark")
        self.assertEqual(json.loads(self.path.read_text())["from_the_future"], "keep me")

    def test_setting_a_value_writes_the_file(self):
        settings = self.Settings(self.path)
        settings.set("time_format", "12h")
        self.assertEqual(json.loads(self.path.read_text())["time_format"], "12h")

    def test_listeners_are_told_about_changes(self):
        settings = self.Settings(self.path)
        seen = []
        settings.connect(seen.append)
        settings.set("compact_mode", True)
        self.assertEqual(seen, ["compact_mode"])

    def test_a_broken_listener_does_not_break_saving(self):
        settings = self.Settings(self.path)
        settings.connect(lambda key: 1 / 0)
        settings.set("compact_mode", True)
        self.assertTrue(settings.get_bool("compact_mode"))


if __name__ == "__main__":
    unittest.main()
