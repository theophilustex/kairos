"""Trusting a server whose certificate this machine does not already know.

A self-hosted box — a NAS, a home server — usually presents a certificate
signed by something no public root trusts. The answers are, in order: point
Kairos at the authority that signed it, get a certificate that is already
trusted, or stop verifying. Only the first keeps the connection safe, so it
had better work.
"""

import tempfile
import unittest
from pathlib import Path

from kairos.config import settings
from kairos.models import CALDAV, Account


class TheCertificateAuthorityFile(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix="kairos-tls-"))
        self.bundle = self.directory / "ca.pem"
        self.bundle.write_text("-----BEGIN CERTIFICATE-----\nnot a real one\n")
        settings.set("ca_certificate_path", "")
        settings.set("verify_tls_certificates", True)

    def tearDown(self):
        settings.set("ca_certificate_path", "")
        settings.set("verify_tls_certificates", True)

    def account(self, **kwargs):
        return Account(id="a", name="NAS", kind=CALDAV,
                       url="https://nas.example.com:5001/caldav/",
                       username="me", **kwargs)

    def verify_argument(self, account):
        """What the backend would hand to requests as ``verify``."""
        from kairos.backends.caldav_backend import CalDAVBackend
        backend = CalDAVBackend(account, password="x")
        captured = {}

        import caldav
        real = caldav.DAVClient

        def spy(*args, **kwargs):
            captured["verify"] = kwargs.get("ssl_verify_cert")
            raise RuntimeError("stop here")

        caldav.DAVClient = spy
        try:
            backend._connect()
        except Exception:
            pass
        finally:
            caldav.DAVClient = real
        return captured.get("verify")

    def test_by_default_it_just_verifies(self):
        self.assertIs(self.verify_argument(self.account()), True)

    def test_a_bundle_is_used_instead_of_plain_true(self):
        settings.set("ca_certificate_path", str(self.bundle))
        self.assertEqual(self.verify_argument(self.account()), str(self.bundle))

    def test_a_missing_file_falls_back_to_ordinary_verification(self):
        """Never silently stop verifying because a path was mistyped."""
        settings.set("ca_certificate_path", str(self.directory / "nope.pem"))
        self.assertIs(self.verify_argument(self.account()), True)

    def test_a_directory_is_not_accepted_as_a_bundle(self):
        settings.set("ca_certificate_path", str(self.directory))
        self.assertIs(self.verify_argument(self.account()), True)

    def test_whitespace_only_is_ignored(self):
        settings.set("ca_certificate_path", "   ")
        self.assertIs(self.verify_argument(self.account()), True)

    def test_an_account_with_verification_off_still_does_not_verify(self):
        settings.set("ca_certificate_path", str(self.bundle))
        self.assertIs(self.verify_argument(self.account(verify_tls=False)), False)

    def test_the_global_switch_still_wins(self):
        settings.set("ca_certificate_path", str(self.bundle))
        settings.set("verify_tls_certificates", False)
        self.assertIs(self.verify_argument(self.account()), False)

    def test_the_default_is_empty(self):
        from kairos.config import DEFAULTS
        self.assertEqual(DEFAULTS["ca_certificate_path"], "")


if __name__ == "__main__":
    unittest.main()
