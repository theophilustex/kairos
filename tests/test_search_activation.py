"""Opening an event from the search results.

The bug these exist for: clicking a search result segfaulted the whole
application.  Leaving search clears the entry, which re-runs the search with
an empty term and empties the results list — so the row that had just been
clicked was destroyed, and the detail popover, still parented to it, crashed
GTK the moment it tried to appear:

    Gtk-WARNING: Finalizing GtkButton, but it still has children left:
       - EventPopover
    Fatal Python error: Segmentation fault

The fix anchors the bubble to the event's chip in the view being switched
to, not to the row that is about to be thrown away.
"""

import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

Adw.init()

from kairos.accounts import AccountStore  # noqa: E402
from kairos.models import Event, local_timezone  # noqa: E402
from kairos.storage import Storage  # noqa: E402
from kairos.sync import SyncManager  # noqa: E402
from kairos.ui.widgets import find_event_widget, mark_event_widget  # noqa: E402
from kairos.ui.window import CalendarWindow  # noqa: E402


class _NullTheme:
    """Stands in for ThemeManager, which the window only stores."""


def drain() -> None:
    """Run the pending idle callbacks, which is where the popover opens."""
    context = GLib.MainContext.default()
    for _ in range(200):
        if not context.pending():
            break
        context.iteration(False)


def pump_until(predicate, seconds: float = 2.0):
    """Run the main loop until ``predicate`` returns something truthy.

    ``Gtk.SearchEntry`` debounces ``search-changed``, so the results do not
    exist the instant the text is set; this waits for them rather than
    guessing at a sleep.
    """
    context = GLib.MainContext.default()
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        found = predicate()
        if found:
            return found
        context.iteration(False)
        time.sleep(0.005)
    return predicate()


class OpeningASearchResult(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix="kairos-search-open-"))
        self.sync = SyncManager(
            store=Storage(self.directory / "cache.db"),
            accounts=AccountStore(self.directory / "accounts.json"),
        )
        self.calendar = self.sync.calendars()[0]
        start = datetime.now(tz=local_timezone()) + timedelta(days=1)
        self.event = Event.new(self.calendar.id, start,
                               start + timedelta(hours=1), "Dentist")
        self.sync.save_event(self.event)

        self.application = Adw.Application(application_id="org.kairos.SearchTest")
        self.window = CalendarWindow(self.application, self.sync, _NullTheme())

    def tearDown(self):
        self.sync.storage.close()

    def result_button(self):
        child = self.window._search_view._list.get_first_child()
        while child is not None:
            if isinstance(child, Gtk.Button):
                return child
            child = child.get_next_sibling()
        return None

    def click_the_first_result(self):
        button = self.show_results()
        button.emit("clicked")
        drain()

    def show_results(self):
        """Open search, type, and wait for the debounced results to appear."""
        self.window.start_search()
        self.window._search_entry.set_text("dentist")
        button = pump_until(self.result_button)
        self.assertIsNotNone(button, "the search found nothing to click")
        return button

    def test_clicking_a_result_does_not_crash(self):
        """It used to take the process down with a segfault."""
        self.click_the_first_result()

    def test_clicking_a_result_opens_the_event(self):
        self.click_the_first_result()
        self.assertIsNotNone(self.window._open_popover,
                             "no detail bubble was opened")

    def test_the_bubble_is_not_anchored_to_the_discarded_row(self):
        """The row is destroyed on leaving search; anchoring there crashed."""
        row = self.show_results()
        row.emit("clicked")
        drain()
        popover = self.window._open_popover
        self.assertIsNotNone(popover)
        self.assertIsNot(popover.get_parent(), row)

    def test_the_bubble_is_anchored_inside_the_window(self):
        self.click_the_first_result()
        parent = self.window._open_popover.get_parent()
        self.assertIsNotNone(parent.get_root(),
                             "anchored to a widget outside the window")

    def test_clicking_a_result_leaves_search(self):
        self.click_the_first_result()
        self.assertNotEqual(self.window._stack.get_visible_child_name(),
                            self.window.SEARCH_PAGE)

    def test_clicking_a_result_goes_to_the_events_day(self):
        self.click_the_first_result()
        self.assertEqual(self.window._current_day,
                         self.event.start.astimezone().date())

    def test_several_results_can_be_opened_in_a_row(self):
        """The popover is torn down and rebuilt each time; that used to leak."""
        for _ in range(3):
            self.click_the_first_result()
            self.assertIsNotNone(self.window._open_popover)


