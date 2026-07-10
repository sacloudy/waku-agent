"""create_event — the flagship tool. "Did the meeting trigger?" is THE
deterministic eval: it either wrote the right row or it didn't.

Local-first by default: events land in state.db and .jarvis/calendar.ics
(an ICS file any calendar app can import). A real Google Calendar adapter is a
drop-in replacement for `_write_ics` + the DB insert — see docs/architecture.md.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from jarvis.tools.registry import Tool


def _write_ics(home: Path, title: str, start: str, end: str, attendees: str) -> None:
    """Append a minimal VEVENT. ISO timestamps like 2026-07-14T09:00 become
    ICS's compact 20260714T090000 form."""
    ics_path = home / "calendar.ics"

    def dt(s: str) -> str:
        return s.replace("-", "").replace(":", "") + ("00" if len(s) == 16 else "")

    event = (
        "BEGIN:VEVENT\n"
        f"SUMMARY:{title}\n"
        f"DTSTART:{dt(start)}\n"
        f"DTEND:{dt(end)}\n"
        f"DESCRIPTION:attendees: {attendees}\n"
        "END:VEVENT\n"
    )
    if ics_path.exists():
        body = ics_path.read_text().replace("END:VCALENDAR\n", "")
    else:
        body = "BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//launch-jarvis//EN\n"
    ics_path.write_text(body + event + "END:VCALENDAR\n")


def make_tool(conn: sqlite3.Connection, home: Path) -> Tool:
    def create_event(title: str, start: str, end: str = "", attendees: str = "", notes: str = "") -> str:
        if not end:
            # default: one hour
            from datetime import datetime, timedelta
            end = (datetime.fromisoformat(start) + timedelta(hours=1)).isoformat(timespec="minutes")

        # idempotence guard: same title+start = same event. A confused model
        # (or an impatient user) must not be able to triple-book a meeting.
        start = start[:16]  # normalize 2026-07-11T17:00:00 → 2026-07-11T17:00
        end = end[:16]
        existing = conn.execute(
            "SELECT id FROM calendar_events WHERE title = ? AND start = ?", (title, start)
        ).fetchone()
        if existing:
            return (
                f"Event '{title}' at {start} already exists (not duplicated). "
                f"It lives in {home / 'calendar.ics'} — import it with: open {home / 'calendar.ics'}"
            )

        conn.execute(
            'INSERT INTO calendar_events (title, start, "end", attendees, notes) VALUES (?,?,?,?,?)',
            (title, start, end, attendees, notes),
        )
        conn.commit()
        _write_ics(home, title, start, end, attendees)
        return (
            f"Event created: '{title}' {start} → {end}"
            + (f" with {attendees}" if attendees else "")
            + f". Saved to {home / 'calendar.ics'} — import into the calendar app with: open {home / 'calendar.ics'}"
        )

    return Tool(
        name="create_event",
        description=(
            "Create a calendar event on the user's local calendar. Use whenever the user "
            "wants to schedule, book, or plan something at a specific time."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Short event title"},
                "start": {"type": "string", "description": "Start time, ISO 8601, e.g. 2026-07-14T09:00"},
                "end": {"type": "string", "description": "End time, ISO 8601. Defaults to start + 1h."},
                "attendees": {"type": "string", "description": "Comma-separated names/emails"},
                "notes": {"type": "string", "description": "Optional context for the event"},
            },
            "required": ["title", "start"],
        },
        fn=create_event,
    )
