"""The sidebar's collapsible sections and "Up next" list, and searching.

The search tests are the important ones here. Searching used to match the
stored iCalendar text directly, which meant every event matched almost any
term — they all carry ``CALSCALE:GREGORIAN``, so "re" returned the whole
calendar. These pin that shut.
"""

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402

Adw.init()

from kairos.accounts import AccountStore  # noqa: E402
from kairos.config import settings  # noqa: E402
from kairos.models import Event, local_timezone, start_of_day  # noqa: E402
from kairos.storage import Storage  # noqa: E402
from kairos.sync import SyncManager  # noqa: E402
from kairos.ui.search_view import SearchView  # noqa: E402
from kairos.ui.upcoming import UpcomingList  # noqa: E402
from kairos.ui.widgets import SidebarSection  # noqa: E402


class WithCalendars(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix="kairos-sidebar-"))
        self.manager = SyncManager(
            store=Storage(self.directory / "cache.db"),
            accounts=AccountStore(self.directory / "accounts.json"),
        )
        self.personal = self.manager.calendars()[0]
        self.work = self.manager.add_local_calendar("Work", "#e01b24")
        self.now = datetime.now(tz=local_timezone())

    def tearDown(self):
        self.manager.storage.close()

    def add(self, title, hours_ahead=1, length=1, calendar=None,
            location="", notes="", all_day=False):
        calendar = calendar or self.personal
        start = self.now + timedelta(hours=hours_ahead)
        if all_day:
            start = start_of_day((self.now + timedelta(days=hours_ahead)).date())
            end = start + timedelta(days=length)
        else:
            end = start + timedelta(hours=length)
        event = Event.new(calendar.id, start, end, title, all_day=all_day)
        event.location = location
        event.description = notes
        self.manager.save_event(event)
        return event


class Sections(unittest.TestCase):
    def setUp(self):
        settings.set("sidebar_calendars_expanded", True)

    def test_it_starts_from_the_setting(self):
        settings.set("sidebar_calendars_expanded", False)
        section = SidebarSection("Calendars", Gtk.Box(),
                                 settings_key="sidebar_calendars_expanded")
        self.assertFalse(section.expanded)

    def test_collapsing_hides_the_child(self):
        section = SidebarSection("Calendars", Gtk.Box(),
                                 settings_key="sidebar_calendars_expanded")
        self.assertTrue(section.expanded)
        section.set_expanded(False)
        self.assertFalse(section.expanded)

    def test_the_state_is_remembered(self):
        section = SidebarSection("Calendars", Gtk.Box(),
                                 settings_key="sidebar_calendars_expanded")
        section.set_expanded(False)
        self.assertFalse(settings.get_bool("sidebar_calendars_expanded"))
        section.set_expanded(True)
        self.assertTrue(settings.get_bool("sidebar_calendars_expanded"))

    def test_a_section_without_a_key_does_not_write_settings(self):
        section = SidebarSection("Ad hoc", Gtk.Box())
        section.set_expanded(False)          # must not raise
        self.assertFalse(section.expanded)

    def test_the_arrow_follows_the_state(self):
        section = SidebarSection("Calendars", Gtk.Box())
        section.set_expanded(True)
        self.assertEqual(section._arrow.get_icon_name(), "pan-down-symbolic")
        section.set_expanded(False)
        self.assertEqual(section._arrow.get_icon_name(), "pan-end-symbolic")


