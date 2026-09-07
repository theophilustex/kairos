"""Finding a meeting link on an event, and refusing to open anything else.

Event text comes from a server and may have been written by anyone who can
put an entry in your calendar. The "Join" button hands its URL to the
desktop's browser, so what counts as a URL here is a security boundary, not
a convenience: only http and https ever come back.
"""

import unittest
from datetime import datetime, timedelta

from kairos import ical
from kairos.models import Event, local_timezone
from kairos.security import find_url, is_meeting_url, safe_external_url


class SafeUrls(unittest.TestCase):
    def test_https_is_allowed(self):
        self.assertEqual(safe_external_url("https://meet.jit.si/room"),
                         "https://meet.jit.si/room")

    def test_http_is_allowed(self):
        self.assertEqual(safe_external_url("http://example.com/x"),
                         "http://example.com/x")

    def test_other_schemes_are_refused(self):
        for candidate in ("file:///etc/passwd",
                          "javascript:alert(1)",
                          "data:text/html;base64,PHNjcmlwdD4=",
                          "ftp://example.com/x",
                          "smb://server/share",
                          "vnc://host",
                          "mailto:someone@example.com",
                          "tel:+1234"):
            with self.subTest(candidate=candidate):
                self.assertIsNone(safe_external_url(candidate))

    def test_a_scheme_in_odd_case_is_still_refused(self):
        self.assertIsNone(safe_external_url("JavaScript:alert(1)"))

    def test_a_url_with_no_host_is_refused(self):
        self.assertIsNone(safe_external_url("https://"))

    def test_nothing_is_refused(self):
        self.assertIsNone(safe_external_url(""))
        self.assertIsNone(safe_external_url("   "))


class FindingTheLink(unittest.TestCase):
    def test_a_bare_url_field_is_used_as_is(self):
        self.assertEqual(find_url("https://zoom.us/j/123"), "https://zoom.us/j/123")

    def test_it_searches_inside_free_text(self):
        self.assertEqual(
            find_url("", "", "Dial in at https://meet.google.com/abc-defg-hij today"),
            "https://meet.google.com/abc-defg-hij")

    def test_the_fields_are_searched_in_order(self):
        """The URL property wins over something buried in the notes."""
        self.assertEqual(
            find_url("https://first.example.com/", "https://second.example.com/"),
            "https://first.example.com/")

    def test_a_trailing_full_stop_is_not_part_of_the_url(self):
        self.assertEqual(find_url("", "", "Join at https://meet.jit.si/room."),
                         "https://meet.jit.si/room")

    def test_a_url_in_brackets_stops_at_the_bracket(self):
        self.assertEqual(find_url("", "", "(https://meet.jit.si/room)"),
                         "https://meet.jit.si/room")

    def test_an_unsafe_url_in_the_text_is_not_offered(self):
        self.assertIsNone(find_url("", "file:///etc/passwd", "javascript:alert(1)"))

    def test_an_unsafe_url_field_does_not_hide_a_safe_one_later(self):
        self.assertEqual(find_url("javascript:alert(1)", "https://example.com/x"),
                         "https://example.com/x")

    def test_no_link_at_all(self):
        self.assertIsNone(find_url("", "Room 3", "Bring the printout"))


class MeetingHosts(unittest.TestCase):
    def test_known_providers_are_recognised(self):
        for url in ("https://zoom.us/j/1", "https://acme.zoom.us/j/1",
                    "https://meet.google.com/abc", "https://teams.microsoft.com/l/x",
                    "https://meet.jit.si/room", "https://whereby.com/room"):
            with self.subTest(url=url):
                self.assertTrue(is_meeting_url(url))

    def test_a_lookalike_host_is_not_a_meeting(self):
        """Suffix matching must not match "notzoom.us" as "zoom.us"."""
        self.assertFalse(is_meeting_url("https://notzoom.us/j/1"))
        self.assertFalse(is_meeting_url("https://evilzoom.us.example.com/"))

    def test_an_ordinary_link_is_not_a_meeting(self):
        self.assertFalse(is_meeting_url("https://example.com/agenda.pdf"))

    def test_nothing_is_not_a_meeting(self):
        self.assertFalse(is_meeting_url(None))
        self.assertFalse(is_meeting_url(""))


