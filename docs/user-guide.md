# User guide

- [The window](#the-window)
- [Views](#views)
- [Creating and editing events](#creating-and-editing-events)
- [Reminders](#reminders)
- [Running in the background](#running-in-the-background)
- [Keyboard shortcuts](#keyboard-shortcuts)
- [Gestures](#gestures)
- [What Kairos does not do](#what-kairos-does-not-do)

---

## The window

A sidebar on the left, the calendar on the right.

The sidebar has a month at the top — click any date to jump to it — and below
it the list of calendars, each with a checkbox that shows or hides it. Hiding
a calendar only changes what you see; its reminders still arrive, and nothing
is deleted.

The header bar has, left to right: **+** for a new event, **‹ Today ›** for
moving about, the current period, a sync button, the view switcher, and the
main menu.

On a window narrower than 1000px the sidebar folds away behind a back button
and the view switcher becomes a dropdown, so the calendar keeps the space.

---

## Views

Four of them, switchable with <kbd>Ctrl</kbd>+<kbd>1</kbd> to
<kbd>Ctrl</kbd>+<kbd>4</kbd>.

**Month** — six weeks at a time. Each day shows a few events and then
"+N more"; how many is [configurable](configuration.md). Timed events appear
as a coloured dot with the time; all-day and multi-day events as a solid bar
across the days they cover.

**Week** and **Day** — a timed grid. Events sit at their real times, and
overlapping ones are placed side by side automatically. A line across the grid
shows the current time. All-day events sit in a strip above it. The view opens
scrolled to about an hour before now rather than at midnight.

**Agenda** — a plain list of what is coming, day by day, skipping empty days.
The most useful view in a narrow window.

Click a day heading in the week view, or "+N more" in the month view, to jump
to that day.

---

## Creating and editing events

**To create one**: press **+**, or <kbd>Ctrl</kbd>+<kbd>N</kbd>, or
double-click a day in the month view or a time in the week view. Double-clicking
puts the event where you clicked.

**To open one**: click it. A bubble appears with the details and **Edit** and
**Delete**.

The editor has:

- **Title**, **Location** and **Notes**
- **Calendar** — which calendar it belongs to. Read-only calendars are not offered.
- **All day** — hides the times and switches to whole days. The end date shown
  is the last day the event covers, which is what people mean, even though
  iCalendar stores it differently underneath.
- **Starts** / **Ends** — a date button and a time button. In 12-hour mode the
  time picker offers 1–12 with AM/PM; in 24-hour mode, 0–23.
- **Repeats** — daily, weekly, fortnightly, monthly, yearly, or every weekday.
  A rule from a server that Kairos cannot express is preserved untouched and
  shown as "(from the server)".
- **Reminders** — any number; see below.

Pressing <kbd>Enter</kbd> in the title field saves.

Editing or deleting a repeating event affects **the whole series**. The delete
dialog says so.

### Times and timezones

Events are always shown on your clock. A server usually stores events in UTC,
so a 7pm meeting in New York is stored as 11pm UTC; Kairos converts on the way
in. Editing such an event and saving it rewrites the start in your timezone —
the same instant, spelled differently.

---

## Reminders

Each event carries its own reminders, and each is an offset before the start.
The dropdown lists the usual ones; **Custom…** takes any number of minutes,
hours, days or weeks. An offset that came from another client — five days,
say — is shown in the list in words, whether or not Kairos would have offered
it.

A due reminder arrives twice over:

- a **desktop notification**, as any application would send; and
- an **alert window** that asks your desktop to bring it to the front, even
  when Kairos is in the background or its window is closed.

The second exists because a notification slides away after a few seconds and
is very easy to miss. The window stays until you answer it:

- **Snooze** — 5 minutes to tomorrow. Snoozes are remembered on disk, so
  "remind me in an hour" survives closing Kairos or suspending the machine.
- **Dismiss** — done with it.
- **Show in calendar** — opens Kairos on that day.

Reminders that fall due together share one window rather than opening several.

If you would rather have notifications alone, turn off *Show an alert window*
in **Preferences → Reminders**. That page also has **Send a test reminder**,
which fires a real one through both routes — the quickest way to find out what
your desktop actually permits.

> Whether the window really takes focus is your desktop's decision, not
> Kairos's. See [troubleshooting](troubleshooting.md#the-alert-window-opens-behind-what-i-am-doing).

---

## Running in the background

A calendar that is not running cannot remind you of anything, so by default
closing the window does not quit Kairos. It carries on in the taskbar, syncing
and firing reminders.

**The taskbar icon** has a menu: *Open Kairos*, *New event*, *Sync now* and
*Quit Kairos*. Clicking the icon opens the window. Not every desktop has a
system tray — GNOME needs an extension — and where there is none the icon
simply does not appear; Kairos still runs, and launching it again from your
applications menu brings the window back.

**To quit properly**: <kbd>Ctrl</kbd>+<kbd>Q</kbd>, or *Quit Kairos* from the
tray menu.

**To have it start with your session**: Preferences → General → Running →
*Start automatically when you log in*. That writes
`~/.config/autostart/org.kairos.Calendar.desktop`, which starts Kairos in the
background so you get reminders without a window appearing in your face.

**To go back to ordinary behaviour**: turn off *Keep running when the window
is closed* in the same place, and closing the window will quit.

---

## Keyboard shortcuts

| | |
|---|---|
| <kbd>Ctrl</kbd>+<kbd>N</kbd> | New event |
| <kbd>Ctrl</kbd>+<kbd>T</kbd> | Go to today |
| <kbd>Ctrl</kbd>+<kbd>R</kbd> or <kbd>F5</kbd> | Sync now |
| <kbd>Ctrl</kbd>+<kbd>1</kbd> … <kbd>Ctrl</kbd>+<kbd>4</kbd> | Month, week, day, agenda |
| <kbd>Alt</kbd>+<kbd>←</kbd> / <kbd>→</kbd> | Previous / next period |
| <kbd>Ctrl</kbd>+<kbd>Page Up</kbd> / <kbd>Page Down</kbd> | The same |
| <kbd>Ctrl</kbd>+<kbd>L</kbd> | Manage calendars |
| <kbd>Ctrl</kbd>+<kbd>,</kbd> | Preferences |
| <kbd>Ctrl</kbd>+<kbd>W</kbd> | Close the window |
| <kbd>Ctrl</kbd>+<kbd>Q</kbd> | Quit |

They are all in one table at the top of
[`kairos/app.py`](../kairos/app.py) if you want different ones.

---

## Gestures

Swipe left or right — two fingers on a touchpad, or a flick on a touchscreen —
to move to the next or previous period, in whatever unit the view is showing.
Vertical scrolling is untouched, and one swipe moves one period however hard
you flick.

---

## What Kairos does not do

Stated plainly, because finding out later is annoying:

- **Modified instances of a repeating event are ignored.** Move one occurrence
  of a weekly meeting in another client and Kairos shows the series as though
  you had not. Editing or deleting a repeating event here affects the whole
  series.
- **No tasks or notes** (VTODO, VJOURNAL). Calendars holding only tasks are
  skipped when an account is added.
- **No invitations, attendees, RSVPs or free/busy.** Kairos reads and writes
  events; it does not do scheduling.
- **No timezone editor.** Events are read in their own timezone and shown in
  yours, but you cannot author an event *in* another timezone.
- **Custom repeat rules are read, not composed.** A rule outside the presets
  is preserved but cannot be built in the interface.
- **No year view**, so swiping never moves a whole year.
- **The sidebar month starts its week where your system locale says**, which
  can differ from the main grid if you have overridden *Week starts on*. GTK's
  calendar widget has no setting for it.
