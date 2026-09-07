"""The `--diagnose` tool, and what it is allowed to touch.

It exists to be run by someone whose calendar is not working, on a machine
already configured. It reads from the server and must not change either.
"""

import unittest

from kairos.config import settings


class ItLeavesSettingsAlone(unittest.TestCase):
    """It relaxes the plain-http rule to ask about an http server.

    Leaving that turned on afterwards would quietly weaken every account on
    the machine because someone once ran a diagnostic.
    """

    def setUp(self):
        self.before = settings.get_bool("allow_insecure_http")

    def tearDown(self):
        settings.set("allow_insecure_http", self.before)

    def run_against(self, url):
        from kairos import diagnostics
        import getpass
        real = getpass.getpass
        getpass.getpass = lambda *a, **k: "not-a-real-password"
        try:
            # Nothing is listening, so it fails at step 1 — which is the
            # point: the setting must be restored even then.
            diagnostics.run([url, "someone"])
        except SystemExit:
            pass
        finally:
            getpass.getpass = real

    def test_an_http_url_leaves_the_setting_as_it_found_it(self):
        settings.set("allow_insecure_http", False)
        self.run_against("http://127.0.0.1:9/")
        self.assertFalse(settings.get_bool("allow_insecure_http"),
                         "a security setting was left switched on")

    def test_an_https_url_never_touches_it(self):
        settings.set("allow_insecure_http", False)
        self.run_against("https://127.0.0.1:9/")
        self.assertFalse(settings.get_bool("allow_insecure_http"))

    def test_it_does_not_turn_the_setting_off_for_someone_who_wanted_it(self):
        settings.set("allow_insecure_http", True)
        self.run_against("http://127.0.0.1:9/")
        self.assertTrue(settings.get_bool("allow_insecure_http"))

    def test_a_failure_to_connect_is_reported_not_raised(self):
        from kairos import diagnostics
        import getpass
        real = getpass.getpass
        getpass.getpass = lambda *a, **k: "x"
        try:
            code = diagnostics.run(["https://127.0.0.1:9/", "someone"])
        finally:
            getpass.getpass = real
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