class TheBubbleAndRebuilds(OpeningASearchResult):
    """A view rebuild must never leave a popover on a discarded chip.

    The bubble is parented to the chip it points at, and every one of these
    throws that chip away.  Finalising a widget that still has a popover
    attached is a segfault, not an exception, so each of these is a crash
    test as much as a behaviour test.
    """

    def open_a_bubble(self):
        self.click_the_first_result()
        self.assertIsNotNone(self.window._open_popover)

    def test_switching_view_closes_it(self):
        self.open_a_bubble()
        self.window.show_view("week")
        drain()
        self.assertIsNone(self.window._open_popover)

    def test_going_to_another_day_closes_it(self):
        self.open_a_bubble()
        self.window.go_to_day(self.event.start.date() + timedelta(days=40))
        drain()
        self.assertIsNone(self.window._open_popover)

    def test_navigating_closes_it(self):
        self.open_a_bubble()
        self.window._navigate(1)
        drain()
        self.assertIsNone(self.window._open_popover)

    def test_a_refresh_closes_it(self):
        """A background sync finishing must not crash an open bubble."""
        self.open_a_bubble()
        self.window.refresh()
        drain()
        self.assertIsNone(self.window._open_popover)

    def test_searching_again_while_one_is_open(self):
        """This is the sequence that still crashed after the first fix."""
        self.open_a_bubble()
        self.click_the_first_result()
        self.assertIsNotNone(self.window._open_popover)


class FindingAChip(unittest.TestCase):
    """The lookup that replaced holding on to the clicked widget."""

    def setUp(self):
        start = datetime.now(tz=local_timezone())
        self.event = Event.new("cal", start, start + timedelta(hours=1), "Thing")
        from kairos.models import Occurrence
        self.occurrence = Occurrence(event=self.event, start=start,
                                     end=start + timedelta(hours=1))

    def test_it_finds_a_tagged_widget(self):
        box, button = Gtk.Box(), Gtk.Button()
        mark_event_widget(button, self.occurrence)
        box.append(button)
        self.assertIs(find_event_widget(box, self.occurrence), button)

    def test_it_finds_one_nested_several_levels_down(self):
        outer, inner, button = Gtk.Box(), Gtk.Box(), Gtk.Button()
        mark_event_widget(button, self.occurrence)
        inner.append(button)
        outer.append(inner)
        self.assertIs(find_event_widget(outer, self.occurrence), button)

    def test_it_returns_none_when_the_event_is_not_drawn(self):
        box = Gtk.Box()
        box.append(Gtk.Button())
        self.assertIsNone(find_event_widget(box, self.occurrence))

    def test_a_different_occurrence_of_the_same_event_does_not_match(self):
        """A weekly meeting has many chips; the right one must be picked."""
        from kairos.models import Occurrence
        later = self.occurrence.start + timedelta(days=7)
        other = Occurrence(event=self.event, start=later,
                           end=later + timedelta(hours=1))
        box, button = Gtk.Box(), Gtk.Button()
        mark_event_widget(button, other)
        box.append(button)
        self.assertIsNone(find_event_widget(box, self.occurrence))
        self.assertIs(find_event_widget(box, other), button)


if __name__ == "__main__":
    unittest.main()


class UndoingADelete(OpeningASearchResult):
    """Deleting is the one destructive action, so it offers an undo."""

    def make_repeating(self):
        start = self.event.start - timedelta(days=7)
        event = Event.new(self.calendar.id, start, start + timedelta(hours=1),
                          "Standup")
        event.rrule = "FREQ=WEEKLY"
        self.sync.save_event(event)
        return self.sync.storage.get_event(self.calendar.id, event.uid)

    def occurrences(self, event, days=21):
        from kairos import recurrence
        from kairos.models import local_timezone
        now = datetime.now(tz=local_timezone())
        return recurrence.expand([event], now - timedelta(days=8),
                                 now + timedelta(days=days))

    def test_restoring_brings_a_deleted_event_back(self):
        before = self.sync.storage.get_event(self.calendar.id, self.event.uid)
        self.sync.delete_event(before)
        self.assertIsNone(self.sync.storage.get_event(self.calendar.id,
                                                      self.event.uid))
        self.sync.restore_event(before)
        restored = self.sync.storage.get_event(self.calendar.id, self.event.uid)
        self.assertIsNotNone(restored)
        self.assertEqual(restored.summary, "Dentist")

    def test_restoring_brings_back_one_deleted_occurrence(self):
        """The undo has to put the EXDATE back, not recreate an event."""
        series = self.make_repeating()
        before = self.occurrences(series)
        self.assertGreaterEqual(len(before), 3)

        self.sync.delete_occurrence(before[1], scope=self.sync.THIS_EVENT)
        after = self.occurrences(
            self.sync.storage.get_event(self.calendar.id, series.uid))
        self.assertEqual(len(after), len(before) - 1)

        self.sync.restore_event(series)
        again = self.occurrences(
            self.sync.storage.get_event(self.calendar.id, series.uid))
        self.assertEqual(len(again), len(before))

    def test_the_window_offers_an_undo_toast(self):
        before = self.sync.storage.get_event(self.calendar.id, self.event.uid)
        self.sync.delete_event(before)
        self.window._offer_undo("Event deleted", before)   # must not raise
        self.sync.restore_event(before)
        self.assertIsNotNone(
            self.sync.storage.get_event(self.calendar.id, self.event.uid))
