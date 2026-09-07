"""The OAuth 2.0 sign-in used for providers that refuse a password.

These run against a **fake authorisation server** on localhost rather than a
mock: the token exchange is HTTP, and a test that stubs out the HTTP tests
the stub. The fake checks what a real one would — that the PKCE verifier
matches the challenge, that the redirect URI is the one that was registered,
that a refresh token is only issued when asked for offline access.

What these cannot cover is Google itself. The endpoints, scope and the
``access_type``/``prompt`` pair are written from Google's documented
behaviour and have not been exercised against Google's servers.
"""

import http.server
import json
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

from kairos import oauth
from kairos.models import local_timezone


class FakeProvider(http.server.BaseHTTPRequestHandler):
    """Answers /token the way an authorisation server would."""

    issued: dict = {}

    def do_POST(self):                            # noqa: N802 (http.server API)
        length = int(self.headers.get("Content-Length", 0))
        fields = urllib.parse.parse_qs(self.rfile.read(length).decode())
        flat = {k: v[0] for k, v in fields.items()}
        state = type(self).issued

        if flat.get("grant_type") == "authorization_code":
            if flat.get("code") != state.get("code"):
                return self._fail(400, "invalid_grant")
            # PKCE: the verifier must hash to the challenge sent earlier.
            if oauth.challenge_for(flat.get("code_verifier", "")) != state.get("challenge"):
                return self._fail(400, "invalid_grant")
            if flat.get("redirect_uri") != state.get("redirect_uri"):
                return self._fail(400, "redirect_uri_mismatch")
            body = {"access_token": "first-access", "expires_in": 3600,
                    "token_type": "Bearer"}
            if state.get("offline"):
                body["refresh_token"] = "the-refresh-token"
            return self._ok(body)

        if flat.get("grant_type") == "refresh_token":
            if flat.get("refresh_token") != "the-refresh-token":
                return self._fail(400, "invalid_grant")
            # Note: no refresh_token in the reply, as real servers do.
            return self._ok({"access_token": "second-access", "expires_in": 3600,
                             "token_type": "Bearer"})

        return self._fail(400, "unsupported_grant_type")

    def _ok(self, payload):
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _fail(self, code, error):
        body = json.dumps({"error": error}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class WithFakeProvider(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.HTTPServer(("127.0.0.1", 0), FakeProvider)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        port = cls.server.server_address[1]
        cls.provider = oauth.Provider(
            name="Fake",
            authorize_url=f"http://127.0.0.1:{port}/authorize",
            token_url=f"http://127.0.0.1:{port}/token",
            scope="calendar",
        )

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.verifier = oauth.make_verifier()
        FakeProvider.issued = {
            "code": "the-code",
            "challenge": oauth.challenge_for(self.verifier),
            "redirect_uri": "http://127.0.0.1:9999/",
            "offline": True,
        }


class PKCE(unittest.TestCase):
    def test_a_verifier_is_long_enough(self):
        """RFC 7636 requires 43 to 128 characters."""
        self.assertTrue(43 <= len(oauth.make_verifier()) <= 128)

    def test_verifiers_are_not_reused(self):
        self.assertNotEqual(oauth.make_verifier(), oauth.make_verifier())

    def test_the_challenge_is_url_safe_base64_without_padding(self):
        challenge = oauth.challenge_for(oauth.make_verifier())
        self.assertNotIn("=", challenge)
        self.assertNotIn("+", challenge)
        self.assertNotIn("/", challenge)

    def test_the_challenge_is_stable_for_a_verifier(self):
        verifier = oauth.make_verifier()
        self.assertEqual(oauth.challenge_for(verifier), oauth.challenge_for(verifier))


class TheConsentUrl(unittest.TestCase):
    def parameters(self):
        url = oauth.authorization_url(
            oauth.GOOGLE, "client-123", "http://127.0.0.1:5000/",
            "a-verifier", "a-state")
        return dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))

    def test_it_asks_for_a_code_with_S256(self):
        query = self.parameters()
        self.assertEqual(query["response_type"], "code")
        self.assertEqual(query["code_challenge_method"], "S256")

    def test_it_sends_the_challenge_not_the_verifier(self):
        query = self.parameters()
        self.assertEqual(query["code_challenge"], oauth.challenge_for("a-verifier"))
        self.assertNotIn("a-verifier", query.values())

    def test_it_asks_for_offline_access(self):
        """Without this Google issues no refresh token and the account dies."""
        query = self.parameters()
        self.assertEqual(query["access_type"], "offline")
        self.assertEqual(query["prompt"], "consent")

    def test_it_carries_the_state(self):
        self.assertEqual(self.parameters()["state"], "a-state")

    def test_no_client_secret_is_put_in_the_url(self):
        """The consent URL goes to the browser, and to its history."""
        self.assertNotIn("client_secret", self.parameters())


