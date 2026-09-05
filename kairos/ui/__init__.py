"""Everything the user sees.

    widgets.py          small reusable pieces (colour swatches, date pickers)
    month_view.py       the month grid
    week_view.py        the week and day grids
    agenda_view.py      the scrolling list of upcoming events
    event_popover.py    the bubble shown when an event is clicked
    event_editor.py     the create/edit event dialog
    calendar_manager.py the calendar and account list
    account_dialog.py   adding a CalDAV/WebDAV account
    preferences.py      the preferences dialog
    window.py           the main window, which ties the above together

The views share one contract, described in :mod:`kairos.ui.window`: each is a
``Gtk.Widget`` with ``set_date(day)``, ``refresh()`` and a ``heading`` property,
and each emits ``event-activated`` and ``create-requested``.
"""
