# Configuration

Everything Kairos stores is a plain file you can open in a text editor.
Nothing is in a binary blob or a registry, and the only thing not in a file is
your passwords, which are in the system keyring.

- [Where things live](#where-things-live)
- [Preferences](#preferences)
- [The settings file](#the-settings-file)
- [Every setting](#every-setting)
- [Your own stylesheet](#your-own-stylesheet)
- [The accounts file](#the-accounts-file)

---

## Where things live

```
~/.config/kairos/settings.json              preferences
~/.config/kairos/accounts.json              accounts, without passwords
~/.config/kairos/custom.css                 your stylesheet
~/.cache/kairos/cache.db                    events, and local calendars
~/.cache/kairos/fired_alarms.json           reminders already shown
~/.cache/kairos/snoozed_reminders.json      reminders you snoozed
~/.config/autostart/org.kairos.Calendar.desktop   if "start at login" is on
```

These honour `XDG_CONFIG_HOME` and `XDG_CACHE_HOME` if you set them. Config
files are written with mode `0600` — readable only by you — and atomically, so
a crash cannot leave half a file behind.

---

## Preferences

<kbd>Ctrl</kbd>+<kbd>,</kbd>, in four pages:

- **General** — views, week start, clock, new-event defaults, grid density,
  and whether Kairos keeps running in the background.
- **Appearance** — light/dark, accent colour, text size, compact spacing, and
  buttons to open `custom.css` and the config folder.
- **Reminders** — notifications, the alert window, default snooze, and a test
  button.
- **Sync & security** — sync interval, how much history to keep, network
  timeout, and the two security switches.

Every row maps to exactly one key in `settings.json` and writes it
immediately. There is no OK button and nothing to apply.

---

## The settings file

`~/.config/kairos/settings.json` is ordinary JSON with one key per preference.
A few settings are only reachable by editing it — `week_view_start_hour`, for
instance.

Kairos is forgiving about what it finds there:

- a value outside the allowed range is **clamped**;
- a value of the wrong type, or not one of the allowed choices, falls back to
  the **default**;
- a key it does not recognise is **kept**, so a file written by a newer
  version survives being opened by an older one;
- a file that is not valid JSON at all is ignored, and Kairos starts with
  defaults rather than refusing to run.

In other words, a typo degrades gracefully. Changes made in the dialog are
written straight away; changes you make by hand are read at the next start.

---

## Every setting

Defaults are what Kairos uses if the key is absent.

### Appearance

<img src="images/light-theme.png" alt="The week view in the light theme">

*The light theme. `theme` follows your desktop unless you pin it, and
`accent_color` tints today, selections and the current-time line.*

| Key | Default | Values | |
|---|---|---|---|
| `theme` | `"system"` | `system` / `light` / `dark` | Follow the desktop, or force one. |
| `accent_color` | `"#3584e4"` | any CSS colour | Tints today, selections and the current-time line. |
| `font_scale` | `1.0` | 0.75–1.5 | Multiplies the whole window's font size. |
| `compact_mode` | `false` | true / false | Tighter padding everywhere. |
| `rounded_event_chips` | `true` | true / false | Square chips read denser. |

### Calendar layout

| Key | Default | Values | |
|---|---|---|---|
| `default_view` | `"month"` | `month` / `week` / `day` / `agenda` | Which view Kairos opens in. |
| `first_day_of_week` | `"monday"` | `monday` / `sunday` / `saturday` | Applies to the main grid. |
| `time_format` | `"24h"` | `24h` / `12h` | Also changes the time *pickers*, not just the labels. |
| `show_week_numbers` | `false` | true / false | A thin ISO week column in the month view. |
| `highlight_weekends` | `true` | true / false | Shades Saturday and Sunday. |
| `week_view_start_hour` | `7` | 0–23 | Hour scrolled to when the week view is not showing today. |
| `hour_height` | `48` | 24–160 | Pixels per hour in the week and day views. |
| `max_chips_per_day` | `4` | 1–12 | Events shown in a month cell before "+N more". |
| `agenda_days` | `30` | 1–365 | How far the agenda view looks ahead. |

### New events

| Key | Default | Values | |
|---|---|---|---|
| `default_event_duration_minutes` | `60` | 5–1440 | Length of an event made by double-clicking. |
| `default_alarm_minutes` | `10` | 0–40320 | Reminder a new event starts with. |
| `week_starts_scrolled_to_now` | `true` | true / false | Open the week view near now rather than at midnight. |

### Sidebar

| Key | Default | Values | |
|---|---|---|---|
| `sidebar_show_upcoming` | `true` | true / false | Show the "Up next" list at all. |
| `sidebar_upcoming_count` | `8` | 1–30 | How many events it lists. |
| `sidebar_upcoming_days` | `30` | 1–365 | How far ahead it looks. |
| `sidebar_upcoming_expanded` | `true` | true / false | Whether that section is folded. Set by clicking its heading. |
| `sidebar_calendars_expanded` | `true` | true / false | The same, for the calendar list. |

### Running

| Key | Default | Values | |
|---|---|---|---|
| `run_in_background` | `true` | true / false | Closing the window leaves Kairos in the taskbar. |

Starting at login is not a key here: it is the presence of
`~/.config/autostart/org.kairos.Calendar.desktop`, so there is nothing that
can disagree with reality.

### Reminders

| Key | Default | Values | |
|---|---|---|---|
| `notifications_enabled` | `true` | true / false | The master switch for all reminders. |
| `notification_lookahead_minutes` | `60` | 5–1440 | How far ahead alarms are planned at a time. |
| `reminder_alert_window` | `true` | true / false | The window that comes to the front. Off means notifications alone. |
| `reminder_snooze_minutes` | `10` | 1–1440 | The Snooze button's default offset. |

### Syncing

| Key | Default | Values | |
|---|---|---|---|
| `sync_interval_minutes` | `15` | 0–1440 | 0 means only when you ask. |
| `sync_on_startup` | `true` | true / false | Sync a couple of seconds after the window appears. |
| `sync_window_past_days` | `365` | 0–3650 | How much history to keep cached. |
| `sync_window_future_days` | `730` | 1–3650 | How far ahead to download. |
| `network_timeout_seconds` | `30` | 5–300 | How long to wait for a server. |

### Security

| Key | Default | Values | |
|---|---|---|---|
| `allow_deleting_events` | `true` | true / false | Off means Kairos never deletes anything. See below. |
| `ca_certificate_path` | `""` | a path | A PEM file for the authority that signed your server's certificate. |
| `verify_tls_certificates` | `true` | true / false | Turning this off is a bad idea; see [security](security.md). |
| `allow_insecure_http` | `false` | true / false | Even on, plain http is only allowed to a private address. |

---

## Your own stylesheet

`~/.config/kairos/custom.css` is loaded after everything else, so anything in
it wins. **It is reloaded the moment you save it** — no restart. Kairos writes
an example file on first run listing the class names.

```css
/* Denser, squarer event chips */
.kairos-event-chip {
    border-radius: 2px;
    font-size: 0.9em;
}

/* Make weekends obvious */
.kairos-day-cell.kairos-weekend {
    background-color: alpha(@accent_bg_color, 0.08);
}

/* A red current-time line instead of the accent colour */
.kairos-now-line { background-color: #e01b24; }

/* Roomier day cells */
.kairos-day-cell { padding: 8px; }
```

### Class names

These are stable; treat them as the styling API.

| Class | |
|---|---|
| `.kairos-day-cell` | One day in the month grid. Also `.today`, `.selected`, `.outside`, `.kairos-weekend`. |
| `.kairos-day-number` | The number in the corner of a day cell. |
| `.kairos-event-chip` | An event in the month grid. |
| `.kairos-chip-timed` | ... when it is a timed event (dot plus time) rather than a banner. |
| `.kairos-chip-more` | The "+N more" link. |
| `.kairos-event-block` | An event in the week or day grid. |
| `.kairos-all-day-chip` | An all-day event in the strip above the week grid. |
| `.kairos-weekday-heading` | "Mon", "Tue", … above the grid. |
| `.kairos-week-number` | The ISO week number column. |
| `.kairos-hour-label` | The times down the side of the week view. |
| `.kairos-hour-row` | An hour rule in the week grid. |
| `.kairos-day-column` | One day column in the week grid. |
| `.kairos-now-line` | The current-time line. |
| `.kairos-agenda-day` | A day heading in the agenda view. Also `.today`. |
| `.kairos-agenda-row` | One event row in the agenda. |
| `.kairos-agenda-time` | The time column in the agenda. |
| `.kairos-calendar-dot` | The colour swatch beside a calendar's name. |
| `.kairos-section-header` | A foldable sidebar heading. |
| `.kairos-section-arrow` | Its disclosure triangle. |
| `.kairos-upcoming` | The "Up next" list. |
| `.kairos-upcoming-day` | A day heading within it. |
| `.kairos-upcoming-row` | One event in it. |
| `.kairos-upcoming-bar` | The calendar-coloured bar beside that event. |
| `.kairos-upcoming-title` / `.kairos-upcoming-time` | Its two lines of text. |
| `.kairos-mini-calendar` | The month in the sidebar. |
| `.kairos-sidebar-heading` | The "CALENDARS" heading. |
| `.kairos-detail-title` | The event title in the detail bubble. |
| `.kairos-detail-meta` | Secondary text in the bubble. |
| `.kairos-alert-title` | The event title in the reminder alert. |
| `.kairos-alert-countdown` | The "In 12 minutes" line. |
| `.kairos-offline-badge` | The "not yet synced" badge. |
| `.kairos-empty-state` | The "nothing to show" placeholder. |

libadwaita's [named colours](https://gnome.pages.gitlab.gnome.org/libadwaita/doc/main/named-colors.html)
— `@accent_bg_color`, `@card_bg_color`, `@window_bg_color` and the rest — are
available, and following them is what keeps a custom stylesheet working in
both light and dark.

A syntax error in your CSS is logged and the offending rule ignored; it will
not stop Kairos from starting.

---

## The accounts file

`~/.config/kairos/accounts.json`, editable by hand:

```json
[
  {
    "id": "a1b2c3d4e5f6",
    "name": "Fastmail",
    "kind": "caldav",
    "url": "https://caldav.fastmail.com/dav/",
    "username": "me@example.com",
    "verify_tls": true,
    "enabled": true
  }
]
```

The password is not there. It is in the keyring under the service
`org.kairos.Calendar`, keyed by `id` — so if you change an `id` by hand, the
password will no longer be found.

Setting `"enabled": false` keeps an account configured but stops Kairos
syncing it.