class ExchangingTheCode(WithFakeProvider):
    def exchange(self, **overrides):
        arguments = dict(provider=self.provider, client_id="id",
                         client_secret="secret", code="the-code",
                         verifier=self.verifier,
                         redirect_uri="http://127.0.0.1:9999/")
        arguments.update(overrides)
        return oauth.exchange_code(**arguments)

    def test_it_returns_an_access_token(self):
        self.assertEqual(self.exchange().access_token, "first-access")

    def test_it_keeps_the_refresh_token(self):
        self.assertEqual(self.exchange().refresh_token, "the-refresh-token")

    def test_the_token_is_not_immediately_expired(self):
        self.assertFalse(self.exchange().expired)

    def test_a_wrong_verifier_is_refused(self):
        """PKCE is what stops another process spending the code."""
        with self.assertRaises(oauth.OAuthError):
            self.exchange(verifier=oauth.make_verifier())

    def test_a_wrong_redirect_uri_is_refused(self):
        with self.assertRaises(oauth.OAuthError):
            self.exchange(redirect_uri="http://127.0.0.1:1/")

    def test_a_wrong_code_is_refused(self):
        with self.assertRaises(oauth.OAuthError):
            self.exchange(code="not-the-code")

    def test_the_error_is_fit_to_show_a_user(self):
        try:
            self.exchange(code="not-the-code")
        except oauth.OAuthError as exc:
            self.assertIn("rejected", str(exc))

    def test_an_unreachable_provider_is_reported_not_raised_raw(self):
        dead = oauth.Provider(name="Dead", authorize_url="",
                              token_url="http://127.0.0.1:1/token", scope="x")
        with self.assertRaises(oauth.OAuthError):
            self.exchange(provider=dead)


class Refreshing(WithFakeProvider):
    def test_it_gets_a_new_access_token(self):
        token = oauth.refresh(self.provider, "id", "secret", "the-refresh-token")
        self.assertEqual(token.access_token, "second-access")

    def test_the_refresh_token_survives_a_reply_that_omits_it(self):
        """Real servers usually do omit it, meaning "keep the one you have"."""
        token = oauth.refresh(self.provider, "id", "secret", "the-refresh-token")
        self.assertEqual(token.refresh_token, "the-refresh-token")

    def test_a_bad_refresh_token_is_refused(self):
        with self.assertRaises(oauth.OAuthError):
            oauth.refresh(self.provider, "id", "secret", "stale")

    def test_no_refresh_token_says_so_plainly(self):
        with self.assertRaises(oauth.OAuthError) as caught:
            oauth.refresh(self.provider, "id", "secret", "")
        self.assertIn("add it again", str(caught.exception))


class Expiry(unittest.TestCase):
    def token(self, seconds):
        return oauth.Token(
            access_token="x",
            expires_at=datetime.now(tz=local_timezone()) + timedelta(seconds=seconds))

    def test_a_fresh_token_is_not_expired(self):
        self.assertFalse(self.token(3600).expired)

    def test_a_past_token_is_expired(self):
        self.assertTrue(self.token(-1).expired)

    def test_it_expires_early_by_the_margin(self):
        """A token about to lapse mid-request is treated as already gone."""
        self.assertTrue(self.token(5).expired)