class Upcoming(WithCalendars):
    def titles(self, widget):
        """The event titles the list is showing, in order."""
        found = []
        child = widget.get_first_child()
        while child is not None:
            if isinstance(child, Gtk.Button):
                found.append(child.get_tooltip_text().split("\n")[0])
            child = child.get_next_sibling()
        return found

    def test_it_lists_what_is_coming(self):
        self.add("Soon", hours_ahead=1)
        self.add("Later", hours_ahead=3)
        self.assertEqual(self.titles(UpcomingList(self.manager)), ["Soon", "Later"])

    def test_it_is_in_time_order(self):
        self.add("Third", hours_ahead=5)
        self.add("First", hours_ahead=1)
        self.add("Second", hours_ahead=3)
        self.assertEqual(self.titles(UpcomingList(self.manager)),
                         ["First", "Second", "Third"])

    def test_finished_events_are_left_out(self):
        self.add("Over and done", hours_ahead=-5)
        self.add("Still to come", hours_ahead=2)
        self.assertEqual(self.titles(UpcomingList(self.manager)), ["Still to come"])

    def test_an_event_happening_now_is_still_listed(self):
        """It has started, but it is very much what is next."""
        self.add("Underway", hours_ahead=-1, length=3)
        self.assertEqual(self.titles(UpcomingList(self.manager)), ["Underway"])

    def test_it_honours_the_count_limit(self):
        for index in range(10):
            self.add(f"Event {index}", hours_ahead=index + 1)
        settings.set("sidebar_upcoming_count", 3)
        try:
            self.assertEqual(len(self.titles(UpcomingList(self.manager))), 3)
        finally:
            settings.set("sidebar_upcoming_count", 8)

    def test_events_beyond_the_horizon_are_left_out(self):
        self.add("Next year", hours_ahead=24 * 300)
        settings.set("sidebar_upcoming_days", 7)
        try:
            self.assertEqual(self.titles(UpcomingList(self.manager)), [])
        finally:
            settings.set("sidebar_upcoming_days", 30)

    def headings(self, widget):
        """The day headings the list is showing, in order."""
        found = []
        child = widget.get_first_child()
        while child is not None:
            if isinstance(child, Gtk.Label):
                found.append(child.get_label())
            child = child.get_next_sibling()
        return found

    def test_days_are_given_headings(self):
        """The heading names the day the event actually falls on.

        Not "Today" unconditionally: an hour from now is tomorrow if the
        suite runs late in the evening, and this used to fail after 11pm.
        """
        event = self.add("A thing", hours_ahead=1)
        expected = UpcomingList._day_label(event.start.astimezone().date())
        self.assertEqual(self.headings(UpcomingList(self.manager)), [expected])

    def test_events_on_different_days_get_a_heading_each(self):
        self.add("Sooner", hours_ahead=1)
        self.add("Days later", hours_ahead=72)
        self.assertEqual(len(self.headings(UpcomingList(self.manager))), 2)

    def test_an_empty_calendar_says_so(self):
        widget = UpcomingList(self.manager)
        self.assertEqual(self.titles(widget), [])
        child = widget.get_first_child()
        self.assertIsInstance(child, Gtk.Label)
        self.assertEqual(child.get_label(), "Nothing coming up")

    def test_hidden_calendars_are_left_out(self):
        self.add("Work thing", hours_ahead=1, calendar=self.work)
        self.manager.set_calendar_visible(self.work, False)
        self.assertEqual(self.titles(UpcomingList(self.manager)), [])


class Searching(WithCalendars):
    def setUp(self):
        super().setUp()
        self.add("Dentist", location="High Street")
        self.add("Design review", location="Room 3", notes="Go through the grid")
        self.add("Standup", calendar=self.work)

    def titles(self, text):
        return sorted(event.summary for event in self.manager.search(text))

    def test_it_matches_the_title(self):
        self.assertEqual(self.titles("dentist"), ["Dentist"])

    def test_it_is_case_insensitive(self):
        self.assertEqual(self.titles("DENTIST"), ["Dentist"])

    def test_it_matches_the_location(self):
        self.assertEqual(self.titles("high street"), ["Dentist"])

    def test_it_matches_the_notes(self):
        self.assertEqual(self.titles("grid"), ["Design review"])

    def test_it_matches_the_start_of_a_word(self):
        """Results appear while you are still typing the word."""
        self.assertEqual(self.titles("re"), ["Design review"])
        self.assertEqual(self.titles("dent"), ["Dentist"])

    def test_it_does_not_match_the_middle_of_a_word(self):
        """"ent" is inside "Dentist", but a search box matches word starts."""
        self.assertEqual(self.titles("ent"), [])

    def test_several_words_must_all_match(self):
        self.assertEqual(self.titles("design review"), ["Design review"])
        self.assertEqual(self.titles("design dentist"), [])

    def test_query_syntax_is_treated_as_plain_text(self):
        """FTS5 would read these as operators; a user means them literally."""
        for term in ("AND", "OR", "NOT", "*", "^", "(", ")", '"', ":", "-"):
            with self.subTest(term=term):
                self.assertEqual(self.titles(term), [],
                                 f"“{term}” was read as query syntax")

    def test_a_quote_does_not_break_the_query(self):
        self.add("Sam's party")
        self.assertEqual(self.titles('sam'), ["Sam's party"])

    def test_icalendar_boilerplate_does_not_match(self):
        """The bug: every event carries CALSCALE:GREGORIAN."""
        for term in ("gregorian", "calscale", "dtstart", "vcalendar",
                     "prodid", "sequence", "dtstamp", "begin"):
            with self.subTest(term=term):
                self.assertEqual(self.titles(term), [],
                                 f"“{term}” matched iCalendar boilerplate")

    def test_the_uid_does_not_match(self):
        event = self.manager.search("dentist")[0]
        self.assertEqual(self.titles(event.uid), [])

    def test_nothing_matches_nonsense(self):
        self.assertEqual(self.titles("zzzznotathing"), [])

    def test_wildcards_are_literal(self):
        self.assertEqual(self.titles("%"), [])
        self.assertEqual(self.titles("_"), [])

    def test_an_empty_search_returns_nothing(self):
        self.assertEqual(self.titles("   "), [])


