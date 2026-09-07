"""Adding a CalDAV/WebDAV account.

Two steps.  First the user types a server address and credentials and presses
*Connect*; that runs on a worker thread so the dialog stays responsive.  Then
they tick the calendars they want and press *Add*.

The password is handed straight to :class:`~kairos.security.CredentialStore`
and is never held anywhere else — not in the account record, not in the
config file, and not in any log line.  If no system keyring is available the
dialog says so plainly rather than quietly writing the password to disk.
"""

from __future__ import annotations

import logging
import secrets
import threading
import uuid
from urllib.parse import quote

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, GObject, Gtk  # noqa: E402

from kairos.backends import AuthenticationError, BackendError
from kairos.backends.caldav_backend import CalDAVBackend
from kairos.config import settings
from kairos.models import CALDAV, Account, Calendar
from kairos.security import SecurityError, credentials, validate_calendar_url
from kairos.ui.widgets import colour_swatch

log = logging.getLogger(__name__)


class AccountDialog(Adw.Dialog):
    """Connect to a server, then choose which of its calendars to use.

    Emits ``account-added(Account, list[Calendar])`` once the user confirms.
    """

    __gsignals__ = {
        "account-added": (GObject.SignalFlags.RUN_FIRST, None, (object, object)),
    }

    def __init__(self, existing: Account | None = None) -> None:
        super().__init__()
        self.set_title("Edit account" if existing else "Add a calendar account")
        self.set_content_width(520)
        self.set_content_height(600)

        self._existing = existing
        self._discovered: list[Calendar] = []
        self._checkboxes: list[tuple[Gtk.CheckButton, Calendar]] = []
        self._busy = False

        self._build()
        if existing is not None:
            self._prefill(existing)

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def _build(self) -> None:
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_show_start_title_buttons(False)
        header.set_show_end_title_buttons(False)

        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        header.pack_start(cancel)

        self._primary = Gtk.Button(label="Connect")
        self._primary.add_css_class("suggested-action")
        self._primary.connect("clicked", self._on_primary_clicked)
        header.pack_end(self._primary)

        self._spinner = Gtk.Spinner()
        header.pack_end(self._spinner)

        toolbar.add_top_bar(header)

        self._banner = Adw.Banner()
        self._banner.set_revealed(False)

        page = Adw.PreferencesPage()
        page.add(self._server_group())
        page.add(self._calendars_group())

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        content.append(self._banner)
        content.append(page)
        page.set_vexpand(True)

        toolbar.set_content(content)
        self.set_child(toolbar)
        # The primary button exists by now, which _sync_kind_rows relabels.
        self._sync_kind_rows()

    def _server_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(title="Server")
        group.set_description(
            "Enter the CalDAV address your provider gave you — for example "
            "https://dav.example.com/calendars/you/"
        )

        # Google refuses Basic authentication on its CalDAV endpoint, so it
        # cannot be reached with a password at all — even an app password.
        # It is a separate kind of account rather than a different URL.
        self._kind_row = Adw.ComboRow(
            title="Account type",
            model=Gtk.StringList.new(["CalDAV server", "Google"]),
        )
        self._kind_row.connect("notify::selected", lambda *_: self._sync_kind_rows())
        group.add(self._kind_row)

        self._name_row = Adw.EntryRow(title="Account name")
        group.add(self._name_row)

        self._url_row = Adw.EntryRow(title="Server address")
        self._url_row.set_text("https://")
        group.add(self._url_row)

        self._username_row = Adw.EntryRow(title="Username")
        group.add(self._username_row)

        self._password_row = Adw.PasswordEntryRow(title="Password")
        self._password_row.connect("entry-activated", lambda *_: self._on_primary_clicked(None))
        group.add(self._password_row)

        self._email_row = Adw.EntryRow(title="Google address")
        group.add(self._email_row)

        self._client_id_row = Adw.EntryRow(title="OAuth client ID")
        group.add(self._client_id_row)

        self._client_secret_row = Adw.PasswordEntryRow(title="OAuth client secret")
        group.add(self._client_secret_row)

        self._oauth_help = Adw.ActionRow(
            title="Where these come from",
            subtitle=(
                "Google requires each application to be registered. In the "
                "Google Cloud console, enable the Calendar API and create an "
                "OAuth client of type “Desktop app”; it gives you an ID and a "
                "secret. Kairos never sees your Google password — you type it "
                "on Google's own page."
            ),
        )
        self._oauth_help.set_subtitle_lines(0)
        group.add(self._oauth_help)

        self._verify_row = Adw.SwitchRow(
            title="Verify the security certificate",
            subtitle="Turn this off only for a self-signed server you trust.",
        )
        self._verify_row.set_active(True)
        group.add(self._verify_row)

        if not credentials.available:
            warning = Adw.ActionRow(
                title="No password manager found",
                subtitle=(
                    "Kairos will remember this password until you quit, but "
                    "cannot store it safely. Install gnome-keyring or "
                    "kwalletmanager to keep it between sessions."
                ),
            )
            warning.add_prefix(Gtk.Image.new_from_icon_name("dialog-warning-symbolic"))
            group.add(warning)

        return group

    @property
    def _is_google(self) -> bool:
        return self._kind_row.get_selected() == 1

    def _sync_kind_rows(self) -> None:
        """Show only the fields the chosen kind of account actually needs."""
        google = self._is_google
        for row in (self._url_row, self._username_row, self._password_row,
                    self._verify_row):
            row.set_visible(not google)
        for row in (self._email_row, self._client_id_row,
                    self._client_secret_row, self._oauth_help):
            row.set_visible(google)
        self._primary.set_label("Sign in" if google else "Connect")

    def _calendars_group(self) -> Adw.PreferencesGroup:
        self._calendar_group = Adw.PreferencesGroup(title="Calendars")
        self._calendar_group.set_description("Connect first, then choose what to show.")
        self._calendar_group.set_visible(False)
        return self._calendar_group

    def _prefill(self, account: Account) -> None:
        if account.uses_oauth:
            self._kind_row.set_selected(1)
            self._email_row.set_text(account.username)
            self._client_id_row.set_text(account.oauth_client_id)
            secret = credentials.get_client_secret(account.id)
            if secret:
                self._client_secret_row.set_text(secret)
        self._name_row.set_text(account.name)
        self._url_row.set_text(account.url)
        self._username_row.set_text(account.username)
        self._verify_row.set_active(account.verify_tls)
        stored = credentials.get_password(account.id)
        if stored:
            self._password_row.set_text(stored)

    # ------------------------------------------------------------------
    # Connecting
    # ------------------------------------------------------------------

    def _on_primary_clicked(self, _button) -> None:
        if self._busy:
            return
        if self._discovered:
            self._finish()
        else:
            self._connect()

    def _connect(self) -> None:
        if self._is_google:
            self._sign_in_with_google()
            return
        self._connect_to_server()

    # -- Google -------------------------------------------------------

    def _sign_in_with_google(self) -> None:
        """Open Google's consent page and wait for the browser to come back.

        Everything slow happens on a worker thread: opening a browser, and
        then waiting — possibly minutes — for someone to finish typing a
        password and a second factor. Blocking the UI thread for that would
        freeze the whole application.
        """
        from kairos import oauth

        email = self._email_row.get_text().strip()
        client_id = self._client_id_row.get_text().strip()
        client_secret = self._client_secret_row.get_text().strip()
        if not (email and client_id and client_secret):
            self._show_error(
                "Fill in your Google address and the OAuth client ID and secret.")
            return

        account = Account(
            id=self._existing.id if self._existing else uuid.uuid4().hex,
            name=self._name_row.get_text().strip() or email,
            kind=CALDAV,
            url=oauth.GOOGLE.caldav_url.format(user=quote(email, safe="@")),
            username=email,
            oauth_provider="google",
            oauth_client_id=client_id,
        )

        self._set_busy(True)
        self._banner.set_title("Waiting for Google in your browser…")
        self._banner.set_revealed(True)

        def worker() -> None:
            try:
                verifier = oauth.make_verifier()
                state = secrets.token_urlsafe(24)
                with oauth.LoopbackReceiver() as receiver:
                    url = oauth.authorization_url(
                        oauth.GOOGLE, client_id, receiver.redirect_uri,
                        verifier, state)
                    if not oauth.open_in_browser(url):
                        GLib.idle_add(
                            self._on_failed,
                            "Could not open a browser to sign in to Google.")
                        return
                    code = receiver.wait_for_code(state)
                    token = oauth.exchange_code(
                        oauth.GOOGLE, client_id, client_secret, code, verifier,
                        receiver.redirect_uri)

                credentials.set_client_secret(account.id, client_secret)
                credentials.set_refresh_token(account.id, token.refresh_token)

                backend = CalDAVBackend(account)
                found = backend.discover_calendars()
                GLib.idle_add(self._on_connected, account, "", found)
            except oauth.OAuthError as exc:
                GLib.idle_add(self._on_failed, str(exc))
            except (AuthenticationError, BackendError) as exc:
                GLib.idle_add(self._on_failed, str(exc))
            except Exception as exc:
                log.exception("unexpected failure signing in to Google")
                GLib.idle_add(self._on_failed,
                              f"Could not sign in: {exc.__class__.__name__}")

        threading.Thread(target=worker, name="kairos-oauth", daemon=True).start()

    # -- an ordinary CalDAV server ------------------------------------

    def _connect_to_server(self) -> None:
        """Validate the form, then talk to the server on a worker thread."""
        try:
            url = validate_calendar_url(
                self._url_row.get_text(),
                allow_insecure_http=settings.get_bool("allow_insecure_http"),
            )
        except SecurityError as exc:
            self._show_error(str(exc))
            return

        username = self._username_row.get_text().strip()
        password = self._password_row.get_text()
        name = self._name_row.get_text().strip() or _name_from_url(url)

        account = Account(
            id=self._existing.id if self._existing else uuid.uuid4().hex,
            name=name,
            kind=CALDAV,
            url=url,
            username=username,
            verify_tls=self._verify_row.get_active(),
        )

        self._set_busy(True)
        self._banner.set_revealed(False)

        def worker() -> None:
            try:
                backend = CalDAVBackend(account, password=password)
                found = backend.discover_calendars()
                GLib.idle_add(self._on_connected, account, password, found)
            except AuthenticationError as exc:
                GLib.idle_add(self._on_failed, str(exc))
            except BackendError as exc:
                GLib.idle_add(self._on_failed, str(exc))
            except Exception as exc:
                log.exception("unexpected failure while connecting")
                GLib.idle_add(self._on_failed, f"Could not connect: {exc.__class__.__name__}")

        threading.Thread(target=worker, name="kairos-discover", daemon=True).start()

    def _on_connected(self, account: Account, password: str, found: list[Calendar]) -> bool:
        self._set_busy(False)
        self._pending_account = account
        self._pending_password = password
        self._discovered = found
        self._show_calendars(found)
        self._primary.set_label("Add")
        self._banner.set_title(f"Found {len(found)} calendar{'s' if len(found) != 1 else ''}.")
        self._banner.set_revealed(True)
        return GLib.SOURCE_REMOVE

    def _on_failed(self, message: str) -> bool:
        self._set_busy(False)
        self._show_error(message)
        return GLib.SOURCE_REMOVE

    def _show_error(self, message: str) -> None:
        self._banner.set_title(message)
        self._banner.set_revealed(True)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._primary.set_sensitive(not busy)
        self._spinner.set_visible(busy)
        if busy:
            self._spinner.start()
        else:
            self._spinner.stop()

    # ------------------------------------------------------------------
    # Choosing calendars
    # ------------------------------------------------------------------

    def _show_calendars(self, calendars: list[Calendar]) -> None:
        for checkbox, _calendar in self._checkboxes:
            row = checkbox.get_ancestor(Adw.ActionRow)
            if row is not None:
                self._calendar_group.remove(row)
        self._checkboxes.clear()

        for calendar in calendars:
            row = Adw.ActionRow(title=calendar.name, subtitle=calendar.url)
            row.add_prefix(colour_swatch(calendar.colour))

            checkbox = Gtk.CheckButton()
            checkbox.set_active(True)
            checkbox.set_valign(Gtk.Align.CENTER)
            row.add_suffix(checkbox)
            row.set_activatable_widget(checkbox)

            self._calendar_group.add(row)
            self._checkboxes.append((checkbox, calendar))

        self._calendar_group.set_visible(True)
        self._calendar_group.set_description(
            "Untick anything you would rather not see. You can change this later."
        )

    def _finish(self) -> None:
        chosen = [calendar for checkbox, calendar in self._checkboxes if checkbox.get_active()]
        if not chosen:
            self._show_error("Choose at least one calendar, or press Cancel.")
            return
        credentials.set_password(self._pending_account.id, self._pending_password)
        self.emit("account-added", self._pending_account, chosen)
        self.close()


def _name_from_url(url: str) -> str:
    """A reasonable default account name, taken from the host."""
    from urllib.parse import urlparse
    host = urlparse(url).hostname or "Calendar account"
    return host