class TheLoopbackReceiver(unittest.TestCase):
    def test_it_listens_only_on_loopback(self):
        with oauth.LoopbackReceiver(timeout=1) as receiver:
            self.assertTrue(receiver.redirect_uri.startswith("http://127.0.0.1:"))

    def test_it_reads_the_code_the_browser_brings_back(self):
        with oauth.LoopbackReceiver(timeout=5) as receiver:
            urllib.request.urlopen(
                receiver.redirect_uri + "?code=abc&state=xyz", timeout=5).read()
            self.assertEqual(receiver.wait_for_code("xyz"), "abc")

    def test_a_mismatched_state_is_refused(self):
        """Someone else's redirect, or one aimed at this port deliberately."""
        with oauth.LoopbackReceiver(timeout=5) as receiver:
            urllib.request.urlopen(
                receiver.redirect_uri + "?code=abc&state=wrong", timeout=5).read()
            with self.assertRaises(oauth.OAuthError):
                receiver.wait_for_code("xyz")

    def test_a_refusal_from_the_provider_is_reported(self):
        with oauth.LoopbackReceiver(timeout=5) as receiver:
            urllib.request.urlopen(
                receiver.redirect_uri + "?error=access_denied&state=xyz",
                timeout=5).read()
            with self.assertRaises(oauth.OAuthError) as caught:
                receiver.wait_for_code("xyz")
            self.assertIn("refused", str(caught.exception))

    def test_giving_up_waiting_is_an_error_not_a_hang(self):
        with oauth.LoopbackReceiver(timeout=0.2) as receiver:
            with self.assertRaises(oauth.OAuthError) as caught:
                receiver.wait_for_code("xyz")
            self.assertIn("Timed out", str(caught.exception))

    def test_each_receiver_takes_its_own_port(self):
        with oauth.LoopbackReceiver(timeout=1) as one:
            with oauth.LoopbackReceiver(timeout=1) as two:
                self.assertNotEqual(one.redirect_uri, two.redirect_uri)


if __name__ == "__main__":
    unittest.main()


class TheBackendSignsRequests(WithFakeProvider):
    """The CalDAV backend's side: attaching and renewing a bearer token."""

    def setUp(self):
        super().setUp()
        from kairos import oauth as oauth_module
        from kairos.models import Account
        from kairos.security import credentials

        # Point the "google" provider at the fake server for this test.
        self._real = oauth_module.PROVIDERS.get("google")
        oauth_module.PROVIDERS["google"] = self.provider

        self.account = Account(
            id="acct-oauth", name="Google", url="https://example.com/caldav/",
            oauth_provider="google", oauth_client_id="id")
        credentials.set_client_secret(self.account.id, "secret")
        credentials.set_refresh_token(self.account.id, "the-refresh-token")

    def tearDown(self):
        from kairos import oauth as oauth_module
        if self._real is not None:
            oauth_module.PROVIDERS["google"] = self._real

    def backend(self, account=None):
        from kairos.backends.caldav_backend import CalDAVBackend
        return CalDAVBackend(account or self.account)

    def signed_request(self, backend):
        import requests
        request = requests.Request("GET", "https://example.com/").prepare()
        return backend._bearer_auth()(request)

    def test_it_puts_a_bearer_token_on_the_request(self):
        signed = self.signed_request(self.backend())
        self.assertEqual(signed.headers["Authorization"], "Bearer second-access")

    def test_it_fetches_a_token_only_once_while_it_is_valid(self):
        backend = self.backend()
        self.signed_request(backend)
        first = backend._token
        self.signed_request(backend)
        self.assertIs(backend._token, first, "refreshed a token that was still good")

    def test_an_expired_token_is_renewed(self):
        backend = self.backend()
        self.signed_request(backend)
        backend._token.expires_at = datetime.now(tz=local_timezone()) - timedelta(1)
        signed = self.signed_request(backend)
        self.assertEqual(signed.headers["Authorization"], "Bearer second-access")
        self.assertFalse(backend._token.expired)

    def test_a_revoked_refresh_token_is_an_authentication_failure(self):
        """Not a transient error: retrying it forever would be wrong."""
        from kairos.backends.base import AuthenticationError
        from kairos.security import credentials
        credentials.set_refresh_token(self.account.id, "revoked")
        with self.assertRaises(AuthenticationError):
            self.signed_request(self.backend())

    def test_an_unknown_provider_is_reported_clearly(self):
        from kairos.backends.base import BackendError
        from kairos.models import Account
        stranger = Account(id="acct-x", name="Nowhere", url="https://example.com/",
                           oauth_provider="not-a-provider")
        with self.assertRaises(BackendError):
            self.signed_request(self.backend(stranger))

    def test_a_password_account_gets_no_bearer_token(self):
        from kairos.models import Account
        ordinary = Account(id="acct-pw", name="Fastmail",
                           url="https://example.com/", username="me")
        self.assertFalse(ordinary.uses_oauth)

    def test_a_rotated_refresh_token_is_saved(self):
        """A provider that rotates them would otherwise sign the user out."""
        from kairos.security import credentials
        backend = self.backend()
        self.signed_request(backend)
        self.assertEqual(credentials.get_refresh_token(self.account.id),
                         "the-refresh-token")


