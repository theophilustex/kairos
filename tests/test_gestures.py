"""Swiping sideways to move through time.

The awkward part is that a touchpad delivers a stream of small deltas rather
than one gesture, so the controller has to add them up, fire once, and then
ignore the rest of the same flick — while never interfering with ordinary
vertical scrolling.
"""

import unittest

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402

Adw.init()

from kairos.ui.widgets import (  # noqa: E402
    SWIPE_COOLDOWN_MS,
    SWIPE_THRESHOLD,
    add_horizontal_swipe,
)


class SwipeTestCase(unittest.TestCase):
    def setUp(self):
        self.fired = []
        self.widget = Gtk.Box()
        add_horizontal_swipe(self.widget, self.fired.append)
        self.scroll = next(
            c for c in self.widget.observe_controllers()
            if isinstance(c, Gtk.EventControllerScroll)
        )

    def push(self, delta_x, delta_y=0.0, times=1):
        """Feed the controller a run of scroll deltas; returns the last verdict."""
        handled = False
        for _ in range(times):
            handled = self.scroll.emit("scroll", delta_x, delta_y)
        return handled

    def nudge_past_threshold(self, direction=1):
        self.push(direction * (SWIPE_THRESHOLD + 0.1))


class VerticalScrollingIsLeftAlone(SwipeTestCase):
    def test_a_vertical_scroll_is_not_handled(self):
        self.assertFalse(self.push(0.0, 1.0))

    def test_a_vertical_scroll_never_pages(self):
        self.push(0.0, 1.0, times=20)
        self.assertEqual(self.fired, [])

    def test_a_mostly_vertical_diagonal_is_left_alone(self):
        self.push(0.3, 4.0, times=20)
        self.assertEqual(self.fired, [])


class HorizontalSwiping(SwipeTestCase):
    def test_one_big_swipe_right_moves_forward(self):
        self.nudge_past_threshold(1)
        self.assertEqual(self.fired, [1])

    def test_one_big_swipe_left_moves_back(self):
        self.nudge_past_threshold(-1)
        self.assertEqual(self.fired, [-1])

    def test_small_deltas_add_up(self):
        """A touchpad sends many small deltas, not one large one."""
        self.push(0.4, times=3)
        self.assertEqual(self.fired, [], "fired before the threshold")
        self.push(0.4, times=4)
        self.assertEqual(self.fired, [1])

    def test_a_nudge_below_the_threshold_does_nothing(self):
        self.push(SWIPE_THRESHOLD * 0.3)
        self.assertEqual(self.fired, [])

    def test_a_horizontal_scroll_is_consumed(self):
        self.assertTrue(self.push(1.0))

    def test_one_flick_turns_one_page(self):
        """Without a cooldown a single flick would race through months."""
        self.push(1.0, times=40)
        self.assertEqual(self.fired, [1])

    def test_the_accumulator_resets_between_gestures(self):
        self.push(0.4, times=3)          # not enough
        self.scroll.emit("scroll-end")
        self.push(0.4, times=3)          # also not enough, starting fresh
        self.assertEqual(self.fired, [])

    def test_direction_changes_are_not_carried_over(self):
        self.push(1.0)                   # partway right
        self.push(-1.0)                  # then back left; nets out
        self.assertEqual(self.fired, [])


class Cooldown(SwipeTestCase):
    def test_a_second_swipe_works_once_the_cooldown_has_passed(self):
        import time
        self.nudge_past_threshold(1)
        self.assertEqual(self.fired, [1])
        time.sleep(SWIPE_COOLDOWN_MS / 1000 + 0.05)
        self.nudge_past_threshold(1)
        self.assertEqual(self.fired, [1, 1])


class AttachedToTheViews(unittest.TestCase):
    """Every view should be swipeable, not just the one that was tested."""

    def test_each_view_has_both_controllers(self):
        import tempfile
        from pathlib import Path
        from kairos.accounts import AccountStore
        from kairos.storage import Storage
        from kairos.sync import SyncManager
        from kairos.ui.window import CalendarWindow

        directory = Path(tempfile.mkdtemp(prefix="kairos-swipe-"))
        manager = SyncManager(
            store=Storage(directory / "cache.db"),
            accounts=AccountStore(directory / "accounts.json"),
        )
        application = Adw.Application(application_id="org.kairos.SwipeTest")
        window = CalendarWindow(application, manager, _NullTheme())
        try:
            for key, view in window._views.items():
                with self.subTest(view=key):
                    kinds = {type(c) for c in view.observe_controllers()}
                    self.assertIn(Gtk.EventControllerScroll, kinds)
                    self.assertIn(Gtk.GestureSwipe, kinds)
        finally:
            manager.storage.close()


class _NullTheme:
    """Stands in for ThemeManager, which the window only stores."""

    def apply(self):
        pass

    def on_settings_changed(self, key):
        pass


if __name__ == "__main__":
    unittest.main()