class SearchResults(WithCalendars):
    def rows(self, view):
        found = []
        child = view._list.get_first_child()
        while child is not None:
            if isinstance(child, Gtk.Button):
                found.append(child)
            child = child.get_next_sibling()
        return found

    def test_an_empty_query_prompts_rather_than_listing(self):
        view = SearchView(self.manager)
        view.search("")
        self.assertEqual(self.rows(view), [])

    def test_results_are_listed(self):
        self.add("Dentist")
        view = SearchView(self.manager)
        view.search("dentist")
        self.assertEqual(len(self.rows(view)), 1)

    def test_no_matches_says_so(self):
        self.add("Dentist")
        view = SearchView(self.manager)
        view.search("nothing like this")
        self.assertEqual(self.rows(view), [])

    def test_a_repeating_event_is_dated_by_its_next_occurrence(self):
        """Not by the master's start, which may be years ago."""
        long_ago = self.now - timedelta(days=400)
        event = Event.new(self.personal.id, long_ago, long_ago + timedelta(hours=1),
                          "Weekly sync")
        event.rrule = "FREQ=WEEKLY"
        self.manager.save_event(event)

        view = SearchView(self.manager)
        occurrence = view._representative(self.manager.search("weekly sync")[0])
        self.assertIsNotNone(occurrence)
        self.assertGreaterEqual(occurrence.start, self.now - timedelta(days=1))

    def test_upcoming_results_come_before_past_ones(self):
        self.add("Past meeting", hours_ahead=-72)
        self.add("Future meeting", hours_ahead=72)
        view = SearchView(self.manager)
        results = view._results("meeting")
        self.assertEqual([o.summary for o in results],
                         ["Future meeting", "Past meeting"])


if __name__ == "__main__":
    unittest.main()


class TheSidebarComponent(WithCalendars):
    """The sidebar as its own widget, talking to the window by signal."""

    def sidebar(self):
        from kairos.ui.sidebar import Sidebar
        return Sidebar(self.manager)

    def test_it_lists_the_calendars(self):
        from gi.repository import Gtk as _Gtk
        bar = self.sidebar()
        names = [w.get_label() for w in _walk(bar)
                 if isinstance(w, _Gtk.Label) and w.get_label() in ("Personal", "Work")]
        self.assertIn("Work", names)

    def test_clicking_a_day_is_reported_once(self):
        bar = self.sidebar()
        seen = []
        bar.connect("date-selected", lambda _b, day: seen.append(day))
        bar._on_day_selected(bar._mini_calendar)
        self.assertEqual(len(seen), 1)

    def test_setting_the_day_does_not_report_it_back(self):
        """Otherwise the window's own update returns as a user selection."""
        from datetime import date, timedelta
        bar = self.sidebar()
        seen = []
        bar.connect("date-selected", lambda _b, day: seen.append(day))
        bar.select_day(date.today() + timedelta(days=3))
        self.assertEqual(seen, [], "the sidebar answered its own update")

    def test_the_manage_button_asks_the_window(self):
        from gi.repository import Gtk as _Gtk
        bar = self.sidebar()
        asked = []
        bar.connect("manage-requested", lambda *_: asked.append(True))
        for widget in _walk(bar):
            if (isinstance(widget, _Gtk.Button)
                    and getattr(widget, "kairos_accessible_label", "")
                    == "Manage calendars"):
                widget.emit("clicked")
        self.assertEqual(asked, [True])

    def test_an_upcoming_event_is_passed_through(self):
        from gi.repository import Gtk as _Gtk
        self.add("Soon", hours_ahead=1)
        bar = self.sidebar()
        seen = []
        bar.connect("event-activated", lambda _b, occ, _w: seen.append(occ.summary))
        for widget in _walk(bar.upcoming):
            if isinstance(widget, _Gtk.Button):
                widget.emit("clicked")
                break
        self.assertEqual(seen, ["Soon"])


def _walk(widget):
    yield widget
    child = widget.get_first_child()
    while child is not None:
        yield from _walk(child)
        child = child.get_next_sibling()