class TheAccountDialog(unittest.TestCase):
    """Choosing between a password server and Google."""

    @classmethod
    def setUpClass(cls):
        import gi
        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        from gi.repository import Adw
        Adw.init()

    def dialog(self, existing=None):
        from kairos.ui.account_dialog import AccountDialog
        return AccountDialog(existing)

    def test_it_starts_on_the_caldav_server_kind(self):
        d = self.dialog()
        self.assertFalse(d._is_google)
        self.assertTrue(d._url_row.get_visible())
        self.assertFalse(d._client_id_row.get_visible())

    def test_choosing_google_swaps_the_fields(self):
        d = self.dialog()
        d._kind_row.set_selected(1)
        self.assertTrue(d._is_google)
        self.assertTrue(d._client_id_row.get_visible())
        self.assertTrue(d._email_row.get_visible())
        self.assertFalse(d._url_row.get_visible(),
                         "a Google account has no server address to type")
        self.assertFalse(d._password_row.get_visible(),
                         "Google will not accept a password here")

    def test_the_button_says_sign_in_for_google(self):
        d = self.dialog()
        d._kind_row.set_selected(1)
        self.assertEqual(d._primary.get_label(), "Sign in")

    def test_google_needs_its_three_fields_before_it_will_start(self):
        d = self.dialog()
        d._kind_row.set_selected(1)
        d._sign_in_with_google()          # nothing filled in
        self.assertTrue(d._banner.get_revealed())
        self.assertIn("client ID", d._banner.get_title())
        self.assertFalse(d._busy, "it should not have started a sign-in")

    def test_editing_a_google_account_comes_back_as_google(self):
        from kairos.models import Account
        from kairos.security import credentials
        account = Account(id="acct-g", name="Work", username="me@example.com",
                          url="https://apidata.googleusercontent.com/caldav/v2/x/user",
                          oauth_provider="google", oauth_client_id="the-id")
        credentials.set_client_secret(account.id, "the-secret")
        d = self.dialog(account)
        self.assertTrue(d._is_google)
        self.assertEqual(d._email_row.get_text(), "me@example.com")
        self.assertEqual(d._client_id_row.get_text(), "the-id")

    def test_editing_an_ordinary_account_stays_ordinary(self):
        from kairos.models import Account
        account = Account(id="acct-p", name="Fastmail", username="me",
                          url="https://caldav.fastmail.com/dav/")
        d = self.dialog(account)
        self.assertFalse(d._is_google)
        self.assertEqual(d._url_row.get_text(), "https://caldav.fastmail.com/dav/")


class TheGoogleAccountShape(unittest.TestCase):
    def test_the_caldav_url_is_built_from_the_address(self):
        url = oauth.GOOGLE.caldav_url.format(user="me%40example.com")
        self.assertTrue(url.startswith("https://apidata.googleusercontent.com/"))
        self.assertIn("me%40example.com", url)

    def test_an_account_reports_that_it_uses_oauth(self):
        from kairos.models import Account
        self.assertTrue(Account(id="a", name="n", oauth_provider="google").uses_oauth)
        self.assertFalse(Account(id="a", name="n").uses_oauth)

    def test_oauth_details_survive_a_round_trip_through_json(self):
        from kairos.models import Account
        account = Account(id="a", name="n", oauth_provider="google",
                          oauth_client_id="cid")
        again = Account.from_json(account.to_json())
        self.assertEqual(again.oauth_provider, "google")
        self.assertEqual(again.oauth_client_id, "cid")

    def test_no_secret_is_written_to_the_accounts_file(self):
        """Only the keyring holds the client secret and the refresh token."""
        from kairos.models import Account
        raw = Account(id="a", name="n", oauth_provider="google",
                      oauth_client_id="cid").to_json()
        self.assertNotIn("client_secret", raw)
        self.assertNotIn("refresh_token", raw)


class RemovingAnOAuthAccount(unittest.TestCase):
    """Every secret has to go, not only the password."""

    def test_the_refresh_token_and_client_secret_are_wiped(self):
        import tempfile
        from pathlib import Path
        from kairos.accounts import AccountStore
        from kairos.models import Account
        from kairos.security import credentials

        store = AccountStore(Path(tempfile.mkdtemp()) / "accounts.json")
        account = Account(id="acct-gone", name="Google",
                          url="https://example.com/", oauth_provider="google")
        store.add(account)
        credentials.set_refresh_token(account.id, "a-refresh-token")
        credentials.set_client_secret(account.id, "a-client-secret")

        store.remove(account.id)

        self.assertIsNone(credentials.get_refresh_token(account.id),
                          "a live credential was left in the keyring")
        self.assertIsNone(credentials.get_client_secret(account.id))