class TheUrlProperty(unittest.TestCase):
    def event(self, **kwargs):
        start = datetime(2026, 9, 8, 10, tzinfo=local_timezone())
        event = Event.new("cal", start, start + timedelta(hours=1), "Standup")
        for key, value in kwargs.items():
            setattr(event, key, value)
        return event

    def round_trip(self, event):
        text = ical.to_ical_text(event)
        return ical.parse_calendar_text(text, "cal")[0]

    def test_it_survives_a_round_trip(self):
        again = self.round_trip(self.event(url="https://meet.jit.si/room"))
        self.assertEqual(again.url, "https://meet.jit.si/room")

    def test_an_event_without_one_writes_no_url(self):
        self.assertNotIn("URL:", ical.to_ical_text(self.event()))

    def test_a_dangerous_url_from_a_server_is_dropped_on_the_way_in(self):
        """The event came from a server; this is the untrusted direction."""
        text = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//EN
BEGIN:VEVENT
UID:evil-1
DTSTAMP:20260908T090000Z
DTSTART:20260908T100000Z
DTEND:20260908T110000Z
SUMMARY:Innocent looking
URL:file:///etc/passwd
END:VEVENT
END:VCALENDAR
"""
        self.assertEqual(ical.parse_calendar_text(text, "cal")[0].url, "")


class TheJoinButton(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import gi
        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        from gi.repository import Adw
        Adw.init()

    def popover(self, **kwargs):
        from kairos.models import Occurrence
        from kairos.ui.event_popover import EventPopover
        start = datetime(2026, 9, 8, 10, tzinfo=local_timezone())
        event = Event.new("cal", start, start + timedelta(hours=1), "Standup")
        for key, value in kwargs.items():
            setattr(event, key, value)
        occurrence = Occurrence(event=event, start=start,
                                end=start + timedelta(hours=1))
        return EventPopover(occurrence, calendar_name="Work",
                            calendar_colour="#e01b24")

    def button_labels(self, popover):
        from gi.repository import Gtk

        def walk(widget):
            yield widget
            child = widget.get_first_child()
            while child is not None:
                yield from walk(child)
                child = child.get_next_sibling()

        return [w.get_label() for w in walk(popover)
                if isinstance(w, Gtk.Button) and w.get_label()]

    def test_a_meeting_link_offers_join(self):
        self.assertIn("Join", self.button_labels(
            self.popover(url="https://meet.jit.si/standup")))

    def test_an_ordinary_link_offers_open_link(self):
        labels = self.button_labels(self.popover(url="https://example.com/agenda"))
        self.assertIn("Open link", labels)
        self.assertNotIn("Join", labels)

    def test_a_link_in_the_location_is_found(self):
        self.assertIn("Join", self.button_labels(
            self.popover(location="https://zoom.us/j/999")))

    def test_a_link_in_the_notes_is_found(self):
        self.assertIn("Join", self.button_labels(
            self.popover(description="Call on https://meet.google.com/x-y-z")))

    def test_no_link_means_no_button(self):
        labels = self.button_labels(self.popover(location="Room 3"))
        self.assertNotIn("Join", labels)
        self.assertNotIn("Open link", labels)

    def test_a_dangerous_link_offers_nothing(self):
        labels = self.button_labels(self.popover(description="file:///etc/passwd"))
        self.assertNotIn("Join", labels)
        self.assertNotIn("Open link", labels)


if __name__ == "__main__":
    unittest.main()


class OnlyOnePrimaryAction(TheJoinButton):
    """Two suggested buttons side by side say nothing about which to press."""

    def suggested(self, popover):
        from gi.repository import Gtk

        def walk(widget):
            yield widget
            child = widget.get_first_child()
            while child is not None:
                yield from walk(child)
                child = child.get_next_sibling()

        return [w.get_label() for w in walk(popover)
                if isinstance(w, Gtk.Button) and w.get_label()
                and w.has_css_class("suggested-action")]

    def test_join_is_the_primary_action_when_there_is_a_link(self):
        self.assertEqual(self.suggested(
            self.popover(url="https://meet.jit.si/x")), ["Join"])

    def test_edit_is_primary_when_there_is_none(self):
        self.assertEqual(self.suggested(self.popover(location="Room 3")), ["Edit"])
